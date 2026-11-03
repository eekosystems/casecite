"""Deleting a document is a tracked operation.

Three steps — search index entries, derived analysis rows, stored file — and
the registry entry goes only when all three are gone. A step that fails keeps
the document listed as DELETE_FAILED with the step and error recorded, a
retry resumes at that step, and an operator can see and retry every failed
deletion. A user whose documents could not be purged is not deleted.
"""

import json
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from app.models.schemas import ConnectorType, Document, DocumentStatus
from app.services.documents import (
    DELETION_STEPS,
    DocumentDeletionError,
    DocumentService,
    document_service,
)

from tests.helpers import headers, make_user

pytestmark = pytest.mark.usefixtures("no_rate_limit")


def _doc(doc_id: str = "d1", user_id: str = "u1", **overrides) -> Document:
    fields = {
        "id": doc_id,
        "user_id": user_id,
        "filename": "Engagement Letter.pdf",
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


@pytest.fixture
def svc(tmp_path):
    """A service over a real upload directory holding one stored file."""
    with patch("app.services.documents.settings") as mock_settings:
        mock_settings.upload_dir = str(tmp_path)
        service = DocumentService()
        service.documents = {"d1": _doc()}
        (tmp_path / "u1").mkdir()
        (tmp_path / "u1" / "d1.pdf").write_bytes(b"ciphertext")
        service._save_index()
        yield service


def _rag(ok: bool = True) -> AsyncMock:
    rag = AsyncMock()
    rag.delete_document = AsyncMock(return_value=ok)
    return rag


def _stored(tmp_path) -> dict:
    return {d["id"]: d for d in json.loads((tmp_path / "index.json").read_text())}


# ---- service -----------------------------------------------------------------


async def test_file_removal_failure_keeps_the_document_as_delete_failed(svc, tmp_path):
    rag = _rag()
    with (
        patch("app.services.documents.rag_service", rag),
        patch.object(svc, "_delete_derived_data", AsyncMock()),
        patch("app.services.documents.os.remove", side_effect=PermissionError(13, "busy")),
        pytest.raises(DocumentDeletionError, match="stored file could not be removed"),
    ):
        await svc.delete_document("d1", "u1")

    doc = svc.documents["d1"]
    assert doc.status == DocumentStatus.DELETE_FAILED
    info = doc.metadata["deletion"]
    assert info["failed_step"] == "file"
    assert info["vectors"] is True and info["derived"] is True and info["file"] is False
    assert info["attempts"] == 1
    assert "busy" in info["error"]
    assert info["failed_at"]
    # Persisted: the failure survives a restart.
    assert _stored(tmp_path)["d1"]["status"] == "delete_failed"
    # The file is still there, and the document is still listed as such.
    assert (tmp_path / "u1" / "d1.pdf").exists()
    assert svc.failed_deletions() == [doc]


async def test_retry_resumes_at_the_failed_step(svc, tmp_path):
    rag = _rag()
    derived = AsyncMock()
    with (
        patch("app.services.documents.rag_service", rag),
        patch.object(svc, "_delete_derived_data", derived),
        patch("app.services.documents.os.remove", side_effect=PermissionError(13, "busy")),
        pytest.raises(DocumentDeletionError),
    ):
        await svc.delete_document("d1", "u1")

    # Second attempt: the volume is writable again.
    with (
        patch("app.services.documents.rag_service", rag),
        patch.object(svc, "_delete_derived_data", derived),
    ):
        assert await svc.delete_document("d1", "u1") is True

    assert "d1" not in svc.documents
    assert _stored(tmp_path) == {}
    assert not (tmp_path / "u1" / "d1.pdf").exists()
    # Steps that had completed were not repeated.
    rag.delete_document.assert_awaited_once()
    derived.assert_awaited_once()


async def test_vector_failure_is_recorded_before_anything_else_is_touched(svc, tmp_path):
    derived = AsyncMock()
    with (
        patch("app.services.documents.rag_service", _rag(ok=False)),
        patch.object(svc, "_delete_derived_data", derived),
        pytest.raises(DocumentDeletionError, match="search index"),
    ):
        await svc.delete_document("d1", "u1")
    doc = svc.documents["d1"]
    assert doc.status == DocumentStatus.DELETE_FAILED
    assert doc.metadata["deletion"]["failed_step"] == "vectors"
    assert svc.deletion_progress(doc) == {"vectors": False, "derived": False, "file": False}
    derived.assert_not_awaited()
    assert (tmp_path / "u1" / "d1.pdf").exists()


async def test_derived_failure_is_recorded_after_vectors(svc):
    with (
        patch("app.services.documents.rag_service", _rag()),
        patch.object(
            svc, "_delete_derived_data", AsyncMock(side_effect=DocumentDeletionError("db down"))
        ),
        pytest.raises(DocumentDeletionError, match="db down"),
    ):
        await svc.delete_document("d1", "u1")
    doc = svc.documents["d1"]
    assert doc.metadata["deletion"]["failed_step"] == "derived"
    assert svc.deletion_progress(doc) == {"vectors": True, "derived": False, "file": False}


async def test_attempts_are_counted(svc):
    for n in (1, 2):
        with (
            patch("app.services.documents.rag_service", _rag(ok=False)),
            pytest.raises(DocumentDeletionError),
        ):
            await svc.delete_document("d1", "u1")
        assert svc.documents["d1"].metadata["deletion"]["attempts"] == n


async def test_successful_delete_reports_every_step(svc, tmp_path):
    with (
        patch("app.services.documents.rag_service", _rag()),
        patch.object(svc, "_delete_derived_data", AsyncMock()),
    ):
        progress = await svc._remove_document(svc.documents["d1"])
    assert progress == dict.fromkeys(DELETION_STEPS, True)


async def test_retry_failed_deletion_ignores_documents_that_did_not_fail(svc):
    assert await svc.retry_failed_deletion("d1") is False
    assert await svc.retry_failed_deletion("nope") is False


async def test_purge_reports_failures_and_keeps_the_upload_dir(svc, tmp_path):
    with (
        patch("app.services.documents.rag_service", _rag()),
        patch.object(svc, "_delete_derived_data", AsyncMock()),
        patch("app.services.documents.os.remove", side_effect=PermissionError(13, "busy")),
    ):
        result = await svc.purge_user_data("u1")
    assert result == {"documents_deleted": 0, "documents_failed": 1, "upload_dir_removed": False}
    assert (tmp_path / "u1" / "d1.pdf").exists()
    assert svc.documents["d1"].status == DocumentStatus.DELETE_FAILED


async def test_purge_removes_the_upload_dir_when_every_delete_completed(svc, tmp_path):
    with (
        patch("app.services.documents.rag_service", _rag()),
        patch.object(svc, "_delete_derived_data", AsyncMock()),
    ):
        result = await svc.purge_user_data("u1")
    assert result == {"documents_deleted": 1, "documents_failed": 0, "upload_dir_removed": True}
    assert not (tmp_path / "u1").exists()


# ---- HTTP --------------------------------------------------------------------


def _register_failed(owner_id: str, doc_id: str = "doc-failed-1") -> Document:
    doc = _doc(
        doc_id,
        owner_id,
        status=DocumentStatus.DELETE_FAILED,
        metadata={
            "deletion": {
                "vectors": True,
                "derived": True,
                "file": False,
                "failed_step": "file",
                "error": "[Errno 13] Permission denied",
                "failed_at": "2026-11-03T10:00:00+00:00",
                "attempts": 2,
            }
        },
    )
    document_service.documents[doc_id] = doc
    return doc


@pytest.fixture
def _clean_registry():
    yield
    for key in [k for k in document_service.documents if k.startswith("doc-failed")]:
        document_service.documents.pop(key, None)


def test_delete_failure_names_the_step_and_is_audited(client, auth_headers, audit_events):
    owner = make_user("del-owner")
    doc = _register_failed(owner.id)
    try:
        with patch.object(
            document_service,
            "delete_document",
            AsyncMock(
                side_effect=DocumentDeletionError(
                    "The document's stored file could not be removed, so the document was "
                    "not deleted."
                )
            ),
        ):
            response = client.delete(f"/api/v1/documents/{doc.id}", headers=headers(owner))
    finally:
        document_service.documents.pop(doc.id, None)
    assert response.status_code == 502
    assert "Could not remove the document's stored file" in response.json()["detail"]
    assert "failed deletion" in response.json()["detail"]
    failures = [
        e
        for e in audit_events
        if (e.get("details") or {}).get("action") == "document_delete_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["details"]["failed_step"] == "file"
    assert failures[0]["details"]["removed"] == {"vectors": True, "derived": True, "file": False}


def test_successful_delete_reports_what_was_removed(client):
    owner = make_user("del-owner")
    with patch.object(document_service, "delete_document", AsyncMock(return_value=True)):
        response = client.delete("/api/v1/documents/doc-x", headers=headers(owner))
    assert response.status_code == 200
    assert response.json() == {
        "status": "deleted",
        "id": "doc-x",
        "removed": {"vectors": True, "derived": True, "file": True},
    }


def test_failed_deletions_are_listed_for_operators(client, auth_headers, _clean_registry):
    doc = _register_failed("someone-else")
    response = client.get("/api/v1/documents/failed-deletions", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    item = body["documents"][0]
    assert item["id"] == doc.id
    assert item["user_id"] == "someone-else"
    assert item["failed_step"] == "file"
    assert item["attempts"] == 2
    assert item["removed"] == {"vectors": True, "derived": True, "file": False}
    assert "Permission denied" in item["error"]


def test_failed_deletions_need_the_settings_permission(client, non_admin_headers):
    response = client.get("/api/v1/documents/failed-deletions", headers=non_admin_headers)
    assert response.status_code == 403


def test_operator_retry_completes_the_delete(client, auth_headers, audit_events, _clean_registry):
    doc = _register_failed("someone-else")
    with (
        patch("app.services.documents.rag_service", _rag()),
        patch.object(document_service, "_delete_derived_data", AsyncMock()),
        patch.object(document_service, "_save_index"),
    ):
        response = client.post(f"/api/v1/documents/{doc.id}/retry-delete", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["removed"] == {"vectors": True, "derived": True, "file": True}
    assert doc.id not in document_service.documents
    retried = [
        e
        for e in audit_events
        if (e.get("details") or {}).get("action") == "document_delete_retried"
    ]
    assert len(retried) == 1


def test_operator_retry_that_fails_again_says_so(client, auth_headers, _clean_registry):
    doc = _register_failed("someone-else")
    with (
        patch("app.services.documents.rag_service", _rag()),
        patch.object(document_service, "_delete_derived_data", AsyncMock()),
        patch.object(document_service, "_save_index"),
        patch("app.services.documents.os.path.exists", return_value=True),
        patch("app.services.documents.os.remove", side_effect=PermissionError(13, "still busy")),
    ):
        response = client.post(f"/api/v1/documents/{doc.id}/retry-delete", headers=auth_headers)
    assert response.status_code == 502
    assert "Could not remove the document's stored file" in response.json()["detail"]
    assert document_service.documents[doc.id].metadata["deletion"]["attempts"] == 3


def test_operator_retry_of_a_healthy_document_is_404(client, auth_headers):
    response = client.post("/api/v1/documents/doc-nope/retry-delete", headers=auth_headers)
    assert response.status_code == 404


async def test_stored_file_is_gone_after_a_real_delete(svc, tmp_path):
    """No mocks on the file step: the encrypted original really is removed."""
    assert (tmp_path / "u1" / "d1.pdf").exists()
    with (
        patch("app.services.documents.rag_service", _rag()),
        patch.object(svc, "_delete_derived_data", AsyncMock()),
    ):
        assert await svc.delete_document("d1", "u1") is True
    assert not os.path.exists(tmp_path / "u1" / "d1.pdf")
