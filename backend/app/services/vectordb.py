import logging
import os
import shutil
import sqlite3
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

try:
    from pinecone.exceptions import PineconeException
except (ImportError, ModuleNotFoundError, AttributeError):
    # pinecone may not be installed, or pinecone-client stub may raise
    class PineconeException(Exception):
        pass  # type: ignore[no-redef]


from app.config import settings
from app.services.lexical_index import LexicalIndex, UnsupportedFilter, open_lexical_index

logger = logging.getLogger(__name__)


def default_lexical_path(chroma_path: str | None = None) -> str:
    """Where the keyword index lives: the configured path, else beside the store."""
    configured = settings.lexical_index_path
    if chroma_path is None and isinstance(configured, str) and configured.strip():
        return configured
    base = os.path.dirname(os.path.abspath(chroma_path or settings.chroma_persist_dir))
    return os.path.join(base, "lexical_index.sqlite3")


class VectorStoreCorruptError(Exception):
    """The on-disk vector store cannot be opened and could not be safely reset.

    Deliberately NOT a RuntimeError/OSError: ``get_vector_db`` and the RAG
    package swallow those to let the app start without search. A store that
    failed to open *and* could not be backed up must stop startup instead, so
    an operator sees it before anything is overwritten.
    """


class VectorDBBase(ABC):
    # Keyword index mirroring this store (None when unavailable). Every write
    # path keeps it in step; see app.services.lexical_index.
    lexical: LexicalIndex | None = None

    def _mirror(self, action: str, fn) -> None:
        """Apply a write to the keyword index; a failure there never fails the store write."""
        lexical = getattr(self, "lexical", None)
        if lexical is None:
            return
        try:
            fn(lexical)
        except Exception as e:  # sqlite3 errors, disk full: search degrades to vector-only
            logger.error(f"Keyword index {action} failed ({type(e).__name__}: {e})")

    async def keyword_search(
        self,
        query: str,
        top_k: int,
        filter: dict | None,
        phrases: list[str] | None = None,
    ) -> list[dict[str, Any]] | None:
        """BM25 keyword search over every chunk in scope, or None when unavailable.

        None (no index, or a filter the index cannot express exactly) tells the
        caller to rank by vector similarity alone; an empty list means the
        index was searched and nothing matched.
        """
        lexical: LexicalIndex | None = getattr(self, "lexical", None)
        if lexical is None:
            return None
        try:
            return lexical.search(query, filter, limit=top_k, phrases=phrases or ())
        except UnsupportedFilter as e:
            logger.info(f"Keyword search skipped: {e}")
            return None
        except sqlite3.Error as e:
            logger.error(f"Keyword search failed ({e}); ranking by vector similarity only")
            return None

    async def get_embeddings(self, ids: list[str]) -> dict[str, list[float]]:
        """Stored vectors by chunk id (ids not in the store are absent). Default: none."""
        return {}

    async def sync_lexical_document(self, document_id: str, user_id: str) -> bool:
        """Copy a document's stored chunks into the keyword index if it has none.

        For stores indexed before the keyword index existed: the chunk text is
        already in the vector store, so nothing is re-extracted or re-embedded.
        Returns True when chunks were copied.
        """
        lexical: LexicalIndex | None = getattr(self, "lexical", None)
        if lexical is None or lexical.has_document(document_id):
            return False
        chunks = await self.get_document_chunks(document_id, {"user_id": user_id}, limit=10_000)
        if not chunks:
            return False
        self._mirror("backfill", lambda lx: lx.add(chunks))
        return lexical.has_document(document_id)

    @abstractmethod
    async def add_documents(self, documents: list[dict[str, Any]]) -> list[str]:
        pass

    @abstractmethod
    async def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        filter: dict | None = None,
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def delete(self, ids: list[str]) -> bool:
        pass

    async def has_document(self, document_id: str) -> bool:
        """True if any chunk for this document exists in the store.

        Used by startup reconciliation to detect registry/index divergence
        (e.g. a Chroma schema upgrade resetting the store while documents stay
        marked INDEXED). Default fails OPEN — backends without a cheap
        existence check must never trigger spurious rebuilds.
        """
        return True

    @abstractmethod
    async def get_document_chunks(
        self, document_id: str, scope: dict, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Fetch a document's stored chunks without a similarity search.

        ``scope`` is the caller's tenant filter (user_id and/or matter_id) and
        is mandatory: a document_id alone must never read another tenant's
        chunks. Returns ``{"id", "text", "metadata"}`` dicts — there is no
        similarity score because nothing was searched.
        """

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        pass

    async def update_document_matter(self, document_id: str, matter_id: str | None) -> bool:
        """Re-tag every chunk of a document with a new matter (or none).

        The matter tag is what lets a matter's members retrieve a shared
        document, so moving a document between matters must rewrite it on the
        vectors too — a stale tag would keep the old matter's members in the
        search scope. ``None`` clears the tag (owner-only). Returns False when
        the store could not be updated; the caller must then not record the
        move. Default: unsupported.
        """
        return False


# Where a chunk sits in its document (set by the structured chunker). Stored
# only when present: vector stores reject None-valued metadata.
STRUCTURE_FIELDS = (
    "section",
    "char_start",
    "char_end",
    "page_start",
    "page_end",
    "chunker",
    "doc_context",
)


def _structure_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: doc[k] for k in STRUCTURE_FIELDS if doc.get(k) not in (None, "")}


def _has_tenant_scope(filter: dict | None) -> bool:
    """True when the filter pins a tenant scope — user_id or matter_id.

    The scope may be a top-level key or nested inside an $and/$or clause: a
    shared-matter search is expressed as
    {"$or": [{"user_id": ...}, {"matter_id": {"$in": [...]}}]}, and a
    document-scoped search as {"$and": [{"user_id": ...}, {"document_id": ...}]}.
    Either shape keeps the query tenant-isolated. Checked recursively so the
    guard cannot be defeated by nesting.
    """
    if not filter:
        return False
    if filter.get("user_id") or filter.get("matter_id"):
        return True
    for key in ("$and", "$or"):
        for cond in filter.get(key) or []:
            if isinstance(cond, dict) and _has_tenant_scope(cond):
                return True
    return False


class ChromaDB(VectorDBBase):
    def __init__(self, path: str | None = None, lexical_path: str | None = None):
        """Open (or create) the store at ``path``; default ``settings.chroma_persist_dir``.

        The keyword index opens at ``lexical_path`` (default: beside the store).
        """
        chroma_path = path or settings.chroma_persist_dir
        store_was_reset = False
        # Ensure the directory exists and is writable
        os.makedirs(chroma_path, exist_ok=True)
        if not os.access(chroma_path, os.W_OK):
            raise RuntimeError(
                f"ChromaDB directory '{chroma_path}' is not writable. "
                f"Check volume mount permissions."
            )
        try:
            self._open(chroma_path)
        except (
            ValueError,
            KeyError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            chromadb.errors.ChromaError,
        ) as e:
            # Schema mismatch (e.g. "no such column: collections.topic") or a
            # damaged store. Resetting is only acceptable once a complete copy
            # of the existing data exists: back up, VERIFY the backup, and only
            # then clear. If the backup cannot be made or verified, stop —
            # deleting a firm's index on the strength of one failed open is
            # never the right default.
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            backup_path = f"{chroma_path}_corrupt_{timestamp}"
            try:
                shutil.copytree(chroma_path, backup_path)
                self._verify_backup(chroma_path, backup_path)
            except (OSError, shutil.Error) as backup_err:
                logger.critical(
                    f"ChromaDB failed to open ({e}) and its data could not be backed up "
                    f"to {backup_path} ({backup_err}). Nothing was deleted. Free disk "
                    f"space / fix permissions on {chroma_path} and restart."
                )
                raise VectorStoreCorruptError(
                    f"ChromaDB at '{chroma_path}' failed to open ({e}) and could not be "
                    f"backed up ({backup_err}); refusing to reset it. Nothing was deleted."
                ) from e
            logger.error(
                f"ChromaDB init failed ({e}). Existing data was copied to {backup_path} "
                "and verified; re-initializing an empty store. Documents will be "
                "re-indexed by startup reconciliation."
            )
            # Remove the unopenable files so PersistentClient can start fresh
            for f in os.listdir(chroma_path):
                fpath = os.path.join(chroma_path, f)
                try:
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                    elif os.path.isdir(fpath):
                        shutil.rmtree(fpath)
                except OSError as rm_err:
                    logger.error(f"Cannot remove {fpath}: {rm_err}")
            self._open(chroma_path)
            store_was_reset = True
            logger.info("ChromaDB re-initialized successfully with fresh database.")

        self.lexical = open_lexical_index(lexical_path or default_lexical_path(path))
        if store_was_reset:
            # The vector store starts empty; keyword entries for documents
            # that then fail to re-index must not stay searchable.
            self._mirror("reset", lambda lx: lx.clear())

    def _open(self, chroma_path: str) -> None:
        self.client = chromadb.PersistentClient(
            path=chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name="legal_documents", metadata={"hnsw:space": "cosine"}
        )

    @staticmethod
    def _dir_manifest(path: str) -> dict[str, int]:
        """Relative file path -> size for every file under ``path``."""
        manifest: dict[str, int] = {}
        for root, _dirs, files in os.walk(path):
            for name in files:
                full = os.path.join(root, name)
                manifest[os.path.relpath(full, path)] = os.path.getsize(full)
        return manifest

    @classmethod
    def _verify_backup(cls, source: str, backup: str) -> None:
        """Raise OSError unless ``backup`` holds every file of ``source`` at full size."""
        src = cls._dir_manifest(source)
        dst = cls._dir_manifest(backup)
        bad = [name for name, size in src.items() if dst.get(name) != size]
        if bad:
            raise OSError(
                f"backup verification failed: {len(bad)} file(s) missing or truncated "
                f"(e.g. {bad[0]})"
            )

    async def add_documents(self, documents: list[dict[str, Any]]) -> list[str]:
        """Add documents with embeddings to the vector store."""
        for doc in documents:
            if not doc.get("user_id"):
                raise ValueError(
                    "user_id is required for all documents to enforce tenant isolation"
                )

        ids = []
        embeddings = []
        metadatas = []
        texts = []

        for doc in documents:
            doc_id = doc.get("id", str(uuid.uuid4()))
            ids.append(doc_id)
            embeddings.append(doc["embedding"])
            meta = {
                "source": doc.get("source", "unknown"),
                "document_id": doc.get("document_id", ""),
                "chunk_index": doc.get("chunk_index", 0),
                "filename": doc.get("filename", ""),
                "doc_type": doc.get("doc_type", ""),
                "created_at": doc.get("created_at", ""),
                "folder_path": doc.get("folder_path", ""),
                "user_id": doc.get("user_id", ""),
            }
            # Chroma rejects None-valued metadata, so only include matter_id when
            # the chunk is tagged to a matter. Untagged chunks stay owner-scoped.
            if doc.get("matter_id"):
                meta["matter_id"] = doc["matter_id"]
            meta.update(_structure_metadata(doc))
            metadatas.append(meta)
            texts.append(doc.get("text", ""))

        self.collection.add(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=texts)
        self._mirror(
            "add",
            lambda lx: lx.add(
                {"id": i, "text": t, "metadata": m}
                for i, t, m in zip(ids, texts, metadatas, strict=True)
            ),
        )

        return ids

    async def has_document(self, document_id: str) -> bool:
        """True if any chunk for this document exists in the collection."""
        try:
            result = self.collection.get(where={"document_id": document_id}, limit=1)
            return bool(result and result.get("ids"))
        except (
            ValueError,
            KeyError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            chromadb.errors.ChromaError,
        ) as e:
            # Fail open: a flaky check must never trigger a rebuild.
            logger.warning(f"has_document check failed for {document_id}: {e}")
            return True

    async def get_document_chunks(
        self, document_id: str, scope: dict, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Fetch a document's chunks directly (no similarity search)."""
        if not _has_tenant_scope(scope):
            raise ValueError("a user_id or matter_id scope is required to fetch document chunks")
        chunks = self.collection.get(
            where={"$and": [{"document_id": document_id}, scope]},
            limit=limit,
            include=["documents", "metadatas"],
        )
        ids = (chunks or {}).get("ids") or []
        documents = chunks.get("documents") or []
        metadatas = chunks.get("metadatas") or []
        return [
            {
                "id": chunk_id,
                "text": documents[i] if i < len(documents) else "",
                "metadata": (metadatas[i] if i < len(metadatas) else None) or {},
            }
            for i, chunk_id in enumerate(ids)
        ]

    async def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        filter: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Search for similar documents."""
        if not _has_tenant_scope(filter):
            raise ValueError(
                "a user_id or matter_id filter is required for search to enforce tenant isolation"
            )
        where = filter if filter else None

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        documents = []
        if results and results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                # Convert distance to similarity (cosine distance to similarity)
                distance = results["distances"][0][i] if results["distances"] else 0
                similarity = 1 - distance  # For cosine distance

                documents.append(
                    {
                        "id": doc_id,
                        "text": results["documents"][0][i] if results["documents"] else "",
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "similarity": similarity,
                        "rank": i + 1,
                    }
                )

        return documents

    async def delete(self, ids: list[str]) -> bool:
        """Delete documents by ID."""
        try:
            self.collection.delete(ids=ids)
        except (chromadb.errors.ChromaError, ValueError):
            return False
        self._mirror("delete", lambda lx: lx.delete_ids(ids))
        return True

    async def delete_by_document(self, document_id: str) -> bool:
        """Delete all chunks for a document."""
        try:
            self.collection.delete(where={"document_id": document_id})
        except (chromadb.errors.ChromaError, ValueError):
            return False
        self._mirror("delete", lambda lx: lx.delete_document(document_id))
        return True

    async def get_embeddings(self, ids: list[str]) -> dict[str, list[float]]:
        """Stored vectors for ``ids`` (ids not in the store are omitted)."""
        if not ids:
            return {}
        try:
            got = self.collection.get(ids=list(ids), include=["embeddings"])
        except (chromadb.errors.ChromaError, ValueError, sqlite3.Error) as e:
            logger.warning(f"Could not read stored embeddings: {e}")
            return {}
        vectors = got.get("embeddings")
        if vectors is None:
            return {}
        return {
            chunk_id: [float(x) for x in vectors[i]]
            for i, chunk_id in enumerate(got.get("ids") or [])
            if i < len(vectors)
        }

    async def update_document_matter(self, document_id: str, matter_id: str | None) -> bool:
        """Rewrite the matter tag on every chunk of a document (see base class)."""
        try:
            existing = self.collection.get(
                where={"document_id": document_id}, include=["metadatas"]
            )
            ids = list((existing or {}).get("ids") or [])
            if not ids:
                return True  # nothing indexed — nothing to re-tag
            metadatas: list[Mapping[str, Any]] = []
            for meta in existing.get("metadatas") or [{}] * len(ids):
                updated: dict[str, Any] = dict(meta or {})
                # Chroma rejects None values, and an update merges keys rather
                # than dropping them, so "none" is the empty string: it matches
                # no matter id, leaving the chunk owner-scoped.
                updated["matter_id"] = matter_id or ""
                metadatas.append(updated)
            self.collection.update(ids=ids, metadatas=metadatas)
            self._mirror("re-tag", lambda lx: lx.update_matter(document_id, matter_id))
            return True
        except (
            chromadb.errors.ChromaError,
            ValueError,
            KeyError,
            RuntimeError,
            OSError,
            sqlite3.Error,
        ) as e:
            logger.error(f"Could not re-tag chunks of document {document_id}: {e}")
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Get collection statistics."""
        count = self.collection.count()

        # Try to get embedding dimensions from stored data
        stored_dimensions = None
        if count > 0:
            try:
                # Get one item to check embedding dimensions
                sample = self.collection.get(limit=1, include=["embeddings"])
                if sample and sample.get("embeddings") and len(sample["embeddings"]) > 0:
                    stored_dimensions = len(sample["embeddings"][0])
            except (chromadb.errors.ChromaError, ValueError):  # nosec B110 - Dimension detection is optional
                pass

        return {
            "total_chunks": count,
            "total_documents": count,  # Alias for frontend compatibility
            "total_vectors": count,  # Alias for frontend compatibility
            "stored_embedding_dimensions": stored_dimensions,
            "collection_name": "legal_documents",
            "db_type": "chroma",
        }


# Pinecone caps metadata at 40 KB per vector. Chunk text lives in metadata (it
# is the only copy Pinecone holds), so it is stored in full up to a bound that
# leaves room for the other fields; a 512-token chunk is ~2-3 KB.
PINECONE_TEXT_BYTES = 30_000


def _fit_utf8(text: str, max_bytes: int) -> str:
    """``text`` cut to at most ``max_bytes`` of UTF-8 without splitting a character."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


class PineconeDB(VectorDBBase):
    """Pinecone backend — EXPERIMENTAL.

    The code path is exercised only by unit tests with a mocked client; it has
    not been validated against a live Pinecone index. ChromaDB is the
    supported store.
    """

    def __init__(self):
        from pinecone import Pinecone, ServerlessSpec

        if not settings.pinecone_api_key:
            raise ValueError("Pinecone API key not configured")
        logger.warning(
            "VECTOR_DB=pinecone is EXPERIMENTAL: this backend is not covered by live "
            "integration tests. Chunk text is stored in Pinecone metadata (it leaves "
            "your infrastructure). ChromaDB is the supported vector store."
        )

        self.pc = Pinecone(api_key=settings.pinecone_api_key)

        # Create index if it doesn't exist. Current Pinecone SDKs require an
        # explicit spec; use serverless (pod-based indexes are legacy). The
        # dimension follows the active embedding model so the two stay in sync.
        if settings.pinecone_index_name not in self.pc.list_indexes().names():
            from app.services.embeddings import embedding_service

            self.pc.create_index(
                name=settings.pinecone_index_name,
                dimension=embedding_service.dimensions,
                metric="cosine",
                spec=ServerlessSpec(cloud="aws", region=settings.pinecone_environment),
            )

        self.index = self.pc.Index(settings.pinecone_index_name)
        self._dimension: int | None = None
        self.lexical = open_lexical_index(default_lexical_path())

    def _probe_vector(self) -> list[float]:
        """A constant non-zero vector for filter-only lookups.

        Pinecone has no "get by metadata filter" call, so a filtered query
        with an arbitrary vector is the way to list matching chunks. The
        scores it returns are meaningless and are discarded.
        """
        if self._dimension is None:
            try:
                self._dimension = int(self.index.describe_index_stats().dimension)
            except (PineconeException, AttributeError, TypeError, ValueError, ConnectionError):
                from app.services.embeddings import embedding_service

                self._dimension = embedding_service.dimensions
        return [1.0] * self._dimension

    @staticmethod
    def _match_to_chunk(match) -> dict[str, Any]:
        metadata = dict(match.metadata or {})
        return {"id": match.id, "text": metadata.get("text", ""), "metadata": metadata}

    async def has_document(self, document_id: str) -> bool:
        """True if any chunk for this document exists in the index."""
        try:
            results = self.index.query(
                vector=self._probe_vector(),
                top_k=1,
                filter={"document_id": document_id},
                include_metadata=False,
            )
            return bool(results.matches)
        except (PineconeException, ConnectionError, ValueError, AttributeError) as e:
            # Fail open: a flaky check must never trigger a rebuild.
            logger.warning(f"has_document check failed for {document_id}: {e}")
            return True

    async def get_document_chunks(
        self, document_id: str, scope: dict, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Fetch a document's chunks directly (no similarity search)."""
        if not _has_tenant_scope(scope):
            raise ValueError("a user_id or matter_id scope is required to fetch document chunks")
        results = self.index.query(
            vector=self._probe_vector(),
            # Pinecone caps top_k at 10,000 with metadata.
            top_k=max(1, min(limit, 10_000)),
            filter={"$and": [{"document_id": document_id}, scope]},
            include_metadata=True,
        )
        chunks = [self._match_to_chunk(m) for m in results.matches]
        chunks.sort(key=lambda c: c["metadata"].get("chunk_index", 0))
        return chunks

    async def add_documents(self, documents: list[dict[str, Any]]) -> list[str]:
        """Add documents with embeddings to Pinecone."""
        for doc in documents:
            if not doc.get("user_id"):
                raise ValueError(
                    "user_id is required for all documents to enforce tenant isolation"
                )

        vectors = []
        ids = []

        for doc in documents:
            doc_id = doc.get("id", str(uuid.uuid4()))
            ids.append(doc_id)
            pine_meta = {
                "text": _fit_utf8(doc.get("text", ""), PINECONE_TEXT_BYTES),
                "source": doc.get("source", "unknown"),
                "document_id": doc.get("document_id", ""),
                "chunk_index": doc.get("chunk_index", 0),
                "filename": doc.get("filename", ""),
                "doc_type": doc.get("doc_type", ""),
                "created_at": doc.get("created_at", ""),
                "folder_path": doc.get("folder_path", ""),
                "user_id": doc.get("user_id", ""),
            }
            if doc.get("matter_id"):
                pine_meta["matter_id"] = doc["matter_id"]
            pine_meta.update(_structure_metadata(doc))
            vectors.append({"id": doc_id, "values": doc["embedding"], "metadata": pine_meta})

        # Batch upsert
        batch_size = 100
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i : i + batch_size]
            self.index.upsert(vectors=batch)

        # The keyword index keeps the full chunk text (Pinecone's copy is
        # bounded by its metadata limit).
        self._mirror(
            "add",
            lambda lx: lx.add(
                {
                    "id": v["id"],
                    "text": doc.get("text", ""),
                    "metadata": {k: val for k, val in v["metadata"].items() if k != "text"},
                }
                for v, doc in zip(vectors, documents, strict=True)
            ),
        )
        return ids

    async def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        filter: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Search Pinecone for similar documents."""
        if not _has_tenant_scope(filter):
            raise ValueError(
                "a user_id or matter_id filter is required for search to enforce tenant isolation"
            )
        results = self.index.query(
            vector=query_embedding, top_k=top_k, filter=filter, include_metadata=True
        )

        documents = []
        for i, match in enumerate(results.matches):
            documents.append(
                {
                    "id": match.id,
                    "text": match.metadata.get("text", "") if match.metadata else "",
                    "metadata": match.metadata or {},
                    "similarity": match.score,
                    "rank": i + 1,
                }
            )

        return documents

    async def delete(self, ids: list[str]) -> bool:
        """Delete documents by ID."""
        try:
            self.index.delete(ids=ids)
        except (PineconeException, ConnectionError, ValueError):
            return False
        self._mirror("delete", lambda lx: lx.delete_ids(ids))
        return True

    async def delete_by_document(self, document_id: str) -> bool:
        """Delete all chunks for a document."""
        try:
            self.index.delete(filter={"document_id": document_id})
        except (PineconeException, ConnectionError, ValueError):
            return False
        self._mirror("delete", lambda lx: lx.delete_document(document_id))
        return True

    async def get_embeddings(self, ids: list[str]) -> dict[str, list[float]]:
        """Stored vectors for ``ids`` (ids not in the index are omitted)."""
        if not ids:
            return {}
        try:
            fetched = self.index.fetch(ids=list(ids))
        except (PineconeException, ConnectionError, ValueError, AttributeError) as e:
            logger.warning(f"Could not read stored embeddings: {e}")
            return {}
        vectors = getattr(fetched, "vectors", None) or {}
        out: dict[str, list[float]] = {}
        for chunk_id, vector in vectors.items():
            values = getattr(vector, "values", None)
            if values is None and isinstance(vector, dict):
                values = vector.get("values")
            if values:
                out[chunk_id] = [float(x) for x in values]
        return out

    async def update_document_matter(self, document_id: str, matter_id: str | None) -> bool:
        """Rewrite the matter tag on every chunk of a document (see base class)."""
        try:
            results = self.index.query(
                vector=self._probe_vector(),
                top_k=10_000,  # Pinecone's ceiling; a single document is far smaller
                filter={"document_id": document_id},
                include_metadata=False,
            )
            for match in results.matches:
                # set_metadata merges, so the tag is overwritten in place; the
                # empty string matches no matter id (owner-only), as in Chroma.
                self.index.update(id=match.id, set_metadata={"matter_id": matter_id or ""})
            self._mirror("re-tag", lambda lx: lx.update_matter(document_id, matter_id))
            return True
        except (PineconeException, ConnectionError, ValueError, AttributeError) as e:
            logger.error(f"Could not re-tag chunks of document {document_id}: {e}")
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Get index statistics."""
        stats = self.index.describe_index_stats()
        return {
            "total_chunks": stats.total_vector_count,
            "total_documents": stats.total_vector_count,  # Alias for frontend compatibility
            "total_vectors": stats.total_vector_count,  # Alias for frontend compatibility
            "index_name": settings.pinecone_index_name,
            "db_type": "pinecone",
        }


_db_instance: VectorDBBase | None = None


def get_vector_db() -> VectorDBBase | None:
    """Factory function to get the configured vector database (singleton).

    Returns None if initialization fails, allowing the app to start
    without vector search capabilities. The one exception is
    VectorStoreCorruptError (an existing store that will not open and could
    not be backed up): that propagates and stops startup.
    """
    global _db_instance
    if _db_instance is None:
        try:
            if settings.vector_db == "pinecone":
                _db_instance = PineconeDB()
            else:
                _db_instance = ChromaDB()
        except (ValueError, KeyError, RuntimeError, OSError) as e:
            logger.error(
                f"Vector DB initialization failed: {e}. "
                f"RAG search will be unavailable until this is resolved."
            )
            return None
    return _db_instance
