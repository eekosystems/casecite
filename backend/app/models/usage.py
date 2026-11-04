"""Per-user AI usage: one row per provider call, with the token counts the
provider reported and whether it ran on the instance's own key.

Lets an administrator see what each user spends, and lets a per-user daily
quota apply to users who rely on the server-level provider keys (BYOK users
pay for their own calls). Rows are written best-effort after each call and
never block it.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String

from app.models.base import Base


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    provider = Column(String(32), nullable=False)
    model = Column(String(120), nullable=True)
    # What the call was for: "answer" (the chat reply), "utility" (routing,
    # grounding, screening), "extract", "draft", ... as the caller named it.
    purpose = Column(String(40), nullable=False, default="llm")
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    # True when the instance's own provider key carried the call.
    server_key = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("idx_usage_events_user_created", "user_id", "created_at"),
        Index("idx_usage_events_created", "created_at"),
    )
