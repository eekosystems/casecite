"""
Unit tests for the Document service.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_document(doc_id="doc-1", user_id="user-1", filename="test.pdf", status="indexed"):
    """Create a mock Document object."""
    from app.models.schemas import ConnectorType, DocumentStatus

    mock_doc = MagicMock()
    mock_doc.id = doc_id
    mock_doc.user_id = user_id
    mock_doc.filename = filename
    mock_doc.content_type = "application/pdf"
    mock_doc.size = 1024
    mock_doc.source = ConnectorType.LOCAL
    mock_doc.source_id = None
    mock_doc.status = DocumentStatus(status)
    mock_doc.chunk_count = 5
    mock_doc.folder_path = None
    mock_doc.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    mock_doc.indexed_at = datetime(2024, 1, 1, tzinfo=UTC)
    mock_doc.metadata = {}
    return mock_doc


class TestDocumentService:
    """Tests for DocumentService."""

    def _make_service(self):
        """Create a DocumentService with mocked dependencies."""
        with (
            patch("app.services.documents.settings") as mock_settings,
            patch("app.services.documents.text_extraction_service"),
            patch("app.services.documents.rag_service"),
            patch("os.path.exists", return_value=False),
        ):
            mock_settings.upload_dir = "/tmp/test_uploads"
            from app.services.documents import DocumentService

            svc = DocumentService()
        return svc

    # ------------------------------------------------------------------ #
    # _classify_document
    # ------------------------------------------------------------------ #

    def test_classify_document_contract(self):
        """Should classify contract-related filenames."""
        svc = self._make_service()
        assert svc._classify_document("service_contract.pdf", "text") == "contract"
        assert svc._classify_document("software_agreement.docx", "text") == "contract"
        assert svc._classify_document("terms_of_service.pdf", "text") == "contract"

    def test_classify_document_legal_filing(self):
        """Should classify legal filing filenames."""
        svc = self._make_service()
        assert svc._classify_document("motion_to_dismiss.pdf", "text") == "legal_filing"
        assert svc._classify_document("brief_in_support.pdf", "text") == "legal_filing"
        assert svc._classify_document("petition_for_review.pdf", "text") == "legal_filing"

    def test_classify_document_memo(self):
        """Should classify memo filenames."""
        svc = self._make_service()
        assert svc._classify_document("legal_memo.docx", "text") == "memo"
        assert svc._classify_document("memorandum_of_law.pdf", "text") == "memo"

    def test_classify_document_default(self):
        """Should default to 'document' for unrecognized filenames."""
        svc = self._make_service()
        assert svc._classify_document("random_file.pdf", "text") == "document"

    # ------------------------------------------------------------------ #
    # _get_user_documents
    # ------------------------------------------------------------------ #

    def test_get_user_documents_filters_by_user(self):
        """Should return only documents belonging to the given user."""
        svc = self._make_service()

        doc1 = _make_mock_document(doc_id="d1", user_id="user-1")
        doc2 = _make_mock_document(doc_id="d2", user_id="user-2")
        doc3 = _make_mock_document(doc_id="d3", user_id="user-1")

        svc.documents = {"d1": doc1, "d2": doc2, "d3": doc3}

        result = svc._get_user_documents("user-1")
        assert len(result) == 2
        assert "d1" in result
        assert "d3" in result
        assert "d2" not in result

    # ------------------------------------------------------------------ #
    # get_document
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_get_document_found(self):
        """Should return document when it exists for the user."""
        svc = self._make_service()
        doc = _make_mock_document(doc_id="d1", user_id="user-1")
        svc.documents = {"d1": doc}

        result = await svc.get_document("d1", "user-1")
        assert result is not None
        assert result.id == "d1"

    @pytest.mark.asyncio
    async def test_get_document_wrong_user(self):
        """Should return None when document belongs to different user."""
        svc = self._make_service()
        doc = _make_mock_document(doc_id="d1", user_id="user-1")
        svc.documents = {"d1": doc}

        result = await svc.get_document("d1", "user-wrong")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_document_not_exists(self):
        """Should return None when document ID does not exist."""
        svc = self._make_service()
        svc.documents = {}

        result = await svc.get_document("nonexistent", "user-1")
        assert result is None

    # ------------------------------------------------------------------ #
    # list_documents
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_list_documents_filters_by_user(self):
        """Should only return documents for the given user."""
        svc = self._make_service()

        doc1 = _make_mock_document(doc_id="d1", user_id="user-1")
        doc2 = _make_mock_document(doc_id="d2", user_id="user-2")

        svc.documents = {"d1": doc1, "d2": doc2}

        result = await svc.list_documents("user-1")
        assert len(result) == 1
        assert result[0].id == "d1"

    @pytest.mark.asyncio
    async def test_list_documents_with_limit_and_offset(self):
        """Should respect limit and offset parameters."""
        svc = self._make_service()

        docs = {}
        for i in range(5):
            doc = _make_mock_document(doc_id=f"d{i}", user_id="user-1")
            doc.created_at = datetime(2024, 1, i + 1, tzinfo=UTC)
            docs[f"d{i}"] = doc

        svc.documents = docs

        result = await svc.list_documents("user-1", limit=2, offset=1)
        assert len(result) == 2

    # ------------------------------------------------------------------ #
    # delete_document
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_delete_document_success(self):
        """Should delete document from index and vector DB."""
        svc = self._make_service()

        doc = _make_mock_document(doc_id="d1", user_id="user-1", filename="test.pdf")
        svc.documents = {"d1": doc}

        mock_rag = AsyncMock()
        mock_rag.delete_document = AsyncMock(return_value=True)
        derived = AsyncMock()

        with (
            patch("app.services.documents.rag_service", mock_rag),
            patch("os.path.exists", return_value=True),
            patch("os.remove"),
            patch.object(svc, "_save_index"),
            patch.object(svc, "_delete_derived_data", derived),
            patch.object(svc, "_get_user_upload_dir", return_value="/tmp/uploads/user-1"),
        ):
            result = await svc.delete_document("d1", "user-1")

        assert result is True
        assert "d1" not in svc.documents
        mock_rag.delete_document.assert_called_once_with("d1")
        # Analyses derived from the document go with it.
        derived.assert_awaited_once_with(["d1"])

    @pytest.mark.asyncio
    async def test_delete_document_keeps_document_when_vectors_remain(self):
        """A failed vector delete must not leave orphaned, searchable chunks."""
        from app.services.documents import DocumentDeletionError

        svc = self._make_service()
        svc.documents = {"d1": _make_mock_document(doc_id="d1", user_id="user-1")}

        mock_rag = AsyncMock()
        mock_rag.delete_document = AsyncMock(return_value=False)
        derived = AsyncMock()

        with (
            patch("app.services.documents.rag_service", mock_rag),
            patch("os.remove") as remove,
            patch.object(svc, "_save_index"),
            patch.object(svc, "_delete_derived_data", derived),
            pytest.raises(DocumentDeletionError),
        ):
            await svc.delete_document("d1", "user-1")

        assert "d1" in svc.documents
        derived.assert_not_awaited()
        remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_document_keeps_document_when_vector_store_raises(self):
        from app.services.documents import DocumentDeletionError

        svc = self._make_service()
        svc.documents = {"d1": _make_mock_document(doc_id="d1", user_id="user-1")}

        mock_rag = AsyncMock()
        mock_rag.delete_document = AsyncMock(side_effect=Exception("chroma is down"))

        with (
            patch("app.services.documents.rag_service", mock_rag),
            patch.object(svc, "_save_index"),
            patch.object(svc, "_delete_derived_data", AsyncMock()),
            pytest.raises(DocumentDeletionError),
        ):
            await svc.delete_document("d1", "user-1")

        assert "d1" in svc.documents

    @pytest.mark.asyncio
    async def test_delete_document_wrong_user(self):
        """Should return False when document belongs to different user."""
        svc = self._make_service()

        doc = _make_mock_document(doc_id="d1", user_id="user-1")
        svc.documents = {"d1": doc}

        result = await svc.delete_document("d1", "user-wrong")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_document_not_exists(self):
        """Should return False when document does not exist."""
        svc = self._make_service()
        svc.documents = {}

        result = await svc.delete_document("nonexistent", "user-1")
        assert result is False

    # ------------------------------------------------------------------ #
    # get_stats
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_get_stats(self):
        """Should return correct stats for user's documents."""

        svc = self._make_service()

        doc1 = _make_mock_document(doc_id="d1", user_id="user-1", status="indexed")
        doc1.chunk_count = 10

        doc2 = _make_mock_document(doc_id="d2", user_id="user-1", status="failed")
        doc2.chunk_count = 0

        svc.documents = {"d1": doc1, "d2": doc2}

        result = await svc.get_stats("user-1")

        assert result["total_documents"] == 2
        assert result["total_chunks"] == 10
        assert result["by_status"]["indexed"] == 1
        assert result["by_status"]["failed"] == 1

    # ------------------------------------------------------------------ #
    # get_documents_by_ids
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_get_documents_by_ids(self):
        """Should return only matching documents for the user."""
        svc = self._make_service()

        doc1 = _make_mock_document(doc_id="d1", user_id="user-1")
        doc2 = _make_mock_document(doc_id="d2", user_id="user-1")
        doc3 = _make_mock_document(doc_id="d3", user_id="user-2")

        svc.documents = {"d1": doc1, "d2": doc2, "d3": doc3}

        result = await svc.get_documents_by_ids(["d1", "d3"], "user-1")
        assert len(result) == 1
        assert result[0].id == "d1"

    # ------------------------------------------------------------------ #
    # _get_folder_path
    # ------------------------------------------------------------------ #

    def test_get_folder_path_from_folder_path_attr(self):
        """Should use doc.folder_path when set."""
        svc = self._make_service()

        doc = MagicMock()
        doc.folder_path = "/Clients/Smith/"
        doc.metadata = {}
        doc.source = MagicMock()
        doc.source.value = "local"

        result = svc._get_folder_path(doc)
        assert result == "/Clients/Smith"

    def test_get_folder_path_default(self):
        """Should fall back to source-based path when no folder info."""
        svc = self._make_service()

        doc = MagicMock()
        doc.folder_path = None
        doc.metadata = {}
        doc.source = MagicMock()
        doc.source.value = "local"

        result = svc._get_folder_path(doc)
        assert result == "/local/"

    def test_get_folder_path_rejects_traversal(self):
        """Should block directory traversal attempts when .. remains after normpath."""
        svc = self._make_service()

        # posixpath.normpath resolves absolute paths like /../../etc/passwd to /etc/passwd
        # (removing the ..). So the actual guard triggers only when ".." survives normpath.
        # A relative path like "foo/../../bar" normalizes to "../bar" which contains "..".
        doc = MagicMock()
        doc.folder_path = "foo/../../bar"
        doc.metadata = {}
        doc.source = MagicMock()
        doc.source.value = "local"

        result = svc._get_folder_path(doc)
        assert result == "/local/"

    # ------------------------------------------------------------------ #
    # clear_all
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_clear_all(self):
        """Should clear all documents for a user."""
        svc = self._make_service()

        doc1 = _make_mock_document(doc_id="d1", user_id="user-1")
        doc2 = _make_mock_document(doc_id="d2", user_id="user-1")
        doc3 = _make_mock_document(doc_id="d3", user_id="user-2")

        svc.documents = {"d1": doc1, "d2": doc2, "d3": doc3}

        mock_rag = AsyncMock()
        mock_rag.delete_document = AsyncMock(return_value=True)
        derived = AsyncMock()

        with (
            patch("app.services.documents.rag_service", mock_rag),
            patch.object(svc, "_save_index"),
            patch.object(svc, "_delete_derived_data", derived),
            patch.object(svc, "_get_user_upload_dir", return_value="/tmp/uploads/user-1"),
            patch("os.listdir", return_value=[]),
        ):
            result = await svc.clear_all("user-1")

        assert result["cleared_documents"] == 2
        assert result["failed_documents"] == 0
        assert result["status"] == "success"
        # user-2's doc should still exist
        assert "d3" in svc.documents
        assert "d1" not in svc.documents
        assert "d2" not in svc.documents
        assert derived.await_count == 2

    @pytest.mark.asyncio
    async def test_clear_all_keeps_documents_whose_vectors_remain(self):
        """A document that cannot be fully removed stays listed, and is reported."""
        svc = self._make_service()
        svc.documents = {
            "d1": _make_mock_document(doc_id="d1", user_id="user-1"),
            "d2": _make_mock_document(doc_id="d2", user_id="user-1"),
        }

        mock_rag = AsyncMock()
        mock_rag.delete_document = AsyncMock(side_effect=[True, False])

        with (
            patch("app.services.documents.rag_service", mock_rag),
            patch.object(svc, "_save_index"),
            patch.object(svc, "_delete_derived_data", AsyncMock()),
            patch.object(svc, "_get_user_upload_dir", return_value="/tmp/uploads/user-1"),
            patch("os.listdir") as listdir,
        ):
            result = await svc.clear_all("user-1")

        assert result == {"cleared_documents": 1, "failed_documents": 1, "status": "partial"}
        assert "d1" not in svc.documents
        assert "d2" in svc.documents
        # The stray-file sweep is skipped: d2 still needs its stored file.
        listdir.assert_not_called()

    # ------------------------------------------------------------------ #
    # upload_and_index
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_upload_and_index_success(self):
        """Should upload, extract text, index, and return document."""
        svc = self._make_service()
        svc.documents = {}

        mock_rag = AsyncMock()
        mock_rag.index_document = AsyncMock(return_value=5)

        from app.services.text_extraction import ExtractionResult

        mock_text_extraction = MagicMock()
        mock_text_extraction.extract.return_value = ExtractionResult(
            text="This is a contract between Party A and Party B."
        )

        _mock_aio_open = AsyncMock()
        mock_aio_file = AsyncMock()
        mock_aio_file.__aenter__ = AsyncMock(return_value=mock_aio_file)
        mock_aio_file.__aexit__ = AsyncMock(return_value=False)
        mock_aio_file.write = AsyncMock()

        with (
            patch("app.services.documents.rag_service", mock_rag),
            patch("app.services.documents.text_extraction_service", mock_text_extraction),
            patch("aiofiles.open", return_value=mock_aio_file),
            patch.object(svc, "_save_index"),
            patch.object(svc, "_get_user_upload_dir", return_value="/tmp/uploads/user-1"),
        ):
            doc = await svc.upload_and_index(
                file_content=b"PDF_CONTENT",
                filename="contract.pdf",
                content_type="application/pdf",
                user_id="user-1",
            )

        assert doc.filename == "contract.pdf"
        assert doc.status.value == "indexed"
        assert doc.chunk_count == 5
        assert doc.user_id == "user-1"

    @pytest.mark.asyncio
    async def test_upload_and_index_empty_text(self):
        """Should set status to FAILED when no text extracted."""
        svc = self._make_service()
        svc.documents = {}

        from app.services.text_extraction import ExtractionResult

        mock_text_extraction = MagicMock()
        mock_text_extraction.extract.return_value = ExtractionResult(
            text="   ",  # Empty after strip
            error="No text could be extracted. This looks like a scanned (image-only) PDF.",
        )

        mock_aio_file = AsyncMock()
        mock_aio_file.__aenter__ = AsyncMock(return_value=mock_aio_file)
        mock_aio_file.__aexit__ = AsyncMock(return_value=False)
        mock_aio_file.write = AsyncMock()

        with (
            patch("app.services.documents.rag_service", MagicMock()),
            patch("app.services.documents.text_extraction_service", mock_text_extraction),
            patch("aiofiles.open", return_value=mock_aio_file),
            patch.object(svc, "_save_index"),
            patch.object(svc, "_get_user_upload_dir", return_value="/tmp/uploads/user-1"),
        ):
            doc = await svc.upload_and_index(
                file_content=b"EMPTY_PDF",
                filename="empty.pdf",
                content_type="application/pdf",
                user_id="user-1",
            )

        assert doc.status.value == "failed"
        # The reason is recorded so the UI can say why, not just "failed".
        assert "scanned" in doc.metadata["error"]


