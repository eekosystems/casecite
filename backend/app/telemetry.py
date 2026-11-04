"""Distributed tracing (OpenTelemetry), on when an OTLP endpoint is configured.

Set ``OTEL_EXPORTER_OTLP_ENDPOINT`` (for example ``http://otel-collector:4318``)
and every request becomes a trace: the HTTP request (FastAPI), its database
statements (SQLAlchemy), outbound calls to providers and CourtListener
(httpx), and the stages of the research pipeline — retrieval, the answer,
citation verification — as named child spans carrying the model, the
intent, how many passages were retrieved and the tokens each provider call
reported. Traces go to the collector over OTLP/HTTP; the collector forwards
them to Jaeger, Tempo, Honeycomb, Datadog or whatever the firm runs.

Without the endpoint nothing is exported and the spans in the code are
no-ops (the OpenTelemetry API's default tracer), so the pipeline carries no
tracing cost in a deployment that does not want it. Request bodies, prompts
and document text are never put on a span: attributes are counts, names
and identifiers only.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("casecite")
_status: dict[str, Any] = {"enabled": False, "endpoint": None, "error": None}


def tracing_status() -> dict[str, Any]:
    return dict(_status)


def configure_tracing(app, *, engine=None) -> bool:
    """Install the SDK, the OTLP exporter and the instrumentations when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. Returns True when tracing is on.
    A failure to configure is logged and leaves the app untraced."""
    from app.config import settings

    endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from app import __version__

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.otel_service_name or "casecite",
                    "service.version": __version__,
                    "deployment.environment": "development" if settings.debug else "production",
                }
            )
        )
        exporter_endpoint = endpoint.rstrip("/")
        if not exporter_endpoint.endswith("/v1/traces"):
            exporter_endpoint = f"{exporter_endpoint}/v1/traces"
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=exporter_endpoint))
        )
        # Probes are noise; everything else is traced.
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls="health,ready,metrics", tracer_provider=provider
        )
        if engine is not None:
            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        # Published last: a failure above leaves the process's tracer a no-op
        # instead of exporting into the void.
        trace.set_tracer_provider(provider)
        _status.update(enabled=True, endpoint=exporter_endpoint, error=None)
        logger.info("Tracing enabled: OTLP/HTTP to %s", exporter_endpoint)
        return True
    except Exception as e:  # noqa: BLE001 - tracing must never stop the app
        _status.update(enabled=False, endpoint=endpoint, error=f"{type(e).__name__}: {e}")
        logger.error("Tracing could not be configured (%s); continuing without it", e)
        return False


@contextmanager
def span(name: str, **attributes: Any):
    """A named span around a pipeline stage. A no-op without a provider."""
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def annotate(**attributes: Any) -> None:
    """Add attributes to whatever span is current (no-op without one)."""
    current = trace.get_current_span()
    if current is None or not current.is_recording():
        return
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value)
