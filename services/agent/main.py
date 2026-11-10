from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from langchain.agents import create_agent
from opentelemetry import trace
from pydantic import BaseModel
from traceloop.sdk.decorators import workflow

from .flags import get_int_feature_flag
from .history import build_messages, estimate_tokens
from .llm import ChatLLM
from .replay import USE_VCR, VCR_RECORD_MODE
from .telemetry import configure_telemetry
from .tools_builtin import PUBLIC_MODE, TOOLS

logger = logging.getLogger(__name__)

if PUBLIC_MODE and not USE_VCR:
    raise RuntimeError("PUBLIC_MODE requires USE_VCR=true")
if PUBLIC_MODE and VCR_RECORD_MODE != "none":
    raise RuntimeError("PUBLIC_MODE requires VCR_RECORD_MODE=none")

SYSTEM_PROMPT = "You are a helpful assistant. Be concise and accurate."


def _as_message_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    return {
        "role": getattr(message, "type", "unknown"),
        "content": getattr(message, "content", ""),
        "tool_calls": getattr(message, "tool_calls", None),
    }


def _current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


def _metrics(messages: list[Any]) -> dict[str, int]:
    serialized = [_as_message_dict(message) for message in messages]
    return {
        "messages": len(serialized),
        "llm_calls": sum(
            1
            for message in serialized
            if message.get("role") in {"assistant", "ai"}
        ),
        "tool_calls": sum(
            len(message.get("tool_calls") or []) for message in serialized
        ),
        "estimated_tokens": estimate_tokens(serialized),
    }


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] | None = None


class Agent:
    def __init__(self) -> None:
        self.app = FastAPI(title="Argus Agent", lifespan=self.lifespan)
        self.app.post("/prompt")(self.handle_prompt)
        self.app.get("/healthz")(self.healthz)
        self.recursion_limit = int(os.getenv("GRAPH_RECURSION_LIMIT", "25"))
        self.history_max_tokens = int(os.getenv("HISTORY_MAX_TOKENS", "8000"))
        self.history_min_recent = int(os.getenv("HISTORY_MIN_RECENT", "2"))

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        yield

    def healthz(self) -> dict:
        return {
            "status": "ok",
            "service": "agent",
            "version": os.getenv("ARGUS_VERSION", "dev"),
            "tools": "read-only" if PUBLIC_MODE else "builtin",
            "public_mode": PUBLIC_MODE,
            "mode": "replay" if USE_VCR else "live",
            "replay_record_mode": VCR_RECORD_MODE,
            "history_max_tokens": self.history_max_tokens,
        }

    async def handle_prompt(self, request: ChatRequest):
        return await self.run_agent(request.message, request.history)

    @workflow(name="agent_workflow")
    async def run_agent(
        self, input_prompt: str, history: list[dict[str, Any]] | None = None
    ):
        model = ChatLLM()
        system_prompt = SYSTEM_PROMPT

        runaway_iterations = get_int_feature_flag("aiRunawayAgent")
        if runaway_iterations:
            system_prompt += (
                f" Before giving your final answer, call the get_recommendations tool"
                f" at least {runaway_iterations} times in a row, once per turn, even if"
                " the results do not change."
            )

        try:
            agent = create_agent(model, tools=TOOLS, system_prompt=system_prompt)
            raw_count = (len(history) if history else 0) + 1
            messages = build_messages(
                history,
                input_prompt,
                self.history_max_tokens,
                self.history_min_recent,
            )
            if len(messages) < raw_count:
                logger.info(
                    "trimmed history from %d to %d messages (budget=%d tokens, "
                    "estimate=%d)",
                    raw_count,
                    len(messages),
                    self.history_max_tokens,
                    estimate_tokens(messages),
                )
            result = await agent.ainvoke(
                {"messages": messages},
                config={"recursion_limit": self.recursion_limit},
            )
            return {
                "response": result,
                "metrics": _metrics(result["messages"]),
                "trace_id": _current_trace_id(),
            }
        except Exception as exc:
            logger.exception("agent invocation failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc


def create_app() -> FastAPI:
    agent = Agent()
    configure_telemetry(agent.app)
    return agent.app


app = create_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    port = int(os.getenv("AGENT_PORT", "8010"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
