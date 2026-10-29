"""Persistent chat sessions for the Matter Strategy composer.

A session is a conversation thread owned by one user; messages preserve the
full payload needed to re-render the thread (content, citations, strategy
brief, stats).

Datetime note: columns are TIMESTAMP WITHOUT TIME ZONE — always store naive
UTC (datetime.now(UTC).replace(tzinfo=None)); tz-aware writes crash asyncpg.
"""

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, String, Text

from app.models.base import Base


class ChatSessionDB(Base):
    """A persisted chat conversation (one per thread, per user)."""

    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=False)
    # Matter this session belongs to. None == owner's personal scope (visible
    # only to the owner). A shared matter makes it visible to that matter's
    # members. See app/services/matters.py.
    matter_id = Column(String(36), nullable=True, index=True)
    title = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (Index("idx_chat_session_user", "user_id"),)


class ChatMessageDB(Base):
    """A single message within a persisted chat session."""

    __tablename__ = "chat_messages"

    id = Column(String(36), primary_key=True)
    session_id = Column(
        String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String(20), nullable=False)  # 'user' | 'assistant'
    content = Column(Text, nullable=False)
    citations = Column(JSON, default=list)
    strategy = Column(JSON, nullable=True)
    stats = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (Index("idx_chat_message_session", "session_id"),)
