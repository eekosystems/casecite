"""
HTTP-level tests for the admin user-management router (/admin/users).

Covers who may call it (admin.users permission), the invite → forced password
change flow, role changes, deactivation, deletion, force sign-out and the
admin MFA reset — including that each one actually cuts the target's access.

DB seeding runs on the TestClient's own event loop (``client.portal``).
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from app.database import AsyncSessionLocal
from app.middleware.security import account_lockout, session_manager
from app.models.db_models import User as DBUser
from app.services.auth import User, UserRole, auth_service
from app.services.passwords import hash_password

from tests.conftest import make_auth_headers

pytestmark = pytest.mark.usefixtures("no_rate_limit")

PASSWORD = "Xy7!longenoughpass"
# PBKDF2 at 600k iterations is slow by design; hash the fixture password once.
_PASSWORD_HASH = hash_password(PASSWORD)

BASE = "/api/v1/admin/users"


async def _seed_coro(uid: str, email: str, roles: list[str], **fields) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            DBUser(
                id=uid,
                email=email,
                name="Managed User",
                password_hash=_PASSWORD_HASH,
                roles=roles,
                email_verified=True,
                is_active=fields.pop("is_active", True),
                **fields,
            )
        )
        await session.commit()


async def _get_coro(uid: str) -> DBUser | None:
    async with AsyncSessionLocal() as session:
        return await session.get(DBUser, uid)


async def _delete_coro(uids: tuple[str, ...]) -> None:
    async with AsyncSessionLocal() as session:
        for uid in uids:
            row = await session.get(DBUser, uid)
            if row is not None:
                await session.delete(row)
        await session.commit()


class _Users:
    def __init__(self, client):
        self._client = client
        self._created: list[str] = []

    def create(self, role: str = "attorney", **fields) -> tuple[str, str]:
        uid = f"adm-{uuid.uuid4().hex[:10]}"
        email = f"{uid}@example.com"
        self._created.append(uid)
        self._client.portal.call(lambda: _seed_coro(uid, email, [role], **fields))
        return uid, email

    def track(self, uid: str) -> None:
        self._created.append(uid)

    def get(self, uid: str) -> DBUser | None:
        return self._client.portal.call(_get_coro, uid)

    def headers(self, uid: str, email: str, role: str) -> dict:
        """A signed-in session (token + registered session) for a seeded user."""
        return make_auth_headers(
            User(
                id=uid,
                email=email,
                name="Managed User",
                roles=[UserRole(role)],
                last_login=datetime.now(UTC),
            )
        )

    def cleanup(self) -> None:
        self._client.portal.call(_delete_coro, tuple(self._created))
        for uid in self._created:
            session_manager.terminate_all_sessions(uid)


@pytest.fixture
def users(client):
    handle = _Users(client)
    client.cookies.clear()
    yield handle
    client.cookies.clear()
    handle.cleanup()


class TestPermissions:
    def test_anonymous_is_refused(self, client):
        assert client.get(BASE).status_code == 401

    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("get", "", None),
            ("post", "/invite", {"email": "x@example.com", "name": "X", "role": "viewer"}),
            ("get", "/roles", None),
            ("patch", "/some-id/role", {"role": "viewer"}),
            ("patch", "/some-id/active", {"is_active": False}),
            ("post", "/some-id/sessions/revoke", None),
            ("post", "/some-id/mfa/reset", None),
            ("get", "/some-id/export", None),
            ("delete", "/some-id", None),
        ],
    )
    def test_non_admin_is_refused_everywhere(self, client, non_admin_headers, method, path, body):
        kwargs = {"headers": non_admin_headers}
        if body is not None:
            kwargs["json"] = body
        resp = getattr(client, method)(f"{BASE}{path}", **kwargs)
        assert resp.status_code == 403, resp.text

    def test_admin_can_list(self, client, auth_headers, users):
        uid, _email = users.create()
        resp = client.get(BASE, headers=auth_headers)
        assert resp.status_code == 200
        listed = {u["id"]: u for u in resp.json()["users"]}
        assert uid in listed
        assert listed[uid]["must_change_password"] is False
        assert listed[uid]["mfa_enabled"] is False


class TestInvite:
    def _invite(self, client, auth_headers, role="paralegal"):
        email = f"invite-{uuid.uuid4().hex[:8]}@example.com"
        resp = client.post(
            f"{BASE}/invite",
            json={"email": email, "name": "New Hire", "role": role},
            headers=auth_headers,
        )
        return email, resp

    def test_invite_creates_account_that_must_change_password(self, client, auth_headers, users):
        email, resp = self._invite(client, auth_headers)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        users.track(body["user"]["id"])
        assert body["user"]["roles"] == ["paralegal"]
        assert body["user"]["must_change_password"] is True
        assert len(body["temporary_password"]) >= 12
        assert users.get(body["user"]["id"]).must_change_password is True

    def test_invited_user_is_walled_off_until_password_changed(self, client, auth_headers, users):
        email, resp = self._invite(client, auth_headers)
        users.track(resp.json()["user"]["id"])
        temp_password = resp.json()["temporary_password"]

        account_lockout.clear_account_lockout(email)
        login = client.post("/api/v1/auth/login", json={"email": email, "password": temp_password})
        assert login.status_code == 200, login.text
        assert login.json()["user"]["must_change_password"] is True
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        client.cookies.clear()

        blocked = client.get("/api/v1/auth/sessions", headers=headers)
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "password_change_required"

        changed = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": temp_password, "new_password": "Brand-new-Pass-77"},
            headers=headers,
        )
        assert changed.status_code == 200, changed.text

        # The temporary password the admin saw no longer works.
        stale = client.post("/api/v1/auth/login", json={"email": email, "password": temp_password})
        assert stale.status_code == 401
        account_lockout.clear_account_lockout(email)
        fresh = client.post(
            "/api/v1/auth/login", json={"email": email, "password": "Brand-new-Pass-77"}
        )
        assert fresh.status_code == 200
        assert fresh.json()["user"]["must_change_password"] is False

    def test_duplicate_email_is_409(self, client, auth_headers, users):
        _uid, email = users.create()
        resp = client.post(
            f"{BASE}/invite",
            json={"email": email, "name": "Dup", "role": "viewer"},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_invalid_role_is_400(self, client, auth_headers):
        _email, resp = self._invite(client, auth_headers, role="wizard")
        assert resp.status_code == 400

    def test_invite_is_audited(self, client, auth_headers, users, audit_events):
        _email, resp = self._invite(client, auth_headers)
        users.track(resp.json()["user"]["id"])
        assert any(
            e["event_type"].name == "USER_CREATE"
            and e["details"].get("action") == "invite_user"
            and e["resource_id"] == resp.json()["user"]["id"]
            for e in audit_events
        )
        # The temporary password is never written to the audit trail.
        assert resp.json()["temporary_password"] not in str(audit_events)

    def test_unwritable_audit_trail_refuses_the_invite_without_creating_the_account(
        self, client, auth_headers, users
    ):
        """With AUDIT_FAIL_CLOSED the audit write happens before the commit: a
        failure must leave no account behind (whose one-time temporary
        password the admin would never have seen)."""
        from app.services.audit import AuditWriteError

        from tests.helpers import db_count

        email = f"invite-{uuid.uuid4().hex[:8]}@example.com"
        failing = AsyncMock(side_effect=AuditWriteError("audit trail unwritable"))
        with patch("app.routers.users.audit_service.log_event", new=failing):
            try:
                resp = client.post(
                    f"{BASE}/invite",
                    json={"email": email, "name": "New Hire", "role": "paralegal"},
                    headers=auth_headers,
                )
            except AuditWriteError:
                resp = None  # the test client re-raises server errors
        assert resp is None or resp.status_code >= 500
        # audit_service is shared, so other request-path events hit the mock
        # too; the invite event itself must have been attempted and failed.
        assert any(
            c.kwargs.get("details", {}).get("action") == "invite_user"
            for c in failing.await_args_list
        )
        assert db_count(DBUser, DBUser.email == email) == 0


class TestRoleChange:
    def test_role_change_applies_and_ends_sessions(self, client, auth_headers, users):
        uid, email = users.create("attorney")
        target_headers = users.headers(uid, email, "attorney")
        assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 200

        resp = client.patch(f"{BASE}/{uid}/role", json={"role": "viewer"}, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["viewer"]
        assert users.get(uid).roles == ["viewer"]
        # The token carrying the old role is dead.
        assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 401
        assert users.get(uid).token_version == 1

    def test_invalid_role_is_400(self, client, auth_headers, users):
        uid, _email = users.create()
        resp = client.patch(f"{BASE}/{uid}/role", json={"role": "wizard"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_unknown_user_is_404(self, client, auth_headers):
        resp = client.patch(f"{BASE}/nope/role", json={"role": "viewer"}, headers=auth_headers)
        assert resp.status_code == 404

    def test_role_change_is_audited(self, client, auth_headers, users, audit_events):
        uid, _email = users.create()
        client.patch(f"{BASE}/{uid}/role", json={"role": "paralegal"}, headers=auth_headers)
        assert any(
            e["event_type"].name == "USER_ROLE_CHANGE" and e["resource_id"] == uid
            for e in audit_events
        )


class TestDeactivate:
    def test_deactivation_cuts_access_immediately(self, client, auth_headers, users, audit_events):
        uid, email = users.create()
        target_headers = users.headers(uid, email, "attorney")

        resp = client.patch(f"{BASE}/{uid}/active", json={"is_active": False}, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False

        assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 401
        account_lockout.clear_account_lockout(email)
        login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 403
        assert any(
            e["event_type"].name == "USER_DEACTIVATE" and e["resource_id"] == uid
            for e in audit_events
        )

    def test_reactivation_restores_login(self, client, auth_headers, users):
        uid, email = users.create(is_active=False)
        resp = client.patch(f"{BASE}/{uid}/active", json={"is_active": True}, headers=auth_headers)
        assert resp.status_code == 200
        account_lockout.clear_account_lockout(email)
        login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200

    def test_admin_cannot_deactivate_themselves(self, client, users):
        uid, email = users.create("admin")
        headers = users.headers(uid, email, "admin")
        resp = client.patch(f"{BASE}/{uid}/active", json={"is_active": False}, headers=headers)
        assert resp.status_code == 400
        assert users.get(uid).is_active is True


class TestForceSignOut:
    def test_force_sign_out_ends_sessions_but_keeps_account(self, client, auth_headers, users):
        uid, email = users.create()
        account_lockout.clear_account_lockout(email)
        login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        refresh_cookie = client.cookies.get("refresh_token")
        target_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        client.cookies.clear()

        resp = client.post(f"{BASE}/{uid}/sessions/revoke", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["sessions_revoked"] == 1

        assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 401
        client.cookies.set("refresh_token", refresh_cookie)
        assert client.post("/api/v1/auth/refresh").status_code == 401
        client.cookies.clear()

        # Not a deactivation: the user can sign in again.
        assert users.get(uid).is_active is True
        again = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert again.status_code == 200

    def test_unknown_user_is_404(self, client, auth_headers):
        assert client.post(f"{BASE}/nope/sessions/revoke", headers=auth_headers).status_code == 404


class TestMfaReset:
    def test_admin_reset_clears_enrollment(self, client, auth_headers, users, audit_events):
        from app.services import mfa as mfa_service

        uid, email = users.create(
            mfa_enabled=True,
            mfa_secret=mfa_service.encrypt_secret(mfa_service.generate_secret()),
            mfa_recovery_codes=["hash-1", "hash-2"],
        )
        target_headers = users.headers(uid, email, "attorney")

        resp = client.post(f"{BASE}/{uid}/mfa/reset", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        row = users.get(uid)
        assert row.mfa_enabled is False
        assert row.mfa_secret is None
        assert row.mfa_recovery_codes is None
        assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 401
        assert any(
            e["event_type"].name == "MFA_DISABLED"
            and e["resource_id"] == uid
            and e["details"].get("action") == "admin_mfa_reset"
            for e in audit_events
        )

    def test_admin_cannot_reset_their_own_mfa_here(self, client, users):
        uid, email = users.create("admin", mfa_enabled=True, mfa_secret="x")
        headers = users.headers(uid, email, "admin")
        resp = client.post(f"{BASE}/{uid}/mfa/reset", headers=headers)
        assert resp.status_code == 400
        assert users.get(uid).mfa_enabled is True


class TestDelete:
    def test_delete_removes_row_and_tombstones_tokens(self, client, auth_headers, users):
        uid, email = users.create()
        target_headers = users.headers(uid, email, "attorney")

        resp = client.delete(f"{BASE}/{uid}", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "deleted"
        assert users.get(uid) is None
        assert auth_service._is_user_revoked(uid) is True
        assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 401

    def test_admin_cannot_delete_themselves(self, client, users):
        uid, email = users.create("admin")
        headers = users.headers(uid, email, "admin")
        resp = client.delete(f"{BASE}/{uid}", headers=headers)
        assert resp.status_code == 400
        assert users.get(uid) is not None

    def test_unknown_user_is_404(self, client, auth_headers):
        assert client.delete(f"{BASE}/nope", headers=auth_headers).status_code == 404

    def test_key_store_failure_leaves_the_account_untouched(self, client, auth_headers, users):
        uid, email = users.create()
        target_headers = users.headers(uid, email, "attorney")

        with patch(
            "app.services.key_storage.delete_user_keys",
            side_effect=RuntimeError("Key storage unavailable"),
        ):
            resp = client.delete(f"{BASE}/{uid}", headers=auth_headers)

        assert resp.status_code == 503
        assert "was not deleted" in resp.json()["detail"]
        assert users.get(uid) is not None
        assert auth_service._is_user_revoked(uid) is False
        assert client.get("/api/v1/auth/me", headers=target_headers).status_code == 200

    def test_delete_is_audited(self, client, auth_headers, users, audit_events):
        uid, _email = users.create()
        client.delete(f"{BASE}/{uid}", headers=auth_headers)
        names = {e["event_type"].name for e in audit_events if e.get("resource_id") == uid}
        assert {"USER_DELETE", "DATA_DELETION"} <= names


class TestExport:
    def test_unknown_user_is_404(self, client, auth_headers):
        assert client.get(f"{BASE}/nope/export", headers=auth_headers).status_code == 404

    def test_export_includes_chats_and_workspace_sessions(
        self, client, auth_headers, users, audit_events
    ):
        uid, email = users.create()
        target_headers = users.headers(uid, email, "attorney")
        created = client.post(
            "/api/v1/workspace-sessions",
            json={"surface": "contracts", "title": "Lease review", "payload": {"k": "v"}},
            headers=target_headers,
        )
        assert created.status_code in (200, 201), created.text
        client.cookies.clear()

        resp = client.get(f"{BASE}/{uid}/export", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["profile"]["email"] == email
        for key in (
            "documents",
            "folders",
            "settings",
            "chat_sessions",
            "contract_analyses",
            "workspace_sessions",
            "matters",
        ):
            assert key in body
        assert [w["title"] for w in body["workspace_sessions"]] == ["Lease review"]
        assert set(body["matters"]) == {"owned", "member_of"}
        # Secrets never leave through the export.
        assert "password" not in str(body).lower()
        exports = [e for e in audit_events if e["event_type"].name == "DATA_EXPORT"]
        assert exports and exports[-1]["details"]["workspace_sessions"] == 1


class TestRolePermissionsMatrix:
    def test_matrix_is_returned(self, client, auth_headers):
        resp = client.get(f"{BASE}/roles", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["roles"]) >= {"admin", "attorney", "paralegal", "viewer"}
        assert "admin.users" in body["assigned"]["admin"]

    def test_unknown_role_is_400(self, client, auth_headers):
        resp = client.put(f"{BASE}/roles/wizard", json={"permissions": []}, headers=auth_headers)
        assert resp.status_code == 400
