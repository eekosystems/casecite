"""Matter & membership models.

A Matter is the unit of collaboration and isolation. Every user owns a personal
matter (``is_personal=True``, created on demand) that holds their private work;
additional matters can be created and shared with colleagues by adding members.

Access to a resource is "is the current user a member of the resource's matter?"
— the personal matter has exactly one member (its owner), so a single-user
install behaves identically to the previous per-user isolation model. Membership
generalizes that: an ethical wall later becomes an exclusion on a matter, and an
org becomes a grouping over matters, both without reworking this spine.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from app.models.base import Base

# owner_id / user_id are stored as plain strings (no FK to users), matching the
# convention used by the other user-scoped content tables (chat_sessions,
# contract_analysis_runs). Authenticated users can exist
# without a users row (demo and SSO-only accounts, see get_current_user), and a
# FK would reject their matters. User deletion purges these rows explicitly.

# Roles a user can hold within a single matter.
MATTER_ROLE_OWNER = "owner"
MATTER_ROLE_MEMBER = "member"


class Matter(Base):
    """A collaboration/isolation unit owning documents, chats, and analyses."""

    __tablename__ = "matters"

    id = Column(String(36), primary_key=True)
    name = Column(String(300), nullable=False)
    client_name = Column(String(300), nullable=True)
    owner_id = Column(String(36), nullable=False)
    # The auto-created private workspace for a user. Exactly one per user; never
    # shown as a shareable matter and never deletable. Preserves single-user
    # behavior: all of a solo user's data lives in their personal matter.
    is_personal = Column(Boolean, default=False, nullable=False)
    # Legal hold: while set, nothing filed under the matter (documents,
    # conversations, the matter itself) can be deleted through any path, and
    # the retention policy is suspended. See app/services/governance.py.
    legal_hold = Column(Boolean, default=False, nullable=False, server_default="0")
    legal_hold_reason = Column(String(500), nullable=True)
    legal_hold_set_by = Column(String(36), nullable=True)
    legal_hold_set_at = Column(DateTime, nullable=True)
    # Retention policy: documents and conversations filed under the matter are
    # removed by the daily retention run once older than this. None = keep.
    retention_days = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_matter_owner", "owner_id"),
        Index("idx_matter_legal_hold", "legal_hold"),
        # Enforce at most one personal matter per user at the DB layer. Only rows
        # with is_personal truthy participate (partial unique index on Postgres;
        # on SQLite this degrades to a plain index, so the service also guards).
        Index(
            "uq_matter_personal_owner",
            "owner_id",
            unique=True,
            sqlite_where=is_personal.is_(True),
            postgresql_where=is_personal.is_(True),
        ),
    )


class MatterMember(Base):
    """Membership of a user in a matter. Presence of a row grants access."""

    __tablename__ = "matter_members"

    id = Column(String(36), primary_key=True)
    matter_id = Column(String(36), ForeignKey("matters.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(36), nullable=False)
    role = Column(String(20), default=MATTER_ROLE_MEMBER, nullable=False)
    added_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("matter_id", "user_id", name="uq_matter_member"),
        Index("idx_matter_member_user", "user_id"),
        Index("idx_matter_member_matter", "matter_id"),
    )


class MatterWall(Base):
    """An ethical wall: a user who must never see this matter.

    Admin-managed. A walled user cannot be a member, their membership is
    removed when the wall goes up, and their work in the matter passes to
    its owner. See app/services/walls.py.
    """

    __tablename__ = "matter_walls"

    id = Column(String(36), primary_key=True)
    matter_id = Column(String(36), ForeignKey("matters.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(36), nullable=False)
    reason = Column(String(500), nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("matter_id", "user_id", name="uq_matter_wall"),
        Index("idx_matter_wall_user", "user_id"),
        Index("idx_matter_wall_matter", "matter_id"),
    )
