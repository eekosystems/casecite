"""Where the document registry is persisted.

Two stores with one interface:

- ``JsonRegistryStore`` — ``index.json`` / ``folders.json`` in the upload
  directory: the historical layout, still used by a service built without a
  database (unit tests) and as the source of a one-time import.
- ``DatabaseRegistryStore`` — the ``document_registry`` / ``document_folders``
  tables, written incrementally (only rows that changed) in one transaction,
  with a shared version counter so every process notices a change another
  made. This is what the running app uses; ``DocumentService.use_database_store``
  switches to it at startup and imports ``index.json`` the first time.

The stores are synchronous: the registry is mutated and saved from
synchronous service code, as the JSON file always was. The database store
uses the sync engine; each save is a few small statements.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from typing import Any

from app.models.schemas import Document

logger = logging.getLogger(__name__)

REGISTRY_VERSION = "document_registry"


def document_json(doc: Document) -> str:
    """A canonical serialisation, used to tell a changed document from an
    unchanged one without comparing objects field by field."""
    return json.dumps(doc.model_dump(mode="json"), sort_keys=True, default=str)


class JsonRegistryStore:
    """``index.json`` and ``folders.json`` under the upload directory."""

    shared = False  # one process only: nothing to refresh from

    def __init__(self, upload_dir: str):
        self.upload_dir = upload_dir
        self._version = 0

    @property
    def index_path(self) -> str:
        return os.path.join(self.upload_dir, "index.json")

    @property
    def folders_path(self) -> str:
        return os.path.join(self.upload_dir, "folders.json")

    def load_documents(self) -> dict[str, Document]:
        """Raises on an unreadable file: see DocumentService._load_index."""
        if not os.path.exists(self.index_path):
            return {}
        with open(self.index_path) as f:
            data = json.load(f)
        loaded: dict[str, Document] = {}
        for doc_data in data:
            if "user_id" not in doc_data:  # legacy rows
                doc_data["user_id"] = "legacy"
            doc = Document(**doc_data)
            loaded[doc.id] = doc
        return loaded

    def save_documents(
        self,
        documents: dict[str, Document],
        changed: dict[str, Document],
        removed: set[str],
    ) -> None:
        """The whole file, atomically (temp + rename). Raises OSError/ValueError."""
        tmp_path: str | None = None
        try:
            data = [doc.model_dump(mode="json") for doc in documents.values()]
            os.makedirs(self.upload_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.upload_dir, suffix=".tmp", delete=False
            ) as f:
                tmp_path = f.name
                json.dump(data, f, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.index_path)  # Atomic on POSIX
        except BaseException:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
        self._version += 1

    def load_folders(self) -> dict[str, list[str]]:
        if not os.path.exists(self.folders_path):
            return {}
        try:
            with open(self.folders_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {u: list(v) for u, v in data.items()}
        except (ValueError, KeyError, OSError) as e:
            logger.error(f"Error loading folders: {e}")
        return {}

    def save_folders(self, folders: dict[str, list[str]]) -> None:
        try:
            os.makedirs(self.upload_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.upload_dir, suffix=".tmp", delete=False
            ) as f:
                json.dump(folders, f)
                tmp_path = f.name
            os.replace(tmp_path, self.folders_path)
        except (ValueError, KeyError, OSError) as e:
            logger.error(f"Error saving folders: {e}")

    def version(self) -> int:
        return self._version

    def bump(self) -> int:
        return self._version


class DatabaseRegistryStore:
    """The ``document_registry`` / ``document_folders`` tables."""

    shared = True

    def __init__(self, session_factory=None):
        if session_factory is None:
            from app.database import SyncSessionLocal

            session_factory = SyncSessionLocal
        self._sessions = session_factory

    def load_documents(self) -> dict[str, Document]:
        from app.models.registry import DocumentRegistryRow

        with self._sessions() as db:
            rows = db.query(DocumentRegistryRow).all()
            out: dict[str, Document] = {}
            for row in rows:
                try:
                    out[row.id] = Document(**row.data)
                except (ValueError, TypeError) as e:  # a row the schema outgrew
                    logger.error(f"Document registry row {row.id} is unreadable: {e}")
            return out

    def save_documents(
        self,
        documents: dict[str, Document],
        changed: dict[str, Document],
        removed: set[str],
    ) -> None:
        """Upsert the changed rows and delete the removed ones, in one
        transaction, and bump the shared version. Raises on failure."""
        from sqlalchemy import delete

        from app.models.registry import DocumentRegistryRow

        if not changed and not removed:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        with self._sessions() as db:
            for doc_id, doc in changed.items():
                row = db.get(DocumentRegistryRow, doc_id)
                payload = doc.model_dump(mode="json")
                if row is None:
                    db.add(
                        DocumentRegistryRow(
                            id=doc_id,
                            user_id=doc.user_id,
                            matter_id=doc.matter_id,
                            data=payload,
                            updated_at=now,
                        )
                    )
                else:
                    row.user_id = doc.user_id
                    row.matter_id = doc.matter_id
                    row.data = payload
                    row.updated_at = now
            if removed:
                db.execute(delete(DocumentRegistryRow).where(DocumentRegistryRow.id.in_(removed)))
            _bump(db, REGISTRY_VERSION)
            db.commit()

    def load_folders(self) -> dict[str, list[str]]:
        from app.models.registry import DocumentFolderRow

        with self._sessions() as db:
            return {row.user_id: list(row.folders or []) for row in db.query(DocumentFolderRow)}

    def save_folders(self, folders: dict[str, list[str]]) -> None:
        from sqlalchemy import delete

        from app.models.registry import DocumentFolderRow

        now = datetime.now(UTC).replace(tzinfo=None)
        with self._sessions() as db:
            existing = {row.user_id: row for row in db.query(DocumentFolderRow)}
            for user_id, paths in folders.items():
                row = existing.pop(user_id, None)
                if row is None:
                    db.add(DocumentFolderRow(user_id=user_id, folders=list(paths), updated_at=now))
                elif list(row.folders or []) != list(paths):
                    row.folders = list(paths)
                    row.updated_at = now
            if existing:
                db.execute(
                    delete(DocumentFolderRow).where(DocumentFolderRow.user_id.in_(list(existing)))
                )
            _bump(db, REGISTRY_VERSION)
            db.commit()

    def version(self) -> int:
        with self._sessions() as db:
            return _version(db, REGISTRY_VERSION)

    def bump(self) -> int:
        with self._sessions() as db:
            value = _bump(db, REGISTRY_VERSION)
            db.commit()
            return value

    def is_empty(self) -> bool:
        from app.models.registry import DocumentFolderRow, DocumentRegistryRow

        with self._sessions() as db:
            return (
                db.query(DocumentRegistryRow.id).first() is None
                and db.query(DocumentFolderRow.user_id).first() is None
            )


# ── Shared version counters ──────────────────────────────────────────────
# Also used by the permission matrix and legal-hold caches (see
# shared_versions.py) so one bump mechanism serves every process-local cache.


def _version(db, name: str) -> int:
    from app.models.registry import SharedVersion

    row = db.get(SharedVersion, name)
    return int(row.version) if row is not None else 0


def _bump(db, name: str) -> int:
    from app.models.registry import SharedVersion

    row = db.get(SharedVersion, name)
    if row is None:
        row = SharedVersion(name=name, version=0)
        db.add(row)
    row.version = int(row.version or 0) + 1
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    db.flush()
    return int(row.version)


def import_json_into_database(
    json_store: JsonRegistryStore, db_store: DatabaseRegistryStore
) -> dict[str, Any]:
    """One-time move of index.json / folders.json into the tables.

    Only when the tables are empty and the files exist. The files are renamed
    (``*.migrated-<timestamp>``), never deleted, so the move can be inspected
    or reversed by hand.
    """
    result = {"documents": 0, "folders": 0, "migrated": False}
    if not db_store.is_empty():
        return result
    has_index = os.path.exists(json_store.index_path)
    has_folders = os.path.exists(json_store.folders_path)
    if not has_index and not has_folders:
        return result
    documents = json_store.load_documents() if has_index else {}
    folders = json_store.load_folders() if has_folders else {}
    db_store.save_documents(documents, dict(documents), set())
    if folders:
        db_store.save_folders(folders)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (json_store.index_path, json_store.folders_path):
        if os.path.exists(path):
            os.replace(path, f"{path}.migrated-{stamp}")
    result.update(documents=len(documents), folders=len(folders), migrated=True)
    logger.info(
        "Document registry moved into the database: %d document(s), %d folder list(s); "
        "index.json kept as index.json.migrated-%s",
        len(documents),
        len(folders),
        stamp,
    )
    return result
