"""The row-level security bypass is explicit, named and used where it must be.

The fence itself is PostgreSQL only (tests/test_rls_postgres.py). What any
database can check is the contract around it: a bypass needs a reason and
is logged, an admin-gated endpoint lifts the fence for its request, and the
retention task — which runs with no request and therefore no user — lifts
it on purpose rather than seeing nothing.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from app import rls


def test_a_bypass_needs_a_reason():
    with pytest.raises(ValueError), rls.bypassed(""):
        pass
    with pytest.raises(ValueError):
        rls.grant_bypass("")


def test_a_bypass_is_logged_and_undone(caplog):
    assert rls.rls_bypass.get() is False
    with caplog.at_level(logging.INFO, logger="app.rls"), rls.bypassed("nightly cleanup"):
        assert rls.rls_bypass.get() is True
    assert rls.rls_bypass.get() is False
    assert any("nightly cleanup" in r.getMessage() for r in caplog.records)


def test_a_session_with_no_user_is_not_a_bypass():
    with rls.scoped(None):
        assert rls.request_user_id.get() is None
        assert rls.rls_bypass.get() is False


@pytest.mark.asyncio
async def test_the_retention_task_runs_bypassed():
    """The task has no request behind it; it must say that it sees every
    matter, or (since migration 027) it would see none."""
    from app.main import _retention_loop

    seen: dict[str, object] = {}

    async def fake_run_retention(db):
        seen["bypass"] = rls.rls_bypass.get()
        seen["user"] = rls.request_user_id.get()
        return {"matters": 0}

    async def stop_after_first_pass(_seconds):
        raise asyncio.CancelledError

    with (
        patch("app.services.governance.run_retention", new=fake_run_retention),
        patch("app.main.asyncio.sleep", new=stop_after_first_pass),
        pytest.raises(asyncio.CancelledError),
    ):
        await _retention_loop()
    assert seen == {"bypass": True, "user": None}
    assert rls.rls_bypass.get() is False  # not leaked past the task body


@pytest.mark.asyncio
async def test_an_admin_gated_endpoint_lifts_the_fence(caplog):
    """require_permission("admin.*") grants the bypass for the request and
    logs it; an ordinary permission does not."""
    from types import SimpleNamespace

    from app.services import permissions as perm

    user = SimpleNamespace(user_id="u-admin", roles=["admin"])
    admin_checker = perm.require_permission("admin.users").dependency
    plain_checker = perm.require_permission("contracts.use").dependency
    with (
        patch.object(
            perm,
            "effective_permissions",
            new=AsyncMock(return_value={"admin.users", "contracts.use"}),
        ),
        caplog.at_level(logging.INFO, logger="app.rls"),
        rls.scoped("u-admin"),
    ):
        await plain_checker(current_user=user)
        assert rls.rls_bypass.get() is False
        await admin_checker(current_user=user)
        assert rls.rls_bypass.get() is True
    assert any("admin.users" in r.getMessage() for r in caplog.records)