class TestCountAndSharedScope:
    """count_documents reports the full match count; filters may span shared matters."""

    def _service_with(self, docs):
        svc = TestDocumentService()._make_service()
        svc.documents = {d.id: d for d in docs}
        return svc

    @pytest.mark.asyncio
    async def test_count_matches_filters_across_all_pages(self):
        from app.models.schemas import ConnectorType

        own = [_make_mock_document(doc_id=f"d{i}", user_id="user-1") for i in range(5)]
        own[0].source = ConnectorType.BOX
        shared = _make_mock_document(doc_id="shared", user_id="user-2")
        shared.matter_id = "matter-1"
        other = _make_mock_document(doc_id="other", user_id="user-2")
        other.matter_id = None
        svc = self._service_with([*own, shared, other])

        page = await svc.list_documents(
            "user-1", limit=2, offset=0, accessible_matter_ids={"matter-1"}
        )
        assert len(page) == 2
        assert await svc.count_documents("user-1", accessible_matter_ids={"matter-1"}) == 6
        assert await svc.count_documents("user-1") == 5  # owner-only without matters
        assert await svc.count_documents("user-1", source=ConnectorType.BOX) == 1
        # Same filters as list_documents: the paged list and the total agree.
        listed = await svc.list_documents("user-1", limit=500, accessible_matter_ids={"matter-1"})
        assert len(listed) == await svc.count_documents(
            "user-1", accessible_matter_ids={"matter-1"}
        )

    @pytest.mark.asyncio
    async def test_filter_reaches_shared_matter_documents_when_asked(self):
        shared = _make_mock_document(doc_id="shared", user_id="user-2")
        shared.matter_id = "matter-1"
        own = _make_mock_document(doc_id="own", user_id="user-1")
        foreign = _make_mock_document(doc_id="foreign", user_id="user-2")
        foreign.matter_id = "matter-9"
        svc = self._service_with([shared, own, foreign])

        assert sorted(await svc.get_documents_by_filter("user-1")) == ["own"]
        assert sorted(
            await svc.get_documents_by_filter("user-1", accessible_matter_ids={"matter-1"})
        ) == ["own", "shared"]
