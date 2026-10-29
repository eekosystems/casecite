"""Tabular review: the human review layer, saved schemas and run history.

``review_cells`` gains the reviewer's columns (an override value, a review
status, a note, who reviewed and when, and a bounded change history);
``review_schemas`` stores reusable field schemas; ``review_runs`` records every
extraction run with its scope and counts. All inspector-guarded so an install
whose runtime create_all backstop already built them upgrades cleanly.

Revision ID: 019
Revises: 018
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "019"
down_revision: str = "018"
branch_labels = None
depends_on = None

_CELL_COLUMNS = (
    sa.Column("override_value", sa.Text(), nullable=True),
    sa.Column("review_status", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("note", sa.Text(), nullable=True),
    sa.Column("reviewed_by", sa.String(64), nullable=True),
    sa.Column("reviewed_at", sa.DateTime(), nullable=True),
    sa.Column("history", sa.JSON(), nullable=True),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _columns(table: str) -> set[str]:
    return {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    if _has_table("review_cells"):
        present = _columns("review_cells")
        missing = [c for c in _CELL_COLUMNS if c.name not in present]
        if missing:
            with op.batch_alter_table("review_cells") as batch:
                for column in missing:
                    batch.add_column(column)

    if not _has_table("review_schemas"):
        op.create_table(
            "review_schemas",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("owner_id", sa.String(64), nullable=False),
            sa.Column("matter_id", sa.String(36), nullable=True),
            sa.Column("name", sa.String(300), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("fields", sa.JSON(), nullable=False),
            sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_review_schemas_owner_id", "review_schemas", ["owner_id"])
        op.create_index("ix_review_schemas_matter_id", "review_schemas", ["matter_id"])

    if not _has_table("review_runs"):
        op.create_table(
            "review_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "table_id",
                sa.String(36),
                sa.ForeignKey("review_tables.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("runner_id", sa.String(64), nullable=False),
            sa.Column("scope", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("job_id", sa.String(36), nullable=True),
            sa.Column("counts", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_review_runs_table_id", "review_runs", ["table_id"])


def downgrade() -> None:
    if _has_table("review_runs"):
        op.drop_table("review_runs")
    if _has_table("review_schemas"):
        op.drop_table("review_schemas")
    if _has_table("review_cells"):
        present = _columns("review_cells")
        with op.batch_alter_table("review_cells") as batch:
            for column in _CELL_COLUMNS:
                if column.name in present:
                    batch.drop_column(column.name)
