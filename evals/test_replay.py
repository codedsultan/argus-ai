from __future__ import annotations

import json

from services.agent.replay import body_similarity


class FakeRequest:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode("utf-8")


def test_identical_bodies_score_one():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert body_similarity(FakeRequest(body), FakeRequest(body)) == 1.0


def test_different_message_counts_never_match():
    short = {"messages": [{"role": "user", "content": "hi"}]}
    long = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    }
    assert body_similarity(FakeRequest(short), FakeRequest(long)) == 0.0


def test_tool_results_are_ignored():
    left = {
        "messages": [
            {"role": "user", "content": "recommend something"},
            {"role": "tool", "content": "result-a"},
        ]
    }
    right = {
        "messages": [
            {"role": "user", "content": "recommend something"},
            {"role": "tool", "content": "result-b"},
        ]
    }
    assert body_similarity(FakeRequest(left), FakeRequest(right)) == 1.0


def test_last_message_carries_more_weight():
    base = [{"role": "user", "content": "list products"}]
    close = base + [{"role": "assistant", "content": "here are the products"}]
    far = base + [{"role": "assistant", "content": "the weather is nice today"}]

    close_score = body_similarity(
        FakeRequest({"messages": close}), FakeRequest({"messages": close})
    )
    far_score = body_similarity(
        FakeRequest({"messages": close}), FakeRequest({"messages": far})
    )
    assert close_score > far_score
