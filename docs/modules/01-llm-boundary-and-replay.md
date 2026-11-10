# Module 1 — LLM boundary and replay

> Code references are to the scaffold commit `5f62a34`. Line numbers may drift; function and
> file names are the stable anchors.

## What you will learn

- Why the agent depends on a `ChatLLM` adapter instead of a provider SDK.
- How replay mode answers prompts with no API key and no network.
- How the fuzzy matcher decides which recorded response to play.
- How to record a new cassette and when replay will betray you.

This guide introduces replay early because the core runs in replay mode by
default. Module 6 goes deeper into determinism; this is the working overview.

## The boundary

The agent calls a LangChain chat model. It does not know whether that model is
OpenAI, Azure, a local server, or a cassette. The adapter is
`services/agent/llm.py`:

```python
class ChatLLM(ChatOpenAI):
    def __init__(self, **kwargs):
        model_name = os.getenv("LLM_MODEL", "azure/gpt-5.5")
        llm_tls_verify = os.getenv("LLM_TLS_VERIFY", "true").lower() == "true"
        cassette_name = f"{model_name.replace('/', '_')}_cassette.yaml"

        if "http_async_client" not in kwargs:
            kwargs["http_async_client"] = httpx.AsyncClient(verify=llm_tls_verify)
        kwargs.setdefault("openai_api_base", os.getenv("LLM_BASE_URL") or None)
        kwargs.setdefault("model", model_name)

        api_key = os.getenv("API_KEY")
        if not api_key:
            api_key = "sk-dummy"
        kwargs.setdefault("api_key", api_key)

        super().__init__(**kwargs)
        object.__setattr__(self, "_use_vcr", USE_VCR)
        object.__setattr__(self, "_cassette_name", cassette_name)
```

| Variable | Default | Meaning |
|---|---|---|
| `LLM_MODEL` | `azure/gpt-5.5` | Model name and cassette stem |
| `LLM_BASE_URL` | blank | OpenAI-compatible base URL; blank uses the SDK default |
| `API_KEY` | blank → `sk-dummy` | Real key for live mode; dummy for replay |
| `LLM_TLS_VERIFY` | `true` | TLS verification for the async HTTP client |
| `USE_VCR` | `true` | Replay switch |
| `VCR_CASSETTE_DIR` | `evals/cassettes` | Cassette directory |
| `VCR_MATCH_THRESHOLD` | `0.85` | Minimum similarity score |

Two details are easy to miss:

- **`http_async_client`** is injected because the agent uses `ainvoke`. The
  client carries the TLS setting.
- **`object.__setattr__`** bypasses Pydantic's attribute handling. `ChatOpenAI`
  is a Pydantic model; plain assignment would be treated as a field, not a
  private runtime flag.

## The call path

Both the sync and async paths do the same three things:

```python
async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
    delay_ms = get_int_feature_flag("aiSlowResponse")
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    if getattr(self, "_use_vcr", False):
        with VCR.use_cassette(self._cassette_name):
            return await super()._agenerate(messages, stop, run_manager, **kwargs)
    return await super()._agenerate(messages, stop, run_manager, **kwargs)
```

1. Apply the optional slow-response delay (`AI_SLOW_RESPONSE_MS`).
2. If replay is on, open the cassette for this call.
3. Delegate to `ChatOpenAI`.

The cassette context is opened per model call, not per request. A single agent
run can make several model calls, and each one is matched separately.

## The replay layer

`services/agent/replay.py` configures a `vcr.VCR` instance:

```python
VCR = vcr.VCR(
    cassette_library_dir=CASSETTE_DIR,
    record_mode="new_episodes",
    serializer="yaml",
    path_transformer=vcr.VCR.ensure_suffix(".yaml"),
    filter_headers=["authorization", "x-api-key", "api-key"],
    match_on=["similar_body"],
    before_record_request=normalize_body,
    before_record_response=clean_response,
    decode_compressed_response=True,
)
VCR.register_matcher("similar_body", similar_body_matcher)
```

The pieces:

| Piece | Job |
|---|---|
| `normalize_body` | Sort JSON keys, clear headers, rewrite URI and method so requests are comparable |
| `clean_response` | Strip volatile fields and refuse to record errors |
| `_normalize_message` | Replace tool-result content with `<tool-result>` during matching |
| `body_similarity` | Score two request bodies in `[0, 1]` |
| `similar_body_matcher` | Raise when the score is below the threshold |
| `_install_best_match_playback` | Pick the highest-scoring unplayed response |

### Why the deserialize patch exists

VCR's httpx stub expects a byte body, but the YAML serializer returns a string.
The module patches `_deserialize_response` to encode the body before handing it
back. Without this shim, replay fails on the pinned VCR version. It is a small
compatibility fix, not part of the agent design.

### How a request is normalized

`normalize_body` runs when recording:

```python
request.body = json.dumps(data, sort_keys=True).encode(encoding)
request.headers = {}
request.uri = "https://vcr.local/"
request.method = "POST"
```

The provider URL, auth headers, and JSON key order are removed from the
comparison. The request is still a POST with the same model payload.

### How responses are cleaned

`clean_response` removes fields that change between runs:

- `id` → `chatcmpl-fixture`
- `created` → `0`
- `usage`, `system_fingerprint`, `prompt_filter_results`, `service_tier`
- per-choice `provider_specific_fields` and `annotations`

It also returns `None` for non-2xx responses and provider error payloads, so a
failed call is never recorded as a fixture.

