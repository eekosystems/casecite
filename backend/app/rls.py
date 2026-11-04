"""PostgreSQL row-level security: a second fence around user content.

Every service function scopes its queries to the caller (owner, or member of
the row's matter). That is the first fence. This module adds a second one at
the database, so a query that forgets the filter still cannot return another
user's rows:

- ``get_current_user`` records the authenticated user id in a context
  variable for the rest of the request; ``require_permission`` records a
  bypass when the endpoint is gated by an ``admin.*`` permission (admin
  oversight, user export and deletion need every user's rows).
- Each PostgreSQL transaction begins with ``SET LOCAL app.user_id`` and
  ``SET LOCAL app.rls_bypass`` taken from those variables (an engine event;
  ``SET LOCAL`` dies with the transaction, so pooled connections never leak
  a scope).
- Migration 024 enables and FORCES row-level security on the content tables
  and installs policies (tightened by migration 027) that allow a row when
  the session is bypassed, the caller owns it, or the caller is a member —
  and not walled off — of its matter. Child tables defer to their parent's
  policy. A transaction with NO user and no bypass sees nothing: forgetting
  to say who is asking is not a way to see everything.
- System work that legitimately needs every user's rows (the retention
  task, Alembic data migrations) says so with :func:`bypassed`, which
  takes a reason and logs it. Background jobs started by a request inherit
  that request's user (contextvars are copied into the task).

SQLite (development, the test suite) has no row-level security; the
listener is not installed there and the policies are not created. The
PostgreSQL test suite applies the DDL itself and checks the fence.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The authenticated user for the current request/task (copied into tasks it
# spawns), or None for unscoped work.
request_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rls_request_user_id", default=None
)
# True while an admin-gated endpoint runs: every user's rows are visible.
rls_bypass: contextvars.ContextVar[bool] = contextvars.ContextVar("rls_bypass", default=False)


@contextmanager
def scoped(user_id: str | None) -> Iterator[None]:
    """Run a block as ``user_id``, restoring the previous scope afterwards.

    ``None`` is a session with no user, which the policies admit to nothing;
    system work that needs every row uses :func:`bypassed` instead.
    """
    token = request_user_id.set(user_id)
    bypass_token = rls_bypass.set(False)
    try:
        yield
    finally:
        request_user_id.reset(token)
        rls_bypass.reset(bypass_token)


def grant_bypass(reason: str) -> None:
    """Lift the fence for the rest of the current context (an admin-gated
    request). ``reason`` goes to the log so every lift is accounted for."""
    if not reason:
        raise ValueError("an RLS bypass needs a reason")
    rls_bypass.set(True)
    logger.info("Row-level security bypass: %s", reason)


@contextmanager
def bypassed(reason: str) -> Iterator[None]:
    """Run a block with every user's rows visible, for a stated reason.

    This is the only way system work (the retention task, a data migration)
    sees rows it does not own; a session that merely has no user sees
    nothing. The reason is logged so a bypass is never silent.
    """
    if not reason:
        raise ValueError("an RLS bypass needs a reason")
    token = rls_bypass.set(True)
    logger.info("Row-level security bypass: %s", reason)
    try:
        yield
    finally:
        rls_bypass.reset(token)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_installed = False


def install_rls_listener(engine: AsyncEngine) -> None:
    """Make every PostgreSQL ORM transaction carry the request's scope.

    Registered on the ORM ``Session`` (which ``AsyncSession`` wraps) rather
    than the engine: the engine's ``begin`` event fires before the dialect
    has actually opened the transaction, so a ``SET LOCAL`` issued there is
    outside any transaction and does nothing. ``after_begin`` fires once the
    connection-level transaction exists. Core-only connections (startup,
    migrations, tests' schema setup) carry no scope and are unscoped.
    """
    global _installed
    if engine.dialect.name != "postgresql" or _installed:
        return

    @event.listens_for(Session, "after_begin")
    def _set_scope(session, transaction, connection) -> None:
        if connection.dialect.name != "postgresql":
            return
        user_id = request_user_id.get()
        bypass = rls_bypass.get()
        connection.exec_driver_sql(f"SET LOCAL app.user_id = {_quote(user_id or '')}")
        connection.exec_driver_sql(
            f"SET LOCAL app.rls_bypass = {_quote('on' if bypass else 'off')}"
        )

    _installed = True
    logger.info("Row-level security scope listener installed")


async def rls_status(session) -> dict[str, object]:
    """Whether the database will actually enforce the policies for the role
    the app connects as. PostgreSQL exempts superusers and roles with
    BYPASSRLS from row-level security entirely, so an app running as the
    image's default superuser has no second fence — the startup log and
    /health say so. None of this applies to SQLite (``enforced`` is None).
    """
    from sqlalchemy import text

    if session.bind is None or session.bind.dialect.name != "postgresql":
        return {"enforced": None, "role": None, "superuser": None, "bypass_rls": None}
    row = (
        await session.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).first()
    if row is None:
        return {"enforced": False, "role": None, "superuser": None, "bypass_rls": None}
    role, superuser, bypass = row
    return {
        "enforced": not (superuser or bypass),
        "role": role,
        "superuser": bool(superuser),
        "bypass_rls": bool(bypass),
    }


class RLSNotEnforcedError(RuntimeError):
    """The app would run without the database fence in production."""


def enforcement_problem(
    status: dict[str, object] | None, *, error: str | None = None
) -> str | None:
    """Why the database fence is not in force, or None when it is.

    ``status`` is :func:`rls_status`'s answer (None when the probe itself
    failed, with ``error`` saying how). On SQLite there is no fence to
    enforce and nothing to report.
    """
    if status is not None and status.get("enforced") is None:
        return None  # SQLite
    if status is not None and status.get("enforced"):
        return None
    how = "it could not be verified" if status is None else "it is not in force"
    if status is None:
        detail = f"the probe failed ({error})"
    elif status.get("superuser"):
        detail = f"database role {status.get('role')!r} is a superuser"
    elif status.get("bypass_rls"):
        detail = f"database role {status.get('role')!r} has BYPASSRLS"
    else:
        detail = f"database role {status.get('role')!r} could not be inspected"
    return (
        f"Row-level security is NOT enforced: {how} because {detail}. PostgreSQL "
        "exempts such a role from the policies, so a query that forgets its user "
        "filter would return every user's rows. Create a non-superuser role that "
        "owns the tables and point DATABASE_URL at it (docs/deployment.md, "
        '"Row-level security needs a non-superuser app role"; the bundled stack '
        "does this with APP_DB_USER / APP_DB_PASSWORD)."
    )


def require_enforced(
    status: dict[str, object] | None,
    *,
    production: bool,
    required: bool,
    error: str | None = None,
) -> str | None:
    """Startup's decision: raise, warn, or pass.

    In production (``DEBUG`` off) with ``RLS_ENFORCEMENT_REQUIRED`` on, a
    fence that is not in force (or cannot be verified) stops the start with
    :class:`RLSNotEnforcedError`. With the requirement switched off, or in
    development, the problem is returned for the log and the app runs
    without the fence. Returns None when the fence is in force or there is
    none to enforce (SQLite).
    """
    problem = enforcement_problem(status, error=error)
    if problem is None:
        return None
    if production and required:
        raise RLSNotEnforcedError(
            problem + " The app refuses to start in production without the fence; "
            "RLS_ENFORCEMENT_REQUIRED=false overrides this for one boot at a time."
        )
    if production:
        return problem + " Running WITHOUT the fence because RLS_ENFORCEMENT_REQUIRED=false."
    return problem + " (development: continuing)"


# ── The policies (shared by migration 024 and the PostgreSQL tests) ──────

# (table, owner column, matter column) — rows the caller owns, or filed under
# a matter they belong to.
OWNED_TABLES: tuple[tuple[str, str, str | None], ...] = (
    ("chat_sessions", "user_id", "matter_id"),
    ("contract_analysis_runs", "user_id", "matter_id"),
    ("review_tables", "owner_id", "matter_id"),
    ("review_schemas", "owner_id", None),
    ("workflows", "owner_id", "matter_id"),
    ("deep_research_runs", "owner_id", "matter_id"),
    ("workspace_sessions", "user_id", "matter_id"),
)
# (table, parent table, key column) — visible when the parent row is.
CHILD_TABLES: tuple[tuple[str, str, str], ...] = (
    ("chat_messages", "chat_sessions", "session_id"),
    ("contract_parties", "contract_analysis_runs", "analysis_id"),
    ("contract_obligations", "contract_analysis_runs", "analysis_id"),
    ("contract_deadlines", "contract_analysis_runs", "analysis_id"),
    ("contract_defined_terms", "contract_analysis_runs", "analysis_id"),
    ("clause_deviations", "contract_analysis_runs", "analysis_id"),
    ("clause_tag_findings", "contract_analysis_runs", "analysis_id"),
    ("review_cells", "review_tables", "table_id"),
    ("review_runs", "review_tables", "table_id"),
    ("workflow_versions", "workflows", "workflow_id"),
    ("workflow_runs", "workflows", "workflow_id"),
    ("workflow_step_runs", "workflow_runs", "run_id"),
)
RLS_TABLES: tuple[str, ...] = tuple(t for t, *_ in OWNED_TABLES) + tuple(
    t for t, *_ in CHILD_TABLES
)

# One statement each: asyncpg prepares statements, so they cannot be batched.
RLS_FUNCTION_STATEMENTS: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION app_rls_user() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.user_id', true), '')
$$
""",
    # Only an explicit bypass lifts the fence. A session with no user set
    # (app_rls_user() IS NULL) matches no owner and no membership, so it
    # sees nothing; before migration 027 it saw everything.
    """
CREATE OR REPLACE FUNCTION app_rls_bypassed() RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT current_setting('app.rls_bypass', true) = 'on'
$$
""",
    """
CREATE OR REPLACE FUNCTION app_rls_visible(owner_id text, matter_id text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT app_rls_bypassed()
      OR owner_id = app_rls_user()
      OR (
        matter_id IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM matter_members mm
          WHERE mm.matter_id = app_rls_visible.matter_id
            AND mm.user_id = app_rls_user()
        )
        AND NOT EXISTS (
          SELECT 1 FROM matter_walls w
          WHERE w.matter_id = app_rls_visible.matter_id
            AND w.user_id = app_rls_user()
        )
      )
$$
""",
)


