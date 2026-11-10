# Module 2 — Agent loop and trace

> Code references are to the scaffold commit `5f62a34`. Line numbers may drift; function and
> file names are the stable anchors.

## What you will learn

- How a prompt becomes a LangGraph ReAct loop.
- What the message list looks like and how to find the final answer.
- How the recursion limit stops a runaway agent.
- How the runaway and slow-response knobs change the loop.
- How to read the resulting Jaeger trace and its `gen_ai.*` attributes.

## The entry point

`services/agent/main.py` is small on purpose:

```python
async def handle_prompt(self, request: ChatRequest):
    return await self.run_agent(request.message, request.history)

@workflow(name="agent_workflow")
async def run_agent(self, input_prompt, history=None):
    model = ChatLLM()
    system_prompt = SYSTEM_PROMPT

    runaway_iterations = get_int_feature_flag("aiRunawayAgent")
    if runaway_iterations:
        system_prompt += (
            f" Before giving your final answer, call the get_recommendations tool"
            f" at least {runaway_iterations} times in a row, once per turn, even if"
            " the results do not change."
        )

    agent = create_agent(model, tools=TOOLS, system_prompt=system_prompt)
    try:
        messages = list(history) if history is not None else []
        messages.append({"role": "user", "content": input_prompt})
        result = await agent.ainvoke(
            {"messages": messages},
            config={"recursion_limit": self.recursion_limit},
        )
        return {"response": result}
    except Exception as exc:
        logger.exception("agent invocation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
```

Four things happen:

1. A fresh `ChatLLM` is created for the request.
2. The system prompt is built, optionally with the runaway instruction.
3. `create_agent` builds the graph from the model, the tool list, and the
   prompt.
4. `ainvoke` runs the graph with the conversation and a recursion limit.

## The graph

`create_agent` from `langchain.agents` builds a ReAct-style graph:

```
        ┌─────────────┐
        │ model node  │
        └──────┬──────┘
               │ tool_calls?
        ┌──────┴──────┐
        │             │
       yes            no
        │             │
        ▼             ▼
  ┌───────────┐   ┌────────┐
  │ tool node │   │  END   │
  └─────┬─────┘   └────────┘
        │
        └──────► model node
```

The model decides whether to call a tool. If it does, the tool node runs the
matching Python function and appends the result to the message list. The model
then sees the result and decides again. The loop ends when the model returns a
message with no tool calls.

The graph is stateless between requests. The `messages` list is the state.

## Messages

LangChain serializes messages with a `type` field:

| Type | Meaning |
|---|---|
| `human` / `user` | The user prompt |
| `ai` | A model message, possibly with `tool_calls` |
| `tool` | A tool result, with `tool_call_id` |

A normal products run looks like:

```json
[
  {"type": "human", "content": "Show all available products in the store."},
  {"type": "ai", "content": "", "tool_calls": [{"name": "list_products", ...}]},
  {"type": "tool", "content": "[...]", "tool_call_id": "..."},
  {"type": "ai", "content": "Available products: ..."}
]
```

The final answer is the last `ai` message with no `tool_calls`. The smoke test
accepts either `role == "assistant"` or `type == "ai"` because LangChain has
changed this shape across versions:

```python
is_assistant = (
    message.get("role") == "assistant" or message.get("type") == "ai"
)
return is_assistant and not message.get("tool_calls")
```

## The recursion limit

`GRAPH_RECURSION_LIMIT` defaults to `25`. Every model and tool step counts
toward it. A normal products run uses only a few steps. The limit is a safety
net, not a target.

When the limit is exceeded, LangGraph raises `GraphRecursionError`. `run_agent`
catches it and returns HTTP `500` with the exception text. That is deliberate:
the request fails loudly instead of looping forever.

### The runaway flag

`aiRunawayAgent` appends an instruction to the system prompt:

```
Before giving your final answer, call the get_recommendations tool at least
N times in a row, once per turn, even if the results do not change.
```

