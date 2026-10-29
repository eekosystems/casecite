"""
Shared-matter access and case-law filters on the answer paths.

Retrieval already searches the caller's shared matters; the passes that read
whole files afterwards (claim grounding, per-file analysis) must be able to
read the same files, or a colleague's document is silently dropped from
verification. Court and date filters a user types must reach CourtListener
instead of being accepted and ignored.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.schemas import RAGSettings
from app.services import resilience
from app.services.courtlistener_client import resolve_court_ids
from app.services.rag import search as rag_search
from app.services.rag.search import case_law_search_filters, search_case_law
from app.services.rag.service import _search_case_law_within_budget

from tests.test_rag_retrieval import RETRIEVED, _llm_client, _service


@pytest.fixture(autouse=True)
def _isolated_breakers():
    resilience._circuit_breakers.clear()
    yield
    resilience._circuit_breakers.clear()


class TestSharedMatterReads:
    @pytest.mark.asyncio
    async def test_grounding_reads_with_the_callers_matters(self):
        from app.services.rag.claim_grounding import ground_answer_claims

        doc_service = MagicMock()
        doc_service.get_document_text = AsyncMock(return_value=None)

        with patch("app.services.documents.document_service", doc_service):
            await ground_answer_claims(
                query="q",
                answer="a",
                search_results=[{"similarity": 0.5, "metadata": {"document_id": "shared"}}],
                user_id="user-1",
                accessible_matter_ids={"matter-1"},
            )

        doc_service.get_document_text.assert_awaited_once_with("shared", "user-1", {"matter-1"})

    @pytest.mark.asyncio
    async def test_answer_path_hands_matters_to_grounding(self):
        service = _service(openai=_llm_client())
        grounding = AsyncMock(return_value=None)

        with (
            patch("app.services.rag.service.search_documents", AsyncMock(return_value=RETRIEVED)),
            patch("app.services.rag.service.ground_answer", grounding),
            patch("app.services.rag.service.enrich_citations_with_reasoning", AsyncMock()),
        ):
            await service.generate_response(
                query="What happens if rent is late?",
                include_case_law=False,
                user_id="user-1",
                matter_ids=["matter-1", "matter-2"],
                rag_settings=RAGSettings(query_expansion=False),
            )

        assert grounding.call_args.kwargs["accessible_matter_ids"] == {"matter-1", "matter-2"}

    @pytest.mark.asyncio
    async def test_per_file_reads_with_the_callers_matters(self):
        from app.services.rag import per_file

        doc_service = MagicMock()
        doc_service.get_document_text = AsyncMock(return_value=None)
        doc_service.get_document = AsyncMock(return_value=None)

        with (
            patch.object(per_file, "document_service", doc_service),
            patch.object(per_file, "make_openai", return_value=MagicMock()),
        ):
            result = await per_file.per_file_answer(
                query="Compare these",
                document_ids=["a", "b"],
                user_id="user-1",
                model="gpt-5.5",
                accessible_matter_ids={"matter-1"},
            )

        assert result is None
        for call in doc_service.get_document_text.await_args_list:
            assert call.args[2] == {"matter-1"}
        for call in doc_service.get_document.await_args_list:
            assert call.args[2] == {"matter-1"}


class TestCourtResolution:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            (["9th"], ["ca9"]),
            (["9th cir."], ["ca9"]),
            (["Ninth Circuit"], ["ca9"]),
            (["ca2"], ["ca2"]),
            (["California"], ["cal", "calctapp"]),
            (["new york"], ["ny", "nyappdiv", "nysupct"]),
            (["dc"], ["cadc"]),
        ],
    )
    def test_typed_courts_become_courtlistener_ids(self, typed, expected):
        assert resolve_court_ids(typed) == expected

    def test_unrecognisable_values_are_dropped_not_sent(self):
        assert resolve_court_ids(["Southern District of Somewhere", ""]) == []

    def test_duplicates_collapse(self):
        assert resolve_court_ids(["9th", "ca9", "ninth circuit"]) == ["ca9"]


class TestCaseLawFilters:
    def test_unfiltered_search_keeps_the_citation_floor(self):
        filters = case_law_search_filters()
        assert filters["cited_gt"] == rag_search.UNFILTERED_MIN_CITATIONS
        assert filters["court_ids"] is None

    def test_narrowed_search_drops_the_citation_floor(self):
        """New authority in the chosen court must not be excluded for being uncited."""
        filters = case_law_search_filters(["9th"], "2024-01-01", None)
        assert filters == {
            "court_ids": ["ca9"],
            "date_after": date(2024, 1, 1),
            "date_before": None,
            "cited_gt": None,
        }

    @pytest.mark.asyncio
    async def test_filters_reach_courtlistener(self):
        search = AsyncMock(return_value=[])
        with patch.object(rag_search.courtlistener_service, "search_opinions", search):
            await search_case_law(
                "qualified immunity", courts=["9th"], date_from="2023-06-01", date_to=None
            )

        kwargs = search.call_args.kwargs
        assert kwargs["court_ids"] == ["ca9"]
        assert kwargs["date_after"] == date(2023, 6, 1)
        assert kwargs["cited_gt"] is None

    @pytest.mark.asyncio
    async def test_filters_typed_in_the_question_reach_the_search(self):
        fast = AsyncMock(return_value=[])
        with patch("app.services.rag.service.search_case_law", fast):
            await _search_case_law_within_budget(
                query="qualified immunity for excessive force court:9th after:2022",
                include_case_law=True,
                case_law_query="qualified immunity excessive force",
                case_law_limit=5,
                deep_case_law=False,
                openai_client=None,
                user_keys=None,
                model="gpt-5.5",
            )

        kwargs = fast.call_args.kwargs
        assert kwargs["courts"] == ["9th"]
        assert kwargs["date_from"] == date(2022, 1, 1)

    @pytest.mark.asyncio
    async def test_deep_research_gets_the_same_filters(self):
        deep = AsyncMock(return_value=([{"id": "x"}], {}))
        with patch("app.services.rag.service.research_authorities", deep):
            await _search_case_law_within_budget(
                query="duty to defend court:california",
                include_case_law=True,
                case_law_query=None,
                case_law_limit=5,
                deep_case_law=True,
                openai_client=MagicMock(),
                user_keys=None,
                model="gpt-5.5",
            )

        filters = deep.call_args.kwargs["search_filters"]
        assert filters["court_ids"] == ["cal", "calctapp"]
        assert filters["cited_gt"] is None

    @pytest.mark.asyncio
    async def test_research_pass_applies_filters_to_every_concept_search(self):
        from app.services import case_law_research

        search = AsyncMock(return_value=[])
        with (
            patch.object(
                case_law_research,
                "_craft_research_targets",
                AsyncMock(return_value=[{"query": "a"}, {"query": "b"}]),
            ),
            patch.object(case_law_research.courtlistener_service, "search_opinions", search),
        ):
            await case_law_research.research_authorities(
                question="q",
                search_query=None,
                client=MagicMock(),
                search_filters={"court_ids": ["ca9"], "cited_gt": None},
            )

        assert search.await_count == 2
        for call in search.await_args_list:
            assert call.kwargs["court_ids"] == ["ca9"]
            assert call.kwargs["cited_gt"] is None
