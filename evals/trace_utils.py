from __future__ import annotations

import ast
import json
import os
import socket
import time
from urllib.parse import urlparse

import httpx

AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8010")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")


def port_is_open(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def jaeger_get(path: str, **params: object) -> dict:
    response = httpx.get(f"{JAEGER_URL}{path}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def tags(span: dict) -> dict[str, object]:
    return {tag["key"]: tag["value"] for tag in span.get("tags", [])}


def span_by_operation(trace: dict, operation: str) -> dict | None:
    for span in trace["spans"]:
        if span["operationName"] == operation:
            return span
    return None


def process_service(trace: dict, span: dict) -> str:
    return trace["processes"][span["processID"]]["serviceName"]


def parent_span_id(span: dict) -> str | None:
    for reference in span.get("references", []):
        if reference.get("refType") == "CHILD_OF":
            return reference.get("spanID")
    return span.get("parentSpanID")


def parent_span(trace: dict, span: dict) -> dict | None:
    parent_id = parent_span_id(span)
    if parent_id is None:
        return None
    for candidate in trace["spans"]:
        if candidate["spanID"] == parent_id:
            return candidate
    return None


def agent_is_up() -> bool:
    try:
        response = httpx.get(f"{AGENT_URL}/healthz", timeout=2)
        return (
            response.status_code == 200
            and response.json().get("service") == "agent"
        )
    except (httpx.HTTPError, ValueError):
        return False


def agent_tools() -> str | None:
    try:
        response = httpx.get(f"{AGENT_URL}/healthz", timeout=2)
        response.raise_for_status()
        return response.json().get("tools")
    except (httpx.HTTPError, ValueError):
        return None


def send_prompt(
    message: str,
    history: list[dict] | None = None,
    timeout: float = 120,
) -> tuple[str | None, dict]:
    """Send one prompt and return ``(trace_id, response_payload)``.

    The agent returns the W3C trace ID of the request. Tests query Jaeger by
    that ID instead of correlating on wall-clock time: the agent runs in a
    container whose clock can be far enough from the host clock to make a
    timestamp window unreliable.
    """
    response = httpx.post(
        f"{AGENT_URL}/prompt",
        json={"message": message, "history": history or []},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("trace_id"), payload


def trace_by_id(
    trace_id: str | None,
    expected_services: set[str] | None = None,
    expected_tool: str | None = None,
    expected_spans: set[tuple[str, str]] | None = None,
    timeout: float = 30,
) -> dict:
    """Poll Jaeger for the exact trace ID returned by the agent.

    A trace can appear before every span has been exported, so the helper keeps
    polling until the expected services, tool call, and spans are present. The
    last span to arrive is usually the root ``POST /prompt`` span, which ends
    after the response is sent.
    """
    if not trace_id:
        raise ValueError(
            "agent response did not include a trace_id; telemetry may be disabled"
        )

    deadline = time.time() + timeout
    last_seen: dict[str, object] = {"services": [], "tools": [], "spans": 0}
    while time.time() < deadline:
        try:
            traces = jaeger_get(f"/api/traces/{trace_id}")["data"]
        except httpx.HTTPError:
            traces = []
        if traces:
            trace = traces[0]
            services = {
                process["serviceName"] for process in trace["processes"].values()
            }
            tools = {
                tags(span).get("gen_ai.tool.name")
                for span in trace["spans"]
                if span["operationName"].startswith("execute_tool ")
            }
            last_seen = {
                "services": sorted(services),
                "tools": sorted(tool for tool in tools if tool),
                "spans": len(trace["spans"]),
            }
            services_ok = expected_services is None or expected_services <= services
            tool_ok = expected_tool is None or expected_tool in tools
            spans_ok = (
                expected_spans is None
                or expected_spans <= trace_span_set(trace)
            )
            if services_ok and tool_ok and spans_ok:
                return trace
        time.sleep(0.5)
    raise TimeoutError(
        f"trace {trace_id} did not reach Jaeger with "
        f"{expected_services or 'any service'}, tool {expected_tool or 'any'}, "
        f"and {len(expected_spans) if expected_spans else 0} spans; "
        f"last seen {last_seen}"
    )


def wait_for_services(expected: set[str], timeout: float = 15) -> set[str]:
    deadline = time.time() + timeout
    services: set[str] = set()
    while time.time() < deadline:
        services = set(jaeger_get("/api/services")["data"])
        if expected <= services:
            return services
        time.sleep(0.5)
    raise TimeoutError(f"services {expected - services} did not reach Jaeger")


def trace_edges(trace: dict) -> set[tuple[str, str]]:
    """Return the directed ``(parent_service, child_service)`` edges."""
    edges: set[tuple[str, str]] = set()
    for span in trace["spans"]:
        parent = parent_span(trace, span)
        if parent is None:
            continue
        edges.add((process_service(trace, parent), process_service(trace, span)))
    return edges


def trace_span_set(trace: dict) -> set[tuple[str, str]]:
    return {
        (process_service(trace, span), span["operationName"])
        for span in trace["spans"]
    }


def _parse_arguments_string(value: str) -> dict | None:
    """Parse JSON first, then Python-repr strings from the instrumentation."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
    return parsed if isinstance(parsed, dict) else None


def parse_tool_arguments(value: object) -> dict:
    """Extract the model's tool arguments from the instrumentation shape.

    The built-in ``execute_tool`` span wraps the arguments in ``input_str``.
    The MCP client span uses ``arguments``. Both are strings inside a larger
    attribute, and the instrumentation may use JSON or a Python repr, so parse
    the inner value rather than returning the wrapper.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        parsed = _parse_arguments_string(value)
        if parsed is None:
            return {"_raw": value}
        return parse_tool_arguments(parsed)
    if isinstance(value, dict):
        if "input_str" in value:
            parsed = _parse_arguments_string(str(value["input_str"]))
            return parsed if parsed is not None else {"_raw": value["input_str"]}
        if isinstance(value.get("arguments"), dict):
            return value["arguments"]
        return value
    return {"_raw": value}


def tool_calls(trace: dict) -> list[dict]:
    """Return the agent's ordered tool calls with parsed arguments."""
    calls: list[dict] = []
    for span in sorted(trace["spans"], key=lambda item: item["startTime"]):
        if process_service(trace, span) != "agent":
            continue
        span_tags = tags(span)
        if span_tags.get("gen_ai.operation.name") != "execute_tool":
            continue
        # The MCP client also emits a `list_products.tool` span with the same
        # operation name. Count the agent's `execute_tool <name>` span once.
        if not span["operationName"].startswith("execute_tool "):
            continue
        name = span_tags.get("gen_ai.tool.name")
        if not name:
            continue
        calls.append(
            {
                "name": name,
                "arguments": parse_tool_arguments(
                    span_tags.get("gen_ai.tool.call.arguments")
                ),
            }
        )
    return calls


def final_answer(payload: dict) -> str | None:
    for message in reversed(payload["response"]["messages"]):
        kind = message.get("role") or message.get("type")
        if kind in {"assistant", "ai"} and not message.get("tool_calls"):
            return message.get("content")
    return None


def normalized_trace(trace: dict, attributes: tuple[str, ...]) -> list[dict]:
    """Reduce a trace to a stable, sorted shape for golden comparison.

    Volatile fields (IDs, timestamps, durations, content) are removed. The
    result still records every span, its service, its operation, its parent,
    and the selected semantic attributes.
    """
    rows: list[dict] = []
    for span in trace["spans"]:
        parent = parent_span(trace, span)
        span_tags = tags(span)
        rows.append(
            {
                "service": process_service(trace, span),
                "operation": span["operationName"],
                "parent": (
                    f"{process_service(trace, parent)}:"
                    f"{parent['operationName']}"
                    if parent is not None
                    else None
                ),
                "attributes": {
                    key: span_tags[key]
                    for key in attributes
                    if key in span_tags
                },
            }
        )
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
