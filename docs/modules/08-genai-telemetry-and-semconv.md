# 8 — GenAI telemetry and semantic conventions

**Concept:** instrument the agent once, then normalize the result into the
GenAI semantic conventions.
**Code:** `services/agent/telemetry.py`, `platform/collector/config.yaml`,
`evals/test_telemetry.py`.

## What you will learn

- The path from a service SDK to Jaeger: OTLP, the collector, and the exporter.
- Why OpenLLMetry (`traceloop.*`, `llm.*`) is not the GenAI semantic convention.
- What `gen_ai_normalizer` renames, what it leaves alone, and why the shapes
  still differ.
- The two gaps the lab corrects: tool names and workflow names.
- How W3C trace context crosses the shop API, and why client instrumentation is
  the part that usually breaks.
- What collector-side redaction can mask, and what it cannot.
- How to test the schema instead of only checking that spans exist.

## The pipeline

Every service exports OTLP to the collector. The collector normalizes, corrects,
redacts, and batches before exporting to Jaeger and the debug log.

```text
agent / shop-api
  │  OTLP/HTTP :4318 (or OTLP/gRPC :4317)
  ▼
otel-collector
  ├─ gen_ai_normalizer              OpenLLMetry → gen_ai.*
  ├─ transform/semconv_corrections  tool/workflow attribute fixes
  ├─ transform/redact_sensitive_data card-shaped numbers
  ├─ redaction                      key-name safety net
  └─ batch
       ├─ Jaeger :4317
       └─ debug (collector logs)
```

The config is [`platform/collector/config.yaml`](../../platform/collector/config.yaml).
The services point at `http://otel-collector:4318` in Compose, or
`http://localhost:4318` when you run them locally. The agent sets both
`OTEL_EXPORTER_OTLP_ENDPOINT` and `TRACELOOP_BASE_URL`; Traceloop uses its own
variable, so setting only the standard one leaves half the spans unsent.

The collector is exposed on the host as `OTEL_HTTP_PORT` (default `4318`) and
`OTEL_GRPC_PORT` (default `4317`). That is what lets the telemetry test send a
synthetic span without going through the agent.

## Two vocabularies

OpenLLMetry predates the GenAI semantic conventions. It emits attributes such as
`traceloop.span.kind`, `traceloop.entity.name`, and `llm.request.model`. The
convention uses `gen_ai.operation.name`, `gen_ai.agent.name`, and
`gen_ai.request.model`.

The normalizer translates the common ones:

| OpenLLMetry | GenAI convention | Notes |
|---|---|---|
| `traceloop.span.kind` | `gen_ai.operation.name` | `workflow` → `invoke_workflow`, `task`/`agent` → `invoke_agent`, `tool` → `execute_tool` |
| `traceloop.entity.name` | `gen_ai.agent.name` | Correct for workflow/agent spans; misleading on tool spans |
| `traceloop.entity.input` | `gen_ai.input.messages` | Shape is preserved, not converted |
| `traceloop.entity.output` | `gen_ai.output.messages` | Shape is preserved, not converted |
| `llm.request.model` | `gen_ai.request.model` | |
| `llm.response.model` | `gen_ai.response.model` | |
| `llm.request.type` | `gen_ai.operation.name` | `chat` → `chat`, `completion` → `text_completion` |
| `llm.request.functions` | `gen_ai.tool.definitions` | Source shape preserved |
| `llm.response.finish_reason` | `gen_ai.response.finish_reasons` | String wrapped into a one-element array |
| `llm.usage.prompt_tokens` | `gen_ai.usage.input_tokens` | Absent in replay; cassettes strip `usage` |
| `llm.usage.completion_tokens` | `gen_ai.usage.output_tokens` | Absent in replay |

The lab runs the normalizer with its defaults:

```yaml
gen_ai_normalizer:
  sources:
    - name: openllmetry
```

Two defaults matter:

- `remove_originals: false` keeps `traceloop.*` and `llm.*`. A trace shows both
  vocabularies while you learn the mapping. Set it to `true` to cut storage and
  stop exporting the originals.
