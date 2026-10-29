"""Persistent Chat Session Service.

Business logic for storing and retrieving Matter Strategy conversations.
Every query filters by user_id — a session is only ever visible to its owner
(tenant isolation). All database interactions are encapsulated here; the
router delegates to this module.

Datetime note: the Postgres columns are TIMESTAMP WITHOUT TIME ZONE, so all
writes use naive UTC (datetime.now(UTC).replace(tzinfo=None)) — tz-aware
datetimes crash asyncpg.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select

from app.database import get_db_context
from app.models.db_models import ChatMessageDB, ChatSessionDB


def _access_clause(user_id: str, accessible_matter_ids: set[str] | None):
    """SQL predicate: the session's owner, or a member of its matter.

    ``accessible_matter_ids=None`` means owner-only (the historical behavior),
    so callers opt into matter sharing by passing the caller's member set.
    """
    if accessible_matter_ids:
        return or_(
            ChatSessionDB.user_id == user_id,
            ChatSessionDB.matter_id.in_(list(accessible_matter_ids)),
        )
    return ChatSessionDB.user_id == user_id


logger = logging.getLogger(__name__)

DEFAULT_SESSION_LIMIT = 50


def _utcnow_naive() -> datetime:
    """Naive UTC timestamp for TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _serialize_session(row: ChatSessionDB, message_count: int = 0) -> dict:
    """Convert a ChatSessionDB row to a summary dict."""
    return {
        "id": row.id,
        "title": row.title,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "message_count": message_count,
        "user_id": row.user_id,
        "matter_id": row.matter_id,
    }


def _serialize_message(row: ChatMessageDB) -> dict:
    """Convert a ChatMessageDB row to a dict."""
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "citations": row.citations or [],
        "strategy": row.strategy,
        "stats": row.stats,
        "created_at": row.created_at,
    }


async def _get_owned_session(
    db, session_id: str, user_id: str, accessible_matter_ids: set[str] | None = None
) -> ChatSessionDB | None:
    """Fetch a session the caller may access (owner or matter member)."""
    stmt = select(ChatSessionDB).where(
        ChatSessionDB.id == session_id,
        _access_clause(user_id, accessible_matter_ids),
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def create_session(user_id: str, title: str, matter_id: str | None = None) -> dict:
    """Create a new chat session for the user and return its summary."""
    now = _utcnow_naive()
    row = ChatSessionDB(
        id=str(uuid.uuid4()),
        user_id=user_id,
        matter_id=matter_id,
        title=(title or "Untitled conversation").strip()[:255] or "Untitled conversation",
        created_at=now,
        updated_at=now,
    )
    async with get_db_context() as db:
        db.add(row)
        await db.flush()
        return _serialize_session(row)


async def append_message(
    session_id: str,
    user_id: str,
    role: str,
    content: str,
    citations: list[dict] | None = None,
    strategy: dict | None = None,
    stats: dict | None = None,
    accessible_matter_ids: set[str] | None = None,
) -> dict:
    """Append a message to a session the caller may access (owner or matter member).

    Raises LookupError if the session does not exist or is outside the caller's
    tenant — callers must not be able to write into foreign sessions.
    """
    async with get_db_context() as db:
        session = await _get_owned_session(db, session_id, user_id, accessible_matter_ids)
        if session is None:
            raise LookupError(f"Chat session {session_id} not found for this user")

        now = _utcnow_naive()
        row = ChatMessageDB(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            content=content,
            citations=citations or [],
            strategy=strategy,
            stats=stats,
            created_at=now,
        )
        db.add(row)
        session.updated_at = now
        await db.flush()
        return _serialize_message(row)


async def list_sessions(
    user_id: str,
    limit: int = DEFAULT_SESSION_LIMIT,
    accessible_matter_ids: set[str] | None = None,
) -> list[dict]:
    """Return the caller's sessions (own + shared matters), newest first."""
    async with get_db_context() as db:
        stmt = (
            select(ChatSessionDB, func.count(ChatMessageDB.id))
            .outerjoin(ChatMessageDB, ChatMessageDB.session_id == ChatSessionDB.id)
            .where(_access_clause(user_id, accessible_matter_ids))
            .group_by(ChatSessionDB.id)
            .order_by(ChatSessionDB.updated_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return [_serialize_session(row, count) for row, count in result.all()]


async def list_sessions_for_matter(
    matter_id: str,
    personal_owner_id: str | None = None,
    limit: int = DEFAULT_SESSION_LIMIT,
) -> list[dict]:
    """Every session filed under one matter, newest first (the matter workspace).

    The caller has already been checked as a member of the matter. For a
    personal matter pass its owner: sessions that predate matters carry no
    matter_id and belong to the personal scope of their owner.
    """
    if personal_owner_id:
        where = or_(
            ChatSessionDB.matter_id == matter_id,
            (ChatSessionDB.user_id == personal_owner_id) & ChatSessionDB.matter_id.is_(None),
        )
    else:
        where = ChatSessionDB.matter_id == matter_id
    async with get_db_context() as db:
        stmt = (
            select(ChatSessionDB, func.count(ChatMessageDB.id))
            .outerjoin(ChatMessageDB, ChatMessageDB.session_id == ChatSessionDB.id)
            .where(where)
            .group_by(ChatSessionDB.id)
            .order_by(ChatSessionDB.updated_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return [_serialize_session(row, count) for row, count in result.all()]


async def get_session_with_messages(
    session_id: str, user_id: str, accessible_matter_ids: set[str] | None = None
) -> dict | None:
    """Return the full thread for a session the caller may access, or None."""
    async with get_db_context() as db:
        session = await _get_owned_session(db, session_id, user_id, accessible_matter_ids)
        if session is None:
            return None

        stmt = (
            select(ChatMessageDB)
            .where(ChatMessageDB.session_id == session_id)
            .order_by(ChatMessageDB.created_at.asc(), ChatMessageDB.id.asc())
        )
        result = await db.execute(stmt)
        messages = result.scalars().all()

        return {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "messages": [_serialize_message(m) for m in messages],
        }


async def delete_session(
    session_id: str, user_id: str, accessible_matter_ids: set[str] | None = None
) -> bool:
    """Delete a session (and its messages) the caller may access. False if not."""
    async with get_db_context() as db:
        session = await _get_owned_session(db, session_id, user_id, accessible_matter_ids)
        if session is None:
            return False

        # Explicit message delete: FK cascade covers Postgres/SQLite-with-pragma,
        # but delete deterministically rather than relying on driver behaviour.
        await db.execute(delete(ChatMessageDB).where(ChatMessageDB.session_id == session_id))
        await db.delete(session)
        await db.flush()
        return True
