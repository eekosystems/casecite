"""
TOTP MFA router.

Enrollment/management (setup, enable, disable, status) require an authenticated
session. The second login step (verify) is public: it takes the short-lived MFA
challenge issued by /auth/login plus a TOTP or recovery code, and returns full
tokens. See app.services.mfa.
"""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
)
from app.middleware.security import account_lockout
from app.models.db_models import User as DBUser
from app.models.responses.auth import LoginResponse
from app.models.schemas import (
    MfaDisableRequest,
    MfaEnableRequest,
    MfaEnableResponse,
    MfaSetupResponse,
    MfaStatusResponse,
    MfaVerifyRequest,
)
from app.services import mfa as mfa_service
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData, auth_service, get_current_user
from app.services.demo_login import DEMO_LOGIN_LOCKED_MESSAGE, is_demo_login_email
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/mfa", tags=["mfa"])


async def _load_user(db: AsyncSession, user_id: str) -> DBUser:
    result = await db.execute(select(DBUser).where(DBUser.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")
    return user


def _management_lockout_key(user: DBUser) -> str:
    """Lockout bucket for the authenticated MFA-management endpoints.

    Separate from the login bucket (keyed by the bare email): guessing codes
    from a hijacked session locks MFA management, not the owner's ability to
    sign in.
    """
    return f"mfa-manage:{user.email}"


def _refuse_if_management_locked(user: DBUser) -> None:
    if account_lockout.is_account_locked(_management_lockout_key(user)):
        raise RateLimitedError(
            "Too many failed attempts. Try again later.",
        )


async def _record_management_failure(user: DBUser, request: Request, action: str) -> None:
    """Count a wrong code/password toward the management lockout and audit it."""
    account_lockout.record_login_failure(_management_lockout_key(user))
    await audit_service.log_event(
        event_type=AuditEventType.MFA_FAILURE,
        user_id=user.id,
        user_email=user.email,
        details={"action": action},
        ip_address=get_client_ip(request),
        success=False,
    )


@router.get("/status", response_model=MfaStatusResponse)
async def mfa_status(
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MfaStatusResponse:
    """Whether MFA is currently enabled for the signed-in user."""
    user = await _load_user(db, current_user.user_id)
    return MfaStatusResponse(
        enabled=bool(getattr(user, "mfa_enabled", False)),
        recovery_codes_remaining=len(user.mfa_recovery_codes or []),
    )


@router.post("/setup", response_model=MfaSetupResponse)
async def mfa_setup(
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MfaSetupResponse:
    """Begin enrollment: generate a secret (stored encrypted, not yet enabled)."""
    user = await _load_user(db, current_user.user_id)
    if is_demo_login_email(user.email):
        # Shared public account: a second factor would lock every other visitor out.
        raise ForbiddenError(DEMO_LOGIN_LOCKED_MESSAGE)
    if user.mfa_enabled:
        # Never let a session-only attacker reset/disable an active second factor.
        # Re-enrollment requires disabling first, which demands a valid code.
        raise ConflictError(
            "MFA is already enabled. Disable it (requires a valid code) before re-enrolling.",
        )
    secret = mfa_service.generate_secret()
    user.mfa_secret = mfa_service.encrypt_secret(secret)
    # Not enabled until the user proves possession via /enable.
    user.mfa_enabled = False
    await db.commit()
    return MfaSetupResponse(
        secret=secret,
        provisioning_uri=mfa_service.provisioning_uri(secret, user.email),
    )


@router.post("/enable", response_model=MfaEnableResponse)
async def mfa_enable(
    body: MfaEnableRequest,
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MfaEnableResponse:
    """Confirm enrollment with a code, enable MFA, and return recovery codes once."""
    user = await _load_user(db, current_user.user_id)
    if user.mfa_enabled:
        raise ConflictError("MFA is already enabled.")
    if not user.mfa_secret:
        raise BadRequestError("Start MFA setup first.")
    _refuse_if_management_locked(user)

    secret = mfa_service.decrypt_secret(user.mfa_secret)
    if not mfa_service.verify_totp(secret, body.code):
        await _record_management_failure(user, request, "enable")
        raise BadRequestError("Invalid code. Try again.")

    plaintext_codes, hashed_codes = mfa_service.generate_recovery_codes()
    user.mfa_enabled = True
    user.mfa_recovery_codes = hashed_codes
    await db.commit()
    account_lockout.clear_account_lockout(_management_lockout_key(user))

    await audit_service.log_event(
        event_type=AuditEventType.MFA_ENABLED,
        user_id=user.id,
        user_email=user.email,
        ip_address=get_client_ip(request),
    )
    return MfaEnableResponse(recovery_codes=plaintext_codes)


@router.post("/disable")
async def mfa_disable(
    body: MfaDisableRequest,
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Disable MFA after proving the password AND a current TOTP or recovery code.

    Both are required so that a hijacked session alone — which holds neither —
    cannot remove the second factor. Accounts without a local password (SSO)
    prove the code only.
    """
    from app.services.passwords import verify_password

    user = await _load_user(db, current_user.user_id)
    if not user.mfa_enabled or not user.mfa_secret:
        return {"status": "mfa_not_enabled"}
    _refuse_if_management_locked(user)

    if user.password_hash:
        if not body.password:
            raise BadRequestError("Your current password is required to disable MFA.")
        if not await asyncio.to_thread(verify_password, body.password, user.password_hash):
            await _record_management_failure(user, request, "disable_wrong_password")
            raise BadRequestError("Invalid password or code.")

    secret = mfa_service.decrypt_secret(user.mfa_secret)
    ok = mfa_service.verify_totp_once(user.id, secret, body.code)
    if not ok:
        consumed, _remaining = mfa_service.consume_recovery_code(
            body.code, user.mfa_recovery_codes or []
        )
        ok = consumed
    if not ok:
        await _record_management_failure(user, request, "disable_invalid_code")
        raise BadRequestError("Invalid password or code.")

    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_recovery_codes = None
    await db.commit()
    account_lockout.clear_account_lockout(_management_lockout_key(user))

    await audit_service.log_event(
        event_type=AuditEventType.MFA_DISABLED,
        user_id=user.id,
        user_email=user.email,
        ip_address=get_client_ip(request),
    )
    return {"status": "mfa_disabled"}


@router.post("/recovery-codes", response_model=MfaEnableResponse)
async def mfa_regenerate_recovery_codes(
    body: MfaEnableRequest,
    request: Request,
    current_user: TokenData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MfaEnableResponse:
    """Regenerate recovery codes after proving a current TOTP code.

    TOTP only — a stolen recovery code must not be able to mint a fresh set.
    All previous codes are invalidated.
    """
    user = await _load_user(db, current_user.user_id)
    if not user.mfa_enabled or not user.mfa_secret:
        raise BadRequestError("MFA is not enabled.")
    _refuse_if_management_locked(user)

    secret = mfa_service.decrypt_secret(user.mfa_secret)
    if not mfa_service.verify_totp_once(user.id, secret, body.code):
        await _record_management_failure(user, request, "recovery_codes")
        raise BadRequestError("Invalid code.")

    plaintext_codes, hashed_codes = mfa_service.generate_recovery_codes()
    user.mfa_recovery_codes = hashed_codes
    await db.commit()
    account_lockout.clear_account_lockout(_management_lockout_key(user))

    await audit_service.log_event(
        event_type=AuditEventType.MFA_ENABLED,
        user_id=user.id,
        user_email=user.email,
        details={"action": "recovery_codes_regenerated"},
        ip_address=get_client_ip(request),
    )
    return MfaEnableResponse(recovery_codes=plaintext_codes)


@router.post("/verify", response_model=LoginResponse)
async def mfa_verify(
    body: MfaVerifyRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Second login step: exchange the MFA challenge + code for tokens."""
    from app.routers.auth import start_session, user_from_row, user_info

    user_id = mfa_service.verify_challenge_token(body.mfa_token)
    if not user_id:
        raise UnauthorizedError("MFA session expired. Please sign in again.")

    user_row = await _load_user(db, user_id)
    if not user_row.is_active or not user_row.mfa_enabled or not user_row.mfa_secret:
        raise UnauthorizedError("MFA is not active for this account.")

    # Per-account brute-force brake: failed second-factor attempts count toward
    # the same lockout as failed passwords, so a stolen password can't be paired
    # with distributed TOTP guessing across many IPs. /auth/login does not clear
    # this counter for MFA accounts — only a completed sign-in (below) does.
    if account_lockout.is_account_locked(user_row.email):
        raise RateLimitedError(
            "Too many failed attempts. Try again later.",
        )

    secret = mfa_service.decrypt_secret(user_row.mfa_secret)
    used_recovery = False
    if not mfa_service.verify_totp_once(user_row.id, secret, body.code):
        consumed, remaining = mfa_service.consume_recovery_code(
            body.code, user_row.mfa_recovery_codes or []
        )
        if not consumed:
            account_lockout.record_login_failure(user_row.email)
            await audit_service.log_event(
                event_type=AuditEventType.LOGIN_FAILURE,
                user_id=user_row.id,
                user_email=user_row.email,
                details={"method": "mfa", "reason": "invalid_code"},
                ip_address=get_client_ip(request),
                success=False,
            )
            raise UnauthorizedError("Invalid code.")
        user_row.mfa_recovery_codes = remaining
        used_recovery = True
        await db.commit()

    # Spend the challenge now that the second factor is proven — a completed
    # challenge must not be replayable to mint a second set of tokens.
    if not mfa_service.consume_challenge_token(body.mfa_token):
        raise UnauthorizedError("MFA session expired. Please sign in again.")

    user_row.last_login = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()

    user = user_from_row(user_row)
    access_token = start_session(request, response, user)
    account_lockout.clear_account_lockout(user.email)

    await audit_service.log_event(
        event_type=AuditEventType.LOGIN_SUCCESS,
        user_id=user.id,
        user_email=user.email,
        details={"method": "mfa", "recovery_code": used_recovery},
        ip_address=get_client_ip(request),
    )

    return LoginResponse(
        access_token=access_token,
        expires_in=int(auth_service.access_token_expire.total_seconds()),
        user=user_info(user, user_row),
    )
