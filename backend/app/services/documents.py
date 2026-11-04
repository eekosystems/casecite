import asyncio
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

import aiofiles

from app.config import settings
from app.exceptions import ServiceUnavailableError
from app.models.schemas import (
    ConnectorType,
    Document,
    DocumentStatus,
    DocumentTreeItem,
    DocumentTreeResponse,
)
from app.services.rag import rag_service
from app.services.registry_store import (
    DatabaseRegistryStore,
    JsonRegistryStore,
    document_json,
    import_json_into_database,
)
from app.services.text_extraction import ExtractionResult, text_extraction_service
from app.utils import file_crypto

if TYPE_CHECKING:
    from app.services.user_keys import UserAPIKeys

logger = logging.getLogger(__name__)


def _humanize_indexing_error(e: Exception) -> str:
    """Turn a raw indexing exception into a message a user can act on.

    Raw SDK errors (JSON payload fragments like ``'code': 'invalid_api_key'``)
    end up rendered in the document list — never show those verbatim.
    """
    raw = str(e) or ""
    lowered = raw.lower()
    if "invalid_api_key" in lowered or "incorrect api key" in lowered or "401" in lowered:
        return (
            "Your embedding API key was rejected. Update the key in Settings, "
            "then re-upload this document."
        )
    if "api key" in lowered and ("missing" in lowered or "no " in lowered):
        return (
            "No embedding API key is configured. Add one in Settings, then re-upload this document."
        )
    if "rate limit" in lowered or "429" in lowered:
        return "The embedding provider rate-limited the request. Try again in a minute."
    return (raw[:200] + "…") if len(raw) > 200 else (raw or "Indexing failed")


class DocumentDeletionError(RuntimeError):
    """A document could not be fully deleted.

    The document stays in the registry with status DELETE_FAILED and
    ``metadata["deletion"]`` recording which steps completed, so the failure
    is visible and the delete can be retried (it resumes at the failed step).
    The message is safe to show the user.
    """


# Steps of a delete, in order. Each is recorded in metadata["deletion"] as it
# completes so a retry resumes where the last attempt stopped.
DELETION_STEPS = ("vectors", "derived", "file")


class DocumentMoveError(RuntimeError):
    """A document's vectors could not be re-tagged for its new matter, so the
    registry was left pointing at the old matter for that document (the two
    must never disagree about who may search it). Safe to show the user."""


class DocumentOnHoldError(RuntimeError):
    """The document, or the matter it is filed under, is on legal hold: it
    cannot be deleted through any path until the hold is lifted. Safe to
    show the user."""


class RegistryWriteError(ServiceUnavailableError):
    """The document registry (index.json) could not be written to disk.

    Raised, never logged away: a document that exists only in this process's
    memory would look uploaded, moved or deleted until the next restart and
    then silently revert. Callers roll back what they changed and the HTTP
    layer answers 503 with this message, which is safe to show the user.
    """

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(
            detail
            or "The document registry could not be written. Nothing was changed; "
            "check the data volume and try again."
        )


# How often, at most, a process asks the shared store whether another
# process changed the registry (one tiny primary-key read).
_REFRESH_CHECK_SECONDS = 0.5


