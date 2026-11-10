from __future__ import annotations

import json
import os

import httpx
import pytest

from services.agent.replay import (
    body_similarity,
    clean_response,
    normalize_body,
    select_best_match,
    similar_body_matcher,
)

AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8010")


class FakeRequest:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode("utf-8")
        self.headers: dict[str, str] = {}
        self.uri = "https://api.example.test/v1/chat/completions"
        self.method = "POST"


def _messages(*contents: str) -> FakeRequest:
    return FakeRequest(
        {"messages": [{"role": "user", "content": text} for text in contents]}
    )


# --- similarity -----------------------------------------------------------


def test_message_count_mismatch_scores_zero():
    short = _messages("hi")
    long = _messages("hi", "hello")
    assert body_similarity(short, long) == 0.0


def test_empty_message_lists_score_one():
    empty = FakeRequest({"messages": []})
    assert body_similarity(empty, empty) == 1.0


def test_last_message_is_weighted_more_than_the_full_conversation():
    base = FakeRequest(
        {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Show me the products"},
                {"role": "assistant", "content": "Here are the products."},
                {"role": "user", "content": "Which one is cheapest?"},
            ]
        }
    )
    earlier_changed = FakeRequest(
        {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "List the products"},
                {"role": "assistant", "content": "Here are the products."},
                {"role": "user", "content": "Which one is cheapest?"},
            ]
        }
    )
    last_changed = FakeRequest(
        {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Show me the products"},
                {"role": "assistant", "content": "Here are the products."},
                {"role": "user", "content": "Which one is the most expensive?"},
            ]
        }
    )

    assert body_similarity(base, earlier_changed) > body_similarity(base, last_changed)


def test_non_message_bodies_fall_back_to_string_similarity():
    left = FakeRequest({"foo": "bar"})
    right = FakeRequest({"foo": "baz"})
    score = body_similarity(left, right)
    assert 0.0 < score < 1.0


def test_matcher_passes_identical_and_raises_below_threshold():
    same = _messages("show all products")
    assert similar_body_matcher(same, same) is None

    with pytest.raises(AssertionError):
        similar_body_matcher(_messages("a" * 200), _messages("z" * 200))


def test_short_different_prompts_can_still_score_above_threshold():
    # The score is a character ratio, so a short prompt that differs almost
    # entirely can still land above the threshold. This is the lenient edge of
    # fuzzy matching, not a bug to "fix" without re-recording.
    score = body_similarity(_messages("aaaa"), _messages("zzzz"))
    assert score >= 0.85


# --- best-match selection -------------------------------------------------


def test_select_best_match_prefers_the_highest_score():
    query = _messages("show all products")
    requests = [_messages("show all product"), query]
    counts = {0: 0, 1: 0}

    index, score = select_best_match(requests, counts, query)
    assert index == 1
    assert score == 1.0


def test_select_best_match_skips_played_entries():
    query = _messages("show all products")
    exact = _messages("show all products")
    partial = _messages("show all product")
    counts = {0: 1, 1: 0}

    index, _ = select_best_match([exact, partial], counts, query)
    assert index == 1


def test_select_best_match_exhausts_entries():
    query = _messages("show all products")
    requests = [_messages("show all products"), _messages("show all products")]
    counts = {0: 0, 1: 0}

    assert select_best_match(requests, counts, query)[0] == 0
    counts[0] += 1
    assert select_best_match(requests, counts, query)[0] == 1
    counts[1] += 1
    assert select_best_match(requests, counts, query) == (None, -1.0)


def test_select_best_match_returns_none_below_threshold():
    query = _messages("show all products")
    requests = [_messages("zzzz zzzz zzzz")]
    assert select_best_match(requests, {0: 0}, query) == (None, -1.0)


# --- cassette normalization ----------------------------------------------


def test_normalize_body_sorts_json_and_strips_request_identity():
    request = FakeRequest({"b": 2, "a": 1})
    request.headers = {"authorization": "secret"}
    normalize_body(request)

    assert json.loads(request.body) == {"a": 1, "b": 2}
    assert request.headers == {}
    assert request.uri == "https://vcr.local/"
    assert request.method == "POST"


def test_normalize_body_leaves_non_json_alone():
    request = FakeRequest({})
    request.body = b"not json"
    assert normalize_body(request) is request
    assert request.body == b"not json"


def test_clean_response_strips_volatile_fields():
    body = {
        "id": "chatcmpl-real",
        "created": 1234567890,
        "model": "azure/gpt-5.5",
        "usage": {"total_tokens": 5},
        "system_fingerprint": "fp_x",
        "choices": [
            {
                "message": {
                    "content": "hi",
                    "provider_specific_fields": {"x": 1},
                    "annotations": [],
                }
            }
        ],
    }
    response = {
        "status": {"code": 200, "message": "OK"},
        "headers": {"set-cookie": "x"},
        "body": {"string": json.dumps(body)},
    }

    cleaned = clean_response(response)
    assert cleaned is not None
    assert cleaned["headers"] == {}

    data = json.loads(cleaned["body"]["string"])
    assert data["id"] == "chatcmpl-fixture"
    assert data["created"] == 0
    assert "usage" not in data
    assert "system_fingerprint" not in data
    assert "provider_specific_fields" not in data["choices"][0]["message"]
    assert "annotations" not in data["choices"][0]["message"]


def test_clean_response_refuses_non_2xx():
    response = {
        "status": {"code": 500, "message": "Server Error"},
        "headers": {},
        "body": {"string": "{}"},
    }
    assert clean_response(response) is None


def test_clean_response_refuses_provider_error_payload():
    response = {
        "status": {"code": 200, "message": "OK"},
        "headers": {},
        "body": {"string": json.dumps({"error": {"message": "bad"}})},
    }
    assert clean_response(response) is None


# --- live determinism -----------------------------------------------------


def _agent_is_up() -> bool:
    try:
        response = httpx.get(f"{AGENT_URL}/healthz", timeout=2)
        return response.status_code == 200 and response.json().get("service") == "agent"
    except (httpx.HTTPError, ValueError):
        return False


def _final_answer(payload: dict) -> str | None:
    for message in reversed(payload["response"]["messages"]):
        kind = message.get("role") or message.get("type")
        if kind in {"assistant", "ai"} and not message.get("tool_calls"):
            return message.get("content")
    return None


@pytest.mark.integration
@pytest.mark.skipif(not _agent_is_up(), reason="agent is not running")
def test_the_same_prompt_twice_returns_the_same_answer():
    request = {
        "message": "Show all available products in the store.",
        "history": [],
    }
    first = httpx.post(f"{AGENT_URL}/prompt", json=request, timeout=120)
    second = httpx.post(f"{AGENT_URL}/prompt", json=request, timeout=120)
    first.raise_for_status()
    second.raise_for_status()

    assert _final_answer(first.json()) == _final_answer(second.json())
