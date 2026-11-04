"""Row-level security admits nothing to a session with no user.

Migration 024's policies let a session with no ``app.user_id`` see every
row, so that background work could run. That made forgetting to set the
user a way to see everything. The policy functions are replaced so that
only an explicit bypass (``SET app.rls_bypass = 'on'``, which the retention
task and the Alembic session set on purpose; see app/rls.py) lifts the
fence; a session with no user matches no owner and no membership and sees
nothing. The table policies themselves are unchanged: they call
``app_rls_visible``, whose body is what changes here.

A no-op on SQLite, which has no row-level security. Idempotent: a fresh
install's migration 024 already created the new functions from app/rls.py.

Revision ID: 027
Revises: 026
Create Date: 2026-11-04
"""

from __future__ import annotations

from alembic import op

revision: str = "027"
down_revision: str = "026"
branch_labels = None
depends_on = None


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _postgres():
        return
    from app.rls import RLS_FUNCTION_STATEMENTS

    for statement in RLS_FUNCTION_STATEMENTS:  # CREATE OR REPLACE, in dependency order
        op.execute(statement)
    op.execute("DROP FUNCTION IF EXISTS app_rls_unscoped()")


def downgrade() -> None:
    if not _postgres():
        return
    op.execute(
        """
CREATE OR REPLACE FUNCTION app_rls_unscoped() RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT app_rls_user() IS NULL
      OR current_setting('app.rls_bypass', true) = 'on'
$$
"""
    )
    op.execute(
        """
CREATE OR REPLACE FUNCTION app_rls_visible(owner_id text, matter_id text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT app_rls_unscoped()
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
"""
    )
    op.execute("DROP FUNCTION IF EXISTS app_rls_bypassed()")
