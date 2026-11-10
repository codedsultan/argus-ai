# Module guides

These guides walk through the public Argus core. Each guide maps to one part of
the loop, references the actual code, and ends with exercises and a checkpoint.

| Guide | Concept | What you will do |
|---|---|---|
| [0 — Domain and request lifecycle](./00-domain-and-request-lifecycle.md) | Tool semantics | Run the fake shop, follow one prompt from HTTP to tool call |
| [1 — LLM boundary and replay](./01-llm-boundary-and-replay.md) | Provider abstraction | Switch between replay and live, understand the fuzzy matcher |
| [2 — Agent loop and trace](./02-agent-loop-and-trace.md) | ReAct + LangChain | Read the message list, break the loop, walk a Jaeger trace |
| [3 — Tool design and safety](./03-tool-design-and-safety.md) | Schemas from docstrings | Add a tool, handle errors, reason about destructive actions |
| [5 — History and the web client](./05-session-history-and-web-client.md) | Statelessness + BFF | Own history in the client, trim it, keep the agent URL server-side |
| [6 — Determinism and replay](./06-determinism-and-replay.md) | Record/replay | Inspect the fuzzy matcher, best-match playback, and cassette normalization |
| [8 — GenAI telemetry and semantic conventions](./08-genai-telemetry-and-semconv.md) | Semconv | Normalize OpenLLMetry, correct tool/workflow attributes, propagate context, redact content |

The numbering skips guides that are not part of this public profile. The public
chatbot is documented in guide 5 as the Next.js client.

## Shared setup

From the repo root:

```sh
cp .env.example .env
make install
docker compose -f platform/compose.core.yaml up -d --build
```

`make install` creates `.venv` and installs the Python dependencies. The Compose
stack starts four containers: `shop-api`, `agent`, `otel-collector`, and
`jaeger`.

Check that everything is up:

```sh
curl -s localhost:8010/healthz
curl -s localhost:8080/healthz
curl -s localhost:16686/api/services
```

The agent reports `"tools": "builtin"` and `"mode": "replay"` by default.

If `8010`, `8080`, or `16686` is already in use, override the host ports:

```sh
AGENT_PORT=8012 SHOP_API_PORT=8081 JAEGER_PORT=16687 \
  docker compose -f platform/compose.core.yaml up -d --build
```

The containers keep their internal ports; only the host mapping changes. Use the
override values in the commands below.

Stop the stack when you are done:

```sh
docker compose -f platform/compose.core.yaml down -v
```

### Run without Docker

Use two terminals for the core:

```sh
# terminal 1
make run-shop

# terminal 2
make run-agent
```

The agent reads `APPLICATION_ENDPOINT=localhost:8080` from `.env`. If `8010` is
taken, set `AGENT_PORT` in `.env` or the shell before `make run-agent`.

### Tests and lint

```sh
make lint
make core-v1-test
```

`make core-v1-test` runs 34 tests with the stack up: four replay-matcher tests,
nine history-trimming tests, fifteen determinism tests, three smoke tests, one
response-metrics test, and two telemetry tests. Without the stack, the six
integration tests skip and 28 pass.

The telemetry tests read `AGENT_URL`, `JAEGER_URL`, and `COLLECTOR_HTTP_URL`.
Their defaults match the Compose ports. If you override the host ports, pass the
matching URLs:

```sh
AGENT_URL=http://localhost:8012 JAEGER_URL=http://localhost:16687 \
COLLECTOR_HTTP_URL=http://localhost:4318 make core-v1-test
```

## How to use a guide

1. Read the concept section.
2. Run the commands and look at the output.
3. Open the referenced files and match the code to the explanation.
4. Do the exercises. They are designed to break something small.
5. Answer the checkpoint questions before moving on.

## Code references

The guides reference function names, file names, span names, and environment
variables rather than line numbers. Those anchors survive refactors better than
line numbers.

## What comes after the guides

- Run the public frontend against the core; see
  [argus-frontend](https://github.com/codedsultan/argus-frontend).
- Read [the hardening roadmap](../hardening.md) before exposing the agent.
- Verify live mode with a real provider key in an isolated environment. The
  public snapshot is replay-only and live mode is unverified.
