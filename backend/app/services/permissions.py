"""Role permissions: a fixed catalog of capabilities, per-role defaults, and
admin-editable overrides.

The four roles (admin/attorney/paralegal/viewer) stay fixed — what an admin
customizes is WHICH capabilities each role grants, via a checkbox matrix in
Settings > Users. Overrides are stored instance-wide (encrypted instance
settings, key ``role_permissions``) and resolved per-request, so changes apply
immediately to every signed-in user without reissuing tokens.

Guardrail: the admin role can never lose ``admin.users`` — otherwise a stray
uncheck would lock every admin out of the very screen that fixes it.

Failure policy (fail closed): the stored matrix is the only source of truth,
and it is never silently replaced by the shipped defaults. Every successful
read is mirrored to an encrypted last-known-good snapshot on the data volume.
When the store cannot be read (database down), the row cannot be decrypted
(key rotated) or it does not parse, the snapshot is served instead; with no
snapshot, every role is reduced to its locked permissions until the store is
readable again. An admin therefore keeps ``admin.users`` and can repair the
matrix, but a restriction an admin configured never quietly comes back off.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from fastapi import Depends, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

_SECRET_NAME = "role_permissions"

# The one permission that must always stay on the admin role.
LOCKED = {"admin": {"admin.users"}}

ROLES = ["admin", "attorney", "paralegal", "viewer"]

# The full catalog. Keys are stable identifiers (stored in overrides and
# referenced by route dependencies); labels/descriptions drive the admin UI.
PERMISSIONS: list[dict[str, str]] = [
    # ── Workspace ────────────────────────────────────────────────────
    {
        "key": "chat.use",
        "label": "Matter Strategy chat",
        "description": "Ask questions, run research, and generate strategy briefs",
        "group": "Workspace",
    },
    {
        "key": "documents.view",
        "label": "View documents",
        "description": "Browse and read the Knowledge Base",
        "group": "Workspace",
    },
    {
        "key": "documents.upload",
        "label": "Upload documents",
        "description": "Upload files, import from cloud pickers, and organize folders",
        "group": "Workspace",
    },
    {
        "key": "documents.delete",
        "label": "Delete documents",
        "description": "Remove documents and folders from the Knowledge Base",
        "group": "Workspace",
    },
    {
        "key": "matters.manage",
        "label": "Create & share matters",
        "description": "Create shared matters and add or remove their members",
        "group": "Workspace",
    },
    {
        "key": "connectors.manage",
        "label": "Manage connectors",
        "description": "Connect, sync, and disconnect cloud document sources",
        "group": "Workspace",
    },
    # ── Analysis ─────────────────────────────────────────────────────
    {
        "key": "contracts.use",
        "label": "Contract analysis",
        "description": "Analyze, review, redline, and draft contracts",
        "group": "Analysis",
    },
    {
        "key": "tools.research",
        "label": "Legal research tools",
        "description": "Case lookup, Shepardize, precedents, dockets, oral arguments, trends",
        "group": "Analysis",
    },
    {
        "key": "judge_intel.use",
        "label": "Judge Intelligence",
        "description": "Build judge profiles, analytics, and strategy briefs",
        "group": "Analysis",
    },
    # ── Administration ───────────────────────────────────────────────
    {
        "key": "admin.users",
        "label": "Manage users & roles",
        "description": "Invite users, change roles, and edit these permissions",
        "group": "Administration",
    },
    {
        "key": "admin.audit",
        "label": "View audit logs",
        "description": "Read and verify the tamper-evident audit trail",
        "group": "Administration",
    },
    {
        "key": "admin.settings",
        "label": "Instance settings",
        "description": "Integrations, connector credentials, and branding",
        "group": "Administration",
    },
]

_ALL_KEYS = {p["key"] for p in PERMISSIONS}

# Permissions that gate privileged or destructive actions. An endpoint behind
# one of these is refused (503) while the audit log cannot be written, before
# anything happens, since its audit record is part of the action
# (app/services/audit.py: PRIVILEGED_EVENT_TYPES, assert_writable).
PRIVILEGED_PERMISSIONS: frozenset[str] = frozenset(
    {"admin.users", "admin.settings", "documents.delete", "matters.manage", "connectors.manage"}
)
_NON_ADMIN_KEYS = {k for k in _ALL_KEYS if not k.startswith("admin.")}

DEFAULT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": set(_ALL_KEYS),
    "attorney": set(_NON_ADMIN_KEYS),
    # Paralegals work inside matters but do not decide who is on them: creating
    # and sharing a matter grants colleagues access to privileged documents.
    "paralegal": _NON_ADMIN_KEYS - {"matters.manage"},
    # Viewer is the read-and-research role by default; an admin can grant more.
    "viewer": {"chat.use", "documents.view", "tools.research", "judge_intel.use"},
}

# In-process cache of stored overrides; invalidated on every save/reset.
# Process-local: correct for the supported single-process deployment (main.py
# refuses UVICORN_WORKERS != 1); a second process would keep serving its stale
# copy until restarted.
_overrides: dict[str, set[str]] | None = None
_loaded = False
# When the store could not be read: the degraded matrix (snapshot, or locked
# permissions only) is served for this long without re-querying, then the
# load is retried. A failed load is never cached as if it were the stored
# matrix.
_FAILED_LOAD_RETRY_SECONDS = 5.0
_failed_load_at: float | None = None
# A loaded matrix is re-read when another process saved one: the shared
# version counter is checked at most this often (one primary-key read).
_VERSION_CHECK_SECONDS = 1.0
_version_seen = 0
_version_checked_at = 0.0
# What the matrix currently being served came from, for /health and the admin
# UI: "stored" (the instance settings row, or its absence = defaults),
# "snapshot" (the encrypted last-known-good copy, because the store is
# unreadable) or "locked_only" (store unreadable and no snapshot: every role
# holds only its locked permissions). "unknown" until the first load.
SOURCE_STORED = "stored"
SOURCE_SNAPSHOT = "snapshot"
SOURCE_LOCKED_ONLY = "locked_only"
_policy_source = "unknown"
_policy_error: str | None = None
_degraded: dict[str, set[str]] | None = None


class PolicyUnavailableError(RuntimeError):
    """The stored matrix could not be read, decrypted or parsed."""


def invalidate_cache() -> None:
    global _overrides, _loaded, _failed_load_at, _degraded
    _overrides = None
    _loaded = False
    _failed_load_at = None
    _degraded = None


# ── Last-known-good snapshot ────────────────────────────────────────────
# Encrypted with the instance key (AES-GCM, so it is also tamper-evident) and
# kept beside the other secret stores on the data volume. It is written on
# every successful read of the store and on every save, so it always mirrors
# the matrix an admin last saw applied.


def _snapshot_path() -> Path:
    return Path(settings.upload_dir).parent / ".role_permissions.snapshot"


def _write_snapshot(overrides: dict[str, set[str]]) -> None:
    from app.services.encryption import encryption_service

    path = _snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"overrides": {r: sorted(k) for r, k in overrides.items()}})
        tmp = path.with_suffix(".snapshot.tmp")
        tmp.write_text(encryption_service.encrypt_string(payload))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        # A snapshot that cannot be written only weakens the fallback; the
        # stored matrix is still being served.
        logger.warning("Could not write role permission snapshot %s: %s", path, e)


def _read_snapshot() -> dict[str, set[str]] | None:
    """The last-known-good overrides, or None when absent or unreadable."""
    from app.services.encryption import encryption_service

    path = _snapshot_path()
    try:
        if not path.exists():
            return None
        raw = encryption_service.decrypt_string(path.read_text())
        parsed = json.loads(raw)
        return _parse_overrides(parsed.get("overrides"))
    except Exception as e:  # any corruption or key mismatch: no snapshot
        logger.error("Role permission snapshot %s is unreadable: %s", path, e)
        return None


def _parse_overrides(parsed: object) -> dict[str, set[str]]:
    """Validate a decoded override document. Raises ValueError if malformed."""
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("role permission overrides must be a JSON object")
    overrides: dict[str, set[str]] = {}
    for role, keys in parsed.items():
        if role in ROLES and isinstance(keys, list):
            overrides[role] = {k for k in keys if k in _ALL_KEYS}
    return overrides


def _locked_only() -> dict[str, set[str]]:
    """An override for every role that grants nothing beyond LOCKED."""
    return {role: set() for role in ROLES}


async def _read_store() -> dict[str, set[str]]:
    """The overrides exactly as stored. Raises PolicyUnavailableError."""
    from app.services.instance_settings import get_secret_strict

    try:
        raw = await get_secret_strict(_SECRET_NAME)
        return _parse_overrides(json.loads(raw) if raw else None)
    except Exception as e:
        raise PolicyUnavailableError(f"{type(e).__name__}: {e}") from e


async def _stale() -> bool:
    """True when another process saved a matrix since this one loaded it."""
    global _version_checked_at
    now = time.monotonic()
    if now - _version_checked_at < _VERSION_CHECK_SECONDS:
        return False
    _version_checked_at = now
    from app.services import shared_versions

    return await shared_versions.get_version_async(shared_versions.PERMISSIONS) != _version_seen


async def _load_overrides() -> dict[str, set[str]]:
    global _overrides, _loaded, _failed_load_at, _policy_source, _policy_error, _degraded
    global _version_seen
    if _loaded:
        if not await _stale():
            return _overrides or {}
        invalidate_cache()
    now = time.monotonic()
    if _failed_load_at is not None and now - _failed_load_at < _FAILED_LOAD_RETRY_SECONDS:
        return _degraded if _degraded is not None else _locked_only()

    try:
        overrides = await _read_store()
    except PolicyUnavailableError as e:
        # Fail closed. Never the shipped defaults: an admin may have removed
        # permissions from them, and this is exactly the moment that
        # restriction must hold. The next call after the back-off retries.
        _failed_load_at = now
        _policy_error = str(e)
        snapshot = _read_snapshot()
        if snapshot is not None:
            _degraded = snapshot
            _policy_source = SOURCE_SNAPSHOT
            logger.error(
                "Role permission overrides are unreadable; serving the last-known-good "
                "snapshot until the next retry: %s",
                e,
            )
        else:
            _degraded = _locked_only()
            _policy_source = SOURCE_LOCKED_ONLY
            logger.critical(
                "Role permission overrides are unreadable and no snapshot exists; every "
                "role is reduced to its locked permissions until the store is readable: %s",
                e,
            )
        return _degraded

    _overrides = overrides
    _loaded = True
    _failed_load_at = None
    _degraded = None
    _policy_source = SOURCE_STORED
    _policy_error = None
    _write_snapshot(overrides)
    from app.services import shared_versions

    _version_seen = await shared_versions.get_version_async(shared_versions.PERMISSIONS)
    _version_checked_at = time.monotonic()
    return overrides


async def get_role_permissions() -> dict[str, set[str]]:
    """Effective permission set per role: stored override, else the default."""
    overrides = await _load_overrides()
    result: dict[str, set[str]] = {}
    for role in ROLES:
        perms = set(overrides.get(role, DEFAULT_ROLE_PERMISSIONS[role]))
        perms |= LOCKED.get(role, set())
        result[role] = perms
    return result


async def policy_status() -> dict[str, object]:
    """Where the matrix being served comes from, for /health and the admin UI.

    ``healthy`` is True only when the stored matrix itself is being served.
    """
    await _load_overrides()
    return {
        "healthy": _policy_source == SOURCE_STORED,
        "source": _policy_source,
        "error": _policy_error,
        "snapshot_present": _snapshot_path().exists(),
    }


async def set_role_permissions(role: str, keys: list[str]) -> None:
    """Store one role's permission set (validated; locked keys re-added)."""
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    unknown = [k for k in keys if k not in _ALL_KEYS]
    if unknown:
        raise ValueError(f"Unknown permissions: {unknown}")
    perms = set(keys) | LOCKED.get(role, set())

    from app.services.instance_settings import set_secret

    # Start from the matrix as stored; if that row is unreadable, from the
    # snapshot — never from nothing, or saving one role would silently drop
    # every other role's customization.
    try:
        current = await _read_store()
    except PolicyUnavailableError:
        current = _read_snapshot() or {}
    current[role] = perms
    stored = {r: sorted(k) for r, k in current.items()}
    await set_secret(_SECRET_NAME, json.dumps(stored))
    invalidate_cache()
    _write_snapshot(current)
    from app.services import shared_versions

    await shared_versions.bump_async(shared_versions.PERMISSIONS)


