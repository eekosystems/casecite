"""Unit tests for the four advanced RAG enhancements."""

import asyncio

from app.models.schemas import Citation, CitationLogic, CitationStatus, ReasoningStep
from app.services.rag.enhancements import (
    annotate_source_locations,
    compress_context_results,
    expand_query,
    merge_search_results,
    verify_document_citations,
)


def _citation(source: str, passage: str, chunk_index: int = 0) -> Citation:
    return Citation(
        id="x",
        source=source,
        type="document",
        confidence=90,
        status=CitationStatus.PENDING,
        similarity=0.9,
        relevance_rank=1,
        chunk_index=chunk_index,
        token_count=10,
        passage=passage,
        reasoning=[ReasoningStep(type="t", description="d")],
        logic=CitationLogic(query_intent="q", matching_criteria="m", application="a"),
        was_cited_by_ai=True,
    )


# --- Context compression -------------------------------------------------- #
def test_compress_keeps_relevant_sentences_and_drops_others():
    text = (
        "The indemnification clause covers third party claims. "
        "The weather was nice that day. "
        "Indemnification is capped at fees paid. "
        "Lunch was served at noon. "
        "An unrelated sentence about parking. "
        "Another filler sentence here."
    )
    results = [{"text": text, "metadata": {}}]
    out = compress_context_results(results, "indemnification cap claims", max_sentences_per_chunk=2)
    assert out[0]["compressed"] is True
    assert "indemnification" in out[0]["text"].lower()
    assert "weather" not in out[0]["text"].lower()
    # original untouched
    assert "weather" in results[0]["text"].lower()


def test_compress_leaves_short_chunks_alone():
    results = [{"text": "Short one. Short two.", "metadata": {}}]
    out = compress_context_results(results, "anything", max_sentences_per_chunk=4)
    assert "compressed" not in out[0]


# --- Query expansion merge ------------------------------------------------ #
def test_merge_dedupes_by_chunk_and_keeps_best_similarity():
    a = [{"text": "x", "similarity": 0.6, "metadata": {"document_id": "d1", "chunk_index": 0}}]
    b = [
        {"text": "x", "similarity": 0.8, "metadata": {"document_id": "d1", "chunk_index": 0}},
        {"text": "y", "similarity": 0.5, "metadata": {"document_id": "d1", "chunk_index": 1}},
    ]
    merged = merge_search_results([a, b], top_k=10)
    assert len(merged) == 2  # same chunk deduped
    assert merged[0]["similarity"] == 0.8  # best kept, sorted first
    assert merged[0]["rank"] == 1 and merged[1]["rank"] == 2


def test_expand_query_falls_back_to_original_without_llm():
    # No clients configured -> call_llm raises -> fallback to [query]
    out = asyncio.run(expand_query("breach of contract", None, None, None, "gpt-5.5"))
    assert out == ["breach of contract"]


# --- Citation verification ------------------------------------------------ #
def test_verify_marks_supported_and_unsupported():
    supported = _citation("lease_agreement.pdf", "The tenant must pay rent monthly in advance.")
    unsupported = _citation("random.pdf", "Quarterly maintenance of elevators by vendor.")
    answer = "According to the lease agreement, the tenant must pay rent monthly."
    verify_document_citations([supported, unsupported], answer)

    assert supported.was_cited_by_ai is True
    assert unsupported.was_cited_by_ai is False
    # Verification flags what the answer used; it never approves a citation —
    # APPROVED is a reviewer's decision.
    assert supported.status == CitationStatus.PENDING
    assert unsupported.status == CitationStatus.PENDING


def test_verify_does_not_treat_a_few_shared_words_as_use():
    """Regression: three words in common used to mark a passage as relied on
    (and APPROVED). Two clauses of the same contract always share that much."""
    passage = (
        "The Supplier shall maintain commercial general liability insurance with "
        "limits of not less than two million dollars per occurrence and shall name "
        "the Customer as an additional insured under each policy."
    )
    unrelated = _citation("exhibit_b.pdf", passage)
    answer = (
        "The agreement lets the Customer terminate for convenience on thirty days "
        "written notice; the Supplier shall then invoice the Customer under each "
        "purchase order, less any disputed amounts."
    )
    verify_document_citations([unrelated], answer)

    assert unrelated.was_cited_by_ai is False
    assert unrelated.status == CitationStatus.PENDING


# --- Source tracking ------------------------------------------------------ #
def test_annotate_adds_location_with_page_and_chars():
    c = _citation("doc.pdf", "passage", chunk_index=2)
    results = [{"metadata": {"page": 5, "char_start": 100, "char_end": 250}}]
    annotate_source_locations([c], results)
    assert "chunk #3" in c.notes
    assert "page 5" in c.notes
    assert "chars 100-250" in c.notes


def test_annotate_falls_back_to_chunk_only():
    c = _citation("doc.pdf", "passage", chunk_index=0)
    annotate_source_locations([c], [{"metadata": {}}])
    assert "chunk #1" in c.notes


def test_merge_keeps_each_phrasings_order_over_raw_similarity():
    """Regression: the merge re-sorted by raw similarity, discarding the
    keyword-fused / reranked order every per-query list arrives in."""

    def r(chunk, sim):
        return {
            "text": chunk,
            "similarity": sim,
            "metadata": {"document_id": "d", "chunk_index": chunk},
        }

    # Both phrasings rank chunk 1 first (e.g. an exact clause-number match
    # that embeds poorly); chunk 9 embeds close to one paraphrase only.
    original = [r(1, 0.41), r(2, 0.55)]
    paraphrase = [r(1, 0.43), r(2, 0.52), r(9, 0.90)]
    merged = merge_search_results([original, paraphrase], top_k=10)

    assert [m["metadata"]["chunk_index"] for m in merged] == [1, 2, 9]
    assert merged[0]["similarity"] == 0.43  # best similarity kept for display
    assert merged[0]["merge_score"] > merged[2]["merge_score"]
