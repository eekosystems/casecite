"""
CaseCite - Main Application

Self-hosted legal research platform: FastAPI app, middleware, routers, and
startup/shutdown lifecycle.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app import __version__
from app.config import settings
from app.database import close_db as close_sqlalchemy_db
from app.database import init_db as init_sqlalchemy_db
from app.error_handlers import register_error_handlers
from app.frontend_routes import mount_frontend
from app.logging_config import configure_logging
from app.middleware.csrf import get_csrf_router
from app.middleware.middleware_config import configure_middleware
from app.routers import (
    addin,
    audit,
    auth,
    branding,
    chat,
    chat_sessions,
    connectors,
    contract_analysis,
    deep_research,
    documents,
    integrations,
    jobs,
    judge_intel,
    legal_docs,
    matters,
    mfa,
    pickers,
    review_tables,
    strategy,
    system,
    tools,
    user_keys,
    users,
    workflows,
    workspace_sessions,
)
from app.routers import settings as settings_router
from app.services.job_queue import job_manager

logger = logging.getLogger(__name__)

# Structured logging with request-id correlation. Plain text in debug for
# readability; JSON in production for ingestion by log tooling.
configure_logging(
    level="DEBUG" if settings.debug else "INFO",
    json_format=not settings.debug,
)

# Run audit-log retention once per day.
_AUDIT_RETENTION_INTERVAL_SECONDS = 24 * 60 * 60


async def _audit_retention_loop() -> None:
    """Periodically compress/delete audit logs per settings.audit_retention_days.

    Runs once shortly after startup, then daily. Retention is enforced here
    because the audit service exposes rotate_old_logs() but nothing scheduled it,
    so logs previously grew unbounded (SOC 2 CC7.2 retention).
    """
    from app.services.audit import audit_service

    while True:
        try:
            result = await audit_service.rotate_old_logs(
                retention_days=settings.audit_retention_days
            )
            logger.info("Audit-log retention run: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # retention must never crash the app
            logger.warning("Audit-log retention failed (non-fatal): %s", e)
        await asyncio.sleep(_AUDIT_RETENTION_INTERVAL_SECONDS)


async def _retention_loop() -> None:
    """Apply matter retention policies once shortly after startup, then daily.

    Every removal goes through the normal delete path (holds, tracked
    deletion, registry rules) and is audited as retention_expired.
    """
    from app.database import get_db_context
    from app.services.governance import run_retention

    while True:
        try:
            async with get_db_context() as db:
                result = await run_retention(db)
            if result.get("matters"):
                logger.info("Matter retention run: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # retention must never crash the app
            logger.warning("Matter retention failed (non-fatal): %s", e)
        await asyncio.sleep(_AUDIT_RETENTION_INTERVAL_SECONDS)


def multi_process_requirements() -> list[str]:
    """What a multi-worker / multi-replica deployment still lacks (empty = ready).

    Since migration 024 the stores that were process-local are shared: the document
    registry and the shared version counters live in the database, sessions
    live in Redis, the permission matrix, legal holds and connector
    credentials reload when another process changes them, and the audit
    chain is serialised through a lock file. What remains is the
    infrastructure those rely on, and a vector store that is not an
    embedded, single-process one.
    """
    from app.database import IS_POSTGRES

    missing: list[str] = []
    if not IS_POSTGRES:
        missing.append("a PostgreSQL DATABASE_URL (SQLite is single-process)")
    if not settings.redis_url:
        missing.append("REDIS_URL (sessions, revocations and rate limits are shared through Redis)")
    if settings.vector_db == "chroma":
        missing.append(
            "a vector store that is not the embedded Chroma client (VECTOR_DB=pinecone): "
            "the embedded store is opened by one process at a time"
        )
    return missing


def multi_process_enabled(workers: str | None = None) -> bool:
    """True when more than one worker process is configured."""
    if workers is None:
        workers = os.environ.get("UVICORN_WORKERS", "1")
    return workers.strip() not in ("", "1")


def check_single_process_deployment(workers: str | None = None) -> None:
    """Refuse more than one worker process unless the shared stores are in place.

    The session store, the document registry, the connector credential and
    permission caches and the audit chain are all safe to share between
    worker processes or container replicas now (see
    ``multi_process_requirements``), provided the deployment has PostgreSQL,
    Redis and a non-embedded vector store. Without one of those a second
    worker would still corrupt or split state, so any UVICORN_WORKERS value
    other than unset/empty/"1" is refused with the reasons.
    """
    if not multi_process_enabled(workers):
        return
    missing = multi_process_requirements()
    if not missing:
        logger.info(
            "UVICORN_WORKERS=%s: multi-process deployment (shared registry, sessions, "
            "caches and audit chain)",
            (workers or os.environ.get("UVICORN_WORKERS", "")).strip(),
        )
        return
    raise RuntimeError(
        f"UVICORN_WORKERS={(workers or os.environ.get('UVICORN_WORKERS', '')).strip()} needs: "
        + "; ".join(missing)
        + ". The session store, document registry, connector credential caches and "
        "audit chain are shared between workers only on that footing (docs/deployment.md, "
        "Scaling). Set UVICORN_WORKERS=1 (or unset it) to run a single process."
    )


# Open handle on the instance lock file; held for the life of the process so
# the kernel releases the lock if the process dies without a clean shutdown.
_instance_lock_handle = None


def acquire_instance_lock(
    data_dir: "str | os.PathLike[str] | None" = None, shared: bool | None = None
) -> None:
    """Hold an advisory lock on ``<data>/.instance.lock``.

    A single-process deployment takes it exclusively: ``uvicorn --workers 2``,
    gunicorn, or a second container replica pointed at the same data volume
    never set UVICORN_WORKERS and would otherwise run two processes against
    stores that are only shared when the multi-process footing is in place.
    A multi-process deployment (``multi_process_enabled`` and no missing
    requirements) takes a shared lock instead, so its workers coexist while
    a single-process start against the same volume is still refused, and
    vice versa: the two modes never mix on one data directory.

    POSIX only (``fcntl``); on Windows development machines the check is
    skipped. Re-entrant within a process so a test harness that starts the app
    more than once does not lock itself out. Raises RuntimeError when another
    process holds a conflicting lock.
    """
    global _instance_lock_handle
    if _instance_lock_handle is not None:
        return
    if sys.platform == "win32":  # no advisory file locks of this kind
        return
    import fcntl
    from pathlib import Path

    if shared is None:
        shared = multi_process_enabled() and not multi_process_requirements()
    directory = Path(data_dir) if data_dir is not None else Path(settings.upload_dir).parent
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".instance.lock"
    handle = open(lock_path, "a+")  # noqa: SIM115 - deliberately kept open
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        if shared:
            raise RuntimeError(
                f"A single-process CaseCite instance holds {lock_path} exclusively, so "
                "a multi-worker instance cannot share this data directory with it. Stop "
                "the other instance first."
            ) from None
        raise RuntimeError(
            f"Another CaseCite process already holds {lock_path}. A single-process "
            "deployment may not share a data directory with another instance (set "
            "UVICORN_WORKERS and the shared stores it needs for a multi-process one; "
            "see docs/deployment.md, Scaling). Stop the other instance, or if it "
            "crashed without releasing the lock, remove the file and start again."
        ) from None
    _instance_lock_handle = handle


def release_instance_lock() -> None:
    """Release the lock taken by ``acquire_instance_lock`` (clean shutdown)."""
    global _instance_lock_handle
    handle, _instance_lock_handle = _instance_lock_handle, None
    if handle is not None:
        handle.close()  # closing the descriptor drops the flock


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    logger.info(f"Starting {settings.app_name}...")
    logger.info(f"Environment: {'Production' if not settings.debug else 'Development'}")
    logger.info(f"Vector DB: {settings.vector_db}")
    logger.info(f"Embedding Model: {settings.openai_embedding_model}")

    # VOLUME TRIPWIRE: everything under data/ (uploaded documents, the vector
    # index, encrypted BYOK keys) must live on a persistent volume. A marker
    # file written on first boot detects a wiped/ephemeral data dir: if it is
    # missing on a later boot, every deploy has been silently erasing user
    # data (API keys "disappearing", documents "un-indexing" after deploys).
    try:
        from pathlib import Path as _Path

        _data_dir = _Path(settings.upload_dir).parent
        _marker = _data_dir / ".volume_marker"
        if _marker.exists():
            logger.info("Data volume marker present — data dir persisted across restarts.")
        else:
            _data_dir.mkdir(parents=True, exist_ok=True)
            from datetime import UTC as _UTC
            from datetime import datetime as _dt

            _marker.write_text(_dt.now(_UTC).isoformat())
            logger.critical(
                "DATA DIR IS FRESH at %s — if this is not the first boot, the data "
                "volume is NOT persisted and every deploy erases uploaded documents, "
                "the vector index, and stored API keys. Mount a persistent volume at "
                "this path (e.g. /app/data in Coolify).",
                _data_dir.resolve(),
            )
    except OSError as _vol_err:
        logger.error("Could not verify data-volume persistence: %s", _vol_err)

    # SPLIT-BRAIN GUARD: refuse to run with more than one worker process, and
    # refuse to run beside another process already serving this data dir.
    check_single_process_deployment()
    acquire_instance_lock()

    # Ensure required directories exist
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.chroma_persist_dir, exist_ok=True)

    # Uploaded client documents are encrypted at rest (AES-256-GCM, see
    # app/utils/file_crypto.py). Encrypt any files that predate the feature.
    try:
        from app.services.documents import document_service

        document_service.encrypt_existing_files()
    except Exception as _enc_err:  # never block startup on migration
        logger.error(f"Encrypted-at-rest migration failed: {_enc_err}")

    # The document registry and the vector store persist independently; a
    # Chroma schema upgrade resets the store while documents stay marked
    # INDEXED. Repair any divergence in the background so users never see
    # "indexed" documents that search can't find. Never blocks startup.
    async def _reconcile_index():
        try:
            from app.services.documents import document_service as _docs

            await _docs.reconcile_index()
        except Exception as _rec_err:  # repair is best-effort
            logger.error(f"Index reconciliation failed: {_rec_err}")

    app.state.index_reconcile_task = asyncio.create_task(_reconcile_index())

    # The Chroma vector store owns its own storage format and is NOT app-layer
    # encrypted; derived embeddings/chunks of privileged text live there. In
    # production this is gated at startup by DISK_ENCRYPTION_ACKNOWLEDGED (see
    # config validation), which fails closed unless the operator confirms the
    # data volume is encrypted. The log line below is a reminder of what that
    # acknowledgement covers.
    if not settings.debug:
        logger.info(
            "DATA-AT-REST: uploaded documents are app-layer encrypted; the vector "
            "store at %s and derived DB text are not. Operator confirmed volume "
            "encryption via DISK_ENCRYPTION_ACKNOWLEDGED — ensure LUKS/BitLocker/"
            "FileVault (or provider disk encryption) remains enabled on this volume.",
            settings.chroma_persist_dir,
        )

    # Initialize SQLAlchemy database.
    # Alembic migrations (run at container startup — see start.sh / Dockerfile
    # CMD) are the source of truth for schema changes, including column ALTERs.
    # create_all here is a non-destructive BACKSTOP: it creates any whole tables
    # not yet covered by a migration and is a no-op for existing tables. It never
    # alters existing tables, so it does not substitute for migrations.
    logger.info("Ensuring database tables exist (backstop; migrations run at startup)...")
    await init_sqlalchemy_db()

    # The document registry lives in the database (shared between processes);
    # the first start with the tables present imports index.json.
    try:
        from app.services.documents import document_service as _registry

        summary = await asyncio.to_thread(_registry.use_database_store)
        if summary.get("migrated"):
            logger.info("Imported the document registry from index.json: %s", summary)
    except Exception as e:
        logger.critical(f"Document registry could not be opened in the database: {e}")
        raise

    # SQLite (development only) never runs Alembic, and create_all cannot add a
    # column to an existing table — add any model columns a dev database lacks.
    try:
        from app.database import IS_SQLITE, async_engine
        from app.models.db_models import Base as _Base
        from app.utils.sqlite_columns import add_missing_sqlite_columns

        if IS_SQLITE:
            async with async_engine.begin() as _conn:
                await _conn.run_sync(add_missing_sqlite_columns, _Base.metadata)
    except Exception as _col_err:
        logger.error(f"SQLite column backstop failed: {_col_err}")

    # Ensure there is always at least one admin. On installs created before the
    # "first user is admin" rule, the earliest ACTIVE account is promoted so
    # admin-only settings (Users, integrations) are reachable. A deactivated
    # account is never promoted: it was switched off deliberately.
    try:
        from sqlalchemy import select

        from app.database import AsyncSessionLocal
        from app.models.db_models import User as DBUser

        async with AsyncSessionLocal() as _admin_session:
            users = (
                (await _admin_session.execute(select(DBUser).order_by(DBUser.created_at)))
                .scalars()
                .all()
            )
            has_admin = any(isinstance(u.roles, list) and "admin" in u.roles for u in users)
            active_users = [u for u in users if u.is_active]
            if active_users and not has_admin:
                oldest = active_users[0]
                oldest.roles = ["admin"]
                await _admin_session.commit()
                logger.info("Promoted earliest active user to admin (no admin existed).")
            if not users and not settings.debug and not settings.registration_bootstrap_token:
                logger.critical(
                    "No user accounts exist and REGISTRATION_BOOTSTRAP_TOKEN is not set: "
                    "the first (admin) registration is refused until it is. Set "
                    "REGISTRATION_BOOTSTRAP_TOKEN (openssl rand -hex 24), restart, and "
                    "supply it as bootstrap_token when registering the first account."
                )
    except Exception as _admin_err:
        logger.warning(f"Could not ensure an admin user exists: {_admin_err}")

    # Apply the instance-wide CourtListener token (admin-set, DB) over the .env
    # fallback so it survives restarts without re-editing .env.
    try:
        from app.services.courtlistener import apply_courtlistener_token
        from app.services.instance_settings import get_secret

        cl_token = await get_secret("courtlistener_api_token")
        if cl_token:
            # Propagate to ALL three CourtListener clients (validation, legal
            # tools, judge intel) — not just one — so every tool authenticates.
            apply_courtlistener_token(cl_token)
            logger.info("Applied instance-wide CourtListener token from storage.")
    except Exception as _cl_err:
        logger.warning(f"Could not load instance CourtListener token: {_cl_err}")

    # Apply instance-wide connector (file picker) credentials — Google Drive,
    # OneDrive/SharePoint, Box, Dropbox — over their .env fallbacks.
    try:
        from app.services.connector_credentials import load_all as _load_connector_creds

        await _load_connector_creds()
    except Exception as _cc_err:
        logger.warning(f"Could not load instance connector credentials: {_cc_err}")
    try:
        from app.services.connectors import registry_summary as _connector_registry_summary

        logger.info(_connector_registry_summary())
    except Exception as _cr_err:  # pragma: no cover - log line only
        logger.warning(f"Could not summarise connector registry: {_cr_err}")

    # Apply the instance-wide custom/local model endpoint (admin-set, DB) over .env.
    try:
        from app.services.llm_clients import load_from_storage as _load_llm_cfg

        await _load_llm_cfg()
    except Exception as _llm_err:
        logger.warning(f"Could not load instance LLM endpoint config: {_llm_err}")

    # Seed clause-intelligence taxonomy / jurisdiction rules / contract type reqs
    try:
        from app.database import AsyncSessionLocal
        from app.services.clause_intel.seeder import seed_clause_intelligence
        from app.services.embeddings import embedding_service

        async def _embed(texts):
            return await embedding_service.embed_texts(texts)

        async with AsyncSessionLocal() as _seed_session:
            seed_result = await seed_clause_intelligence(
                _seed_session,
                embed_fn=_embed if settings.openai_api_key else None,
                embedding_model=embedding_service.model,
            )
        logger.info(f"Clause-intel seed: {seed_result}")
    except Exception as _seed_err:
        logger.warning(f"Clause-intel seed failed (non-fatal): {_seed_err}")

    # Recover orphaned jobs from previous crash
    orphaned = await job_manager.recover_orphans()
    if orphaned:
        logger.info(f"Recovered {orphaned} orphaned jobs from previous run")

    # Row-level security is the database's second fence around user content,
    # but PostgreSQL exempts superusers from it: say so loudly if the app has
    # been pointed at the database as one.
    try:
        from app.database import IS_POSTGRES, get_db_context
        from app.rls import rls_status

        if IS_POSTGRES:
            async with get_db_context() as db:
                status = await rls_status(db)
            if status["enforced"]:
                logger.info("Row-level security enforced for database role %s", status["role"])
            else:
                logger.error(
                    "Row-level security is NOT enforced: the app connects as database role "
                    "%s, which is a superuser or has BYPASSRLS. Create a non-superuser role "
                    "that owns the tables and point DATABASE_URL at it (docs/deployment.md).",
                    status["role"],
                )
    except Exception as e:  # a probe must never block startup
        logger.warning("Could not check row-level security enforcement: %s", e)

    # Legal holds are enforced by the document service without a DB session:
    # load the held matters now, before any delete can run.
    try:
        from app.database import get_db_context
        from app.services.governance import refresh_document_service_holds

        async with get_db_context() as db:
            held = await refresh_document_service_holds(db)
        if held:
            logger.info(f"{len(held)} matter(s) on legal hold")
    except Exception as e:  # a missing column on a half-migrated DB must not block startup
        logger.error(f"Could not load legal holds: {e}")

    # Start the daily audit-log retention task (compress/delete old JSONL files)
    # and the daily matter-retention task.
    retention_task = asyncio.create_task(_audit_retention_loop())
    matter_retention_task = asyncio.create_task(_retention_loop())

    yield

    # Shutdown
    logger.info("Shutting down...")
    for task in (retention_task, matter_retention_task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await job_manager.graceful_shutdown()
    await close_sqlalchemy_db()
    release_instance_lock()


app = FastAPI(
    title=settings.app_name,
    description="""
    CaseCite API — self-hosted legal research over your firm's documents.

    ## Features
    - Retrieval-augmented research chat with verified citations
    - Document management, indexing, and cloud-storage connectors
    - Contract analysis, redlining, and drafting
    - Case law, judge, and citation tools backed by CourtListener
    - Tamper-evident (HMAC-chained) audit trail

    ## Authentication
    Every endpoint requires a bearer token (or the session cookie) except:
    /health and /ready (probes; detail is admin-only), GET /api/v1/branding,
    GET /api/v1/csrf-token, the OAuth callback under /api/v1/connectors, and
    the sign-in endpoints under /api/v1/auth (login, register, refresh,
    password reset, MFA verify). /metrics requires an admin.
    Obtain one from POST /auth/login (email + password, optional TOTP MFA);
    an Azure AD SSO exchange is available at POST /auth/azure/login.
    """,
    version=__version__,
    lifespan=lifespan,
    openapi_url="/openapi.json" if settings.debug else None,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# ==================== Security Middleware ====================
configure_middleware(app)

# ==================== System Routes (health, metrics, config) ====================
app.include_router(system.router)

# ==================== API Routes ====================

# Authentication (public)
app.include_router(auth.router, prefix=settings.api_prefix)

# Protected routes
# chat_sessions is registered BEFORE chat so /chat/sessions/* can never be
# shadowed by /chat routes (the chat router currently has no catch-all, but
# ordering keeps this safe if one is ever added).
app.include_router(chat_sessions.router, prefix=settings.api_prefix)
app.include_router(workspace_sessions.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(matters.router, prefix=settings.api_prefix)
app.include_router(review_tables.router, prefix=settings.api_prefix)
app.include_router(deep_research.router, prefix=settings.api_prefix)
app.include_router(workflows.router, prefix=settings.api_prefix)
app.include_router(strategy.router, prefix=settings.api_prefix)
app.include_router(documents.router, prefix=settings.api_prefix)
app.include_router(connectors.router, prefix=settings.api_prefix)
app.include_router(settings_router.router, prefix=settings.api_prefix)
app.include_router(tools.router, prefix=settings.api_prefix)
app.include_router(judge_intel.router, prefix=settings.api_prefix)
app.include_router(legal_docs.router, prefix=settings.api_prefix)
app.include_router(mfa.router, prefix=settings.api_prefix)

# AI-powered feature routers
app.include_router(contract_analysis.router, prefix=settings.api_prefix)
app.include_router(integrations.router, prefix=settings.api_prefix)
app.include_router(users.router, prefix=settings.api_prefix)
app.include_router(audit.router, prefix=settings.api_prefix)

# File Pickers (No OAuth verification required)
app.include_router(pickers.router, prefix=settings.api_prefix)

# User API Key Management (server-side storage)
app.include_router(user_keys.router, prefix=settings.api_prefix)

# Async Job Queue
app.include_router(jobs.router, prefix=settings.api_prefix)

# White-Label Branding (public GET + admin mutations)
app.include_router(branding.router, prefix=settings.api_prefix)
app.include_router(addin.router, prefix=settings.api_prefix)

# CSRF token endpoint (for SPA frontends)
app.include_router(get_csrf_router(), prefix=settings.api_prefix)

# ==================== Error Handlers ====================
register_error_handlers(app)

# ==================== Frontend Serving ====================
mount_frontend(app)


if __name__ == "__main__":
    import uvicorn

    # Bind to all interfaces for container deployments (guarded by firewall/reverse proxy)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=settings.debug)  # nosec B104