class DocumentService:
    def __init__(self, store=None):
        # The registry is held in memory and persisted through a store:
        # index.json for a service built on its own (unit tests, the legacy
        # layout) or the database once the app has started
        # (use_database_store). ``_snapshot`` is each document's serialised
        # form as last persisted, so a save writes only what changed and a
        # refresh from another process never clobbers an unsaved change.
        self._store = store if store is not None else JsonRegistryStore(settings.upload_dir)
        self._documents: dict[str, Document] = {}
        self._snapshot: dict[str, str] = {}
        self._seen_version = 0
        self._last_version_check = 0.0
        # Per-user list of explicit (possibly empty) knowledge-base folders.
        self.folders: dict[str, list[str]] = {}
        # Matters on legal hold. Loaded at startup and refreshed on every hold
        # change by app/services/governance.py (and from the shared version
        # counter when several processes run).
        self.held_matter_ids: set[str] = set()
        self._load_index()
        self._load_folders()

    # ==================== Registry storage ====================

    @property
    def documents(self) -> dict[str, Document]:
        """The registry. With a shared store, picks up other processes' changes."""
        self._maybe_refresh()
        return self._documents

    @documents.setter
    def documents(self, value: dict[str, Document]) -> None:
        # A registry assigned wholesale is this process's truth and has not
        # been persisted: every entry counts as unsaved, so a refresh from the
        # shared store keeps it and the next save writes it. (Loading and
        # refreshing set _documents and _snapshot directly, not through here.)
        self._documents = value  # the caller's object, so its later mutations are seen
        self._snapshot = {}

    @documents.deleter
    def documents(self) -> None:
        # unittest.mock's patch.object restores a class property by deleting
        # the instance attribute it set: an empty registry is the right result.
        self._documents = {}
        self._snapshot = {}

    def use_database_store(self) -> dict[str, Any]:
        """Switch the registry to the database tables (the running app).

        The first time, ``index.json`` / ``folders.json`` are imported and
        kept as ``*.migrated-<timestamp>``. Returns the import summary.
        """
        json_store = (
            self._store
            if isinstance(self._store, JsonRegistryStore)
            else JsonRegistryStore(settings.upload_dir)
        )
        db_store = DatabaseRegistryStore()
        summary = import_json_into_database(json_store, db_store)
        self._store = db_store
        self._load_index(replace=True)
        self._load_folders()
        return summary

    def _maybe_refresh(self) -> None:
        """Reload what other processes changed, keeping this process's unsaved edits."""
        store = getattr(self, "_store", None)  # a bare __new__ in tests has none
        if store is None or not store.shared:
            return
        now = time.monotonic()
        if now - self._last_version_check < _REFRESH_CHECK_SECONDS:
            return
        self._last_version_check = now
        try:
            version = self._store.version()
        except Exception as e:  # a failed check keeps serving the copy we have
            logger.warning(f"Could not check the document registry version: {e}")
            return
        if version == self._seen_version:
            return
        try:
            fresh = self._store.load_documents()
        except Exception as e:
            logger.error(f"Could not refresh the document registry: {e}")
            return
        self._merge_fresh(fresh)
        self._seen_version = version

    def _merge_fresh(self, fresh: dict[str, Document]) -> None:
        """Three-way merge: a document this process has not changed since it
        last loaded or saved it takes the shared version (or goes, if deleted
        elsewhere); a document with unsaved local changes is kept."""
        merged: dict[str, Document] = {}
        snapshot: dict[str, str] = {}
        for doc_id, local in self._documents.items():
            local_json = document_json(local)
            dirty = local_json != self._snapshot.get(doc_id)
            if doc_id in fresh:
                if dirty:
                    merged[doc_id] = local
                    snapshot[doc_id] = self._snapshot.get(doc_id, "")
                else:
                    merged[doc_id] = fresh[doc_id]
                    snapshot[doc_id] = document_json(fresh[doc_id])
            elif dirty:
                merged[doc_id] = local
                snapshot[doc_id] = self._snapshot.get(doc_id, "")
            # else: deleted by another process and untouched here — dropped
        for doc_id, doc in fresh.items():
            if doc_id not in merged:
                merged[doc_id] = doc
                snapshot[doc_id] = document_json(doc)
        self._documents = merged
        self._snapshot = snapshot

    # ==================== Legal hold ====================

    def _refresh_holds_if_stale(self) -> None:
        """Reload the held matters when another process changed a hold."""
        store = getattr(self, "_store", None)
        if store is None or not store.shared:
            return
        now = time.monotonic()
        if now - getattr(self, "_holds_checked_at", 0.0) < _REFRESH_CHECK_SECONDS * 2:
            return
        self._holds_checked_at = now
        from app.services import shared_versions

        version = shared_versions.get_version(shared_versions.LEGAL_HOLDS)
        if version == getattr(self, "_holds_version", 0):
            return
        try:
            from app.database import SyncSessionLocal
            from app.models.matters import Matter

            with SyncSessionLocal() as db:
                held = {
                    str(matter_id)
                    for (matter_id,) in db.query(Matter.id).filter(Matter.legal_hold.is_(True))
                }
        except Exception as e:  # noqa: BLE001 - keep the set we have
            logger.warning(f"Could not refresh legal holds: {e}")
            return
        self.held_matter_ids = held
        self._holds_version = version

    def is_held(self, doc: Document) -> bool:
        """True when the document itself, or its matter, is on legal hold."""
        if (doc.metadata or {}).get("legal_hold"):
            return True
        self._refresh_holds_if_stale()
        return bool(doc.matter_id) and doc.matter_id in self.held_matter_ids

    def set_document_hold(
        self, doc_id: str, *, on: bool, reason: str | None, set_by: str
    ) -> Document | None:
        """Place or lift a hold on one document (the matter-level hold is
        separate). Persisted in the registry. None when the id is unknown."""
        doc = self.documents.get(doc_id)
        if doc is None:
            return None
        if on:
            doc.metadata["legal_hold"] = {
                "reason": (reason or "").strip()[:500] or None,
                "set_by": set_by,
                "set_at": datetime.now(UTC).isoformat(),
            }
        else:
            doc.metadata.pop("legal_hold", None)
        self._save_index()
        return doc

    def _refuse_if_held(self, doc: Document) -> None:
        if self.is_held(doc):
            raise DocumentOnHoldError(
                f"'{doc.filename}' is on legal hold and cannot be deleted until the hold is lifted."
            )

    def _get_index_path(self) -> str:
        return os.path.join(settings.upload_dir, "index.json")

    @property
    def registry_backend(self) -> str:
        return "database" if self._store.shared else "json"

    def _get_user_upload_dir(self, user_id: str) -> str:
        """Get the upload directory for a specific user."""
        user_dir = os.path.join(settings.upload_dir, user_id)
        os.makedirs(user_dir, exist_ok=True)
        return user_dir

    def _load_index(self, replace: bool = False):
        """Load the document registry from its store.

        An existing registry that cannot be read or parsed is fatal: starting
        with an empty registry would hide every document and the next save
        would overwrite the file, turning a transient read error into
        permanent loss. The file is left untouched for the operator.
        """
        where = self._get_index_path() if isinstance(self._store, JsonRegistryStore) else "database"
        try:
            loaded = self._store.load_documents()
        except (ValueError, KeyError, TypeError, OSError) as e:
            logger.critical(f"Document registry {where} is unreadable: {e}")
            raise RuntimeError(
                f"The document registry {where} could not be loaded ({e}). "
                "Refusing to start with an empty registry, which would hide every "
                "document and overwrite the file on the next save. Restore the file "
                "from a backup or repair it, then start again."
            ) from e
        if replace:
            self._documents = {}
        self._documents.update(loaded)
        self._snapshot = {doc_id: document_json(doc) for doc_id, doc in self._documents.items()}
        try:
            self._seen_version = self._store.version()
        except Exception as e:  # noqa: BLE001 - the next refresh retries
            logger.warning(f"Could not read the document registry version: {e}")

    def _save_index(self):
        """Persist the registry with an atomic write (temp + rename).

        Raises ``RegistryWriteError`` when the write fails. The in-memory
        registry is not touched here: each caller decides what to roll back
        so that nothing it changed is left looking committed.
        """
        current = {doc_id: document_json(doc) for doc_id, doc in self._documents.items()}
        changed = {
            doc_id: self._documents[doc_id]
            for doc_id, serialised in current.items()
            if self._snapshot.get(doc_id) != serialised
        }
        removed = set(self._snapshot) - set(current)
        try:
            self._store.save_documents(self._documents, changed, removed)
        except Exception as e:  # file system or database errors alike
            logger.error(f"Error saving document index: {e}")
            raise RegistryWriteError() from e
        self._snapshot = current
        try:
            self._seen_version = self._store.version()
        except Exception as e:  # noqa: BLE001 - the next refresh retries
            logger.warning(f"Could not read the document registry version: {e}")

    def _get_user_documents(self, user_id: str) -> dict[str, Document]:
        """Get all documents belonging to a specific user."""
        return {doc_id: doc for doc_id, doc in self.documents.items() if doc.user_id == user_id}

    # ==================== Knowledge-base folders ====================

    def _get_folders_path(self) -> str:
        return os.path.join(settings.upload_dir, "folders.json")

    def _load_folders(self):
        """Load the per-user folder lists from the store."""
        try:
            self.folders = self._store.load_folders()
        except Exception as e:  # noqa: BLE001 - folders are a convenience, never fatal
            logger.error(f"Error loading folders: {e}")

    def _save_folders(self):
        """Persist the per-user folder lists."""
        try:
            self._store.save_folders(self.folders)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error saving folders: {e}")

    @staticmethod
    def _normalize_folder(raw: str) -> str:
        """Normalize a folder path to '/Name' or '/Parent/Child' (no trailing slash)."""
        import posixpath

        if not raw or not raw.strip():
            raise ValueError("Folder name cannot be empty")
        candidate = raw.strip()
        if not candidate.startswith("/"):
            candidate = "/" + candidate
        normalized = posixpath.normpath(candidate)
        if ".." in normalized.split("/") or normalized in ("/", "."):
            raise ValueError("Invalid folder name")
        return normalized

    def list_folders(self, user_id: str) -> list[str]:
        """List the user's explicit folders (sorted)."""
        return sorted(self.folders.get(user_id, []))

    def create_folder(self, user_id: str, name: str) -> str:
        """Create (persist) an explicit folder for the user. Returns its normalized path."""
        path = self._normalize_folder(name)
        existing = self.folders.setdefault(user_id, [])
        if path not in existing:
            existing.append(path)
            self._save_folders()
        return path

    def delete_folder(self, user_id: str, path: str) -> bool:
        """Delete a folder; documents inside it move back to General (root)."""
        normalized = self._normalize_folder(path)
        existing = self.folders.get(user_id, [])
        if normalized in existing:
            existing.remove(normalized)
        # Move any documents in (or under) this folder back to General.
        moved = False
        for doc in self._get_user_documents(user_id).values():
            if doc.folder_path and (
                doc.folder_path == normalized or doc.folder_path.startswith(normalized + "/")
            ):
                doc.folder_path = None
                moved = True
        self._save_folders()
        if moved:
            self._save_index()
        return True

    def move_document(self, doc_id: str, user_id: str, folder_path: str | None) -> bool:
        """Move a document into a folder (None / '' / '/' = General/root). Owner-scoped."""
        doc = self.documents.get(doc_id)
        if not doc or doc.user_id != user_id:
            return False
        previous = doc.folder_path
        if folder_path and folder_path not in ("/", ""):
            doc.folder_path = self._normalize_folder(folder_path)
        else:
            doc.folder_path = None
        try:
            self._save_index()
        except RegistryWriteError:
            doc.folder_path = previous
            raise
        return True

    def documents_in_matter(
        self, matter_id: str, personal_owner_id: str | None = None
    ) -> list[Document]:
        """Every document filed under a matter, newest first.

        Pass ``personal_owner_id`` for a personal matter: documents in the
        personal scope carry no matter_id (the historical owner-only layout),
        so that owner's untagged documents belong to it too. The caller has
        already checked membership of the matter.
        """
        docs = [
            d
            for d in self.documents.values()
            if d.matter_id == matter_id
            or (
                personal_owner_id is not None and d.user_id == personal_owner_id and not d.matter_id
            )
        ]
        docs.sort(key=lambda d: d.created_at, reverse=True)
        return docs

    def document_ids_in_matters(
        self,
        matter_ids: list[str],
        user_id: str,
        accessible_matter_ids: set[str] | None,
        personal_matter_id: str | None = None,
    ) -> list[str]:
        """Indexed documents filed under the given matters, for the caller.

        Only matters the caller belongs to contribute; any other id adds
        nothing (and reveals nothing about whether it exists). The caller's
        personal matter also gathers their own unfiled documents, as its
        workspace does. Order: matters as given, newest document first.
        """
        allowed = set(accessible_matter_ids or ())
        if personal_matter_id:
            allowed.add(personal_matter_id)
        ids: list[str] = []
        seen: set[str] = set()
        for matter_id in dict.fromkeys(matter_ids):
            if matter_id not in allowed:
                continue
            owner = user_id if matter_id == personal_matter_id else None
            for doc in self.documents_in_matter(matter_id, owner):
                if (
                    doc.status == DocumentStatus.INDEXED
                    and doc.id not in seen
                    and self._can_access(doc, user_id, accessible_matter_ids)
                ):
                    seen.add(doc.id)
                    ids.append(doc.id)
        return ids

    async def move_documents_to_matter(
        self, docs: list[Document], target_matter_id: str | None
    ) -> list[str]:
        """File documents under another matter (None = the owner's personal scope).

        The vector chunks are re-tagged BEFORE the registry changes, document
        by document: a document whose vectors could not be updated keeps its
        old matter, the ones already moved stay moved, and DocumentMoveError
        names the document that stopped the run. Returns the ids moved.
        """
        from app.services.vectordb import get_vector_db

        vector_db = get_vector_db()
        moved: list[str] = []
        try:
            for doc in docs:
                if (doc.matter_id or None) == (target_matter_id or None):
                    continue
                if vector_db is not None and not await vector_db.update_document_matter(
                    doc.id, target_matter_id
                ):
                    raise DocumentMoveError(
                        f"'{doc.filename}' could not be moved: its search index entries "
                        "could not be updated. Nothing else changed for that document."
                    )
                doc.matter_id = target_matter_id
                moved.append(doc.id)
        finally:
            if moved:
                self._save_index()
        return moved

    async def read_file(self, file_path: str) -> bytes:
        """Read an uploaded file from disk, decrypting it if encrypted at rest."""
        async with aiofiles.open(file_path, "rb") as f:
            raw = await f.read()
        return file_crypto.decrypt_bytes(raw)

    async def extract(self, file_path: str, content_type: str) -> ExtractionResult:
        """Extract a stored file, returning the full result (text, the reason
        when there is none, and warnings about anything left unread)."""
        try:
            content = await self.read_file(file_path)
            # PDF/DOCX parsing (pdfplumber/pypdf/python-docx) is CPU-bound and
            # synchronous; run it off the event loop so a large document doesn't
            # stall every concurrent request.
            return await asyncio.to_thread(
                text_extraction_service.extract,
                file_content=content,
                content_type=content_type,
                filename=file_path,
            )
        except (ValueError, KeyError, OSError, file_crypto.FileDecryptionError) as e:
            logger.error(f"Error extracting text from {file_path}: {e}")
            raise

    async def extract_text(self, file_path: str, content_type: str) -> str:
        """Extract text from various file formats via the unified extraction service."""
        return (await self.extract(file_path, content_type)).text

    async def upload_and_index(
        self,
        file_content: bytes,
        filename: str,
        content_type: str,
        user_id: str,
        source: ConnectorType = ConnectorType.LOCAL,
        source_id: str | None = None,
        metadata: dict[str, Any] = None,
        replace_existing: bool = True,
        user_keys: Optional["UserAPIKeys"] = None,
        matter_id: str | None = None,
    ) -> Document:
        """Upload a file and index it.

        A new upload replaces an existing document only when it is the same
        document: same owner, filename, source, matter AND folder (and, for
        connector files, the same remote id). Two different files that merely
        share a name ("Engagement Letter.pdf" for two clients) are both kept.

        When it is a replacement, the new file is indexed first and the old
        document is deleted only once that succeeded, so a failed re-upload
        never costs the user the copy they already had. The replaced id is
        recorded in ``metadata["replaced_document_id"]`` and audited.

        Args:
            user_id: Owner user ID for tenant isolation.
            user_keys: Optional user-provided API keys for BYOK embeddings.
            matter_id: Matter to file the document under. None keeps it in the
                owner's personal scope (owner-only visibility). A shared matter
                makes the document visible to every member of that matter.
        """

        existing_doc = self._find_replaceable(
            filename=filename,
            source=source,
            user_id=user_id,
            matter_id=matter_id,
            source_id=source_id,
            metadata=metadata,
        )
        if existing_doc and not replace_existing:
            raise ValueError(
                f"Document '{filename}' already exists. Delete it first or use replace_existing=True."
            )

        doc_id = str(uuid.uuid4())

        # Save file in user-scoped directory
        user_upload_dir = self._get_user_upload_dir(user_id)
        file_ext = os.path.splitext(filename)[1]
        file_path = os.path.join(user_upload_dir, f"{doc_id}{file_ext}")

        # Encrypt at rest — uploaded client documents never touch disk in plaintext.
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(file_crypto.encrypt_bytes(file_content))

        # Create document record
        doc = Document(
            id=doc_id,
            user_id=user_id,
            matter_id=matter_id,
            filename=filename,
            content_type=content_type,
            size=len(file_content),
            source=source,
            source_id=source_id,
            status=DocumentStatus.PROCESSING,
            created_at=datetime.now(UTC),
            metadata=metadata or {},
        )
        self.documents[doc_id] = doc
        try:
            self._save_index()
        except RegistryWriteError:
            await self._discard_unpersisted(doc, file_path)
            raise

        try:
            # Extract text
            extraction = await self.extract(file_path, content_type)
            text = extraction.text

            if not text.strip():
                # Say why (scanned PDF without OCR, password-protected, corrupt…)
                # instead of leaving a bare FAILED row.
                doc.status = DocumentStatus.FAILED
                doc.metadata["error"] = extraction.error or "No text could be extracted."
                if extraction.needs_ocr:
                    # A scan on a server without OCR: say so in a field the UI
                    # and an operator can act on, not only in prose.
                    doc.metadata["needs_ocr"] = True
                try:
                    self._save_index()
                except RegistryWriteError:
                    await self._discard_unpersisted(doc, file_path)
                    raise
                return doc
            if extraction.warnings:
                doc.metadata["extraction_warning"] = " ".join(extraction.warnings)

            # Determine document type
            doc_type = self._classify_document(filename, text)

            # Index the document (using user's API keys for embeddings if provided)
            if rag_service is None:
                raise RuntimeError("RAG service not available. Check vector DB configuration.")
            chunk_count = await rag_service.index_document(
                document_id=doc_id,
                text=text,
                filename=filename,
                source=source.value,
                doc_type=doc_type,
                metadata=metadata,
                user_keys=user_keys,
                user_id=user_id,
                matter_id=matter_id,
                page_starts=extraction.page_starts or None,
            )
            if not chunk_count:
                # Never claim an index entry that does not exist.
                raise ValueError("Indexing produced no searchable content for this document.")

            doc.status = DocumentStatus.INDEXED
            doc.chunk_count = chunk_count
            doc.indexed_at = datetime.now(UTC)

        except Exception as e:  # any extraction/embedding/vector error
            # Never 500 the upload: record the document as failed with the reason
            # (e.g. missing embedding API key, unsupported content) so the UI can
            # show it instead of a generic server error.
            logger.error(f"Error indexing document {doc_id}: {e}", exc_info=True)
            doc.status = DocumentStatus.FAILED
            doc.metadata["error"] = _humanize_indexing_error(e)

        try:
            self._save_index()
        except RegistryWriteError:
            # The upload would look complete to this process and vanish on
            # restart, leaving its file and vectors orphaned. Undo it instead.
            await self._discard_unpersisted(doc, file_path)
            raise

        if existing_doc and doc.status == DocumentStatus.INDEXED:
            await self._retire_replaced(existing_doc, doc)
        return doc

    async def _discard_unpersisted(self, doc: Document, file_path: str) -> None:
        """Undo an upload whose registry entry could not be written: drop it
        from memory and remove its vectors and stored file, best effort."""
        self.documents.pop(doc.id, None)
        # The first write may have persisted the PROCESSING row: try once to
        # bring the file back in line so a restart does not resurrect it.
        try:
            self._save_index()
        except RegistryWriteError:
            logger.error(f"Registry still unwritable; row for {doc.id} may survive a restart")
        if rag_service is not None and doc.status == DocumentStatus.INDEXED:
            try:
                await rag_service.delete_document(doc.id)
            except Exception as e:  # vector stores raise their own exception trees
                logger.error(f"Orphaned vectors for unpersisted upload {doc.id}: {e}")
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as e:
            logger.error(f"Orphaned file for unpersisted upload {doc.id}: {e}")

    def _find_replaceable(
        self,
        *,
        filename: str,
        source: ConnectorType,
        user_id: str,
        matter_id: str | None,
        source_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> Document | None:
        """The existing document a new upload supersedes, if any.

        A file from a connector or picker is identified by its remote id: the
        same remote file re-synced (even renamed or moved) replaces its earlier
        copy, and two different remote files never replace each other however
        alike their names are.

        Without a remote id on both sides (local uploads, and documents synced
        before remote ids were recorded) everything that places a document must
        match — owner, filename, source, matter and folder. A shared filename
        alone is not identity.
        """
        remote_id = source_id or (metadata or {}).get("source_id")
        new_folder = self._derive_folder(None, metadata, source).rstrip("/")
        fallback: Document | None = None
        for doc in self.documents.values():
            if doc.user_id != user_id or doc.source != source:
                continue
            if (doc.matter_id or None) != (matter_id or None):
                continue
            doc_remote_id = doc.source_id or (doc.metadata or {}).get("source_id")
            if remote_id and doc_remote_id:
                if doc_remote_id == remote_id:
                    return doc
                continue
            if (
                fallback is None
                and doc.filename == filename
                and self._get_folder_path(doc).rstrip("/") == new_folder
            ):
                fallback = doc
        return fallback

    async def _retire_replaced(self, old: Document, new: Document) -> None:
        """Delete a document that a successful re-upload has superseded."""
        try:
            deleted = await self.delete_document(old.id, old.user_id)
        except DocumentOnHoldError:
            # The old copy is on legal hold: both stay.
            logger.info(f"Replaced document {old.id} kept: it is on legal hold")
            return
        except (DocumentDeletionError, RegistryWriteError, OSError, ValueError, KeyError) as e:
            # The new copy is indexed and persisted; keeping the old one too is
            # the safe failure. (A RegistryWriteError here means the old
            # content is gone but its row may survive a restart.)
            logger.error(f"Could not remove replaced document {old.id}: {e}")
            return
        if not deleted:
            return
        new.metadata["replaced_document_id"] = old.id
        self._save_index()
        logger.info(f"Document {old.id} replaced by re-upload {new.id}")
        try:
            from app.services.audit import AuditEventType, audit_service

            await audit_service.log_event(
                event_type=AuditEventType.DOCUMENT_DELETE,
                user_id=new.user_id,
                resource_type="document",
                resource_id=old.id,
                details={"action": "replaced_by_upload", "replaced_by": new.id},
            )
        except Exception as e:  # the replacement already happened; never fail the upload
            logger.error(f"Audit log failed for replaced document {old.id}: {e}")

    def encrypt_existing_files(self) -> dict[str, int]:
        """One-time startup migration: rewrite legacy plaintext uploads encrypted.

        Iterates the document index (never touches index.json/folders.json or
        other non-document files), so it is safe to run on every boot; already-
        encrypted files are skipped. Uses atomic replace so a crash mid-write
        can't corrupt a document.
        """
        import tempfile

        migrated = 0
        failed = 0
        for doc_id, doc in list(self.documents.items()):
            file_ext = os.path.splitext(doc.filename)[1]
            candidates = [
                os.path.join(settings.upload_dir, doc.user_id, f"{doc_id}{file_ext}"),
                os.path.join(settings.upload_dir, f"{doc_id}{file_ext}"),
            ]
            path = next((p for p in candidates if os.path.exists(p)), None)
            if path is None:
                continue
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                if file_crypto.is_encrypted(raw):
                    continue
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=os.path.dirname(path), suffix=".tmp", delete=False
                ) as tmp:
                    tmp.write(file_crypto.encrypt_bytes(raw))
                    tmp_path = tmp.name
                os.replace(tmp_path, path)
                migrated += 1
            except OSError as e:
                failed += 1
                logger.error(f"Failed to encrypt existing file {path}: {e}")
        if migrated or failed:
            logger.info(
                f"Encrypted-at-rest migration: {migrated} file(s) encrypted, {failed} failed"
            )
        return {"migrated": migrated, "failed": failed}

    async def reconcile_index(self) -> dict[str, int]:
        """Detect and repair documents whose chunks vanished from the vector store.

        The document registry (index.json) and the vector store persist
        independently: a Chroma schema upgrade backs up and RESETS the store,
        and a wiped volume empties it — while every document stays marked
        INDEXED. Users then see "N chunks indexed" on files no search can find.

        For each INDEXED document missing from the store, re-extract the stored
        encrypted file and re-index it with the owner's stored API keys. If
        repair fails, mark the document FAILED with an actionable reason —
        the registry must never claim an index entry that does not exist.
        """
        from app.services.user_keys import UserAPIKeys

        stats = {"checked": 0, "reindexed": 0, "failed": 0, "keyword_backfilled": 0}
        if rag_service is None or rag_service.vector_db is None:
            logger.warning("Index reconciliation skipped: vector DB unavailable")
            return stats

        user_keys_cache: dict[str, UserAPIKeys] = {}
        for doc in list(self.documents.values()):
            if doc.status != DocumentStatus.INDEXED:
                continue
            stats["checked"] += 1
            if await rag_service.vector_db.has_document(doc.id):
                # Indexed before the keyword index existed (or its write
                # failed): copy the stored chunks in, without re-embedding.
                try:
                    if await rag_service.vector_db.sync_lexical_document(doc.id, doc.user_id):
                        stats["keyword_backfilled"] += 1
                except Exception as e:  # keyword index is an enhancement; keep going
                    logger.warning(f"Keyword index backfill failed for {doc.id}: {e}")
                continue

            logger.warning(
                f"Document {doc.id} is marked indexed but has no chunks in the "
                "vector store — re-indexing from the stored file."
            )
            if doc.user_id not in user_keys_cache:
                user_keys_cache[doc.user_id] = UserAPIKeys.for_user(doc.user_id)
            try:
                chunk_count = await self._index_from_stored_file(doc, user_keys_cache[doc.user_id])
                stats["reindexed"] += 1
                logger.info(f"Re-indexed document {doc.id}: {chunk_count} chunks")
            except Exception as e:  # per-document; keep repairing the rest
                doc.status = DocumentStatus.FAILED
                doc.metadata["error"] = (
                    "The search index was reset (likely by an upgrade) and automatic "
                    f"re-indexing failed: {_humanize_indexing_error(e)} "
                    "Re-upload this document to restore it."
                )
                stats["failed"] += 1
                logger.error(f"Re-index failed for document {doc.id}: {e}")

        if stats["reindexed"] or stats["failed"]:
            self._save_index()
        if stats["checked"]:
            logger.info(f"Index reconciliation: {stats}")
        return stats

    def _stored_file_path(self, doc: Document) -> str | None:
        """Path of a document's stored file under its OWNER's upload dir (or the
        legacy flat layout), or None when it is missing."""
        file_ext = os.path.splitext(doc.filename)[1]
        candidates = [
            os.path.join(self._get_user_upload_dir(doc.user_id), f"{doc.id}{file_ext}"),
            os.path.join(settings.upload_dir, f"{doc.id}{file_ext}"),
        ]
        return next((p for p in candidates if os.path.exists(p)), None)

    async def _index_from_stored_file(
        self,
        doc: Document,
        user_keys: Optional["UserAPIKeys"] = None,
        replace: bool = False,
    ) -> int:
        """(Re-)index a document from its stored file, keeping where it is filed.

        The matter, folder and metadata travel with the vectors: dropping them
        on a rebuild would silently un-share a matter document. Raises when the
        file is missing, yields no text, or produces no chunks — the caller
        decides how to record the failure.

        ``replace=True`` is for a document that already has vectors: the new
        chunks are embedded first and the old ones swapped out only once that
        succeeded, so a provider failure leaves the document searchable.
        """
        if rag_service is None:
            raise RuntimeError("RAG service not available. Check vector DB configuration.")
        file_path = self._stored_file_path(doc)
        if not file_path:
            raise FileNotFoundError("the stored file is missing")

        extraction = await self.extract(file_path, doc.content_type)
        text = extraction.text
        if not text.strip():
            raise ValueError(extraction.error or "no text could be extracted")

        metadata = {k: v for k, v in (doc.metadata or {}).items() if k != "error"}
        index = rag_service.replace_document if replace else rag_service.index_document
        chunk_count = await index(
            document_id=doc.id,
            text=text,
            filename=doc.filename,
            source=doc.source.value,
            doc_type=self._classify_document(doc.filename, text),
            metadata=metadata,
            folder_path=doc.folder_path,
            user_keys=user_keys,
            user_id=doc.user_id,
            matter_id=doc.matter_id,
            page_starts=extraction.page_starts or None,
        )
        if not chunk_count:
            raise ValueError("indexing produced no searchable content")
        doc.status = DocumentStatus.INDEXED
        doc.chunk_count = chunk_count
        doc.indexed_at = datetime.now(UTC)
        doc.metadata.pop("error", None)
        return chunk_count

    async def reindex_document(
        self,
        doc_id: str,
        user_id: str,
        user_keys: Optional["UserAPIKeys"] = None,
    ) -> Document | None:
        """Rebuild one document's vectors from its stored file (owner-scoped).

        The new chunks are extracted and embedded BEFORE the old vectors are
        touched, so an unreadable file or a provider failure raises and leaves
        the document indexed and searchable exactly as it was. Only when the
        store itself fails after the old vectors were removed is the document
        marked FAILED with the reason — it is never left INDEXED with no
        vectors behind it.
        """
        doc = self.documents.get(doc_id)
        if not doc or doc.user_id != user_id:
            return None
        try:
            await self._index_from_stored_file(doc, user_keys, replace=True)
        except Exception as e:  # provider SDK / store errors share no base class
            if not getattr(e, "vectors_removed", False):
                raise
            doc.status = DocumentStatus.FAILED
            doc.chunk_count = 0
            doc.metadata["error"] = f"Re-indexing failed: {_humanize_indexing_error(e)}"
            logger.error(f"Re-index failed for document {doc.id}: {e}")
        self._save_index()
        return doc

    def _classify_document(self, filename: str, text: str) -> str:
        """Classify document type based on filename and content."""
        filename_lower = filename.lower()

        if any(x in filename_lower for x in ["contract", "agreement", "terms"]):
            return "contract"
        elif any(x in filename_lower for x in ["brief", "motion", "petition"]):
            return "legal_filing"
        elif any(x in filename_lower for x in ["memo", "memorandum"]):
            return "memo"
        elif any(x in filename_lower for x in ["policy", "procedure", "guide"]):
            return "policy"
        elif any(x in filename_lower for x in ["form", "application"]):
            return "form"
        elif any(x in filename_lower for x in ["case", "matter"]):
            return "case_file"
        else:
            return "document"

    @staticmethod
    def _can_access(doc: Document, user_id: str, accessible_matter_ids: set[str] | None) -> bool:
        """Access rule: the owner always; a matter member when the doc is filed
        under a matter the caller belongs to. ``accessible_matter_ids=None`` means
        owner-only (the historical behavior), so callers opt into sharing by
        passing the caller's member-matter set."""
        if doc.user_id == user_id:
            return True
        return bool(
            accessible_matter_ids and doc.matter_id and doc.matter_id in accessible_matter_ids
        )

    async def get_document(
        self,
        doc_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None = None,
    ) -> Document | None:
        """Get a document by ID, scoped to the caller's tenant (owner or matter)."""
        doc = self.documents.get(doc_id)
        if doc and self._can_access(doc, user_id, accessible_matter_ids):
            return doc
        return None

    async def get_document_text(
        self,
        doc_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None = None,
    ) -> tuple[str, str] | None:
        """Return (filename, extracted_text) for an accessible document, or None.

        Resolves the stored file under the OWNER's upload dir (``doc.user_id``),
        so a matter member reading a shared document finds the owner's file.
        """
        doc = await self.get_document(doc_id, user_id, accessible_matter_ids)
        if not doc:
            return None
        file_path = self._stored_file_path(doc)
        if not file_path:
            return None
        text = await self.extract_text(file_path, doc.content_type)
        return doc.filename, text

    def _matching_documents(
        self,
        user_id: str,
        source: ConnectorType | None = None,
        status: DocumentStatus | None = None,
        accessible_matter_ids: set[str] | None = None,
    ) -> list[Document]:
        """Every document the caller may see that matches the filters (unpaged)."""
        docs = [
            d
            for d in self.documents.values()
            if self._can_access(d, user_id, accessible_matter_ids)
        ]

        if source:
            docs = [d for d in docs if d.source == source]
        if status:
            docs = [d for d in docs if d.status == status]
        return docs

    async def count_documents(
        self,
        user_id: str,
        source: ConnectorType | None = None,
        status: DocumentStatus | None = None,
        accessible_matter_ids: set[str] | None = None,
    ) -> int:
        """Total documents matching the same filters ``list_documents`` applies,
        across every page — what a pager needs, as opposed to the page length."""
        return len(self._matching_documents(user_id, source, status, accessible_matter_ids))

    async def list_documents(
        self,
        user_id: str,
        source: ConnectorType | None = None,
        status: DocumentStatus | None = None,
        limit: int = 100,
        offset: int = 0,
        accessible_matter_ids: set[str] | None = None,
    ) -> list[Document]:
        """List documents scoped to the caller's tenant (owner + member matters)."""
        docs = self._matching_documents(user_id, source, status, accessible_matter_ids)

        # Sort by created_at descending
        docs.sort(key=lambda d: d.created_at, reverse=True)

        return docs[offset : offset + limit]

    async def delete_document(
        self,
        doc_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None = None,
    ) -> bool:
        """Delete a document, scoped to the caller's tenant (owner or matter member).

        A delete is a tracked operation with three steps — the search index
        entries, the analysis rows derived from the document (which quote its
        text verbatim), and the stored file. The registry entry goes only
        once all three are gone. If a step fails, ``DocumentDeletionError``
        is raised and the document stays listed with status DELETE_FAILED,
        the failed step and the error recorded in ``metadata["deletion"]``;
        deleting it again resumes at that step. A document never disappears
        from the list while any of its content remains.
        """
        doc = self.documents.get(doc_id)
        if not doc or not self._can_access(doc, user_id, accessible_matter_ids):
            return False
        await self._remove_document(doc)
        try:
            self._save_index()
        except RegistryWriteError as e:
            raise RegistryWriteError(
                "The document's content was removed, but the registry could not be "
                "written, so its entry may reappear after a restart. Delete it again "
                "if it does, and check the data volume."
            ) from e
        return True

    @staticmethod
    def deletion_progress(doc: Document) -> dict[str, bool]:
        """Which delete steps have completed for ``doc`` (all False if none)."""
        recorded = (doc.metadata or {}).get("deletion") or {}
        return {step: bool(recorded.get(step)) for step in DELETION_STEPS}

    def _record_deletion_failure(
        self, doc: Document, progress: dict[str, bool], step: str, error: str
    ) -> None:
        """Keep the document listed as DELETE_FAILED with what did and did not
        complete, persisted so the failure survives a restart."""
        previous = (doc.metadata or {}).get("deletion") or {}
        doc.status = DocumentStatus.DELETE_FAILED
        doc.metadata["deletion"] = {
            **progress,
            "failed_step": step,
            "error": error,
            "failed_at": datetime.now(UTC).isoformat(),
            "attempts": int(previous.get("attempts") or 0) + 1,
        }
        try:
            self._save_index()
        except RegistryWriteError:
            logger.error(f"Could not persist the failed-deletion state of {doc.id}")

    async def _remove_document(self, doc: Document) -> dict[str, bool]:
        """Remove one document's vectors, derived rows, stored file and registry
        entry, in that order, resuming at the first step not already recorded
        as done. The caller saves the index. Returns the completed steps."""
        doc_id = doc.id
        # A hold is checked before anything is touched, on every path (user
        # delete, purge, clear-all, replacement by re-upload, retention).
        self._refuse_if_held(doc)
        progress = self.deletion_progress(doc)

        # Vectors first: if these cannot be removed, stop before anything else.
        if not progress["vectors"]:
            if rag_service is not None:
                message = (
                    "The document's search index entries could not be removed. "
                    "The document was not deleted; try again."
                )
                try:
                    removed = await rag_service.delete_document(doc_id)
                except Exception as e:  # vector stores raise their own exception trees
                    logger.error(f"Vector deletion failed for document {doc_id}: {e}")
                    self._record_deletion_failure(doc, progress, "vectors", str(e))
                    raise DocumentDeletionError(message) from e
                if removed is False:
                    self._record_deletion_failure(
                        doc, progress, "vectors", "the vector store refused the delete"
                    )
                    raise DocumentDeletionError(message)
            else:
                logger.warning(f"RAG service unavailable, skipping vector deletion for {doc_id}")
            progress["vectors"] = True

        if not progress["derived"]:
            try:
                await self._delete_derived_data([doc_id])
            except DocumentDeletionError as e:
                self._record_deletion_failure(doc, progress, "derived", str(e))
                raise
            progress["derived"] = True

        # Delete file from the OWNER's upload dir (a member deleting a shared doc
        # still removes the owner's stored file), then legacy path. A file that
        # cannot be removed is a privacy failure, not a footnote: the document
        # stays listed as DELETE_FAILED until a retry removes it.
        if not progress["file"]:
            try:
                file_ext = os.path.splitext(doc.filename)[1]
                user_file_path = os.path.join(
                    self._get_user_upload_dir(doc.user_id), f"{doc_id}{file_ext}"
                )
                legacy_file_path = os.path.join(settings.upload_dir, f"{doc_id}{file_ext}")

                if os.path.exists(user_file_path):
                    os.remove(user_file_path)
                elif os.path.exists(legacy_file_path):
                    os.remove(legacy_file_path)
            except (ValueError, OSError) as e:
                logger.error(f"Error deleting stored file for document {doc_id}: {e}")
                self._record_deletion_failure(doc, progress, "file", str(e))
                raise DocumentDeletionError(
                    "The document's stored file could not be removed, so the document "
                    "was not deleted. Its search entries are already gone; it stays "
                    "listed as a failed deletion — retry once the data volume is "
                    "writable."
                ) from e
            progress["file"] = True

        # Remove from index
        self.documents.pop(doc_id, None)
        return progress

    def failed_deletions(self) -> list[Document]:
        """Every document, any owner, whose delete did not complete."""
        return [d for d in self.documents.values() if d.status == DocumentStatus.DELETE_FAILED]

    async def retry_failed_deletion(self, doc_id: str) -> bool:
        """Resume a failed delete regardless of owner (an operator action).
        False when the document is not a failed deletion."""
        doc = self.documents.get(doc_id)
        if not doc or doc.status != DocumentStatus.DELETE_FAILED:
            return False
        await self._remove_document(doc)
        self._save_index()
        return True

    async def _delete_derived_data(self, document_ids: list[str]) -> None:
        """Delete analysis output derived from the given documents.

        Contract-analysis runs (with their parties, obligations, deadlines,
        defined terms, clause findings and deviations) quote the source text
        verbatim and have no
        foreign key to cascade from, so they are removed explicitly — children
        before their parent runs.
        """
        if not document_ids:
            return
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import select
        from sqlalchemy.exc import SQLAlchemyError

        from app.database import get_db_context
        from app.models.clause_intel import (
            ClauseDeviationFinding,
            ClauseTagFinding,
            ContractAnalysisRun,
        )
        from app.models.contract_analysis import (
            ContractDeadline,
            ContractDefinedTerm,
            ContractObligation,
            ContractParty,
        )

        try:
            async with get_db_context() as db:
                run_ids = (
                    (
                        await db.execute(
                            select(ContractAnalysisRun.id).where(
                                ContractAnalysisRun.document_id.in_(document_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if run_ids:
                    for child in (
                        ContractParty,
                        ContractObligation,
                        ContractDeadline,
                        ContractDefinedTerm,
                        ClauseDeviationFinding,
                        ClauseTagFinding,
                    ):
                        await db.execute(sa_delete(child).where(child.analysis_id.in_(run_ids)))
                    await db.execute(
                        sa_delete(ContractAnalysisRun).where(ContractAnalysisRun.id.in_(run_ids))
                    )
        except SQLAlchemyError as e:
            logger.error(f"Derived-data deletion failed for {len(document_ids)} document(s): {e}")
            raise DocumentDeletionError(
                "The analyses derived from this document could not be removed. "
                "The document was not deleted; try again."
            ) from e

    async def purge_user_data(self, user_id: str) -> dict[str, Any]:
        """Delete ALL of a user's documents (files + vectors + index) and their
        upload directory. Used for right-to-erasure (GDPR/CCPA)."""
        import shutil

        doc_ids = [doc_id for doc_id, doc in self.documents.items() if doc.user_id == user_id]
        deleted = 0
        failed = 0
        on_hold = 0
        for doc_id in doc_ids:
            try:
                if await self.delete_document(doc_id, user_id):
                    deleted += 1
            except DocumentOnHoldError:
                on_hold += 1
            except (DocumentDeletionError, OSError, ValueError, KeyError) as e:
                failed += 1
                logger.error(f"Error deleting document {doc_id} during purge: {e}")

        # Remove the user's upload directory (catches any stray files) — only
        # once nothing that failed still needs its file. A directory that
        # cannot be removed is reported, not logged away: the caller must not
        # treat the purge as complete while the user's files remain.
        user_dir = os.path.join(settings.upload_dir, user_id)
        upload_dir_removed = False
        if not failed and not on_hold:
            try:
                if os.path.isdir(user_dir):
                    shutil.rmtree(user_dir)
                upload_dir_removed = True
            except OSError as e:
                logger.error(f"Error removing user upload dir {user_dir}: {e}")

        # Drop the user's folder list.
        if self.folders.pop(user_id, None) is not None:
            self._save_folders()

        return {
            "documents_deleted": deleted,
            "documents_failed": failed,
            "documents_on_hold": on_hold,
            "upload_dir_removed": upload_dir_removed,
        }

    def export_user_data(self, user_id: str) -> dict[str, Any]:
        """Return a JSON-serializable export of the user's documents and folders."""
        docs = [doc.model_dump(mode="json") for doc in self._get_user_documents(user_id).values()]
        return {"documents": docs, "folders": self.list_folders(user_id)}

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """Get document statistics, scoped to user."""
        user_docs = self._get_user_documents(user_id)
        total = len(user_docs)
        by_status = {}
        by_source = {}
        total_chunks = 0

        for doc in user_docs.values():
            by_status[doc.status.value] = by_status.get(doc.status.value, 0) + 1
            by_source[doc.source.value] = by_source.get(doc.source.value, 0) + 1
            total_chunks += doc.chunk_count

        return {
            "total_documents": total,
            "total_chunks": total_chunks,
            "by_status": by_status,
            "by_source": by_source,
        }

    async def clear_all(self, user_id: str) -> dict[str, Any]:
        """Clear all of a user's documents: vectors, derived analyses, files, index.

        Each document goes through the same path as a single delete. One whose
        vectors or derived rows cannot be removed stays in the registry (and is
        counted in ``failed_documents``) rather than vanishing from the list
        while its content remains searchable.
        """

        user_docs = self._get_user_documents(user_id)
        cleared = 0
        failed = 0
        on_hold = 0
        for doc in list(user_docs.values()):
            try:
                await self._remove_document(doc)
                cleared += 1
            except DocumentOnHoldError:
                on_hold += 1
            except (DocumentDeletionError, OSError, ValueError, KeyError) as e:
                failed += 1
                logger.error(f"Error clearing document {doc.id}: {e}")
        self._save_index()

        # Remove stray files — only once nothing that failed still needs its file.
        if not failed and not on_hold:
            import shutil

            user_upload_dir = self._get_user_upload_dir(user_id)
            try:
                for item in os.listdir(user_upload_dir):
                    item_path = os.path.join(user_upload_dir, item)
                    if os.path.isfile(item_path):
                        os.remove(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
            except (ValueError, OSError) as e:
                logger.error(f"Error clearing upload directory: {e}")

        return {
            "cleared_documents": cleared,
            "failed_documents": failed,
            "on_hold_documents": on_hold,
            "status": "success" if not failed and not on_hold else "partial",
        }

    def _get_folder_path(self, doc: Document) -> str:
        """Extract or derive folder path from document metadata."""
        return self._derive_folder(doc.folder_path, doc.metadata, doc.source)

    @staticmethod
    def _derive_folder(
        folder_path: str | None, metadata: dict[str, Any] | None, source: ConnectorType
    ) -> str:
        """Folder a document lives in: the explicit one, else the one its
        connector metadata describes, else the source's root."""
        import posixpath

        raw_path = None

        # Check if folder_path is explicitly set
        if folder_path:
            raw_path = folder_path
        elif metadata:
            if "folder_path" in metadata:
                raw_path = metadata["folder_path"]
            elif "parent_folder" in metadata:
                raw_path = metadata["parent_folder"]
            elif "path" in metadata:
                # Extract folder from full path
                path = metadata["path"]
                if "/" in path:
                    raw_path = "/".join(path.split("/")[:-1]) + "/"

        if not raw_path:
            return f"/{source.value}/"

        # Sanitize: normalize and reject directory traversal
        normalized = posixpath.normpath(raw_path)
        if ".." in normalized.split("/"):
            logger.warning("Blocked directory traversal in a document folder path")
            return f"/{source.value}/"

        return normalized

    async def get_document_tree(self, user_id: str) -> DocumentTreeResponse:
        """Build a tree structure of all indexed documents organized by folder, scoped to user."""
        # Build folder structure
        folders: dict[str, dict] = {}
        sources = set()

        user_docs = self._get_user_documents(user_id)
        for doc in user_docs.values():
            if doc.status != DocumentStatus.INDEXED:
                continue

            folder_path = self._get_folder_path(doc)
            sources.add(doc.source.value)

            # Create folder hierarchy
            parts = [p for p in folder_path.split("/") if p]
            current_path = ""

            for part in parts:
                parent_path = current_path
                current_path = f"{current_path}/{part}"

                if current_path not in folders:
                    folders[current_path] = {
                        "name": part,
                        "path": current_path,
                        "parent": parent_path,
                        "documents": [],
                        "subfolders": set(),
                    }

                if parent_path and parent_path in folders:
                    folders[parent_path]["subfolders"].add(current_path)

            # Add document to its folder
            if current_path in folders:
                folders[current_path]["documents"].append(doc)

        # Build tree recursively
        def build_tree_item(folder_path: str) -> DocumentTreeItem:
            folder = folders[folder_path]
            children = []
            doc_count = len(folder["documents"])

            # Add subfolders first
            for subfolder_path in sorted(folder["subfolders"]):
                child_item = build_tree_item(subfolder_path)
                children.append(child_item)
                doc_count += child_item.document_count

            # Add files
            for doc in sorted(folder["documents"], key=lambda d: d.filename):
                children.append(
                    DocumentTreeItem(
                        id=doc.id,
                        name=doc.filename,
                        type="file",
                        path=f"{folder_path}/{doc.filename}",
                        source=doc.source,
                        doc_type=doc.metadata.get("doc_type", "document")
                        if doc.metadata
                        else "document",
                        size=doc.size,
                        children=[],
                        document_count=1,
                    )
                )

            return DocumentTreeItem(
                id=folder_path,
                name=folder["name"],
                type="folder",
                path=folder_path,
                source=None,
                children=children,
                document_count=doc_count,
            )

        # Build root tree items (top-level folders)
        root_items = []
        for path, folder in folders.items():
            if not folder["parent"]:  # Root level folder
                root_items.append(build_tree_item(path))

        # Sort by name
        root_items.sort(key=lambda x: x.name)

        return DocumentTreeResponse(
            tree=root_items,
            total_documents=len(
                [d for d in user_docs.values() if d.status == DocumentStatus.INDEXED]
            ),
            total_folders=len(folders),
            sources=list(sources),
        )

    async def get_documents_by_filter(
        self,
        user_id: str,
        document_ids: list[str] | None = None,
        folder_paths: list[str] | None = None,
        sources: list[ConnectorType] | None = None,
        doc_types: list[str] | None = None,
        include_subfolders: bool = True,
        accessible_matter_ids: set[str] | None = None,
    ) -> list[str]:
        """Get document IDs matching the given filters, scoped to the caller.

        Owner-only by default; ``accessible_matter_ids`` widens the scope to
        documents filed under matters the caller belongs to.
        """
        user_docs = {
            doc_id: doc
            for doc_id, doc in self.documents.items()
            if self._can_access(doc, user_id, accessible_matter_ids)
        }
        logger.debug(f"[DocFilter Debug] Total docs for user {user_id}: {len(user_docs)}")
        logger.debug(
            f"[DocFilter Debug] Filter params: doc_ids={document_ids}, folders={folder_paths}"
        )

        matching_ids = []

        for doc in user_docs.values():
            if doc.status != DocumentStatus.INDEXED:
                continue

            # Explicitly selected documents always match. A mixed selection
            # (specific files + folders) is a UNION: the folder criteria below
            # still run; an ids-only filter matches nothing else.
            if document_ids is not None:
                if doc.id in document_ids:
                    matching_ids.append(doc.id)
                    continue
                if not folder_paths:
                    continue

            # Filter by source
            if sources is not None and doc.source not in sources:
                continue

            # Filter by doc_type
            if doc_types is not None:
                doc_type = doc.metadata.get("doc_type", "document") if doc.metadata else "document"
                if doc_type not in doc_types:
                    continue

            # Filter by folder path
            if folder_paths is not None:
                doc_folder = self._get_folder_path(doc)
                matched = False
                for folder_path in folder_paths:
                    # Normalize paths
                    norm_folder = folder_path.rstrip("/") + "/"
                    norm_doc_folder = doc_folder.rstrip("/") + "/"

                    if include_subfolders:
                        # Match if document is in this folder or any subfolder
                        if (
                            norm_doc_folder.startswith(norm_folder)
                            or norm_doc_folder == norm_folder
                        ):
                            matched = True
                            break
                    else:
                        # Exact folder match only
                        if norm_doc_folder == norm_folder:
                            matched = True
                            break

                if not matched:
                    continue

            matching_ids.append(doc.id)

        return matching_ids

    async def get_documents_by_ids(
        self,
        doc_ids: list[str],
        user_id: str,
        accessible_matter_ids: set[str] | None = None,
    ) -> list[Document]:
        """Get documents by their IDs, scoped to the caller (owner, or matter member)."""
        return [
            self.documents[doc_id]
            for doc_id in doc_ids
            if doc_id in self.documents
            and self._can_access(self.documents[doc_id], user_id, accessible_matter_ids)
        ]


document_service = DocumentService()