async def reset_role_permissions() -> None:
    """Drop every override — all roles return to the shipped defaults."""
    from app.services.instance_settings import delete_secret

    await delete_secret(_SECRET_NAME)
    invalidate_cache()
    _write_snapshot({})
    from app.services import shared_versions

    await shared_versions.bump_async(shared_versions.PERMISSIONS)


async def effective_permissions(user_roles: list[str]) -> set[str]:
    """Union of permissions across all of a user's roles (case-insensitive)."""
    matrix = await get_role_permissions()
    lower = {r.lower() for r in (user_roles or [])}
    perms: set[str] = set()
    for role in ROLES:
        if role in lower:
            perms |= matrix[role]
    return perms


def _permission_label(key: str) -> str:
    for p in PERMISSIONS:
        if p["key"] == key:
            return p["label"]
    return key


def require_permission(key: str):
    """FastAPI dependency: authenticated user with the given permission.

    Same shape as ``require_roles`` — use as a default value:
        current_user: TokenData = require_permission("contracts.use")
    """
    if key not in _ALL_KEYS:  # fail at import time, not per-request
        raise ValueError(f"Unknown permission: {key}")

    from app.services.auth import get_current_user

    async def checker(current_user=Depends(get_current_user)):
        perms = await effective_permissions(current_user.roles or [])
        if key not in perms:
            raise HTTPException(
                status_code=403,
                detail=f"Your role does not include permission: {_permission_label(key)}. "
                "Ask an administrator to grant it in Settings > Users.",
            )
        if key in PRIVILEGED_PERMISSIONS:
            from app.services.audit import audit_service

            await audit_service.assert_writable()
        if key.startswith("admin."):
            # Admin oversight, export and deletion read every user's rows:
            # lift the database row-level security scope for this request.
            from app import rls

            rls.grant_bypass(f"{key} endpoint for user {current_user.user_id}")
        return current_user

    return Depends(checker)


def require_any_permission(*keys: str):
    """FastAPI dependency: authenticated user holding AT LEAST ONE of ``keys``.

    For shared utility endpoints that several features call (e.g. file text
    extraction used by the viewer, Contracts and Case Citations).
    """
    unknown = [k for k in keys if k not in _ALL_KEYS]
    if not keys or unknown:  # fail at import time, not per-request
        raise ValueError(f"Unknown permission(s): {unknown or keys}")

    from app.services.auth import get_current_user

    async def checker(current_user=Depends(get_current_user)):
        perms = await effective_permissions(current_user.roles or [])
        if not perms.intersection(keys):
            labels = ", ".join(_permission_label(k) for k in keys)
            raise HTTPException(
                status_code=403,
                detail=f"Your role does not include any of these permissions: {labels}. "
                "Ask an administrator to grant one in Settings > Users.",
            )
        if all(k in PRIVILEGED_PERMISSIONS for k in keys):
            from app.services.audit import audit_service

            await audit_service.assert_writable()
        if all(k.startswith("admin.") for k in keys):
            from app import rls

            rls.grant_bypass(f"{'/'.join(keys)} endpoint for user {current_user.user_id}")
        return current_user

    return Depends(checker)
