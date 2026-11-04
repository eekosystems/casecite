"""
Security Tests for CaseCite Platform

Tests critical security functionality including:
- Authentication and authorization
- Token management
- Input validation
- Rate limiting
- CSRF protection
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


class TestAuthentication:
    """Test authentication functionality."""

    def test_dev_login_requires_debug(self, client):
        """The dev login (/auth/demo/login) only works when DEBUG is true."""
        from app.config import Settings, settings
        from app.routers.auth import DEV_ACCOUNT_EMAIL

        # Production mode should reject the dev login
        # Must provide all required production settings so Settings() can be created
        with patch.dict(
            "os.environ",
            {
                "DEBUG": "false",
                "SECRET_KEY": "a" * 64,
                "ENCRYPTION_SALT": "b" * 32,
                "AUDIT_HMAC_KEY": "c" * 64,
                "REDIS_URL": "redis://localhost:6379/0",
                "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/db",
                "OPENAI_API_KEY": "sk-test",
                "DISK_ENCRYPTION_ACKNOWLEDGED": "true",
            },
        ):
            prod_settings = Settings()
            assert not prod_settings.debug

        # The dev login route must fail closed (403) unless debug is true.
        prod_like = MagicMock(wraps=settings)
        prod_like.debug = False
        # Bypass the login rate limiter for this request: earlier tests in the
        # suite legitimately consume the 3-per-5-min dev-login bucket and this
        # test asserts the 403 fail-closed gate, not the (separately tested)
        # 429 rate limit.
        with (
            patch("app.routers.auth.settings", prod_like),
            patch(
                "app.middleware.security.RateLimiter.is_allowed",
                return_value=(True, {"limit": 100, "remaining": 99}),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/demo/login",
                json={"email": DEV_ACCOUNT_EMAIL, "password": "demo"},
            )
            assert resp.status_code == 403

    def test_removed_demo_settings_are_ignored(self):
        """Stale DEMO_* / SALES_EMAIL keys in an old .env must not break startup."""
        from app.config import Settings

        with patch.dict(
            "os.environ",
            {
                "DEBUG": "true",
                "SECRET_KEY": "a" * 64,
                "ENCRYPTION_SALT": "b" * 32,
                "AUDIT_HMAC_KEY": "c" * 64,
                "OPENAI_API_KEY": "sk-test",
            },
            clear=True,
        ):
            # Removed keys arrive exactly the way a dotenv file delivers them.
            s = Settings(demo_mode="true", demo_daily_query_limit="5")  # type: ignore[call-arg]
            assert s.debug is True
            assert not hasattr(s, "demo_mode")

            # DEBUG is unaffected in the other direction too.
            s = Settings(
                debug="false",
                demo_mode="true",
                redis_url="redis://localhost:6379/0",
                database_url="postgresql+asyncpg://user:pass@localhost/db",
                disk_encryption_acknowledged=True,
            )  # type: ignore[call-arg]
            assert s.debug is False

    def test_secret_key_validation(self):
        """Secret key must be at least 32 characters in production."""
        from app.config import Settings

        # Short secret key should raise error in production
        with (
            pytest.raises(ValueError, match="SECRET_KEY"),
            patch.dict(
                "os.environ",
                {
                    "DEBUG": "false",
                    "SECRET_KEY": "tooshort",
                    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/db",
                    "REDIS_URL": "redis://localhost:6379/0",
                    "OPENAI_API_KEY": "sk-test",
                },
            ),
        ):
            Settings()

    def test_audit_hmac_key_minimum_length(self):
        """The audit chain uses the first 32 bytes of AUDIT_HMAC_KEY; shorter
        values silently weaken the tamper evidence and are refused."""
        from app.config import Settings

        with (
            pytest.raises(ValueError, match="AUDIT_HMAC_KEY must be at least 32"),
            patch.dict(
                "os.environ",
                {
                    "DEBUG": "false",
                    "SECRET_KEY": "a" * 64,
                    "ENCRYPTION_SALT": "b" * 32,
                    "AUDIT_HMAC_KEY": "audit",
                    "REDIS_URL": "redis://localhost:6379/0",
                    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/db",
                    "OPENAI_API_KEY": "sk-test",
                    "DISK_ENCRYPTION_ACKNOWLEDGED": "true",
                },
            ),
        ):
            Settings()

    def test_database_required_in_production(self):
        """DATABASE_URL must be set in production."""
        from app.config import Settings

        with (
            pytest.raises(ValueError, match="DATABASE_URL"),
            patch.dict(
                "os.environ",
                {
                    "DEBUG": "false",
                    "SECRET_KEY": "a" * 64,
                    "ENCRYPTION_SALT": "b" * 32,
                    "AUDIT_HMAC_KEY": "c" * 64,
                    "REDIS_URL": "redis://localhost:6379/0",
                    "OPENAI_API_KEY": "sk-test",
                },
                clear=True,
            ),
        ):
            Settings()

    def test_redis_required_in_production(self):
        """REDIS_URL must be set in production."""
        from app.config import Settings

        with (
            pytest.raises(ValueError, match="REDIS_URL"),
            patch.dict(
                "os.environ",
                {
                    "DEBUG": "false",
                    "SECRET_KEY": "a" * 64,
                    "ENCRYPTION_SALT": "b" * 32,
                    "AUDIT_HMAC_KEY": "c" * 64,
                    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/db",
                    "OPENAI_API_KEY": "sk-test",
                },
                clear=True,
            ),
        ):
            Settings()

    def test_disk_encryption_acknowledgement_required_in_production(self):
        """Production must fail closed until disk encryption is acknowledged."""
        from app.config import Settings

        base_env = {
            "DEBUG": "false",
            "SECRET_KEY": "a" * 64,
            "ENCRYPTION_SALT": "b" * 32,
            "AUDIT_HMAC_KEY": "c" * 64,
            "REDIS_URL": "redis://localhost:6379/0",
            "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/db",
            "OPENAI_API_KEY": "sk-test",
        }

        # Unacknowledged → refuse to start.
        with (
            pytest.raises(ValueError, match="UNENCRYPTED at rest"),
            patch.dict("os.environ", base_env, clear=True),
        ):
            Settings()

        # Acknowledged → boots normally.
        with patch.dict(
            "os.environ", {**base_env, "DISK_ENCRYPTION_ACKNOWLEDGED": "true"}, clear=True
        ):
            assert Settings().disk_encryption_acknowledged is True


class TestTokenManagement:
    """Test JWT token management."""

    def test_create_access_token(self):
        """Test access token creation."""
        from app.services.auth import User, UserRole, auth_service

        user = User(
            id="test-user-1",
            email="test@example.com",
            name="Test User",
            roles=[UserRole.ATTORNEY],
            last_login=datetime.now(UTC),
        )

        token = auth_service.create_access_token(user)
        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 50  # JWT should be reasonably long

    def test_verify_valid_token(self):
        """Test that valid tokens are verified correctly."""
        from app.services.auth import User, UserRole, auth_service

        user = User(
            id="test-user-1",
            email="test@example.com",
            name="Test User",
            roles=[UserRole.ATTORNEY],
            last_login=datetime.now(UTC),
        )

        token = auth_service.create_access_token(user)
        token_data = auth_service.verify_token(token)

        assert token_data is not None
        assert token_data.user_id == user.id
        assert token_data.email == user.email
        assert UserRole.ATTORNEY.value in token_data.roles

    def test_verify_invalid_token(self):
        """Test that invalid tokens are rejected."""
        from app.services.auth import auth_service

        with pytest.raises(HTTPException) as exc_info:
            auth_service.verify_token("invalid.token.here")

        assert exc_info.value.status_code == 401

    def test_token_revocation(self):
        """Test that revoked tokens are rejected."""
        from app.services.auth import User, UserRole, auth_service

        user = User(
            id="test-user-1",
            email="test@example.com",
            name="Test User",
            roles=[UserRole.ATTORNEY],
            last_login=datetime.now(UTC),
        )

        token = auth_service.create_access_token(user)
        token_data = auth_service.verify_token(token)

        # Revoke the token
        auth_service.revoke_token(token_data.jti)

        # Token should now be invalid
        with pytest.raises(HTTPException) as exc_info:
            auth_service.verify_token(token)

        assert exc_info.value.status_code == 401


class TestInputValidation:
    """Test input validation in schemas."""

    def test_chat_request_query_validation(self):
        """Test that ChatRequest validates query properly."""
        from app.models.schemas import ChatRequest

        # Empty query should fail
        with pytest.raises(ValueError):
            ChatRequest(query="")

        # Query with only whitespace should fail
        with pytest.raises(ValueError):
            ChatRequest(query="   ")

        # Valid query should pass
        request = ChatRequest(query="What is contract law?")
        assert request.query == "What is contract law?"

    def test_chat_request_strips_whitespace(self):
        """Test that query whitespace is stripped."""
        from app.models.schemas import ChatRequest

        request = ChatRequest(query="  What is tort law?  ")
        assert request.query == "What is tort law?"

    def test_chat_request_removes_control_characters(self):
        """Test that control characters are removed from query."""
        from app.models.schemas import ChatRequest

        request = ChatRequest(query="Test\x00query\x1fhere")
        assert "\x00" not in request.query
        assert "\x1f" not in request.query

    def test_conversation_id_validation(self):
        """Test conversation ID format validation."""
        from app.models.schemas import ChatRequest

        # Valid conversation ID
        request = ChatRequest(query="test", conversation_id="abc-123_XYZ")
        assert request.conversation_id == "abc-123_XYZ"

        # Invalid conversation ID (special characters)
        with pytest.raises(ValueError):
            ChatRequest(query="test", conversation_id="abc<script>")

    def test_rag_settings_validation(self):
        """Test RAG settings validation."""
        from app.models.schemas import RAGSettings

        # Valid settings
        settings = RAGSettings(similarity_threshold=0.7, top_k=20, temperature=0.5)
        assert settings.similarity_threshold == 0.7

        # Invalid similarity threshold (> 1.0)
        with pytest.raises(ValueError):
            RAGSettings(similarity_threshold=1.5)

        # Invalid temperature (> 2.0)
        with pytest.raises(ValueError):
            RAGSettings(temperature=3.0)

        # Invalid top_k (> 100)
        with pytest.raises(ValueError):
            RAGSettings(top_k=200)


class TestEncryption:
    """Test encryption service."""

    def test_encrypt_decrypt_roundtrip(self):
        """Test that encryption and decryption work correctly."""
        from app.services.encryption import EncryptionService

        service = EncryptionService()
        original = "sensitive data here"

        encrypted = service.encrypt_string(original)
        assert encrypted != original
        assert len(encrypted) > len(original)

        decrypted = service.decrypt_string(encrypted)
        assert decrypted == original

    def test_different_plaintexts_produce_different_ciphertexts(self):
        """Test that same plaintext doesn't always produce same ciphertext."""
        from app.services.encryption import EncryptionService

        service = EncryptionService()
        plaintext = "test data"

        ciphertext1 = service.encrypt_string(plaintext)
        ciphertext2 = service.encrypt_string(plaintext)

        # Due to random IV, same plaintext should produce different ciphertext
        assert ciphertext1 != ciphertext2


