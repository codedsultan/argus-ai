# Module 6 — Determinism and the replay layer in depth

> Code references are to the commit that adds `select_best_match()` and the
> determinism tests (`40e38a0`). Line numbers may drift; function names, file
> names, and span names are the stable anchors.

## What you will learn

- What a cassette records, and what is stripped before it is stored.
- Why exact request matching fails, and what the fuzzy matcher compares.
- How the score is built: message count, tool-result normalization, and a
  40/60 split between the whole conversation and the last message.
- How best-match playback chooses an entry, and what `play_counts` does.
- Why the same prompt can be replayed twice, and when entries are exhausted.
- Which determinism guarantees replay gives you, and which it does not.

## The problem

A live LLM call is non-deterministic, slow, and costs money. A test suite or a
public demo cannot depend on it. **Replay** records one real request/response pair and
plays it back later, so the agent runs with no API key and no network.

```text
record:  agent -> provider        -> cassette (request + response)
replay:  agent -> matcher -> cassette -> recorded response
```

The layer lives in `services/agent/replay.py` and is wired into `ChatLLM`:

```python
if getattr(self, "_use_vcr", False):
    with VCR.use_cassette(self._cassette_name):
        return await super()._agenerate(...)
return await super()._agenerate(...)
```

`USE_VCR=true` is the default. The cassette name is derived from the model:

```python
cassette_name = f"{model_name.replace('/', '_')}_cassette.yaml"
```

So `azure/gpt-5.5` becomes `evals/cassettes/azure_gpt-5.5_cassette.yaml`. There is
one cassette per model, not per prompt.

## What a cassette stores

A cassette is a YAML list of `interactions`. Each interaction has a normalized
request and a cleaned response:

```yaml
interactions:
- request:
    body: '{"messages": [...], "model": "azure/gpt-5.5", "tools": [...]}'
    headers: {}
    method: POST
    uri: https://vcr.local/
  response:
    body:
      string: '{"choices": [...], "created": 0, "id": "chatcmpl-fixture", ...}'
    headers: {}
    status: {code: 200, message: OK}
```

Two functions make that shape:

- `normalize_body()` sorts the JSON, clears headers, and rewrites the URI and
  method to a constant. This removes request identity (auth, host, key order) so
  the same logical request always serializes the same way.
- `clean_response()` removes volatile fields (`id` becomes `chatcmpl-fixture`,
  `created` becomes `0`, `usage`, `system_fingerprint`, provider-specific fields,
  annotations). It also refuses to record non-2xx responses and provider error
  payloads, so a failed call never becomes a fixture.

The point is a **stable artifact**: no secrets, no timestamps, no token counts
that change between runs.

## Why matching is fuzzy

The request body contains the full message list *and* the tool schemas. Small,
harmless differences appear all the time:

- JSON key order and whitespace,
- tool descriptions re-wrapped or re-ordered,
- tool results with different JSON formatting,
- a message list built by a different framework version.

Exact byte matching would break on all of them. Instead, the matcher parses the
body and compares the `messages` array. It deliberately ignores the `tools`
array and every other request field (guide 1).

## The matcher

`body_similarity()` returns a score in `[0, 1]`:

```python
def body_similarity(request_a, request_b):
    data_a = _parse_body(request_a)
    data_b = _parse_body(request_b)
    messages_a = data_a.get("messages") if isinstance(data_a, dict) else None
    messages_b = data_b.get("messages") if isinstance(data_b, dict) else None

    if not isinstance(messages_a, list) or not isinstance(messages_b, list):
        return SequenceMatcher(None, _stringify(data_a), _stringify(data_b)).ratio()

    if len(messages_a) != len(messages_b):
        return 0.0
    if not messages_a:
        return 1.0

    normalized_a = [_normalize_message(m) for m in messages_a]
    normalized_b = [_normalize_message(m) for m in messages_b]

    full_ratio = SequenceMatcher(None, _stringify(normalized_a),
                                 _stringify(normalized_b)).ratio()
    last_ratio = SequenceMatcher(None, _stringify(normalized_a[-1]),
                                 _stringify(normalized_b[-1])).ratio()
    return 0.4 * full_ratio + 0.6 * last_ratio
```

Three rules matter:

1. **Message count is a gate.** If the counts differ, the score is `0.0`. A tool
   call adds messages, so a different loop state never matches.
2. **Tool results are normalized.** `_normalize_message()` replaces any
   `role: tool` content with `<tool-result>`. Two different product lists still
   match; the matcher is comparing the conversation, not the data.
3. **The last message dominates.** The score is 40% the whole conversation and
   60% the last message. The last message is usually the user's question or the
   newest tool result, so it is the strongest signal.

The threshold is `VCR_MATCH_THRESHOLD` (default `0.85`). A score at or above it
matches; below it does not.

### The lenient edge

The ratio is character-based, so a short prompt that differs almost entirely can
still score above the threshold:

```python
body_similarity(_messages("aaaa"), _messages("zzzz"))  # >= 0.85
```

That is a real property of fuzzy matching, not a bug to "fix" casually. Raising
the threshold reduces false matches but also rejects legitimate drift. Re-record
when the conversation genuinely changed.

## Best-match playback

VCR's default matcher takes the first request that matches. The lab replaces
`Cassette.play_response` with a best-match version. The selection rule is now a
pure function:

