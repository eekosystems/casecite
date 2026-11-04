"""The document registry in the database.

Creates ``document_registry`` (one row per document, the document's JSON),
``document_folders`` (a user's knowledge-base folder list) and
``shared_versions`` (counters every process checks so a change made by one
worker is seen by the others). The running app imports ``index.json`` into
these tables the first time it starts with them present, and keeps the file
as ``index.json.migrated-<timestamp>``. Purely additive.

Revision ID: 024
Revises: 023
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "024"
down_revision: str = "023"
branch_labels = None
depends_on = None

_TABLES = ("document_registry", "document_folders", "shared_versions")


def _metadata():
    from app.models.base import Base
    from app.models.registry import (  # noqa: F401
        DocumentFolderRow,
        DocumentRegistryRow,
        SharedVersion,
    )

    return Base.metadata


def upgrade() -> None:
    bind = op.get_bind()
    metadata = _metadata()
    for name in _TABLES:
        table = metadata.tables.get(name)
        if table is not None:
            metadata.create_all(bind=bind, tables=[table], checkfirst=True)


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name in _TABLES:
        if name in existing:
            op.drop_table(name)
