from __future__ import annotations

from services.agent.history import (
    build_messages,
    estimate_tokens,
    message_kind,
    trim_messages,
)


def test_message_kind_handles_role_and_type():
    assert message_kind({"role": "user"}) == "user"
    assert message_kind({"role": "tool"}) == "tool"
    assert message_kind({"type": "human"}) == "user"
    assert message_kind({"type": "ai"}) == "assistant"
    assert message_kind({"type": "tool"}) == "tool"


def test_estimate_tokens_grows_with_content():
    short = [{"role": "user", "content": "hi"}]
    long = [{"role": "user", "content": "hi" * 100}]
    assert estimate_tokens(long) > estimate_tokens(short)


def test_estimate_tokens_counts_tool_calls():
    plain = [{"role": "assistant", "content": ""}]
    with_calls = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "1", "name": "list_products", "args": {}}],
        }
    ]
    assert estimate_tokens(with_calls) > estimate_tokens(plain)


def test_trim_is_noop_under_budget():
    messages = [{"role": "user", "content": "hi"}]
    assert trim_messages(messages, max_tokens=1000) == messages


def test_trim_disabled_when_budget_is_zero():
    messages = [{"role": "user", "content": "x" * 1000}]
    assert trim_messages(messages, max_tokens=0) == messages


def test_trim_drops_oldest_and_keeps_latest_user():
    messages = [
        {"role": "user", "content": "old " * 200},
        {"role": "assistant", "content": "old answer " * 200},
        {"role": "user", "content": "new question"},
    ]
    trimmed = trim_messages(messages, max_tokens=50, min_recent=1)
    assert trimmed[-1]["content"] == "new question"
    assert len(trimmed) < len(messages)


def test_trim_preserves_leading_system_message():
    messages = [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "old " * 200},
        {"role": "user", "content": "new question"},
    ]
    trimmed = trim_messages(messages, max_tokens=20, min_recent=1)
    assert trimmed[0] == {"role": "system", "content": "be nice"}
    assert trimmed[-1]["content"] == "new question"


def test_trim_never_leaves_orphaned_tool_result():
    messages = [
        {"role": "user", "content": "old " * 200},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "result " * 200, "tool_call_id": "1"},
        {"role": "user", "content": "new question"},
    ]
    trimmed = trim_messages(messages, max_tokens=30, min_recent=1)
    assert message_kind(trimmed[0]) != "tool"
    assert trimmed[-1]["content"] == "new question"


def test_build_messages_appends_prompt_and_trims():
    history = [
        {"role": "user", "content": "old " * 200},
        {"role": "assistant", "content": "old answer " * 200},
    ]
    messages = build_messages(history, "new question", max_tokens=30, min_recent=1)
    assert messages[-1] == {"role": "user", "content": "new question"}
    assert len(messages) < 3
