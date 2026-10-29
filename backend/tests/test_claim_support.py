"""
Claim support checking: a verbatim quote is not proof that the answer is right.

Grounding pins each claim to an exact passage; the support check then reads
the passage in context and says whether it supports the claim as stated. The
answer must show the claims that are not supported (and what was not checked)
instead of presenting every quoted claim as verified.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.schemas import RAGSettings
from app.services.rag import claim_grounding as cg
from app.services.rag.claim_grounding import GroundingResult, ground_answer

from tests.test_rag_retrieval import RETRIEVED, _llm_client, _service

MSA = (
    "5.2 Termination for Cause. Either Party may terminate this Agreement upon thirty (30) "
    "days' written notice to the other Party if the other Party commits a Material Breach, "
    "provided that the breaching Party fails to cure such Material Breach within the thirty "
    "(30) day notice period."
)
ANSWER = "Either party can terminate the agreement at any time with 30 days' notice."


def _reply(payload) -> MagicMock:
    return MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps(payload)))])


def _client(*payloads) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=[_reply(p) for p in payloads])
    return client


def _docs(texts: dict[str, str]) -> MagicMock:
    service = MagicMock()
    service.get_document_text = AsyncMock(
        side_effect=lambda did, user_id, *_: (f"{did}.txt", texts[did]) if did in texts else None
    )
    return service


GROUNDED = {
    "claims": [
        {
            "claim": "Either party may terminate on 30 days' notice",
            "answer_quote": ANSWER,
            "file_ref": "f0",
            "quote": "Either Party may terminate this Agreement upon thirty (30) days' written notice",
        }
    ]
}


async def _ground(client, texts=None, results=None, **kwargs):
    with (
        patch("app.services.documents.document_service", _docs(texts or {"msa": MSA})),
        patch.object(cg, "make_openai", return_value=client),
    ):
        return await ground_answer(
            query="Can either party terminate at will?",
            answer=kwargs.pop("answer", ANSWER),
            search_results=results or [{"similarity": 0.6, "metadata": {"document_id": "msa"}}],
            user_id="user-1",
            **kwargs,
        )


class TestSupportCheck:
    @pytest.mark.asyncio
    async def test_a_real_quote_that_drops_the_cure_right_is_flagged(self):
        client = _client(
            GROUNDED,
            {
                "items": [
                    {
                        "n": 0,
                        "label": "partial",
                        "note": "Only after a Material Breach the other party fails to cure.",
                    }
                ]
            },
        )
        result = await _ground(client)

        citation = result.citations[0]
        assert citation.verified is True  # the quote is genuine...
        assert citation.support == "partial"  # ...but it does not say what the answer says
        assert "Material Breach" in citation.support_note
        assert citation.reasoning[-1].type == "Does the source support it"

        notice = result.notice()
        assert "not fully supported" in notice
        assert ANSWER in notice and "Material Breach" in notice

        # The check saw the clause around the quote, including its proviso.
        support_prompt = client.chat.completions.create.await_args_list[1].kwargs["messages"][0][
            "content"
        ]
        assert "fails to cure" in support_prompt

    @pytest.mark.asyncio
    async def test_supported_claims_raise_no_notice(self):
        client = _client(GROUNDED, {"items": [{"n": 0, "label": "supported", "note": ""}]})
        result = await _ground(client)
        assert result.citations[0].support == "supported"
        assert result.flagged == [] and result.notice() is None

    @pytest.mark.asyncio
    async def test_claims_with_no_support_are_reported_not_dropped(self):
        payload = {
            "claims": [
                *GROUNDED["claims"],
                {
                    "claim": "The term is ten years",
                    "answer_quote": "It lasts ten years.",
                    "file_ref": None,
                    "quote": "",
                },
            ]
        }
        client = _client(payload, {"items": [{"n": 0, "label": "supported", "note": ""}]})
        result = await _ground(client)

        assert [f.label for f in result.flagged] == ["unsupported"]
        assert "It lasts ten years." in result.notice()
        assert "no supporting passage" in result.notice()
        assert len(result.citations) == 1  # no citation without a passage

    @pytest.mark.asyncio
    async def test_check_can_be_switched_off(self):
        client = _client(GROUNDED)
        result = await _ground(client, check_support=False)
        assert client.chat.completions.create.await_count == 1
        assert result.citations[0].support is None
        assert result.support_checked is False

    @pytest.mark.asyncio
    async def test_a_failed_check_keeps_the_grounded_claims(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[_reply(GROUNDED), RuntimeError("provider down")]
        )
        result = await _ground(client)
        assert result.citations[0].verified is True
        assert result.citations[0].support is None
        assert result.support_checked is False


class TestCoverage:
    @pytest.mark.asyncio
    async def test_more_files_than_the_cap_checks_the_best_and_says_so(self):
        texts = {f"d{i}": MSA for i in range(cg.GROUND_MAX_FILES + 2)}
        results = [
            {"similarity": 0.1 * i, "metadata": {"document_id": f"d{i}"}} for i in range(len(texts))
        ]
        client = _client(GROUNDED, {"items": [{"n": 0, "label": "supported", "note": ""}]})
        result = await _ground(client, texts=texts, results=results)

        assert result.files_checked == cg.GROUND_MAX_FILES
        assert result.files_total == cg.GROUND_MAX_FILES + 2
        assert (
            f"{cg.GROUND_MAX_FILES} of the {cg.GROUND_MAX_FILES + 2} source files"
            in result.notice()
        )
        grounding_prompt = client.chat.completions.create.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        # The best-matching file is in; the weakest is not.
        assert f'FILE "d{len(texts) - 1}.txt"' in grounding_prompt
        assert 'FILE "d0.txt"' not in grounding_prompt

    def test_long_answers_say_what_was_not_checked(self):
        result = GroundingResult(files_checked=1, files_total=1, answer_truncated=True)
        assert "first 12,000 characters" in result.notice()

    def test_long_files_are_read_where_retrieval_found_the_answer(self):
        filler = "Boilerplate recital. " * 3000  # far past the prompt budget
        clause = "The Indemnity Cap is $4,250,000."
        text = filler + clause + filler
        start = text.index(clause)
        excerpt = cg.file_excerpt(text, [(start, start + len(clause))])
        assert clause in excerpt
        assert len(excerpt) <= cg.GROUND_TEXT_LIMIT + 50
        assert excerpt.startswith(text[:200])  # the opening is always included

    @pytest.mark.asyncio
    async def test_claim_citations_carry_page_and_section(self):
        results = [
            {
                "similarity": 0.6,
                "metadata": {
                    "document_id": "msa",
                    "chunk_index": 4,
                    "char_start": 0,
                    "char_end": len(MSA),
                    "section": "ARTICLE 5 TERM AND TERMINATION > 5.2 Termination for Cause",
                    "page_start": 7,
                    "page_end": 7,
                },
            }
        ]
        client = _client(GROUNDED, {"items": [{"n": 0, "label": "supported", "note": ""}]})
        result = await _ground(client, results=results)
        citation = result.citations[0]
        assert citation.page_start == 7 and citation.chunk_index == 4
        assert citation.section.startswith("ARTICLE 5")
        assert "(page 7)" in citation.reasoning[1].description


class TestAnswerShowsTheNotice:
    @pytest.mark.asyncio
    async def test_flagged_claims_are_appended_to_the_answer(self):
        service = _service(openai=_llm_client(ANSWER))
        flagged = GroundingResult(
            flagged=[cg.FlaggedClaim("x", ANSWER, "partial", "Needs a Material Breach.")],
            files_checked=1,
            files_total=1,
        )
        with (
            patch("app.services.rag.service.search_documents", AsyncMock(return_value=RETRIEVED)),
            patch("app.services.rag.service.ground_answer", AsyncMock(return_value=flagged)) as g,
            patch("app.services.rag.service.enrich_citations_with_reasoning", AsyncMock()),
        ):
            result = await service.generate_response(
                query="Can either party terminate at will?",
                include_case_law=False,
                user_id="user-1",
                rag_settings=RAGSettings(query_expansion=False),
            )

        assert result["content"].startswith(ANSWER)
        assert "Source check" in result["content"]
        assert "Needs a Material Breach." in result["content"]
        assert g.call_args.kwargs["check_support"] is True

    @pytest.mark.asyncio
    async def test_citation_verification_off_skips_the_support_check(self):
        service = _service(openai=_llm_client(ANSWER))
        with (
            patch("app.services.rag.service.search_documents", AsyncMock(return_value=RETRIEVED)),
            patch("app.services.rag.service.ground_answer", AsyncMock(return_value=None)) as g,
            patch("app.services.rag.service.enrich_citations_with_reasoning", AsyncMock()),
        ):
            await service.generate_response(
                query="q",
                include_case_law=False,
                user_id="user-1",
                rag_settings=RAGSettings(query_expansion=False, citation_verification=False),
            )
        assert g.call_args.kwargs["check_support"] is False
