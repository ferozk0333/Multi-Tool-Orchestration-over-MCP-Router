# Multi-Tool Agentic Orchestration over MCP

In this project, I orchestrate 504 tools across 11 custom MCP servers using dense embedding and BM25-based retrieval methods. If you connect a few MCP servers, the tool count outgrows the prompt very quickly. Sending all of them to the model every time is inefficient. In fact, all 504 schemas are approx 60k tokens in context, which leads to latency issues, degraded selection accuracy and even hallucinations. Using the proposed retrieval mechanism, I found that the context window token usage drops from 60k to 1.8k tokens on average for tool schemas.

<!-- TODO: screen recording of the trace running goes here, before any prose. -->

## How It Works
The model starts each conversation with zero catalogue tools, plus two system tools: `request_more_tools` and `ask_clarification`. The agent calls the tools, chains them across MCP servers, asks user a question when the request is unclear, and asks for different tools when the retrieved ones are wrong.

From there, every turn is the same decision:

model </br>
 ├─ need tools?      -> router retrieves top 12 -> back to the model </br>
 ├─ need info?       -> ask the user -> wait </br>
 ├─ ready to act?    -> call tools -> results back to the model </br>
 └─ done?            -> answer </br>

<img width="686" height="841" alt="Screenshot 2026-09-06 at 1 19 17 PM" src="https://github.com/user-attachments/assets/26264cc6-84f1-4b6b-91dd-45e9022ce523" />


## Key Components

**Router.** It is not a model, just a search index. Dense embeddings search across tool schemas in about 7ms.

**Agent loop.** The model starts with no tools. If it can answer from the conversation it just
answers, and no retrieval happens. If it needs data, it says what capability it needs, and the
router fetches tools for that.

**Tool execution.** The pool holds all 11 servers over stdio and dispatches calls concurrently.

## Production concerns

- **Conditional retries.** Timeouts retry, validation errors do not, since they fail the same way
  every time.
- **Iteration cap.** At 10 turns it stops and answers from what it gathered.
- **Concurrency.** Independent calls in one turn run together, capped at 5.

## Results

| | |
|---|---|
| Prompt tokens, 504 schemas vs routed 12 | 59,394 -> 1,822 (**96.9% saved**) |
| recall@12 on 60 labelled queries | dense **91.7%**, hybrid 85.0%, BM25 70.0% |
| Single tool query, end-to-end | 5 to 7 seconds |

## Future Work

As a follow-up step, I plan to move this project to production using AWS Services.

**Servers to ECS Fargate over Streamable HTTP.** 
Each local MCP server becomes its own Fargate service behind an internal ALB so they scale and fail
independently. The pool already handles a server going away.

**Index to OpenSearch Serverless.** The embeddings are a numpy matrix loaded into every process,
which does not survive more than one API instance. A shared vector index fixes that, and moving
embeddings to Bedrock drops a lot of boiler plate code.

**Session state to DynamoDB, credentials to Secrets Manager.** Conversations live in a Python
dict today, so a restart loses them and you cannot run two instances. The API would run on
Fargate behind an ALB, with model calls going to Bedrock so everything stays in one VPC.
