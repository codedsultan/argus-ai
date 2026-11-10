# Core v1

**Stack:** `platform/compose.core.yaml`
**Test command:** `make core-v1-test`
**Public frontends:** [argus-frontend](https://github.com/codedsultan/argus-frontend) and [argus-agent-web](https://github.com/codedsultan/argus-agent-web)

Core v1 is the public profile of Argus. It is a curated subset of the agent
loop: the smallest coherent surface that still produces a useful trace.

The profile starts four containers and demonstrates the whole loop:

```text
prompt -> agent -> tool call -> shop API -> replayed LLM -> answer -> trace
```

Replay is the default, so the core answers the three default prompts without an
API key and produces the same run every time.

## Included

- `shop-api`, `agent`, `otel-collector`, and `jaeger`.
- Built-in tools, `ChatLLM`, and the VCR replay layer (`USE_VCR=true`).
- Live mode as an opt-in (`USE_VCR=false` plus a provider key).
- Client-owned session history and token-budget trimming.
- Determinism tests, the three-prompt smoke test, GenAI telemetry checks, and
  the public proxy and public-mode guards.
- `make core-v1-test`: replay, history, determinism, smoke, telemetry, proxy,
  and public-mode tests.

## Not included

This profile intentionally leaves out experimental transports, fault-injection
selectors, trace-based evals, and the full observability stack. The public
surfaces are the customer storefront and the trace-first agent web.

## Run

```sh
cp .env.example .env
docker compose -f platform/compose.core.yaml up -d --build
```

Then:

```sh
curl -s localhost:8010/healthz
curl -s localhost:8010/prompt \
  -H 'content-type: application/json' \
  -d '{"message":"Show all available products in the store.","history":[]}'
```

Open <http://localhost:16686> and search for the `agent` service.

If the default host ports are taken:

```sh
AGENT_PORT=8012 SHOP_API_PORT=8081 \
  docker compose -f platform/compose.core.yaml up -d --build
```

## Test

With the core stack up:

```sh
make install
make core-v1-test
```

That runs 49 tests with the stack reachable at `AGENT_URL`: replay, history,
determinism, the three smoke prompts, telemetry, the public proxy, and public
mode. Without the stack, the integration tests skip.

See [the module guides](modules/README.md) for the code walkthrough and
[the hardening status](hardening.md) for the public-edge controls and residual
risks.
