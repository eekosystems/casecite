"""
Authentication Service

Supports:
- JWT access/refresh tokens with JTI revocation
- Azure AD token verification (SSO identity only; roles come from the database)
- Role-based access control (RBAC)
- Password reset tokens
"""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWTError as JWTError
from pydantic import BaseModel, Field

from app.config import settings
from app.redis_utils import RedisError, get_redis
from app.services.audit import AuditEventType, audit_service

logger = logging.getLogger(__name__)


class UserRole(str, Enum):
    ADMIN = "admin"  # Full access
    ATTORNEY = "attorney"  # Full document access
    PARALEGAL = "paralegal"  # Limited access
    VIEWER = "viewer"  # Read-only


class User(BaseModel):
    id: str
    email: str
    name: str
    roles: list[UserRole]
    tenant_id: str | None = None
    azure_oid: str | None = None  # Azure AD Object ID
    mfa_enabled: bool = False
    last_login: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Mirrors users.token_version; embedded in tokens as the ``tv`` claim.
    token_version: int = 0


class TokenData(BaseModel):
    user_id: str
    email: str
    roles: list[str]
    exp: datetime
    iat: datetime
    jti: str  # JWT ID for revocation
    token_type: str = "access"  # "access" or "refresh"
    session_id: str | None = None  # ``sid`` claim: the sign-in this token belongs to
    token_version: int = 0  # ``tv`` claim: must equal users.token_version
    # ``cl`` claim on refresh tokens: the kind of client the sign-in was issued
    # to. "device" marks a device-code grant (Word add-in), whose refresh token
    # travels in the response body and is accepted back in a request body;
    # cookie sign-ins carry no claim and are refreshed from the cookie only.
    client: str | None = None


class AuthActionRequired(HTTPException):
    """403 raised while an account must complete a step before using the API.

    ``code`` is surfaced in the JSON error body (see error_handlers) so the
    frontend can route to the right screen instead of showing a generic error:
    ``password_change_required`` or ``mfa_enrollment_required``.
    """

    def __init__(self, code: str, detail: str):
        super().__init__(status_code=403, detail=detail)
        self.code = code


# Maximum lifetime of any token; revocation records are kept this long.
_REVOCATION_TTL_SECONDS = 7 * 24 * 60 * 60
# A rotated refresh token replayed within this window is treated as a benign
# race (two tabs refreshing at once) and simply rejected; beyond it, replay
# means the token was copied, and every session of the user is revoked.
REFRESH_REUSE_GRACE_SECONDS = 10
# Compact revoked_tokens.jsonl after this many appended lines.
_REVOCATION_COMPACT_EVERY = 500


