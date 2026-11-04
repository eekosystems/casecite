"""
Database Configuration - SQLAlchemy with PostgreSQL/SQLite Support

Production: PostgreSQL (async with asyncpg)
Development: SQLite (async with aiosqlite)

Set DATABASE_URL environment variable for production:
  postgresql+asyncpg://user:password@host:5432/casecite

For development, defaults to SQLite in ./data/casecite.db
"""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)


# Import Base from models to avoid circular imports
# Base is defined in db_models.py and imported here for convenience
def get_base():
    """Lazy import to avoid circular dependency."""
    from app.models.db_models import Base

    return Base


# Database URL configuration
# Check environment variable first, then fall back to config
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    # Try to get from settings (may fail during Alembic if app not fully loaded)
    try:
        from app.config import settings

        DATABASE_URL = settings.database_url
    except (ImportError, AttributeError, ValueError):  # Expected during Alembic migrations
        pass

# Default to SQLite for development
if not DATABASE_URL:
    # Resolve data directory relative to backend/, not CWD, so the path is stable
    # regardless of where the server is started from
    _backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    _data_dir = os.path.join(_backend_dir, "data")
    os.makedirs(_data_dir, exist_ok=True)
    _db_path = os.path.join(_data_dir, "casecite.db")
    DATABASE_URL = f"sqlite+aiosqlite:///{_db_path}"
    logger.warning(
        f"DATABASE_URL not set - using SQLite at {_db_path}. "
        "IMPORTANT: in a container this path MUST be on a persistent volume "
        "(e.g. mount /app/data in Coolify) or ALL accounts and data are wiped on "
        "every redeploy/restart — you will be able to sign up but not log back in. "
        "For production set DATABASE_URL=postgresql+asyncpg://user:pass@host/db."
    )

# Determine if we're using SQLite or PostgreSQL
IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_POSTGRES = "postgresql" in DATABASE_URL or "postgres" in DATABASE_URL

# Convert postgres:// to postgresql:// if needed (Heroku compatibility)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql+psycopg2://"):
    # A sync-driver URL crashes create_async_engine at import time (which also
    # breaks alembic, since migrations/env.py imports this module) — coerce it.
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


# Outside DEBUG, keep bound parameters out of SQLAlchemy error messages and
# logs: they carry chat/document text, password hashes and encrypted secrets.
_HIDE_PARAMETERS = os.environ.get("DEBUG", "").lower() not in ("true", "1")


