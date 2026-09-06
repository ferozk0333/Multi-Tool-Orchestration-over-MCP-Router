# Multi-Tool Agentic Orchestration over MCP

In this project, I orchestrate 504 tools across 11 custom MCP servers using dense embedding and BM25-based retrieval. Connect a few MCP servers and the tool count quickly outgrows the prompt. All 504 schemas are 60k tokens in context, which means higher latency, degraded selection accuracy and hallucinations. With retrieval, that drops to 1.8k tokens per query. The goal is to optimize correct tool retrieval keeping cost and latency at minimum.

<!-- TODO: screen recording of the trace running goes here, before any prose. -->

## How It Works
The model starts each conversation with zero catalogue tools, plus two system tools: `request_more_tools` and `ask_clarification`. The agent calls the tools, chains them across MCP servers, asks user a question when the request is unclear, and asks for different tools when the retrieved ones are wrong.

From there, every turn is the same decision:

<img width="550" height="620" alt="Screenshot 2026-09-06 at 2 45 37 PM" src="https://github.com/user-attachments/assets/578d86ce-6963-40ce-be97-445a11a82d0a" />



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

| Criteria | Results |
|---|---|
| Prompt tokens, 504 schemas vs routed 12 | 59,394 -> 1,822 (**96.9% saved**) |
| recall@12 on 60 labelled queries | dense **91.7%**, hybrid 85.0%, BM25 70.0% |
| Single tool query, end-to-end | 5 to 7 seconds |

## Future Work

As a follow-up step, I plan to move this project to production using AWS Services.

**Servers to ECS Fargate over Streamable HTTP.** 
Each local MCP server becomes its own Fargate service behind an internal ALB so they scale and fail
independently.

**Index to OpenSearch Serverless.** The embeddings are a NumPy matrix loaded into every process,
which does not survive more than one API instance. A shared vector index fixes that, and moving
embeddings to Bedrock drops a lot of boilerplate code. I would also use an ALB behind an API Gateway as entry point, CloudWatch for metrics, logs and alarms, and other scalable services.

**Evaluations.** I plan to evaluate the application more systematically by performing 1) component-level evals (tool correctness, tool argument correctness), 2) pipeline-level evals where all components are tied together and finally 3) application-level evals where I test latency, TTFT, bias, tone etc. I would log the results of each run on an experiment tracking service like MLflow and visualize the metrics of interest.

