"""The document registry (index.json) must never fail silently.

A write that fails raises RegistryWriteError and the operation rolls back
what it changed, so a document never looks uploaded, moved or deleted in this
process while the file on disk says otherwise. A registry that cannot be read
at startup is fatal rather than replaced by an empty one.
"""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from app.services.documents import DocumentService, RegistryWriteError
from app.services.text_extraction import ExtractionResult

from tests.helpers import headers, make_user

pytestmark = pytest.mark.usefixtures("no_rate_limit")

_UPLOAD = {"file_content": b"PDF", "filename": "a.pdf", "content_type": "application/pdf"}


@pytest.fixture
def svc(tmp_path):
    """A service over a real, empty upload directory."""
    with patch("app.services.documents.settings") as mock_settings:
        mock_settings.upload_dir = str(tmp_path)
        yield DocumentService()


def _rag(chunks: int = 5) -> AsyncMock:
    rag = AsyncMock()
    rag.index_document = AsyncMock(return_value=chunks)
    rag.delete_document = AsyncMock(return_value=True)
    return rag


def _failing_replace(fail_on_call: int):
    """os.replace that raises on the Nth call (1-based) and works otherwise."""
    real = os.replace
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise OSError(28, "No space left on device")
        return real(src, dst)

    return replace


def _on_disk(tmp_path) -> list[str]:
    return [d["id"] for d in json.loads((tmp_path / "index.json").read_text())]


async def _persisted_upload(svc, rag=None):
    with (
        patch("app.services.documents.rag_service", rag or _rag()),
        patch.object(svc, "extract", AsyncMock(return_value=ExtractionResult(text="hello"))),
    ):
        return await svc.upload_and_index(user_id="u1", **_UPLOAD)


# ---- startup -----------------------------------------------------------------


def test_corrupt_registry_refuses_to_start_and_keeps_the_file(tmp_path):
    index = tmp_path / "index.json"
    index.write_text("[{not json")
    with patch("app.services.documents.settings") as mock_settings:
        mock_settings.upload_dir = str(tmp_path)
        with pytest.raises(RuntimeError, match="Refusing to start"):
            DocumentService()
    assert index.read_text() == "[{not json"


def test_registry_with_a_malformed_entry_refuses_to_start(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps([{"id": "d1"}]))
    with patch("app.services.documents.settings") as mock_settings:
        mock_settings.upload_dir = str(tmp_path)
        with pytest.raises(RuntimeError, match="could not be loaded"):
            DocumentService()


def test_missing_registry_starts_empty(svc):
    assert svc.documents == {}


# ---- upload ------------------------------------------------------------------


async def test_upload_whose_first_registry_write_fails_leaves_nothing_behind(svc, tmp_path):
    rag = _rag()
    with (
        patch("app.services.documents.rag_service", rag),
        patch("app.services.documents.os.replace", side_effect=OSError(28, "No space left")),
        pytest.raises(RegistryWriteError, match="Nothing was changed"),
    ):
        await svc.upload_and_index(user_id="u1", **_UPLOAD)
    assert svc.documents == {}
    assert os.listdir(tmp_path / "u1") == []
    assert not (tmp_path / "index.json").exists()
    rag.index_document.assert_not_called()


async def test_upload_whose_final_registry_write_fails_is_undone(svc, tmp_path):
    """The vectors were written and the file stored, but the registry entry
    could not be. Both are removed again so the upload is not half-kept, and
    the PROCESSING row the first write persisted is removed from disk too."""
    rag = _rag()
    with (
        patch("app.services.documents.rag_service", rag),
        patch.object(svc, "extract", AsyncMock(return_value=ExtractionResult(text="hello"))),
        patch("app.services.documents.os.replace", side_effect=_failing_replace(2)),
        pytest.raises(RegistryWriteError),
    ):
        await svc.upload_and_index(user_id="u1", **_UPLOAD)
    assert svc.documents == {}
    assert os.listdir(tmp_path / "u1") == []
    rag.index_document.assert_awaited_once()
    rag.delete_document.assert_awaited_once()
    assert _on_disk(tmp_path) == []


async def test_upload_that_persists_is_on_disk(svc, tmp_path):
    doc = await _persisted_upload(svc)
    saved = json.loads((tmp_path / "index.json").read_text())
    assert [d["id"] for d in saved] == [doc.id]
    assert saved[0]["status"] == "indexed"


# ---- delete / move -----------------------------------------------------------


async def test_delete_whose_registry_write_fails_says_the_entry_may_return(svc, tmp_path):
    rag = _rag()
    doc = await _persisted_upload(svc, rag)
    with (
        patch("app.services.documents.rag_service", rag),
        patch.object(svc, "_delete_derived_data", AsyncMock()),
        patch("app.services.documents.os.replace", side_effect=OSError(13, "Permission denied")),
        pytest.raises(RegistryWriteError, match="may reappear after a restart"),
    ):
        await svc.delete_document(doc.id, "u1")
    # On disk the entry is still there: a restart will show it again.
    assert _on_disk(tmp_path) == [doc.id]


async def test_move_whose_registry_write_fails_is_rolled_back(svc, tmp_path):
    doc = await _persisted_upload(svc)
    with (
        patch("app.services.documents.os.replace", side_effect=OSError(13, "Permission denied")),
        pytest.raises(RegistryWriteError),
    ):
        svc.move_document(doc.id, "u1", "/Cases/Smith")
    assert svc.documents[doc.id].folder_path is None


# ---- HTTP --------------------------------------------------------------------


def test_upload_registry_failure_is_a_503_and_not_audited(client, audit_events):
    from app.services.audit import AuditEventType
    from app.services.documents import document_service

    owner = make_user("up-owner")
    with patch.object(
        document_service, "upload_and_index", AsyncMock(side_effect=RegistryWriteError())
    ):
        response = client.post(
            "/api/v1/documents",
            files={"file": ("notes.txt", b"hello world", "text/plain")},
            headers=headers(owner),
        )
    assert response.status_code == 503, response.text
    assert "registry could not be written" in response.json()["detail"]
    assert not [e for e in audit_events if e["event_type"] == AuditEventType.DOCUMENT_UPLOAD]


def test_delete_registry_failure_is_a_503(client):
    from app.services.documents import document_service

    owner = make_user("del-owner")
    with patch.object(
        document_service,
        "delete_document",
        AsyncMock(side_effect=RegistryWriteError("content removed; entry may reappear")),
    ):
        response = client.delete("/api/v1/documents/doc-x", headers=headers(owner))
    assert response.status_code == 503, response.text
    assert "may reappear" in response.json()["detail"]


def test_save_index_cleans_up_its_temp_file(svc, tmp_path):
    with (
        patch("app.services.documents.os.replace", side_effect=OSError(28, "No space left")),
        pytest.raises(RegistryWriteError),
    ):
        svc._save_index()
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []


def test_registry_write_error_is_a_503_app_error():
    from app.exceptions import ServiceUnavailableError

    assert isinstance(RegistryWriteError(), ServiceUnavailableError)
    assert RegistryWriteError().status_code == 503
