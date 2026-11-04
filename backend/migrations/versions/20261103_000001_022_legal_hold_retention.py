"""Legal hold and retention policy on matters.

Adds to ``matters``: ``legal_hold`` (nothing filed under the matter can be
deleted while set), who set it, when and why, and ``retention_days`` (content
older than this is removed by the daily retention run unless the matter is on
hold). Purely additive; existing matters are not on hold and keep everything.

Revision ID: 022
Revises: 021
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: str = "021"
branch_labels = None
depends_on = None

_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("legal_hold_reason", sa.String(500), nullable=True),
    sa.Column("legal_hold_set_by", sa.String(36), nullable=True),
    sa.Column("legal_hold_set_at", sa.DateTime(), nullable=True),
    sa.Column("retention_days", sa.Integer(), nullable=True),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    if "matters" not in _inspector().get_table_names():
        return
    present = _columns("matters")
    missing = [c for c in _COLUMNS if c.name not in present]
    if missing:
        with op.batch_alter_table("matters") as batch:
            for column in missing:
                batch.add_column(column)
    op.create_index("idx_matter_legal_hold", "matters", ["legal_hold"], if_not_exists=True)


def downgrade() -> None:
    if "matters" not in _inspector().get_table_names():
        return
    op.drop_index("idx_matter_legal_hold", table_name="matters", if_exists=True)
    present = _columns("matters")
    with op.batch_alter_table("matters") as batch:
        for column in _COLUMNS:
            if column.name in present:
                batch.drop_column(column.name)
