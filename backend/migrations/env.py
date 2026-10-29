"""
Alembic Migration Environment Configuration

This file configures how Alembic runs migrations.
Supports both sync (for migrations) and async (for the app) database access.

Usage:
    # Generate migration
    alembic revision --autogenerate -m "description"

    # Run migrations
    alembic upgrade head

    # Rollback one version
    alembic downgrade -1
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add the backend directory to the path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import our models and database configuration
from app.database import DATABASE_URL

# Import models defined outside db_models.py so Alembic autogenerate discovers them
from app.models.db_models import Base
from app.services.judge_intel import JudgeCacheDB  # noqa: F401

# This is the Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate support
target_metadata = Base.metadata


# Get database URL from environment or use default
def get_url():
    """Get database URL, converting async URL to sync for Alembic."""
    url = DATABASE_URL

    # Convert async drivers to sync drivers
    if "aiosqlite" in url:
        url = url.replace("sqlite+aiosqlite", "sqlite")
    elif "asyncpg" in url:
        url = url.replace("postgresql+asyncpg", "postgresql+psycopg2")

    return url


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # Detect column type changes
        compare_server_default=True,  # Detect default value changes
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a
    connection with the context.
    """
    # Create configuration dict with our URL
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # Detect column type changes
            compare_server_default=True,  # Detect default value changes
            # Include schemas if using PostgreSQL schemas
            # include_schemas=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
