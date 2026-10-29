"""
Security Middleware

Provides:
- Per-IP rate limiting and per-account login lockout
- Security headers (CSP, HSTS, frame/nosniff, permissions policy)
- Request logging
- Session tracking keyed by token JTI
"""

import json
import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.redis_utils import RedisError, _is_production, get_redis, require_redis
from app.services.audit import AuditEventType, audit_service

logger = logging.getLogger(__name__)

# A path segment that is an identifier rather than a route name: all digits, a
# UUID, or a long hex/opaque token. Collapsed to "{id}" for rate-limit buckets
# so /documents/<id-1> and /documents/<id-2> draw on ONE quota.
_ID_SEGMENT = re.compile(r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|(?=[^/]*\d)[A-Za-z0-9_-]{16,})$")


def rate_limit_bucket(path: str) -> str:
    """Normalize a request path to its rate-limit bucket (identifiers collapsed)."""
    return "/".join("{id}" if _ID_SEGMENT.match(seg) else seg for seg in path.split("/"))


class RateLimiter:
    """
    Token bucket rate limiter.

    Uses Redis when available for distributed deployments.
    Falls back to in-memory for single-instance deployments.
    """

    def __init__(self):
        # In-memory fallback: {ip: {endpoint: [timestamps]}}
        self._requests: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # Cleanup stale entries every 5 minutes

        # Rate limits: (requests, window_seconds)
        # Stricter limits for security-sensitive endpoints
        self.limits: dict[str, tuple[int, int]] = {
            "default": (100, 60),  # 100 req/min
            "/api/v1/chat": (20, 60),  # 20 req/min (expensive)
            "/api/v1/documents": (50, 60),  # 50 req/min
            "/api/v1/auth/login": (
                3,
                300,
            ),  # 3 per 5 min (stricter brute force prevention)
            "/api/v1/auth/demo/login": (3, 300),  # 3 per 5 min
            "/api/v1/auth/register": (3, 300),  # 3 per 5 min (PBKDF2 CPU + enumeration)
            "/api/v1/auth/change-password": (5, 300),  # 5 per 5 min
            "/api/v1/auth/refresh": (10, 60),  # 10 per min (token minting)
            "/api/v1/auth/forgot-password": (3, 300),  # 3 per 5 min
            "/api/v1/auth/reset-password": (5, 300),  # 5 per 5 min
            "/api/v1/auth/verify-reset-token": (10, 300),  # 10 per 5 min
            # Second-factor endpoints — brute-force protection (the per-account
            # lockout in routers/mfa.py is the primary brake; this is per-IP).
            "/api/v1/auth/mfa/verify": (5, 300),  # 5 per 5 min
            "/api/v1/auth/mfa/enable": (5, 300),  # 5 per 5 min
            "/api/v1/auth/mfa/disable": (5, 300),  # 5 per 5 min
            "/api/v1/auth/mfa/recovery-codes": (5, 300),  # 5 per 5 min (code-proof gated)
            # Device-code sign-in (Word add-in). Starting a code is as cheap to
            # abuse as a login attempt; polling runs every 5 s by design.
            "/api/v1/auth/device/start": (5, 300),  # 5 per 5 min
            "/api/v1/auth/device/token": (30, 60),  # 30 per min (one poll per 5 s + slack)
            "/api/v1/auth/device/approve": (10, 300),  # 10 per 5 min (user-code guessing)
            "/api/v1/auth/device/deny": (10, 300),  # 10 per 5 min
            # Expensive AI / extraction operations — stricter limits to prevent cost abuse
            "/api/v1/legal-docs": (20, 60),  # 20 per min (text extraction, PDF render)
            "/api/v1/strategy/brief": (5, 60),  # 5 per min
            # Job polling: the client polls every 2 s (30/min per tab) and the
            # limit is per IP, so an office NAT or a second tab must not push
            # a running job into "failed". Cheap in-memory reads — a dedicated
            # bucket well above one tab's rate, still bounded.
            "/api/v1/jobs": (300, 60),  # 300 per min (polling)
            # Key management — prevent brute-force key enumeration
            "/api/v1/user/keys/save": (5, 60),  # 5 per min
        }

        # Track failures for progressive delays
        self._failure_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        # Account-level lockout tracking: {email: [timestamps]}
        self._account_failures: dict[str, list[float]] = defaultdict(list)
        self._account_lockout_threshold: int = 5  # failures before lockout
        self._account_lockout_window: int = 900  # 15 minutes
        self._account_lockout_duration: int = 900  # 15 minutes

    def _get_limit(self, path: str) -> tuple:
        """Get rate limit for a path (exact match or path prefix with /).

        The most specific (longest) matching rule wins, independent of the
        order the rules are declared in.
        """
        best: str | None = None
        for pattern in self.limits:
            if pattern == "default":
                continue
            # Exact match or proper path prefix (prevents /auth/login matching /auth/login-history)
            if (path == pattern or path.startswith(pattern + "/")) and (
                best is None or len(pattern) > len(best)
            ):
                best = pattern
        return self.limits[best] if best else self.limits["default"]

    def _cleanup_stale_entries(self):
        """Periodically remove stale IPs/paths from in-memory storage to prevent unbounded growth."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now

        # Find the maximum window across all limits
        max_window = max(w for _, w in self.limits.values())
        cutoff = now - max_window

        stale_ips = []
        for ip, paths in self._requests.items():
            stale_paths = []
            for path, timestamps in paths.items():
                # Remove timestamps older than the max window
                fresh = [t for t in timestamps if t > cutoff]
                if fresh:
                    paths[path] = fresh
                else:
                    stale_paths.append(path)
            for p in stale_paths:
                del paths[p]
            if not paths:
                stale_ips.append(ip)
        for ip in stale_ips:
            del self._requests[ip]

    def is_allowed(self, ip: str, path: str) -> tuple[bool, dict]:
        """Check if request is allowed under rate limit.

        The counter is keyed by the path with identifier segments collapsed
        (see rate_limit_bucket), not by the literal URL: otherwise every
        document/job/session id would get a full quota of its own and the
        limit would not bound enumeration across ids.
        """
        self._cleanup_stale_entries()
        max_requests, window = self._get_limit(path)
        path = rate_limit_bucket(path)

        # Use Redis if available (sliding window with sorted sets)
        redis_client = require_redis()
        if redis_client:
            try:
                now = time.time()
                key = f"rate_limit:{ip}:{path}"

                # Remove old entries and count current
                pipe = redis_client.pipeline()
                pipe.zremrangebyscore(key, 0, now - window)
                pipe.zcard(key)
                pipe.zadd(key, {str(now): now})
                pipe.expire(key, window)
                results = pipe.execute()

                current_count = results[1]

                if current_count >= max_requests:
                    return False, {
                        "limit": max_requests,
                        "remaining": 0,
                        "retry_after": window,
                    }

                return True, {
                    "limit": max_requests,
                    "remaining": max_requests - current_count - 1,
                    "reset": int(now + window),
                }
            except (RedisError, ConnectionError, OSError):  # nosec B110 - Intentional fallback to in-memory
                # In production, fail closed — don't fall through to in-memory
                if _is_production():
                    logger.critical("Redis error during rate limiting in production")
                    return False, {
                        "limit": max_requests,
                        "remaining": 0,
                        "retry_after": 30,
                        "unavailable": True,
                    }
        elif _is_production():
            # Redis required in production — fail closed
            return False, {
                "limit": max_requests,
                "remaining": 0,
                "retry_after": 30,
                "unavailable": True,
            }

        # In-memory fallback (dev/demo only)
        now = time.time()
        cutoff = now - window

        # Clean old requests
        self._requests[ip][path] = [t for t in self._requests[ip][path] if t > cutoff]

        current_count = len(self._requests[ip][path])

        if current_count >= max_requests:
            retry_after = int(window - (now - self._requests[ip][path][0]))
            return False, {
                "limit": max_requests,
                "remaining": 0,
                "retry_after": retry_after,
            }

        # Record this request
        self._requests[ip][path].append(now)

        return True, {
            "limit": max_requests,
            "remaining": max_requests - current_count - 1,
            "retry_after": 0,
        }

    def record_login_failure(self, email: str):
        """Record a failed login attempt for account-level lockout."""
        if not email:
            return
        email = email.lower().strip()
        now = time.time()

        redis_client = require_redis()
        if redis_client:
            try:
                key = f"account_lockout:{email}"
                redis_client.zadd(key, {str(now): now})
                redis_client.zremrangebyscore(key, 0, now - self._account_lockout_window)
                redis_client.expire(
                    key, self._account_lockout_window + self._account_lockout_duration
                )
                return
            except (RedisError, ConnectionError, OSError):
                if _is_production():
                    logger.critical(
                        "Redis error recording login failure in production — "
                        "falling through to in-memory (is_account_locked fails closed)"
                    )

        # In-memory fallback
        cutoff = now - self._account_lockout_window
        self._account_failures[email] = [t for t in self._account_failures[email] if t > cutoff]
        self._account_failures[email].append(now)

    def _get_lockout_duration(self, failure_count: int) -> int:
        """Exponential backoff lockout: 15min, 30min, 1hr, 2hr, max 4hr."""
        if failure_count < self._account_lockout_threshold:
            return 0
        # How many times the threshold has been exceeded
        multiplier = min(1 << (failure_count // self._account_lockout_threshold - 1), 16)
        return self._account_lockout_duration * multiplier

    def is_account_locked(self, email: str) -> bool:
        """Check if an account is locked due to too many failed attempts."""
        if not email:
            return False
        email = email.lower().strip()
        now = time.time()

        redis_client = require_redis()
        if redis_client:
            try:
                key = f"account_lockout:{email}"
                cutoff = now - self._account_lockout_window
                redis_client.zremrangebyscore(key, 0, cutoff)
                count = redis_client.zcard(key)
                if count < self._account_lockout_threshold:
                    return False
                # Exponential backoff: check if lockout duration has elapsed
                last_failure = redis_client.zrange(key, -1, -1, withscores=True)
                if last_failure:
                    last_ts = float(last_failure[0][1])
                    lockout_secs = self._get_lockout_duration(count)
                    return (now - last_ts) < lockout_secs
                return True
            except (RedisError, ConnectionError, OSError):
                if _is_production():
                    logger.critical(
                        "Redis error checking account lockout in production — "
                        "failing closed (treating as locked)"
                    )
                    return True
        elif _is_production():
            # Redis required in production — fail closed (treat as locked)
            return True

        # In-memory fallback (dev/demo only)
        cutoff = now - self._account_lockout_window
        self._account_failures[email] = [t for t in self._account_failures[email] if t > cutoff]
        count = len(self._account_failures[email])
        if count < self._account_lockout_threshold:
            return False
        # Exponential backoff
        last_ts = self._account_failures[email][-1]
        lockout_secs = self._get_lockout_duration(count)
        return (now - last_ts) < lockout_secs

    def clear_account_lockout(self, email: str):
        """Clear lockout state after successful login."""
        if not email:
            return
        email = email.lower().strip()

        redis_client = get_redis()
        if redis_client:
            try:
                redis_client.delete(f"account_lockout:{email}")
            except (RedisError, ConnectionError, OSError):
                pass

        self._account_failures.pop(email, None)


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Comprehensive security middleware.
    """

    def __init__(self, app):
        super().__init__(app)
        self.rate_limiter = RateLimiter()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()

        # Get client IP (handle proxies)
        client_ip = self._get_client_ip(request)

        # Rate limiting
        allowed, rate_info = self.rate_limiter.is_allowed(client_ip, request.url.path)
        if not allowed:
            # Redis unavailable in production — return 503, not 429
            if rate_info.get("unavailable"):
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Service temporarily unavailable"},
                    headers={"Retry-After": "30"},
                )

            await audit_service.log_event(
                event_type=AuditEventType.RATE_LIMIT_EXCEEDED,
                ip_address=client_ip,
                details={"path": request.url.path, "limit": rate_info["limit"]},
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={
                    "Retry-After": str(rate_info["retry_after"]),
                    "X-RateLimit-Limit": str(rate_info["limit"]),
                    "X-RateLimit-Remaining": "0",
                },
            )

        # Process request
        try:
            response = await call_next(request)
        except Exception as e:  # Intentional broad catch - error boundary
            # Record that the request failed. Only the exception TYPE is kept:
            # the message can carry SQL parameters or document text, which must
            # not be copied into the long-retention audit trail.
            await audit_service.log_event(
                event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                ip_address=client_ip,
                details={"error_type": type(e).__name__, "path": request.url.path},
                success=False,
            )
            raise

        # Add security headers
        response = self._add_security_headers(response, rate_info, request.url.path)

        # Log request
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)

        return response

    def _get_client_ip(self, request: Request) -> str:
        """Get real client IP, handling proxies.

        Delegates to the shared ip_resolution utility which supports
        CIDR-based trusted proxy matching and secure right-to-left
        X-Forwarded-For walking with IP validation.
        """
        from app.utils.ip_resolution import get_client_ip

        return get_client_ip(request)

    def _add_security_headers(
        self, response: Response, rate_info: dict, request_path: str = ""
    ) -> Response:
        """Add security headers to response."""
        # Rate limit headers
        response.headers["X-RateLimit-Limit"] = str(rate_info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(rate_info["remaining"])

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        # Content Security Policy — the same policy the bundled proxies send
        # (Caddyfile, nginx.unified.conf); keep the three in step.
        # Sources are the ones the app actually loads: Google Sign-In + Picker
        # (accounts/apis.google.com, fetched only when the Google picker opens),
        # the Dropbox Chooser (www.dropbox.com), the Box Content Picker
        # (cdn01.boxcdn.net, api/upload.box.com), and Microsoft sign-in + Graph
        # for the OneDrive picker, whose launch is a form post to OneDrive or
        # SharePoint (form-action). Fonts are bundled; no font CDN. The browser
        # never calls an AI provider, so none is listed. Inline styles are
        # allowed for React style attributes; inline scripts only in DEBUG (the
        # Vite dev server). frame-src includes blob: for the PDF viewer.
        script_inline = " 'unsafe-inline'" if settings.debug else ""
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self'{script_inline} https://accounts.google.com https://apis.google.com https://www.dropbox.com https://cdn01.boxcdn.net; "
            "style-src 'self' 'unsafe-inline' https://accounts.google.com https://cdn01.boxcdn.net; "
            "font-src 'self'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://login.microsoftonline.com https://graph.microsoft.com https://accounts.google.com https://apis.google.com https://www.googleapis.com https://api.box.com https://upload.box.com; "
            "frame-src 'self' blob: https://accounts.google.com https://docs.google.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self' https://onedrive.live.com https://*.sharepoint.com"
        )

        # HSTS (production only — never in local development)
        # Enables strict HTTPS for 1 year, including subdomains
        if not settings.debug:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Cache-Control for API responses (prevent caching of sensitive data)
        # Only apply to API endpoints, not static files
        if "/api/" in request_path:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"

        return response


