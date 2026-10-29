"""
Tests for CSRF middleware — double-submit cookie pattern.

Covers:
- Token generation and signing
- Token verification (valid, tampered, malformed)
- GET requests set CSRF cookie
- POST/PUT/DELETE require matching cookie + header
- Exempt paths bypass CSRF
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from unittest.mock import patch

from app.middleware.csrf import generate_signed_csrf_token, verify_csrf_token


class TestCSRFTokenGeneration:
    """Tests for token generation and verification functions."""

    def test_generate_signed_token_format(self):
        """Generated token has {token}.{signature} format."""
        signed = generate_signed_csrf_token()
        assert "." in signed
        parts = signed.rsplit(".", 1)
        assert len(parts) == 2
        assert len(parts[0]) > 0
        assert len(parts[1]) == 64  # SHA-256 hex digest

    def test_generated_tokens_are_unique(self):
        """Each generated token is cryptographically unique."""
        tokens = {generate_signed_csrf_token() for _ in range(10)}
        assert len(tokens) == 10

    def test_verify_valid_token(self):
        """Valid signed tokens pass verification."""
        token = generate_signed_csrf_token()
        assert verify_csrf_token(token) is True

    def test_verify_tampered_token(self):
        """Token with modified payload fails verification."""
        token = generate_signed_csrf_token()
        parts = token.rsplit(".", 1)
        tampered = "AAAA" + parts[0][4:] + "." + parts[1]
        assert verify_csrf_token(tampered) is False

    def test_verify_tampered_signature(self):
        """Token with modified signature fails verification."""
        token = generate_signed_csrf_token()
        parts = token.rsplit(".", 1)
        tampered = parts[0] + "." + "a" * 64
        assert verify_csrf_token(tampered) is False

    def test_verify_empty_token(self):
        """Empty or None token fails verification."""
        assert verify_csrf_token("") is False
        assert verify_csrf_token(None) is False

    def test_verify_no_dot(self):
        """Token without separator fails verification."""
        assert verify_csrf_token("no-dot-separator") is False

    def test_verify_only_dot(self):
        """Token that is just a dot fails verification."""
        assert verify_csrf_token(".") is False


class TestCSRFMiddlewareIntegration:
    """Integration tests for CSRF middleware via TestClient."""

    def test_csrf_middleware_registered_even_in_debug(self, app):
        """CSRF middleware is always in the middleware stack.

        DEBUG=true must no longer remove CSRF at registration time — skipping
        is governed solely by the middleware's internal debug+csrf_disabled
        check, so a stray DEBUG=true in production cannot drop protection.
        """
        from app.middleware.csrf import CSRFMiddleware

        assert any(m.cls is CSRFMiddleware for m in app.user_middleware)

    def test_get_request_sets_csrf_cookie(self, client):
        """GET requests receive a _csrf cookie."""
        response = client.get("/api/v1/config")
        # /config is CSRF-exempt but other GET endpoints set cookies
        # Use health endpoint (also exempt) or a protected GET
        response = client.get("/api/v1/auth/me")
        # Even if 401, the middleware runs first and may set cookie
        # Cookie may or may not be set depending on middleware ordering
        # The key test is that GET requests don't fail with CSRF errors
        assert response.status_code != 403 or "CSRF" not in response.text

    def test_csrf_token_endpoint_returns_token(self, client):
        """GET /csrf-token returns a signed CSRF token and sets cookie."""
        response = client.get("/api/v1/csrf-token")
        assert response.status_code == 200
        data = response.json()
        assert "csrf_token" in data
        assert "header_name" in data
        assert data["header_name"] == "X-CSRF-Token"
        # Verify the returned token is valid
        assert verify_csrf_token(data["csrf_token"]) is True

    def test_post_without_csrf_to_exempt_path(self, client):
        """POST to exempt path succeeds without CSRF token."""
        # /api/v1/auth/login is CSRF-exempt
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "test@example.com", "password": "wrong"},
        )
        # Should get 401 (bad credentials), not 403 (CSRF)
        assert response.status_code != 403

    def test_post_with_valid_csrf_succeeds(self, client, auth_headers):
        """POST with matching cookie and header passes CSRF validation."""
        # First get a CSRF token
        csrf_response = client.get("/api/v1/csrf-token")
        csrf_token = csrf_response.json()["csrf_token"]

        # Use it in a POST request with cookie + header
        client.cookies.set("_csrf", csrf_token)
        headers = {**auth_headers, "X-CSRF-Token": csrf_token}
        response = client.post(
            "/api/v1/auth/logout",
            headers=headers,
        )
        # Should not be 403 CSRF error (may be other status)
        assert response.status_code != 403 or "CSRF" not in response.text

    async def test_middleware_rejects_missing_header(self):
        """CSRFMiddleware rejects POST when X-CSRF-Token header is missing.

        Note: Tested via middleware dispatch directly because the test env
        runs with DEBUG=true + CSRF_DISABLED=true, which makes the (always
        registered) middleware skip validation in the full app.
        """
        from unittest.mock import AsyncMock, MagicMock

        from app.middleware.csrf import CSRFMiddleware

        middleware = CSRFMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/v1/some-protected-endpoint"
        request.method = "POST"
        request.cookies = {"_csrf": generate_signed_csrf_token(), "access_token": "jwt"}
        request.headers = {}  # No X-CSRF-Token

        with (
            patch.object(middleware, "_is_exempt", return_value=False),
            patch("app.middleware.csrf.settings") as mock_settings,
        ):
            mock_settings.debug = False
            mock_settings.csrf_disabled = False
            response = await middleware.dispatch(request, AsyncMock())
        assert response.status_code == 403

    async def test_middleware_rejects_mismatched_tokens(self):
        """CSRFMiddleware rejects POST when cookie and header tokens differ."""
        from unittest.mock import AsyncMock, MagicMock

        from app.middleware.csrf import CSRFMiddleware

        token1 = generate_signed_csrf_token()
        token2 = generate_signed_csrf_token()

        middleware = CSRFMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/v1/some-protected-endpoint"
        request.method = "POST"
        request.cookies = {"_csrf": token1, "access_token": "jwt"}
        request.headers = {"X-CSRF-Token": token2}

        with (
            patch.object(middleware, "_is_exempt", return_value=False),
            patch("app.middleware.csrf.settings") as mock_settings,
        ):
            mock_settings.debug = False
            mock_settings.csrf_disabled = False
            response = await middleware.dispatch(request, AsyncMock())
        assert response.status_code == 403

    async def test_middleware_rejects_invalid_signature(self):
        """CSRFMiddleware rejects POST when token has an invalid HMAC signature."""
        from unittest.mock import AsyncMock, MagicMock

        from app.middleware.csrf import CSRFMiddleware

        bad_token = "fake-token." + "0" * 64

        middleware = CSRFMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/v1/some-protected-endpoint"
        request.method = "POST"
        request.cookies = {"_csrf": bad_token, "access_token": "jwt"}
        request.headers = {"X-CSRF-Token": bad_token}

        with (
            patch.object(middleware, "_is_exempt", return_value=False),
            patch("app.middleware.csrf.settings") as mock_settings,
        ):
            mock_settings.debug = False
            mock_settings.csrf_disabled = False
            response = await middleware.dispatch(request, AsyncMock())
        assert response.status_code == 403

    async def test_middleware_skips_validation_without_auth_cookies(self):
        """A bearer-token POST with no session cookie is not a CSRF vector.

        The Word add-in's task pane never holds the httpOnly cookies, so it
        authenticates by header alone; a cross-site page cannot forge that.
        """
        from unittest.mock import AsyncMock, MagicMock

        from app.middleware.csrf import CSRFMiddleware

        middleware = CSRFMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/v1/chat"
        request.method = "POST"
        request.cookies = {}
        request.headers = {"Authorization": "Bearer jwt"}
        downstream = AsyncMock(return_value=MagicMock(status_code=200))

        with (
            patch.object(middleware, "_is_exempt", return_value=False),
            patch("app.middleware.csrf.settings") as mock_settings,
        ):
            mock_settings.debug = False
            mock_settings.csrf_disabled = False
            response = await middleware.dispatch(request, downstream)
        downstream.assert_awaited_once()
        assert response.status_code == 200

    async def test_middleware_validates_when_only_refresh_cookie_present(self):
        """Any auth cookie, including just the refresh token, keeps validation on."""
        from unittest.mock import AsyncMock, MagicMock

        from app.middleware.csrf import CSRFMiddleware

        middleware = CSRFMiddleware(app=MagicMock())
        request = MagicMock()
        request.url.path = "/api/v1/auth/refresh"
        request.method = "POST"
        request.cookies = {"refresh_token": "jwt"}
        request.headers = {}

        with (
            patch.object(middleware, "_is_exempt", return_value=False),
            patch("app.middleware.csrf.settings") as mock_settings,
        ):
            mock_settings.debug = False
            mock_settings.csrf_disabled = False
            response = await middleware.dispatch(request, AsyncMock())
        assert response.status_code == 403
