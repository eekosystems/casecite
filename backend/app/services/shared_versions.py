"""Version counters shared by every process, for the caches each keeps.

A process-local cache (the role permission matrix, the set of matters on
legal hold, the document registry) is correct for one process and stale
for the others once a second one runs. Instead of moving every cache to
Redis, each cache checks a counter in the ``shared_versions`` table — one
primary-key read, throttled — and reloads when the counter moved; whoever
changes the underlying data bumps the counter, in the same transaction when
there is one. Without the table (a database that predates migration 025)
reads answer 0 and bumps are no-ops, which is the single-process behaviour.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PERMISSIONS = "role_permissions"
LEGAL_HOLDS = "legal_holds"


def get_version(name: str) -> int:
    """Sync read (service code without an event loop in hand)."""
    try:
        from app.database import SyncSessionLocal
        from app.models.registry import SharedVersion

        with SyncSessionLocal() as db:
            row = db.get(SharedVersion, name)
            return int(row.version) if row is not None else 0
    except Exception as e:  # noqa: BLE001 - a missing table or a blip reads as "unchanged"
        logger.debug(f"shared version {name} unavailable: {e}")
        return 0


def bump(name: str) -> int:
    """Sync bump in its own transaction."""
    try:
        from app.database import SyncSessionLocal

        with SyncSessionLocal() as db:
            value = _bump_sync(db, name)
            db.commit()
            return value
    except Exception as e:  # noqa: BLE001
        logger.warning(f"shared version {name} could not be bumped: {e}")
        return 0


def _bump_sync(db, name: str) -> int:
    from app.models.registry import SharedVersion

    row = db.get(SharedVersion, name)
    if row is None:
        row = SharedVersion(name=name, version=0)
        db.add(row)
    row.version = int(row.version or 0) + 1
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    db.flush()
    return int(row.version)


async def get_version_async(name: str) -> int:
    try:
        from app.database import AsyncSessionLocal
        from app.models.registry import SharedVersion

        async with AsyncSessionLocal() as db:
            row = await db.get(SharedVersion, name)
            return int(row.version) if row is not None else 0
    except Exception as e:  # noqa: BLE001
        logger.debug(f"shared version {name} unavailable: {e}")
        return 0


async def bump_async(name: str) -> int:
    """Async bump in its own transaction (after the data it describes committed)."""
    try:
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            value = await bump_in_session(db, name)
            await db.commit()
            return value
    except Exception as e:  # noqa: BLE001
        logger.warning(f"shared version {name} could not be bumped: {e}")
        return 0


async def bump_in_session(db: AsyncSession, name: str) -> int:
    """Bump inside the caller's transaction, so the counter and the change it
    announces commit together (or not at all)."""
    from app.models.registry import SharedVersion

    row = await db.get(SharedVersion, name)
    if row is None:
        row = SharedVersion(name=name, version=0)
        db.add(row)
    row.version = int(row.version or 0) + 1
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await db.flush()
    return int(row.version)
