"""
Migrations 005 and 009 against both schema lineages, on a throwaway SQLite file.

Production migrates PostgreSQL only, but these two revisions are the ones a
brownfield install hits right after ``migration_bootstrap`` stamps it at 004,
and their failure modes were "the table/index this ALTER names does not exist
here" — which SQLite reproduces faithfully:

- **create_all-built** (``Base.metadata.create_all``): what the runtime backstop
  leaves behind. Columns, indexes and client-side defaults already match the
  models, and the 1.2.0 product removals mean the document-assembly tables that
  005 used to ALTER unconditionally are absent.
- **migration-built**: the ``users`` / ``audit_logs`` / ``generated_documents``
  tables as 001 created them (``server_default=now()``, the ``idx_*`` indexes,
  no MFA or hash-chain columns), hand-built because 001 itself uses constraint
  ALTERs SQLite cannot run, so the real 001→004 chain cannot be replayed here.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

# The same imports migrations/env.py makes, so Base.metadata holds every table
# the backstop's create_all would build.
from app.models.db_models import Base

_VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load_migration(revision: str):
    (path,) = _VERSIONS.glob(f"*_{revision}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn) -> None:
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        fn()


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _indexes(engine, table: str) -> dict[str, dict]:
    return {ix["name"]: ix for ix in sa.inspect(engine).get_indexes(table)}


def _server_default(engine, table: str, column: str):
    for col in sa.inspect(engine).get_columns(table):
        if col["name"] == column:
            return col.get("default")
    raise AssertionError(f"{table}.{column} missing")


@pytest.fixture
def create_all_engine(tmp_path):
    """A database built by the runtime backstop (current models, nothing else)."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'create_all.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def migration_built_engine(tmp_path):
    """The pre-005 state of the tables 005 touches, as 001 built them."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE, "
            "name VARCHAR(255) NOT NULL, azure_oid VARCHAR(255) UNIQUE, is_active BOOLEAN, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.exec_driver_sql("CREATE INDEX idx_user_email ON users (email)")
        conn.exec_driver_sql("CREATE INDEX idx_user_azure_oid ON users (azure_oid)")
        conn.exec_driver_sql("CREATE INDEX idx_user_email_active ON users (email, is_active)")
        conn.exec_driver_sql(
            "CREATE TABLE audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "event_type VARCHAR(100) NOT NULL, user_id VARCHAR(36) REFERENCES users (id), "
            "resource_type VARCHAR(100), resource_id VARCHAR(255))"
        )
        conn.exec_driver_sql("CREATE INDEX idx_audit_timestamp ON audit_logs (timestamp)")
        conn.exec_driver_sql("CREATE INDEX idx_audit_event_type ON audit_logs (event_type)")
        conn.exec_driver_sql("CREATE INDEX idx_audit_user ON audit_logs (user_id)")
        conn.exec_driver_sql(
            "CREATE TABLE generated_documents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name VARCHAR(255) NOT NULL, parent_id INTEGER REFERENCES generated_documents (id), "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.exec_driver_sql(
            "INSERT INTO users (id, email, name, is_active) VALUES ('u1', 'old@example.com', 'Old', 1)"
        )
        conn.exec_driver_sql("INSERT INTO audit_logs (event_type, user_id) VALUES ('login', 'u1')")
    return engine


class TestMigration005:
    _USER_COLUMNS = {"mfa_secret", "mfa_recovery_codes", "password_changed_at"}
    _AUDIT_COLUMNS = {"entry_hash", "previous_hash", "correlation_id"}

    def test_chain_position(self):
        migration = _load_migration("005")
        assert migration.revision == "005"
        assert migration.down_revision == "004"

    def test_create_all_schema_upgrades_without_the_removed_tables(self, create_all_engine):
        """The brownfield path: stamped at 004 by migration_bootstrap, then 005."""
        engine = create_all_engine
        assert "generated_documents" not in sa.inspect(engine).get_table_names()
        before = {t: _columns(engine, t) for t in ("users", "audit_logs")}
        migration = _load_migration("005")

        _run(engine, migration.upgrade)
        _run(engine, migration.upgrade)  # idempotent

        assert {t: _columns(engine, t) for t in ("users", "audit_logs")} == before
        assert _server_default(engine, "users", "created_at") is None
        users = _indexes(engine, "users")
        assert users["ix_users_email"]["unique"]
        assert "idx_user_email" not in users

    def test_migration_built_schema_is_aligned(self, migration_built_engine):
        engine = migration_built_engine
        migration = _load_migration("005")

        _run(engine, migration.upgrade)

        assert _columns(engine, "users") >= self._USER_COLUMNS
        assert _columns(engine, "audit_logs") >= self._AUDIT_COLUMNS
        for table, column in (("users", "created_at"), ("audit_logs", "timestamp")):
            assert _server_default(engine, table, column) is None
        assert _server_default(engine, "generated_documents", "created_at") is None
        users = _indexes(engine, "users")
        assert users["ix_users_email"]["unique"] and users["ix_users_azure_oid"]["unique"]
        assert not {"idx_user_email", "idx_user_azure_oid"} & set(users)
        audit = _indexes(engine, "audit_logs")
        assert {"idx_audit_resource", "ix_audit_logs_event_type", "ix_audit_logs_timestamp"} <= set(
            audit
        )
        assert not {"idx_audit_event_type", "idx_audit_timestamp"} & set(audit)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT email FROM users").scalar_one() == "old@example.com"
            assert conn.exec_driver_sql("SELECT user_id FROM audit_logs").scalar_one() == "u1"

    def test_migration_built_schema_upgrade_is_idempotent(self, migration_built_engine):
        engine = migration_built_engine
        migration = _load_migration("005")
        _run(engine, migration.upgrade)
        snapshot = (_columns(engine, "users"), set(_indexes(engine, "users")))
        _run(engine, migration.upgrade)
        assert (_columns(engine, "users"), set(_indexes(engine, "users"))) == snapshot


class TestMigration009:
    _TABLES = (
        "chat_sessions",
        "contract_analysis_runs",
        "authority_map_runs",
        "workspace_sessions",
    )

    def test_chain_position(self):
        migration = _load_migration("009")
        assert migration.revision == "009"
        assert migration.down_revision == "008"
        assert tuple(migration._TABLES) == self._TABLES

    def test_downgrade_after_a_fresh_build(self, create_all_engine):
        """A fresh build creates these tables from the models (matter_id and
        ``ix_<table>_matter_id`` included), so upgrade() adds nothing and the
        downgrade must not assume its own ``idx_<table>_matter`` exists."""
        engine = create_all_engine
        migration = _load_migration("009")
        # authority_map_runs is in the migration's list but no longer in the
        # models (the feature was removed; 018 drops the table), so a fresh
        # build does not have it and 009 skips it.
        built = set(sa.inspect(engine).get_table_names())
        assert "authority_map_runs" not in built
        tables = [t for t in self._TABLES if t in built]
        assert len(tables) == len(self._TABLES) - 1
        for table in tables:
            assert "matter_id" in _columns(engine, table)
            assert f"idx_{table}_matter" not in _indexes(engine, table)

        _run(engine, migration.upgrade)
        _run(engine, migration.downgrade)

        for table in tables:
            assert "matter_id" not in _columns(engine, table)
            assert not [
                ix for ix in _indexes(engine, table).values() if "matter_id" in ix["column_names"]
            ]

    def test_round_trip_on_a_migration_built_table(self, tmp_path):
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'ws.db'}")
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE workspace_sessions (id VARCHAR(36) PRIMARY KEY, "
                "user_id VARCHAR(36) NOT NULL, created_at DATETIME NOT NULL)"
            )
        migration = _load_migration("009")

        _run(engine, migration.upgrade)
        assert "matter_id" in _columns(engine, "workspace_sessions")
        assert "idx_workspace_sessions_matter" in _indexes(engine, "workspace_sessions")

        _run(engine, migration.downgrade)
        assert "matter_id" not in _columns(engine, "workspace_sessions")
        assert "idx_workspace_sessions_matter" not in _indexes(engine, "workspace_sessions")