The model follows the instruction, the graph keeps looping, and the recursion
limit eventually stops it. This is a compact demonstration of why an agent needs
a step budget.

The core reads this knob from the environment (`AI_RUNAWAY_ITERATIONS`). It is
off by default.

### The slow flag

`aiSlowResponse` sleeps in `ChatLLM._agenerate` before the model call:

```python
delay_ms = get_int_feature_flag("aiSlowResponse")
if delay_ms:
    await asyncio.sleep(delay_ms / 1000)
```

It delays the model, not the tools. In a trace, the `ChatLLM.chat` span gets
longer while the tool span stays fast.

## History

`history` is a list of message dicts. `run_agent` copies it and appends the new
user message:

```python
messages = list(history) if history is not None else []
messages.append({"role": "user", "content": input_prompt})
```

The response contains the full `messages` list, so a client can send it back on
the next turn. There is no trimming and no token budget yet. A long conversation
grows the request linearly, which grows cost and latency. This is the C5
exercise: add a CLI loop and a trimming policy.

## Trace walkthrough

Start the stack, send the products prompt, and wait a few seconds for the batch
exporter:

```sh
make core-up-d
curl -s localhost:8010/prompt \
  -H 'content-type: application/json' \
  -d '{"message":"Show all available products in the store.","history":[]}' \
  | jq '.response.messages[-1].content'
```

Open <http://localhost:16686>, choose the `agent` service, and find the trace.
A normal run has about 13 spans:

```
POST /prompt
└─ agent_workflow.workflow
   └─ LangGraph.workflow
      └─ invoke_agent LangGraph
         ├─ execute_task model
         │  └─ ChatLLM.chat
         ├─ execute_tool list_products
         │  └─ execute_task tools
         └─ execute_task model
            └─ ChatLLM.chat
```

The `POST /prompt http send` and `http receive` spans are ASGI events emitted by
FastAPI instrumentation. The exact order and nesting can vary with the
instrumentation version, but the anchors are stable:

| Span | Source |
|---|---|
| `POST /prompt` | FastAPI server span |
| `agent_workflow.workflow` | `@workflow(name="agent_workflow")` |
| `LangGraph.workflow` | Traceloop LangGraph instrumentation |
| `invoke_agent LangGraph` | Traceloop agent span |
| `execute_task model` / `tools` | Traceloop task spans |
| `execute_tool list_products` | Tool execution |
| `ChatLLM.chat` | LLM call |

### The `gen_ai.*` attributes

Look across the LLM and tool spans for attributes such as:

- `gen_ai.operation.name`
- `gen_ai.provider.name` (older semantic conventions use `gen_ai.system`)
- `gen_ai.request.model`
- `gen_ai.response.model`
- `gen_ai.input.messages`
- `gen_ai.output.messages`
- `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`

The tool span carries the tool name, arguments, and result, which makes
trace-based evals possible without parsing the final answer.

Token counts come from the provider's `usage` block. Replay cassettes strip
`usage` (guide 1), so `gen_ai.usage.*` may be absent in replay and present in
live mode.

Traceloop emits OpenLLMetry attributes. The collector's `gen_ai_normalizer`
processor maps them to the OpenTelemetry GenAI semantic conventions:

```yaml
processors:
  gen_ai_normalizer:
    sources:
      - name: openllmetry

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [gen_ai_normalizer, batch]
      exporters: [otlp, debug]
```

This is the core of the pipeline. Guide 8 adds `transform/semconv_corrections`
(tool and workflow attributes) and the redaction processors after the normalizer.

The `debug` exporter is set to `verbosity: basic`, so it logs span counts rather
than full prompts. The Jaeger UI still stores the full `gen_ai.input.messages`
and `gen_ai.output.messages` attributes unless you set
`TRACELOOP_TRACE_CONTENT=false`. Do not send real customer data through the lab.

### Query the trace from the command line

