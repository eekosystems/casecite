"""Admin user-management router.

Lets an admin list users, invite new users with an assigned role, change
existing users' roles, deactivate or delete accounts, force a sign-out, and
reset a user's MFA. Admin-only. Self-hosted: "invite" creates an active
account with a generated temporary password (returned once to the admin to
hand off) since email delivery may not be configured; the invited user must
replace it at first sign-in before anything else is reachable.
"""

import asyncio
import logging
import secrets
import string
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.models.db_models import User as DBUser
from app.models.schemas import (
    AdminUserItem,
    AdminUserListResponse,
    InviteUserRequest,
    InviteUserResponse,
    SetUserActiveRequest,
    UpdateRolePermissionsRequest,
    UpdateUserRoleRequest,
)
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData, UserRole, auth_service
from app.services.demo_login import is_demo_login_email
from app.services.permissions import require_permission
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])

_VALID_ROLES = {r.value for r in UserRole}


def _to_item(u: DBUser) -> AdminUserItem:
    roles = (
        u.roles
        if isinstance(u.roles, list)
        else (["attorney"] if u.roles is None else list(u.roles))
    )
    return AdminUserItem(
        id=u.id,
        email=u.email,
        name=u.name or "",
        roles=roles,
        is_active=bool(u.is_active),
        mfa_enabled=bool(u.mfa_enabled),
        must_change_password=bool(u.must_change_password),
        created_at=u.created_at,
        last_login=u.last_login,
    )


def _generate_temp_password() -> str:
    """Generate a random password that satisfies the standard complexity policy."""
    alphabet = string.ascii_letters + string.digits
    specials = "!@#$%^&*-_"
    core = "".join(secrets.choice(alphabet) for _ in range(14))
    # Guarantee one of each required class.
    return (
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice(specials)
        + core
    )


async def _end_all_sessions(db: AsyncSession, target: DBUser) -> int:
    """Invalidate every token of ``target`` on every device; returns sessions ended.

    Bumps users.token_version (outstanding access and refresh tokens carry the
    old version and are rejected from now on) and drops the live sessions.
    """
    target.token_version = int(target.token_version or 0) + 1
    await db.commit()
    return auth_service.revoke_user_sessions(target.id)


async def _get_target(db: AsyncSession, user_id: str) -> DBUser:
    target = (await db.execute(select(DBUser).where(DBUser.id == user_id))).scalar_one_or_none()
    if target is None:
        raise NotFoundError("User not found")
    return target


