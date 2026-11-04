import asyncio
import contextvars
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from cryptography.exceptions import InvalidTag

from app.config import settings
from app.models.schemas import ConnectorType
from app.services.encryption import encryption_service
from app.utils.upload_validation import ALLOWED_MIME_TYPES

logger = logging.getLogger(__name__)

# Throttling responses worth waiting out: 429 (rate limited) and 503 (busy).
_THROTTLE_STATUSES = (429, 503)
# A provider's Retry-After is honoured up to this many seconds per attempt;
# anything longer (or absent) falls back to a short backoff.
RETRY_AFTER_MAX_WAIT = 30.0
RETRY_AFTER_ATTEMPTS = 3

# Folder markers returned by providers' listings (recursed into, never imported).
_FOLDER_MIME_TYPES = ("application/vnd.google-apps.folder", "folder")


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    """Seconds a 429/503 asked us to wait (delta-seconds or HTTP-date), or None."""
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())


async def call_with_retry_after(
    operation: Callable[[], Awaitable[Any]],
    *,
    attempts: int = RETRY_AFTER_ATTEMPTS,
    max_wait: float = RETRY_AFTER_MAX_WAIT,
) -> Any:
    """Run ``operation``, waiting out a 429/503 as the provider asks (bounded).

    Honours ``Retry-After`` up to ``max_wait`` seconds per attempt (a short
    backoff when the header is missing) for at most ``attempts`` tries, then
    re-raises so the caller records the failure. Other errors are not retried.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _THROTTLE_STATUSES or attempt == attempts:
                raise
            wait = retry_after_seconds(e.response)
            if wait is None:
                wait = min(2.0 * attempt, max_wait)
            wait = min(max(wait, 0.0), max_wait)
            logger.info(
                "Provider returned HTTP %s; retrying in %.1fs (attempt %d/%d)",
                e.response.status_code,
                wait,
                attempt,
                attempts,
            )
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


# Context variable for current user_id (async-safe, request-scoped)
_connector_user_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_connector_user_ctx", default=None
)


class ConnectorError(RuntimeError):
    """A connector could not complete a provider call (auth lost, bad response)."""


class FileTooLargeError(ValueError):
    """A remote file exceeds the instance upload size limit."""


# Legacy global credential files already reported in the log (once per process).
_legacy_warned: set[str] = set()


@dataclass
class FileInfo:
    """Information about a file from a connector."""

    id: str
    name: str
    mime_type: str
    size: int
    modified_at: datetime
    path: str | None = None
    download_url: str | None = None
    metadata: dict[str, Any] = None


class BaseConnector(ABC):
    """Base class for all data source connectors.

    Credentials are isolated per-user via ``contextvars``.  Before calling any
    method that reads or writes credentials the caller must invoke
    ``BaseConnector.set_current_user(user_id)`` so the connector knows whose
    credentials to load/save.  The router layer handles this automatically.
    """

    connector_type: ConnectorType = None

    # Whether ``list_files(folder_id)`` can scope a crawl to one folder. A
    # connector that only has a flat listing sets this False so a folder-scoped
    # sync is refused instead of silently importing the whole account.
    supports_folder_sync: bool = True

    # True for connectors that authenticate with one firm-wide credential
    # rather than a per-user OAuth grant. The router only lets users holding
    # ``admin.settings`` use them, since the credential carries the access of
    # the whole firm rather than of the person syncing.
    requires_admin: bool = False

    def __init__(self):
        # Per-user credential cache: {user_id: {cred dict}}
        # Kept in memory for the lifetime of the process to avoid repeated
        # disk reads within a single request.  Each entry is small (~1 KB).
        self._credentials_cache: dict[str, dict[str, Any]] = {}
        # Modification time of the credentials file each cached entry was
        # read from: a token refresh written by another process changes it,
        # and the next read here reloads instead of serving the stale token.
        self._credentials_mtime: dict[str, float | None] = {}

    # ------------------------------------------------------------------
    # User context helpers (async-safe via contextvars)
    # ------------------------------------------------------------------

    @staticmethod
    def set_current_user(user_id: str) -> None:
        """Set the current user for credential isolation (call from router)."""
        _connector_user_ctx.set(user_id)

    # ------------------------------------------------------------------
    # Credential path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_user_id(user_id: str) -> str:
        """Sanitise user_id for safe use in file paths."""
        return re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)

    def _get_credentials_path(self, user_id: str) -> str:
        """Get path to *user-scoped* credentials file."""
        safe_id = self._safe_user_id(user_id)
        return os.path.join(
            settings.upload_dir,
            f"credentials_{self.connector_type.value}_{safe_id}.json",
        )

    def _get_legacy_credentials_path(self) -> str:
        """Legacy (pre-isolation) global credentials file."""
        return os.path.join(
            settings.upload_dir,
            f"credentials_{self.connector_type.value}.json",
        )

    # ------------------------------------------------------------------
    # Low-level credential I/O (always user-scoped)
    # ------------------------------------------------------------------

    def _read_credentials_file(self, path: str) -> dict[str, Any]:
        """Read and decrypt a credentials file, returning {} on failure."""
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                encrypted_data = f.read()
            if not encrypted_data:
                return {}
            try:
                decrypted = encryption_service.decrypt_string(encrypted_data)
                return json.loads(decrypted)
            except (ValueError, KeyError, ConnectionError, TimeoutError, OSError, InvalidTag):
                # Legacy unencrypted file – migrate silently
                try:
                    creds = json.loads(encrypted_data)
                    # Re-save encrypted at the same path
                    self._write_credentials_file(path, creds)
                    logger.info("Migrated legacy unencrypted credentials to encrypted format")
                    return creds
                except (json.JSONDecodeError, ValueError):
                    return {}
        except OSError:
            return {}

    @staticmethod
    def _write_credentials_file(path: str, credentials: dict[str, Any]) -> None:
        """Encrypt and write credentials to *path* atomically with 0600 perms."""
        import os
        import stat

        try:
            json_data = json.dumps(credentials)
            encrypted_data = encryption_service.encrypt_string(json_data)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w") as f:
                f.write(encrypted_data)
            try:
                os.chmod(
                    tmp_path, stat.S_IRUSR | stat.S_IWUSR
                )  # owner-only (best effort on Windows)
            except OSError:
                pass
            os.replace(tmp_path, path)  # atomic swap — no truncated file on crash
        except (ValueError, OSError, TypeError, RuntimeError) as e:
            logger.error(f"Error saving credentials: {e}")

    # ------------------------------------------------------------------
    # Public credential accessors (user-scoped via contextvars)
    # ------------------------------------------------------------------

    def _load_credentials_for_user(self, user_id: str) -> dict[str, Any]:
        """Load the user-scoped credentials for *user_id*.

        A pre-isolation global ``credentials_<type>.json`` is never adopted:
        it holds one account's tokens with no record of whose they were, so
        handing it to whichever user happened to load first would give that
        user someone else's drive. It is left on disk untouched and reported
        once so an administrator can remove it; users reconnect via OAuth.
        """
        creds = self._read_credentials_file(self._get_credentials_path(user_id))
        if creds:
            return creds

        legacy_path = self._get_legacy_credentials_path()
        if legacy_path not in _legacy_warned and os.path.exists(legacy_path):
            _legacy_warned.add(legacy_path)
            logger.warning(
                "Ignoring legacy global credentials file for %s (%s): it is not tied "
                "to a user. Delete it and have each user reconnect the connector.",
                self.connector_type.value,
                os.path.basename(legacy_path),
            )

        return {}

    def _save_credentials_for_user(self, user_id: str, creds: dict[str, Any]) -> None:
        path = self._get_credentials_path(user_id)
        self._write_credentials_file(path, creds)

    def _clear_credentials_for_user(self, user_id: str) -> None:
        self._credentials_cache.pop(user_id, None)
        getattr(self, "_credentials_mtime", {}).pop(user_id, None)
        path = self._get_credentials_path(user_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # self.credentials property – drop-in replacement for subclasses
    # ------------------------------------------------------------------

    def _credentials_file_mtime(self, user_id: str) -> float | None:
        try:
            return os.stat(self._get_credentials_path(user_id)).st_mtime
        except OSError:
            return None

    @property
    def credentials(self) -> dict[str, Any]:
        """Return credentials for the *current* user (set via set_current_user).

        Cached per user, but the cache follows the credentials file: when the
        file changed since this entry was read (a refresh in another worker
        process, a reconnect, a removal), it is reloaded. An entry set here
        and not yet saved is kept until ``_save_credentials`` writes it.
        """
        user_id = _connector_user_ctx.get()
        if not user_id:
            return {}
        mtimes = getattr(self, "_credentials_mtime", None)
        if mtimes is None:
            mtimes = self._credentials_mtime = {}
        current = self._credentials_file_mtime(user_id)
        if user_id not in self._credentials_cache or (
            user_id in mtimes and mtimes[user_id] != current
        ):
            self._credentials_cache[user_id] = self._load_credentials_for_user(user_id)
            mtimes[user_id] = current
        return self._credentials_cache[user_id]

    @credentials.setter
    def credentials(self, value: dict[str, Any]) -> None:
        user_id = _connector_user_ctx.get()
        if user_id:
            self._credentials_cache[user_id] = value
            # Unsaved: not tied to any file state until _save_credentials runs.
            getattr(self, "_credentials_mtime", {}).pop(user_id, None)

    # Legacy helpers – now user-scoped via the property
    def _save_credentials(self) -> None:
        """Persist credentials for the current user to disk."""
        user_id = _connector_user_ctx.get()
        if not user_id:
            logger.warning("Cannot save credentials: no user context set")
            return
        creds = self._credentials_cache.get(user_id, {})
        self._save_credentials_for_user(user_id, creds)
        mtimes = getattr(self, "_credentials_mtime", None)
        if mtimes is not None:
            mtimes[user_id] = self._credentials_file_mtime(user_id)

    def _clear_credentials(self) -> None:
        """Clear credentials for the current user."""
        user_id = _connector_user_ctx.get()
        if not user_id:
            return
        self._clear_credentials_for_user(user_id)

    @property
    def is_connected(self) -> bool:
        """Check if connector has valid credentials for the current user."""
        return bool(self.credentials.get("access_token"))

    @property
    def is_configured(self) -> bool:
        """Check if connector has required OAuth/API credentials configured.
        Subclasses should override to check their specific required settings."""
        return True

    @abstractmethod
    def get_auth_url(self, state: str) -> str:
        """Get OAuth authorization URL."""
        pass

    @abstractmethod
    async def handle_callback(self, code: str, state: str) -> dict[str, Any]:
        """Handle OAuth callback and exchange code for tokens."""
        pass

    @abstractmethod
    async def refresh_token(self) -> bool:
        """Refresh access token if expired."""
        pass

    @abstractmethod
    async def get_account_info(self) -> dict[str, Any]:
        """Get connected account information."""
        pass

    @abstractmethod
    async def list_files(
        self, folder_id: str | None = None, page_token: str | None = None
    ) -> tuple[list[FileInfo], str | None]:
        """List files in a folder. Returns (files, next_page_token)."""
        pass

    @abstractmethod
    async def download_file(self, file_id: str) -> bytes:
        """Download a file's content."""
        pass

    async def _download_capped(
        self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> bytes:
        """Stream a provider download, refusing bodies over the upload limit.

        Checked against the declared Content-Length first and then while
        reading, so an oversized (or mis-declared) file is never buffered whole.
        """
        max_bytes = settings.max_upload_size
        async with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FileTooLargeError(f"File exceeds the {max_bytes}-byte upload limit")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise FileTooLargeError(f"File exceeds the {max_bytes}-byte upload limit")
            return bytes(chunks)

    async def crawl_all_files(
        self,
        supported_types: list[str] = None,
        folder_id: str | None = None,
        errors: list[dict[str, str]] | None = None,
    ) -> AsyncGenerator[FileInfo, None]:
        """Crawl files recursively, starting at *folder_id* (root when None).

        ``supported_types`` defaults to every type the upload endpoint accepts
        (one allowlist for uploads, picker imports and syncs). A subfolder
        whose listing fails (403 on a restricted folder, 429 past the bounded
        retry) is recorded in ``errors`` as ``{"name", "reason"}`` and skipped
        instead of aborting the whole crawl; a failing START folder still
        raises, since then nothing could be synced at all.
        """
        if folder_id and not self.supports_folder_sync:
            raise ConnectorError(
                f"{self.connector_type.value} cannot sync a single folder; "
                "only a full-account sync is available."
            )
        if supported_types is None:
            supported_types = list(ALLOWED_MIME_TYPES)

        async for file_info in self._crawl_folder(
            folder_id, supported_types, errors if errors is not None else [], is_start=True
        ):
            yield file_info

    async def _crawl_folder(
        self,
        folder_id: str | None,
        supported_types: list[str],
        errors: list[dict[str, str]],
        *,
        is_start: bool = False,
        folder_name: str | None = None,
    ) -> AsyncGenerator[FileInfo, None]:
        """Recursively crawl a folder."""
        page_token = None

        while True:
            try:
                files, next_token = await call_with_retry_after(
                    lambda: self.list_files(folder_id, page_token)
                )
            except httpx.HTTPStatusError as e:
                if is_start:
                    raise
                # One unreadable subfolder must not take the whole sync down.
                label = folder_name or folder_id or "folder"
                logger.warning(
                    "Skipping folder %s: provider returned HTTP %s",
                    label,
                    e.response.status_code,
                )
                errors.append(
                    {
                        "name": f"{label}/",
                        "reason": f"folder skipped: provider returned HTTP {e.response.status_code}",
                    }
                )
                return

            for file_info in files:
                # Check if it's a supported file type
                if file_info.mime_type in supported_types:
                    yield file_info
                # If it's a folder, recurse into it
                elif file_info.mime_type in _FOLDER_MIME_TYPES:
                    async for sub_file in self._crawl_folder(
                        file_info.id, supported_types, errors, folder_name=file_info.name
                    ):
                        yield sub_file

            if not next_token:
                break
            page_token = next_token

    async def disconnect(self):
        """Disconnect: revoke the provider grant where possible, then clear credentials.

        Revocation is best-effort and never blocks the local deletion — a
        provider outage must not leave tokens on disk after the user asked to
        disconnect.
        """
        creds = dict(self.credentials)
        if creds:
            await _revoke_token_best_effort(self.connector_type, creds)
        self._clear_credentials()

    async def get_status(self) -> dict[str, Any]:
        """Get connector status."""
        configured = self.is_configured
        if not configured:
            return {"connected": False, "configured": False, "type": self.connector_type.value}

        if not self.is_connected:
            return {"connected": False, "configured": True, "type": self.connector_type.value}

        try:
            account = await self.get_account_info()
            return {
                "connected": True,
                "configured": True,
                "type": self.connector_type.value,
                "account_name": account.get("name"),
                "account_email": account.get("email"),
                "last_sync": self.credentials.get("last_sync"),
            }
        except (
            httpx.HTTPError,
            ConnectorError,
            ConnectionError,
            TimeoutError,
            KeyError,
            ValueError,
        ):
            # Includes an expired grant that can no longer be refreshed: report
            # "not connected" instead of failing the whole connector list.
            return {
                "connected": False,
                "configured": True,
                "type": self.connector_type.value,
                "error": "Failed to get account info",
            }


# ----------------------------------------------------------------------
# Right-to-erasure helpers (module-level; used by user deletion)
# ----------------------------------------------------------------------


def _credentials_path_for(connector_type: ConnectorType, user_id: str) -> str:
    """Path of the user-scoped credential file for *connector_type*."""
    safe_id = BaseConnector._safe_user_id(user_id)
    return os.path.join(settings.upload_dir, f"credentials_{connector_type.value}_{safe_id}.json")


def _read_credentials_at(path: str) -> dict[str, Any]:
    """Decrypt a credential file for revocation; {} on any failure. Never writes."""
    try:
        with open(path) as f:
            raw = f.read()
        if not raw:
            return {}
        try:
            return json.loads(encryption_service.decrypt_string(raw))
        except (ValueError, KeyError, TypeError, OSError, InvalidTag):
            return json.loads(raw)  # legacy unencrypted file
    except (OSError, ValueError, TypeError):
        return {}


async def _fresh_dropbox_access_token(creds: dict[str, Any]) -> str | None:
    """Mint a new Dropbox access token from the stored refresh token, for revocation.

    None when there is no refresh token, the app is not configured, or Dropbox
    refuses — the caller then falls back to the stored access token.
    """
    from app.services import connector_credentials

    refresh_token = creds.get("refresh_token")
    app_key = connector_credentials.get("dropbox_app_key")
    app_secret = connector_credentials.get("dropbox_app_secret")
    if not (refresh_token and app_key and app_secret):
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.dropboxapi.com/oauth2/token",
                data={
                    "client_id": app_key,
                    "client_secret": app_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if not resp.is_success:
            logger.warning(
                "Dropbox token refresh before revocation returned HTTP %s", resp.status_code
            )
            return None
        token = resp.json().get("access_token")
        return token if isinstance(token, str) and token else None
    except (httpx.HTTPError, OSError, ValueError, AttributeError) as e:
        logger.warning("Dropbox token refresh before revocation failed: %s", e)
        return None


async def _revoke_token_best_effort(connector_type: ConnectorType, creds: dict[str, Any]) -> bool:
    """Invalidate the provider-side OAuth grant where a cheap endpoint exists.

    Google: POST oauth2.googleapis.com/revoke with the refresh token (falls back
    to the access token); revoking either invalidates the whole grant.
    Dropbox: POST /2/auth/token/revoke with a bearer access token, which also
    disables the paired refresh token — but only if that access token is still
    valid. Dropbox access tokens live four hours, so a stored one is usually
    expired and the refresh token would survive; when a refresh token is on
    file a fresh access token is minted first and that one is revoked.
    Box: POST api.box.com/oauth2/revoke with the app's client credentials and
    the refresh token (falls back to the access token); revoking either
    invalidates the pair. Other providers have no comparably cheap endpoint
    (Microsoft only offers revoking every session of the user), so their files
    are just deleted. Failures are logged, not raised.
    """
    try:
        if connector_type in (ConnectorType.GOOGLE_DRIVE, ConnectorType.GOOGLE_PICKER):
            token = creds.get("refresh_token") or creds.get("access_token")
            if not token:
                return False
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/revoke", data={"token": token}
                )
        elif connector_type == ConnectorType.DROPBOX:
            token = await _fresh_dropbox_access_token(creds) or creds.get("access_token")
            if not token:
                return False
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.dropboxapi.com/2/auth/token/revoke",
                    headers={"Authorization": f"Bearer {token}"},
                )
        elif connector_type in (ConnectorType.BOX, ConnectorType.BOX_PICKER):
            from app.services import connector_credentials

            token = creds.get("refresh_token") or creds.get("access_token")
            client_id = connector_credentials.get("box_client_id")
            client_secret = connector_credentials.get("box_client_secret")
            if not (token and client_id and client_secret):
                return False
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.box.com/oauth2/revoke",
                    data={"client_id": client_id, "client_secret": client_secret, "token": token},
                )
        else:
            return False
        if resp.is_success:
            return True
        logger.warning(
            "Token revocation for %s returned HTTP %s", connector_type.value, resp.status_code
        )
        return False
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.warning("Token revocation for %s failed: %s", connector_type.value, e)
        return False