class AuthService:
    """Authentication and authorization service."""

    def __init__(self):
        # Domain-separated JWT signing key (HKDF from SECRET_KEY), so a leaked
        # token-signing key can't be reversed to the encryption/CSRF keys.
        from app.utils.keys import derive_subkey

        self.secret_key = derive_subkey("jwt-hs256")
        self.algorithm = "HS256"
        self.access_token_expire = timedelta(minutes=settings.access_token_expire_minutes)
        self.refresh_token_expire = timedelta(days=7)

        # Revocation store. The process-local maps (restored from
        # revoked_tokens.jsonl at startup, appended to on every revocation) are
        # authoritative for this process; Redis is written as well and consulted
        # in addition, never instead — so an evicted or flushed Redis key cannot
        # bring a revoked token back to life.
        # {jti: expiry epoch}
        self._revoked_tokens: dict[str, float] = {}
        # {jti: epoch the refresh token was rotated} — distinguishes "rotated,
        # then replayed" (theft signal) from an ordinary logout revocation.
        self._rotated_tokens: dict[str, float] = {}
        # User-level tombstones {user_id: expiry epoch}: a deleted user's id is
        # recorded here so any still-unexpired token is rejected even though the
        # user's DB row — and its is_active check — is gone.
        self._revoked_users: dict[str, float] = {}
        self._revocation_file = Path(settings.upload_dir).parent / "revoked_tokens.jsonl"
        self._revocation_appends = 0
        self._load_revoked_tokens_from_file()
        self._compact_revocation_file()

        if not settings.debug and not get_redis():
            logger.critical(
                "CRITICAL: Redis unavailable in production. Token revocation "
                "will rely on file-based fallback only."
            )

        # Azure AD configuration — require explicit tenant to prevent any-tenant auth
        self.azure_tenant = settings.microsoft_tenant_id
        self.azure_client_id = settings.microsoft_client_id
        if self.azure_client_id and not self.azure_tenant:
            logger.warning(
                "Azure AD client_id configured without tenant_id — SSO disabled for safety. "
                "Set MICROSOFT_TENANT_ID to enable Azure AD SSO."
            )
            self.azure_client_id = None  # Disable SSO rather than accept 'common'

        # Azure AD JWKS cache
        self._jwks_cache = None
        self._jwks_cache_time = None
        self._jwks_cache_ttl = 3600  # 1 hour

    def _load_revoked_tokens_from_file(self):
        """Load unexpired token revocations and user tombstones on startup."""
        try:
            if not self._revocation_file.exists():
                return
            now = datetime.now(UTC)
            with open(self._revocation_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        expires_at = datetime.fromisoformat(entry["expires_at"])
                        if expires_at <= now:
                            continue
                        if "user_id" in entry:
                            self._revoked_users[entry["user_id"]] = expires_at.timestamp()
                            continue
                        self._revoked_tokens[entry["jti"]] = expires_at.timestamp()
                        if entry.get("rotated_at") is not None:
                            self._rotated_tokens[entry["jti"]] = float(entry["rotated_at"])
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        continue
            logger.info(
                f"Loaded {len(self._revoked_tokens)} revoked tokens and "
                f"{len(self._revoked_users)} user tombstones from file"
            )
        except OSError as e:
            logger.warning(f"Could not load revoked tokens from file: {e}")

    def _restrict_revocation_file(self):
        """Owner-only permissions (0o600) on the revocation file."""
        import os
        import stat

        try:
            os.chmod(self._revocation_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows may not support chmod

    def _append_revocation(self, entry: dict):
        """Append one revocation record to the durable file."""
        try:
            self._revocation_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._revocation_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
            self._restrict_revocation_file()
        except OSError as e:
            logger.error(f"Could not persist revocation to file: {e}")
            return
        self._revocation_appends += 1
        if self._revocation_appends >= _REVOCATION_COMPACT_EVERY:
            self._compact_revocation_file()

    def _persist_revocation_to_file(self, jti: str, rotated_at: float | None = None):
        """Write a token revocation to the durable file."""
        expires_at = datetime.now(UTC) + timedelta(seconds=_REVOCATION_TTL_SECONDS)
        entry: dict = {"jti": jti, "expires_at": expires_at.isoformat()}
        if rotated_at is not None:
            entry["rotated_at"] = rotated_at
        self._append_revocation(entry)

    def _compact_revocation_file(self):
        """Drop expired entries from memory and rewrite the file with the live ones.

        Every refresh and logout appends a line, and an expired revocation is
        dead weight (the token it names can no longer verify), so without this
        the file — and the in-memory maps — grow without bound.
        """
        now = time.time()
        self._revoked_tokens = {j: e for j, e in self._revoked_tokens.items() if e > now}
        self._rotated_tokens = {
            j: t for j, t in self._rotated_tokens.items() if j in self._revoked_tokens
        }
        self._revoked_users = {u: e for u, e in self._revoked_users.items() if e > now}
        self._revocation_appends = 0
        if not self._revocation_file.exists():
            return
        try:
            tmp = self._revocation_file.with_name(self._revocation_file.name + ".tmp")
            with open(tmp, "w") as f:
                for jti, exp in self._revoked_tokens.items():
                    entry: dict = {
                        "jti": jti,
                        "expires_at": datetime.fromtimestamp(exp, tz=UTC).isoformat(),
                    }
                    if jti in self._rotated_tokens:
                        entry["rotated_at"] = self._rotated_tokens[jti]
                    f.write(json.dumps(entry) + "\n")
                for user_id, exp in self._revoked_users.items():
                    tombstone = {
                        "user_id": user_id,
                        "expires_at": datetime.fromtimestamp(exp, tz=UTC).isoformat(),
                    }
                    f.write(json.dumps(tombstone) + "\n")
            tmp.replace(self._revocation_file)
            self._restrict_revocation_file()
        except OSError as e:
            logger.warning(f"Could not compact revocation file: {e}")

    # ==================== JWT Token Management ====================

    def create_access_token(self, user: User, session_id: str | None = None) -> str:
        """Create a new access token."""
        import uuid

        now = datetime.now(UTC)
        payload = {
            "sub": user.id,
            "email": user.email,
            "name": user.name,
            "roles": [r.value for r in user.roles],
            "iat": now,
            "exp": now + self.access_token_expire,
            "jti": str(uuid.uuid4()),
            "type": "access",
            "aud": "casecite",
            "tv": user.token_version,
        }
        if session_id:
            payload["sid"] = session_id
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def create_refresh_token(
        self, user: User, session_id: str | None = None, client: str | None = None
    ) -> str:
        """Create a new refresh token bound to the sign-in session ``session_id``.

        ``client="device"`` marks a device-code grant whose refresh token is
        carried in request bodies instead of the httpOnly cookie.
        """
        import uuid

        now = datetime.now(UTC)
        payload = {
            "sub": user.id,
            "iat": now,
            "exp": now + self.refresh_token_expire,
            "jti": str(uuid.uuid4()),
            "type": "refresh",
            "aud": "casecite",
            "tv": user.token_version,
        }
        if session_id:
            payload["sid"] = session_id
        if client:
            payload["cl"] = client
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def verify_token(self, token: str, check_revocation: bool = True) -> TokenData:
        """Verify and decode a token.

        ``check_revocation=False`` is for /auth/refresh only, which must be able
        to tell a rotated-and-replayed refresh token (see claim_refresh_token)
        from an invalid one; every other caller rejects revoked tokens here.
        """
        try:
            payload = jwt.decode(
                token, self.secret_key, algorithms=[self.algorithm], audience="casecite"
            )

            # Check if token is revoked
            jti = payload.get("jti")
            if not jti or (check_revocation and self._is_token_revoked(jti)):
                raise HTTPException(status_code=401, detail="Token has been revoked")

            return TokenData(
                user_id=payload["sub"],
                email=payload.get("email", ""),
                roles=payload.get("roles", []),
                exp=datetime.fromtimestamp(payload["exp"], tz=UTC),
                iat=datetime.fromtimestamp(payload["iat"], tz=UTC),
                jti=jti,
                token_type=payload.get("type"),
                session_id=payload.get("sid"),
                token_version=int(payload.get("tv") or 0),
                client=payload.get("cl"),
            )
        except JWTError as e:
            logger.debug(f"Token validation details: {e}")
            logger.warning("Token validation failed")
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    def _is_token_revoked(self, jti: str) -> bool:
        """Check if a token is revoked (process-local store, then Redis)."""
        if not jti:
            return True  # Reject tokens with null/empty JTI

        expiry = self._revoked_tokens.get(jti)
        if expiry is not None and expiry > time.time():
            return True

        redis_client = get_redis()
        if redis_client:
            try:
                return bool(redis_client.exists(f"revoked_token:{jti}"))
            except (RedisError, ConnectionError, OSError):
                # The local store was already consulted above.
                return False
        return False

    def revoke_token(self, jti: str):
        """Revoke a token by its ID."""
        # Immediate in-memory revocation (closes race window where _is_token_revoked
        # checks Redis before the Redis write completes)
        self._revoked_tokens[jti] = time.time() + _REVOCATION_TTL_SECONDS

        # Persist to file so the revocation survives a restart and a Redis flush
        self._persist_revocation_to_file(jti)

        # Persist to Redis for distributed deployments
        redis_client = get_redis()
        if redis_client:
            try:
                redis_client.setex(f"revoked_token:{jti}", _REVOCATION_TTL_SECONDS, "1")
            except (RedisError, ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.warning(f"Redis revoke failed: {e}, in-memory revocation still active")

    def claim_refresh_token(self, jti: str) -> str:
        """Spend a refresh token for rotation: revoke it if (and only if) it is unspent.

        Returns ``"ok"`` when this call spent the token, ``"revoked"`` when it
        was already revoked by logout or was rotated moments ago (a benign
        double-refresh race), and ``"reused"`` when a token rotated more than
        REFRESH_REUSE_GRACE_SECONDS ago is presented again — the signal that a
        copied refresh token is in use.

        The local check-and-set has no await between check and write, and the
        Redis write is a single SET NX, so two concurrent refreshes with the
        same token cannot both succeed.
        """
        now = time.time()

        def _classify(rotated_at: float | None) -> str:
            if rotated_at is not None and now - rotated_at > REFRESH_REUSE_GRACE_SECONDS:
                return "reused"
            return "revoked"

        expiry = self._revoked_tokens.get(jti)
        if expiry is not None and expiry > now:
            return _classify(self._rotated_tokens.get(jti))

        redis_client = get_redis()
        if redis_client:
            key = f"revoked_token:{jti}"
            try:
                if not redis_client.set(key, f"rot:{now}", nx=True, ex=_REVOCATION_TTL_SECONDS):
                    value = redis_client.get(key) or ""
                    rotated_at = None
                    if isinstance(value, str) and value.startswith("rot:"):
                        try:
                            rotated_at = float(value[4:])
                        except ValueError:
                            rotated_at = None
                    return _classify(rotated_at)
            except (RedisError, ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.warning(f"Redis refresh-claim failed: {e}, using local revocation store")

        self._revoked_tokens[jti] = now + _REVOCATION_TTL_SECONDS
        self._rotated_tokens[jti] = now
        self._persist_revocation_to_file(jti, rotated_at=now)
        return "ok"

    def revoke_user(self, user_id: str):
        """Tombstone a deleted user so outstanding access tokens are rejected.

        A deleted user has no DB row, so get_current_user's is_active check no
        longer fires; this tombstone (checked on every request; kept in the
        durable revocation file and in Redis) rejects any of their
        still-unexpired tokens. TTL matches the maximum token lifetime.
        """
        if not user_id:
            return
        expires = time.time() + _REVOCATION_TTL_SECONDS
        self._revoked_users[user_id] = expires
        self._append_revocation(
            {
                "user_id": user_id,
                "expires_at": datetime.fromtimestamp(expires, tz=UTC).isoformat(),
            }
        )
        redis_client = get_redis()
        if redis_client:
            try:
                redis_client.setex(f"revoked_user:{user_id}", _REVOCATION_TTL_SECONDS, "1")
            except (RedisError, ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.warning(f"Redis user-revoke failed: {e}, in-memory tombstone still active")

    def _is_user_revoked(self, user_id: str) -> bool:
        """Whether a user has been tombstoned (deleted)."""
        if not user_id:
            return False
        expiry = self._revoked_users.get(user_id)
        if expiry is not None and expiry > time.time():
            return True
        redis_client = get_redis()
        if redis_client:
            try:
                return bool(redis_client.exists(f"revoked_user:{user_id}"))
            except (RedisError, ConnectionError, OSError):
                return False
        return False

    def revoke_user_sessions(self, user_id: str) -> int:
        """Revoke every live session of a user in this process; returns the count.

        Revokes each session's current access-token JTI and drops the session
        records. Because /auth/refresh requires a live session, this also stops
        the user's refresh tokens from minting new access tokens. Callers that
        need the invalidation to hold even if the session store is lost also
        bump ``users.token_version``.
        """
        from app.middleware.security import session_manager

        sessions = session_manager.get_active_sessions_raw(user_id)
        for session in sessions:
            self.revoke_token(session["jti"])
        session_manager.terminate_all_sessions(user_id)
        return len(sessions)

    # ==================== Azure AD Integration ====================

    async def _get_azure_jwks(self) -> dict:
        """Get Azure AD JWKS with 1-hour cache."""
        import time

        now = time.time()
        if (
            self._jwks_cache
            and self._jwks_cache_time
            and (now - self._jwks_cache_time) < self._jwks_cache_ttl
        ):
            return self._jwks_cache

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0)) as client:
            config_url = f"https://login.microsoftonline.com/{self.azure_tenant}/v2.0/.well-known/openid-configuration"
            config_resp = await client.get(config_url)
            config = config_resp.json()
            jwks_resp = await client.get(config["jwks_uri"])
            jwks = jwks_resp.json()

        self._jwks_cache = jwks
        self._jwks_cache_time = now
        return jwks

    async def verify_azure_token(self, token: str) -> dict:
        """Verify an Azure AD token."""
        jwks = await self._get_azure_jwks()

        # Decode and verify the token
        try:
            # Get the key ID from token header
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")

            # Find the matching public key
            public_key = None
            for key in jwks["keys"]:
                if key["kid"] == kid:
                    # PyJWT accepts both dict and JSON string for from_jwk
                    import json as json_module

                    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json_module.dumps(key))
                    break

            if not public_key:
                raise HTTPException(status_code=401, detail="Unable to find matching key")

            # Verify the token
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self.azure_client_id,
                issuer=f"https://login.microsoftonline.com/{self.azure_tenant}/v2.0",
            )

            return payload

        except JWTError as e:
            logger.debug(f"Azure token validation details: {e}")
            logger.warning("Azure token validation failed")
            raise HTTPException(status_code=401, detail="Invalid or expired Azure token")

    async def verify_azure_identity(self, azure_token: str) -> dict:
        """Verify an Azure AD token and return the identity claims we act on.

        Returns ``{"id", "email", "name", "azure_oid", "tenant_id"}``. This
        deliberately carries NO roles: authorization is decided by the local
        account row (``users.roles``), never by the identity provider's
        token, so an admin's role changes take effect on the next login and
        a first-time SSO user gets the configured default rather than a
        privileged role.
        """
        payload = await self.verify_azure_token(azure_token)
        return {
            "id": payload.get("oid") or payload["sub"],
            "email": (payload.get("email") or payload.get("preferred_username") or "")
            .lower()
            .strip(),
            "name": payload.get("name") or "",
            "azure_oid": payload.get("oid"),
            "tenant_id": payload.get("tid"),
        }

    async def issue_azure_login(
        self, user: User, request: Request, session_id: str | None = None
    ) -> dict:
        """Mint local tokens for an already-resolved SSO user and audit the login.

        ``user.roles`` must come from the database row, not from the token.
        """
        from app.utils.ip_resolution import get_client_ip

        access_token = self.create_access_token(user, session_id=session_id)
        refresh_token = self.create_refresh_token(user, session_id=session_id)

        await audit_service.log_event(
            event_type=AuditEventType.LOGIN_SUCCESS,
            user_id=user.id,
            user_email=user.email,
            details={
                "method": "azure_ad",
                "tenant": user.tenant_id,
                "roles": [r.value for r in user.roles],
            },
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",  # nosec B105 - OAuth2 token type, not a password
            "expires_in": self.access_token_expire.total_seconds(),
            "user": user.model_dump(),
        }

    # ==================== Authorization ====================

    def check_permission(self, user_roles: list[str], required_roles: list[UserRole]) -> bool:
        """Check if user has any of the required roles."""
        user_role_set = set(user_roles)
        required_role_set = {r.value for r in required_roles}
        return bool(user_role_set & required_role_set)

    def require_roles(self, *roles: UserRole):
        """Dependency to require specific roles."""

        async def check_roles(token_data: TokenData = Depends(get_current_user)):
            if not self.check_permission(token_data.roles, list(roles)):
                raise HTTPException(status_code=403, detail="Insufficient permissions")
            return token_data

        return check_roles


# ==================== FastAPI Dependencies ====================

security = HTTPBearer(auto_error=False)
auth_service = AuthService()


def _token_predates_password_change(token_iat: datetime, password_changed_at: datetime) -> bool:
    """True if a token issued at ``token_iat`` predates the last password change.

    ``password_changed_at`` is stored naive UTC (to match the Postgres TIMESTAMP
    WITHOUT TIME ZONE column); ``token_iat`` is tz-aware UTC. Both are normalized
    to naive UTC. A 5-second allowance absorbs clock skew between token issuance
    and the DB write so a token minted at reset time isn't spuriously rejected.
    """
    iat_naive = token_iat.astimezone(UTC).replace(tzinfo=None) if token_iat.tzinfo else token_iat
    pwc = (
        password_changed_at.replace(tzinfo=None)
        if password_changed_at.tzinfo
        else password_changed_at
    )
    return iat_naive < (pwc - timedelta(seconds=5))


# Paths (relative to the API prefix) that stay reachable while an account must
# change its temporary password, and while it must enrol MFA (REQUIRE_MFA).
_PASSWORD_CHANGE_ALLOWED_PATHS = frozenset(
    {"/auth/change-password", "/auth/logout", "/auth/logout/all", "/auth/me"}
)
_MFA_ENROLLMENT_ALLOWED_PATHS = _PASSWORD_CHANGE_ALLOWED_PATHS | {
    "/auth/mfa/status",
    "/auth/mfa/setup",
    "/auth/mfa/enable",
}


def _api_relative_path(request: Request) -> str:
    path = request.url.path
    prefix = settings.api_prefix
    return path[len(prefix) :] if path.startswith(prefix) else path


def mfa_enrollment_required(mfa_enabled: bool, has_password: bool) -> bool:
    """Whether REQUIRE_MFA obliges this account to enrol before using the API.

    SSO-only accounts (no local password) are exempt: their second factor is
    the identity provider's policy.
    """
    return bool(settings.require_mfa and has_password and not mfa_enabled)


async def get_current_user(
    request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)
) -> TokenData | None:
    """Get the current authenticated user from the request."""
    if not credentials:
        # Fall back to httpOnly cookie
        cookie_token = request.cookies.get("access_token")
        if not cookie_token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        token = cookie_token
    else:
        token = credentials.credentials

    token_data = auth_service.verify_token(token)

    # Reject non-access tokens (e.g. refresh tokens used as access tokens)
    if token_data.token_type != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    # Reject tokens belonging to a deleted (tombstoned) user. This runs before
    # the DB lookup so it catches users whose row is already gone — the exact
    # gap where the missing row would otherwise skip the is_active check.
    if auth_service._is_user_revoked(token_data.user_id):
        raise HTTPException(status_code=401, detail="Account disabled or deleted")

    # From here on, every database transaction in this request (and in tasks
    # it spawns) is scoped to this user at the database too (app/rls.py).
    from app import rls

    rls.request_user_id.set(token_data.user_id)
    rls.rls_bypass.set(False)

    # Verify user is still active in the database
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.db_models import User as DBUser

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                DBUser.is_active,
                DBUser.password_changed_at,
                DBUser.token_version,
                DBUser.must_change_password,
                DBUser.mfa_enabled,
                DBUser.password_hash,
            ).where(DBUser.id == token_data.user_id)
        )
        row = result.one_or_none()
    # A token whose user row is gone (deleted by hand, database restored from
    # a backup that predates the account) must die with the row, not keep
    # working for its remaining lifetime. Only the DEBUG-only dev account,
    # which has no row by design, may pass without one.
    if row is None and not settings.debug:
        raise HTTPException(status_code=401, detail="Account disabled or deleted")
    must_change_password = False
    needs_mfa_enrollment = False
    if row is not None:
        is_active, password_changed_at, token_version, must_change, mfa_enabled, pw_hash = row
        if not is_active:
            raise HTTPException(status_code=401, detail="Account disabled or deleted")
        # Invalidate any token issued before the last password change, so a
        # password reset immediately kills all outstanding access AND refresh
        # tokens without needing to track individual JTIs.
        if password_changed_at is not None and _token_predates_password_change(
            token_data.iat, password_changed_at
        ):
            raise HTTPException(status_code=401, detail="Session expired, please sign in again")
        # "Log out everywhere" / admin force sign-out / refresh-token reuse bump
        # users.token_version; a token minted under an older version is dead.
        if token_data.token_version != int(token_version or 0):
            raise HTTPException(status_code=401, detail="Session expired, please sign in again")
        must_change_password = bool(must_change)
        needs_mfa_enrollment = mfa_enrollment_required(bool(mfa_enabled), pw_hash is not None)

    # Enforce session validation.
    # IMPORTANT: resolve the client IP the same way create_session does
    # (get_client_ip honors X-Forwarded-For). Using request.client.host here
    # compares against the reverse-proxy IP (e.g. nginx / 127.0.0.1) while the
    # session was stored under the real client IP, so session IP binding fails
    # and every request 401s behind a proxy (Coolify/Traefik/nginx).
    from app.middleware.security import session_manager
    from app.utils.ip_resolution import get_client_ip

    session_valid = session_manager.validate_session(
        token_data.user_id,
        token_data.jti,
        ip_address=get_client_ip(request),
        token_exp=token_data.exp,
    )
    if not session_valid:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    # An invited account still on its admin-issued temporary password may only
    # change that password (or sign out) until it has done so.
    relative_path = _api_relative_path(request)
    if must_change_password and relative_path not in _PASSWORD_CHANGE_ALLOWED_PATHS:
        raise AuthActionRequired(
            "password_change_required",
            "You must change your temporary password before continuing.",
        )
    if needs_mfa_enrollment and relative_path not in _MFA_ENROLLMENT_ALLOWED_PATHS:
        raise AuthActionRequired(
            "mfa_enrollment_required",
            "Multi-factor authentication is required. Set up an authenticator app to continue.",
        )

    return token_data


