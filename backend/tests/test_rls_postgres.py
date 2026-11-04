"""Row-level security is a real second fence (PostgreSQL only).

Runs only when the suite is pointed at PostgreSQL (CI's Postgres job; locally
``DATABASE_URL=postgresql+asyncpg://...``). Applies the same DDL migrations
024 and 027 install, then — connected as a non-superuser role, because
PostgreSQL superusers bypass row-level security entirely — proves that a
deliberately unscoped query returns only the caller's rows, that matter
membership grants and an ethical wall revokes, that a session with no user
sees (and can write) nothing while an explicit bypass sees everything, and
that a row cannot be written under another user's id.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from app import rls
from app.database import DATABASE_URL, IS_POSTGRES, AsyncSessionLocal, async_engine
from app.models.chat_sessions import ChatMessageDB, ChatSessionDB
from app.models.matters import Matter, MatterMember, MatterWall
from app.models.workflows import Workflow
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="row-level security is PostgreSQL only")

# The role the scoped queries run as. The suite's own role (POSTGRES_USER in
# CI's service container) is a superuser, which PostgreSQL exempts from RLS.
_ROLE = "casecite_rls_test"
_ROLE_PASSWORD = "rls-test-only"


def _scoped_url() -> str:
    """DATABASE_URL with the credentials swapped for the non-superuser role."""
    scheme, rest = DATABASE_URL.split("://", 1)
    host_part = rest.split("@", 1)[1] if "@" in rest else rest
    return f"{scheme}://{_ROLE}:{_ROLE_PASSWORD}@{host_part}"


@pytest_asyncio.fixture(autouse=True)
async def _rls_installed():
    """Idempotent: the schema, the DDL migration 024 installs, and a
    non-superuser role with full table access for the scoped queries."""
    from app.models.db_models import Base

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in rls.rls_enable_statements():
            await conn.execute(text(statement))
        exists = (
            await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": _ROLE})
        ).first()
        if exists is None:
            await conn.execute(
                text(
                    f"CREATE ROLE {_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{_ROLE_PASSWORD}'"
                )
            )
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {_ROLE}"))
        await conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {_ROLE}"))
        await conn.execute(text(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {_ROLE}"))
        await conn.execute(text(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {_ROLE}"))
    yield
    # asyncpg connections belong to the event loop that opened them, and each
    # test gets its own loop: drop the pool so the next test starts fresh.
    await async_engine.dispose()


@pytest_asyncio.fixture
async def scoped_sessions():
    """A session factory connected as the non-superuser role. The scope
    listener is registered on the ORM Session class, so it applies here."""
    engine = create_async_engine(_scoped_url())
    rls.install_rls_listener(engine)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_the_scoped_role_enforces_and_the_suite_role_does_not(scoped_sessions):
    """Documents why the scoped queries use their own role: PostgreSQL
    exempts superusers from RLS, and the app reports that rather than
    pretending otherwise."""
    async with scoped_sessions() as db:
        status = await rls.rls_status(db)
    assert status == {"enforced": True, "role": _ROLE, "superuser": False, "bypass_rls": False}
    async with AsyncSessionLocal() as db:
        status = await rls.rls_status(db)
    if status["superuser"] or status["bypass_rls"]:
        assert status["enforced"] is False


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture
async def rows():
    """Two users, a shared matter owned by u1 with u2 as member, and content:
    u1's private chat, u1's chat in the matter, u2's private chat, a workflow
    of u1 in the matter."""
    tag = uuid.uuid4().hex[:8]
    u1, u2, u3 = f"rls-u1-{tag}", f"rls-u2-{tag}", f"rls-u3-{tag}"
    matter_id = f"rls-m-{tag}"
    ids = {
        "u1": u1,
        "u2": u2,
        "u3": u3,
        "matter": matter_id,
        "u1_private": f"c1-{tag}",
        "u1_matter": f"c2-{tag}",
        "u2_private": f"c3-{tag}",
        "workflow": f"w-{tag}",
    }
    with rls.bypassed("test fixture: seed rows"):
        async with AsyncSessionLocal() as db:
            db.add_all(
                [
                    Matter(id=matter_id, name="RLS", owner_id=u1, is_personal=False),
                    MatterMember(id=f"mm1-{tag}", matter_id=matter_id, user_id=u1, role="owner"),
                    MatterMember(id=f"mm2-{tag}", matter_id=matter_id, user_id=u2, role="member"),
                    ChatSessionDB(
                        id=ids["u1_private"],
                        user_id=u1,
                        matter_id=None,
                        title="private",
                        created_at=_now(),
                        updated_at=_now(),
                    ),
                    ChatSessionDB(
                        id=ids["u1_matter"],
                        user_id=u1,
                        matter_id=matter_id,
                        title="shared",
                        created_at=_now(),
                        updated_at=_now(),
                    ),
                    ChatSessionDB(
                        id=ids["u2_private"],
                        user_id=u2,
                        matter_id=None,
                        title="theirs",
                        created_at=_now(),
                        updated_at=_now(),
                    ),
                    Workflow(
                        id=ids["workflow"], owner_id=u1, matter_id=matter_id, name="wf", steps=[]
                    ),
                ]
            )
            await db.flush()
            db.add(
                ChatMessageDB(
                    id=f"msg-{tag}",
                    session_id=ids["u1_matter"],
                    role="user",
                    content="hello",
                    created_at=_now(),
                )
            )
            await db.commit()
    yield ids
    with rls.bypassed("test fixture: remove rows"):
        async with AsyncSessionLocal() as db:
            like = {"p": f"%-{tag}"}
            await db.execute(text("DELETE FROM chat_messages WHERE session_id LIKE :p"), like)
            await db.execute(text("DELETE FROM chat_sessions WHERE id LIKE :p"), like)
            await db.execute(text("DELETE FROM workflows WHERE id LIKE :p"), like)
            await db.execute(
                text("DELETE FROM matter_walls WHERE matter_id = :m"), {"m": matter_id}
            )
            await db.execute(
                text("DELETE FROM matter_members WHERE matter_id = :m"), {"m": matter_id}
            )
            await db.execute(text("DELETE FROM matters WHERE id = :m"), {"m": matter_id})
            await db.commit()


async def _chat_ids(sessions, tag_ids) -> set[str]:
    """A query with NO user filter at all — the bug the fence is for."""
    async with sessions() as db:
        found = (await db.execute(select(ChatSessionDB.id))).scalars().all()
    return {i for i in found if i in tag_ids.values()}


async def test_an_unscoped_query_returns_only_the_callers_rows(rows, scoped_sessions):
    with rls.scoped(rows["u1"]):
        assert await _chat_ids(scoped_sessions, rows) == {rows["u1_private"], rows["u1_matter"]}
    with rls.scoped(rows["u2"]):
        # Own private chat, plus the shared matter's chat through membership.
        assert await _chat_ids(scoped_sessions, rows) == {rows["u2_private"], rows["u1_matter"]}
    with rls.scoped(rows["u3"]):
        assert await _chat_ids(scoped_sessions, rows) == set()


async def test_child_rows_follow_their_parent(rows, scoped_sessions):
    async def messages() -> int:
        async with scoped_sessions() as db:
            result = await db.execute(
                select(ChatMessageDB.id).where(ChatMessageDB.session_id == rows["u1_matter"])
            )
            return len(result.scalars().all())

    with rls.scoped(rows["u2"]):
        assert await messages() == 1  # member of the matter
    with rls.scoped(rows["u3"]):
        assert await messages() == 0


async def test_an_ethical_wall_revokes_what_membership_granted(rows, scoped_sessions):
    with rls.bypassed("test: place a wall"):
        async with AsyncSessionLocal() as db:
            db.add(
                MatterWall(
                    id=f"wall-{rows['matter']}",
                    matter_id=rows["matter"],
                    user_id=rows["u2"],
                    created_at=_now(),
                )
            )
            await db.commit()
    with rls.scoped(rows["u2"]):
        assert await _chat_ids(scoped_sessions, rows) == {rows["u2_private"]}
        async with scoped_sessions() as db:
            found = await db.execute(select(Workflow.id).where(Workflow.id == rows["workflow"]))
            assert found.first() is None


async def test_a_session_with_no_user_sees_nothing(rows, scoped_sessions):
    """Forgetting to say who is asking is not a way to see everything: a
    transaction with neither a user nor a bypass matches no policy."""
    from sqlalchemy.exc import DBAPIError

    with rls.scoped(None):
        assert await _chat_ids(scoped_sessions, rows) == set()
        async with scoped_sessions() as db:
            found = await db.execute(select(Workflow.id).where(Workflow.id == rows["workflow"]))
            assert found.first() is None
            result = await db.execute(
                select(ChatMessageDB.id).where(ChatMessageDB.session_id == rows["u1_matter"])
            )
            assert result.scalars().all() == []
        # Nor a way to write: WITH CHECK refuses the row too.
        async with scoped_sessions() as db:
            db.add(
                ChatSessionDB(
                    id=f"nouser-{rows['matter']}",
                    user_id=rows["u1"],
                    matter_id=None,
                    title="no user",
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()


async def test_an_explicit_bypass_sees_everything(rows, scoped_sessions):
    everything = {rows["u1_private"], rows["u1_matter"], rows["u2_private"]}
    with rls.scoped(rows["u3"]), rls.bypassed("test: admin oversight"):
        assert await _chat_ids(scoped_sessions, rows) == everything
    with rls.scoped(None), rls.bypassed("test: system task"):
        assert await _chat_ids(scoped_sessions, rows) == everything


async def test_the_migration_session_setting_lifts_the_fence(rows, scoped_sessions):
    """Alembic sets ``app.rls_bypass`` for its session (migrations/env.py);
    a data migration run as the app role then sees the content tables."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_scoped_url())
    try:
        async with engine.connect() as conn:
            before = (await conn.execute(select(ChatSessionDB.id))).scalars().all()
            assert rows["u2_private"] not in before
            await conn.execute(text("SET app.rls_bypass = 'on'"))
            after = (await conn.execute(select(ChatSessionDB.id))).scalars().all()
            assert rows["u2_private"] in after
    finally:
        await engine.dispose()


