from __future__ import annotations

import json
import os
import time
import uuid

import pytest

from evals.trace_utils import (
    JAEGER_URL,
    agent_tools,
    jaeger_get,
    port_is_open,
    send_prompt,
    span_by_operation,
    tags,
    trace_by_id,
)

COLLECTOR_HTTP_URL = os.getenv(
    "COLLECTOR_HTTP_URL",
    os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
)


@pytest.mark.integration
@pytest.mark.skipif(not port_is_open(JAEGER_URL), reason="Jaeger is not running")
def test_agent_trace_has_genai_semconv_and_propagation() -> None:
    tools = agent_tools()
    if tools is None:
        pytest.skip("agent is not running")

    trace_id, _ = send_prompt("Show all available products in the store.")

    expected_services = {"agent", "shop-api"}
    trace = trace_by_id(
        trace_id,
        expected_services,
        expected_tool="list_products",
        # The workflow span ends after its LLM and tool children. Waiting for
        # it keeps the attribute assertions below from reading a partial trace.
        expected_spans={
            ("agent", "agent_workflow.workflow"),
            ("agent", "ChatLLM.chat"),
            ("agent", "execute_tool list_products"),
        },
    )
    services = {process["serviceName"] for process in trace["processes"].values()}
    assert expected_services <= services

    chat = span_by_operation(trace, "ChatLLM.chat")
    assert chat is not None
    chat_tags = tags(chat)
    assert chat_tags["gen_ai.operation.name"] == "chat"
    assert chat_tags["gen_ai.request.model"]
    assert isinstance(json.loads(chat_tags["gen_ai.input.messages"]), list)
    assert isinstance(json.loads(chat_tags["gen_ai.output.messages"]), list)

    tool = span_by_operation(trace, "execute_tool list_products")
    assert tool is not None
    tool_tags = tags(tool)
    assert tool_tags["gen_ai.operation.name"] == "execute_tool"
    assert tool_tags["gen_ai.tool.name"] == "list_products"
    assert "gen_ai.agent.name" not in tool_tags
    assert tool_tags["gen_ai.tool.call.arguments"]
    assert tool_tags["gen_ai.tool.call.result"]

    workflow = span_by_operation(trace, "agent_workflow.workflow")
    assert workflow is not None
    workflow_tags = tags(workflow)
    assert workflow_tags["gen_ai.operation.name"] == "invoke_workflow"
    assert workflow_tags["gen_ai.workflow.name"] == "agent_workflow"
    assert workflow_tags["traceloop.span.kind"] == "workflow"
    assert isinstance(json.loads(workflow_tags["gen_ai.input.messages"]), dict)


@pytest.mark.integration
@pytest.mark.skipif(not port_is_open(JAEGER_URL), reason="Jaeger is not running")
@pytest.mark.skipif(
    not port_is_open(COLLECTOR_HTTP_URL), reason="collector is not running"
)
def test_collector_redacts_and_corrects_synthetic_span() -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    run_id = uuid.uuid4().hex
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": "telemetry-test", "test.run_id": run_id}
        )
    )
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{COLLECTOR_HTTP_URL}/v1/traces"))
    )
    tracer = provider.get_tracer("telemetry-test")
    with tracer.start_as_current_span("checkout") as span:
        card = "4432-8015-6152-0454"
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "checkout")
        span.set_attribute("gen_ai.agent.name", "checkout")
        span.set_attribute(
            "gen_ai.tool.call.arguments",
            json.dumps({"creditCardNumber": card, "creditCardCvv": 672}),
        )
        span.set_attribute(
            "gen_ai.input.messages",
            json.dumps(
                [
                    {
                        "role": "user",
                        "parts": [{"type": "text", "content": f"card {card}"}],
                    }
                ]
            ),
        )
        span.set_attribute("password", "hunter2")
        span.set_attribute("authorization", "Bearer secret")
        span.set_attribute("test.run_id", run_id)
    provider.force_flush()
    provider.shutdown()

    found = None
    deadline = time.time() + 15
    while time.time() < deadline:
        traces = jaeger_get("/api/traces", service="telemetry-test", limit=20)["data"]
        for candidate in traces:
            for span in candidate["spans"]:
                if tags(span).get("test.run_id") == run_id:
                    found = (candidate, span)
                    break
            if found:
                break
        if found:
            break
        time.sleep(0.5)
    if not found:
        pytest.fail("synthetic span did not reach Jaeger")

    _, span = found
    span_tags = tags(span)
    assert span_tags["gen_ai.tool.name"] == "checkout"
    assert "gen_ai.agent.name" not in span_tags
    assert "4432-8015-6152-0454" not in span_tags["gen_ai.tool.call.arguments"]
    assert "****-****-****-****" in span_tags["gen_ai.tool.call.arguments"]
    assert "4432-8015-6152-0454" not in span_tags["gen_ai.input.messages"]
    assert span_tags["password"] == "****"
    assert span_tags["authorization"] == "****"
