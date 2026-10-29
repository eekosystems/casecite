"""Contract comparison: quote-verified alignment, honest about fabrication."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.contract_analysis.compare import _sections, compare_contracts

CONTRACT_A = (
    "MASTER SERVICES AGREEMENT\n\n"
    "TERM. This Agreement shall have an initial term of one (1) year from the Effective Date.\n\n"
    "GOVERNING LAW. This Agreement shall be governed by the laws of the State of Delaware.\n\n"
    "LIMITATION OF LIABILITY. Liability is capped at the fees paid in the twelve months "
    "preceding the claim.\n"
)

CONTRACT_B = (
    "MASTER SERVICES AGREEMENT\n\n"
    "TERM. This Agreement shall have an initial term of three (3) years from the Effective Date.\n\n"
    "GOVERNING LAW. This Agreement shall be governed by the laws of the State of Delaware.\n\n"
    "INDEMNIFICATION. Vendor shall indemnify Customer against third-party claims arising "
    "from Vendor's breach of this Agreement.\n"
)


def _client_with(payload: dict) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


class TestSections:
    def test_headed_contract_yields_titled_sections(self):
        sections = _sections(CONTRACT_A)
        assert sections
        assert all({"title", "start", "end", "preview"} <= set(s) for s in sections)
        joined = " ".join(s["title"] for s in sections)
        assert "TERM" in joined or "GOVERNING" in joined

    def test_short_doc_falls_back_to_single_section(self):
        sections = _sections("Short note.")
        assert len(sections) == 1
        assert sections[0]["start"] == 0


class TestCompareContracts:
    @pytest.mark.asyncio
    async def test_verified_quotes_get_real_spans(self):
        quote_a = "initial term of one (1) year"
        quote_b = "initial term of three (3) years"
        payload = {
            "clauses": [
                {
                    "topic": "Term",
                    "status": "changed",
                    "summary": "Term extended from one to three years, favoring the vendor.",
                    "quote_a": quote_a,
                    "quote_b": quote_b,
                }
            ],
            "overall": "The term tripled.",
        }
        result = await compare_contracts(_client_with(payload), CONTRACT_A, CONTRACT_B)
        assert result["overall"] == "The term tripled."
        assert result["label_a"] == "Version A"
        assert result["label_b"] == "Version B"
        clause = result["clauses"][0]
        assert clause["verified_a"] is True
        assert clause["verified_b"] is True
        assert CONTRACT_A[clause["span_a_start"] : clause["span_a_end"]] == quote_a
        assert CONTRACT_B[clause["span_b_start"] : clause["span_b_end"]] == quote_b

    @pytest.mark.asyncio
    async def test_fabricated_quote_is_nulled_and_unverified(self):
        payload = {
            "clauses": [
                {
                    "topic": "Limitation of Liability",
                    "status": "removed",
                    "summary": "The liability cap was dropped.",
                    "quote_a": "This sentence appears nowhere in either contract.",
                    "quote_b": "",
                }
            ],
            "overall": "Cap removed.",
        }
        result = await compare_contracts(_client_with(payload), CONTRACT_A, CONTRACT_B)
        clause = result["clauses"][0]
        assert clause["verified_a"] is False
        assert clause["quote_a"] is None
        assert clause["span_a_start"] is None
        assert clause["span_a_end"] is None
        # No quote proposed for B (clause absent there) — nothing to verify.
        assert clause["quote_b"] is None
        assert clause["verified_b"] is None

    @pytest.mark.asyncio
    async def test_added_clause_verifies_only_against_b(self):
        quote_b = "Vendor shall indemnify Customer against third-party claims"
        payload = {
            "clauses": [
                {
                    "topic": "Indemnification",
                    "status": "added",
                    "summary": "New indemnity from Vendor, favoring Customer.",
                    "quote_a": "",
                    "quote_b": quote_b,
                }
            ],
            "overall": "Indemnity added.",
        }
        result = await compare_contracts(_client_with(payload), CONTRACT_A, CONTRACT_B)
        clause = result["clauses"][0]
        assert clause["status"] == "added"
        assert clause["verified_b"] is True
        assert CONTRACT_B[clause["span_b_start"] : clause["span_b_end"]] == quote_b
        assert clause["quote_a"] is None
        assert clause["verified_a"] is None

    @pytest.mark.asyncio
    async def test_custom_labels_pass_through(self):
        payload = {"clauses": [], "overall": "Identical."}
        result = await compare_contracts(
            _client_with(payload), CONTRACT_A, CONTRACT_B, label_a="Redline", label_b="Template"
        )
        assert result["label_a"] == "Redline"
        assert result["label_b"] == "Template"

    @pytest.mark.asyncio
    async def test_clause_list_capped_at_40(self):
        payload = {
            "clauses": [
                {
                    "topic": f"Topic {i}",
                    "status": "changed",
                    "summary": "x",
                    "quote_a": "",
                    "quote_b": "",
                }
                for i in range(45)
            ],
            "overall": "Many changes.",
        }
        result = await compare_contracts(_client_with(payload), CONTRACT_A, CONTRACT_B)
        assert len(result["clauses"]) == 40

    @pytest.mark.asyncio
    async def test_none_client_raises_value_error(self):
        with pytest.raises(ValueError):
            await compare_contracts(None, CONTRACT_A, CONTRACT_B)

    @pytest.mark.asyncio
    async def test_llm_failure_returns_shaped_failure(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=ConnectionError("down"))
        result = await compare_contracts(client, CONTRACT_A, CONTRACT_B)
        assert result == {
            "overall": "Comparison failed.",
            "clauses": [],
            "label_a": "Version A",
            "label_b": "Version B",
        }

    @pytest.mark.asyncio
    async def test_malformed_json_returns_shaped_failure(self):
        client = MagicMock()
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "not json at all"
        client.chat.completions.create = AsyncMock(return_value=response)
        result = await compare_contracts(client, CONTRACT_A, CONTRACT_B)
        assert result["overall"] == "Comparison failed."
        assert result["clauses"] == []
