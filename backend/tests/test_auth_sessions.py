"""
End-to-end tests for session lifetime, token invalidation and forced steps.

Covers the controls a sign-in must honour after it is issued:
- a token refresh continues the SAME session (no extra session, no extension
  of the absolute lifetime) and is refused once that session has ended
- "log out everywhere" kills refresh tokens on other devices
- replay of a rotated refresh token ends every session of the account
- an invited user must change the temporary password before anything else
- REQUIRE_MFA forces enrollment
- MFA: single-use codes, no lockout reset by re-entering the password,
  password required to disable
- password reset cannot give an SSO-only account a local password

DB seeding runs on the TestClient's own event loop (``client.portal``).
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from app.config import settings
from app.database import AsyncSessionLocal
from app.middleware.security import account_lockout, session_manager
from app.models.db_models import User as DBUser
from app.services import mfa as mfa_service
from app.services.auth import auth_service, password_reset_service
from app.services.passwords import hash_password


@pytest.fixture(autouse=True)
def _process_local_sessions():
    """These tests cover the process-local store (dict mirrored to a file).
    CI runs the suite with a Redis service configured, which would route
    sessions through it; tests/test_session_redis_backend.py covers that."""
    with patch("app.middleware.security.get_redis", return_value=None):
        yield


pytestmark = pytest.mark.usefixtures("no_rate_limit")

PASSWORD = "Xy7!longenoughpass"
NEW_PASSWORD = "Qz4#another-long-one"
# PBKDF2 at 600k iterations is slow by design; hash the fixture password once.
_PASSWORD_HASH = hash_password(PASSWORD)


async def _seed_coro(uid: str, email: str, **fields) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            DBUser(
                id=uid,
                email=email,
                name="Session Test",
                password_hash=fields.pop("password_hash", _PASSWORD_HASH),
                roles=fields.pop("roles", ["attorney"]),
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

    def create(self, domain: str = "test.local", **fields) -> tuple[str, str]:
        uid = f"sess-{uuid.uuid4().hex[:10]}"
        email = f"{uid}@{domain}"
        self._created.append(uid)
        self._client.portal.call(lambda: _seed_coro(uid, email, **fields))
        return uid, email

    def get(self, uid: str) -> DBUser | None:
        return self._client.portal.call(_get_coro, uid)

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


def _login(client, email: str, password: str = PASSWORD):
    account_lockout.clear_account_lockout(email)
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _bearer(resp) -> dict:
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestRefreshContinuesTheSession:
    def test_refresh_does_not_add_a_session(self, client, users):
        uid, email = users.create()
        assert _login(client, email).status_code == 200
        assert len(session_manager.get_active_sessions_raw(uid)) == 1

        for _ in range(3):
            assert client.post("/api/v1/auth/refresh").status_code == 200
        assert len(session_manager.get_active_sessions_raw(uid)) == 1

    def test_refresh_keeps_the_original_absolute_expiry(self, client, users):
        uid, email = users.create()
        _login(client, email)
        before = session_manager.get_active_sessions_raw(uid)[0]["expires_at"]

        assert client.post("/api/v1/auth/refresh").status_code == 200
        assert session_manager.get_active_sessions_raw(uid)[0]["expires_at"] == before

    def test_old_access_token_stops_working_after_refresh(self, client, users):
        _uid, email = users.create()
        login = _login(client, email)
        old = _bearer(login)
        client.cookies.delete("access_token")

        refreshed = client.post("/api/v1/auth/refresh")
        assert refreshed.status_code == 200
        client.cookies.delete("access_token")

        assert client.get("/api/v1/auth/me", headers=old).status_code == 401
        assert client.get("/api/v1/auth/me", headers=_bearer(refreshed)).status_code == 200

    def test_refresh_refused_past_absolute_lifetime(self, client, users):
        uid, email = users.create()
        _login(client, email)
        for s in session_manager._sessions[uid]:
            s["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

        assert client.post("/api/v1/auth/refresh").status_code == 401

    def test_refresh_refused_after_idle_timeout(self, client, users):
        uid, email = users.create()
        _login(client, email)
        idle = session_manager.idle_timeout
        assert idle is not None, "idle timeout should be on by default"
        for s in session_manager._sessions[uid]:
            s["last_activity"] = datetime.now(UTC) - idle - timedelta(minutes=1)

        assert client.post("/api/v1/auth/refresh").status_code == 401

    def test_idle_session_rejects_requests(self, client, users):
        uid, email = users.create()
        login = _login(client, email)
        for s in session_manager._sessions[uid]:
            s["last_activity"] = datetime.now(UTC) - session_manager.idle_timeout * 2

        client.cookies.clear()
        assert client.get("/api/v1/auth/me", headers=_bearer(login)).status_code == 401

    def test_refresh_without_a_session_is_refused(self, client, users):
        """A refresh token whose session is gone (evicted, signed out) mints nothing."""
        uid, email = users.create()
        _login(client, email)
        session_manager.terminate_all_sessions(uid)

        assert client.post("/api/v1/auth/refresh").status_code == 401

    def test_session_cap_holds_across_refreshes(self, client, users):
        uid, email = users.create()
        for _ in range(session_manager.max_sessions_per_user + 2):
            client.cookies.clear()
            assert _login(client, email).status_code == 200
            assert client.post("/api/v1/auth/refresh").status_code == 200
        assert (
            len(session_manager.get_active_sessions_raw(uid))
            == session_manager.max_sessions_per_user
        )


class TestLogoutEverywhere:
    def test_refresh_token_on_another_device_dies(self, client, users):
        _uid, email = users.create()
        # Device B signs in and keeps its refresh cookie.
        _login(client, email)
        device_b_refresh = client.cookies.get("refresh_token")
        client.cookies.clear()
        # Device A signs in and logs out everywhere.
        login_a = _login(client, email)
        assert client.post("/api/v1/auth/logout/all", headers=_bearer(login_a)).status_code == 200

        client.cookies.clear()
        client.cookies.set("refresh_token", device_b_refresh)
        assert client.post("/api/v1/auth/refresh").status_code == 401

    def test_token_version_is_bumped(self, client, users):
        uid, email = users.create()
        login = _login(client, email)
        assert users.get(uid).token_version == 0
        client.post("/api/v1/auth/logout/all", headers=_bearer(login))
        assert users.get(uid).token_version == 1

    def test_stale_token_rejected_even_if_session_store_is_lost(self, client, users):
        """The version check is independent of the in-memory session store."""
        uid, email = users.create()
        login = _login(client, email)
        stale_refresh = client.cookies.get("refresh_token")
        stale_session = dict(session_manager._sessions[uid][0])
        client.post("/api/v1/auth/logout/all", headers=_bearer(login))

        # Simulate a restored session record (e.g. an old sessions.jsonl).
        session_manager._sessions[uid].append(stale_session)
        client.cookies.clear()
        client.cookies.set("refresh_token", stale_refresh)
        assert client.post("/api/v1/auth/refresh").status_code == 401

    def test_user_can_sign_in_again(self, client, users):
        _uid, email = users.create()
        login = _login(client, email)
        client.post("/api/v1/auth/logout/all", headers=_bearer(login))
        client.cookies.clear()

        again = _login(client, email)
        assert again.status_code == 200
        client.cookies.delete("access_token")
        assert client.get("/api/v1/auth/me", headers=_bearer(again)).status_code == 200


class TestRefreshTokenReuse:
    def test_replay_of_rotated_token_ends_every_session(self, client, users, audit_events):
        uid, email = users.create()
        _login(client, email)
        stolen = client.cookies.get("refresh_token")
        assert client.post("/api/v1/auth/refresh").status_code == 200
        current = client.cookies.get("refresh_token")
        assert current != stolen

        with patch("app.services.auth.REFRESH_REUSE_GRACE_SECONDS", -1):
            client.cookies.clear()
            client.cookies.set("refresh_token", stolen)
            assert client.post("/api/v1/auth/refresh").status_code == 401

        assert session_manager.get_active_sessions_raw(uid) == []
        assert users.get(uid).token_version == 1
        assert any(
            e["event_type"].name == "SESSION_HIJACK_ATTEMPT" and e["user_id"] == uid
            for e in audit_events
        )
        # The legitimate holder's newer token is dead too.
        client.cookies.clear()
        client.cookies.set("refresh_token", current)
        assert client.post("/api/v1/auth/refresh").status_code == 401

    def test_immediate_double_refresh_is_not_treated_as_theft(self, client, users):
        """Two tabs refreshing at once: the loser gets a 401, nobody is signed out."""
        uid, email = users.create()
        _login(client, email)
        first = client.cookies.get("refresh_token")
        assert client.post("/api/v1/auth/refresh").status_code == 200
        winner = client.cookies.get("refresh_token")

        client.cookies.clear()
        client.cookies.set("refresh_token", first)
        assert client.post("/api/v1/auth/refresh").status_code == 401
        assert len(session_manager.get_active_sessions_raw(uid)) == 1
        assert users.get(uid).token_version == 0

        client.cookies.clear()
        client.cookies.set("refresh_token", winner)
        assert client.post("/api/v1/auth/refresh").status_code == 200


class TestMustChangePassword:
    def test_login_and_me_report_the_flag(self, client, users):
        _uid, email = users.create(must_change_password=True)
        login = _login(client, email)
        assert login.status_code == 200
        assert login.json()["user"]["must_change_password"] is True
        me = client.get("/api/v1/auth/me", headers=_bearer(login))
        assert me.status_code == 200
        assert me.json()["must_change_password"] is True

    def test_api_is_closed_until_password_changed(self, client, users):
        _uid, email = users.create(must_change_password=True)
        login = _login(client, email)
        blocked = client.get("/api/v1/auth/sessions", headers=_bearer(login))
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "password_change_required"
        assert client.get("/api/v1/documents", headers=_bearer(login)).status_code == 403

    def test_change_password_clears_the_flag(self, client, users):
        uid, email = users.create(must_change_password=True)
        login = _login(client, email)
        changed = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=_bearer(login),
        )
        assert changed.status_code == 200, changed.text
        assert users.get(uid).must_change_password is False

        client.cookies.clear()
        again = _login(client, email, NEW_PASSWORD)
        assert again.status_code == 200
        assert again.json()["user"]["must_change_password"] is False
        assert client.get("/api/v1/auth/sessions", headers=_bearer(again)).status_code == 200

    def test_new_password_must_differ(self, client, users):
        uid, email = users.create(must_change_password=True)
        login = _login(client, email)
        resp = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": PASSWORD},
            headers=_bearer(login),
        )
        assert resp.status_code == 400
        assert users.get(uid).must_change_password is True

    def test_reset_password_clears_the_flag(self, client, users):
        uid, email = users.create(must_change_password=True)
        token = password_reset_service.generate_reset_token(email)
        resp = client.post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        assert users.get(uid).must_change_password is False


class TestRequireMfa:
    def test_unenrolled_account_must_enroll_first(self, client, users):
        _uid, email = users.create()
        forced = settings.model_copy(update={"require_mfa": True})
        with patch("app.services.auth.settings", forced):
            login = _login(client, email)
            assert login.json()["user"]["mfa_enrollment_required"] is True
            headers = _bearer(login)

            blocked = client.get("/api/v1/auth/sessions", headers=headers)
            assert blocked.status_code == 403
            assert blocked.json()["code"] == "mfa_enrollment_required"
            # The enrollment endpoints stay open.
            assert client.get("/api/v1/auth/mfa/status", headers=headers).status_code == 200
            assert client.post("/api/v1/auth/mfa/setup", headers=headers).status_code == 200

    def test_off_by_default(self, client, users):
        _uid, email = users.create()
        login = _login(client, email)
        assert login.json()["user"]["mfa_enrollment_required"] is False
        assert client.get("/api/v1/auth/sessions", headers=_bearer(login)).status_code == 200

    def test_sso_only_account_is_exempt(self):
        from app.services.auth import mfa_enrollment_required

        forced = settings.model_copy(update={"require_mfa": True})
        with patch("app.services.auth.settings", forced):
            assert mfa_enrollment_required(mfa_enabled=False, has_password=False) is False
            assert mfa_enrollment_required(mfa_enabled=False, has_password=True) is True
            assert mfa_enrollment_required(mfa_enabled=True, has_password=True) is False


def _enroll(users) -> tuple[str, str, str]:
    """Create a user with MFA already enabled; returns (uid, email, secret)."""
    secret = mfa_service.generate_secret()
    uid, email = users.create(mfa_enabled=True, mfa_secret=mfa_service.encrypt_secret(secret))
    return uid, email, secret


def _code(secret: str, step: int = 0) -> str:
    """A valid TOTP code; ``step`` picks a neighbouring 30s window (still accepted)."""
    import time

    import pyotp

    return pyotp.TOTP(secret).at(time.time() + 30 * step)


class TestMfaHardening:
    def test_totp_code_cannot_be_replayed(self, client, users):
        _uid, email, secret = _enroll(users)
        code = _code(secret)

        challenge = _login(client, email).json()["mfa_token"]
        ok = client.post("/api/v1/auth/mfa/verify", json={"mfa_token": challenge, "code": code})
        assert ok.status_code == 200, ok.text

        client.cookies.clear()
        challenge = _login(client, email).json()["mfa_token"]
        replay = client.post("/api/v1/auth/mfa/verify", json={"mfa_token": challenge, "code": code})
        assert replay.status_code == 401

    def test_correct_password_does_not_reset_the_mfa_lockout_counter(self, client, users):
        _uid, email, _secret = _enroll(users)
        account_lockout.clear_account_lockout(email)
        # The counter under test is the in-memory one; with a Redis service
        # (CI) the failures would be recorded there and the dict stay empty.
        no_redis = patch.multiple(
            "app.middleware.security",
            require_redis=lambda: None,
            get_redis=lambda: None,
        )
        no_redis.start()
        try:
            for _ in range(2):
                challenge = client.post(
                    "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
                ).json()["mfa_token"]
                bad = client.post(
                    "/api/v1/auth/mfa/verify", json={"mfa_token": challenge, "code": "000000"}
                )
                assert bad.status_code == 401
            # Entering the password again (twice above) must not have wiped them.
            assert len(account_lockout._account_failures[email]) == 2
        finally:
            no_redis.stop()
            account_lockout.clear_account_lockout(email)

    def test_repeated_bad_codes_lock_the_account(self, client, users):
        _uid, email, _secret = _enroll(users)
        account_lockout.clear_account_lockout(email)
        try:
            statuses = []
            for _ in range(6):
                login = client.post(
                    "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
                )
                if login.status_code == 429:
                    statuses.append(429)
                    break
                resp = client.post(
                    "/api/v1/auth/mfa/verify",
                    json={"mfa_token": login.json()["mfa_token"], "code": "000000"},
                )
                statuses.append(resp.status_code)
            assert statuses[-1] == 429
        finally:
            account_lockout.clear_account_lockout(email)

    def _signed_in(self, client, email, secret) -> dict:
        challenge = _login(client, email).json()["mfa_token"]
        resp = client.post(
            "/api/v1/auth/mfa/verify", json={"mfa_token": challenge, "code": _code(secret)}
        )
        assert resp.status_code == 200, resp.text
        return _bearer(resp)

    def test_disable_requires_the_password(self, client, users):
        uid, email, secret = _enroll(users)
        headers = self._signed_in(client, email, secret)

        missing = client.post(
            "/api/v1/auth/mfa/disable", json={"code": _code(secret, 1)}, headers=headers
        )
        assert missing.status_code == 400
        wrong = client.post(
            "/api/v1/auth/mfa/disable",
            json={"code": _code(secret, 1), "password": "not-the-password"},
            headers=headers,
        )
        assert wrong.status_code == 400
        assert users.get(uid).mfa_enabled is True

        ok = client.post(
            "/api/v1/auth/mfa/disable",
            json={"code": _code(secret, 1), "password": PASSWORD},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
        assert users.get(uid).mfa_enabled is False
        account_lockout.clear_account_lockout(f"mfa-manage:{email}")

    def test_disable_locks_after_repeated_bad_codes(self, client, users):
        uid, email, secret = _enroll(users)
        headers = self._signed_in(client, email, secret)
        try:
            statuses = [
                client.post(
                    "/api/v1/auth/mfa/disable",
                    json={"code": "000000", "password": PASSWORD},
                    headers=headers,
                ).status_code
                for _ in range(6)
            ]
            assert statuses[:5] == [400] * 5
            assert statuses[5] == 429
            # Even a correct code is refused while locked.
            locked = client.post(
                "/api/v1/auth/mfa/disable",
                json={"code": _code(secret, 1), "password": PASSWORD},
                headers=headers,
            )
            assert locked.status_code == 429
            assert users.get(uid).mfa_enabled is True
        finally:
            account_lockout.clear_account_lockout(f"mfa-manage:{email}")

    def test_recovery_codes_endpoint_locks_after_repeated_bad_codes(self, client, users):
        _uid, email, secret = _enroll(users)
        headers = self._signed_in(client, email, secret)
        try:
            statuses = [
                client.post(
                    "/api/v1/auth/mfa/recovery-codes", json={"code": "000000"}, headers=headers
                ).status_code
                for _ in range(6)
            ]
            assert statuses[-1] == 429
        finally:
            account_lockout.clear_account_lockout(f"mfa-manage:{email}")


class TestVerifyTotpOnce:
    def test_second_use_of_a_code_is_refused(self):
        secret = mfa_service.generate_secret()
        code = _code(secret)
        assert mfa_service.verify_totp_once("u-once-1", secret, code) is True
        assert mfa_service.verify_totp_once("u-once-1", secret, code) is False

    def test_wrong_code_is_refused(self):
        secret = mfa_service.generate_secret()
        assert mfa_service.verify_totp_once("u-once-2", secret, "00000") is False

    def test_tracking_is_per_user(self):
        secret = mfa_service.generate_secret()
        code = _code(secret)
        assert mfa_service.verify_totp_once("u-once-3", secret, code) is True
        assert mfa_service.verify_totp_once("u-once-4", secret, code) is True


class TestPasswordResetForSsoAccounts:
    def test_forgot_password_issues_no_token_for_sso_only_account(self, client, users):
        _uid, email = users.create(password_hash=None)
        with patch.object(password_reset_service, "generate_reset_token") as generate:
            resp = client.post("/api/v1/auth/forgot-password", json={"email": email})
        assert resp.status_code == 200
        generate.assert_not_called()

    def test_reset_cannot_give_an_sso_account_a_password(self, client, users):
        uid, email = users.create(password_hash=None)
        token = password_reset_service.generate_reset_token(email)
        resp = client.post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
        )
        assert resp.status_code == 400
        assert users.get(uid).password_hash is None

    def test_forgot_password_response_is_identical_for_unknown_and_known(self, client, users):
        _uid, email = users.create()
        known = client.post("/api/v1/auth/forgot-password", json={"email": email})
        unknown = client.post(
            "/api/v1/auth/forgot-password", json={"email": "nobody-here@test.local"}
        )
        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()

    def test_email_failure_never_surfaces(self, client, users):
        """A mail-provider error must not turn into a 500 for existing accounts."""
        _uid, email = users.create()
        configured = settings.model_copy(update={"sendgrid_api_key": "SG.test"})

        def _boom(*_args, **_kwargs):
            raise Exception("HTTP Error 401: Unauthorized")

        with (
            patch("app.routers.auth.settings", configured),
            patch("app.services.email.settings", configured),
            patch("app.services.email._send_via_sendgrid", _boom),
        ):
            resp = client.post("/api/v1/auth/forgot-password", json={"email": email})
        assert resp.status_code == 200


class TestLoginAudit:
    def test_login_to_disabled_account_is_audited(self, client, users, audit_events):
        uid, email = users.create(is_active=False)
        resp = _login(client, email)
        assert resp.status_code == 403
        assert any(
            e["event_type"].name == "LOGIN_FAILURE"
            and e.get("user_id") == uid
            and e["details"].get("reason") == "account_disabled"
            for e in audit_events
        )


class TestRegistrationGates:
    def test_closed_registration_does_not_reveal_existing_accounts(self, client, users):
        """403 'registration disabled' must come before the 409 duplicate check."""
        _uid, email = users.create(domain="example.com")
        closed = settings.model_copy(update={"debug": False, "allow_registration": False})
        with patch("app.routers.auth.settings", closed):
            resp = client.post(
                "/api/v1/auth/register",
                json={"email": email, "name": "Probe", "password": PASSWORD},
            )
        assert resp.status_code == 403

    async def test_first_account_needs_bootstrap_token_in_production(self):
        """On an empty production instance, registration fails closed without the token."""
        from app.models.schemas import RegisterRequest
        from app.routers import auth as auth_router
        from fastapi import HTTPException, Response
        from starlette.requests import Request

        class _EmptyDb:
            async def execute(self, _stmt):
                class _Result:
                    def scalar_one(self):
                        return 0

                return _Result()

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/auth/register",
                "headers": [],
                "query_string": b"",
                "client": ("203.0.113.9", 1234),
            }
        )
        body = RegisterRequest(email="first@example.com", name="First", password=PASSWORD)
        production = settings.model_copy(
            update={"debug": False, "registration_bootstrap_token": ""}
        )
        with patch("app.routers.auth.settings", production), pytest.raises(HTTPException) as exc:
            await auth_router.register(request, Response(), body, _EmptyDb())
        assert exc.value.status_code == 403
        assert "REGISTRATION_BOOTSTRAP_TOKEN" in exc.value.detail

        # With a token configured, a wrong/missing value is refused too.
        tokened = settings.model_copy(
            update={"debug": False, "registration_bootstrap_token": "s3cret-bootstrap"}
        )
        with patch("app.routers.auth.settings", tokened), pytest.raises(HTTPException) as exc:
            await auth_router.register(request, Response(), body, _EmptyDb())
        assert exc.value.status_code == 403


class TestRevocationStore:
    """The durable revocation store must not depend on Redis keeping its keys."""

    @staticmethod
    def _service(tmp_path):
        from app.services.auth import AuthService

        with patch("app.services.auth.settings") as cfg:
            cfg.access_token_expire_minutes = 60
            cfg.upload_dir = str(tmp_path / "uploads")
            cfg.debug = True
            cfg.microsoft_tenant_id = None
            cfg.microsoft_client_id = None
            return AuthService()

    def test_revocation_survives_restart(self, tmp_path):
        first = self._service(tmp_path)
        first.revoke_token("jti-durable")
        first.revoke_user("user-gone")

        second = self._service(tmp_path)
        assert second._is_token_revoked("jti-durable") is True
        assert second._is_user_revoked("user-gone") is True

    def test_local_store_wins_when_redis_lost_the_key(self, tmp_path):
        from unittest.mock import MagicMock

        service = self._service(tmp_path)
        service.revoke_token("jti-evicted")
        service.revoke_user("user-evicted")

        amnesiac_redis = MagicMock()
        amnesiac_redis.exists.return_value = 0  # the key was evicted
        with patch("app.services.auth.get_redis", return_value=amnesiac_redis):
            assert service._is_token_revoked("jti-evicted") is True
            assert service._is_user_revoked("user-evicted") is True

    def test_expired_entries_are_pruned_from_the_file(self, tmp_path):
        import json

        service = self._service(tmp_path)
        expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with open(service._revocation_file, "w") as f:
            f.write(json.dumps({"jti": "old", "expires_at": expired}) + "\n")
            f.write(json.dumps({"user_id": "old-user", "expires_at": expired}) + "\n")
        service.revoke_token("fresh")

        reloaded = self._service(tmp_path)  # loads, then compacts
        content = reloaded._revocation_file.read_text()
        assert '"old"' not in content and "old-user" not in content
        assert "fresh" in content
        assert reloaded._is_token_revoked("old") is False

    def test_claim_refresh_token_classifies_replay(self, tmp_path):
        service = self._service(tmp_path)
        assert service.claim_refresh_token("r1") == "ok"
        # Immediately again: inside the grace window — a race, not theft.
        assert service.claim_refresh_token("r1") == "revoked"
        with patch("app.services.auth.REFRESH_REUSE_GRACE_SECONDS", -1):
            assert service.claim_refresh_token("r1") == "reused"

    def test_logged_out_token_is_never_classified_as_theft(self, tmp_path):
        service = self._service(tmp_path)
        service.revoke_token("r-logout")
        with patch("app.services.auth.REFRESH_REUSE_GRACE_SECONDS", -1):
            assert service.claim_refresh_token("r-logout") == "revoked"

    def test_rotation_marker_survives_restart(self, tmp_path):
        first = self._service(tmp_path)
        assert first.claim_refresh_token("r-rot") == "ok"
        second = self._service(tmp_path)
        with patch("app.services.auth.REFRESH_REUSE_GRACE_SECONDS", -1):
            assert second.claim_refresh_token("r-rot") == "reused"


def test_auth_service_refresh_token_is_bound_to_a_session():
    from app.services.auth import User, UserRole

    user = User(id="bind-1", email="b@test.local", name="B", roles=[UserRole.VIEWER])
    token = auth_service.create_refresh_token(user, session_id="sid-123")
    data = auth_service.verify_token(token)
    assert data.token_type == "refresh"
    assert data.session_id == "sid-123"
    assert data.token_version == 0


class TestDeletedUserToken:
    """A token survives the deletion of its user row only in DEBUG (the
    row-less dev account). In production a missing row is a dead account:
    hand-deleted users and backups restored from before an account was
    created must not leave a working bearer token behind."""

    def test_token_for_missing_row_is_rejected_outside_debug(self, client, users):
        uid, email = users.create()
        login = _login(client, email)
        headers = _bearer(login)
        client.cookies.delete("access_token")
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

        client.portal.call(_delete_coro, (uid,))

        from app.services import auth as auth_module

        prod_like = MagicMock(wraps=auth_module.settings)
        prod_like.debug = False
        with patch("app.services.auth.settings", prod_like):
            resp = client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 401

    def test_token_for_missing_row_still_passes_in_debug(self, client, users):
        uid, email = users.create()
        headers = _bearer(_login(client, email))
        client.cookies.delete("access_token")
        client.portal.call(_delete_coro, (uid,))
        # DEBUG is true for the suite: the dev-account path stays open.
        assert client.get("/api/v1/auth/me", headers=headers).status_code != 401