class TestAuditLogging:
    """Test audit logging functionality."""

    @pytest.mark.asyncio
    async def test_audit_log_creation(self):
        """Test that audit logs are created correctly."""
        from app.services.audit import AuditEventType, audit_service

        await audit_service.log_event(
            event_type=AuditEventType.LOGIN_SUCCESS,
            user_id="test-user-1",
            user_email="test@example.com",
            ip_address="192.168.1.1",
            details={"method": "password"},
        )

        # Verify log was created (implementation-specific)
        # This would need to check the actual log storage

    @pytest.mark.asyncio
    async def test_security_event_logging(self):
        """Test that security events are logged."""
        from app.services.audit import AuditEventType, audit_service

        await audit_service.log_event(
            event_type=AuditEventType.SECURITY_ALERT,
            user_id="test-user-1",
            ip_address="192.168.1.1",
            details={"reason": "brute_force_detected"},
            success=False,
        )


class TestFileValidation:
    """Test file upload validation."""

    def test_sanitize_filename_removes_path_traversal(self):
        """Test that path traversal attempts are blocked."""
        from app.routers.documents import sanitize_filename

        # Path traversal attempts should be sanitized
        assert sanitize_filename("../../../etc/passwd") == "passwd"
        assert sanitize_filename("..\\..\\windows\\system32") == "system32"

    def test_sanitize_filename_removes_control_characters(self):
        """Test that control characters are removed from filenames."""
        from app.routers.documents import sanitize_filename

        assert "\x00" not in sanitize_filename("test\x00file.pdf")
        assert "\n" not in sanitize_filename("test\nfile.pdf")

    def test_sanitize_filename_handles_dangerous_characters(self):
        """Test that dangerous characters are replaced."""
        from app.routers.documents import sanitize_filename

        sanitized = sanitize_filename('test<script>alert("xss")</script>.pdf')
        assert "<" not in sanitized
        assert ">" not in sanitized

    def test_validate_file_content_detects_mismatch(self):
        """Test that file content validation detects mismatches."""
        from app.routers.documents import validate_file_content

        # PDF magic bytes
        pdf_content = b"%PDF-1.4 some content"
        is_valid, detected, error = validate_file_content(
            pdf_content, "application/pdf", "test.pdf"
        )
        assert is_valid

        # Mismatch: PDF content claimed as DOCX
        is_valid, detected, error = validate_file_content(
            pdf_content,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "test.docx",
        )
        assert not is_valid