def require_roles(*roles: UserRole):
    """Dependency that requires specific roles."""
    return Depends(auth_service.require_roles(*roles))


# ==================== Password Reset Support ====================


class PasswordResetService:
    """Handles password reset tokens and validation."""

    def __init__(self):
        self._reset_tokens = {}  # In-memory fallback

    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash a reset token before using as a storage key.

        The raw token is only sent via email; storage only holds the hash.
        """
        import hashlib

        return hashlib.sha256(token.encode()).hexdigest()

    def generate_reset_token(self, email: str) -> str:
        """Generate a password reset token for an email."""
        import secrets
        from datetime import timedelta

        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        data = {
            "email": email.lower(),
            "expires_at": expires_at.isoformat(),
        }

        redis_client = get_redis()
        if redis_client:
            try:
                import json

                redis_client.setex(
                    f"password_reset:{token_hash}",
                    3600,  # 1 hour TTL
                    json.dumps(data),
                )
                return token
            except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.warning(f"Redis store failed: {e}")

        # In-memory fallback
        self._reset_tokens[token_hash] = data
        return token

    def verify_reset_token(self, token: str) -> str | None:
        """Verify a reset token and return the email if valid."""
        import json

        token_hash = self._hash_token(token)

        redis_client = get_redis()
        if redis_client:
            try:
                data_str = redis_client.get(f"password_reset:{token_hash}")
                if data_str:
                    data = json.loads(data_str)
                    expires_at = datetime.fromisoformat(data["expires_at"])
                    if expires_at > datetime.now(UTC):
                        return data["email"]
                    # Token expired, delete it
                    redis_client.delete(f"password_reset:{token_hash}")
                return None
            except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.warning(f"Redis verify failed: {e}")

        # In-memory fallback
        data = self._reset_tokens.get(token_hash)
        if data:
            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at > datetime.now(UTC):
                return data["email"]
            # Token expired, delete it
            del self._reset_tokens[token_hash]
        return None

    def invalidate_token(self, token: str):
        """Invalidate a used reset token."""
        token_hash = self._hash_token(token)

        redis_client = get_redis()
        if redis_client:
            try:
                redis_client.delete(f"password_reset:{token_hash}")
            except (RedisError, ConnectionError, OSError):
                pass

        if token_hash in self._reset_tokens:
            del self._reset_tokens[token_hash]


password_reset_service = PasswordResetService()
