"""
Hosted reranking: a precision layer without shipping PyTorch.

The reranker must reorder by its own relevance score, keep the measured
similarity untouched, respect the AI-provider allowlist (passages leave the
instance), and fall back to the fused order on any failure.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.services import reranking
from app.services.provider_policy import ProviderNotAllowedError


def _settings(**overrides):
    return reranking.settings.model_copy(update=overrides)


def _results():
    return [
        {
            "id": "a",
            "text": "Rent is due monthly.",
            "similarity": 0.7,
            "metadata": {"filename": "lease.pdf"},
        },
        {
            "id": "b",
            "text": "A late charge of five percent applies.",
            "similarity": 0.5,
            "metadata": {"filename": "lease.pdf", "section": "2.2 Late Charge"},
        },
        {"id": "c", "text": "Governing law is Maine.", "similarity": 0.4, "metadata": {}},
    ]


class _FakeClient:
    """Stands in for httpx.AsyncClient; records the request it was sent."""

    sent: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.sent = {"url": url, "headers": headers, "json": json}
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )


class TestAvailability:
    def test_none_never_reranks(self):
        with patch.object(reranking, "settings", _settings(reranker_provider="none")):
            assert reranking.reranker_available() is False

    def test_cohere_needs_a_key(self):
        with patch.object(
            reranking, "settings", _settings(reranker_provider="cohere", cohere_api_key=None)
        ):
            assert reranking.reranker_available() is False
            assert reranking.reranker_available(MagicMock(cohere="user-key")) is True


class TestCohereRerank:
    @pytest.mark.asyncio
    async def test_reorders_by_relevance_and_keeps_similarity(self):
        with (
            patch.object(
                reranking, "settings", _settings(reranker_provider="cohere", cohere_api_key="k")
            ),
            patch.object(reranking.httpx, "AsyncClient", _FakeClient),
            patch("app.services.provider_policy.enforce_provider_allowed"),
        ):
            ranked = await reranking.rerank("late fee?", _results(), top_k=2)

        assert [r["id"] for r in ranked] == ["b", "a"]
        assert ranked[0]["rerank_score"] == 0.95
        assert ranked[0]["similarity"] == 0.5  # the measured cosine is untouched
        sent = _FakeClient.sent["json"]
        assert sent["query"] == "late fee?" and sent["top_n"] == 3
        assert sent["documents"][1].startswith("lease.pdf\n2.2 Late Charge")  # context travels
        assert _FakeClient.sent["headers"]["Authorization"] == "Bearer k"

    @pytest.mark.asyncio
    async def test_user_key_is_preferred(self):
        with (
            patch.object(
                reranking,
                "settings",
                _settings(reranker_provider="cohere", cohere_api_key="instance"),
            ),
            patch.object(reranking.httpx, "AsyncClient", _FakeClient),
            patch("app.services.provider_policy.enforce_provider_allowed"),
        ):
            await reranking.rerank("q", _results(), top_k=3, user_keys=MagicMock(cohere="mine"))
        assert _FakeClient.sent["headers"]["Authorization"] == "Bearer mine"

    @pytest.mark.asyncio
    async def test_unapproved_provider_keeps_fused_order_and_sends_nothing(self):
        _FakeClient.sent = {}
        with (
            patch.object(
                reranking, "settings", _settings(reranker_provider="cohere", cohere_api_key="k")
            ),
            patch.object(reranking.httpx, "AsyncClient", _FakeClient),
            patch(
                "app.services.provider_policy.enforce_provider_allowed",
                side_effect=ProviderNotAllowedError("cohere is not approved"),
            ),
        ):
            ranked = await reranking.rerank("q", _results(), top_k=3)
        assert [r["id"] for r in ranked] == ["a", "b", "c"]
        assert _FakeClient.sent == {}

    @pytest.mark.asyncio
    async def test_http_errors_keep_fused_order(self):
        class Failing(_FakeClient):
            async def post(self, url, headers=None, json=None):
                raise httpx.ConnectError("down")

        with (
            patch.object(
                reranking, "settings", _settings(reranker_provider="cohere", cohere_api_key="k")
            ),
            patch.object(reranking.httpx, "AsyncClient", Failing),
            patch("app.services.provider_policy.enforce_provider_allowed"),
        ):
            ranked = await reranking.rerank("q", _results(), top_k=2)
        assert [r["id"] for r in ranked] == ["a", "b"]
