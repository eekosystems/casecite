"""
Unit tests for the strategy brief service.

Covers the pure coverage/citation functions (grouping, ref assignment, ref
joining, unknown-ref dropping) and full response assembly with mocked
LLM / vector search / CourtListener.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.strategy import (
    MAX_DOCUMENTS,
    SNIPPET_MAX_CHARS,
    StrategyServiceUnavailable,
    StrategySynthesisError,
    build_passages,
    group_chunks_by_document,
    join_citations,
    scale_confidence,
    strategy_service,
)


def _chunk(doc_id: str, text: str = "Some passage text.", similarity: float = 0.8) -> dict:
    return {
        "id": f"{doc_id}-chunk",
        "text": text,
        "metadata": {"document_id": doc_id, "filename": f"{doc_id}.pdf"},
        "similarity": similarity,
    }


class TestScaleConfidence:
    def test_scales_zero_to_one_range(self):
        assert scale_confidence(0.0) == 0
        assert scale_confidence(0.5) == 50
        assert scale_confidence(1.0) == 100

    def test_clamps_out_of_range_scores(self):
        assert scale_confidence(-0.3) == 0
        assert scale_confidence(1.7) == 100


class TestGroupChunksByDocument:
    def test_groups_by_document_preserving_rank_order(self):
        results = [_chunk("a", "first"), _chunk("b", "second"), _chunk("a", "third")]
        grouped = group_chunks_by_document(results, {"a", "b"})

        assert set(grouped.keys()) == {"a", "b"}
        assert [c["text"] for c in grouped["a"]] == ["first", "third"]
        assert [c["text"] for c in grouped["b"]] == ["second"]

    def test_caps_chunks_per_document(self):
        results = [_chunk("a", f"text {i}") for i in range(6)]
        grouped = group_chunks_by_document(results, {"a"}, per_doc=3)

        assert len(grouped["a"]) == 3
        assert [c["text"] for c in grouped["a"]] == ["text 0", "text 1", "text 2"]

    def test_discards_documents_outside_working_set(self):
        results = [_chunk("a"), _chunk("other-users-or-out-of-scope")]
        grouped = group_chunks_by_document(results, {"a"})

        assert set(grouped.keys()) == {"a"}

    def test_ignores_results_without_document_id(self):
        grouped = group_chunks_by_document([{"text": "orphan", "metadata": {}}], {"a"})
        assert grouped == {}


class TestBuildPassages:
    def test_assigns_sequential_refs_in_document_order(self):
        grouped = {
            "a": [_chunk("a", "alpha one"), _chunk("a", "alpha two")],
            "b": [_chunk("b", "beta one")],
        }
        passages = build_passages([("a", "alpha.pdf"), ("b", "beta.pdf")], grouped)

        assert [p["ref"] for p in passages] == ["d0", "d1", "d2"]
        assert passages[0]["filename"] == "alpha.pdf"
        assert passages[2]["document_id"] == "b"

    def test_every_covered_document_is_represented(self):
        doc_order = [(f"doc{i}", f"doc{i}.pdf") for i in range(5)]
        grouped = {doc_id: [_chunk(doc_id)] for doc_id, _ in doc_order}
        passages = build_passages(doc_order, grouped)

        assert {p["document_id"] for p in passages} == {doc_id for doc_id, _ in doc_order}

    def test_truncates_snippets(self):
        grouped = {"a": [_chunk("a", "x" * 1000)]}
        passages = build_passages([("a", "a.pdf")], grouped)

        assert len(passages[0]["snippet"]) == SNIPPET_MAX_CHARS
        # The display snippet is short; the synthesis still gets the whole chunk.
        assert len(passages[0]["text"]) == 1000

    def test_skips_empty_chunks(self):
        grouped = {"a": [_chunk("a", "   ")]}
        assert build_passages([("a", "a.pdf")], grouped) == []


class TestJoinCitations:
    def _passages(self):
        return [
            {
                "ref": "d0",
                "document_id": "a",
                "filename": "alpha.pdf",
                "snippet": "alpha passage",
                "score": 0.9,
            },
            {
                "ref": "d1",
                "document_id": "b",
                "filename": "beta.pdf",
                "snippet": "beta passage",
                "score": 0.4,
            },
        ]

    def test_resolves_refs_to_citation_ids_and_citations(self):
        llm_data = {
            "position": "Strong position.",
            "strengths": [{"point": "Good clause", "refs": ["d0", "d1"]}],
            "weaknesses": [{"point": "Late notice", "refs": ["d1"]}],
            "next_steps": [{"step": "Depose the CFO", "refs": ["d0"]}],
        }
        sections, citations = join_citations(llm_data, self._passages())

        assert sections["position"] == "Strong position."
        assert sections["strengths"][0]["citation_ids"] == ["d0", "d1"]
        assert sections["weaknesses"][0]["citation_ids"] == ["d1"]
        assert sections["next_steps"][0]["citation_ids"] == ["d0"]

        by_id = {c["id"]: c for c in citations}
        assert set(by_id) == {"d0", "d1"}
        assert by_id["d0"]["source"] == "alpha.pdf"
        assert by_id["d0"]["type"] == "document"
        assert by_id["d0"]["document_id"] == "a"
        assert by_id["d0"]["passage"] == "alpha passage"
        assert by_id["d0"]["confidence"] == 90
        assert by_id["d1"]["confidence"] == 40

    def test_drops_unknown_refs(self):
        llm_data = {
            "position": "p",
            "strengths": [{"point": "Hallucinated support", "refs": ["d0", "d99", "nonsense"]}],
        }
        sections, citations = join_citations(llm_data, self._passages())

        assert sections["strengths"][0]["citation_ids"] == ["d0"]
        assert [c["id"] for c in citations] == ["d0"]

    def test_only_cited_passages_become_citations(self):
        llm_data = {"position": "p", "strengths": [{"point": "s", "refs": ["d1"]}]}
        _, citations = join_citations(llm_data, self._passages())

        assert [c["id"] for c in citations] == ["d1"]

    def test_tolerates_malformed_llm_output(self):
        llm_data = {
            "position": None,
            "strengths": [{"refs": ["d0"]}, "not-a-dict", {"point": "  "}],
            "weaknesses": "not-a-list",
            "next_steps": [{"step": "Do a thing", "refs": "not-a-list"}],
        }
        sections, citations = join_citations(llm_data, self._passages())

        assert sections["position"] == ""
        assert sections["strengths"] == []
        assert sections["weaknesses"] == []
        assert sections["next_steps"] == [{"step": "Do a thing", "citation_ids": []}]
        assert citations == []

    def test_deduplicates_repeated_refs_within_a_point(self):
        llm_data = {"position": "p", "strengths": [{"point": "s", "refs": ["d0", "d0"]}]}
        sections, citations = join_citations(llm_data, self._passages())

        assert sections["strengths"][0]["citation_ids"] == ["d0"]
        assert len(citations) == 1


def _mock_llm_client(payload: dict) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


def _doc(doc_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=doc_id, filename=f"{doc_id}.pdf")


def _opinion(opinion_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=opinion_id,
        case_name=f"Case {opinion_id}",
        citation=[f"{opinion_id} F.3d 1"],
        absolute_url=f"https://www.courtlistener.com/opinion/{opinion_id}/",
        snippet="The court held...",
    )


def _mock_llm_client_seq(payloads: list[dict]) -> MagicMock:
    """LLM client that answers with each payload in turn, repeating the last.

    The strategy flow makes one synthesis call followed by one judge call per
    case-law candidate; tests supply [synthesis_payload, judge_payload].
    """
    client = MagicMock()
    remaining = [json.dumps(p) for p in payloads]

    async def _create(*args, **kwargs):
        content = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = content
        return response

    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


class TestGenerateBrief:
    """Response assembly with mocked LLM / vector search / CourtListener."""

    LLM_PAYLOAD = {
        "position": "The client is well positioned.",
        "strengths": [{"point": "Clear indemnity clause", "refs": ["d0"]}],
        "weaknesses": [{"point": "Notice was late", "refs": ["d1", "d99"]}],
        "next_steps": [{"step": "Collect delivery logs", "refs": ["d1"]}],
    }

    def _patches(
        self,
        doc_ids: list[str],
        search_results: list[dict],
        fallback_chunks: dict[str, list[dict]] | None = None,
        llm_client: MagicMock | None = None,
        matched_ids: list[str] | None = None,
    ):
        fallback_chunks = fallback_chunks or {}

        async def fake_direct(vector_db, document_ids, limit=20, user_id="", matter_ids=None):
            return fallback_chunks.get(document_ids[0], [])

        vector_db = MagicMock()
        vector_db.search = AsyncMock(return_value=search_results)

        return [
            patch(
                "app.services.strategy.make_openai",
                return_value=llm_client or _mock_llm_client(self.LLM_PAYLOAD),
            ),
            patch("app.services.strategy.get_vector_db", return_value=vector_db),
            patch(
                "app.services.strategy.embedding_service.embed_query",
                new=AsyncMock(return_value=[0.1] * 8),
            ),
            patch(
                "app.services.strategy.document_service.get_documents_by_filter",
                new=AsyncMock(return_value=matched_ids if matched_ids is not None else doc_ids),
            ),
            patch(
                "app.services.strategy.document_service.get_documents_by_ids",
                new=AsyncMock(side_effect=lambda ids, user_id, *_: [_doc(doc_id) for doc_id in ids]),
            ),
            patch("app.services.strategy.get_document_chunks_directly", new=fake_direct),
        ]

    async def _run(self, patches, **kwargs):
        for p in patches:
            p.start()
        try:
            return await strategy_service.generate_brief(
                question=kwargs.pop("question", "Can we enforce the indemnity clause?"),
                user_id="user-1",
                **kwargs,
            )
        finally:
            for p in patches:
                p.stop()

    @pytest.mark.asyncio
    async def test_every_document_contributes_a_passage(self):
        # Semantic search only covers doc "a"; doc "b" must be covered via fallback.
        patches = self._patches(
            doc_ids=["a", "b"],
            search_results=[_chunk("a", "alpha passage", 0.9)],
            fallback_chunks={"b": [_chunk("b", "beta first chunk", 1.0)]},
        )
        result = await self._run(patches)

        cited_docs = {c["document_id"] for c in result["citations"]}
        assert cited_docs == {"a", "b"}
        # Fallback chunk must not inherit the direct-fetch 1.0 similarity.
        beta = next(c for c in result["citations"] if c["document_id"] == "b")
        assert beta["confidence"] == 0

    @pytest.mark.asyncio
    async def test_every_selected_folder_scopes_the_brief(self):
        """Several selected folders are a union — not just the first one."""
        patches = self._patches(
            doc_ids=["a", "b"],
            search_results=[_chunk("a", "alpha passage", 0.9), _chunk("b", "beta passage", 0.5)],
        )
        by_filter = next(p.new for p in patches if p.attribute == "get_documents_by_filter")
        result = await self._run(patches, folder_paths=["/Cases/Smith/", "/Cases/Jones/"])

        assert by_filter.await_args.kwargs["folder_paths"] == ["/Cases/Smith/", "/Cases/Jones/"]
        assert result["scope"]["folder_paths"] == ["/Cases/Smith/", "/Cases/Jones/"]
        assert result["scope"]["folder_path"] == "/Cases/Smith/"

    @pytest.mark.asyncio
    async def test_response_shape_and_unknown_ref_dropping(self):
        patches = self._patches(
            doc_ids=["a", "b"],
            search_results=[_chunk("a", "alpha passage", 0.9), _chunk("b", "beta passage", 0.5)],
        )
        result = await self._run(patches)

        assert result["question"] == "Can we enforce the indemnity clause?"
        scope = result["scope"]
        assert scope["folder_path"] is None
        assert scope["documents_considered"] == 2
        assert scope["documents_total"] == 2
        # The payload says how much was actually read — passages, not whole files.
        assert scope["passages_read"] == 2
        assert scope["truncated"] is False
        assert "not read in full" in scope["coverage_note"]
        assert result["case_law_removed"] == []
        assert result["position"] == "The client is well positioned."
        assert result["strengths"][0]["citation_ids"] == ["d0"]
        # "d99" is an unknown ref and must be dropped.
        assert result["weaknesses"][0]["citation_ids"] == ["d1"]
        assert result["next_steps"][0]["citation_ids"] == ["d1"]
        assert {c["id"] for c in result["citations"]} == {"d0", "d1"}

    @pytest.mark.asyncio
    async def test_truncation_is_visible_in_scope(self):
        doc_ids = [f"doc{i}" for i in range(MAX_DOCUMENTS + 5)]
        patches = self._patches(
            doc_ids=doc_ids[:MAX_DOCUMENTS],
            search_results=[_chunk(doc_id) for doc_id in doc_ids[:MAX_DOCUMENTS]],
            matched_ids=doc_ids,
        )
        result = await self._run(patches)

        assert result["scope"]["documents_total"] == MAX_DOCUMENTS + 5
        assert result["scope"]["documents_considered"] == MAX_DOCUMENTS

    OPINION_TEXT = (
        "The indemnity clause is enforceable as written. "
        "Late notice does not defeat coverage absent prejudice."
    )

    CRAFT_PAYLOAD = {
        "targets": [
            {
                "proposition": "An indemnity clause is enforceable as written.",
                "query": "indemnity clause enforceability",
            },
            {
                "proposition": "Late notice does not defeat coverage absent prejudice.",
                "query": "late notice prejudice",
            },
        ]
    }

    @pytest.mark.asyncio
    async def test_case_law_attached_only_with_verified_quote(self):
        judge = {
            "supports": True,
            "quote": "Late notice does not defeat coverage absent prejudice",
            "how": "Holds that late notice alone does not defeat the claim.",
        }
        llm_client = _mock_llm_client_seq([self.LLM_PAYLOAD, self.CRAFT_PAYLOAD, judge])
        patches = self._patches(
            doc_ids=["a", "b"],
            search_results=[_chunk("a", "alpha", 0.9), _chunk("b", "beta", 0.5)],
            llm_client=llm_client,
        )
        search_opinions = AsyncMock(return_value=[_opinion(101), _opinion(102)])
        patches += [
            patch(
                "app.services.strategy.courtlistener_service.search_opinions",
                new=search_opinions,
            ),
            patch(
                "app.services.strategy.courtlistener_service.get_opinion",
                new=AsyncMock(return_value=SimpleNamespace(text=self.OPINION_TEXT)),
            ),
        ]
        result = await self._run(patches, include_case_law=True)

        assert result["strengths"][0]["case_refs"] == ["c0", "c1"]
        case_citations = [c for c in result["citations"] if c["type"] == "case_law"]
        assert len(case_citations) == 4  # 2 candidates verified x 2 points
        first = case_citations[0]
        assert first["id"] == "c0"
        assert first["source"] == "Case 101"
        assert first["reference"] == "101 F.3d 1"
        assert first["opinionId"] == "101"
        assert first["url"].startswith("https://www.courtlistener.com/")
        # The passage is the quote as located in the REAL opinion text.
        assert first["passage"] == "Late notice does not defeat coverage absent prejudice"
        assert first["explanation"] == "Holds that late notice alone does not defeat the claim."
        assert first["verified"] is True
        # Searches ran with the crafted doctrine queries, not raw point text.
        queried = {c.kwargs.get("query") for c in search_opinions.await_args_list}
        assert queried == {t["query"] for t in self.CRAFT_PAYLOAD["targets"]}
        # Candidates were judged against the crafted legal PROPOSITIONS,
        # not the raw strength/weakness text.
        judge_prompts = [
            c.kwargs["messages"][0]["content"]
            for c in llm_client.chat.completions.create.await_args_list
            if "PROPOSITION TO SUPPORT" in c.kwargs["messages"][0]["content"]
        ]
        assert judge_prompts
        assert any(self.CRAFT_PAYLOAD["targets"][0]["proposition"] in p for p in judge_prompts)
        # The brief reports what the case-law stage did.
        assert result["case_law"] == {
            "requested": True,
            "points_searched": 2,
            "opinions_read": 4,
            "partially_read": 0,
            "attached": 4,
            "unreadable": 0,
            "unsupportive": 0,
            "quote_unverified": 0,
            "errors": 0,
            "searches_failed": 0,
        }

    @pytest.mark.asyncio
    async def test_unverifiable_quote_drops_the_authority(self):
        judge = {
            "supports": True,
            "quote": "this sentence appears nowhere in the opinion",
            "how": "Sounds supportive.",
        }
        patches = self._patches(
            doc_ids=["a"],
            search_results=[_chunk("a", "alpha", 0.9)],
            llm_client=_mock_llm_client_seq([self.LLM_PAYLOAD, self.CRAFT_PAYLOAD, judge]),
        )
        patches += [
            patch(
                "app.services.strategy.courtlistener_service.search_opinions",
                new=AsyncMock(return_value=[_opinion(101)]),
            ),
            patch(
                "app.services.strategy.courtlistener_service.get_opinion",
                new=AsyncMock(return_value=SimpleNamespace(text=self.OPINION_TEXT)),
            ),
        ]
        result = await self._run(patches, include_case_law=True)

        assert all(c["type"] == "document" for c in result["citations"])
        assert "case_refs" not in result["strengths"][0]
        # The brief must SAY nothing survived — and say WHY.
        assert result["case_law"]["attached"] == 0
        assert result["case_law"]["opinions_read"] == 2
        assert result["case_law"]["quote_unverified"] == 2
        assert result["case_law"]["unsupportive"] == 0

    @pytest.mark.asyncio
    async def test_unsupportive_opinion_is_dropped(self):
        judge = {"supports": False, "quote": "", "how": ""}
        patches = self._patches(
            doc_ids=["a"],
            search_results=[_chunk("a", "alpha", 0.9)],
            llm_client=_mock_llm_client_seq([self.LLM_PAYLOAD, self.CRAFT_PAYLOAD, judge]),
        )
        patches += [
            patch(
                "app.services.strategy.courtlistener_service.search_opinions",
                new=AsyncMock(return_value=[_opinion(101)]),
            ),
            patch(
                "app.services.strategy.courtlistener_service.get_opinion",
                new=AsyncMock(return_value=SimpleNamespace(text=self.OPINION_TEXT)),
            ),
        ]
        result = await self._run(patches, include_case_law=True)

        assert all(c["type"] == "document" for c in result["citations"])
        assert "case_refs" not in result["strengths"][0]
        assert result["case_law"]["attached"] == 0
        assert result["case_law"]["unsupportive"] == 2

    @pytest.mark.asyncio
    async def test_unreadable_opinion_is_counted_not_read(self):
        """A candidate with no usable text is 'unreadable', never 'read in full'."""
        judge = {"supports": True, "quote": self.OPINION_TEXT[:50], "how": "x"}
        patches = self._patches(
            doc_ids=["a"],
            search_results=[_chunk("a", "alpha", 0.9)],
            llm_client=_mock_llm_client_seq([self.LLM_PAYLOAD, self.CRAFT_PAYLOAD, judge]),
        )
        patches += [
            patch(
                "app.services.strategy.courtlistener_service.search_opinions",
                new=AsyncMock(return_value=[_opinion(101)]),
            ),
            patch(
                "app.services.strategy.courtlistener_service.get_opinion",
                new=AsyncMock(return_value=SimpleNamespace(text="   ")),
            ),
        ]
        result = await self._run(patches, include_case_law=True)

        assert result["case_law"]["attached"] == 0
        assert result["case_law"]["opinions_read"] == 0
        assert result["case_law"]["unreadable"] == 2

    @pytest.mark.asyncio
    async def test_long_opinion_is_read_in_full_across_windows(self):
        """The holding lives past the first judge window; it must still be found."""
        from app.services.strategy import CASE_LAW_WINDOW_CHARS

        holding = "Late notice does not defeat coverage absent prejudice."
        filler = "The procedural history of this matter is lengthy. " * (
            CASE_LAW_WINDOW_CHARS // 40
        )
        long_opinion = filler + holding
        assert len(long_opinion) > CASE_LAW_WINDOW_CHARS

        synthesis = {
            "position": "The client is well positioned.",
            "strengths": [{"point": "Late notice is excusable", "refs": ["d0"]}],
            "weaknesses": [],
            "next_steps": [],
        }
        craft = {
            "targets": [
                {
                    "proposition": "Late notice does not defeat coverage absent prejudice.",
                    "query": "late notice prejudice",
                }
            ]
        }
        judge_miss = {"supports": False, "quote": "", "how": ""}
        judge_hit = {
            "supports": True,
            "quote": "Late notice does not defeat coverage absent prejudice.",
            "how": "States the controlling rule.",
        }
        patches = self._patches(
            doc_ids=["a"],
            search_results=[_chunk("a", "alpha", 0.9)],
            llm_client=_mock_llm_client_seq([synthesis, craft, judge_miss, judge_hit]),
        )
        patches += [
            patch(
                "app.services.strategy.courtlistener_service.search_opinions",
                new=AsyncMock(return_value=[_opinion(101)]),
            ),
            patch(
                "app.services.strategy.courtlistener_service.get_opinion",
                new=AsyncMock(return_value=SimpleNamespace(text=long_opinion)),
            ),
        ]
        result = await self._run(patches, include_case_law=True)

        assert result["case_law"]["attached"] == 1
        case_citations = [c for c in result["citations"] if c["type"] == "case_law"]
        assert case_citations[0]["passage"] == holding
        assert case_citations[0]["verified"] is True

    @pytest.mark.asyncio
    async def test_case_law_failure_is_silently_skipped(self):
        patches = self._patches(
            doc_ids=["a"],
            search_results=[_chunk("a", "alpha", 0.9)],
            llm_client=_mock_llm_client_seq([self.LLM_PAYLOAD, self.CRAFT_PAYLOAD]),
        )
        patches.append(
            patch(
                "app.services.strategy.courtlistener_service.search_opinions",
                new=AsyncMock(side_effect=ConnectionError("CourtListener down")),
            )
        )
        result = await self._run(patches, include_case_law=True)

        assert all(c["type"] == "document" for c in result["citations"])
        assert "case_refs" not in result["strengths"][0]
        assert result["case_law"] == {
            "requested": True,
            "points_searched": 2,
            "opinions_read": 0,
            "partially_read": 0,
            "attached": 0,
            "unreadable": 0,
            "unsupportive": 0,
            "quote_unverified": 0,
            "errors": 0,
            "searches_failed": 2,
            # The concrete failure is surfaced so the UI can say WHY instead
            # of a generic "service could not be reached".
            "search_error": "ConnectionError: CourtListener down",
        }

    @pytest.mark.asyncio
    async def test_llm_failure_raises_synthesis_error(self):
        broken_client = MagicMock()
        broken_client.chat.completions.create = AsyncMock(
            side_effect=ConnectionError("upstream down")
        )
        patches = self._patches(
            doc_ids=["a"],
            search_results=[_chunk("a")],
            llm_client=broken_client,
        )
        with pytest.raises(StrategySynthesisError):
            await self._run(patches)

    @pytest.mark.asyncio
    async def test_empty_scope_raises_lookup_error(self):
        patches = self._patches(doc_ids=[], search_results=[], matched_ids=[])
        with pytest.raises(LookupError):
            await self._run(patches)

    @pytest.mark.asyncio
    async def test_vector_db_unavailable_raises_service_unavailable(self):
        patches = self._patches(doc_ids=["a"], search_results=[])
        # Replace the vector-db patch with one returning None.
        patches[1] = patch("app.services.strategy.get_vector_db", return_value=None)
        with pytest.raises(StrategyServiceUnavailable):
            await self._run(patches)


class TestSynthesisReadsWholePassages:
    """The brief used to be synthesised from 400-character openings."""

    @pytest.mark.asyncio
    async def test_prompt_carries_the_full_passage_not_the_snippet(self):
        tail = "THE-CAP-IS-IN-THE-LAST-SENTENCE"
        long_passage = ("Indemnity recital. " * 60) + tail
        assert len(long_passage) > SNIPPET_MAX_CHARS
        llm_client = _mock_llm_client(TestGenerateBrief.LLM_PAYLOAD)
        helper = TestGenerateBrief()
        patches = helper._patches(
            doc_ids=["a", "b"],
            search_results=[_chunk("a", long_passage, 0.9), _chunk("b", "beta passage", 0.5)],
            llm_client=llm_client,
        )
        result = await helper._run(patches)

        prompt = llm_client.chat.completions.create.await_args_list[0].kwargs["messages"][0][
            "content"
        ]
        assert tail in prompt
        # Citations still show the short display snippet.
        alpha = next(c for c in result["citations"] if c["document_id"] == "a")
        assert len(alpha["passage"]) == SNIPPET_MAX_CHARS

    @pytest.mark.asyncio
    async def test_scope_reports_documents_left_out(self):
        from app.services.strategy import MAX_DOCUMENTS

        doc_ids = [f"d{i}" for i in range(MAX_DOCUMENTS + 3)]
        helper = TestGenerateBrief()
        patches = helper._patches(
            doc_ids=doc_ids,
            search_results=[_chunk(doc_id, f"passage {doc_id}", 0.5) for doc_id in doc_ids],
        )
        result = await helper._run(patches)

        assert result["scope"]["documents_considered"] == MAX_DOCUMENTS
        assert result["scope"]["truncated"] is True
        assert "3 more in scope were not read" in result["scope"]["coverage_note"]


class TestBriefCaseLawGuard:
    """Synthesis prose is model output: a case it names from its own weights goes."""

    @pytest.mark.asyncio
    async def test_invented_case_is_removed_and_reported(self):
        payload = {
            "position": "Strong under Hadley v. Baxendale, 9 Exch. 341.",
            "strengths": [{"point": "See Fakename v. Nowhere, 123 F.3d 456.", "refs": ["d0"]}],
            "weaknesses": [{"point": "Notice was late.", "refs": ["d1"]}],
            "next_steps": [{"step": "Gather the notices.", "refs": ["d1"]}],
        }
        helper = TestGenerateBrief()
        patches = helper._patches(
            doc_ids=["a", "b"],
            search_results=[_chunk("a", "alpha passage", 0.9), _chunk("b", "beta passage", 0.5)],
            llm_client=_mock_llm_client(payload),
        )
        result = await helper._run(patches)

        assert "Fakename" not in result["strengths"][0]["point"]
        assert "unverified case-law reference removed" in result["strengths"][0]["point"]
        assert "Hadley" not in result["position"]
        assert any("Fakename" in r for r in result["case_law_removed"])
        # Ordinary prose is untouched.
        assert result["weaknesses"][0]["point"] == "Notice was late."

    @pytest.mark.asyncio
    async def test_case_quoted_in_the_clients_own_documents_is_kept(self):
        payload = {
            "position": "The motion relies on Smith v. Jones.",
            "strengths": [{"point": "Their brief cites Smith v. Jones.", "refs": ["d0"]}],
            "weaknesses": [],
            "next_steps": [],
        }
        helper = TestGenerateBrief()
        patches = helper._patches(
            doc_ids=["a"],
            search_results=[_chunk("a", "As held in Smith v. Jones, notice is required.", 0.9)],
            llm_client=_mock_llm_client(payload),
        )
        result = await helper._run(patches)

        assert result["position"] == "The motion relies on Smith v. Jones."
        assert result["case_law_removed"] == []