class SessionManager:
    """
    Secure session management.

    A session is one sign-in. It is keyed by the JTI of its CURRENT access
    token and carries a stable ``session_id`` that the refresh token is bound
    to, so a token refresh continues the same session instead of starting a
    new one.

    - Absolute lifetime: a session ends ``session_timeout`` after sign-in, no
      matter how often it is refreshed
    - Idle timeout: a session with no authenticated request for
      ``idle_timeout`` is ended
    - Concurrent session limit (oldest sign-in is evicted)
    - Optional session binding (IP)
    - File-based persistence to survive restarts without Redis
    """

    def __init__(self):
        # {user_id: [session_info]}
        self._sessions: dict[str, list] = defaultdict(list)
        self.max_sessions_per_user = 5
        self.session_timeout = timedelta(hours=settings.session_absolute_timeout_hours)
        self.idle_timeout = (
            timedelta(minutes=settings.session_idle_timeout_minutes)
            if settings.session_idle_timeout_minutes > 0
            else None
        )
        self._last_cleanup = datetime.now(UTC)
        self._cleanup_interval = timedelta(minutes=15)

        # File-based session persistence
        # Lives beside the other data-dir stores (upload_dir's parent IS the data
        # dir; the previous extra "data" segment created data/data/).
        self._session_file = Path(settings.upload_dir).parent / "sessions.jsonl"
        self._load_sessions_from_file()

    # ---------- File-based persistence ----------

    def _load_sessions_from_file(self):
        """Restore unexpired sessions from file on startup."""
        try:
            if not self._session_file.exists():
                return
            now = datetime.now(UTC)
            loaded = 0
            with open(self._session_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        expires_at = datetime.fromisoformat(entry["expires_at"])
                        if expires_at <= now:
                            continue
                        created_at = datetime.fromisoformat(entry["created_at"])
                        self._sessions[entry["user_id"]].append(
                            {
                                "jti": entry["jti"],
                                "session_id": entry.get("session_id") or entry["jti"],
                                "ip_address": entry.get("ip_address", "unknown"),
                                "user_agent": entry.get("user_agent", ""),
                                "created_at": created_at,
                                "expires_at": expires_at,
                                # The file is only rewritten on session changes,
                                # so its last_activity is stale by design. Time
                                # the process was down is not user idleness:
                                # restart the idle clock (the absolute lifetime
                                # in expires_at is unaffected).
                                "last_activity": now,
                            }
                        )
                        loaded += 1
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
            if loaded:
                logger.info(f"Restored {loaded} sessions from file")
        except OSError as e:
            logger.warning(f"Could not load sessions from file: {e}")

    @staticmethod
    def _file_entry(user_id: str, session: dict) -> dict:
        return {
            "user_id": user_id,
            "jti": session["jti"],
            "session_id": session.get("session_id") or session["jti"],
            "ip_address": session["ip_address"],
            "user_agent": session["user_agent"],
            "created_at": session["created_at"].isoformat(),
            "expires_at": session["expires_at"].isoformat(),
            "last_activity": session["last_activity"].isoformat(),
        }

    def _restrict_session_file(self):
        """Owner-only permissions (0o600) on the session file."""
        try:
            import os
            import stat

            os.chmod(self._session_file, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows may not support chmod

    def _persist_session(self, user_id: str, session: dict):
        """Append a session to the persistence file."""
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._session_file, "a") as f:
                f.write(json.dumps(self._file_entry(user_id, session)) + "\n")
            self._restrict_session_file()
        except OSError:
            pass  # Best-effort persistence

    def _rewrite_session_file(self):
        """Rewrite the session file with only active sessions (compaction)."""
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._session_file, "w") as f:
                for user_id, sessions in self._sessions.items():
                    for s in sessions:
                        f.write(json.dumps(self._file_entry(user_id, s)) + "\n")
            self._restrict_session_file()
        except OSError:
            pass

    def _is_idle(self, session: dict, now: datetime) -> bool:
        idle_timeout = getattr(self, "idle_timeout", None)
        return idle_timeout is not None and now - session["last_activity"] > idle_timeout

    def _is_live(self, session: dict, now: datetime) -> bool:
        return session["expires_at"] > now and not self._is_idle(session, now)

    def _cleanup_expired_sessions(self):
        """Periodically remove all expired sessions to prevent unbounded memory growth."""
        now = datetime.now(UTC)
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now

        stale_users = []
        for user_id, sessions in self._sessions.items():
            self._sessions[user_id] = [s for s in sessions if self._is_live(s, now)]
            if not self._sessions[user_id]:
                stale_users.append(user_id)
        for uid in stale_users:
            del self._sessions[uid]

        # Compact the session persistence file
        self._rewrite_session_file()

    def create_session(
        self,
        user_id: str,
        ip_address: str,
        user_agent: str,
        jti: str,
        session_id: str | None = None,
        client: str | None = None,
        max_sessions: int | None = None,
    ) -> bool:
        """Create a new session (one per sign-in), enforcing limits.

        ``session_id`` is the stable id the refresh token is bound to (``sid``
        claim); it defaults to a fresh random id. ``client`` labels the kind of
        client that signed in ("web" by default, "Word add-in" for a
        device-code grant) so the sessions list can tell them apart.
        ``max_sessions`` overrides the per-user concurrent-session cap (the
        shared public demo account is signed into by many visitors at once).
        """
        self._cleanup_expired_sessions()
        now = datetime.now(UTC)

        # Clean expired sessions
        self._sessions[user_id] = [s for s in self._sessions[user_id] if self._is_live(s, now)]

        # Check session limit
        cap = self.max_sessions_per_user if max_sessions is None else max_sessions
        evicted = False
        while len(self._sessions[user_id]) >= cap:
            # Remove oldest session
            self._sessions[user_id].sort(key=lambda s: s["created_at"])
            self._sessions[user_id].pop(0)
            evicted = True

        # Add new session
        session = {
            "jti": jti,
            "session_id": session_id or uuid.uuid4().hex,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "client": client or "web",
            "created_at": now,
            "expires_at": now + self.session_timeout,
            "last_activity": now,
        }
        self._sessions[user_id].append(session)
        if evicted:
            # The evicted sign-in must not come back from disk after a restart.
            self._rewrite_session_file()
        else:
            self._persist_session(user_id, session)

        return True

    def rotate_session(
        self, user_id: str, session_id: str | None, new_jti: str, ip_address: str | None = None
    ) -> str | None:
        """Move a live session onto a freshly minted access token (token refresh).

        Returns the JTI of the access token being replaced, or None when no live
        session with ``session_id`` exists (signed out, evicted by the session
        cap, idle too long, or past its absolute lifetime) — in which case the
        refresh must be refused. The session keeps its original ``created_at``
        and ``expires_at``: refreshing never extends the absolute lifetime.
        """
        if not session_id:
            return None
        now = datetime.now(UTC)
        for session in self._sessions.get(user_id, []):
            if session.get("session_id") != session_id:
                continue
            if not self._is_live(session, now):
                self._sessions[user_id] = [s for s in self._sessions[user_id] if s is not session]
                self._rewrite_session_file()
                return None
            if settings.enforce_session_ip_binding and ip_address:
                if session["ip_address"] != ip_address:
                    return None
            old_jti: str = session["jti"]
            session["jti"] = new_jti
            session["last_activity"] = now
            self._rewrite_session_file()
            return old_jti
        return None

    def validate_session(
        self,
        user_id: str,
        jti: str,
        ip_address: str | None = None,
        token_exp: datetime | None = None,
    ) -> bool:
        """Validate a session is still valid."""
        now = datetime.now(UTC)

        # Enforce JWT expiration independently of session expiration
        if token_exp is not None and token_exp < now:
            return False

        for session in self._sessions.get(user_id, []):
            if session["jti"] == jti:
                # Check absolute expiration
                if session["expires_at"] < now:
                    return False

                # Check idle timeout
                if self._is_idle(session, now):
                    return False

                # Check IP binding if enabled
                if settings.enforce_session_ip_binding and ip_address:
                    if session["ip_address"] != ip_address:
                        return False

                # Update last activity
                session["last_activity"] = now
                return True

        return False

    def terminate_session(self, user_id: str, jti: str):
        """Terminate a specific session."""
        self._sessions[user_id] = [s for s in self._sessions[user_id] if s["jti"] != jti]
        # Compact the persistence file immediately: a terminated session must
        # not survive on disk until the periodic 15-minute compaction.
        self._rewrite_session_file()

    def terminate_all_sessions(self, user_id: str):
        """Terminate all sessions for a user."""
        self._sessions[user_id] = []
        # Compact the persistence file immediately (see terminate_session).
        self._rewrite_session_file()

    def get_active_sessions(self, user_id: str) -> list:
        """Get all active sessions for a user (sanitized for API response)."""
        now = datetime.now(UTC)
        return [
            {
                "created_at": s["created_at"].isoformat(),
                "last_activity": s["last_activity"].isoformat(),
                "expires_at": s["expires_at"].isoformat(),
                "ip_address": s["ip_address"][:20] + "..."
                if len(s["ip_address"]) > 20
                else s["ip_address"],
                # Sessions persisted before the label existed are web sign-ins.
                "client": s.get("client") or "web",
            }
            for s in self._sessions.get(user_id, [])
            if self._is_live(s, now)
        ]

    def get_active_sessions_raw(self, user_id: str) -> list:
        """Get all active sessions with JTIs (internal use for token revocation)."""
        now = datetime.now(UTC)
        return [s for s in self._sessions.get(user_id, []) if self._is_live(s, now)]


# Global instances
session_manager = SessionManager()
account_lockout = RateLimiter()  # Shared instance for account-level lockout