async def test_a_row_cannot_be_written_under_another_users_id(rows, scoped_sessions):
    from sqlalchemy.exc import DBAPIError

    forged = f"forged-{rows['matter']}"
    with rls.scoped(rows["u3"]):
        async with scoped_sessions() as db:
            db.add(
                ChatSessionDB(
                    id=forged,
                    user_id=rows["u1"],
                    matter_id=None,
                    title="forged",
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            with pytest.raises(DBAPIError):
                await db.commit()
            await db.rollback()
    with rls.bypassed("test: check nothing was written"):
        async with AsyncSessionLocal() as db:
            found = await db.execute(select(ChatSessionDB.id).where(ChatSessionDB.id == forged))
            assert found.first() is None


async def test_the_scope_does_not_leak_between_transactions(rows, scoped_sessions):
    """SET LOCAL dies with the transaction: a pooled connection used by u1
    must not keep u1's scope for the next session, and a bypass must not
    outlive its transaction either."""
    with rls.scoped(rows["u1"]):
        assert rows["u2_private"] not in await _chat_ids(scoped_sessions, rows)
    with rls.scoped(rows["u1"]), rls.bypassed("test: one bypassed transaction"):
        assert rows["u2_private"] in await _chat_ids(scoped_sessions, rows)
    with rls.scoped(rows["u1"]):
        assert rows["u2_private"] not in await _chat_ids(scoped_sessions, rows)
