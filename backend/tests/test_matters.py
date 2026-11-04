"""Unit tests for the Matter membership & access service.

Self-contained: builds an in-memory async SQLite engine and exercises the
service directly, so it does not depend on app startup or Redis.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

import pytest
import pytest_asyncio
from app.models.base import Base
from app.models.matters import Matter, MatterMember, MatterWall  # noqa: F401  (register metadata)
from app.services import matters as svc
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Create just the matter tables (FKs to users aren't enforced by SQLite
        # by default, so we don't need the full schema for these unit tests).
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Matter.__table__, MatterMember.__table__, MatterWall.__table__],
        )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_personal_matter_created_once_and_reused(session):
    m1 = await svc.get_or_create_personal_matter(session, "user-a")
    m2 = await svc.get_or_create_personal_matter(session, "user-a")
    assert m1.id == m2.id
    assert m1.is_personal is True
    assert m1.owner_id == "user-a"
    # Owner is a member of their own personal matter.
    assert await svc.is_member(session, m1.id, "user-a") is True


async def test_member_matter_ids_is_pure_and_isolated(session):
    # Pure query: a fresh user with no memberships gets an empty set (no write,
    # no lazy personal-matter creation on the read path).
    assert await svc.get_member_matter_ids(session, "user-a") == []

    # After the user gets a personal matter and a shared one, both appear, and
    # another user's matters never leak in.
    await svc.get_or_create_personal_matter(session, "user-a")
    shared = await svc.create_matter(session, owner_id="user-a", name="Acme")
    ids_a = await svc.get_member_matter_ids(session, "user-a")
    assert len(ids_a) == 2 and shared.id in ids_a
    assert await svc.get_member_matter_ids(session, "user-b") == []
    assert shared.id not in await svc.get_member_matter_ids(session, "user-b")


async def test_share_matter_grants_access_to_member_only(session):
    matter = await svc.create_matter(session, owner_id="user-a", name="Acme v. Beta")
    # Before sharing, B cannot see it.
    assert await svc.is_member(session, matter.id, "user-b") is False
    assert matter.id not in await svc.get_member_matter_ids(session, "user-b")

    await svc.add_member(session, matter.id, "user-b", added_by="user-a")

    # After sharing, B is a member and it shows in B's accessible set.
    assert await svc.is_member(session, matter.id, "user-b") is True
    assert matter.id in await svc.get_member_matter_ids(session, "user-b")
    # A third user still has no access.
    assert await svc.is_member(session, matter.id, "user-c") is False


async def test_add_member_is_idempotent(session):
    matter = await svc.create_matter(session, owner_id="user-a", name="Shared")
    m1 = await svc.add_member(session, matter.id, "user-b", added_by="user-a")
    m2 = await svc.add_member(session, matter.id, "user-b", added_by="user-a")
    assert m1.id == m2.id


async def test_personal_matter_cannot_be_shared(session):
    personal = await svc.get_or_create_personal_matter(session, "user-a")
    with pytest.raises(svc.MatterAccessError):
        await svc.add_member(session, personal.id, "user-b", added_by="user-a")


async def test_remove_member_revokes_access_but_not_owner(session):
    matter = await svc.create_matter(session, owner_id="user-a", name="Shared")
    await svc.add_member(session, matter.id, "user-b", added_by="user-a")
    assert await svc.remove_member(session, matter.id, "user-b") is True
    assert await svc.is_member(session, matter.id, "user-b") is False

    # The owner cannot be removed.
    with pytest.raises(svc.MatterAccessError):
        await svc.remove_member(session, matter.id, "user-a")


async def test_list_matters_personal_first(session):
    await svc.get_or_create_personal_matter(session, "user-a")
    await svc.create_matter(session, owner_id="user-a", name="Second")
    matters = await svc.list_matters_for_user(session, "user-a")
    assert len(matters) == 2
    assert matters[0].is_personal is True  # personal always first
