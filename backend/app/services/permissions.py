"""Role permissions: a fixed catalog of capabilities, per-role defaults, and
admin-editable overrides.

The four roles (admin/attorney/paralegal/viewer) stay fixed — what an admin
customizes is WHICH capabilities each role grants, via a checkbox matrix in
Settings > Users. Overrides are stored instance-wide (encrypted instance
settings, key ``role_permissions``) and resolved per-request, so changes apply
immediately to every signed-in user without reissuing tokens.

Guardrail: the admin role can never lose ``admin.users`` — otherwise a stray
uncheck would lock every admin out of the very screen that fixes it.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import Depends, HTTPException

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
# When the store could not be read: the shipped defaults are served for this
# long without re-querying, then the load is retried. A failed load is never
# cached as if it were the stored matrix.
_FAILED_LOAD_RETRY_SECONDS = 5.0
_failed_load_at: float | None = None


def invalidate_cache() -> None:
    global _overrides, _loaded, _failed_load_at
    _overrides = None
    _loaded = False
    _failed_load_at = None


async def _load_overrides() -> dict[str, set[str]]:
    global _overrides, _loaded, _failed_load_at
    if _loaded:
        return _overrides or {}
    now = time.monotonic()
    if _failed_load_at is not None and now - _failed_load_at < _FAILED_LOAD_RETRY_SECONDS:
        return {}
    from app.services.instance_settings import get_secret

    try:
        raw = await get_secret(_SECRET_NAME)
    except Exception as e:
        # The store is unreachable (DB down, key unavailable). Serve the
        # defaults for now — no more than any role is granted out of the box —
        # but do not cache them: the next call after the back-off retries, and
        # a successful load replaces them for the rest of the process.
        _failed_load_at = now
        logger.error(
            "Failed to load role permission overrides; serving defaults until the next retry: %s",
            e,
        )
        return {}

    overrides: dict[str, set[str]] = {}
    try:
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for role, keys in parsed.items():
                    if role in ROLES and isinstance(keys, list):
                        overrides[role] = {k for k in keys if k in _ALL_KEYS}
    except (ValueError, TypeError) as e:  # a bad row must not break auth
        logger.warning("Failed to parse role permission overrides: %s", e)
    _overrides = overrides
    _loaded = True
    _failed_load_at = None
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


async def set_role_permissions(role: str, keys: list[str]) -> None:
    """Store one role's permission set (validated; locked keys re-added)."""
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    unknown = [k for k in keys if k not in _ALL_KEYS]
    if unknown:
        raise ValueError(f"Unknown permissions: {unknown}")
    perms = set(keys) | LOCKED.get(role, set())

    from app.services.instance_settings import get_secret, set_secret

    stored: dict[str, list[str]] = {}
    raw = await get_secret(_SECRET_NAME)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                stored = {r: k for r, k in parsed.items() if r in ROLES and isinstance(k, list)}
        except (ValueError, TypeError):
            stored = {}
    stored[role] = sorted(perms)
    await set_secret(_SECRET_NAME, json.dumps(stored))
    invalidate_cache()


async def reset_role_permissions() -> None:
    """Drop every override — all roles return to the shipped defaults."""
    from app.services.instance_settings import delete_secret

    await delete_secret(_SECRET_NAME)
    invalidate_cache()


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
        return current_user

    return Depends(checker)
