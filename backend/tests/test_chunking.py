"""
Structure-aware chunking and page provenance.

A chunk must be a verbatim slice of the document (quotes verify against it,
and contract analysis reassembles documents from chunks), must not cut a
clause in half when it fits, and must say where it sits: section path and
page. Its embedding carries the document's title and parties so a clause is
findable by the agreement it belongs to.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app.services.chunking import chunk_document, document_context, embedding_text
from app.services.contract_analysis.service import merge_overlapping_chunks
from app.services.text_extraction import TextExtractionService

CORPUS = Path(__file__).resolve().parents[1] / "evals" / "corpus"


def words(text: str) -> int:
    """A deterministic stand-in tokenizer: one token per word."""
    return len(text.split())


def chunks_of(text: str, size: int = 120, overlap: int = 30, **kw) -> list[dict]:
    return chunk_document(text, chunk_size=size, chunk_overlap=overlap, count_tokens=words, **kw)


AGREEMENT = """SERVICES AGREEMENT

This Services Agreement is made between Acme Corp. ("Provider") and Beta LLC ("Client").

ARTICLE 1
DEFINITIONS

1.1 "Change Event" means a sale of all or substantially all of Client's assets.

1.2 "Fees" means the fees in each order.

ARTICLE 2
TERMINATION

2.1 Termination for Cause. Either party may terminate on thirty days' notice if the other party fails to cure a material breach.

2.2 Effect. Fees accrued before termination remain payable.

SCHEDULE A
PRICING

