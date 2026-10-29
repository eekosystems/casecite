"""Drop the authority-map tables and the chat message's ``authority_map`` column.

The authority map (per-proposition case-law lookup over one uploaded document)
was removed: the Matter Strategy brief does the same quote-verified authority
lookup across the whole matter, so the standalone feature, its chat intent and
its storage go. Inspector-guarded: a database that never had the tables (the
runtime create_all backstop no longer knows them) upgrades cleanly.

Downgrade recreates the empty tables and column from the schema 006 and 014
established, so the chain stays reversible; the rows themselves are gone.

Revision ID: 018
Revises: 017
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "018"
down_revision: str = "017"
branch_labels = None
depends_on = None

_TABLES = ("authority_mappings", "authority_map_runs")


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    for table in _TABLES:
        if _has_table(table):
            op.drop_table(table)
    if _has_table("chat_messages") and _has_column("chat_messages", "authority_map"):
        with op.batch_alter_table("chat_messages") as batch:
            batch.drop_column("authority_map")


def downgrade() -> None:
    if _has_table("chat_messages") and not _has_column("chat_messages", "authority_map"):
        op.add_column("chat_messages", sa.Column("authority_map", sa.JSON(), nullable=True))
    if not _has_table("authority_map_runs"):
        op.create_table(
            "authority_map_runs",
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
    if not _has_table("authority_mappings"):
        op.create_table(
            "authority_mappings",
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
        )
