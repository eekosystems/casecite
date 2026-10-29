"""
Tests for registry/vector-store reconciliation and honest search failures.

The incident these guard against: a Chroma schema upgrade (or wiped volume)
empties the vector store while the document registry keeps saying INDEXED —
users see "7 chunks indexed" on a document no search can find, and the chat
reports the infrastructure failure as "no relevant documents".
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.schemas import ConnectorType, Document, DocumentStatus
from app.services.documents import DocumentService, _humanize_indexing_error
from app.services.rag.search import SearchUnavailableError, search_documents


def _doc(doc_id: str, user_id: str = "u1", status=DocumentStatus.INDEXED) -> Document:
    return Document(
        id=doc_id,
        user_id=user_id,
        filename=f"{doc_id}.txt",
        content_type="text/plain",
        size=10,
        source=ConnectorType.LOCAL,
        status=status,
        created_at=datetime.now(UTC),
        chunk_count=7,
        metadata={},
    )


def _service_with_docs(docs: list[Document]) -> DocumentService:
    service = DocumentService.__new__(DocumentService)
    service.documents = {d.id: d for d in docs}
    service.folders = {}
    service._save_index = MagicMock()
    return service


class TestReconcileIndex:
    @pytest.mark.asyncio
    async def test_documents_present_in_store_are_untouched(self):
        service = _service_with_docs([_doc("a")])
        rag = MagicMock()
        rag.vector_db.has_document = AsyncMock(return_value=True)

        with patch("app.services.documents.rag_service", rag):
            stats = await service.reconcile_index()

        assert stats == {"checked": 1, "reindexed": 0, "failed": 0, "keyword_backfilled": 0}
        assert service.documents["a"].status == DocumentStatus.INDEXED
        service._save_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_document_is_reindexed_from_stored_file(self, tmp_path):
        doc = _doc("a")
        service = _service_with_docs([doc])
        service.extract_text = AsyncMock(return_value="recovered text")

        rag = MagicMock()
        rag.vector_db.has_document = AsyncMock(return_value=False)
        rag.index_document = AsyncMock(return_value=5)

        stored = tmp_path / "u1" / "a.txt"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"encrypted-bytes")

        with (
            patch("app.services.documents.rag_service", rag),
            patch("app.services.documents.settings") as mock_settings,
            patch(
                "app.services.user_keys.UserAPIKeys.for_user",
                return_value=MagicMock(),
            ),
        ):
            mock_settings.upload_dir = str(tmp_path)
            stats = await service.reconcile_index()

        assert stats == {"checked": 1, "reindexed": 1, "failed": 0, "keyword_backfilled": 0}
        assert doc.status == DocumentStatus.INDEXED
        assert doc.chunk_count == 5
        rag.index_document.assert_awaited_once()
        service._save_index.assert_called_once()

    @pytest.mark.asyncio
    async def test_unrepairable_document_is_marked_failed_with_reason(self, tmp_path):
        doc = _doc("a")
        service = _service_with_docs([doc])

        rag = MagicMock()
        rag.vector_db.has_document = AsyncMock(return_value=False)

        # No stored file exists → repair must fail loudly, not lie.
        with (
            patch("app.services.documents.rag_service", rag),
            patch("app.services.documents.settings") as mock_settings,
        ):
            mock_settings.upload_dir = str(tmp_path)
            stats = await service.reconcile_index()

        assert stats == {"checked": 1, "reindexed": 0, "failed": 1, "keyword_backfilled": 0}
        assert doc.status == DocumentStatus.FAILED
        assert "re-upload" in doc.metadata["error"].lower()

    @pytest.mark.asyncio
    async def test_non_indexed_documents_are_skipped(self):
        service = _service_with_docs([_doc("a", status=DocumentStatus.FAILED)])
        rag = MagicMock()
        rag.vector_db.has_document = AsyncMock(return_value=False)

        with patch("app.services.documents.rag_service", rag):
            stats = await service.reconcile_index()

        assert stats == {"checked": 0, "reindexed": 0, "failed": 0, "keyword_backfilled": 0}
        rag.vector_db.has_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_vector_db_skips_quietly(self):
        service = _service_with_docs([_doc("a")])
        with patch("app.services.documents.rag_service", None):
            stats = await service.reconcile_index()
        assert stats == {"checked": 0, "reindexed": 0, "failed": 0, "keyword_backfilled": 0}


class TestSearchUnavailable:
    @pytest.mark.asyncio
    async def test_embedding_failure_raises_instead_of_empty(self):
        vector_db = MagicMock()
        with (
            patch(
                "app.services.rag.search.embedding_service.embed_query",
                new=AsyncMock(side_effect=ValueError("Error code: 401 - invalid_api_key")),
            ),
            pytest.raises(SearchUnavailableError) as exc,
        ):
            await search_documents(vector_db, "any query", user_id="u1")
        assert "api key" in str(exc.value).lower()
        vector_db.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_embedding_failure_has_actionable_message(self):
        vector_db = MagicMock()
        with (
            patch(
                "app.services.rag.search.embedding_service.embed_query",
                new=AsyncMock(side_effect=ConnectionError("connection reset")),
            ),
            pytest.raises(SearchUnavailableError) as exc,
        ):
            await search_documents(vector_db, "any query", user_id="u1")
        assert "embedding" in str(exc.value).lower()


class TestHumanizeIndexingError:
    def test_invalid_api_key_payload_becomes_actionable(self):
        raw = Exception("Error code: 401 - {'error': {'code': 'invalid_api_key'}}")
        msg = _humanize_indexing_error(raw)
        assert "invalid_api_key" not in msg
        assert "Settings" in msg

    def test_long_raw_errors_are_truncated(self):
        msg = _humanize_indexing_error(Exception("x" * 500))
        assert len(msg) <= 210

    def test_plain_errors_pass_through(self):
        assert _humanize_indexing_error(Exception("no text could be extracted")) == (
            "no text could be extracted"
        )
