"""
Instance Settings — persistent, encrypted, instance-wide secrets.

Unlike per-user keys (Redis/TTL cached), these are stored in the database so an
operator can set an instance-wide value once from the admin UI and have it
persist across restarts. Used for the CourtListener API token (free; only raises
rate limits) and any future instance-level integration credentials.
"""

import logging
from datetime import datetime

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.tracking import InstanceSecretDB
from app.services.encryption import encryption_service

logger = logging.getLogger(__name__)


class SecretUnreadableError(RuntimeError):
    """A stored instance secret exists but cannot be decrypted.

    Distinct from "unset" (``None``): callers that fail closed on an
    unreadable policy (role permissions) must be able to tell a missing
    override from one that is present but corrupt or encrypted under a
    different key.
    """


async def get_secret_strict(name: str) -> str | None:
    """Return the decrypted instance secret, ``None`` if unset.

    Raises ``SecretUnreadableError`` when the row exists but cannot be
    decrypted, and propagates database errors.
    """
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(InstanceSecretDB).where(InstanceSecretDB.name == name))
        ).scalar_one_or_none()
        if not row:
            return None
        try:
            return encryption_service.decrypt_string(row.value_encrypted)
        except Exception as e:
            logger.warning("Failed to decrypt instance secret %s: %s", name, e)
            raise SecretUnreadableError(f"instance secret {name!r} cannot be decrypted") from e


async def get_secret(name: str) -> str | None:
    """Return the decrypted instance secret, or None if unset/undecryptable."""
    try:
        return await get_secret_strict(name)
    except SecretUnreadableError:
        return None


async def set_secret(name: str, value: str) -> None:
    """Upsert an encrypted instance secret."""
    enc = encryption_service.encrypt_string(value)
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(InstanceSecretDB).where(InstanceSecretDB.name == name))
        ).scalar_one_or_none()
        if row:
            row.value_encrypted = enc
            row.updated_at = datetime.utcnow()
        else:
            db.add(InstanceSecretDB(name=name, value_encrypted=enc))
        await db.commit()


async def delete_secret(name: str) -> None:
    """Remove an instance secret (falls back to the .env value, if any)."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(InstanceSecretDB).where(InstanceSecretDB.name == name))
        ).scalar_one_or_none()
        if row:
            await db.delete(row)
            await db.commit()
