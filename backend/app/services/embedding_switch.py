"""Changing the embedding model at runtime (Settings → Embedding).

Vectors from two models can't be searched together, so each model has its own
collection in the vector store (``embedding_service.collection_name()``). A
switch:

1. checks the new server answers and learns its vector size (the router calls
   ``embedding_service.probe`` first),
2. saves the settings (encrypted instance secret) and applies them,
3. points the vector store at that model's collection,
4. re-embeds in the background every document the collection doesn't hold
   (``document_service.reconcile_index``), with progress for the settings page,
5. re-embeds the clause-intelligence reference clauses with the new model.

Until a document is re-embedded, vector search misses it; keyword search still
finds it. Switching back to a model used before is quick: its collection is
still there, so only documents added in between are embedded.

Only the built-in vector store (Chroma) holds several collections; Pinecone's
index has one fixed vector size, so it keeps the .env settings.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.services.embeddings import INSTANCE_KEY, embedding_service

logger = logging.getLogger(__name__)

_IDLE: dict[str, Any] = {
    "state": "idle",  # idle | running | done | failed
    "total": 0,
    "done": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_status: dict[str, Any] = dict(_IDLE)
_task: asyncio.Task | None = None
_run_id = 0


def reindex_status() -> dict[str, Any]:
    """Progress of the latest re-embedding run, for the settings page."""
    return dict(_status)


def _vector_db():
    from app.services.rag import rag_service

    return rag_service.vector_db if rag_service is not None else None


def vector_store_kind() -> str:
    db = _vector_db()
    if db is None:
        return "none"
    return "chroma" if db.supports_collection_switch else "pinecone"


def can_switch() -> bool:
    """Whether the vector store can hold one collection per embedding model."""
    db = _vector_db()
    return db is None or db.supports_collection_switch


def activate_collection() -> None:
    """Point the vector store at the active model's collection."""
    db = _vector_db()
    if db is not None and db.supports_collection_switch:
        db.use_collection(embedding_service.collection_name())


async def load_from_storage() -> None:
    """Apply the admin's saved settings. Runs at startup, before reconciliation,
    so documents are checked against the right collection."""
    from app.services.instance_settings import get_secret

    try:
        raw = await get_secret(INSTANCE_KEY)
        if raw and can_switch():
            embedding_service.apply_instance_config(json.loads(raw))
            logger.info(
                "Applied instance embedding settings: %s at %s",
                embedding_service.model,
                embedding_service.base_url,
            )
        elif raw:
            logger.warning(
                "Saved embedding settings ignored: the vector store has one fixed index "
                "(Pinecone). Embeddings use the .env settings."
            )
    except Exception as e:  # never block startup on this
        logger.warning("Could not load instance embedding settings: %s", e)
    activate_collection()


async def save_and_apply(cfg: dict[str, Any] | None) -> None:
    """Store the settings (``None`` clears them: back to .env), apply them, and
    start re-embedding into the model's collection."""
    from app.services.instance_settings import delete_secret, set_secret

    if cfg:
        await set_secret(INSTANCE_KEY, json.dumps(cfg))
    else:
        await delete_secret(INSTANCE_KEY)
    embedding_service.apply_instance_config(cfg)
    activate_collection()
    start_reindex()


def start_reindex() -> None:
    """Re-embed what the active collection is missing, in the background.

    A run already in progress is cancelled first: it belongs to settings that
    no longer apply.
    """
    global _task, _run_id
    if _task is not None and not _task.done():
        _task.cancel()
    _run_id += 1
    _status.clear()
    _status.update(_IDLE, state="running", started_at=_now())
    _task = asyncio.create_task(_run(_run_id))


async def _run(run_id: int) -> None:
    from app.services.documents import document_service

    def report(**fields: Any) -> None:
        # A cancelled run must not write over the run that replaced it.
        if run_id == _run_id:
            _status.update(fields)

    def progress(done: int, total: int, failed: int) -> None:
        report(done=done, total=total, failed=failed)

    try:
        stats = await document_service.reconcile_index(after_model_switch=True, progress=progress)
        report(failed=stats["failed"], state="done", finished_at=_now())
    except asyncio.CancelledError:
        raise
    except Exception as e:  # report it on the settings page
        logger.error("Re-embedding after an embedding switch failed: %s", e)
        report(state="failed", error=str(e), finished_at=_now())
    await _reembed_clause_references()


async def _reembed_clause_references() -> None:
    """Clause intelligence compares contract text with reference clauses
    embedded by the old model; embed them with the new one."""
    if not embedding_service.is_usable():
        return
    try:
        from app.database import AsyncSessionLocal
        from app.services.clause_intel.seeder import seed_clause_intelligence

        async with AsyncSessionLocal() as session:
            await seed_clause_intelligence(
                session,
                embed_fn=embedding_service.embed_texts,
                embedding_model=embedding_service.model,
            )
    except Exception as e:  # clause analysis falls back to regex matching
        logger.warning("Re-embedding clause references failed: %s", e)


def _now() -> str:
    return datetime.now(UTC).isoformat()