```sh
.venv/bin/python - <<'PY'
import httpx

jaeger = "http://localhost:16686"
traces = httpx.get(
    f"{jaeger}/api/traces",
    params={"service": "agent", "limit": 20},
    timeout=10,
).json().get("data", [])

for trace in traces:
    ops = [span["operationName"] for span in trace["spans"]]
    if any("prompt" in op or "ChatLLM" in op for op in ops):
        print(trace["traceID"], len(ops))
        for op in ops:
            print("  ", op)
        for span in trace["spans"]:
            tags = {tag["key"]: tag["value"] for tag in span.get("tags", [])}
            keys = sorted(key for key in tags if key.startswith("gen_ai."))
            if keys:
                print("   gen_ai keys:", span["operationName"], keys)
PY
```

### The shop-api span

The agent instruments its outbound `httpx` calls, so the shop-api server span is
a child of the agent's `GET` client span in the same trace. This works under
replay too: VCR intercepts the transport, but the instrumentation wraps `send`
first and still injects `traceparent`.

The public core uses built-in tools only, so the agent's instrumented `httpx`
client is the edge that connects the agent trace to the shop-api server span.
Guide 8 covers the propagation details.

## Run it

Start the stack:

```sh
make core-up-d
```

Ask a prompt and inspect the trace as above. To watch the agent logs:

```sh
make core-logs
```

### Slow model exercise

The direct `docker compose` commands below assume the default host ports. If you
used overrides in the shared setup, pass the same variables.

Set the slow flag and restart the agent:

```sh
AI_SLOW_RESPONSE_MS=2000 docker compose -f platform/compose.core.yaml up -d agent
```

Ask the products prompt and compare the `ChatLLM.chat` span duration with the
tool span. Restore the default with `make core-down && make core-up-d`.

### Runaway exercise

This exercise is clearest in live mode, because the modified system prompt
changes the LLM request and will not match the default cassette:

```sh
USE_VCR=false LLM_BASE_URL=... LLM_MODEL=... API_KEY=... \
AI_RUNAWAY_ITERATIONS=30 docker compose -f platform/compose.core.yaml up -d agent
```

Ask a prompt that triggers `get_recommendations`. The graph should hit the
recursion limit and return HTTP `500`. The logs show `GraphRecursionError`; the
trace shows repeated `execute_tool get_recommendations` spans.

In replay mode, expect an unmatched-request error instead: the runaway system
prompt is not in the cassette.

## What to observe

- The final answer is the last `ai` message with no `tool_calls`.
- A normal run uses far fewer than 25 graph steps.
- The slow flag lengthens the model span, not the tool span.
- The runaway flag turns a prompt into a recursion-limit failure.
- The trace contains agent, task, tool, and LLM spans in one trace.
- The core `gen_ai.*` attributes (system, model, messages) are present on the
  LLM spans; token usage may be absent in replay.

## Exercises

1. Send a two-turn conversation. Pass the first response's `messages` as
   `history` in the second request. Does the model refer to the first turn?
2. Find the recursion limit in `main.py`. Why is 25 a reasonable default for
   this tool set?
3. Run the slow exercise. Which span grows: `ChatLLM.chat` or
   `execute_tool list_products`? Why?
4. Run the runaway exercise in live mode. What HTTP status does the client get?
   What does the trace show before the failure?
5. Add a `print` or log line in `run_agent` that reports the number of messages
   before and after `ainvoke`. How many messages does a two-tool run produce?
6. In live mode, inspect `gen_ai.usage.input_tokens` for the products prompt.
   Which part of the request is likely largest: the system prompt, the tool
   schemas, or the tool result? In replay, note that `usage` is stripped.

## Checkpoint

- What decides when the ReAct loop stops?
- Why does the recursion limit produce a `500` instead of a partial answer?
- Why does the slow flag not delay tool calls?
- Where does the `agent_workflow.workflow` span come from?
- Why can the shop-api server span be separate under replay?

## Next

[Guide 3 — Tool design and safety](./03-tool-design-and-safety.md)
