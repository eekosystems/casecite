"""Workflows: versions, archiving, and the version a run executed.

``workflows`` gains ``version`` (incremented whenever the steps change) and
``archived_at``; ``workflow_runs`` gains ``workflow_version``; the new
``workflow_versions`` table holds a snapshot of the steps at every version.
Inspector-guarded so an install whose runtime create_all backstop already
built them upgrades cleanly.

Revision ID: 020
Revises: 019
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "020"
down_revision: str = "019"
branch_labels = None
depends_on = None

_WORKFLOW_COLUMNS = (
    sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("archived_at", sa.DateTime(), nullable=True),
)
_RUN_COLUMNS = (sa.Column("workflow_version", sa.Integer(), nullable=False, server_default="1"),)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _columns(table: str) -> set[str]:
    return {c["name"] for c in _inspector().get_columns(table)}


def _add_missing(table: str, columns: tuple[sa.Column, ...]) -> None:
    if not _has_table(table):
        return
    present = _columns(table)
    missing = [c for c in columns if c.name not in present]
    if missing:
        with op.batch_alter_table(table) as batch:
            for column in missing:
                batch.add_column(column)


def upgrade() -> None:
    _add_missing("workflows", _WORKFLOW_COLUMNS)
    _add_missing("workflow_runs", _RUN_COLUMNS)

    if not _has_table("workflow_versions"):
        op.create_table(
            "workflow_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workflow_id",
                sa.String(36),
                sa.ForeignKey("workflows.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(300), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("steps", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("change_note", sa.Text(), nullable=True),
        )
        op.create_index("ix_workflow_versions_workflow_id", "workflow_versions", ["workflow_id"])
        op.create_index(
            "idx_workflow_versions_workflow_version",
            "workflow_versions",
            ["workflow_id", "version"],
            unique=True,
        )


def downgrade() -> None:
    if _has_table("workflow_versions"):
        op.drop_table("workflow_versions")
    for table, columns in (("workflow_runs", _RUN_COLUMNS), ("workflows", _WORKFLOW_COLUMNS)):
        if _has_table(table):
            present = _columns(table)
            with op.batch_alter_table(table) as batch:
                for column in columns:
                    if column.name in present:
                        batch.drop_column(column.name)
