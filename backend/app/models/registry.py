"""The document registry in the database, and shared version counters.

The registry (which documents exist, who owns them, where they are filed,
their status) lived in ``data/uploads/index.json``, read once per process and
rewritten whole on every change — one of the stores that kept CaseCite to a
single process. These tables hold it instead: one row per document with the
document's full JSON, one row per user's folder list, and a version counter
every process checks so a change made by one worker is seen by the others
(see ``app/services/registry_store.py``).

The rows are not row-level-secured: the document service reads the whole
registry into memory and enforces access itself, as it always has, and every
query here runs unscoped at startup or from service code.
"""

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Index, Integer, String

from app.models.base import Base


class DocumentRegistryRow(Base):
    """One registered document, as the ``Document`` schema serialises it."""

    __tablename__ = "document_registry"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    matter_id = Column(String(36), nullable=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_document_registry_user", "user_id"),
        Index("idx_document_registry_matter", "matter_id"),
    )


class DocumentFolderRow(Base):
    """A user's explicit (possibly empty) knowledge-base folders."""

    __tablename__ = "document_folders"

    user_id = Column(String(64), primary_key=True)
    folders = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SharedVersion(Base):
    """A counter bumped on every change to a shared store, so each process
    can tell cheaply whether its copy is stale."""

    __tablename__ = "shared_versions"

    name = Column(String(64), primary_key=True)
    version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
