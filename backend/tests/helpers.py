"""Shared helpers for HTTP-level tests: users, auth headers, and row seeding.

Rows are seeded through a synchronous engine on the same SQLite file the app
uses. A committed synchronous write is immediately visible to the app's async
connection; writing through the async engine from a test would cross event
loops and be flakily invisible (see test_document_sharing_e2e.py).
"""

import uuid
from datetime import UTC, datetime

from app.models.enums import UserRole
from app.services.auth import User
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def make_user(prefix: str = "user", *roles: UserRole) -> User:
    """A unique in-memory user (token subject). Defaults to the attorney role."""
    uid = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return User(
        id=uid,
        email=f"{uid}@casecite.legal",
        name=uid,
        roles=list(roles) or [UserRole.ATTORNEY],
        last_login=datetime.now(UTC),
    )


def headers(user: User) -> dict:
    """Bearer headers for ``user`` with a registered session."""
    from tests.conftest import make_auth_headers

    return make_auth_headers(user)


def _sync_engine():
    from app.database import async_engine

    return create_engine(f"sqlite:///{async_engine.url.database}")


def db_add(*objects) -> None:
    """Insert ORM objects with a committed synchronous write."""
    engine = _sync_engine()
    try:
        # Keep attributes readable after the commit so a test can still use
        # the objects (ids, owners) and hand them to db_delete.
        with Session(engine, expire_on_commit=False) as session:
            session.add_all(objects)
            session.commit()
    finally:
        engine.dispose()


def db_delete(*objects) -> None:
    """Delete ORM rows by primary key (objects may be detached or already gone)."""
    engine = _sync_engine()
    try:
        with Session(engine) as session:
            for obj in objects:
                row = session.get(type(obj), obj.id)
                if row is not None:
                    session.delete(row)
            session.commit()
    finally:
        engine.dispose()


def db_count(model, *criteria) -> int:
    """Count rows of ``model`` matching ``criteria`` (fresh synchronous read)."""
    engine = _sync_engine()
    try:
        with Session(engine) as session:
            return session.query(model).filter(*criteria).count()
    finally:
        engine.dispose()


def insert_db_user(user: User) -> None:
    """Give ``user`` a users-table row (needed for FK'd tables and invites)."""
    from app.models.auth import User as DBUser

    db_add(
        DBUser(
            id=user.id,
            email=user.email,
            name=user.name,
            password_hash="x",
            roles=[r.value for r in user.roles],
            is_active=True,
            email_verified=True,
        )
    )


def make_shared_matter(owner: User, *members: User) -> str:
    """Create a shared matter owned by ``owner`` with ``members``; returns its id."""
    from app.models.matters import Matter, MatterMember

    matter_id = str(uuid.uuid4())
    rows = [
        Matter(id=matter_id, name="Acme v. Beta", owner_id=owner.id, is_personal=False),
        MatterMember(
            id=str(uuid.uuid4()),
            matter_id=matter_id,
            user_id=owner.id,
            role="owner",
            added_by=owner.id,
        ),
    ]
    rows += [
        MatterMember(
            id=str(uuid.uuid4()),
            matter_id=matter_id,
            user_id=m.id,
            role="member",
            added_by=owner.id,
        )
        for m in members
    ]
    db_add(*rows)
    return matter_id


def register_document(owner: User, *, filename: str = "contract.pdf", matter_id: str | None = None):
    """Register a document in the in-memory registry (no file, no vectors)."""
    from app.models.enums import DocumentStatus
    from app.models.schemas import ConnectorType, Document
    from app.services.documents import document_service

    doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    document_service.documents[doc_id] = Document(
        id=doc_id,
        user_id=owner.id,
        matter_id=matter_id,
        filename=filename,
        content_type="application/pdf",
        size=100,
        source=ConnectorType.LOCAL,
        status=DocumentStatus.INDEXED,
        created_at=datetime.now(UTC),
    )
    return doc_id
