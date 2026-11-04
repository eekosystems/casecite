"""
System Router - Health and Metrics Endpoints

Provides:
- Root endpoint (API info / frontend redirect)
- Health check
- Readiness check (Kubernetes)
- Prometheus-compatible metrics
"""

import asyncio
import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.config import settings
from app.exceptions import AppError, ForbiddenError, ServiceUnavailableError
from app.models.responses.system import (
    HealthResponse,
    MetricsResponse,
    ReadyResponse,
)
from app.services.auth import TokenData, UserRole, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"])

# Build/process fingerprint. BUILD_SHA = git sha baked into the image at build
# (Coolify passes SOURCE_COMMIT); started_at = when THIS process booted.
# started_at is public so "which container answered me?" is checkable from a
# browser: refresh /health — a changing started_at means multiple containers
# are serving one URL, which the embedded vector store cannot support. The
# release version and build sha are shown to admins only (no reconnaissance).
BUILD_SHA = os.environ.get("SOURCE_COMMIT", os.environ.get("GIT_SHA", "unknown"))[:12]
PROCESS_STARTED_AT = datetime.now(UTC).isoformat()

# Bound for blocking probes moved off the event loop via asyncio.to_thread.
_PROBE_TIMEOUT_SECONDS = 5.0


async def _authenticate(request: Request) -> TokenData:
    """Authenticate the caller exactly as every other endpoint does.

    Delegates to ``get_current_user`` (Bearer header, else the httpOnly cookie)
    so the token-type, tombstone, is_active, password-change and session checks
    all apply — these endpoints must not be a weaker door than the rest of the
    API. Raises HTTPException(401) when the caller is not authenticated.
    """
    credentials = None
    auth_header = request.headers.get("authorization", "")
    if auth_header[:7].lower() == "bearer ":
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth_header[7:])
    return await get_current_user(request, credentials)


def _is_admin(token_data: TokenData) -> bool:
    return UserRole.ADMIN.value in (token_data.roles or [])


def _redis_ping() -> None:
    """Blocking Redis connectivity probe (call via asyncio.to_thread)."""
    import redis

    r = redis.from_url(settings.redis_url, socket_timeout=2)
    r.ping()


def _redis_memory_info() -> dict:
    """Blocking Redis INFO memory probe (call via asyncio.to_thread)."""
    import redis

    r = redis.from_url(settings.redis_url, socket_timeout=2)
    return r.info("memory")


