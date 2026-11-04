"""Legal hold and retention for matters.

A legal hold freezes a matter: nothing filed under it — documents,
conversations, the matter itself — can be deleted by anyone through any
path (user action, a purge of a departing user's account, re-upload
replacement, retention) until the hold is lifted. Individual documents can
also be held on their own. A retention policy is the opposite: a matter can
say that documents and conversations filed under it are removed once they
are older than N days; a hold suspends it.

Holds are enforced in the services that delete (``documents.py`` keeps a
process-local set of held matter ids — correct for the supported
single-process deployment, refreshed on every change and at startup — and
``chat_sessions.py`` reads the matter), so a new delete path gets the check
by calling the same service. Every change of hold or retention, and every
retention deletion, is audited.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_sessions import ChatSessionDB
from app.models.matters import Matter

logger = logging.getLogger(__name__)

# Bounds for a retention policy, in days. Shorter than a week is almost
# certainly a typo for a legal document; longer than 30 years is "keep".
RETENTION_MIN_DAYS = 7
RETENTION_MAX_DAYS = 365 * 30


class GovernanceError(Exception):
    """A hold or retention change that is not allowed. Safe to show."""


async def held_matter_ids(session: AsyncSession) -> set[str]:
    rows = await session.execute(select(Matter.id).where(Matter.legal_hold.is_(True)))
    return {str(r) for r in rows.scalars().all()}


async def refresh_document_service_holds(session: AsyncSession) -> set[str]:
    """Push the current set of held matters into the document service, which
    enforces holds without a database session of its own."""
    from app.services.documents import document_service

    held = await held_matter_ids(session)
    document_service.held_matter_ids = held
    return held


async def set_legal_hold(
    session: AsyncSession,
    matter: Matter,
    *,
    on: bool,
    reason: str | None,
    set_by: str,
) -> Matter:
    """Place or lift a legal hold on a matter."""
    if matter.is_personal:
        raise GovernanceError("A personal matter cannot be placed on legal hold")
    if on:
        matter.legal_hold = True
        matter.legal_hold_reason = (reason or "").strip()[:500] or None
        matter.legal_hold_set_by = set_by
        matter.legal_hold_set_at = datetime.now(UTC).replace(tzinfo=None)
    else:
        matter.legal_hold = False
        matter.legal_hold_reason = None
        matter.legal_hold_set_by = None
        matter.legal_hold_set_at = None
    await session.flush()
    # The counter commits with the hold, so other processes reload the set
    # of held matters together with the change, never before it.
    from app.services import shared_versions

    await shared_versions.bump_in_session(session, shared_versions.LEGAL_HOLDS)
    await refresh_document_service_holds(session)
    return matter


async def set_retention(session: AsyncSession, matter: Matter, days: int | None) -> Matter:
    """Set (or clear, with None) how long content filed under the matter is
    kept. Documents and conversations older than this are removed by the
    daily retention run, unless the matter is on hold."""
    if matter.is_personal:
        raise GovernanceError("A retention policy cannot be set on a personal matter")
    if days is not None and not (RETENTION_MIN_DAYS <= days <= RETENTION_MAX_DAYS):
        raise GovernanceError(
            f"Retention must be between {RETENTION_MIN_DAYS} and {RETENTION_MAX_DAYS} days, "
            "or empty to keep everything"
        )
    matter.retention_days = days
    await session.flush()
    return matter


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)


async def retention_due(session: AsyncSession, matter: Matter) -> dict[str, Any]:
    """What the next retention run would remove from this matter: the ids of
    documents and conversations past the policy. Nothing when there is no
    policy or the matter is on hold."""
    from app.services.documents import document_service

    out: dict[str, Any] = {
        "retention_days": matter.retention_days,
        "legal_hold": bool(matter.legal_hold),
        "documents": [],
        "chat_sessions": [],
    }
    if not matter.retention_days or matter.legal_hold:
        return out
    cutoff = _cutoff(matter.retention_days)
    for doc in document_service.documents.values():
        if doc.matter_id != matter.id:
            continue
        created = doc.created_at.replace(tzinfo=None) if doc.created_at.tzinfo else doc.created_at
        if created < cutoff and not document_service.is_held(doc):
            out["documents"].append(doc.id)
    rows = await session.execute(
        select(ChatSessionDB.id).where(
            ChatSessionDB.matter_id == matter.id, ChatSessionDB.updated_at < cutoff
        )
    )
    out["chat_sessions"] = [str(r) for r in rows.scalars().all()]
    return out


async def run_retention(session: AsyncSession) -> dict[str, int]:
    """Apply every matter's retention policy once. Each removal goes through
    the normal delete path (so holds, tracked deletion and the registry rules
    all apply) and is audited. Failures are counted, never raised."""
    from app.services import chat_sessions as chat_session_service
    from app.services.audit import AuditEventType, audit_service
    from app.services.documents import DocumentDeletionError, document_service

    stats = {"matters": 0, "documents_deleted": 0, "chat_sessions_deleted": 0, "failed": 0}
    matters = (
        (
            await session.execute(
                select(Matter).where(
                    Matter.retention_days.is_not(None), Matter.legal_hold.is_(False)
                )
            )
        )
        .scalars()
        .all()
    )
    for matter in matters:
        due = await retention_due(session, matter)
        if not due["documents"] and not due["chat_sessions"]:
            continue
        stats["matters"] += 1
        for doc_id in due["documents"]:
            doc = document_service.documents.get(doc_id)
            if doc is None:
                continue
            try:
                if await document_service.delete_document(doc_id, doc.user_id):
                    stats["documents_deleted"] += 1
                    await audit_service.log_event(
                        event_type=AuditEventType.DATA_DELETION,
                        user_id="system",
                        resource_type="document",
                        resource_id=doc_id,
                        details={
                            "action": "retention_expired",
                            "matter_id": matter.id,
                            "retention_days": matter.retention_days,
                        },
                    )
            except (DocumentDeletionError, Exception) as e:  # keep going; counted
                stats["failed"] += 1
                logger.error(f"Retention could not delete document {doc_id}: {e}")
        for session_id in due["chat_sessions"]:
            try:
                owner = (
                    await session.execute(
                        select(ChatSessionDB.user_id).where(ChatSessionDB.id == session_id)
                    )
                ).scalar_one_or_none()
                if owner is None:
                    continue
                if await chat_session_service.delete_session(session_id, str(owner)):
                    stats["chat_sessions_deleted"] += 1
                    await audit_service.log_event(
                        event_type=AuditEventType.DATA_DELETION,
                        user_id="system",
                        resource_type="chat_session",
                        resource_id=session_id,
                        details={
                            "action": "retention_expired",
                            "matter_id": matter.id,
                            "retention_days": matter.retention_days,
                        },
                    )
            except Exception as e:  # keep going; counted
                stats["failed"] += 1
                logger.error(f"Retention could not delete chat session {session_id}: {e}")
    if stats["matters"]:
        logger.info(f"Retention run: {stats}")
    return stats
