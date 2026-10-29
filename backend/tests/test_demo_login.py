"""
Public demo account (DEMO_LOGIN_EMAIL / DEMO_LOGIN_PASSWORD).

The account is shared by every visitor, so it must be impossible to take
hostage: its password, MFA and role are locked, it is exempt from the login
lockout, it is reset at every startup, and GET /branding publishes the
credentials so the sign-in form prefills them. Off unless both settings are set.

DB seeding runs on the TestClient's own event loop (``client.portal``).
"""

from unittest.mock import AsyncMock, patch

import pytest
from app.config import Settings, settings
from app.database import AsyncSessionLocal
from app.middleware.security import account_lockout
from app.models.db_models import User as DBUser
from app.services import demo_login
from app.services.auth import password_reset_service
from app.services.passwords import verify_password

pytestmark = pytest.mark.usefixtures("no_rate_limit")

DEMO_EMAIL = "demo-login-test@example.com"
DEMO_PASSWORD = "Password1!"  # deliberately below the policy: public anyway

DEMO_SETTINGS = settings.model_copy(
    update={"demo_login_email": DEMO_EMAIL, "demo_login_password": DEMO_PASSWORD}
)

# Every module that reads `settings` at call time for the demo checks.
_SETTINGS_TARGETS = (
    "app.services.demo_login.settings",
    "app.routers.auth.settings",
    "app.routers.branding.settings",
)


@pytest.fixture
def demo_enabled():
    patches = [patch(target, DEMO_SETTINGS) for target in _SETTINGS_TARGETS]
    for p in patches:
        p.start()
    try:
        yield DEMO_SETTINGS
    finally:
        for p in patches:
            p.stop()


async def _seed_coro() -> DBUser:
    async with AsyncSessionLocal() as session:
        user = await demo_login.ensure_demo_login_account(session)
        assert user is not None
        return user


async def _get_coro() -> DBUser | None:
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        result = await session.execute(select(DBUser).where(DBUser.email == DEMO_EMAIL))
        return result.scalar_one_or_none()


async def _mutate_coro(**fields) -> None:
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        result = await session.execute(select(DBUser).where(DBUser.email == DEMO_EMAIL))
        user = result.scalar_one()
        for key, value in fields.items():
            setattr(user, key, value)
        await session.commit()


async def _delete_coro() -> None:
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        result = await session.execute(select(DBUser).where(DBUser.email == DEMO_EMAIL))
        user = result.scalar_one_or_none()
        if user is not None:
            await session.delete(user)
            await session.commit()


@pytest.fixture
def demo_user(client, demo_enabled):
    """The seeded demo account; removed again afterwards."""
    client.cookies.clear()
    account_lockout.clear_account_lockout(DEMO_EMAIL)
    user = client.portal.call(_seed_coro)
    yield user
    client.cookies.clear()
    client.portal.call(_delete_coro)


