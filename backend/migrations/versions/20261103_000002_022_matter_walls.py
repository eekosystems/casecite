"""Ethical walls: users denied a matter.

Creates ``matter_walls``: one row per (matter, user) that must never see the
matter, with the reason and who set it. Purely additive.

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


def _metadata():
    from app.models.base import Base
    from app.models.matters import MatterWall  # noqa: F401

    return Base.metadata


def upgrade() -> None:
    bind = op.get_bind()
    table = _metadata().tables.get("matter_walls")
    if table is not None:
        _metadata().create_all(bind=bind, tables=[table], checkfirst=True)


def downgrade() -> None:
    if "matter_walls" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("matter_walls")
