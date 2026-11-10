"""Single-container public proxy.

The public deployment runs the agent, shop API, collector, and Jaeger in one
container. This proxy exposes only the surfaces the product needs:

- ``/prompt`` and ``/healthz`` -> the agent
- ``/jaeger`` -> the Jaeger UI and query API, for the operator console

Everything else stays on localhost. The Next.js frontend calls this proxy from
its server-side route handler, so the browser never talks to it directly.

Public-mode controls live here as well:

- an optional shared secret for the BFF (``PUBLIC_BFF_TOKEN``)
- a per-client request budget (``PUBLIC_RATE_LIMIT_PER_MINUTE``)
- a global in-flight prompt cap (``PUBLIC_MAX_CONCURRENT``)
- a hard body-size cap (``MAX_PROMPT_BYTES``)
- the recorded-prompt allowlist
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)

AGENT_URL = os.getenv("AGENT_INTERNAL_URL", "http://127.0.0.1:8010").rstrip("/")
JAEGER_URL = os.getenv("JAEGER_INTERNAL_URL", "http://127.0.0.1:16686").rstrip("/")
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
MAX_PROMPT_BYTES = int(os.getenv("MAX_PROMPT_BYTES", "16384"))
BFF_TOKEN = os.getenv("PUBLIC_BFF_TOKEN", "").strip()
RATE_LIMIT_PER_MINUTE = int(os.getenv("PUBLIC_RATE_LIMIT_PER_MINUTE", "12"))
MAX_CONCURRENT_PROMPTS = int(os.getenv("PUBLIC_MAX_CONCURRENT", "4"))
RATE_WINDOW_SECONDS = 60.0

# The public cassette contains exactly these first-turn prompts. Reject
# everything else at the edge so a direct caller cannot reach the agent's
# tools or trigger a cassette miss.
REPLAY_PROMPTS = (
    "Show all available products in the store.",
    "What currencies are supported by the Argus Field Supply?",
    "What current promotions are available on binoculars?",
)

HOP_BY_HOP = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class RateLimiter:
    """Small fixed-window limiter for a single-process deployment."""

    def __init__(self, limit: int, window: float = RATE_WINDOW_SECONDS) -> None:
        self.limit = max(1, limit)
        self.window = window
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        async with self._lock:
            if len(self._buckets) > 10_000:
                self._buckets = {
                    bucket_key: bucket
                    for bucket_key, bucket in self._buckets.items()
                    if now - bucket[0] < self.window
                }

            window_start, count = self._buckets.get(key, (now, 0))
            if now - window_start >= self.window:
                window_start, count = now, 0

            if count >= self.limit:
                retry_after = max(1.0, self.window - (now - window_start))
                return False, retry_after

            self._buckets[key] = (window_start, count + 1)
            return True, 0.0


class ConcurrencyLimiter:
    """Non-blocking in-flight counter for expensive prompt requests."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._active = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        return self._active


def _headers(response: httpx.Response) -> dict[str, str]:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    location = headers.get("location")
    if location:
        headers["location"] = location.replace(JAEGER_URL, "")
    return headers


def _json_response(
    payload: dict[str, object],
    status_code: int,
    headers: dict[str, str] | None = None,
) -> Response:
    return Response(
        content=json.dumps(payload),
        status_code=status_code,
        media_type="application/json",
        headers={"cache-control": "no-store", **(headers or {})},
    )


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _authorized(request: Request) -> bool:
    if not BFF_TOKEN:
        return True
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        token.strip(), BFF_TOKEN
    )


async def _forward(
    client: httpx.AsyncClient,
    request: Request,
    url: str,
    content: bytes | None = None,
) -> Response:
    if content is None:
        content = await request.body()

    try:
        upstream = await client.request(
            request.method,
            url,
            content=content,
            headers={
                key: value
                for key, value in request.headers.items()
                if key.lower() not in HOP_BY_HOP and key.lower() != "host"
            },
        )
    except httpx.HTTPError:
        logger.exception("upstream request failed: %s", url)
        return _json_response(
            {"error": "upstream unavailable"},
            502,
        )

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_headers(upstream),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=TIMEOUT)
    app.state.rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)
    app.state.concurrency = ConcurrencyLimiter(MAX_CONCURRENT_PROMPTS)
    yield
    await app.state.client.aclose()


app = FastAPI(title="Argus Public Proxy", lifespan=lifespan)


@app.get("/")
async def index() -> dict[str, str]:
    return {
        "service": "argus-core",
        "mode": "replay",
        "agent": "/prompt",
        "health": "/healthz",
        "jaeger": "/jaeger",
    }


