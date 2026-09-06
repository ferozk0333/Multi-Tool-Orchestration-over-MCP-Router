# Multi-Tool Agentic Orchestration over MCP

11 MCP servers, 504 tools, and a client that routes over them.

<!-- TODO: screen recording of the trace running goes here, before any prose. -->

## What it does

Connect a few MCP servers and you quickly have hundreds of tools. Sending all of them to the
model every time is wasteful. This retrieves only the dozen tools that match your question and
sends those.

**504 tools in the catalogue. 12 go to the model.**

It also calls the tools, chains them across servers, asks you a question when the request is
unclear, and asks for different tools when the retrieved ones are wrong.

## Why routing

Here is `mcp.json`, the same shape Claude Desktop uses:

```json
{
  "mcpServers": {
    "github": { "command": "python", "args": ["-m", "servers.github"] },
    "slack":  { "command": "python", "args": ["-m", "servers.slack"] }
  }
}
```

Eleven of those gives 504 tools, which is 59,394 tokens of schemas. It fits in the context
window, but you pay it on every call before the user has asked anything, and the model picks
from 504 options instead of 12.

## How it works

```
user message
     │
     ▼
┌──────────────────────────────────────────────────────────┐
│ AGENT LOOP                              max 10 turns     │
│                                                          │
│   ┌────────────────────────────────────────┐             │
│   │ MODEL                                  │◀─────────┐  │
│   │ conversation + tools loaded so far     │          │  │
│   │ + request_more_tools                   │          │  │
│   │ + ask_clarification                    │          │  │
│   └────────────────────────────────────────┘          │  │
│      │           │                   │                │  │
│  plain answer  ask_clarification  request_more_tools  │  │
│      │           │                   │                │  │
│      ▼           ▼                   ▼                │  │
│   ANSWER   question to user    ┌──────────┐           │  │
│                                │  ROUTER  │ ~7ms      │  │
│                                │ 504 → 12 │           │  │
│                                └──────────┘           │  │
│                                     │ schemas ────────┘  │
│      │                                                   │
│  tool calls                                              │
│      ▼                                                   │
│  ┌─────────────────┐                                     │
│  │ MCP CLIENT POOL │ concurrent, with retries            │
│  └─────────────────┘                                     │
│      │ results ───────────────────────────────────────┐  │
└──────┼────────────────────────────────────────────────┼──┘
       ▼                                                │
  11 stdio MCP servers ──────────────────── back to the model
```

**Router.** Not a model, just a local search index. Dense embeddings with `bge-small-en-v1.5`,
about 7ms, no API cost.

**Agent loop.** The model starts with no tools. If it can answer from the conversation it just
answers, and no retrieval happens. If it needs data it says what capability it needs, and the
router fetches tools for that. Those tools stay loaded for the rest of the conversation.

**Tool execution.** The pool holds all 11 servers over stdio, dispatches calls concurrently, and
returns failures as data so the model can correct itself.

## Recovering from a bad route

Retrieval is not perfect, so what happens when the model gets the wrong twelve tools?

The obvious fix is to widen the search when the model says it cannot help. But that only catches
refusals, and models rarely refuse. Give one only Slack tools and ask a GitHub question, and it
will call a Slack tool and report the useless result as an answer.

So the model gets a tool called `request_more_tools`. It is in no server's catalogue and is never
executed. Its only job is to let the model say "wrong toolbox" in its own words:

```
12 tools retrieved     slack 10 · github 2                       2.0k tokens
slack   conversations_info                                            15 ms

Wrong toolbox   widened 12 → 46
  └ GitHub: list pull requests for a repository, open state, sorted by date

46 tools retrieved     github 28 · slack 13 · twilio 2 · hr 1       6.8k tokens
github  pulls_list                                                    12 ms
slack   chat_post_message                                              4 ms
```

Two things that caught me out. Widening must only add tools: my first version re-ranked at a
bigger `k` and dropped `slack_chat_post_message`, so the model found the PR and had nothing to
post it with. And retrieving on what the model asks for beats retrieving on the raw question. For
the query above `github_pulls_list` does not appear even at k=40, but comes back at rank 3 when
searched on the model's own description.

`ask_clarification` works the same way. Before it, I was checking whether the answer ended with a
"?", which worked until a real question ended with ")".

## Production concerns

- **Conditional retries.** Timeouts retry, validation errors do not, since they fail the same way
  every time.
- **Iteration cap.** At 10 turns it stops and answers from what it gathered, and says it was cut
  short. Never an error page.
- **Errors are data.** A failed call goes back to the model as a tool result, not an exception.
- **Bounded concurrency.** Independent calls in one turn run together, capped at 5.
- **A server dying does not kill the app.** It is marked unavailable, its tools drop out of the
  index, and a background task retries it.
- **Failure injection.** Some tools are set to time out or reject arguments, so the retry paths
  actually run instead of being assumed.

## Numbers

| | |
|---|---|
| Prompt tokens, 504 schemas vs routed 12 | 59,394 → 1,822 (**96.9% saved**) |
| recall@12 on 60 labelled queries | dense **91.7%**, hybrid 85.0%, BM25 70.0% |
| Single tool query, end to end | 5 to 7 seconds |
| 11 servers connected, 504 tools aggregated | 0.7s |
| Retrieval search, warm | 7ms |

One result went against the design. I built the usual BM25 plus dense hybrid with reciprocal rank
fusion, and measured it was **worse** than dense alone: BM25 added nothing, since all three score
100% on lexical queries. RRF reads ranks, not scores, so a weak wrong match votes as hard as a
strong right one. The default is dense now.

## Limitations

The tools are mocked. The MCP protocol and the orchestration are real, the integrations are not.
Schemas come from the actual GitHub, Slack, Stripe and Twilio OpenAPI specs, but responses come
from seeded in-memory state.

220 of the 504 tools are synthetic. The eval set is generated, 60 queries with 25% hand-checked.
stdio transport only, and conversations are lost on restart.

## Running it

Needs Python 3.10+, Node 18+, and an Anthropic API key.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r pyproject.toml
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

set -a; . ./.env; set +a
.venv/bin/python -m server.app          # backend on :8000

cd frontend && npm install && npm run dev   # UI on :5173
```

First start takes about 10 seconds to spawn 11 subprocesses and embed 504 tools. Embeddings are
cached after that. Model is `claude-sonnet-5`, a demo run costs a few cents.

```bash
.venv/bin/python -m pytest tests/ -q      # retrieval unit tests
.venv/bin/python scripts/smoke_pool.py    # 11 servers, degraded path, reconnect
.venv/bin/python scripts/smoke_agent.py   # six scenarios, costs API calls
.venv/bin/python evals/report.py          # recall@12 table
```

Two queries to try. Simple:

```
what are the open pull requests on acme/api-server?
```

Clarification then a four call chain. Answer `acme/api-server` when it asks:

```
Find my newest open PR, post a one-line summary to the eng channel, then pin
that message and add an eyes reaction to it.
```

## What I would do next

**Servers to ECS Fargate over Streamable HTTP.** Eleven local subprocesses is fine for a demo.
Each server becomes its own Fargate service behind an internal ALB so they scale and fail
independently. The pool already handles a server going away.

**Index to OpenSearch Serverless.** The embeddings are a numpy matrix loaded into every process,
which does not survive more than one API instance. A shared vector index fixes that, and moving
embeddings to Bedrock drops the 900MB torch dependency.

**Session state to DynamoDB, credentials to Secrets Manager.** Conversations live in a Python
dict today, so a restart loses them and you cannot run two instances. The API would run on
Fargate behind an ALB, with model calls going to Bedrock so everything stays in one VPC.
