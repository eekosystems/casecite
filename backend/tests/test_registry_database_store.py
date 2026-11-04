"""The document registry in the database: shared between processes.

Each process keeps the registry in memory, saves only what changed, and
picks up what other processes saved through a shared version counter — never
losing an unsaved local change. index.json is imported once and kept.
"""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from app.models.registry import DocumentFolderRow, DocumentRegistryRow, SharedVersion
from app.models.schemas import ConnectorType, Document, DocumentStatus
from app.services.documents import DocumentService, RegistryWriteError
from app.services.registry_store import (
    REGISTRY_VERSION,
    DatabaseRegistryStore,
    JsonRegistryStore,
    import_json_into_database,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def sessions(tmp_path):
    """A throwaway SQLite database holding only the registry tables."""
    from app.models.base import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            DocumentRegistryRow.__table__,
            DocumentFolderRow.__table__,
            SharedVersion.__table__,
        ],
    )
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _doc(doc_id: str, user_id: str = "u1", **overrides) -> Document:
    fields = {
        "id": doc_id,
        "user_id": user_id,
        "filename": f"{doc_id}.pdf",
        "content_type": "application/pdf",
        "size": 10,
        "source": ConnectorType.LOCAL,
        "status": DocumentStatus.INDEXED,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "chunk_count": 3,
    }
    fields.update(overrides)
    return Document(**fields)


def _service(sessions, upload_dir) -> DocumentService:
    with patch("app.services.documents.settings") as mock_settings:
        mock_settings.upload_dir = str(upload_dir)
        return DocumentService(store=DatabaseRegistryStore(sessions))


# ---- store -----------------------------------------------------------------------


def test_save_writes_only_what_changed_and_bumps_the_version(sessions):
    store = DatabaseRegistryStore(sessions)
    a, b = _doc("a"), _doc("b")
    store.save_documents({"a": a, "b": b}, {"a": a, "b": b}, set())
    assert store.version() == 1
    assert set(store.load_documents()) == {"a", "b"}

    b2 = _doc("b", status=DocumentStatus.FAILED)
    store.save_documents({"b": b2}, {"b": b2}, {"a"})
    assert store.version() == 2
    loaded = store.load_documents()
    assert set(loaded) == {"b"} and loaded["b"].status == DocumentStatus.FAILED

    store.save_documents({"b": b2}, {}, set())  # nothing changed: no bump
    assert store.version() == 2


def test_folders_round_trip(sessions):
    store = DatabaseRegistryStore(sessions)
    store.save_folders({"u1": ["/Cases", "/Cases/Smith"], "u2": []})
    assert store.load_folders() == {"u1": ["/Cases", "/Cases/Smith"], "u2": []}
    store.save_folders({"u1": ["/Cases"]})
    assert store.load_folders() == {"u1": ["/Cases"]}


def test_index_json_is_imported_once_and_kept(sessions, tmp_path):
    json_store = JsonRegistryStore(str(tmp_path))
    (tmp_path / "index.json").write_text(json.dumps([_doc("a").model_dump(mode="json")]), "utf-8")
    (tmp_path / "folders.json").write_text(json.dumps({"u1": ["/Cases"]}), "utf-8")
    db_store = DatabaseRegistryStore(sessions)

    result = import_json_into_database(json_store, db_store)
    assert result["migrated"] is True and result["documents"] == 1 and result["folders"] == 1
    assert set(db_store.load_documents()) == {"a"}
    assert db_store.load_folders() == {"u1": ["/Cases"]}
    assert not (tmp_path / "index.json").exists()
    assert [p.name for p in tmp_path.glob("index.json.migrated-*")]
    assert [p.name for p in tmp_path.glob("folders.json.migrated-*")]

    # A second start finds the tables populated and leaves things alone.
    (tmp_path / "index.json").write_text("[]", "utf-8")
    assert import_json_into_database(json_store, db_store)["migrated"] is False
    assert (tmp_path / "index.json").exists()


# ---- service ---------------------------------------------------------------------


def test_service_persists_through_the_database_and_reloads(sessions, tmp_path):
    svc = _service(sessions, tmp_path)
    svc.documents["a"] = _doc("a")
    svc._save_index()
    assert svc.registry_backend == "database"

    again = _service(sessions, tmp_path)
    assert set(again.documents) == {"a"}
    assert not (tmp_path / "index.json").exists()


def test_two_processes_see_each_others_changes(sessions, tmp_path):
    one = _service(sessions, tmp_path)
    two = _service(sessions, tmp_path)
    one.documents["a"] = _doc("a")
    one._save_index()
    two._last_version_check = 0.0  # the refresh throttle would otherwise wait
    assert "a" in two.documents

    two.documents.pop("a")
    two._save_index()
    one._last_version_check = 0.0
    assert "a" not in one.documents


def test_a_refresh_keeps_an_unsaved_local_change(sessions, tmp_path):
    one = _service(sessions, tmp_path)
    two = _service(sessions, tmp_path)
    one.documents["a"] = _doc("a")
    one.documents["b"] = _doc("b")
    one._save_index()
    two._last_version_check = 0.0
    assert set(two.documents) == {"a", "b"}

    # Process two edits "a" but has not saved; process one deletes "b" and
    # changes "a" as well.
    two._documents["a"].status = DocumentStatus.FAILED
    one.documents.pop("b")
    one.documents["a"].chunk_count = 99
    one._save_index()

    two._last_version_check = 0.0
    docs = two.documents
    assert "b" not in docs  # deleted elsewhere, untouched here: gone
    assert docs["a"].status == DocumentStatus.FAILED  # the unsaved local edit survives
    assert docs["a"].chunk_count == 3  # ...and is not overwritten by the other change
    two._save_index()  # now persisted: the local version wins for "a"
    one._last_version_check = 0.0
    assert one.documents["a"].status == DocumentStatus.FAILED


def test_a_failed_database_write_is_a_registry_write_error(sessions, tmp_path):
    svc = _service(sessions, tmp_path)
    svc.documents["a"] = _doc("a")
    with (
        patch.object(svc._store, "save_documents", side_effect=RuntimeError("db down")),
        pytest.raises(RegistryWriteError),
    ):
        svc._save_index()
    # Nothing is recorded as persisted; the next save retries the row.
    assert "a" not in svc._snapshot


def test_version_counter_row(sessions):
    store = DatabaseRegistryStore(sessions)
    assert store.version() == 0
    assert store.bump() == 1
    with sessions() as db:
        assert db.get(SharedVersion, REGISTRY_VERSION).version == 1
