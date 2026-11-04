"""Tracing: the research pipeline's stages are spans, carrying counts and
names but never content; off (and free) without an OTLP endpoint."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app import telemetry
from app.models.schemas import RAGSettings
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.test_rag_retrieval import RETRIEVED, _llm_client, _service


@pytest.fixture
def spans():
    """Route the API's global tracer into memory for one test."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("casecite-test")
    with patch.object(telemetry, "_tracer", tracer):
        yield exporter


def test_tracing_is_off_without_an_endpoint():
    app = MagicMock()
    with patch(
        "app.config.settings",
        SimpleNamespace(otel_exporter_otlp_endpoint="", otel_service_name="x", debug=True),
    ):
        assert telemetry.configure_tracing(app) is False
    assert telemetry.tracing_status()["enabled"] is False
    # Spans are no-ops: no provider, no cost, no error.
    with telemetry.span("rag.retrieve", passages=3):
        telemetry.annotate(anything="fine")


def test_a_bad_endpoint_is_logged_not_fatal():
    app = MagicMock()
    with (
        patch(
            "app.config.settings",
            SimpleNamespace(
                otel_exporter_otlp_endpoint="http://collector:4318",
                otel_service_name="casecite",
                debug=True,
            ),
        ),
        patch(
            "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
            side_effect=RuntimeError("no"),
        ),
    ):
        assert telemetry.configure_tracing(app) is False
    status = telemetry.tracing_status()
    assert status["enabled"] is False and "RuntimeError" in status["error"]


async def test_the_research_pipeline_is_three_spans_without_content(spans):
    service = _service(openai=_llm_client("The rent is due on the first."))
    with (
        patch("app.services.rag.service.search_documents", AsyncMock(return_value=RETRIEVED)),
        patch("app.services.rag.service.ground_answer", AsyncMock(return_value=None)),
        patch("app.services.rag.service.enrich_citations_with_reasoning", AsyncMock()),
    ):
        await service.generate_response(
            query="When is rent due under the lease? The tenant is Marlow Holdings.",
            include_case_law=False,
            user_id="user-1",
            rag_settings=RAGSettings(query_expansion=False),
        )
    names = [s.name for s in spans.get_finished_spans()]
    assert "rag.retrieve" in names
    assert "rag.answer" in names
    assert "rag.citations" in names
    by_name = {s.name: s for s in spans.get_finished_spans()}
    assert by_name["rag.retrieve"].attributes["rag.passages"] == len(RETRIEVED)
    assert by_name["rag.answer"].attributes["gen_ai.request.model"]
    # Nothing a span carries is the question, the prompt or document text.
    for s in spans.get_finished_spans():
        for value in s.attributes.values():
            assert "Marlow" not in str(value)
            assert "rent" not in str(value).lower()


def test_usage_annotates_the_current_span(spans):
    from app.services.usage import tokens_from_response

    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15)
    )
    with telemetry.span("call"):
        prompt, completion, _ = tokens_from_response("openai", response)
        telemetry.annotate(
            **{"gen_ai.usage.input_tokens": prompt, "gen_ai.usage.output_tokens": completion}
        )
    (finished,) = spans.get_finished_spans()
    assert finished.attributes["gen_ai.usage.input_tokens"] == 11
    assert finished.attributes["gen_ai.usage.output_tokens"] == 4
    assert trace.get_current_span() is not None
