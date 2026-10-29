"""Key-terms extraction: clause-anchored, quote-verified, honest about gaps."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.contract_analysis.key_terms import _collect_excerpts, extract_key_terms

CONTRACT = (
    'This Agreement is effective as of January 5, 2026 (the "Effective Date").\n'
    "GOVERNING LAW. This Agreement shall be governed by the laws of the State of Delaware.\n"
    "LIMITATION OF LIABILITY. Liability is capped at the fees paid in the twelve months "
    "preceding the claim.\n"
)


def _client_with(payload: dict) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


class TestCollectExcerpts:
    def test_preamble_always_included(self):
        excerpts = _collect_excerpts(CONTRACT, [])
        assert excerpts[0][0] == "preamble"
        assert "Effective Date" in excerpts[0][1]

    def test_tagged_spans_become_labeled_excerpts(self):
        start = CONTRACT.index("GOVERNING LAW")
        tags = [{"canonical_slug": "governing_law", "span_start": start, "span_end": start + 90}]
        excerpts = _collect_excerpts(CONTRACT, tags)
        labels = [label for label, _ in excerpts]
        assert "governing_law" in labels


class TestExtractKeyTerms:
    @pytest.mark.asyncio
    async def test_verified_quote_gets_real_offsets(self):
        quote = "This Agreement shall be governed by the laws of the State of Delaware."
        payload = {
            "fields": {
                "governing_law": {"value": "Delaware", "quote": quote, "not_found": False},
                "term": {"value": "", "not_found": True},
            }
        }
        result = await extract_key_terms(_client_with(payload), CONTRACT, [])
        assert "term" not in result
        gl = result["governing_law"]
        assert gl["value"] == "Delaware"
        assert gl["verified"] is True
        assert CONTRACT[gl["span_start"] : gl["span_end"]] == quote

    @pytest.mark.asyncio
    async def test_fabricated_quote_is_marked_unverified(self):
        payload = {
            "fields": {
                "liability_cap": {
                    "value": "Capped at 12 months' fees",
                    "quote": "This sentence does not appear in the contract.",
                    "not_found": False,
                }
            }
        }
        result = await extract_key_terms(_client_with(payload), CONTRACT, [])
        cap = result["liability_cap"]
        assert cap["verified"] is False
        assert cap["span_start"] is None

    @pytest.mark.asyncio
    async def test_no_client_returns_empty(self):
        assert await extract_key_terms(None, CONTRACT, []) == {}

    @pytest.mark.asyncio
    async def test_llm_failure_is_never_fatal(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=ConnectionError("down"))
        with patch(
            "app.services.contract_analysis.key_terms.openai_chat",
            side_effect=ConnectionError("down"),
        ):
            assert await extract_key_terms(client, CONTRACT, []) == {}
