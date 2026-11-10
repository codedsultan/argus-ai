# Agent service

`POST /prompt` runs a LangChain ReAct loop over the tools in
`domain/tools.py`.

## Request

```json
{"message": "Show all available products in the store.", "history": []}
```

`history` is the full message list returned by the previous response. The agent
is stateless; the caller owns the conversation. See
[module guide 5](../docs/modules/05-session-history-and-web-client.md).

## Response

The LangGraph message list, per-request cost counters, and the trace ID:

```json
{
  "response": {"messages": []},
  "metrics": {
    "messages": 4,
    "llm_calls": 2,
    "tool_calls": 1,
    "estimated_tokens": 412
  },
  "trace_id": "3d026765deacc4facf4d486a87531400"
}
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APPLICATION_ENDPOINT` | `localhost:8080` | Shop API host:port |
| `LLM_BASE_URL` | unset | OpenAI-compatible base URL |
| `LLM_MODEL` | `azure/gpt-5.5` | Model name and cassette key |
| `API_KEY` | `sk-dummy` | Provider key; not needed in replay |
| `USE_VCR` | `true` | Replay recorded LLM responses |
| `VCR_CASSETTE_DIR` | `evals/cassettes` | Cassette directory |
| `VCR_MATCH_THRESHOLD` | `0.85` | Fuzzy request-match threshold |
| `GRAPH_RECURSION_LIMIT` | `25` | Agent loop ceiling |
| `HISTORY_MAX_TOKENS` | `8000` | Approximate history budget; `0` disables trimming |
| `HISTORY_MIN_RECENT` | `2` | Minimum newest messages to keep |
| `AI_SLOW_RESPONSE_MS` | `0` | Optional delay before each LLM call |
| `AI_RUNAWAY_ITERATIONS` | `0` | Force repeated tool calls for a runaway demo |
| `OTEL_ENABLED` | `true` | Enable tracing |
| `OTEL_SERVICE_NAME` | `agent` | Service name in traces |
| `TRACELOOP_TRACE_CONTENT` | `true` | Record prompt/completion content; set `false` for privacy |

## Layout

```
main.py            FastAPI surface and agent loop
llm.py             ChatLLM boundary and replay switch
replay.py          VCR fuzzy matcher and best-match playback
history.py         Token estimate and history trimming
tools_builtin.py   LangChain wrappers around domain tools
flags.py           Environment-backed slow/runaway knobs
telemetry.py       Traceloop, FastAPI, and httpx instrumentation
```

The public core uses built-in tools only. Experimental transports and
fault-injection selectors are not part of this profile.