@router.get("/", response_model=None)
async def root() -> dict | FileResponse:
    """Root endpoint - serves frontend if built, otherwise returns API info."""
    _index = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "frontend",
        "dist",
        "index.html",
    )
    if os.path.exists(_index):
        return FileResponse(_index)
    return {
        "name": settings.app_name,
        "version": __version__,
        "status": "running",
        "docs": "/docs" if settings.debug else "disabled",
    }


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> dict:
    """
    Health check endpoint.

    Used by load balancers and monitoring.

    In production mode the public response is minimal (status, timestamp and
    process start time). The release version, build and component breakdown are
    returned only to an authenticated admin (Bearer token or session cookie),
    preventing reconnaissance by unauthenticated parties. In debug mode full
    details are always returned for convenience.
    """
    from app.services.vectordb import get_vector_db

    overall_healthy = True

    # --- Run component checks (always, for status determination) ---
    # A probe must never 500: any failure, whatever the driver raises, means
    # "this component is down" — hence the deliberately broad excepts below.

    # Vector DB
    vector_db_ok = True
    try:
        db = get_vector_db()
        if db is None:
            vector_db_ok = False
        else:
            await db.get_stats()
    except Exception as e:
        logger.debug("Health check: vector database unavailable: %s", e)
        vector_db_ok = False
    if not vector_db_ok:
        overall_healthy = False

    # Database
    db_ok = True
    try:
        from sqlalchemy import text

        from app.database import async_engine

        async def _db_probe() -> None:
            async with async_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        await asyncio.wait_for(_db_probe(), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.debug("Health check: database unreachable: %s", e)
        db_ok = False
        overall_healthy = False

    # Redis
    redis_ok = True
    redis_configured = bool(settings.redis_url)
    if redis_configured:
        try:
            # Sync client — run off the event loop with a hard bound.
            await asyncio.wait_for(asyncio.to_thread(_redis_ping), timeout=_PROBE_TIMEOUT_SECONDS)
        except Exception as e:
            logger.debug("Health check: redis unreachable: %s", e)
            redis_ok = False
            overall_healthy = False

    # Row-level security: PostgreSQL only; None on SQLite. "enforced" is False
    # when the app's database role is a superuser (which RLS exempts).
    rls_info: dict = {"enforced": None}
    if db_ok:
        try:
            from app.database import IS_POSTGRES, get_db_context
            from app.rls import rls_status

            if IS_POSTGRES:
                async with get_db_context() as _db:
                    rls_info = await asyncio.wait_for(
                        rls_status(_db), timeout=_PROBE_TIMEOUT_SECONDS
                    )
        except Exception as e:
            logger.debug("Health check: row-level security status unavailable: %s", e)
            rls_info = {"enforced": False, "error": type(e).__name__}

    # Role permission policy. Served from the stored matrix normally; from the
    # last-known-good snapshot, or locked permissions only, when the store is
    # unreadable (see app/services/permissions.py). Either degraded state is
    # security-relevant — an admin's restrictions may not be the ones in
    # force, or users may be locked out — so it degrades the instance.
    policy: dict = {"healthy": False, "source": "unknown", "error": None}
    try:
        from app.services import permissions as perm

        policy = await asyncio.wait_for(perm.policy_status(), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.debug("Health check: role permission policy unavailable: %s", e)
        policy["error"] = type(e).__name__
    if not policy.get("healthy"):
        overall_healthy = False

    # LLM — informational only. A server-level key is optional: this is a BYOK
    # product (see Settings validation in app/config.py), so a missing server
    # key is a valid configuration and must not report the instance unhealthy.
    llm_server_key = bool(settings.openai_api_key or settings.anthropic_api_key)

    status = "healthy" if overall_healthy else "degraded"

    # --- Determine if caller is authenticated admin ---
    is_admin = False
    if settings.debug:
        is_admin = True  # Always show details in local development
    elif request.headers.get("authorization") or request.cookies.get("access_token"):
        try:
            is_admin = _is_admin(await _authenticate(request))
        # A probe carrying a stale token (401) — or arriving while the database
        # the auth check needs is down — must still get its 200, minus details.
        except (
            HTTPException,
            AppError,
            SQLAlchemyError,
            OSError,
            ValueError,
            KeyError,
            AttributeError,
        ):
            is_admin = False

    # --- Build response ---
    response: dict = {
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
        "started_at": PROCESS_STARTED_AT,
    }

    if is_admin:
        # Version, build and component breakdown (admin / dev only)
        response["version"] = __version__
        response["build"] = BUILD_SHA
        response["environment"] = "production" if not settings.debug else "development"
        response["components"] = {
            "vector_db": {"healthy": vector_db_ok},
            "database": {"healthy": db_ok},
            "redis": {"healthy": redis_ok, "configured": redis_configured},
            "llm": {"configured": llm_server_key, "byok": True},
            "role_permissions": policy,
            "row_level_security": rls_info,
        }

    return response


@router.get("/ready", response_model=ReadyResponse)
async def readiness_check() -> dict | JSONResponse:
    """
    Readiness check for Kubernetes.

    Returns 200 only when app is ready to serve traffic.
    Checks all required services are available.
    """
    issues = []
    # /ready is unauthenticated and both proxies expose it, so raw exception
    # text (driver errors, internal hostnames, Redis addresses) must not leak to
    # clients in production. Detail is logged; the response carries generic codes.
    expose_detail = settings.debug

    def _issue(label: str, err: Exception | None = None) -> str:
        if err is not None:
            logger.warning("Readiness check — %s: %s", label, err)
        return f"{label}: {err}" if (expose_detail and err is not None) else label

    # No LLM-provider check: a server-level key is optional (BYOK — users supply
    # their own keys), so its absence must not take the instance out of rotation.

    # Check database connectivity
    try:
        from sqlalchemy import text

        from app.database import async_engine

        async def _db_probe() -> None:
            async with async_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        await asyncio.wait_for(_db_probe(), timeout=_PROBE_TIMEOUT_SECONDS)
    # Broad on purpose (here and below): whatever a driver raises, the probe
    # reports "not ready" instead of 500-ing.
    except Exception as e:
        issues.append(_issue("Database connection failed", e))

    # Check vector database
    try:
        from app.services.vectordb import get_vector_db

        db = get_vector_db()
        if db is not None:
            await db.get_stats()
        else:
            issues.append(_issue("Vector database not initialized"))
    except Exception as e:
        issues.append(_issue("Vector database failed", e))

    # Check Redis connectivity (required in production)
    if settings.redis_url and not settings.debug:
        try:
            # Sync client — run off the event loop with a hard bound.
            await asyncio.wait_for(asyncio.to_thread(_redis_ping), timeout=_PROBE_TIMEOUT_SECONDS)
        except Exception as e:
            issues.append(_issue("Redis connection failed", e))

    if issues:
        return JSONResponse(
            status_code=503,
            content={"ready": False, "issues": issues},
        )

    return {"ready": True}


@router.get("/metrics", response_model=MetricsResponse)
async def metrics(request: Request) -> dict:
    """
    Prometheus-compatible metrics endpoint.

    Returns key application metrics for monitoring and alerting.
    Requires an authenticated admin in production mode.
    """
    # Require an authenticated admin in production — same validation as every
    # other endpoint (Bearer token or session cookie; see _authenticate).
    if not settings.debug:
        try:
            token_data = await _authenticate(request)
        except (SQLAlchemyError, OSError) as e:
            logger.warning("Metrics auth check failed — database unavailable: %s", e)
            raise ServiceUnavailableError("Service temporarily unavailable")
        if not _is_admin(token_data):
            raise ForbiddenError("Admin access required")

    from app.services.documents import document_service
    from app.services.vectordb import get_vector_db

    metrics_data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "application": {
            "name": settings.app_name,
            "version": __version__,
            "build": BUILD_SHA,
            "environment": "production" if not settings.debug else "development",
        },
        "documents": {
            "total_count": len(document_service.documents),
            "indexed_count": sum(
                1 for d in document_service.documents.values() if d.status.value == "indexed"
            ),
            "failed_count": sum(
                1 for d in document_service.documents.values() if d.status.value == "failed"
            ),
            "total_chunks": sum(d.chunk_count for d in document_service.documents.values()),
        },
    }

    # Vector DB stats
    try:
        db = get_vector_db()
        if db is not None:
            db_stats = await db.get_stats()
            metrics_data["vector_db"] = {
                "type": settings.vector_db,
                "total_vectors": db_stats.get("total_chunks", 0),
                "healthy": True,
            }
        else:
            metrics_data["vector_db"] = {"healthy": False, "error": "not initialized"}
    except Exception as e:
        metrics_data["vector_db"] = {"healthy": False, "error": str(e)}

    # Database stats
    try:
        from app.database import IS_POSTGRES, async_engine

        metrics_data["database"] = {
            "type": "postgresql" if IS_POSTGRES else "sqlite",
            "pool_size": async_engine.pool.size() if hasattr(async_engine.pool, "size") else "N/A",
        }
    except (AttributeError, ImportError, RuntimeError):
        metrics_data["database"] = {"type": "unknown"}

    # Redis stats (if configured)
    if settings.redis_url:
        try:
            # Sync client — run off the event loop with a hard bound.
            info = await asyncio.wait_for(
                asyncio.to_thread(_redis_memory_info), timeout=_PROBE_TIMEOUT_SECONDS
            )
            metrics_data["redis"] = {
                "connected": True,
                "used_memory_mb": round(info.get("used_memory", 0) / 1024 / 1024, 2),
            }
        except Exception as e:
            logger.debug("Metrics: redis unreachable: %s", e)
            metrics_data["redis"] = {"connected": False}

    return metrics_data