def _login(client, password: str = DEMO_PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": password})


def _bearer(resp) -> dict:
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestSettings:
    def test_off_by_default(self):
        assert settings.demo_login_enabled is False
        assert demo_login.is_demo_login_email("anyone@example.com") is False

    def test_email_and_password_must_be_set_together(self):
        with pytest.raises(ValueError, match="must be set together"):
            Settings(demo_login_email=DEMO_EMAIL)
        with pytest.raises(ValueError, match="must be set together"):
            Settings(demo_login_password=DEMO_PASSWORD)

    def test_cannot_combine_with_require_mfa(self):
        with pytest.raises(ValueError, match="REQUIRE_MFA"):
            Settings(
                demo_login_email=DEMO_EMAIL, demo_login_password=DEMO_PASSWORD, require_mfa=True
            )

    def test_email_is_normalized(self):
        cfg = Settings(demo_login_email="  Demo@Example.COM ", demo_login_password=DEMO_PASSWORD)
        assert cfg.demo_login_email == "demo@example.com"
        assert cfg.demo_login_enabled is True

    def test_is_demo_login_email_ignores_case(self, demo_enabled):
        assert demo_login.is_demo_login_email(DEMO_EMAIL.upper()) is True
        assert demo_login.is_demo_login_email("other@example.com") is False
        assert demo_login.is_demo_login_email(None) is False


class TestSeeding:
    def test_disabled_is_a_noop(self, client):
        async def _run():
            async with AsyncSessionLocal() as session:
                return await demo_login.ensure_demo_login_account(session)

        assert client.portal.call(_run) is None

    def test_creates_attorney_with_configured_password(self, client, demo_user):
        row = client.portal.call(_get_coro)
        assert row is not None
        assert row.roles == ["attorney"]
        assert row.is_active is True
        assert row.mfa_enabled is False
        assert row.must_change_password is False
        assert verify_password(DEMO_PASSWORD, row.password_hash)

    def test_restart_undoes_whatever_a_visitor_changed(self, client, demo_user):
        client.portal.call(
            lambda: _mutate_coro(
                roles=["admin"],
                is_active=False,
                mfa_enabled=True,
                mfa_secret="enc",
                must_change_password=True,
                password_hash="not-the-demo-password",
            )
        )
        before = client.portal.call(_get_coro)
        client.portal.call(_seed_coro)
        row = client.portal.call(_get_coro)

        assert row.id == before.id  # reset in place, not recreated
        assert row.roles == ["attorney"]
        assert row.is_active is True
        assert row.mfa_enabled is False
        assert row.mfa_secret is None
        assert row.must_change_password is False
        assert verify_password(DEMO_PASSWORD, row.password_hash)
        assert row.token_version == before.token_version + 1


class TestBranding:
    def test_branding_publishes_credentials_when_enabled(self, client, demo_enabled):
        with patch(
            "app.routers.branding._get_branding",
            new_callable=AsyncMock,
            return_value={
                "firm_name": "CaseCite",
                "primary_color": "#1a365d",
                "secondary_color": "#2d3748",
                "accent_color": "#3182ce",
            },
        ):
            resp = client.get("/api/v1/branding")
        assert resp.status_code == 200
        assert resp.json()["demo_login"] == {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}

    def test_branding_has_no_credentials_by_default(self, client):
        with patch(
            "app.routers.branding._get_branding",
            new_callable=AsyncMock,
            return_value={
                "firm_name": "CaseCite",
                "primary_color": "#1a365d",
                "secondary_color": "#2d3748",
                "accent_color": "#3182ce",
            },
        ):
            resp = client.get("/api/v1/branding")
        assert resp.status_code == 200
        assert resp.json()["demo_login"] is None


class TestHostageProofing:
    def test_can_sign_in_with_published_credentials(self, client, demo_user):
        resp = _login(client)
        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == DEMO_EMAIL
        assert resp.json()["user"]["roles"] == ["attorney"]

    def test_bad_passwords_do_not_lock_the_account(self, client, demo_user):
        for _ in range(6):
            assert _login(client, "wrong-password").status_code == 401
        assert account_lockout.is_account_locked(DEMO_EMAIL) is False
        assert _login(client).status_code == 200

    def test_cannot_change_password(self, client, demo_user):
        headers = _bearer(_login(client))
        resp = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": DEMO_PASSWORD, "new_password": "Qz4#another-long-one"},
            headers=headers,
        )
        assert resp.status_code == 403
        row = client.portal.call(_get_coro)
        assert verify_password(DEMO_PASSWORD, row.password_hash)

    def test_cannot_enrol_mfa(self, client, demo_user):
        headers = _bearer(_login(client))
        resp = client.post("/api/v1/auth/mfa/setup", headers=headers)
        assert resp.status_code == 403
        row = client.portal.call(_get_coro)
        assert row.mfa_secret is None

    def test_cannot_be_reset_by_email(self, client, demo_user):
        token = password_reset_service.generate_reset_token(DEMO_EMAIL)
        resp = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "Qz4#another-long-one"},
        )
        assert resp.status_code == 400
        row = client.portal.call(_get_coro)
        assert verify_password(DEMO_PASSWORD, row.password_hash)

    def test_many_visitors_can_be_signed_in_at_once(self, client, demo_user):
        """The ordinary cap of 5 would evict the first visitor on the sixth sign-in."""
        first = _bearer(_login(client))
        for _ in range(7):
            assert _login(client).status_code == 200
        assert client.get("/api/v1/auth/me", headers=first).status_code == 200

    def test_refresh_replay_does_not_sign_everyone_out(self, client, demo_user):
        _login(client)
        stolen = client.cookies.get("refresh_token")
        assert client.post("/api/v1/auth/refresh").status_code == 200
        client.cookies.clear()
        other_visitor = _bearer(_login(client))

        with patch("app.services.auth.REFRESH_REUSE_GRACE_SECONDS", -1):
            client.cookies.clear()
            client.cookies.set("refresh_token", stolen)
            assert client.post("/api/v1/auth/refresh").status_code == 401

        # Only the replayed refresh was refused; the other visitor is still in.
        client.cookies.clear()
        assert client.get("/api/v1/auth/me", headers=other_visitor).status_code == 200
        row = client.portal.call(_get_coro)
        assert row.token_version == demo_user.token_version

    def test_admin_cannot_change_its_role(self, client, demo_user, auth_headers):
        resp = client.patch(
            f"/api/v1/admin/users/{demo_user.id}/role",
            json={"role": "admin"},
            headers=auth_headers,
        )
        assert resp.status_code == 403
        row = client.portal.call(_get_coro)
        assert row.roles == ["attorney"]