@router.get("", response_model=AdminUserListResponse)
async def list_users(
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> AdminUserListResponse:
    """List all user accounts."""
    result = await db.execute(select(DBUser).order_by(DBUser.created_at))
    users = result.scalars().all()
    return AdminUserListResponse(users=[_to_item(u) for u in users])


@router.post("/invite", response_model=InviteUserResponse, status_code=201)
async def invite_user(
    body: InviteUserRequest,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> InviteUserResponse:
    """Create a user with the given role and a temporary password.

    The temporary password is returned once, to the inviting admin. Because the
    admin has seen it, the account is flagged ``must_change_password``: until
    the invited user sets their own password, every API call except
    change-password/logout/me is refused.
    """
    from app.services.passwords import hash_password

    if body.role not in _VALID_ROLES:
        raise BadRequestError(f"Invalid role. Must be one of: {sorted(_VALID_ROLES)}")

    email = body.email.lower().strip()
    existing = await db.execute(select(DBUser).where(DBUser.email == email))
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("An account with this email already exists.")

    temp_password = _generate_temp_password()
    # PBKDF2 (600k iterations) is CPU-bound — run it off the event loop.
    temp_password_hash = await asyncio.to_thread(hash_password, temp_password)
    db_user = DBUser(
        id=str(uuid.uuid4()),
        email=email,
        name=body.name.strip(),
        password_hash=temp_password_hash,
        roles=[body.role],
        email_verified=True,
        is_active=True,
        must_change_password=True,
        password_changed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(db_user)
    await db.flush()

    # Audit BEFORE the commit. With AUDIT_FAIL_CLOSED an unwritable trail must
    # refuse the invite outright — not create the account and then fail the
    # response, which would lose the one-time temporary password.
    await audit_service.log_event(
        event_type=AuditEventType.USER_CREATE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=db_user.id,
        ip_address=get_client_ip(request),
        details={"action": "invite_user", "invited_email": email, "role": body.role},
    )

    await db.commit()
    await db.refresh(db_user)

    return InviteUserResponse(user=_to_item(db_user), temporary_password=temp_password)


# ── Role permissions (literal paths BEFORE the /{user_id} catch-alls) ──


async def _roles_payload() -> dict:
    """The matrix the UI renders: catalog, defaults, current, customized."""
    from app.services import permissions as perm

    matrix = await perm.get_role_permissions()
    return {
        "permissions": perm.PERMISSIONS,
        "roles": perm.ROLES,
        "defaults": {r: sorted(perm.DEFAULT_ROLE_PERMISSIONS[r]) for r in perm.ROLES},
        "assigned": {r: sorted(matrix[r]) for r in perm.ROLES},
        "customized": [r for r in perm.ROLES if matrix[r] != perm.DEFAULT_ROLE_PERMISSIONS[r]],
        "locked": {r: sorted(keys) for r, keys in perm.LOCKED.items()},
    }


@router.get("/roles")
async def get_role_permissions(
    current_user: TokenData = require_permission("admin.users"),
) -> dict:
    """Permission catalog plus each role's current and default grants."""
    return await _roles_payload()


@router.put("/roles/{role}")
async def update_role_permissions(
    role: str,
    body: UpdateRolePermissionsRequest,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
) -> dict:
    """Set one role's permission list. Locked permissions are always re-added."""
    from app.services import permissions as perm

    try:
        await perm.set_role_permissions(role, body.permissions)
    except ValueError as e:
        raise BadRequestError(str(e))

    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="role",
        resource_id=role,
        ip_address=get_client_ip(request),
        details={
            "action": "role_permissions_set",
            "role": role,
            "permissions": sorted(body.permissions),
        },
    )
    return await _roles_payload()


@router.post("/roles/reset")
async def reset_role_permissions(
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
) -> dict:
    """Drop every customization — all roles return to the shipped defaults."""
    from app.services import permissions as perm

    await perm.reset_role_permissions()

    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="role",
        ip_address=get_client_ip(request),
        details={"action": "role_permissions_reset"},
    )
    return await _roles_payload()


@router.patch("/{user_id}/role", response_model=AdminUserItem)
async def update_user_role(
    user_id: str,
    body: UpdateUserRoleRequest,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> AdminUserItem:
    """Change a user's role. Prevents removing the last remaining admin."""
    if body.role not in _VALID_ROLES:
        raise BadRequestError(f"Invalid role. Must be one of: {sorted(_VALID_ROLES)}")

    result = await db.execute(select(DBUser).where(DBUser.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise NotFoundError("User not found")
    if is_demo_login_email(target.email):
        # The shared public demo account has a public password; giving it any
        # other role hands that role to everyone on the internet.
        raise ForbiddenError("The demo account's role is fixed by configuration.")

    current_roles = target.roles if isinstance(target.roles, list) else []
    was_admin = UserRole.ADMIN.value in current_roles
    becoming_non_admin = body.role != UserRole.ADMIN.value

    # Guard against locking everyone out: don't demote the last ACTIVE admin
    # (an inactive admin can't sign in, so counting them would allow lockout).
    # JSON column membership is dialect-specific, so count in Python.
    if was_admin and becoming_non_admin and target.is_active:
        all_users = (await db.execute(select(DBUser))).scalars().all()
        active_admin_count = sum(
            1
            for u in all_users
            if u.is_active and isinstance(u.roles, list) and UserRole.ADMIN.value in u.roles
        )
        if active_admin_count <= 1:
            raise BadRequestError("Cannot remove the last remaining admin.")

    target.roles = [body.role]
    # Revoke the user's sessions so tokens carrying the OLD role stop working
    # immediately (roles are embedded in the access token at issue time).
    await _end_all_sessions(db, target)
    await db.refresh(target)

    await audit_service.log_event(
        event_type=AuditEventType.USER_ROLE_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=target.id,
        ip_address=get_client_ip(request),
        details={"action": "update_role", "new_role": body.role},
    )

    return _to_item(target)


@router.patch("/{user_id}/active", response_model=AdminUserItem)
async def set_user_active(
    user_id: str,
    body: SetUserActiveRequest,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> AdminUserItem:
    """Activate or deactivate (offboard) a user account.

    A deactivated user cannot authenticate: get_current_user rejects inactive
    accounts, and their existing sessions are torn down here so access is cut
    immediately. Guards against self-lockout and disabling the last admin.
    """
    result = await db.execute(select(DBUser).where(DBUser.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise NotFoundError("User not found")

    if not body.is_active:
        if target.id == current_user.user_id:
            raise BadRequestError("You cannot deactivate your own account.")
        # Don't allow disabling the last remaining admin.
        target_is_admin = isinstance(target.roles, list) and UserRole.ADMIN.value in target.roles
        if target_is_admin:
            all_users = (await db.execute(select(DBUser))).scalars().all()
            active_admins = sum(
                1
                for u in all_users
                if u.is_active and isinstance(u.roles, list) and UserRole.ADMIN.value in u.roles
            )
            if active_admins <= 1:
                raise BadRequestError("Cannot deactivate the last remaining admin.")

    target.is_active = body.is_active
    if body.is_active:
        await db.commit()
    else:
        # On deactivation, revoke sessions/tokens so access is cut immediately
        # rather than lingering until token expiry.
        await _end_all_sessions(db, target)
    await db.refresh(target)

    await audit_service.log_event(
        event_type=AuditEventType.USER_UPDATE if body.is_active else AuditEventType.USER_DEACTIVATE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=target.id,
        ip_address=get_client_ip(request),
        details={"action": "activate" if body.is_active else "deactivate"},
    )

    return _to_item(target)


@router.post("/{user_id}/sessions/revoke")
async def force_sign_out(
    user_id: str,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sign a user out everywhere (incident containment, lost device).

    Ends every live session and invalidates every outstanding access and
    refresh token of the account. The account stays active: the user can sign
    in again with their credentials. To keep them out, deactivate instead.
    """
    target = await _get_target(db, user_id)
    ended = await _end_all_sessions(db, target)

    await audit_service.log_event(
        event_type=AuditEventType.USER_UPDATE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=target.id,
        ip_address=get_client_ip(request),
        details={"action": "force_sign_out", "sessions_revoked": ended},
    )
    return {"status": "signed_out", "user_id": target.id, "sessions_revoked": ended}


@router.post("/{user_id}/mfa/reset")
async def reset_user_mfa(
    user_id: str,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a user's MFA enrollment (lost authenticator and no recovery codes).

    Clears the TOTP secret and recovery codes and signs the user out
    everywhere. With REQUIRE_MFA on, they must enrol again at next sign-in.
    An admin cannot reset their own MFA here — that goes through
    /auth/mfa/disable, which demands their password and a valid code.
    """
    target = await _get_target(db, user_id)
    if target.id == current_user.user_id:
        raise BadRequestError(
            "Use your own security settings to disable MFA on your account.",
        )

    had_mfa = bool(target.mfa_enabled)
    target.mfa_enabled = False
    target.mfa_secret = None
    target.mfa_recovery_codes = None
    ended = await _end_all_sessions(db, target)

    await audit_service.log_event(
        event_type=AuditEventType.MFA_DISABLED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=target.id,
        ip_address=get_client_ip(request),
        details={"action": "admin_mfa_reset", "had_mfa": had_mfa, "sessions_revoked": ended},
    )
    return {"status": "mfa_reset", "user_id": target.id}


def _row(obj) -> dict:
    """One ORM row as JSON-serialisable column values."""
    out = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        out[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return out


@router.get("/{user_id}/export")
async def export_user_data(
    user_id: str,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Export everything the instance holds about a user, as JSON.

    Included: profile; document metadata and folders; saved settings (playbook,
    practice profile, custom prompts); chat sessions with their messages;
    contract analyses with their parties, obligations, deadlines, defined terms
    and clause findings; workspace
    sessions; matters the user owns and matters they are a member of.

    Not included: the uploaded files themselves (download those per document),
    stored API keys and connector tokens (secrets), and audit-log entries (use
    the audit export).
    """
    from app.models.chat_sessions import ChatMessageDB, ChatSessionDB
    from app.models.clause_intel import (
        ClauseDeviationFinding,
        ClauseTagFinding,
        ContractAnalysisRun,
    )
    from app.models.contract_analysis import (
        ContractDeadline,
        ContractDefinedTerm,
        ContractObligation,
        ContractParty,
    )
    from app.models.matters import Matter, MatterMember
    from app.models.tracking import WorkspaceSessionDB
    from app.services.documents import document_service
    from app.services.user_settings import load_user_settings

    target = (await db.execute(select(DBUser).where(DBUser.id == user_id))).scalar_one_or_none()
    if target is None:
        raise NotFoundError("User not found")

    async def _rows(model, *where) -> list:
        return list((await db.execute(select(model).where(*where))).scalars().all())

    chat_sessions = []
    for session in await _rows(ChatSessionDB, ChatSessionDB.user_id == user_id):
        messages = await _rows(ChatMessageDB, ChatMessageDB.session_id == session.id)
        messages.sort(key=lambda m: m.created_at)
        chat_sessions.append({**_row(session), "messages": [_row(m) for m in messages]})

    contract_analyses = []
    for run in await _rows(ContractAnalysisRun, ContractAnalysisRun.user_id == user_id):
        children = {}
        for key, model in (
            ("parties", ContractParty),
            ("obligations", ContractObligation),
            ("deadlines", ContractDeadline),
            ("defined_terms", ContractDefinedTerm),
            ("clause_deviations", ClauseDeviationFinding),
            ("clause_tags", ClauseTagFinding),
        ):
            children[key] = [_row(r) for r in await _rows(model, model.analysis_id == run.id)]
        contract_analyses.append({**_row(run), **children})

    workspace_sessions = [
        _row(w) for w in await _rows(WorkspaceSessionDB, WorkspaceSessionDB.user_id == user_id)
    ]

    owned = await _rows(Matter, Matter.owner_id == user_id)
    memberships = await _rows(MatterMember, MatterMember.user_id == user_id)
    owned_ids = {m.id for m in owned}
    joined_ids = [m.matter_id for m in memberships if m.matter_id not in owned_ids]
    joined = await _rows(Matter, Matter.id.in_(joined_ids)) if joined_ids else []

    export = {
        "profile": {
            "id": target.id,
            "email": target.email,
            "name": target.name,
            "roles": target.roles,
            "created_at": target.created_at.isoformat() if target.created_at else None,
            "last_login": target.last_login.isoformat() if target.last_login else None,
        },
        **document_service.export_user_data(user_id),
        "settings": load_user_settings(user_id).model_dump(mode="json"),
        "chat_sessions": chat_sessions,
        "contract_analyses": contract_analyses,
        "workspace_sessions": workspace_sessions,
        "matters": {
            "owned": [_row(m) for m in owned],
            "member_of": [_row(m) for m in joined],
        },
    }

    await audit_service.log_event(
        event_type=AuditEventType.DATA_EXPORT,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=user_id,
        ip_address=get_client_ip(request),
        details={
            "action": "user_data_export",
            "chat_sessions": len(chat_sessions),
            "contract_analyses": len(contract_analyses),
            "workspace_sessions": len(workspace_sessions),
        },
    )
    return export


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    current_user: TokenData = require_permission("admin.users"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a user and erase their data (right-to-erasure, GDPR/CCPA).

    Purges uploaded files + vector embeddings + the encrypted key store +
    connector OAuth tokens (revoked provider-side where cheap) + per-user
    settings, then deletes the DB row (relational data cascades). Guards
    against deleting yourself or the last remaining admin.
    """
    from app.services.connectors.base import delete_user_credentials
    from app.services.documents import document_service
    from app.services.key_storage import delete_user_keys
    from app.services.user_settings import delete_user_settings

    target = (await db.execute(select(DBUser).where(DBUser.id == user_id))).scalar_one_or_none()
    if target is None:
        raise NotFoundError("User not found")

    if target.id == current_user.user_id:
        raise BadRequestError("You cannot delete your own account.")

    if target.is_active and isinstance(target.roles, list) and UserRole.ADMIN.value in target.roles:
        # Count ACTIVE admins only — an inactive admin row must not satisfy the
        # guard, or deleting the last usable admin locks the firm out. (Deleting
        # an inactive admin can't cause lockout, so the guard skips them.)
        all_users = (await db.execute(select(DBUser))).scalars().all()
        active_admin_count = sum(
            1
            for u in all_users
            if u.is_active and isinstance(u.roles, list) and UserRole.ADMIN.value in u.roles
        )
        if active_admin_count <= 1:
            raise BadRequestError("Cannot delete the last remaining admin.")

    target_email = target.email

    # Stored API keys go first, before anything else is touched: if the key
    # store cannot be written the deletion stops here with the account intact,
    # rather than removing the user while their secrets stay on disk.
    try:
        delete_user_keys(user_id)
    except RuntimeError:
        logger.error(f"User {user_id} not deleted: the API key store could not be updated")
        raise ServiceUnavailableError(
            "The user's stored API keys could not be removed, so the account "
            "was not deleted. Nothing was changed; check the data volume and try again."
        )

    # Revoke sessions/tokens next so access stops immediately.
    auth_service.revoke_user_sessions(user_id)
    # Tombstone the user id so any still-unexpired token is rejected on every
    # worker even after the DB row (and its is_active check) is gone.
    auth_service.revoke_user(user_id)

    # Purge user-owned data outside the relational DB: documents + vectors,
    # BYOK keys, connector OAuth credential files (revoking the grant where the
    # provider offers a cheap endpoint), and the per-user settings file.
    purge_result = await document_service.purge_user_data(user_id)
    purge_result.update(await delete_user_credentials(user_id))
    purge_result["settings_files_deleted"] = delete_user_settings(user_id)

    # Purge user-owned rows whose tables carry user_id as a plain column with no
    # ON DELETE CASCADE to users — deleting the users row alone would orphan this
    # privileged content (chat threads, contract analyses).
    # Children keyed by analysis_id/run_id are removed before their parent runs;
    # chat_messages cascade from chat_sessions via a real FK.
    from sqlalchemy import delete as sa_delete

    from app.models.chat_sessions import ChatSessionDB
    from app.models.clause_intel import (
        ClauseDeviationFinding,
        ClauseTagFinding,
        ContractAnalysisRun,
    )
    from app.models.contract_analysis import (
        ContractDeadline,
        ContractDefinedTerm,
        ContractObligation,
        ContractParty,
    )

    run_ids = (
        (
            await db.execute(
                select(ContractAnalysisRun.id).where(ContractAnalysisRun.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    if run_ids:
        for child in (
            ContractParty,
            ContractObligation,
            ContractDeadline,
            ContractDefinedTerm,
            ClauseDeviationFinding,
            ClauseTagFinding,
        ):
            await db.execute(sa_delete(child).where(child.analysis_id.in_(run_ids)))
        await db.execute(sa_delete(ContractAnalysisRun).where(ContractAnalysisRun.id.in_(run_ids)))

    await db.execute(sa_delete(ChatSessionDB).where(ChatSessionDB.user_id == user_id))

    # Matters & memberships have no users FK (they tolerate demo/SSO accounts),
    # so purge them explicitly. Remove the user's memberships everywhere, then
    # the matters they own (which cascades those matters' remaining members).
    from app.models.matters import Matter, MatterMember

    await db.execute(sa_delete(MatterMember).where(MatterMember.user_id == user_id))
    await db.execute(sa_delete(Matter).where(Matter.owner_id == user_id))

    # Delete the DB row; ON DELETE CASCADE removes FK-linked relational rows.
    await db.delete(target)
    await db.commit()

    await audit_service.log_event(
        event_type=AuditEventType.USER_DELETE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=user_id,
        ip_address=get_client_ip(request),
        details={"deleted_email": target_email, **purge_result},
    )
    await audit_service.log_event(
        event_type=AuditEventType.DATA_DELETION,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="user",
        resource_id=user_id,
        ip_address=get_client_ip(request),
        details=purge_result,
    )
    return {"status": "deleted", "user_id": user_id, **purge_result}
