"""
Integration tests for the SystemRouter (/, /health, /ready, /metrics).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app import __version__

pytestmark = pytest.mark.usefixtures("no_rate_limit")


class TestSystemRouter:
    """Tests for system endpoints (/, /health, /ready, /metrics)."""

    # ==================== Root ====================

    def test_root_returns_api_info(self, client):
        """Root endpoint returns API info when no frontend build exists."""
        with patch("os.path.exists", return_value=False):
            resp = client.get("/")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "CaseCite"
            assert data["status"] == "running"
            assert data["version"] == __version__ == "1.3.0"

    # ==================== Health Check ====================

    def test_health_check(self, client):
        """Health check returns status."""
        with (
            patch("app.services.vectordb.get_vector_db") as mock_vdb,
            patch("app.database.async_engine") as mock_engine,
        ):
            mock_db = AsyncMock()
            mock_db.get_stats = AsyncMock(return_value={"total_chunks": 100})
            mock_vdb.return_value = mock_db

            mock_conn = AsyncMock()
            mock_conn.execute = AsyncMock()
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_engine.connect.return_value = mock_conn

            resp = client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert "status" in data
            assert "timestamp" in data
            # In debug mode, detailed components are always returned
            assert "components" in data

    def test_health_check_degraded(self, client):
        """Health check returns degraded when components fail."""
        with (
            patch("app.services.vectordb.get_vector_db", side_effect=RuntimeError("VDB down")),
            patch("app.database.async_engine") as mock_engine,
        ):
            # Router treats infrastructure errors (OperationalError/DBAPIError/OSError)
            # as a degraded component; other exceptions propagate.
            mock_conn = AsyncMock()
            mock_conn.execute = AsyncMock(side_effect=OSError("DB down"))
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_engine.connect.return_value = mock_conn

            resp = client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "degraded"

    # ==================== Readiness Check ====================

    def test_ready_check(self, client):
        """Readiness check returns ready when all services up."""
        with (
            patch("app.services.vectordb.get_vector_db") as mock_vdb,
            patch("app.database.async_engine") as mock_engine,
            patch("app.config.settings") as mock_settings,
        ):
            mock_settings.openai_api_key = "test-key"
            mock_settings.anthropic_api_key = None
            mock_settings.redis_url = None
            mock_settings.debug = True

            mock_db = AsyncMock()
            mock_db.get_stats = AsyncMock(return_value={"total_chunks": 0})
            mock_vdb.return_value = mock_db

            mock_conn = AsyncMock()
            mock_conn.execute = AsyncMock()
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_engine.connect.return_value = mock_conn

            resp = client.get("/ready")
            assert resp.status_code == 200
            assert resp.json()["ready"] is True

    def test_ready_check_not_ready(self, client):
        """Readiness check returns 503 when services down."""
        with (
            patch("app.services.vectordb.get_vector_db", return_value=None),
            patch("app.database.async_engine") as mock_engine,
            patch("app.config.settings") as mock_settings,
        ):
            mock_settings.openai_api_key = None
            mock_settings.anthropic_api_key = None
            mock_settings.redis_url = None
            mock_settings.debug = True

            # Readiness check catches infrastructure errors only (OSError etc.)
            mock_conn = AsyncMock()
            mock_conn.execute = AsyncMock(side_effect=OSError("DB down"))
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_engine.connect.return_value = mock_conn

            resp = client.get("/ready")
            assert resp.status_code == 503
            data = resp.json()
            assert data["ready"] is False
            assert len(data["issues"]) > 0

    # ==================== Metrics ====================

    def test_metrics_in_debug_mode(self, client):
        """Metrics endpoint is accessible in debug mode without auth."""
        with (
            patch("app.services.documents.document_service") as mock_docs,
            patch("app.services.vectordb.get_vector_db") as mock_vdb,
        ):
            mock_docs.documents = {}

            mock_db = AsyncMock()
            mock_db.get_stats = AsyncMock(return_value={"total_chunks": 50})
            mock_vdb.return_value = mock_db

            resp = client.get("/metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert "timestamp" in data
            assert "application" in data
            assert "documents" in data
            assert data["application"]["environment"] == "development"

    def test_metrics_returns_document_counts(self, client):
        """Metrics includes document statistics."""
        mock_doc = MagicMock()
        mock_doc.status = MagicMock(value="indexed")
        mock_doc.chunk_count = 10

        with (
            patch("app.services.documents.document_service") as mock_docs,
            patch("app.services.vectordb.get_vector_db") as mock_vdb,
        ):
            mock_docs.documents = {"doc1": mock_doc}

            mock_db = AsyncMock()
            mock_db.get_stats = AsyncMock(return_value={"total_chunks": 10})
            mock_vdb.return_value = mock_db

            resp = client.get("/metrics")
            assert resp.status_code == 200
            data = resp.json()
            assert data["documents"]["total_count"] == 1
            assert data["documents"]["indexed_count"] == 1
            assert data["documents"]["total_chunks"] == 10


class TestSystemRouterProduction:
    """/health and /metrics with DEBUG off — the token-gated branches, which
    debug mode short-circuits and the tests above therefore never reach."""

    @pytest.fixture(autouse=True)
    def _production(self, client):
        # `client` first: the app must finish starting before its services are mocked.
        from app.config import settings

        # Settings is frozen; swap the router's module-level reference for a copy.
        prod = settings.model_copy(update={"debug": False, "redis_url": None})
        with (
            patch("app.routers.system.settings", prod),
            patch("app.services.documents.document_service") as mock_docs,
            patch("app.services.vectordb.get_vector_db") as mock_vdb,
        ):
            mock_docs.documents = {}
            mock_db = AsyncMock()
            mock_db.get_stats = AsyncMock(return_value={"total_chunks": 0})
            mock_vdb.return_value = mock_db
            yield

    # ==================== Health Check ====================

    def test_health_is_minimal_without_token(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("healthy", "degraded")
        assert data["components"] is None
        # No version / build fingerprint for unauthenticated callers.
        assert data["version"] is None
        assert data["build"] is None
        assert data["environment"] is None
        assert data["started_at"]  # public: lets an operator spot multiple containers

    def test_health_shows_version_and_build_to_admin(self, client, auth_headers):
        data = client.get("/health", headers=auth_headers).json()
        assert data["version"] == __version__
        assert data["build"]
        assert data["environment"] == "production"
        assert data["components"]["llm"]["byok"] is True

    def test_health_accepts_the_session_cookie(self, client, auth_headers):
        """The browser session (httpOnly cookie) is honoured, not only a Bearer header."""
        token = auth_headers["Authorization"].removeprefix("Bearer ")
        client.cookies.set("access_token", token)
        try:
            resp = client.get("/health")
        finally:
            client.cookies.clear()
        assert resp.status_code == 200
        assert resp.json()["components"] is not None

    def test_health_is_healthy_without_a_server_llm_key(self, client):
        """BYOK-only is a supported configuration, not a degraded one."""
        from app.config import settings

        byok = settings.model_copy(
            update={
                "debug": False,
                "redis_url": None,
                "openai_api_key": None,
                "anthropic_api_key": None,
            }
        )
        with (
            patch("app.routers.system.settings", byok),
            patch("app.database.async_engine") as mock_engine,
        ):
            mock_conn = AsyncMock()
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_engine.connect.return_value = mock_conn
            assert client.get("/health").json()["status"] == "healthy"
            ready = client.get("/ready")
            assert ready.status_code == 200
            assert ready.json() == {"ready": True, "issues": None}

    def test_health_is_degraded_when_vector_db_is_missing(self, client):
        with patch("app.services.vectordb.get_vector_db", return_value=None):
            assert client.get("/health").json()["status"] == "degraded"

    def test_health_survives_an_unexpected_probe_error(self, client):
        """A driver-specific exception must degrade the probe, not 500 it."""

        class DriverError(Exception):
            pass

        with patch("app.services.vectordb.get_vector_db", side_effect=DriverError("boom")):
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "degraded"
            assert client.get("/ready").status_code == 503

    def test_health_reports_ocr_availability_to_admin(self, client, auth_headers):
        resp = client.get("/health", headers=auth_headers)
        ocr = resp.json()["components"]["ocr"]
        assert set(ocr) >= {"available", "rasterizer", "tesseract", "reason"}
        assert isinstance(ocr["available"], bool)

    def test_health_shows_components_to_admin(self, client, auth_headers):
        resp = client.get("/health", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["components"] is not None

    def test_health_hides_components_from_non_admin(self, client, non_admin_headers):
        resp = client.get("/health", headers=non_admin_headers)
        assert resp.status_code == 200
        assert resp.json()["components"] is None

    def test_health_survives_invalid_token(self, client):
        """A probe carrying a stale token still gets its 200, just no details."""
        resp = client.get("/health", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 200
        assert resp.json()["components"] is None

    # ==================== Metrics ====================

    def test_metrics_requires_token(self, client):
        assert client.get("/metrics").status_code == 401

    def test_metrics_rejects_invalid_token(self, client):
        resp = client.get("/metrics", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401

    def test_metrics_rejects_non_admin(self, client, non_admin_headers):
        assert client.get("/metrics", headers=non_admin_headers).status_code == 403

    def test_metrics_allows_admin(self, client, auth_headers):
        resp = client.get("/metrics", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["application"]["environment"] == "production"
        assert resp.json()["application"]["version"] == __version__

    def test_metrics_accepts_the_session_cookie(self, client, auth_headers):
        token = auth_headers["Authorization"].removeprefix("Bearer ")
        client.cookies.set("access_token", token)
        try:
            resp = client.get("/metrics")
        finally:
            client.cookies.clear()
        assert resp.status_code == 200

    def _admin(self):
        from datetime import UTC, datetime

        from app.services.auth import User, UserRole

        return User(
            id="sys-admin-no-session",
            email="sys-admin@casecite.legal",
            name="Admin",
            roles=[UserRole.ADMIN],
            last_login=datetime.now(UTC),
        )

    def test_metrics_rejects_admin_token_without_a_session(self, client):
        """A signed admin token whose session was ended (logout) must not work."""
        from app.services.auth import auth_service

        token = auth_service.create_access_token(self._admin())
        resp = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        # /health still answers, but without the admin-only detail.
        health = client.get("/health", headers={"Authorization": f"Bearer {token}"})
        assert health.status_code == 200
        assert health.json()["components"] is None

    def test_metrics_rejects_a_refresh_token(self, client):
        from app.services.auth import auth_service

        token = auth_service.create_refresh_token(self._admin())
        resp = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