- `overwrite: false` keeps an existing `gen_ai.operation.name`. That is why the
  LangChain instrumentation's `execute_task` survives even though the
  normalizer would map `traceloop.span.kind=task` to `invoke_agent`.

Some attributes do not come from the normalizer at all. They are emitted
directly by the instrumentation:

- `gen_ai.system_instructions` — the system prompt on `ChatLLM.chat`.
- `gen_ai.provider.name` — `langchain` on the chat span, `langgraph` on the
  tool span.
- `gen_ai.tool.name` — set on `execute_tool`.
- `gen_ai.tool.definitions` — the tool schemas on the chat span.

## What the normalizer does not do

The normalizer **renames attributes**. It does not parse, reshape, or validate
their values. That is the single most important lesson in this module.

`gen_ai.input.messages` has at least five shapes in one trace:

| Span | Shape of `gen_ai.input.messages` |
|---|---|
| `ChatLLM.chat` | `[{"role": "user", "parts": [...]}]` — canonical semconv |
| `agent_workflow.workflow` | `{"args": [...], "kwargs": {}}` — Traceloop entity input |
| `LangGraph.workflow` | `{"inputs": ..., "tags": ..., "metadata": ..., "kwargs": ...}` |
| `execute_task` | `{"inputs": ...}` / `{"outputs": ...}` |
| `execute_tool` | `{"input_str": "{}", ...}` / `{"output": ...}` |

A consumer that assumes `gen_ai.input.messages` is always the canonical list
will break on four of those. Target the span you mean — usually
`ChatLLM.chat` — and validate the shape before parsing.

`gen_ai.operation.name` has the same problem. The values in the public core are:

```text
invoke_workflow   agent_workflow.workflow
invoke_agent      invoke_agent LangGraph, LangGraph.workflow
execute_task      execute_task model, execute_task tools   ← not a GenAI enum
chat              ChatLLM.chat
execute_tool      execute_tool list_products
```

`execute_task` is a LangChain concept, not a GenAI semantic convention. The
normalizer would map the Traceloop `task` kind to `invoke_agent`, but
`overwrite: false` preserves the more specific value already on the span. A
backend that indexes by `gen_ai.operation.name` should either set
`overwrite: true` or add a transform. The lab keeps the gap visible because it
is a real interoperability problem, not a bug in the lab.

The `gen_ai.usage.*` attributes are also absent in replay. `clean_response()`
strips `usage` from the cassette, so the normalizer has nothing to map. In live
mode, the mapping appears:

```text
llm.usage.prompt_tokens     → gen_ai.usage.input_tokens
llm.usage.completion_tokens → gen_ai.usage.output_tokens
```

The per-request `metrics` (`estimated_tokens`, `llm_calls`, `tool_calls`) are
the lab's cost proxy. They are not provider billing data.

## The two corrections

The normalizer's `traceloop.entity.name` → `gen_ai.agent.name` mapping is right
for workflow and agent spans, but wrong for tool spans: the entity name is the
tool, not an agent. The collector fixes both the tool name and the workflow
name:

```yaml
transform/semconv_corrections:
  error_mode: ignore
  trace_statements:
    - context: span
      statements:
        - set(span.attributes["gen_ai.tool.name"], span.attributes["gen_ai.agent.name"]) where span.attributes["gen_ai.operation.name"] == "execute_tool" and span.attributes["gen_ai.tool.name"] == nil and span.attributes["gen_ai.agent.name"] != nil
        - delete_key(span.attributes, "gen_ai.agent.name") where span.attributes["gen_ai.operation.name"] == "execute_tool"
        - set(span.attributes["gen_ai.workflow.name"], span.attributes["traceloop.workflow.name"]) where span.attributes["gen_ai.operation.name"] == "invoke_workflow" and span.attributes["traceloop.workflow.name"] != nil
```

After the correction:

```text
execute_tool list_products   gen_ai.tool.name=list_products   no gen_ai.agent.name
agent_workflow.workflow      gen_ai.workflow.name=agent_workflow
```

The first statement is conditional on `gen_ai.tool.name == nil`. The built-in
`execute_tool` span already has the right tool name, so the copy is a fallback.
The second statement removes the misleading agent name from every tool span.