class TestErrorHandling:
    """Test that errors don't leak sensitive information."""

    def test_error_handler_hides_internal_details(self):
        """The router error helper returns a safe message, never the exception text."""
        import logging

        from app.utils.error_handler import handle_service_error

        internal_exception = OSError("Database connection failed: password=secret123")

        response = handle_service_error(
            internal_exception, "Failed to process document", logging.getLogger("test")
        )

        assert response.status_code == 500
        assert response.detail == "Failed to process document. Please try again."
        assert "secret123" not in response.detail
        assert "password" not in response.detail.lower()


class TestMultiWorkerGuard:
    """More than one worker process is allowed only on the footing that makes
    the once process-local stores shared: PostgreSQL, Redis and a vector
    store that is not the embedded Chroma client. Anything less is refused
    with the reasons."""

    def test_sqlite_chroma_and_no_redis_are_each_named(self):
        import os

        from app.main import check_single_process_deployment, settings

        guarded = MagicMock(wraps=settings)
        guarded.vector_db = "chroma"
        guarded.redis_url = None
        with (
            patch("app.main.settings", guarded),
            patch("app.database.IS_POSTGRES", False),
            patch.dict(os.environ, {"UVICORN_WORKERS": "2"}),
            pytest.raises(RuntimeError) as excinfo,
        ):
            check_single_process_deployment()
        message = str(excinfo.value)
        assert "UVICORN_WORKERS=2 needs" in message
        assert "PostgreSQL" in message
        assert "REDIS_URL" in message
        assert "embedded Chroma" in message
        assert "UVICORN_WORKERS=1" in message

    def test_the_shared_footing_allows_workers(self):
        import os

        from app.main import check_single_process_deployment, settings

        guarded = MagicMock(wraps=settings)
        guarded.vector_db = "pinecone"
        guarded.redis_url = "redis://redis:6379/0"
        with (
            patch("app.main.settings", guarded),
            patch("app.database.IS_POSTGRES", True),
            patch.dict(os.environ, {"UVICORN_WORKERS": "4"}),
        ):
            check_single_process_deployment()  # must not raise

    def test_pinecone_alone_is_not_enough(self):
        import os

        from app.main import check_single_process_deployment, settings

        guarded = MagicMock(wraps=settings)
        guarded.vector_db = "pinecone"
        guarded.redis_url = None
        with (
            patch("app.main.settings", guarded),
            patch("app.database.IS_POSTGRES", True),
            patch.dict(os.environ, {"UVICORN_WORKERS": "2"}),
            pytest.raises(RuntimeError) as excinfo,
        ):
            check_single_process_deployment()
        assert "REDIS_URL" in str(excinfo.value)
        assert "PostgreSQL" not in str(excinfo.value)

    @pytest.mark.parametrize("workers", ["", "1", " 1 "])
    def test_single_worker_is_accepted(self, workers):
        from app.main import check_single_process_deployment

        check_single_process_deployment(workers)  # must not raise

    def test_unset_env_is_accepted(self):
        import os

        from app.main import check_single_process_deployment

        env = {k: v for k, v in os.environ.items() if k != "UVICORN_WORKERS"}
        with patch.dict(os.environ, env, clear=True):
            check_single_process_deployment()