| Item | Price |
| Base | $100 |
"""


class TestStructure:
    def test_chunks_are_verbatim_slices_with_offsets(self):
        for c in chunks_of(AGREEMENT):
            assert AGREEMENT[c["char_start"] : c["char_end"]] == c["text"]

    def test_section_paths_name_the_article_and_clauses(self):
        sections = [c["section"] for c in chunks_of(AGREEMENT, size=40)]
        assert any(
            s.startswith("ARTICLE 1 DEFINITIONS") and "“Change Event”" in s for s in sections
        )
        assert any(
            "ARTICLE 2 TERMINATION" in s and "2.1 Termination for Cause" in s for s in sections
        )
        assert any("SCHEDULE A PRICING" in s for s in sections)

    def test_a_clause_that_fits_is_never_split(self):
        clause = "2.1 Termination for Cause. Either party may terminate on thirty days' notice"
        assert any(clause in c["text"] for c in chunks_of(AGREEMENT, size=40))

    def test_oversized_clauses_split_at_sentences_with_overlap_and_progress(self):
        sentences = " ".join(f"Sentence number {i} states an obligation." for i in range(60))
        text = f"ARTICLE 1 GENERAL\n\n1.1 Long Clause. {sentences}\n"
        chunks = chunks_of(text, size=50, overlap=12)
        assert len(chunks) > 3
        for a, b in zip(chunks, chunks[1:], strict=False):
            assert b["char_start"] > a["char_start"]  # always advances
            assert b["char_start"] < a["char_end"]  # with overlap
            assert words(a["text"]) <= 50
        assert merge_overlapping_chunks([c["text"] for c in chunks]).strip() == text.strip()

    def test_documents_reassemble_from_their_chunks(self):
        for path in sorted(CORPUS.glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            chunks = chunks_of(text, size=200, overlap=40)
            rebuilt = merge_overlapping_chunks([c["text"] for c in chunks])
            assert rebuilt.split() == text.split(), path.name

    def test_signature_fragments_fold_into_a_neighbour(self):
        text = (CORPUS / "northwind_brightline_msa.txt").read_text(encoding="utf-8")
        chunks = chunks_of(text, size=400, overlap=60)
        assert all(words(c["text"]) >= 400 // 8 for c in chunks)
        assert "Daniel Okafor" in chunks[-1]["text"]

    def test_unstructured_text_still_chunks(self):
        text = " ".join(["plain words without any headings"] * 200)
        chunks = chunks_of(text, size=100, overlap=20)
        assert chunks and all(words(c["text"]) <= 100 for c in chunks)

    def test_wrapped_lines_starting_with_numbers_are_not_clauses(self):
        text = (
            "ARTICLE 1 PAYMENT\n\n1.1 Payment. Customer shall pay each invoice within\n"
            "30 days after receipt, and late amounts bear interest.\n"
        )
        chunks = chunks_of(text, size=200)
        assert len(chunks) == 1
        assert "30 " not in chunks[0]["section"]

    def test_empty_text(self):
        assert chunks_of("   \n\n ") == []


class TestPages:
    def test_chunks_name_their_pages(self):
        page_one = "ARTICLE 1 TERM\n\n1.1 Term. The term is five years."
        page_two = "ARTICLE 2 RENT\n\n2.1 Rent. Rent is due monthly."
        text = page_one + "\n\n" + page_two
        starts = [(0, 1), (len(page_one) + 2, 2)]
        chunks = chunks_of(text, size=10, page_starts=starts)
        rent = next(c for c in chunks if "Rent is due" in c["text"])
        term = next(c for c in chunks if "five years" in c["text"])
        assert (term["page_start"], term["page_end"]) == (1, 1)
        assert (rent["page_start"], rent["page_end"]) == (2, 2)

    def test_without_page_information_pages_are_unknown(self):
        assert all(c["page_start"] is None for c in chunks_of(AGREEMENT))

    def test_pdf_extraction_reports_page_starts_skipping_blank_pages(self):
        fitz = pytest.importorskip("fitz")
        doc = fitz.open()
        for body in ("First page clause text.", None, "Third page clause text."):
            page = doc.new_page()
            if body:
                page.insert_text((72, 72), body)
        content = doc.tobytes()
        result = TextExtractionService()._extract_pdf(content)
        assert [p for _, p in result.page_starts] == [1, 3]
        offset, _ = result.page_starts[1]
        assert result.text[offset:].startswith("Third page")


class TestOcrTrigger:
    def test_sparse_pages_are_ocred(self):
        assert TextExtractionService._needs_ocr("p. 3")

    def test_a_scan_with_a_text_header_is_ocred(self):
        header = "CONFIDENTIAL — PRODUCED IN LITIGATION — BATES ACME-000123 — PAGE 3 OF 40"
        assert TextExtractionService._needs_ocr(header, image_coverage=0.9)

    def test_a_text_page_with_a_logo_is_not(self):
        body = "The parties agree as follows. " * 40
        assert not TextExtractionService._needs_ocr(body, image_coverage=0.9)
        heading_page = (
            "ARTICLE 7 — INDEMNIFICATION AND LIMITATION OF LIABILITY (continued on the next page)"
        )
        assert not TextExtractionService._needs_ocr(heading_page, image_coverage=0.1)


class TestContext:
    def test_title_and_parties_from_the_preamble(self):
        context = document_context(AGREEMENT, "acme.txt")
        assert context.startswith("SERVICES AGREEMENT — ")
        assert "Acme Corp." in context and "Beta LLC" in context

    def test_falls_back_to_the_file_name(self):
        assert document_context("no structure here", "calloway_lease.pdf") == "calloway lease"

    def test_embedding_text_adds_context_but_keeps_the_text(self):
        chunk = {
            "text": "Rent is late after ten days.",
            "section": "SECTION 2. RENT > 2.2 Late Charge",
        }
        embedded = embedding_text(chunk, "lease.pdf", "LEASE — between Landlord and Calloway")
        assert embedded.endswith(chunk["text"])
        assert "Calloway" in embedded and "2.2 Late Charge" in embedded


class TestIndexing:
    @pytest.mark.asyncio
    async def test_stored_text_is_verbatim_and_location_is_recorded(self, chroma_db):
        from app.services.rag.service import RAGService

        embedded: list[str] = []

        async def embed_texts(texts, user_keys=None):
            embedded.extend(texts)
            return [[1.0, float(i)] for i, _ in enumerate(texts)]

        service = RAGService(vector_db=chroma_db)
        with patch(
            "app.services.rag.service.embedding_service.embed_texts",
            AsyncMock(side_effect=embed_texts),
        ):
            count = await service.index_document(
                document_id="agr",
                text=AGREEMENT,
                filename="acme.txt",
                user_id="user-1",
                page_starts=[(0, 1)],
            )

        stored = chroma_db.collection.get(include=["documents", "metadatas"])
        assert count == len(stored["ids"])
        for text, meta in zip(stored["documents"], stored["metadatas"], strict=True):
            assert AGREEMENT[meta["char_start"] : meta["char_end"]] == text
            assert meta["page_start"] == 1 and meta["chunker"] == "structured-v1"
            assert "Acme Corp." in meta["doc_context"]
        # What was embedded carries the context; what was stored does not.
        assert all("Acme Corp." in e for e in embedded)
        hits = await chroma_db.keyword_search("Beta", 10, {"user_id": "user-1"})
        assert len(hits) == count  # the parties are searchable from every clause