The workflow correction uses `traceloop.workflow.name`, not
`gen_ai.agent.name`. Both are `agent_workflow` on this span, but the OpenLLMetry
original is the more direct source. The normalizer has no
`gen_ai.workflow.name` mapping.

## What about span names?

The collector config calls:

```yaml
set_semconv_span_name("1.43.0", "original_span_name")
```

It is tempting to expect it to rename `ChatLLM.chat` to `chat azure/gpt-5.5`.
It does not. In contrib `0.159.0`, `set_semconv_span_name` derives names for
HTTP, RPC, database, and messaging spans. It has no GenAI rules. The lab's HTTP
spans already use the `METHOD route` form, so the function would be a no-op
here, and the public core does not include it.

If you need GenAI span names, write the rules yourself. The convention is:

```text
chat {gen_ai.request.model}
execute_tool {gen_ai.tool.name}
invoke_agent {gen_ai.agent.name}
invoke_workflow {gen_ai.workflow.name}
```

A transform can do it, but decide first whether you want to lose the
instrumentation's original names. The lab keeps the original names and uses
attributes for queries. That is usually the better trade: names are for humans,
attributes are for machines.

## Trace topology

One prompt produces one trace:

```text
POST /prompt (agent server)
└─ agent_workflow.workflow (invoke_workflow)
   └─ invoke_agent LangGraph
      └─ LangGraph.workflow
         ├─ execute_task model
         │  └─ ChatLLM.chat
         ├─ execute_task tools
         │  └─ execute_tool list_products
         │     └─ GET (agent httpx client)
         │        └─ GET /api/products (shop-api server)
         └─ execute_task model
            └─ ChatLLM.chat
```

The important detail is the bridge between services: **the shop-api span is a
child of the agent's HTTP client span.** Without client instrumentation, the
outbound call has no `traceparent`, and the shop-api starts a new trace. The
client span is the bridge.

The instrumentation may add `POST /prompt http send` and `POST /prompt http
receive` spans around the server span. They are transport spans, not part of the
agent's reasoning loop. Filter by service and operation name when you query.

## Trace propagation

The W3C propagator is the mechanism. The server side (FastAPI) and the client
side (`httpx`) both need instrumentation.

```python
Traceloop.init(app_name=os.getenv("OTEL_SERVICE_NAME", "agent"))
FastAPIInstrumentor.instrument_app(app)

httpx_instrumentor = HTTPXClientInstrumentor()
if not httpx_instrumentor.is_instrumented_by_opentelemetry:
    httpx_instrumentor.instrument()
```

`HTTPXClientInstrumentor` wraps `httpx`'s transport. VCR also intercepts the
transport, but the instrumentation wraps `send` first, so the header is still
injected and the shop-api still joins the trace. Replay does not break
propagation.

## Redaction and privacy

The lab records full prompt and tool payloads by default. That is useful for
learning, and it is exactly what you must not do with real customer data. The
collector adds a safety net:

```yaml
transform/redact_sensitive_data:
  error_mode: ignore
  trace_statements:
    - context: span
      statements:
        - replace_pattern(span.attributes["gen_ai.tool.call.arguments"], "\\b\\d{4}-\\d{4}-\\d{4}-\\d{4}\\b", "****-****-****-****") where span.attributes["gen_ai.tool.call.arguments"] != nil
        - replace_pattern(span.attributes["gen_ai.tool.call.result"], "\\b\\d{4}-\\d{4}-\\d{4}-\\d{4}\\b", "****-****-****-****") where span.attributes["gen_ai.tool.call.result"] != nil
        - replace_pattern(span.attributes["gen_ai.input.messages"], "\\b\\d{4}-\\d{4}-\\d{4}-\\d{4}\\b", "****-****-****-****") where span.attributes["gen_ai.input.messages"] != nil
        - replace_pattern(span.attributes["gen_ai.output.messages"], "\\b\\d{4}-\\d{4}-\\d{4}-\\d{4}\\b", "****-****-****-****") where span.attributes["gen_ai.output.messages"] != nil
        - replace_pattern(span.attributes["traceloop.entity.input"], "\\b\\d{4}-\\d{4}-\\d{4}-\\d{4}\\b", "****-****-****-****") where span.attributes["traceloop.entity.input"] != nil
        - replace_pattern(span.attributes["traceloop.entity.output"], "\\b\\d{4}-\\d{4}-\\d{4}-\\d{4}\\b", "****-****-****-****") where span.attributes["traceloop.entity.output"] != nil
```

