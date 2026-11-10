# 5 — History and the web client

**Concept:** the agent is stateless; the client owns the conversation.
**Code:** `services/agent/history.py`, `services/agent/main.py`, and the
[argus-frontend](https://github.com/codedsultan/argus-frontend) and
[argus-agent-web](https://github.com/codedsultan/argus-agent-web) route handlers.

## Why the agent is stateless

`POST /prompt` accepts two fields:

```json
{
  "message": "What currencies are supported by the Argus Field Supply?",
  "history": []
}
```

The agent does not keep a session store. It takes the caller's history, appends
the new user message, trims the result to a budget, and runs the tool loop. The
same request always starts from the same state.

That is a deliberate simplification. It makes the agent easy to replay, easy to
test, and impossible to accidentally share one user's history with another. The
cost is that every client must send the history back.

## The history contract

`build_messages()` in `services/agent/history.py` does three things:

1. Copies the caller's history.
2. Appends `{"role": "user", "content": input_prompt}`.
3. Calls `trim_messages()` with `HISTORY_MAX_TOKENS` and
   `HISTORY_MIN_RECENT`.

The agent returns the complete message list in `response.messages`. The client
should keep that list and send it back on the next turn.

```python
messages = build_messages(history, input_prompt, max_tokens, min_recent)
result = await agent.ainvoke({"messages": messages})
return {"response": result, "metrics": ..., "trace_id": ...}
```

`response.messages` includes the system prompt, user messages, assistant
tool-call messages, tool results, and the final assistant answer. The frontend
does not need to reconstruct the tool loop; it only needs to preserve the list.

## Trimming

`estimate_tokens()` is intentionally approximate: about four characters per
token plus a small per-message overhead. It is dependency-free and good enough
to drive a budget, not to bill a provider.

`trim_messages()` preserves four invariants:

- A leading system message stays.
- The newest `HISTORY_MIN_RECENT` messages stay when possible, so the current
  prompt is never dropped.
- Tool results are not left without the assistant turn that produced them.
- `HISTORY_MAX_TOKENS <= 0` disables trimming.

The trimmer drops old turns; it does not summarize them. That is another
deliberate tradeoff: summarization would make the replay less deterministic.

## The Next.js BFF

Both public frontends keep the agent URL server-side. The trace-first
[`argus-agent-web`](https://github.com/codedsultan/argus-agent-web) calls
`/api/prompt` on the Next.js app; the route handler calls the agent:

```ts
const upstream = await fetch(`${AGENT_URL}/prompt`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "x-forwarded-for": clientAddress(request),
    ...(BFF_TOKEN ? { authorization: `Bearer ${BFF_TOKEN}` } : {}),
  },
  body: JSON.stringify({ message, history }),
});
```

The customer [`argus-frontend`](https://github.com/codedsultan/argus-frontend)
uses `/api/chat` instead. That handler calls the same agent endpoint and returns
only normalized customer data: the answer, product cards, promotions, currencies,
and friendly source names. Trace IDs, tool calls, metrics, and operator detail
stay out of the customer response.

This avoids CORS entirely and means `AGENT_URL` never appears in the browser
bundle. The optional `ARGUS_BFF_TOKEN` is read server-side and sent as a bearer
header. The agent-web app also proxies `/api/health` so the mode badge can show
`replay` or `live` without exposing the backend.

The client keeps two pieces of state:

- `history`: the full `response.messages` list returned by the agent.
- `turns`: the visible transcript, including the prompt, the final answer, the
  tool calls, the metrics, and the trace ID.

The trace panel reads the assistant tool-call messages and the matching tool
results from `response.messages`. It does not parse the trace from Jaeger; it
shows what the agent returned and links the trace ID to Jaeger for the full
span tree.

The public profile is replay-only and allows only the three recorded first-turn
prompts. It rejects a non-empty `history`, so the customer workspace and the
agent web both expose a **New chat** control instead of a follow-up loop. The
local development profile keeps multi-turn history.

## Exercises

1. Send two prompts and inspect the second request in the browser network tab.
   Confirm that the first turn is in `history`.
2. Set `HISTORY_MAX_TOKENS=120` and send a long conversation. Watch the agent
   log report a trimmed history. Confirm the newest prompt still reaches the
   model.
3. Delete the `history` field from a request. Confirm the agent treats it as a
   fresh conversation.
4. Add a "New conversation" button to the frontend that clears both `history`
   and `turns`.

## Checkpoint

- Why does the agent return the full message list instead of only the answer?
- What happens to a tool result when its assistant tool-call turn is trimmed?
- Why is the BFF better than enabling CORS on the agent for a public frontend?
- What is the tradeoff between trimming and summarizing old turns?
