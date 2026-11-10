# Argus

**A traceable tool-calling agent, replayable without an API key.**

Argus is a small, reproducible agent lab built around one loop:

```text
prompt -> agent -> tool call -> shop API -> replayed LLM -> answer -> trace
```

The agent answers questions about **Argus Field Supply**, a fictional
field-optics shop. Every prompt produces an OpenTelemetry trace
with the agent workflow, the LLM call, the selected tool, and the shop API
request. Replay is the default, so the public demo needs no provider key and
returns the same answers every time.

- **Customer frontend:** [codedsultan/argus-frontend](https://github.com/codedsultan/argus-frontend) — Next.js storefront, assistant workspace, and operations console.
- **Agent web:** [codedsultan/argus-agent-web](https://github.com/codedsultan/argus-agent-web) — trace-first chat UI that exposes the tool loop.
- **Agent core:** this repository — FastAPI agent, shop API, replay cassettes, collector, and Jaeger.

**Live:** [argus.xurl.fyi](https://argus.xurl.fyi) · [argus-agent.xurl.fyi](https://argus-agent.xurl.fyi) · [public edge health](https://argus-api.xurl.fyi/healthz)

## Quick start

```sh
cp .env.example .env
docker compose -f platform/compose.core.yaml up -d --build
```

The core starts four containers:

| Service | Host port | Purpose |
|---|---:|---|
| `agent` | `8010` | FastAPI `POST /prompt`, LangChain agent loop |
| `shop-api` | `8080` | Fake catalog, cart, ads, and shipping API |
| `otel-collector` | `4317` / `4318` | OTLP pipeline, GenAI normalization, redaction |
| `jaeger` | `16686` | Trace UI |

Ask the first prompt:

```sh
curl -s localhost:8010/prompt \
  -H 'content-type: application/json' \
  -d '{"message":"Show all available products in the store.","history":[]}' | jq
```

Open <http://localhost:16686>, choose the `agent` service, and inspect the trace.
You should see the agent workflow, the `ChatLLM.chat` span, the
`execute_tool list_products` span, and the `shop-api` server span.

If `8010` or `8080` is already taken, override the host ports:

```sh
AGENT_PORT=8012 SHOP_API_PORT=8081 \
  docker compose -f platform/compose.core.yaml up -d --build
curl -s localhost:8012/prompt ...
```

Stop the stack with:

```sh
docker compose -f platform/compose.core.yaml down -v
```

## What is in the core

- A stateless FastAPI agent with a LangChain tool loop and a `/prompt` contract.
- Ten typed shop tools. The public profile removes `add_to_cart`, `checkout`,
  `empty_cart`, and `get_cart`, leaving read-only catalog, currency,
  recommendation, ads, and shipping tools.
- A VCR replay layer with a fuzzy best-match matcher and two model cassettes.
- Client-owned conversation history with a token-budget trimmer.
- OpenTelemetry GenAI spans, collector normalization, and content redaction.
- Three deterministic smoke prompts, replay/determinism tests, history tests,
  and telemetry checks.

## Replay and live mode

| Mode | `USE_VCR` | Provider key | LLM network |
|---|---|---|---|
| Replay (default) | `true` | not needed | only on a cassette miss |
| Live | `false` | required | yes |

Replay still calls the local shop API. It replaces only the external LLM call.
The cassettes live in `evals/cassettes/` and are keyed by `LLM_MODEL`, with `/`
replaced by `_` and `_cassette.yaml` appended. The public fixtures cover the
three smoke prompts as first-turn questions; arbitrary follow-ups are cassette
misses by design. Use **New chat** in the frontend between recorded prompts, or
switch to live mode locally to ask your own questions.

Replay is **not** a network sandbox. A request that misses every cassette falls
through to the configured provider under `record_mode="new_episodes"`. The
default configuration has no real key, so a miss fails instead of leaking a
prompt, but the request is still attempted. Keep sensitive prompts out of replay
or isolate the agent's network until the hardening plan is complete.

Live mode is implemented but unverified in this public snapshot because no real
provider key was available. It remains a local operator feature, not a public
endpoint.

## Tests

Install the local test dependencies:

```sh
make install
```

With the core stack reachable at `AGENT_URL` (default `http://localhost:8010`):

```sh
make core-v1-test
```

That runs 49 tests: replay matcher, history trimming, determinism, the public
proxy and public-mode guards, the three smoke prompts, and telemetry. Without the
stack, the integration tests skip.

```sh
make lint      # ruff
make core-test # all tests in evals/
```

## The frontends

The customer storefront is
[codedsultan/argus-frontend](https://github.com/codedsultan/argus-frontend). It
uses a server-side BFF at `/api/chat`, returns only normalized customer data, and
ships an operations console at `/admin` for traces, recorded scenarios, and
hardening status.

The trace-first surface is
[codedsultan/argus-agent-web](https://github.com/codedsultan/argus-agent-web). It
uses `/api/prompt` and `/api/health`, renders the tool loop, and shows the trace
identifier.

Both keep the agent URL server-side, send the optional `ARGUS_BFF_TOKEN`, and
never expose the backend to the browser bundle.

## Deployment

The public backend is packaged as one replay-only container in
[`deploy/public/`](deploy/public/). It runs the shop API, agent, collector, and
Jaeger behind a proxy on `$PORT`, with Jaeger served under `/jaeger`. The
container binds the internal services to localhost, removes destructive tools,
forces fail-closed replay, and disables Traceloop product telemetry and trace
content.

```sh
docker build -f deploy/public/Dockerfile -t argus-core .
docker run --rm -p 8088:8080 argus-core
```

Set `PUBLIC_BFF_TOKEN` to require a bearer token on `/prompt` and `/jaeger`.
[`fly.toml`](fly.toml) and [`render.yaml`](render.yaml) are ready for a
single-machine deployment. Point both frontends' server-only `AGENT_URL` at the
backend and set the same `ARGUS_BFF_TOKEN`. The customer frontend also needs
`JAEGER_URL`, `ADMIN_PASSWORD`, `ADMIN_SESSION_SECRET`, and
`NEXT_PUBLIC_SITE_URL`. See
[`deploy/public/README.md`](deploy/public/README.md) for the host-specific
steps and limits.

## Documentation

- [Core v1 scope](docs/core-v1.md)
- [Hardening status](docs/hardening.md)
- [Module guides](docs/modules/README.md) — domain, LLM boundary, agent loop,
  tool design, history and the web client, determinism, and GenAI telemetry.

## Safety

The public profile allowlists the three recorded first-turn prompts, rejects
non-empty history, caps bodies at 16 KiB, enforces a per-client rate limit and a
global concurrency cap, and removes destructive tools. Replay misses fail
closed. The optional `PUBLIC_BFF_TOKEN` requires a bearer token from the BFFs;
`/jaeger` is protected by the same token. The local development stack still
exposes the full tool surface.

The public profile sets `TRACELOOP_TRACE_CONTENT=false`, the collector drops
prompt/completion/entity content, and the operations console filters sensitive
tags again. See [`docs/hardening.md`](docs/hardening.md) for the residual risks.

## License

Apache License 2.0. See [`LICENSE`](LICENSE) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
