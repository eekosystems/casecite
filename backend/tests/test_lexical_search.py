"""
Full-corpus keyword search and its fusion with vector search.

The point of the keyword index is to find a passage the vector search did not
put in its candidate pool at all (a defined term, a clause number, a name).
These tests pin that behaviour, and the properties it must not trade away:
tenant isolation, no widening of a filter it cannot express, no stale entries
for deleted documents.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from unittest.mock import patch

import pytest
from app.services import resilience
from app.services.lexical_index import (
    LexicalIndex,
    UnsupportedFilter,
    build_match_query,
    compile_filter,
)
from app.services.rag import search as rag_search
from app.services.rag.search import reciprocal_rank_fusion, search_documents
from app.services.vectordb import ChromaDB

from tests.test_rag_retrieval import chunk, fake_embedding


@pytest.fixture(autouse=True)
def _isolated_breakers():
    resilience._circuit_breakers.clear()
    yield
    resilience._circuit_breakers.clear()


@pytest.fixture
def store(chroma_db):
    """The shared Chroma fixture: a real ChromaDB with its own keyword index."""
    return chroma_db


@pytest.fixture
def fake_embeddings():
    """Route query embedding through the deterministic topic embedding."""

    async def embed_query(query, user_keys=None):
        return fake_embedding(query)

    with patch.object(rag_search.embedding_service, "embed_query", embed_query):
        yield


def _entry(chunk_id, text, user_id="user-1", document_id="doc", **meta):
    return {
        "id": chunk_id,
        "text": text,
        "metadata": {"document_id": document_id, "user_id": user_id, **meta},
    }


@pytest.fixture
def lexical(tmp_path):
    index = LexicalIndex(str(tmp_path / "lexical.sqlite3"))
    yield index
    index.close()


class TestMatchQuery:
    def test_content_words_are_ored_and_stopwords_dropped(self):
        assert build_match_query("What is the late charge?") == '"late" OR "charge"'

    def test_clause_numbers_and_amounts_stay_phrases(self):
        query = build_match_query("What does Section 11.3(c) say about $250,000?")
        assert '"11 3 c"' in query
        assert '"250 000"' in query
        assert '"11"' not in query  # not a loose term that matches every "11"

    def test_quoted_phrases_are_kept(self):
        query = build_match_query("acceleration", phrases=["Liquidity Event"])
        assert query.startswith('"liquidity event"')

    def test_fts_syntax_in_user_text_is_inert(self):
        query = build_match_query('rent" OR NEAR(x) * -foo ^bar')
        # Every piece is a quoted token sequence; no raw operators survive.
        for part in query.split(" OR "):
            assert part.startswith('"') and part.endswith('"')

    def test_nothing_left_means_no_query(self):
        assert build_match_query("what is the") is None


class TestFilterCompilation:
    def test_tenant_scope_shapes(self):
        sql, params = compile_filter(
            {"$or": [{"user_id": "u1"}, {"matter_id": {"$in": ["m1", "m2"]}}]}
        )
        assert params == ["u1", "m1", "m2"]
        assert "c.user_id = ?" in sql and "c.matter_id IN (?,?)" in sql

    def test_metadata_keys_read_from_json(self):
        sql, params = compile_filter({"$and": [{"user_id": "u1"}, {"doc_type": "contract"}]})
        assert "json_extract(c.metadata, '$.doc_type') = ?" in sql
        assert params == ["u1", "contract"]

    @pytest.mark.parametrize(
        "bad",
        [{"user_id": "u1", "date": "2024"}, {"user_id": {"$ne": "u2"}}, {"$and": []}],
    )
    def test_unsupported_filters_are_refused_not_dropped(self, bad):
        with pytest.raises(UnsupportedFilter):
            compile_filter(bad)


class TestLexicalIndex:
    def test_search_requires_a_tenant_scope(self, lexical):
        with pytest.raises(ValueError):
            lexical.search("rent", None)

    def test_finds_by_stem_and_ranks_by_bm25(self, lexical):
        lexical.add(
            [
                _entry("a", "The Tenant may terminate on notice."),
                _entry("b", "Termination of the agreement requires termination notice."),
                _entry("c", "Rent is due monthly."),
            ]
        )
        hits = lexical.search("terminating", {"user_id": "user-1"})
        assert [h["id"] for h in hits] == ["b", "a"]
        assert hits[0]["lexical_rank"] == 1 and hits[0]["bm25"] > hits[1]["bm25"]
        assert "similarity" not in hits[0]  # a BM25 score is not a cosine

    def test_never_returns_another_tenants_chunk(self, lexical):
        lexical.add([_entry("mine", "Zylphor clause", user_id="user-1")])
        lexical.add([_entry("theirs", "Zylphor clause", user_id="user-2")])
        hits = lexical.search("Zylphor", {"user_id": "user-1"})
        assert [h["id"] for h in hits] == ["mine"]

    def test_matter_scope_and_retagging(self, lexical):
        lexical.add([_entry("shared", "Zylphor clause", user_id="user-2", document_id="d2")])
        scope = {"$or": [{"user_id": "user-1"}, {"matter_id": {"$in": ["m1"]}}]}
        assert lexical.search("Zylphor", scope) == []
        lexical.update_matter("d2", "m1")
        assert [h["id"] for h in lexical.search("Zylphor", scope)] == ["shared"]
        lexical.update_matter("d2", None)
        assert lexical.search("Zylphor", scope) == []

    def test_re_adding_a_chunk_leaves_no_stale_terms(self, lexical):
        lexical.add([_entry("a", "old wording about Zylphor")])
        lexical.add([_entry("a", "new wording about Quentaro")])
        scope = {"user_id": "user-1"}
        assert lexical.search("Zylphor", scope) == []
        assert [h["id"] for h in lexical.search("Quentaro", scope)] == ["a"]
        assert lexical.count() == 1

    def test_delete_document_and_clear(self, lexical):
        lexical.add(
            [_entry("a", "Zylphor", document_id="d1"), _entry("b", "Zylphor", document_id="d2")]
        )
        lexical.delete_document("d1")
        assert [h["id"] for h in lexical.search("Zylphor", {"user_id": "user-1"})] == ["b"]
        lexical.clear()
        assert lexical.count() == 0

    def test_heading_matches_the_file_name(self, lexical):
        lexical.add([_entry("a", "Late charge of five percent.", filename="calloway_lease.pdf")])
        assert [h["id"] for h in lexical.search("Calloway", {"user_id": "user-1"})] == ["a"]


class TestStoreMirroring:
    @pytest.fixture
    def chroma(self, tmp_path):
        store = ChromaDB(path=str(tmp_path / "chroma"), lexical_path=str(tmp_path / "lx.sqlite3"))
        yield store
        store.lexical.close()

    @pytest.mark.asyncio
    async def test_writes_reach_the_keyword_index(self, chroma):
        await chroma.add_documents([chunk("lease", 0, "Zylphor late fee", matter_id="m1")])
        scope = {"user_id": "user-1"}
        hits = await chroma.keyword_search("Zylphor", 5, scope)
        assert [h["id"] for h in hits] == ["lease_0"]
        assert hits[0]["metadata"]["matter_id"] == "m1"

        assert await chroma.update_document_matter("lease", "m2")
        colleague = {"$or": [{"user_id": "user-9"}, {"matter_id": {"$in": ["m2"]}}]}
        assert [h["id"] for h in await chroma.keyword_search("Zylphor", 5, colleague)] == [
            "lease_0"
        ]

        assert await chroma.delete_by_document("lease")
        assert await chroma.keyword_search("Zylphor", 5, scope) == []

    @pytest.mark.asyncio
    async def test_unsupported_filter_means_unavailable_not_unfiltered(self, chroma):
        await chroma.add_documents([chunk("lease", 0, "Zylphor")])
        assert await chroma.keyword_search("Zylphor", 5, {"user_id": "user-1", "date": "x"}) is None

    @pytest.mark.asyncio
    async def test_backfill_copies_stored_chunks_without_embedding(self, chroma):
        lexical, chroma.lexical = chroma.lexical, None  # indexed before the keyword index
        await chroma.add_documents([chunk("lease", 0, "Zylphor"), chunk("lease", 1, "Quentaro")])
        chroma.lexical = lexical
        assert lexical.count() == 0

        assert await chroma.sync_lexical_document("lease", "user-1") is True
        assert lexical.count() == 2
        assert await chroma.sync_lexical_document("lease", "user-1") is False  # already there

    @pytest.mark.asyncio
    async def test_stored_embeddings_are_readable(self, chroma):
        await chroma.add_documents([chunk("lease", 0, "rent is late")])
        vectors = await chroma.get_embeddings(["lease_0", "missing"])
        assert set(vectors) == {"lease_0"}
        assert vectors["lease_0"] == pytest.approx(fake_embedding("rent is late"), rel=1e-5)


class TestFusion:
    def test_rrf_merges_and_keeps_both_scores(self):
        vector = [{"id": "a", "similarity": 0.9}, {"id": "b", "similarity": 0.8}]
        keyword = [
            {"id": "b", "bm25": 7.0, "lexical_rank": 1},
            {"id": "c", "bm25": 3.0, "lexical_rank": 2},
        ]
        fused = reciprocal_rank_fusion([vector, keyword], k=60)
        assert [r["id"] for r in fused] == ["b", "a", "c"]
        assert fused[0]["similarity"] == 0.8 and fused[0]["bm25"] == 7.0
        assert fused[0]["retrieved_by"] == ["vector", "keyword"]
        assert fused[2]["retrieved_by"] == ["keyword"]


async def _load_rent_heavy(store, n=6):
    """Several passages that out-rank the target on the vector axis."""
    await store.add_documents(
        [
            chunk(
                f"rent{i}",
                0,
                "Rent is paid late; a late payment fee applies when rent is paid late.",
            )
            for i in range(n)
        ]
    )
    # The target shares no topic word with the query's vector, only a rare term.
    await store.add_documents(
        [chunk("msa", 0, "Upon a Zylphor Event all fees become immediately due and payable.")]
    )


class TestHybridRetrieval:
    @pytest.mark.asyncio
    async def test_keyword_search_finds_what_the_vector_pool_missed(self, store, fake_embeddings):
        await _load_rent_heavy(store)
        small_pool = rag_search.settings.model_copy(update={"hybrid_candidate_pool": 3})
        with patch.object(rag_search, "settings", small_pool):
            vector_only = await search_documents(
                store,
                "late rent payment Zylphor",
                top_k=3,
                user_id="user-1",
                similarity_threshold=0.001,
                use_lexical=False,
            )
            hybrid = await search_documents(
                store,
                "late rent payment Zylphor",
                top_k=3,
                user_id="user-1",
                similarity_threshold=0.001,
            )

        assert "msa_0" not in {r["id"] for r in vector_only}
        target = next(r for r in hybrid if r["id"] == "msa_0")
        assert "keyword" in target["retrieved_by"]
        # Its similarity is the real cosine from its stored vector.
        assert 0.0 < target["similarity"] < 1.0

    @pytest.mark.asyncio
    async def test_keyword_hits_respect_the_tenant_scope(self, store, fake_embeddings):
        await _load_rent_heavy(store, n=2)
        await store.add_documents([chunk("theirs", 0, "Zylphor Zylphor Zylphor", user_id="user-2")])
        results = await search_documents(
            store, "Zylphor", top_k=10, user_id="user-1", similarity_threshold=0.001
        )
        assert "theirs_0" not in {r["id"] for r in results}

    @pytest.mark.asyncio
    async def test_stale_keyword_entries_are_dropped(self, store, fake_embeddings):
        await _load_rent_heavy(store, n=1)
        # A keyword entry whose vector is gone (e.g. a failed mirror delete).
        store.lexical.add([_entry("ghost_0", "Zylphor", document_id="ghost", user_id="user-1")])
        results = await search_documents(
            store, "Zylphor", top_k=10, user_id="user-1", similarity_threshold=0.001
        )
        assert "ghost_0" not in {r["id"] for r in results}

    @pytest.mark.asyncio
    async def test_scoped_search_stays_in_scope_and_spreads(self, store, fake_embeddings):
        await _load_rent_heavy(store, n=4)
        results = await search_documents(
            store,
            "late rent Zylphor",
            top_k=4,
            user_id="user-1",
            document_ids=["msa", "rent0"],
            similarity_threshold=0.001,
        )
        assert {r["metadata"]["document_id"] for r in results} == {"msa", "rent0"}
