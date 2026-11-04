"""PostgreSQL row-level security on user content.

Installs the helper functions and, on every content table, a policy that
admits a row only when the session is unscoped (no user set: background
jobs, startup, migrations), bypassed (an admin-gated endpoint), the caller
owns the row, or the caller is a member — and not walled off — of its
matter. Child tables defer to their parent. Row-level security is FORCED so
the table-owning application role is not exempt. See app/rls.py, which also
supplies the per-transaction ``SET LOCAL`` of the scope.

A no-op on SQLite, which has no row-level security.

Revision ID: 023
Revises: 022
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str = "022"
branch_labels = None
depends_on = None


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if not _postgres():
        return
    from app.rls import rls_enable_statements

    present = _existing_tables()
    for statement in rls_enable_statements():
        table = statement.split()[2] if statement.startswith("ALTER TABLE") else None
        if statement.startswith(("DROP POLICY", "CREATE POLICY")):
            table = statement.split(" ON ")[1].split()[0]
        if table and table not in present:
            continue  # a table the backstop has not created yet gets its policy on the next run
        op.execute(statement)


def downgrade() -> None:
    if not _postgres():
        return
    from app.rls import rls_disable_statements

    present = _existing_tables()
    for statement in rls_disable_statements():
        if " ON " in statement:
            table = statement.split(" ON ")[1].split()[0]
        elif statement.startswith("ALTER TABLE"):
            table = statement.split()[2]
        else:
            table = None
        if table and table not in present:
            continue
        op.execute(statement)
