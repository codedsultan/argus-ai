from __future__ import annotations

import os

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from traceloop.sdk import Traceloop


def configure_telemetry(app) -> None:
    if os.getenv("OTEL_ENABLED", "true").lower() != "true":
        return

    # This lab is low-volume and the evals query Jaeger by trace ID right after
    # the prompt returns. Export each span synchronously so a full batch queue
    # cannot drop the last spans of a short trace under CI load.
    Traceloop.init(
        app_name=os.getenv("OTEL_SERVICE_NAME", "agent"),
        disable_batch=True,
    )
    FastAPIInstrumentor.instrument_app(app)

    # Inject W3C trace context into outbound HTTP calls. Without this, the
    # agent and every service it calls appear as unrelated traces in Jaeger.
    # The instrumentor is idempotent, but the guard makes that explicit and
    # keeps a second configure_telemetry() call from re-wrapping the client.
    httpx_instrumentor = HTTPXClientInstrumentor()
    if not httpx_instrumentor.is_instrumented_by_opentelemetry:
        httpx_instrumentor.instrument()
