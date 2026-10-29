import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

import aiofiles

from app.config import settings
from app.models.schemas import (
    ConnectorType,
    Document,
    DocumentStatus,
    DocumentTreeItem,
    DocumentTreeResponse,
)
from app.services.rag import rag_service
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
    """A document could not be fully deleted. Nothing was removed from the
    registry, so the caller can retry; the message is safe to show the user."""


class DocumentMoveError(RuntimeError):
    """A document's vectors could not be re-tagged for its new matter, so the
    registry was left pointing at the old matter for that document (the two
    must never disagree about who may search it). Safe to show the user."""


class DocumentService:
    def __init__(self):
        self.documents: dict[str, Document] = {}
        # Per-user list of explicit (possibly empty) knowledge-base folders.
        self.folders: dict[str, list[str]] = {}
        self._load_index()
        self._load_folders()

    def _get_index_path(self) -> str:
        return os.path.join(settings.upload_dir, "index.json")

    def _get_user_upload_dir(self, user_id: str) -> str:
        """Get the upload directory for a specific user."""
        user_dir = os.path.join(settings.upload_dir, user_id)
        os.makedirs(user_dir, exist_ok=True)
        return user_dir

    def _load_index(self):
        """Load document index from disk."""
        index_path = self._get_index_path()
        if os.path.exists(index_path):
            try:
                with open(index_path) as f:
                    data = json.load(f)
                    for doc_data in data:
                        # Migrate legacy docs without user_id
                        if "user_id" not in doc_data:
                            doc_data["user_id"] = "legacy"
                        doc = Document(**doc_data)
                        self.documents[doc.id] = doc
            except (ValueError, KeyError, OSError) as e:
                logger.error(f"Error loading document index: {e}")

    def _save_index(self):
        """Save document index to disk using atomic write (temp + rename)."""
        import tempfile

        index_path = self._get_index_path()
        try:
            data = [doc.model_dump(mode="json") for doc in self.documents.values()]
            dir_name = os.path.dirname(index_path)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=dir_name, suffix=".tmp", delete=False
            ) as f:
                json.dump(data, f, default=str)
                tmp_path = f.name
            os.replace(tmp_path, index_path)  # Atomic on POSIX
        except (ValueError, KeyError, OSError) as e:
            logger.error(f"Error saving document index: {e}")
            # Clean up temp file on failure
            try:
                if "tmp_path" in locals():
                    os.unlink(tmp_path)
            except OSError:
                pass

    def _get_user_documents(self, user_id: str) -> dict[str, Document]:
        """Get all documents belonging to a specific user."""
        return {doc_id: doc for doc_id, doc in self.documents.items() if doc.user_id == user_id}

    # ==================== Knowledge-base folders ====================

    def _get_folders_path(self) -> str:
        return os.path.join(settings.upload_dir, "folders.json")

    def _load_folders(self):
        """Load the per-user folder list from disk."""
        path = self._get_folders_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.folders = {u: list(v) for u, v in data.items()}
            except (ValueError, KeyError, OSError) as e:
                logger.error(f"Error loading folders: {e}")

    def _save_folders(self):
        """Persist the per-user folder list atomically."""
        import tempfile

        path = self._get_folders_path()
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=os.path.dirname(path), suffix=".tmp", delete=False
            ) as f:
                json.dump(self.folders, f)
                tmp_path = f.name
            os.replace(tmp_path, path)
        except (ValueError, KeyError, OSError) as e:
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
        if folder_path and folder_path not in ("/", ""):
            doc.folder_path = self._normalize_folder(folder_path)
        else:
            doc.folder_path = None
        self._save_index()
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
        self._save_index()

        try:
            # Extract text
            extraction = await self.extract(file_path, content_type)
            text = extraction.text

            if not text.strip():
                # Say why (scanned PDF without OCR, password-protected, corrupt…)
                # instead of leaving a bare FAILED row.
                doc.status = DocumentStatus.FAILED
                doc.metadata["error"] = extraction.error or "No text could be extracted."
                self._save_index()
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

        self._save_index()

        if existing_doc and doc.status == DocumentStatus.INDEXED:
            await self._retire_replaced(existing_doc, doc)
        return doc

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
        except (DocumentDeletionError, OSError, ValueError, KeyError) as e:
            # The new copy is indexed; keeping the old one too is the safe failure.
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

        Removes everything derived from it as well: its vectors, and the
        contract analyses run on it (which hold verbatim quotes of the text). Raises ``DocumentDeletionError`` — leaving the
        document in place so the delete can be retried — when the vectors or
        derived rows cannot be removed; a document must never disappear from
        the list while its content stays searchable.
        """
        doc = self.documents.get(doc_id)
        if not doc or not self._can_access(doc, user_id, accessible_matter_ids):
            return False
        await self._remove_document(doc)
        self._save_index()
        return True

    async def _remove_document(self, doc: Document) -> None:
        """Remove one document's vectors, derived rows, stored file and registry
        entry, in that order. The caller saves the index."""
        doc_id = doc.id

        # Vectors first: if these cannot be removed, stop before anything else.
        if rag_service is not None:
            try:
                removed = await rag_service.delete_document(doc_id)
            except Exception as e:  # vector stores raise their own exception trees
                logger.error(f"Vector deletion failed for document {doc_id}: {e}")
                raise DocumentDeletionError(
                    "The document's search index entries could not be removed. "
                    "The document was not deleted; try again."
                ) from e
            if removed is False:
                raise DocumentDeletionError(
                    "The document's search index entries could not be removed. "
                    "The document was not deleted; try again."
                )
        else:
            logger.warning(f"RAG service unavailable, skipping vector deletion for {doc_id}")

        await self._delete_derived_data([doc_id])

        # Delete file from the OWNER's upload dir (a member deleting a shared doc
        # still removes the owner's stored file), then legacy path.
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

        # Remove from index
        self.documents.pop(doc_id, None)

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
        for doc_id in doc_ids:
            try:
                if await self.delete_document(doc_id, user_id):
                    deleted += 1
            except (DocumentDeletionError, OSError, ValueError, KeyError) as e:
                failed += 1
                logger.error(f"Error deleting document {doc_id} during purge: {e}")

        # Remove the user's upload directory (catches any stray files).
        user_dir = os.path.join(settings.upload_dir, user_id)
        try:
            if os.path.isdir(user_dir):
                shutil.rmtree(user_dir)
        except OSError as e:
            logger.error(f"Error removing user upload dir {user_dir}: {e}")

        # Drop the user's folder list.
        if self.folders.pop(user_id, None) is not None:
            self._save_folders()

        return {"documents_deleted": deleted, "documents_failed": failed}

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
        for doc in list(user_docs.values()):
            try:
                await self._remove_document(doc)
                cleared += 1
            except (DocumentDeletionError, OSError, ValueError, KeyError) as e:
                failed += 1
                logger.error(f"Error clearing document {doc.id}: {e}")
        self._save_index()

        # Remove stray files — only once nothing that failed still needs its file.
        if not failed:
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
            "status": "success" if not failed else "partial",
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

    async def get_documents_by_ids(self, doc_ids: list[str], user_id: str) -> list[Document]:
        """Get documents by their IDs, scoped to user."""
        return [
            self.documents[doc_id]
            for doc_id in doc_ids
            if doc_id in self.documents and self.documents[doc_id].user_id == user_id
        ]


document_service = DocumentService()
