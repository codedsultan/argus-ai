from __future__ import annotations

import json
from typing import Any

Message = dict[str, Any]

# LangChain serializes messages with a `type` field; callers usually send
# OpenAI-style `role` fields. Treat both as the same conversation.
_TYPE_ALIASES = {
    "human": "user",
    "ai": "assistant",
}


def message_kind(message: Message) -> str:
    """Return a normalized role for either OpenAI-style or LangChain messages."""
    role = message.get("role")
    if isinstance(role, str) and role:
        return role
    type_ = message.get("type")
    if isinstance(type_, str):
        return _TYPE_ALIASES.get(type_, type_)
    return "unknown"


def _text_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, default=str))


def estimate_tokens(messages: list[Message]) -> int:
    """Estimate tokens as ~4 characters per token plus per-message overhead.

    This is intentionally dependency-free and only needs to be good enough to
    drive trimming. Swap in a real tokenizer if you need exact counts; the
    trimming logic does not depend on how the number is produced.
    """
    chars = 0
    for message in messages:
        chars += 16  # per-message overhead (role, delimiters, metadata)
        chars += _text_size(message.get("content"))
        if message.get("tool_calls"):
            chars += _text_size(message["tool_calls"])
    return (chars + 3) // 4


def trim_messages(
    messages: list[Message],
    max_tokens: int,
    min_recent: int = 2,
) -> list[Message]:
    """Drop the oldest messages until the estimate fits the budget.

    Invariants:

    - A leading system message is preserved.
    - The last ``min_recent`` messages are kept when possible, so the newest
      user prompt is never dropped.
    - Tool results are never left without the assistant turn that produced
      them; dropping such a turn also drops its results.
    - ``max_tokens <= 0`` disables trimming.
    """
    kept = list(messages)
    if max_tokens <= 0 or estimate_tokens(kept) <= max_tokens:
        return kept

    head: list[Message] = []
    if kept and message_kind(kept[0]) == "system":
        head.append(kept.pop(0))

    floor = max(1, min_recent)
    while len(kept) > floor and estimate_tokens(head + kept) > max_tokens:
        kept.pop(0)
        # The assistant tool-call turn is gone, so its results are orphans.
        while len(kept) > 1 and message_kind(kept[0]) == "tool":
            kept.pop(0)

    # Guard against a history that already started with an orphaned result.
    while len(kept) > 1 and message_kind(kept[0]) == "tool":
        kept.pop(0)

    return head + kept


def build_messages(
    history: list[Message] | None,
    input_prompt: str,
    max_tokens: int,
    min_recent: int = 2,
) -> list[Message]:
    """Append the new prompt to the caller-supplied history and trim it."""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": input_prompt})
    return trim_messages(messages, max_tokens, min_recent)
