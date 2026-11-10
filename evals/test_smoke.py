from __future__ import annotations

import os

import httpx
import pytest

AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8010")

PROMPTS = [
    "Show all available products in the store.",
    "What currencies are supported by the Argus Field Supply?",
    "What current promotions are available on binoculars?",
]


def _agent_is_up() -> bool:
    try:
        response = httpx.get(f"{AGENT_URL}/healthz", timeout=2)
        return (
            response.status_code == 200
            and response.json().get("service") == "agent"
        )
    except (httpx.HTTPError, ValueError):
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _agent_is_up(), reason="agent is not running")
@pytest.mark.parametrize("prompt", PROMPTS)
def test_default_prompts(prompt: str):
    response = httpx.post(
        f"{AGENT_URL}/prompt",
        json={"message": prompt, "history": []},
        timeout=120,
    )
    response.raise_for_status()

    payload = response.json()
    messages = payload["response"]["messages"]
    assert messages

    def is_final_assistant(message: dict) -> bool:
        is_assistant = (
            message.get("role") == "assistant" or message.get("type") == "ai"
        )
        return is_assistant and not message.get("tool_calls")

    assert any(is_final_assistant(message) for message in messages)
