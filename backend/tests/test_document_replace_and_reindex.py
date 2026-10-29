"""
Upload replacement, re-indexing and derived-data deletion in the document service.

The incidents these guard against:
- a second "Engagement Letter.pdf" for a different client silently destroying
  the first one's file and vectors;
- a failed re-upload costing the user the copy they already had;
- a rebuild dropping a document's matter tag, or leaving it INDEXED with no
  vectors behind it;
- contract analyses (verbatim quotes) outliving the document
  they were derived from.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.schemas import ConnectorType, Document, DocumentStatus
from app.services.documents import DocumentService
from app.services.text_extraction import ExtractionResult


def _doc(doc_id: str, filename: str = "Engagement Letter.pdf", **overrides) -> Document:
    fields = {
        "id": doc_id,
        "user_id": "u1",
        "filename": filename,
        "content_type": "application/pdf",
        "size": 10,
        "source": ConnectorType.LOCAL,
        "status": DocumentStatus.INDEXED,
        "created_at": datetime.now(UTC),
        "chunk_count": 7,
        "metadata": {},
    }
    fields.update(overrides)
    return Document(**fields)


def _service(docs: list[Document], tmp_path) -> DocumentService:
    service = DocumentService.__new__(DocumentService)
    service.documents = {d.id: d for d in docs}
    service.folders = {}
    service._save_index = MagicMock()
    service._get_user_upload_dir = MagicMock(return_value=str(tmp_path))
    service._delete_derived_data = AsyncMock()
    return service


def _rag(chunks: int = 3) -> MagicMock:
    rag = MagicMock()
    rag.index_document = AsyncMock(return_value=chunks)
    rag.replace_document = AsyncMock(return_value=chunks)
    rag.delete_document = AsyncMock(return_value=True)
    return rag


async def _upload(service: DocumentService, rag, text: str = "contract text", **kwargs) -> Document:
    extraction = MagicMock()
    extraction.extract.return_value = ExtractionResult(text=text)
    audit = MagicMock()
    audit.log_event = AsyncMock()
    with (
        patch("app.services.documents.rag_service", rag),
        patch("app.services.documents.text_extraction_service", extraction),
        patch("app.services.audit.audit_service", audit),
    ):
        doc = await service.upload_and_index(
            file_content=b"bytes",
            filename=kwargs.pop("filename", "Engagement Letter.pdf"),
            content_type="application/pdf",
            user_id="u1",
            **kwargs,
        )
    service._audit = audit
    return doc


class TestSameFilenameUpload:
    @pytest.mark.asyncio
    async def test_same_name_in_another_folder_keeps_both(self, tmp_path):
        """The reported data-loss case: same filename, different client folder."""
        old = _doc("old", folder_path="/Client A")
        service = _service([old], tmp_path)
        rag = _rag()

        new = await _upload(service, rag)

        assert "old" in service.documents
        assert new.id in service.documents
        rag.delete_document.assert_not_awaited()
        assert "replaced_document_id" not in new.metadata

    @pytest.mark.asyncio
    async def test_same_name_in_another_matter_keeps_both(self, tmp_path):
        old = _doc("old", matter_id="matter-a")
        service = _service([old], tmp_path)
        rag = _rag()

        new = await _upload(service, rag, matter_id="matter-b")

        assert set(service.documents) == {"old", new.id}
        rag.delete_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_distinct_remote_files_with_one_name_keep_both(self, tmp_path):
        old = _doc(
            "old", filename="Agreement.pdf", source=ConnectorType.GOOGLE_DRIVE, source_id="r1"
        )
        service = _service([old], tmp_path)
        rag = _rag()

        new = await _upload(
            service,
            rag,
            filename="Agreement.pdf",
            source=ConnectorType.GOOGLE_DRIVE,
            source_id="r2",
        )

        assert set(service.documents) == {"old", new.id}

    @pytest.mark.asyncio
    async def test_same_remote_file_replaces_its_copy_even_when_renamed(self, tmp_path):
        old = _doc(
            "old",
            filename="Draft.pdf",
            source=ConnectorType.GOOGLE_DRIVE,
            source_id="r1",
            metadata={"folder_path": "/Old Folder"},
        )
        other = _doc(
            "other", filename="Final.pdf", source=ConnectorType.GOOGLE_DRIVE, source_id="r9"
        )
        service = _service([old, other], tmp_path)

        new = await _upload(
            service,
            _rag(),
            filename="Final.pdf",
            source=ConnectorType.GOOGLE_DRIVE,
            source_id="r1",
        )

        # Matched on the remote id: the renamed file replaces its own old copy,
        # not the unrelated document that now shares its name.
        assert set(service.documents) == {"other", new.id}
        assert new.metadata["replaced_document_id"] == "old"

    @pytest.mark.asyncio
    async def test_remote_id_in_metadata_is_honoured(self, tmp_path):
        old = _doc(
            "old",
            filename="Agreement.pdf",
            source=ConnectorType.BOX,
            metadata={"source_id": "r1"},
        )
        service = _service([old], tmp_path)

        new = await _upload(
            service,
            _rag(),
            filename="Agreement.pdf",
            source=ConnectorType.BOX,
            metadata={"source_id": "r2"},
        )

        assert set(service.documents) == {"old", new.id}

    @pytest.mark.asyncio
    async def test_legacy_synced_copy_without_remote_id_is_still_replaced(self, tmp_path):
        old = _doc("old", filename="Agreement.pdf", source=ConnectorType.BOX)
        service = _service([old], tmp_path)

        new = await _upload(
            service, _rag(), filename="Agreement.pdf", source=ConnectorType.BOX, source_id="r1"
        )

        assert set(service.documents) == {new.id}

    @pytest.mark.asyncio
    async def test_true_reupload_replaces_after_indexing_and_is_audited(self, tmp_path):
        old = _doc("old")
        service = _service([old], tmp_path)
        rag = _rag()
        order: list[str] = []
        rag.index_document.side_effect = lambda **_: order.append("index") or 3
        rag.delete_document.side_effect = lambda _id: order.append("delete") or True

        new = await _upload(service, rag)

        # New copy indexed first; only then is the old one removed.
        assert order == ["index", "delete"]
        assert set(service.documents) == {new.id}
        assert new.metadata["replaced_document_id"] == "old"
        service._delete_derived_data.assert_awaited_once_with(["old"])
        call = service._audit.log_event.await_args.kwargs
        assert call["resource_id"] == "old"
        assert call["details"] == {"action": "replaced_by_upload", "replaced_by": new.id}

    @pytest.mark.asyncio
    async def test_failed_reupload_keeps_the_existing_document(self, tmp_path):
        old = _doc("old")
        service = _service([old], tmp_path)
        rag = _rag()
        rag.index_document = AsyncMock(side_effect=RuntimeError("embedding provider down"))

        new = await _upload(service, rag)

        assert new.status == DocumentStatus.FAILED
        assert "old" in service.documents
        assert service.documents["old"].status == DocumentStatus.INDEXED
        rag.delete_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replace_existing_false_still_rejects_a_true_duplicate(self, tmp_path):
        service = _service([_doc("old")], tmp_path)
        with pytest.raises(ValueError, match="already exists"):
            await _upload(service, _rag(), replace_existing=False)

    @pytest.mark.asyncio
    async def test_zero_chunks_is_a_failure_not_an_empty_index_entry(self, tmp_path):
        service = _service([], tmp_path)
        doc = await _upload(service, _rag(chunks=0))
        assert doc.status == DocumentStatus.FAILED
        assert "no searchable content" in doc.metadata["error"]

    @pytest.mark.asyncio
    async def test_extraction_warnings_are_recorded(self, tmp_path):
        service = _service([], tmp_path)
        extraction = MagicMock()
        extraction.extract.return_value = ExtractionResult(
            text="text", warnings=["Only the first 5,000 of 6,200 pages were read."], truncated=True
        )
        with (
            patch("app.services.documents.rag_service", _rag()),
            patch("app.services.documents.text_extraction_service", extraction),
        ):
            doc = await service.upload_and_index(
                file_content=b"x", filename="big.pdf", content_type="application/pdf", user_id="u1"
            )
        assert doc.status == DocumentStatus.INDEXED
        assert "6,200 pages" in doc.metadata["extraction_warning"]


class TestReindex:
    def _stored(self, tmp_path, doc: Document) -> None:
        (tmp_path / f"{doc.id}.pdf").write_bytes(b"stored")

    @pytest.mark.asyncio
    async def test_reconcile_preserves_matter_folder_and_metadata(self, tmp_path):
        doc = _doc(
            "a",
            matter_id="matter-a",
            folder_path="/Client A",
            metadata={"doc_type": "contract", "error": "stale"},
        )
        service = _service([doc], tmp_path)
        self._stored(tmp_path, doc)
        service.extract = AsyncMock(return_value=ExtractionResult(text="recovered"))
        rag = _rag(chunks=4)
        rag.vector_db.has_document = AsyncMock(return_value=False)

        with (
            patch("app.services.documents.rag_service", rag),
            patch("app.services.user_keys.UserAPIKeys.for_user", return_value=MagicMock()),
        ):
            stats = await service.reconcile_index()

        assert stats == {"checked": 1, "reindexed": 1, "failed": 0, "keyword_backfilled": 0}
        kwargs = rag.index_document.await_args.kwargs
        assert kwargs["matter_id"] == "matter-a"
        assert kwargs["folder_path"] == "/Client A"
        assert kwargs["metadata"] == {"doc_type": "contract"}
        assert "error" not in doc.metadata

    @pytest.mark.asyncio
    async def test_reconcile_marks_zero_chunk_rebuild_failed(self, tmp_path):
        doc = _doc("a")
        service = _service([doc], tmp_path)
        self._stored(tmp_path, doc)
        service.extract = AsyncMock(return_value=ExtractionResult(text="recovered"))
        rag = _rag(chunks=0)
        rag.vector_db.has_document = AsyncMock(return_value=False)

        with (
            patch("app.services.documents.rag_service", rag),
            patch("app.services.user_keys.UserAPIKeys.for_user", return_value=MagicMock()),
        ):
            stats = await service.reconcile_index()

        assert stats["failed"] == 1
        assert doc.status == DocumentStatus.FAILED
        assert doc.metadata["error"]

    @pytest.mark.asyncio
    async def test_reindex_document_keeps_the_index_when_embedding_fails(self, tmp_path):
        """A provider failure happens before the old vectors are touched."""
        doc = _doc("a", matter_id="matter-a")
        service = _service([doc], tmp_path)
        self._stored(tmp_path, doc)
        service.extract = AsyncMock(return_value=ExtractionResult(text="text"))
        rag = _rag()
        rag.replace_document = AsyncMock(side_effect=RuntimeError("401 invalid_api_key"))

        with (
            patch("app.services.documents.rag_service", rag),
            pytest.raises(RuntimeError, match="invalid_api_key"),
        ):
            await service.reindex_document("a", "u1")

        rag.delete_document.assert_not_awaited()
        assert doc.status == DocumentStatus.INDEXED
        assert doc.chunk_count == 7
        assert "error" not in doc.metadata

    @pytest.mark.asyncio
    async def test_reindex_document_marks_failed_when_the_store_write_fails(self, tmp_path):
        """Old vectors gone and the new ones not stored: the document is FAILED."""
        from app.services.rag.service import VectorWriteError

        doc = _doc("a")
        service = _service([doc], tmp_path)
        self._stored(tmp_path, doc)
        service.extract = AsyncMock(return_value=ExtractionResult(text="text"))
        rag = _rag()
        rag.replace_document = AsyncMock(
            side_effect=VectorWriteError("storing failed", vectors_removed=True)
        )

        with patch("app.services.documents.rag_service", rag):
            result = await service.reindex_document("a", "u1")

        assert result is doc
        assert doc.status == DocumentStatus.FAILED
        assert doc.chunk_count == 0
        assert doc.metadata["error"].startswith("Re-indexing failed")

    @pytest.mark.asyncio
    async def test_reindex_document_swaps_vectors_and_keeps_where_it_is_filed(self, tmp_path):
        doc = _doc("a", matter_id="matter-a", folder_path="/Client A")
        service = _service([doc], tmp_path)
        self._stored(tmp_path, doc)
        service.extract = AsyncMock(return_value=ExtractionResult(text="text"))
        rag = _rag(chunks=5)

        with patch("app.services.documents.rag_service", rag):
            result = await service.reindex_document("a", "u1")

        assert result.status == DocumentStatus.INDEXED
        assert result.chunk_count == 5
        kwargs = rag.replace_document.await_args.kwargs
        assert kwargs["matter_id"] == "matter-a"
        assert kwargs["folder_path"] == "/Client A"
        rag.index_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reindex_document_keeps_vectors_when_file_is_unreadable(self, tmp_path):
        doc = _doc("a")
        service = _service([doc], tmp_path)
        self._stored(tmp_path, doc)
        service.extract = AsyncMock(
            return_value=ExtractionResult(text="", error="The PDF could not be read.")
        )
        rag = _rag()

        with (
            patch("app.services.documents.rag_service", rag),
            pytest.raises(ValueError, match="could not be read"),
        ):
            await service.reindex_document("a", "u1")

        rag.delete_document.assert_not_awaited()
        assert doc.status == DocumentStatus.INDEXED

    @pytest.mark.asyncio
    async def test_reindex_document_is_owner_scoped(self, tmp_path):
        service = _service([_doc("a")], tmp_path)
        with patch("app.services.documents.rag_service", _rag()):
            assert await service.reindex_document("a", "someone-else") is None


class TestDerivedDataDeletion:
    @pytest.mark.asyncio
    async def test_runs_and_children_for_the_document_are_deleted(self, tmp_path):
        """Real tables: analyses go; other documents' stay."""
        from app.models.clause_intel import ClauseTagFinding, ContractAnalysisRun
        from app.models.contract_analysis import ContractParty
        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'derived.db'}")
        tables = [
            m.__table__
            for m in (
                ContractAnalysisRun,
                ContractParty,
                ClauseTagFinding,
            )
        ]
        from app.models.clause_intel import ClauseDeviationFinding
        from app.models.contract_analysis import (
            ContractDeadline,
            ContractDefinedTerm,
            ContractObligation,
        )

        tables += [
            m.__table__
            for m in (
                ClauseDeviationFinding,
                ContractDeadline,
                ContractDefinedTerm,
                ContractObligation,
            )
        ]
        async with engine.begin() as conn:
            for table in tables:
                await conn.run_sync(table.create)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

        async with session_factory() as db:
            for run_id, doc_id in (("run-gone", "doc-gone"), ("run-kept", "doc-kept")):
                db.add(
                    ContractAnalysisRun(
                        id=run_id,
                        user_id="u1",
                        document_id=doc_id,
                        contract_type="nda",
                        document_length_chars=10,
                    )
                )
                db.add(
                    ContractParty(
                        analysis_id=run_id,
                        canonical_name="Acme LLC",
                        detection_method="defined",
                    )
                )
            await db.commit()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx():
            async with session_factory() as session:
                yield session
                await session.commit()

        service = DocumentService.__new__(DocumentService)
        with patch("app.database.get_db_context", _ctx):
            await service._delete_derived_data(["doc-gone"])

        async with session_factory() as db:

            async def ids(column):
                return set((await db.execute(select(column))).scalars().all())

            assert await ids(ContractAnalysisRun.id) == {"run-kept"}
            assert await ids(ContractParty.analysis_id) == {"run-kept"}
            assert (await db.execute(select(func.count()).select_from(ContractParty))).scalar() == 1
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_database_failure_blocks_the_delete(self, tmp_path):
        """If derived rows cannot be removed the document must stay, for a retry."""
        from app.services.documents import DocumentDeletionError

        doc = _doc("a")
        service = _service([doc], tmp_path)
        service._delete_derived_data = AsyncMock(
            side_effect=DocumentDeletionError("could not be removed")
        )
        with (
            patch("app.services.documents.rag_service", _rag()),
            pytest.raises(DocumentDeletionError),
        ):
            await service.delete_document("a", "u1")
        assert "a" in service.documents