def get_async_engine():
    """Create async SQLAlchemy engine with appropriate settings."""
    if IS_SQLITE:
        # SQLite-specific settings. A file-backed database gets the normal
        # pool — one connection per checked-out session — because SQLite
        # transactions are per-connection: with a single shared connection
        # (StaticPool) one session's close/ROLLBACK discards another session's
        # uncommitted INSERT, which surfaced as rows vanishing between a write
        # and the next request. StaticPool is only right for ":memory:", where
        # every new connection would be a fresh, empty database.
        in_memory = ":memory:" in DATABASE_URL or DATABASE_URL.endswith("sqlite+aiosqlite://")
        sqlite_kwargs: dict = {"poolclass": StaticPool} if in_memory else {}
        engine = create_async_engine(
            DATABASE_URL,
            echo=os.environ.get("SQL_DEBUG", "").lower() == "true",
            hide_parameters=_HIDE_PARAMETERS,
            # timeout: seconds a writer waits on a locked database before
            # raising, instead of failing on the first concurrent write.
            connect_args={"check_same_thread": False, "timeout": 30},
            **sqlite_kwargs,
        )
    else:
        # PostgreSQL settings
        pg_connect_args = {}
        db_ssl = os.environ.get("DB_SSL", "").lower()

        # In production (non-debug, non-demo), require SSL unless explicitly opted out
        _is_production = os.environ.get("DEBUG", "").lower() not in ("true", "1")

        if _is_production and db_ssl == "disable":
            raise ValueError(
                "SECURITY ERROR: DB_SSL=disable is not allowed in production. "
                "Database connections must use SSL to protect client-privileged legal data. "
                "For a database on a trusted private network (e.g. the bundled Postgres "
                "on an isolated Docker network) use DB_SSL=internal instead."
            )

        if db_ssl == "internal":
            # Explicit opt-out for a database reachable only over a trusted private
            # network — the bundled postgres:16-alpine ships without TLS, and its
            # traffic never leaves the pinned Docker bridge subnet. Do NOT use this
            # for a managed/external database (that path forces TLS below).
            logger.warning(
                "DB_SSL=internal: connecting to PostgreSQL WITHOUT TLS. This is only "
                "safe when the database is on a trusted private network (bundled "
                "Postgres on an isolated Docker network). Use DB_SSL=require for any "
                "database reached over an untrusted network."
            )
        elif db_ssl in ("true", "require", "1") or _is_production:
            import ssl

            ca_file = os.environ.get("DB_SSL_CA")
            if ca_file:
                ssl_ctx = ssl.create_default_context(cafile=ca_file)
            else:
                # For managed databases (RDS, Supabase) that use public CAs
                # create_default_context() sets CERT_REQUIRED, check_hostname=True,
                # and loads system CA certificates automatically
                ssl_ctx = ssl.create_default_context()
            pg_connect_args["ssl"] = ssl_ctx

            if _is_production:
                logger.info("Database SSL enabled for production PostgreSQL connection")

        engine = create_async_engine(
            DATABASE_URL,
            echo=os.environ.get("SQL_DEBUG", "").lower() == "true",
            hide_parameters=_HIDE_PARAMETERS,
            pool_size=20,
            max_overflow=30,
            pool_pre_ping=True,  # Verify connections before use
            pool_recycle=3600,  # Recycle connections after 1 hour
            connect_args=pg_connect_args if pg_connect_args else {},
        )
    return engine


def get_sync_engine():
    """Create sync SQLAlchemy engine for Alembic migrations."""
    # Convert async URL to sync URL
    sync_url = DATABASE_URL
    if "aiosqlite" in sync_url:
        sync_url = sync_url.replace("sqlite+aiosqlite", "sqlite")
    elif "asyncpg" in sync_url:
        sync_url = sync_url.replace("postgresql+asyncpg", "postgresql+psycopg2")

    if IS_SQLITE:
        engine = create_engine(
            sync_url,
            echo=os.environ.get("SQL_DEBUG", "").lower() == "true",
            hide_parameters=_HIDE_PARAMETERS,
            connect_args={"check_same_thread": False},
        )
    else:
        engine = create_engine(
            sync_url,
            echo=os.environ.get("SQL_DEBUG", "").lower() == "true",
            hide_parameters=_HIDE_PARAMETERS,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
    return engine


# Create engines
async_engine = get_async_engine()
sync_engine = get_sync_engine()

# PostgreSQL only: every transaction carries the request's user scope so the
# row-level security policies (migration 023, app/rls.py) can enforce it.
from app.rls import install_rls_listener  # noqa: E402  (needs the engine first)

install_rls_listener(async_engine)

# Session factories
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency for database sessions.

    Usage:
        @router.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Item))
            return result.scalars().all()
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except (
            BaseException
        ):  # Session error boundary: rollback on any failure (incl. cancellation)
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for database sessions (for use outside FastAPI routes).

    Usage:
        async with get_db_context() as db:
            result = await db.execute(select(Item))
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except (
            BaseException
        ):  # Session error boundary: rollback on any failure (incl. cancellation)
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """
    Initialize database tables via create_all (no-op for existing tables).

    Schema changes (new columns, constraints, etc.) MUST go through Alembic
    migrations — never alter tables at runtime.
    """
    Base = get_base()
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized")


async def close_db():
    """Close database connections on shutdown."""
    await async_engine.dispose()
    sync_engine.dispose()
    logger.info("Database connections closed")


# SQLite-specific: Enable foreign keys
if IS_SQLITE:

    @event.listens_for(sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(async_engine.sync_engine, "connect")
    def set_sqlite_pragma_async(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