The `redaction` processor adds a key-name net:

```yaml
redaction:
  allow_all_keys: true
  blocked_key_patterns:
    - "^password$"
    - "^secret$"
    - "^api[_-]?key$"
    - "^authorization$"
  summary: info
```

`allow_all_keys: true` is important. Without it, the processor drops every
attribute not on its allow list, which would strip the GenAI attributes too.

The synthetic-span test proves the behavior:

```text
gen_ai.tool.call.arguments  {"creditCardNumber": "****-****-****-****", "creditCardCvv": 672}
gen_ai.input.messages       [{"role": "user", "parts": [{"type": "text", "content": "card ****-****-****-****"}]}]
password                    ****
authorization               ****
gen_ai.agent.name           (deleted by the correction)
```

### What redaction misses

- **Nested or escaped JSON.** The checkout tool's arguments are a JSON string
  inside `input_str`; the card number is plain text inside it, so the pattern
  matches. A CVV is a three-digit number and survives.
- **Values that do not look like cards.** Emails, phone numbers, and names need
  their own patterns. The key-name net only catches keys that literally match
  the patterns.
- **Any attribute the transform does not list.** New instrumentation versions
  add attributes; a new content attribute is unredacted until you add it.

The stronger control is to not collect the content in the first place. Traceloop
reads `TRACELOOP_TRACE_CONTENT`:

```sh
TRACELOOP_TRACE_CONTENT=false
```

With it set, the same prompt produces a trace with:

- no `gen_ai.input.messages` / `gen_ai.output.messages`,
- no `traceloop.entity.input` / `traceloop.entity.output`,
- no `gen_ai.system_instructions`,
- no `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result`.

Operation names, models, finish reasons, tool names, and tool schemas remain.
That is enough for latency, topology, and tool-usage evals, and it is a much
better privacy story than regex masking. Use collector redaction as the second
line of defense, not the first.

## Replay versus live

The schema test runs in replay, so some attributes differ from a live provider:

| Attribute | Replay | Live |
|---|---|---|
| `gen_ai.usage.input_tokens` / `output_tokens` | absent | mapped from `llm.usage.*` |
| `gen_ai.response.id` | `chatcmpl-fixture` | provider response ID |
| `gen_ai.provider.name` | `langchain` (instrumentation) | `langchain`; provider-specific attributes may appear |
| Tool results | recorded fixtures | live shop API |
| Prompt/completion content | recorded fixtures | provider output |

The test asserts the shape, not the token counts. If you run it in live mode,
add an assertion for `gen_ai.usage.*`; in replay, assert that it is absent and
explain why.

## Testing the schema

[`evals/test_telemetry.py`](../../evals/test_telemetry.py) has two tests.

### `test_agent_trace_has_genai_semconv_and_propagation`

It sends a prompt, polls Jaeger for the new trace, and asserts:

- the trace contains `agent` and `shop-api`;
- `ChatLLM.chat` has `gen_ai.operation.name=chat`, a request model, and
  list-shaped `gen_ai.input.messages` / `gen_ai.output.messages`;
- `execute_tool list_products` has `gen_ai.tool.name=list_products`, no
  `gen_ai.agent.name`, and both call arguments and result;
- `agent_workflow.workflow` has `gen_ai.operation.name=invoke_workflow`,
  `gen_ai.workflow.name=agent_workflow`, `traceloop.span.kind=workflow`, and
  dict-shaped `gen_ai.input.messages`.

The shape assertions are the point. A test that only checks span names passes
even when the message payload is unparseable.

### `test_collector_redacts_and_corrects_synthetic_span`

It sends a synthetic OTLP span directly to the collector, so it does not need a
checkout cassette. The span carries:

- `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name=checkout`,
  `gen_ai.agent.name=checkout`;
- a card-shaped number in `gen_ai.tool.call.arguments` and
  `gen_ai.input.messages`;
- `password` and `authorization` attributes;
- a unique `test.run_id` so the test finds its own span.

It then asserts that the collector masked the card and the credentials, deleted
the tool's `gen_ai.agent.name`, and kept `gen_ai.tool.name`.

Run the suite with the stack up:

```sh
make core-up-d
make core-v1-test
```

If you override the host ports, pass the matching URLs:

```sh
AGENT_URL=http://localhost:8012 \
JAEGER_URL=http://localhost:16687 \
COLLECTOR_HTTP_URL=http://localhost:4318 \
make core-v1-test
```

The telemetry tests skip when Jaeger or the collector is not reachable. The
synthetic test needs the collector's OTLP HTTP port published, which
`compose.core.yaml` does through `OTEL_HTTP_PORT`.

Current counts:

| Environment | Result |
|---|---|
| No stack | 28 passed, 6 skipped |
| Core stack | 34 passed |

## Exercises

1. Open the normalizer's README and compare its mapping table with the
   attributes in a trace. Pick one `traceloop.*` attribute that has no
   `gen_ai.*` counterpart. Would you add a mapping, a transform, or leave it?
2. Set `overwrite: true` in `gen_ai_normalizer` and send a prompt. What happens
   to `execute_task`? Is `invoke_agent` a better name for a LangGraph node?
   What breaks if a dashboard groups by `gen_ai.operation.name`?
3. Remove `transform/semconv_corrections` and inspect the tool spans. Which
   attribute is misleading? Re-add the transform.
4. Set `remove_originals: true`. Which attributes disappear? What do you lose
   for debugging? What do you gain for storage and privacy?
5. Send a checkout prompt in live mode (or record a new cassette). Inspect
   `gen_ai.tool.call.arguments`. Is the card number masked? Is the CVV? Add a
   CVV pattern and check whether the inner JSON is still parseable. Why is
   nested JSON hard to redact with a regex?
6. Set `TRACELOOP_TRACE_CONTENT=false` on the agent and send a prompt. Which
   attributes disappear? Which tests still work? Which is the better privacy
   control: collector redaction or not collecting content?
7. Remove `HTTPXClientInstrumentor` from the agent and send a prompt. How many
   traces appear in Jaeger? What is the parent of the shop-api span? Re-add it.
8. Add `set_semconv_span_name("1.43.0", "original_span_name")` to the collector.
   Which spans change? Why do the GenAI spans not change? What transform would
   rename them?
9. Write a live-mode assertion for `gen_ai.usage.input_tokens` and
   `gen_ai.usage.output_tokens`. Why does replay not have them? What would you
   assert in replay instead?

## Checkpoint

- What are the two vocabularies, and which processor translates between them?
- What does the normalizer rename, and what does it not change?
- Why is `gen_ai.input.messages` a list on `ChatLLM.chat` but an object on
  `agent_workflow.workflow`?
- Why does `execute_task` survive as an operation name? How would you enforce
  the enum?
- Which two attributes does the lab correct, and why?
- Why did the shop-api span start as a separate trace before client
  instrumentation? What fixed it?
- What does the collector redaction mask, and what does it miss?
- Why is `gen_ai.usage.*` absent in replay?
- What does the telemetry test assert that a span-name check does not?

## Next

Run the public frontend against the core and inspect the trace link it renders:
[argus-frontend](https://github.com/codedsultan/argus-frontend).

Before exposing the agent, read [the hardening roadmap](../hardening.md).

## References

- [`platform/collector/config.yaml`](../../platform/collector/config.yaml)
- [`services/agent/telemetry.py`](../../services/agent/telemetry.py)
- [`evals/test_telemetry.py`](../../evals/test_telemetry.py)
- [Module 2 — the agent loop and trace](02-agent-loop-and-trace.md)
- [Module 6 — determinism and replay](06-determinism-and-replay.md)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Collector `gen_ai_normalizer`](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/genainormalizerprocessor)
- [Collector `transform` processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/transformprocessor)
