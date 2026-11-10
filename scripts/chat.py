"""Minimal stateless chat client.

The agent keeps no conversation state. This client owns the history and sends it
back on every request, which is the point of module 5. It is also a stand-in for
the public web client: same `POST /prompt` contract, no browser.

Usage:

    PYTHONPATH=. python scripts/chat.py
    PYTHONPATH=. python scripts/chat.py --once "Show all products."
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx


def final_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        kind = message.get("role") or message.get("type")
        if kind in {"assistant", "ai"} and not message.get("tool_calls"):
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return "(no final answer)"


def send(
    client: httpx.Client,
    message: str,
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    response = client.post(
        "/prompt",
        json={"message": message, "history": history},
    )
    response.raise_for_status()
    messages = response.json()["response"]["messages"]
    print(f"agent> {final_text(messages)}\n")
    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Stateless chat client")
    parser.add_argument(
        "--agent",
        default=os.getenv("AGENT_URL", "http://localhost:8010"),
        help="agent base URL",
    )
    parser.add_argument(
        "--once",
        metavar="MESSAGE",
        help="send one message and exit",
    )
    args = parser.parse_args()

    history: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.agent, timeout=120) as client:
        if args.once is not None:
            send(client, args.once, history)
            return

        print("Chat with the agent. Type 'exit' or Ctrl-D to quit.")
        while True:
            try:
                message = input("you> ").strip()
            except EOFError:
                print()
                break
            if not message or message.lower() in {"exit", "quit"}:
                break
            history = send(client, message, history)


if __name__ == "__main__":
    main()