## How matching works

The matcher compares the **messages** array, not the raw JSON. It does not compare
the `tools` array or any other request field.

1. If the message counts differ, the score is `0.0`. The agent's message count
   changes when a tool call is added or removed, and those are different states.
2. Every `role == "tool"` message has its content replaced with
   `<tool-result>`. This is the key trick: the local shop can return different
   product JSON, but the LLM request still matches.
3. The score is `0.4 * full_conversation_ratio + 0.6 * last_message_ratio`.
   The latest message gets more weight because it determines the next action.
4. A score at or above `VCR_MATCH_THRESHOLD` matches.

The tests in `evals/test_replay.py` pin these properties:

- identical bodies score `1.0`
- different message counts score `0.0`
- tool results are ignored
- the last message carries more weight

### Best-match playback

VCR's default behavior is to play the first matching response. That is wrong
when several recorded calls look similar. The patch scans every unplayed entry,
scores it, and plays the highest-scoring one above the threshold:

```python
for idx, recorded in enumerate(self.requests):
    if self.play_counts[idx] > 0:
        continue
    score = body_similarity(recorded, request)
    if score >= VCR_MATCH_THRESHOLD and score > best_score:
        best_score = score
        best_idx = idx
```

If nothing matches, it raises `UnhandledHTTPRequestError`. With
`record_mode="new_episodes"`, VCR can then record a new exchange when a real
endpoint and credentials are available.

## Recording a new cassette

1. Pick a model name. The cassette stem is the model name with `/` replaced by
   `_`: `azure/gpt-5.5` → `azure_gpt-5.5_cassette.yaml`.
2. Set the environment:

   ```sh
   export USE_VCR=true
   export LLM_MODEL=your-model
   export LLM_BASE_URL=https://your-endpoint/v1
   export API_KEY=...
   export VCR_CASSETTE_DIR=evals/cassettes
   ```

3. Remove or rename the old cassette if you want a fresh recording.
4. Run the prompt. Unmatched requests are recorded; `clean_response` strips
   volatile fields; `filter_headers` removes auth headers.
5. Inspect the YAML and commit it only if it contains no secrets.

Two caveats:

- **Matching ignores the tool schema.** `body_similarity` compares only the
  `messages` array. Adding or re-describing a tool alone does not break replay,
  but the recorded response may call a tool that no longer exists, or with
  arguments the new schema rejects. Re-record when the tool set changes.
- **Errors are not recorded.** A non-2xx response leaves no cassette entry, so
  replay cannot reproduce that failure.

## Live mode

Set:

```sh
export USE_VCR=false
export LLM_BASE_URL=https://your-endpoint/v1
export LLM_MODEL=your-model
export API_KEY=...
```

The same `ChatLLM` path runs without a cassette. Tool calls still go to the shop
API. `LLM_TLS_VERIFY=false` is available for self-signed development endpoints;
do not use it in production.

The two modes in the public core are:

| Mode | `USE_VCR` | LLM | Network | Status |
|---|---|---|---|---|
| Replay | `true` | cassette | none | default |
| Live | `false` | real endpoint | yes | implemented; unverified in this snapshot |

## Run it

Start the stack and ask a prompt:

```sh
make core-up-d
curl -s localhost:8010/prompt \
  -H 'content-type: application/json' \
  -d '{"message":"What currencies are supported by the Argus Field Supply?","history":[]}' \
  | jq '.response.messages[-1].content'
```

Inspect a cassette:

```sh
ls evals/cassettes
sed -n '1,60p' evals/cassettes/azure_gpt-5.5_cassette.yaml
```

Run the matcher tests:

```sh
make core-test
```

Try the matcher directly:

```sh
.venv/bin/python - <<'PY'
import json
from services.agent.replay import body_similarity

class FakeRequest:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

a = FakeRequest({"messages": [
    {"role": "user", "content": "list products"},
    {"role": "tool", "content": "result-a"},
]})
b = FakeRequest({"messages": [
    {"role": "user", "content": "list products"},
    {"role": "tool", "content": "result-b"},
]})
print(body_similarity(a, b))  # 1.0: tool results are ignored
PY
```

## What to observe

- The cassette stores the real request and response, but matching ignores the
  tool schema and the tool-result content.
- Response IDs are `chatcmpl-fixture`, `created` is `0`, and `usage` is gone.
- Auth headers are not in the cassette.
- The default prompts answer with no API key and no external LLM call.
- The shop API is still called for real; replay only replaces the model.

## Exercises

1. Change the products prompt by one word and ask again. Does it still replay?
   Which part of the score changed?
2. Set `VCR_MATCH_THRESHOLD=1.0` and ask a prompt whose last message differs.
   What error do you get? Restore `0.85` afterwards.
3. Remove `list_products` from `TOOLS` and ask the products prompt in replay.
   Does the cassette still match? What happens when the recorded response calls
   the missing tool? Restore it afterwards.
4. Open a cassette and find a `role: tool` message. What content is stored?
   Why does the matcher replace it with `<tool-result>`?
5. Run the same prompt twice in replay. Are the responses identical? What does
   that prove, and what does it not prove about the agent?

## Checkpoint

- Why does `ChatLLM` need a dummy API key in replay mode?
- What does `normalize_body` remove, and why?
- Why is the last message weighted more heavily than the full conversation?
- When would you choose live mode over replay?
- What kind of failure can replay hide?

## Next

[Guide 2 — Agent loop and trace](./02-agent-loop-and-trace.md)
