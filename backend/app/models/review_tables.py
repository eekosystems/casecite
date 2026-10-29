"""Tabular review models — one schema, many contracts, one cited cell per field.

A ``ReviewTable`` is a saved review: who owns it, which matter it belongs to,
the fields the reviewer wants extracted (a user-defined schema) and the
documents in scope. A ``ReviewCell`` is one (document, field) answer carrying
the value, the verbatim quote that supports it and where that quote sits in
the document text, so every exported figure can be traced back to its source.

These tables use SQLAlchemy 2.0 ``Mapped[]`` columns, so attribute reads are
typed as plain Python values (the older ``Column`` tables are not; see the
mypy ratchet notes).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Cell statuses. ``extracted`` means the quote was found verbatim in the
# document; ``unverified`` means the model answered but its quote could not be
# located, so the value is shown de-emphasised and never as sourced.
CELL_EXTRACTED = "extracted"
CELL_UNVERIFIED = "unverified"
CELL_NOT_FOUND = "not_found"
CELL_ERROR = "error"
CELL_STATUSES = (CELL_EXTRACTED, CELL_UNVERIFIED, CELL_NOT_FOUND, CELL_ERROR)

# Human review of a cell. The model proposes a value; a reviewer may override
# it, approve it, or flag it for a colleague. A re-run never touches an
# override or an approval unless the model's own value changed.
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_FLAGGED = "flagged"
REVIEW_STATUSES = (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_FLAGGED)
# History entries kept per cell (oldest dropped first).
HISTORY_LIMIT = 20

# One extraction run of a table, for the run history.
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"


class ReviewTable(Base):
    """A saved tabular review: schema + document scope, owned like other work product."""

    __tablename__ = "review_tables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Owner and matter follow the other user-scoped tables (plain strings, no FK).
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    matter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Schema: list of {key, name, description, type}. ``key`` is the stable slug
    # cells are stored under; ``type`` is one of the ReviewFieldType values.
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # Explicit document scope (ids), in review order.
    document_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (Index("idx_review_tables_owner_matter", "owner_id", "matter_id"),)


class ReviewCell(Base):
    """One extracted field for one document, with its verifying quote."""

    __tablename__ = "review_cells"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    field_key: Mapped[str] = mapped_column(String(80), nullable=False)

    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    span_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CELL_NOT_FOUND)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Length of the document text the span refers to; a re-indexed document
    # that changes length invalidates the offsets.
    text_len: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Human review layer. ``override_value`` replaces ``value`` when set; the
    # effective value is the override if present, else the model's value.
    override_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=REVIEW_PENDING, server_default=REVIEW_PENDING
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The last HISTORY_LIMIT changes: [{at, by, kind, value}], kind in
    # model | override | approve | flag | clear.
    history: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("table_id", "document_id", "field_key", name="uq_review_cell"),
        Index("idx_review_cells_table_doc", "table_id", "document_id"),
    )

    @property
    def effective_value(self) -> str | None:
        return self.override_value if self.override_value is not None else self.value


class ReviewSchema(Base):
    """A saved field schema a reviewer reuses across tables. Built-in schemas
    are defined in code, not stored here."""

    __tablename__ = "review_schemas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # None = personal (owner only); a matter id shares it with the matter's members.
    matter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ReviewRun(Base):
    """One extraction run of a table: who ran it, over what, and what it found."""

    __tablename__ = "review_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # {document_ids: [...] | null, field_keys: [...] | null}; null = the whole table.
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=RUN_RUNNING)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # {extracted, unverified, not_found, errors, changed, cells, documents, fields}
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