class TestInstanceLock:
    """acquire_instance_lock refuses a second process on the same data dir
    (the UVICORN_WORKERS check cannot see gunicorn or a second replica)."""

    @pytest.fixture(autouse=True)
    def _release(self):
        from app import main

        main.release_instance_lock()
        yield
        main.release_instance_lock()

    def test_first_holder_acquires_and_is_reentrant(self, tmp_path):
        from app import main

        main.acquire_instance_lock(tmp_path)
        main.acquire_instance_lock(tmp_path)  # same process: no error
        main.release_instance_lock()
        assert main._instance_lock_handle is None

    def test_second_process_is_refused(self, tmp_path):
        pytest.importorskip("fcntl", reason="advisory file locks are POSIX-only")
        import subprocess
        import sys
        import textwrap

        from app import main

        lock_path = tmp_path / ".instance.lock"
        # Another process takes the lock and holds it until stdin closes.
        holder = subprocess.Popen(  # noqa: S603 - fixed interpreter and script
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import fcntl, sys
                    fh = open({str(lock_path)!r}, "a+")
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    print("locked", flush=True)
                    sys.stdin.read()
                    """
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "locked"
            with pytest.raises(RuntimeError, match="already holds"):
                main.acquire_instance_lock(tmp_path)
            assert main._instance_lock_handle is None
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

        # Once the other process is gone the lock is free again.
        main.acquire_instance_lock(tmp_path)


class TestRateLimitBuckets:
    """Limits are counted per route, not per literal URL: identifier segments
    are collapsed so every document/job/session id draws on ONE quota."""

    @staticmethod
    def _limiter():
        from app.middleware.security import RateLimiter

        return RateLimiter()

    def test_ids_are_collapsed(self):
        from app.middleware.security import rate_limit_bucket

        assert (
            rate_limit_bucket("/api/v1/documents/3f2b8c1e-1111-2222-3333-444455556666/file")
            == "/api/v1/documents/{id}/file"
        )
        assert rate_limit_bucket("/api/v1/contract-analysis/analyses/42/export") == (
            "/api/v1/contract-analysis/analyses/{id}/export"
        )
        assert rate_limit_bucket("/api/v1/jobs/0123456789abcdef0123") == "/api/v1/jobs/{id}"

    def test_route_names_are_not_collapsed(self):
        from app.middleware.security import rate_limit_bucket

        for path in (
            "/api/v1/auth/mfa/recovery-codes",
            "/api/v1/auth/verify-reset-token",
            "/api/v1/workspace-sessions",
            "/api/v1/contract-analysis/draft/generate",
        ):
            assert rate_limit_bucket(path) == path

    def test_different_ids_share_one_quota(self):
        import uuid

        limiter = self._limiter()
        limit, _window = limiter._get_limit("/api/v1/documents/x")
        allowed = 0
        for _ in range(limit + 10):
            ok, _info = limiter.is_allowed("203.0.113.7", f"/api/v1/documents/{uuid.uuid4()}")
            allowed += ok
        assert allowed == limit

    def test_other_clients_and_routes_are_unaffected(self):
        import uuid

        limiter = self._limiter()
        limit, _window = limiter._get_limit("/api/v1/documents/x")
        for _ in range(limit):
            limiter.is_allowed("203.0.113.8", f"/api/v1/documents/{uuid.uuid4()}")
        assert limiter.is_allowed("203.0.113.8", f"/api/v1/documents/{uuid.uuid4()}")[0] is False
        assert limiter.is_allowed("203.0.113.9", f"/api/v1/documents/{uuid.uuid4()}")[0] is True
        assert limiter.is_allowed("203.0.113.8", "/api/v1/settings/rag")[0] is True

    def test_most_specific_rule_wins(self):
        limiter = self._limiter()
        limiter.limits = {
            "default": (100, 60),
            "/api/v1/auth": (50, 60),
            "/api/v1/auth/login": (3, 300),
        }
        assert limiter._get_limit("/api/v1/auth/login") == (3, 300)
        assert limiter._get_limit("/api/v1/auth/me") == (50, 60)
        assert limiter._get_limit("/api/v1/auth/login-history") == (50, 60)
        assert limiter._get_limit("/api/v1/other") == (100, 60)

    def test_every_rule_names_a_real_route(self, app):
        """A rule for a route that does not exist is dead configuration."""
        limiter = self._limiter()
        paths = set(app.openapi()["paths"])
        for pattern in limiter.limits:
            if pattern == "default":
                continue
            assert any(p == pattern or p.startswith(pattern + "/") for p in paths), pattern

    def test_job_polling_has_its_own_generous_bucket(self):
        """The client polls every 2 s per tab and the limit is per IP: two tabs
        or an office behind one NAT must not be throttled into "job failed"."""
        limiter = self._limiter()
        limit, window = limiter._get_limit("/api/v1/jobs/0123456789abcdef0123")
        assert (limit, window) == (300, 60)
        # Five tabs polling for a full minute from one address all get through.
        for _ in range(5 * 30):
            ok, _info = limiter.is_allowed("203.0.113.20", "/api/v1/jobs/0123456789abcdef0123")
            assert ok is True
        # Everything else keeps its own, stricter rule.
        assert limiter._get_limit("/api/v1/chat") == (20, 60)

    def test_mfa_management_endpoints_are_tightly_limited(self):
        limiter = self._limiter()
        for path in (
            "/api/v1/auth/mfa/verify",
            "/api/v1/auth/mfa/enable",
            "/api/v1/auth/mfa/disable",
            "/api/v1/auth/mfa/recovery-codes",
        ):
            assert limiter._get_limit(path) == (5, 300), path


class TestEnvironmentDebugContradiction:
    def test_production_environment_with_debug_is_refused(self):
        from app.config import Settings

        with (
            patch.dict("os.environ", {"DEBUG": "true", "ENVIRONMENT": "production"}),
            pytest.raises(ValueError, match="ENVIRONMENT is production but DEBUG=true"),
        ):
            Settings()

    def test_development_environment_with_debug_is_fine(self):
        from app.config import Settings

        with patch.dict("os.environ", {"DEBUG": "true", "ENVIRONMENT": "development"}):
            assert Settings().debug is True
