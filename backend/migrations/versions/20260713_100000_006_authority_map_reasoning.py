"""Bring the authority-map tables under migration control and add reasoning.

authority_map_runs / authority_mappings were previously created only by the
runtime create_all() backstop (their models were never imported by
migrations/env.py, so Alembic could not see them). This migration creates them
from the model metadata where missing, and adds the new
authority_mappings.reasoning JSON column (the auditable reasoning chain shown
in the citation modal) on installs whose table predates it.

Revision ID: 006
Revises: 005
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: str = "005"
branch_labels = None
depends_on = None

_TABLES = ["authority_map_runs", "authority_mappings"]


def _metadata() -> sa.MetaData:
    """The authority-map tables as they stood at this revision.

    The feature was removed in 018 (the strategy brief does the same verified
    authority lookup), so its models no longer exist to import. The schema is
    inlined here so a fresh database still upgrades through the full chain;
    018 drops both tables again.
    """
    metadata = sa.MetaData()
    sa.Table(
        "authority_map_runs",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=True, index=True),
        sa.Column("matter_id", sa.String(36), nullable=True, index=True),
        sa.Column("document_id", sa.String(64), nullable=True, index=True),
        sa.Column("document_name", sa.String(300), nullable=True),
        sa.Column("document_length_chars", sa.Integer(), nullable=False, default=0),
        sa.Column("jurisdiction", sa.String(20), nullable=True),
        sa.Column("propositions_found", sa.Integer(), nullable=False, default=0),
        sa.Column("authorities_found", sa.Integer(), nullable=False, default=0),
        sa.Column("verified_count", sa.Integer(), nullable=False, default=0),
        sa.Column("summary", sa.JSON(), nullable=False, default=dict),
        sa.Column("is_complete", sa.Integer(), nullable=False, default=0),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    sa.Table(
        "authority_mappings",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False, index=True),
        sa.Column("proposition", sa.Text(), nullable=False),
        sa.Column("doc_quote", sa.Text(), nullable=True),
        sa.Column("doc_span_start", sa.Integer(), nullable=True),
        sa.Column("doc_span_end", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("case_name", sa.String(400), nullable=True),
        sa.Column("citation", sa.String(200), nullable=True),
        sa.Column("source_ref", sa.String(64), nullable=True),
        sa.Column("source_url", sa.String(500), nullable=True),
        sa.Column("support_quote", sa.Text(), nullable=True),
        sa.Column("source_span_start", sa.Integer(), nullable=True),
        sa.Column("source_span_end", sa.Integer(), nullable=True),
        sa.Column("verified", sa.Integer(), nullable=False, default=0),
        sa.Column("relevance", sa.Float(), nullable=False, default=0.0),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Index("idx_authority_mappings_run", "run_id"),
    )
    return metadata


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    metadata = _metadata()

    # Create the tables from the models where missing (fresh migration-built DBs).
    tables = [metadata.tables[name] for name in _TABLES if name in metadata.tables]
    metadata.create_all(bind=bind, tables=tables, checkfirst=True)

    # Installs whose tables came from an older create_all lack the new column.
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("authority_mappings")}
    if "reasoning" not in columns:
        op.add_column("authority_mappings", sa.Column("reasoning", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns("authority_mappings")}
    if "reasoning" in columns:
        op.drop_column("authority_mappings", "reasoning")
    # The tables themselves are left in place (they may hold user data and
    # predate migration control).
