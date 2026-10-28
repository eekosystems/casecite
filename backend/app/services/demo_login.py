"""
Public demo account (DEMO_LOGIN_EMAIL / DEMO_LOGIN_PASSWORD).

Off unless both settings are set. When they are, one shared account exists
whose credentials are public: GET /branding returns them and the sign-in form
prefills them. Because every visitor shares it, the account must be impossible
to take hostage. The auth and MFA routers consult ``is_demo_login_email`` to
refuse password changes, email resets, MFA enrolment and role changes, and to
exempt it from the login lockout; ``ensure_demo_login_account`` puts it back
in its known state at every startup regardless of what the last visitor did.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import User as DBUser
from app.services.passwords import hash_password

logger = logging.getLogger(__name__)

DEMO_LOGIN_NAME = "Demo User"
DEMO_LOGIN_ROLES = ["attorney"]

# The message every refused self-service action returns, so the UI can explain.
DEMO_LOGIN_LOCKED_MESSAGE = (
    "This is the shared demo account: its password, email and security settings cannot be changed."
)


def is_demo_login_email(email: "str | Column[str] | None") -> bool:
    """True when ``email`` is the configured public demo account.

    Accepts a plain string or a ``User.email`` attribute (a str at runtime).
    """
    if not settings.demo_login_enabled or not email:
        return False
    return str(email).strip().lower() == settings.demo_login_email


async def ensure_demo_login_account(db: AsyncSession) -> DBUser | None:
    """Create the demo account, or reset an existing one to its known state.

    Resets everything a visitor (or an admin) could have changed: password,
    role, active flag, MFA, must-change-password. Returns the row, or None
    when the demo login is not configured.
    """
    if not settings.demo_login_enabled:
        return None

    email = settings.demo_login_email
    result = await db.execute(select(DBUser).where(DBUser.email == email))
    user = result.scalar_one_or_none()
    # PBKDF2 (600k iterations) is CPU-bound — keep it off the event loop.
    password_hash = await asyncio.to_thread(hash_password, settings.demo_login_password)
    now = datetime.now(UTC).replace(tzinfo=None)

    if user is None:
        user = DBUser(
            id=str(uuid.uuid4()),
            email=email,
            name=DEMO_LOGIN_NAME,
            password_hash=password_hash,
            roles=list(DEMO_LOGIN_ROLES),
            email_verified=True,
            is_active=True,
            must_change_password=False,
            password_changed_at=now,
        )
        db.add(user)
        action = "created"
    else:
        await db.execute(
            update(DBUser)
            .where(DBUser.id == user.id)
            .values(
                password_hash=password_hash,
                roles=list(DEMO_LOGIN_ROLES),
                is_active=True,
                must_change_password=False,
                mfa_enabled=False,
                mfa_secret=None,
                mfa_recovery_codes=None,
                password_changed_at=now,
                # Invalidate tokens issued against the previous state.
                token_version=DBUser.token_version + 1,
            )
        )
        action = "reset"

    await db.commit()
    await db.refresh(user)
    logger.warning(
        "PUBLIC DEMO LOGIN is enabled: account %s %s with the attorney role. Its "
        "credentials are published by GET /branding. Do not run this on an instance "
        "that holds real client data.",
        email,
        action,
    )
    return user
