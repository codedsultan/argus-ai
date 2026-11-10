from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from deploy.public import proxy


@pytest.fixture
def client(monkeypatch):
    async def fake_forward(_client, _request, url, content=None):
        return proxy.Response(
            content=json.dumps(
                {
                    "ok": True,
                    "url": url,
                    "body": json.loads(content) if content else None,
                }
            ),
            status_code=200,
            media_type="application/json",
        )

    monkeypatch.setattr(proxy, "_forward", fake_forward)
    with TestClient(proxy.app) as test_client:
        yield test_client


def test_accepts_recorded_prompt_with_empty_history(client):
    response = client.post(
        "/prompt",
        json={"message": proxy.REPLAY_PROMPTS[0], "history": []},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["url"].endswith("/prompt")
    assert payload["body"]["message"] == proxy.REPLAY_PROMPTS[0]
    assert payload["body"]["history"] == []


def test_rejects_arbitrary_prompt(client):
    response = client.post(
        "/prompt",
        json={"message": "Show me the checkout flow.", "history": []},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "replay-only"


def test_rejects_follow_up_history(client):
    response = client.post(
        "/prompt",
        json={
            "message": proxy.REPLAY_PROMPTS[0],
            "history": [{"role": "user", "content": "previous turn"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "replay-only"


def test_rejects_oversized_payload(client):
    response = client.post(
        "/prompt",
        content=b"x" * (proxy.MAX_PROMPT_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"] == "request-too-large"


def test_rejects_invalid_json(client):
    response = client.post(
        "/prompt",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid-json"


def test_rejects_invalid_utf8(client):
    response = client.post(
        "/prompt",
        content=b"\xff",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid-json"


def test_requires_bff_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(proxy, "BFF_TOKEN", "s3cret")

    unauthenticated = client.post(
        "/prompt",
        json={"message": proxy.REPLAY_PROMPTS[0], "history": []},
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"] == "unauthorized"

    authenticated = client.post(
        "/prompt",
        json={"message": proxy.REPLAY_PROMPTS[0], "history": []},
        headers={"authorization": "Bearer s3cret"},
    )
    assert authenticated.status_code == 200


def test_rate_limit_returns_retry_after(client):
    client.app.state.rate_limiter = proxy.RateLimiter(2, 60)
    payload = {"message": proxy.REPLAY_PROMPTS[0], "history": []}

    assert client.post("/prompt", json=payload).status_code == 200
    assert client.post("/prompt", json=payload).status_code == 200

    limited = client.post("/prompt", json=payload)
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate-limited"
    assert "retry-after" in limited.headers


def test_concurrency_limiter_blocks_when_full():
    async def scenario() -> None:
        limiter = proxy.ConcurrencyLimiter(1)
        assert await limiter.acquire()
        assert not await limiter.acquire()
        await limiter.release()
        assert await limiter.acquire()

    asyncio.run(scenario())


def test_rate_limiter_window_resets():
    async def scenario() -> None:
        limiter = proxy.RateLimiter(1, window=0.01)
        assert (await limiter.allow("client"))[0]
        assert not (await limiter.allow("client"))[0]
        await asyncio.sleep(0.02)
        assert (await limiter.allow("client"))[0]

    asyncio.run(scenario())


def test_health_reports_edge_controls(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["edge"]["bff_auth"] is False
    assert payload["edge"]["rate_limit_per_minute"] >= 1
    assert payload["edge"]["max_concurrent"] >= 1
    assert payload["edge"]["max_prompt_bytes"] == proxy.MAX_PROMPT_BYTES