@app.api_route(
    "/healthz",
    methods=["GET", "HEAD"],
)
async def health(request: Request) -> Response:
    upstream = await _forward(app.state.client, request, f"{AGENT_URL}/healthz")

    if (
        request.method == "GET"
        and upstream.status_code == 200
        and "application/json" in upstream.headers.get("content-type", "")
    ):
        try:
            payload = json.loads(upstream.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return upstream
        if isinstance(payload, dict):
            payload["edge"] = {
                "bff_auth": bool(BFF_TOKEN),
                "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
                "max_concurrent": MAX_CONCURRENT_PROMPTS,
                "max_prompt_bytes": MAX_PROMPT_BYTES,
            }
            return _json_response(payload, 200)

    return upstream


@app.api_route(
    "/prompt",
    methods=["POST"],
)
async def prompt(request: Request) -> Response:
    if not _authorized(request):
        return _json_response(
            {
                "error": "unauthorized",
                "detail": "A valid BFF bearer token is required.",
            },
            401,
            {"www-authenticate": "Bearer"},
        )

    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > MAX_PROMPT_BYTES:
                return _json_response(
                    {
                        "error": "request-too-large",
                        "detail": "Prompt payload is too large.",
                    },
                    413,
                )
        except ValueError:
            return _json_response(
                {
                    "error": "invalid-content-length",
                    "detail": "Invalid Content-Length header.",
                },
                400,
            )

    allowed, retry_after = await app.state.rate_limiter.allow(
        _client_key(request)
    )
    if not allowed:
        return _json_response(
            {
                "error": "rate-limited",
                "detail": "Too many preview requests. Try again shortly.",
            },
            429,
            {"retry-after": str(int(retry_after))},
        )

    body = await request.body()
    if len(body) > MAX_PROMPT_BYTES:
        return _json_response(
            {
                "error": "request-too-large",
                "detail": "Prompt payload is too large.",
            },
            413,
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_response(
            {
                "error": "invalid-json",
                "detail": "Request body must be valid JSON.",
            },
            400,
        )

    if not isinstance(payload, dict):
        return _json_response(
            {
                "error": "invalid-payload",
                "detail": "Request body must be a JSON object.",
            },
            400,
        )

    message = payload.get("message")
    history = payload.get("history")
    if not isinstance(message, str) or message.strip() not in REPLAY_PROMPTS:
        return _json_response(
            {
                "error": "replay-only",
                "detail": (
                    "This public demo is replay-only. Use New chat and pick one "
                    "of the three recorded prompts."
                ),
            },
            400,
        )

    if history:
        return _json_response(
            {
                "error": "replay-only",
                "detail": (
                    "This public demo answers one recorded prompt per new chat. "
                    "Use New chat before the next prompt."
                ),
            },
            400,
        )

    if not await app.state.concurrency.acquire():
        return _json_response(
            {
                "error": "busy",
                "detail": "Argus is handling the maximum number of preview requests.",
            },
            429,
            {"retry-after": "1"},
        )

    try:
        # Normalize to exactly what the cassette recorded.
        payload["message"] = message.strip()
        payload["history"] = []
        body = json.dumps(payload).encode("utf-8")

        response = await _forward(
            app.state.client,
            request,
            f"{AGENT_URL}/prompt",
            content=body,
        )
    finally:
        await app.state.concurrency.release()

    response.headers["cache-control"] = "no-store"
    return response


def _jaeger_response(response: Response) -> Response:
    response.headers.setdefault("cache-control", "no-store")
    response.headers.setdefault("x-content-type-options", "nosniff")
    return response


@app.api_route(
    "/jaeger",
    methods=["GET", "HEAD"],
)
async def jaeger_root(request: Request) -> Response:
    if not _authorized(request):
        return _json_response(
            {
                "error": "unauthorized",
                "detail": "A valid operator bearer token is required.",
            },
            401,
            {"www-authenticate": "Bearer"},
        )
    response = await _forward(
        app.state.client, request, f"{JAEGER_URL}/jaeger/"
    )
    return _jaeger_response(response)


@app.api_route(
    "/jaeger/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def jaeger(path: str, request: Request) -> Response:
    if not _authorized(request):
        return _json_response(
            {
                "error": "unauthorized",
                "detail": "A valid operator bearer token is required.",
            },
            401,
            {"www-authenticate": "Bearer"},
        )
    response = await _forward(
        app.state.client, request, f"{JAEGER_URL}/jaeger/{path}"
    )
    return _jaeger_response(response)
