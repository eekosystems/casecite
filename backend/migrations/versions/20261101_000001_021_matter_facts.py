"""Matters: a Facts field the case team writes, read by chat and strategy.

``matters`` gains ``facts`` (text), ``facts_updated_at`` and
``facts_updated_by``. Inspector-guarded so an install whose runtime create_all
backstop already added them upgrades cleanly.

Revision ID: 021
Revises: 020
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "021"
down_revision: str = "020"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("facts", sa.Text(), nullable=True),
    sa.Column("facts_updated_at", sa.DateTime(), nullable=True),
    sa.Column("facts_updated_by", sa.String(36), nullable=True),
)


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    present = _columns("matters")
    missing = [c for c in _COLUMNS if c.name not in present]
    if missing:
        with op.batch_alter_table("matters") as batch:
            for column in missing:
                batch.add_column(column)


def downgrade() -> None:
    present = _columns("matters")
    with op.batch_alter_table("matters") as batch:
        for column in reversed(_COLUMNS):
            if column.name in present:
                batch.drop_column(column.name)