async def delete_user_credentials(user_id: str) -> dict[str, int]:
    """Erase every connector credential file for *user_id* (right-to-erasure).

    Iterates every ``ConnectorType``; for providers with a cheap revocation
    endpoint the stored token is revoked best-effort first, then the file is
    deleted and the in-process credential cache is dropped. Returns
    ``{"connector_credentials_deleted": n, "connector_tokens_revoked": m}``.
    """
    deleted = 0
    revoked = 0
    for connector_type in ConnectorType:
        path = _credentials_path_for(connector_type, user_id)
        if not os.path.exists(path):
            continue
        creds = _read_credentials_at(path)
        if creds and await _revoke_token_best_effort(connector_type, creds):
            revoked += 1
        try:
            os.remove(path)
            deleted += 1
        except OSError as e:
            logger.warning("Could not delete %s credentials for user: %s", connector_type.value, e)

    # Drop the per-process cache so a live connector object cannot resurrect
    # the tokens for the rest of this process's lifetime.
    try:
        from app.services.connectors import CONNECTORS

        for connector in CONNECTORS.values():
            connector._credentials_cache.pop(user_id, None)
    except ImportError:  # pragma: no cover - registry not importable in isolation
        pass

    return {"connector_credentials_deleted": deleted, "connector_tokens_revoked": revoked}
