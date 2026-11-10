# Single-container public backend

This is the replay-only backend profile used by the public Argus demo. One
container runs the shop API, agent, OpenTelemetry collector, and Jaeger behind a
small proxy:

```text
browser -> Next.js BFF -> proxy :$PORT -> agent :8010 -> shop API :8081
                                      \-> Jaeger UI /jaeger
```

Only the proxy is bound to the public port. The agent, shop API, collector, and
Jaeger bind to `127.0.0.1` inside the container. The proxy exposes:

- `POST /prompt` — the agent contract
- `GET /healthz` — the agent health check and edge metadata
- `GET /jaeger` — the read-only Jaeger UI and API

When `PUBLIC_BFF_TOKEN` is set, `/prompt` and `/jaeger` require
`Authorization: Bearer <token>`. The Next.js BFFs send it server-side; the
browser never sees it. The health endpoint stays public so a status page can
read the mode.

## Public profile

The container forces replay mode and removes destructive tools:

```text
USE_VCR=true
VCR_RECORD_MODE=none
PUBLIC_MODE=true
TRACELOOP_TELEMETRY=false
TRACELOOP_TRACE_CONTENT=false
```

`PUBLIC_MODE=true` removes `add_to_cart`, `checkout`, `empty_cart`, and
`get_cart`. The agent refuses to start if public mode is enabled without
fail-closed replay.

## Edge controls

| Variable | Default | Purpose |
|---|---:|---|
| `PUBLIC_BFF_TOKEN` | empty | Optional bearer token for `/prompt` and `/jaeger` |
| `PUBLIC_RATE_LIMIT_PER_MINUTE` | `12` | Per-client request budget |
| `PUBLIC_MAX_CONCURRENT` | `4` | Global in-flight prompt cap |
| `MAX_PROMPT_BYTES` | `16384` | Request body cap |
| `PORT` | `8080` | Public proxy port |

The proxy also allowlists the three recorded first-turn prompts and rejects
non-empty history. The rate limiter and concurrency counter are in-process, so
they apply per container.

## Build and run

```sh
docker build -f deploy/public/Dockerfile -t argus-core .
docker run --rm -p 8088:8080 argus-core
```

With an edge token:

```sh
docker run --rm -p 8088:8080 \
  -e PUBLIC_BFF_TOKEN=replace-me \
  argus-core
```

`PORT` defaults to `8080`. To test a host-assigned port:

```sh
docker run --rm -p 8088:9090 -e PORT=9090 argus-core
```

Ask one of the recorded prompts:

```sh
curl -s localhost:8088/prompt \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer replace-me' \
  -d '{"message":"Show all available products in the store.","history":[]}' | jq
```

Open <http://localhost:8088/jaeger> for the trace UI. When a token is set, the
Jaeger UI and API need the same bearer header. The returned `trace_id` resolves
at `/jaeger/trace/<trace_id>`.

## Fly.io

[`fly.toml`](../../fly.toml) is ready for a single machine. Change the app name
if it is taken, then:

```sh
fly launch --no-deploy --copy-config
fly deploy
```

The machine uses 1 GB of memory. Keep the internal port at `8080`; the proxy
defaults to that when `PORT` is not set.

## Render

[`render.yaml`](../../render.yaml) defines a Docker web service. Create a
Blueprint from the repository, or create a Docker service manually with:

- Dockerfile: `deploy/public/Dockerfile`
- Docker context: repository root
- Health check: `/healthz`

The Blueprint starts on the free plan, which is enough for the replay demo but
has 512 MB of memory and spins down when idle. Use a 1 GB plan if the service
restarts under load.

## Frontend environment

The customer storefront is
[`argus-frontend`](https://github.com/codedsultan/argus-frontend). The trace-first
agent surface is
[`argus-agent-web`](https://github.com/codedsultan/argus-agent-web). Both point
at the same backend.

Customer frontend:

```text
AGENT_URL=https://<backend-host>
JAEGER_URL=https://<backend-host>/jaeger
ARGUS_BFF_TOKEN=<same value as PUBLIC_BFF_TOKEN>
ADMIN_PASSWORD=<operator password>
ADMIN_SESSION_SECRET=<long random value>
NEXT_PUBLIC_SITE_URL=https://<customer-host>
```

Agent web:

```text
AGENT_URL=https://<backend-host>
ARGUS_BFF_TOKEN=<same value as PUBLIC_BFF_TOKEN>
NEXT_PUBLIC_JAEGER_URL=
```

`AGENT_URL`, `JAEGER_URL`, and `ARGUS_BFF_TOKEN` are server-only. Do not expose
them as `NEXT_PUBLIC_*`. The agent web leaves `NEXT_PUBLIC_JAEGER_URL` blank
because the browser cannot send the edge token to `/jaeger`; it still shows the
trace identifier and the tool detail returned by the agent.

## Limits

This is a demo deployment, not a general production service. The proxy accepts
only the three recorded first-turn prompts, rejects non-empty history, caps
bodies at 16 KiB, and enforces a per-client rate limit and a global concurrency
cap. Replay misses fail closed. See
[`docs/hardening.md`](../../docs/hardening.md) for the residual risks and the
verification commands.