```python
def select_best_match(requests, play_counts, request):
    best_idx = None
    best_score = -1.0
    for idx, recorded in enumerate(requests):
        if play_counts[idx] > 0:
            continue
        score = body_similarity(recorded, request)
        if score >= VCR_MATCH_THRESHOLD and score > best_score:
            best_score = score
            best_idx = idx
    return best_idx, best_score
```

- It considers only **unplayed** entries (`play_counts[idx] == 0`).
- It picks the **highest** score, not the first one.
- It returns `(None, -1.0)` when nothing qualifies, and playback raises
  `UnhandledHTTPRequestError`.

Why best-match instead of first-match? Cassettes contain several prompts. A new
prompt can be close to more than one entry; picking the closest one is more
accurate than picking whichever happens to be first in the file.

## The cassette lifecycle

This is the subtle part. `ChatLLM` opens the cassette once per **LLM call**:

```python
with VCR.use_cassette(self._cassette_name):
    return await super()._agenerate(...)
```

Each `use_cassette` creates a fresh `Cassette`, so `play_counts` starts at zero
for every call. That is why the same prompt can be replayed twice: the second
request does not see the first request's play count.

If you ever wrap several LLM calls in **one** cassette context, the skip-played
rule kicks in and the entries are consumed in order. That is the "shared cassette
state" caveat from the extraction plan (finding F7): the matcher is only as
deterministic as the scope you give it.

## Record mode and the network

The VCR is configured with `record_mode="new_episodes"`:

```python
VCR = vcr.VCR(
    cassette_library_dir=CASSETTE_DIR,
    record_mode="new_episodes",
    ...
)
```

In this mode, a request that matches nothing is **not rejected**. It is sent to
the provider and, if it succeeds, recorded as a new episode. In replay mode the
dummy key makes it fail with `401 Incorrect API key`, but the prompt has already
left the process.

Treat a replay miss as a failed replay, not a safe no-op. If prompts are
sensitive, isolate the agent's network or run with a key you are willing to use.
A stricter lab could switch to `record_mode="none"` and use a separate recording
run; this repo keeps `new_episodes` to preserve the permissive recording behavior.

## What replay guarantees

**Guaranteed**, when the request matches a cassette:

- the same response bytes (after normalization),
- the same tool calls and the same final answer,
- no provider call, no cost, no API key,
- stable traces (tool names and message shapes are identical).

**Not guaranteed:**

- **Different prompt, same answer.** A near-match can return another prompt's
  recorded answer. That is why the threshold matters.
- **Live domain state.** The shop API still runs. `add_to_cart` and `checkout`
  mutate in-memory state, so a replayed conversation can see a different cart
  than the recording did.
- **Concurrency order.** Parallel prompts can pick entries in a different order
  if they share one cassette context.
- **Tool-set changes.** Matching ignores the `tools` array, but a recorded
  response can call a tool that no longer exists, or with arguments the new
  schema rejects.
- **History trimming.** Trimming changes the `messages` array, so a long
  conversation can stop matching (module 5).

## Observe it

With the core stack running, the agent logs each playback at `INFO`:

```text
VCR best-match playback: entry 0 (score=1.0000)
VCR best-match playback: entry 1 (score=1.0000)
```

A miss logs a warning and the request falls through:

```text
VCR: not recording response (HTTP 401)
```

Ask the same prompt twice and compare the answers:

```sh
for i in 1 2; do
  curl -s -X POST localhost:8010/prompt \
    -H 'Content-Type: application/json' \
    -d '{"message":"Show all available products in the store.","history":[]}' \
    | jq -r '.response.messages[-1].content' | head -3
done
```

## Exercises

1. Set `VCR_MATCH_THRESHOLD=1.0` and ask the default prompts. Which still match?
   Restore `0.85` afterwards.
2. Change only the last message of a prompt and compare its score to changing an
   earlier message. Which is penalized more, and why?
3. Reorder the tools in `TOOLS` and replay. Does the score change? Explain using
   the fields the matcher reads.
4. Remove `list_products` from the tools and replay the products prompt. The
   request still matches; what happens when the recorded response calls the
   missing tool?
5. Run the same prompt twice in one process. Do both succeed? Which log lines
   prove that `play_counts` reset?
6. Open a cassette. Find the request and response normalization: sorted JSON,
   `chatcmpl-fixture`, `created: 0`, no `usage`, no auth header.
7. Make a request that matches nothing. What status do you get, and where did the
   prompt go? Why is that a safety issue, not just a test failure?

## Checkpoint

- What does a cassette store, and what is stripped before it is written?
- Why does the matcher ignore the `tools` array?
- What are the three rules in `body_similarity()`, and how is the score weighted?
- What does `play_counts` do, and why does it reset for each LLM call?
- When does a replay miss make a real network call?
- Name three things replay does **not** make deterministic.

## Next

Continue with
[Module 8 — GenAI telemetry and semantic conventions](./08-genai-telemetry-and-semconv.md).

Before exposing the agent, read [the hardening roadmap](../hardening.md).

## References

- `services/agent/replay.py`, `services/agent/llm.py`
- `evals/cassettes/azure_gpt-5.5_cassette.yaml`
- `evals/test_replay.py`, `evals/test_determinism.py`
