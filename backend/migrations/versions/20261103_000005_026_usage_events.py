"""Per-user AI usage events.

One row per provider call with the reported token counts and whether the
instance's own key carried it; the basis of the admin usage view and of the
daily quota for users on server-level keys. Purely additive.

Revision ID: 026
Revises: 025
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: str = "025"
branch_labels = None
depends_on = None


def _metadata():
    from app.models.base import Base
    from app.models.usage import UsageEvent  # noqa: F401

    return Base.metadata


def upgrade() -> None:
    table = _metadata().tables.get("usage_events")
    if table is not None:
        _metadata().create_all(bind=op.get_bind(), tables=[table], checkfirst=True)


def downgrade() -> None:
    if "usage_events" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("usage_events")