def _policy(table: str, predicate: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        # The application role owns the tables; without FORCE the owner is exempt.
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_scope ON {table}",
        f"CREATE POLICY {table}_scope ON {table} USING ({predicate}) WITH CHECK ({predicate})",
    ]


def rls_enable_statements() -> list[str]:
    """DDL that installs the fence (idempotent)."""
    statements: list[str] = list(RLS_FUNCTION_STATEMENTS)
    for table, owner, matter in OWNED_TABLES:
        matter_expr = f"{table}.{matter}" if matter else "NULL"
        statements += _policy(table, f"app_rls_visible({table}.{owner}, {matter_expr})")
    for table, parent, key in CHILD_TABLES:
        # DDL built from the identifiers in CHILD_TABLES above, never from input.
        statements += _policy(
            table,
            f"EXISTS (SELECT 1 FROM {parent} p WHERE p.id = {table}.{key})",  # nosec B608
        )
    return statements


def rls_disable_statements() -> list[str]:
    statements: list[str] = []
    for table in RLS_TABLES:
        statements += [
            f"DROP POLICY IF EXISTS {table}_scope ON {table}",
            f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
        ]
    statements += [
        "DROP FUNCTION IF EXISTS app_rls_visible(text, text)",
        "DROP FUNCTION IF EXISTS app_rls_bypassed()",
        "DROP FUNCTION IF EXISTS app_rls_unscoped()",  # before migration 027
        "DROP FUNCTION IF EXISTS app_rls_user()",
    ]
    return statements
