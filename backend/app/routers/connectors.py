import logging
import re
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.exceptions import BadRequestError, ForbiddenError, NotFoundError, UpstreamError
from app.middleware.csrf import cookie_secure
from app.models.responses.common import StatusResponse
from app.models.responses.connectors import (
    ConnectorCallbackResponse,
    ConnectorFolder,
    ConnectorFoldersResponse,
    ConnectorListResponse,
    SyncStartResponse,
)
from app.models.schemas import (
    ConnectorAuthUrl,
    ConnectorStatus,
    ConnectorType,
    Document,
    DocumentStatus,
)
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.connectors import get_all_connector_statuses, get_connector, is_experimental
from app.services.connectors.base import (
    _FOLDER_MIME_TYPES,
    BaseConnector,
    ConnectorError,
    FileInfo,
    FileTooLargeError,
    call_with_retry_after,
)
from app.services.connectors.sync_state import (
    OAUTH_BINDING_COOKIE,
    oauth_binding_matches,
    oauth_binding_token,
    store_oauth_state,
    validate_oauth_state,
)
from app.services.documents import document_service
from app.services.job_queue import RETRY_POLICIES, job_manager
from app.services.permissions import effective_permissions, require_permission
from app.services.user_keys import UserAPIKeys
from app.utils.ip_resolution import get_client_ip
from app.utils.upload_validation import sanitize_filename, validate_import_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connectors", tags=["connectors"])

# Errors that fail one file (or one provider call) without being a bug:
# provider HTTP errors (403/404/429...), network trouble, a lost grant,
# an oversized or disallowed file.
_PROVIDER_ERRORS = (
    httpx.HTTPError,
    ConnectorError,
    ValueError,
    KeyError,
    ConnectionError,
    TimeoutError,
    OSError,
)

# Stop a sync once this many files in a row have failed: the grant was most
# likely revoked or the provider is throttling, and hammering on helps nobody.
_MAX_CONSECUTIVE_FAILURES = 10

# Per-file errors reported back (the count is always complete).
MAX_REPORTED_ERRORS = 20

_FOLDER_ID_MAX_LENGTH = 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_ADMIN_ONLY_DETAIL = (
    "This connector uses a firm-wide credential and can only be used by an administrator."
)

# The OAuth state lives 10 minutes (store_oauth_state); so does its cookie.
_OAUTH_STATE_TTL_SECONDS = 600


def _oauth_cookie_path() -> str:
    return f"{settings.api_prefix}/connectors"


class SyncRequest(BaseModel):
    """POST /connectors/{type}/sync body.

    A sync is scoped to ``folder_id``. Importing the whole account has to be
    asked for explicitly with ``sync_all`` — it is never the silent default.
    """

    folder_id: str | None = Field(default=None, max_length=_FOLDER_ID_MAX_LENGTH)
    sync_all: bool = False


def _describe_error(exc: BaseException) -> str:
    """Short, user-safe reason for a failure (no URLs, tokens or tracebacks)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"provider returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "could not reach the provider"
    if isinstance(exc, ValueError | ConnectorError):
        return str(exc) or type(exc).__name__
    return type(exc).__name__


async def _is_connector_admin(current_user: TokenData) -> bool:
    perms = await effective_permissions(current_user.roles or [])
    return "admin.settings" in perms


async def _require_connector_access(connector: BaseConnector, current_user: TokenData) -> None:
    """Refuse firm-wide-credential connectors to anyone but an administrator."""
    if connector.requires_admin and not await _is_connector_admin(current_user):
        raise ForbiddenError(_ADMIN_ONLY_DETAIL)


@router.get("", response_model=ConnectorListResponse)
async def list_connectors(
    current_user: TokenData = require_permission("connectors.manage"),
) -> ConnectorListResponse:
    """Get status of all connectors. Requires authentication."""
    BaseConnector.set_current_user(current_user.user_id)
    statuses = await get_all_connector_statuses(
        include_admin_only=await _is_connector_admin(current_user)
    )
    # Add id field matching connector type for frontend lookup
    for s in statuses:
        s["id"] = s.get("type", "")
    return {"connectors": statuses}


@router.get("/{connector_type}/status", response_model=ConnectorStatus)
async def get_connector_status(
    connector_type: ConnectorType, current_user: TokenData = require_permission("connectors.manage")
) -> ConnectorStatus:
    """Get status of a specific connector. Requires authentication."""
    BaseConnector.set_current_user(current_user.user_id)
    connector = get_connector(connector_type)
    experimental = is_experimental(connector_type)
    if connector.requires_admin and not await _is_connector_admin(current_user):
        return ConnectorStatus(
            type=connector_type,
            connected=False,
            configured=connector.is_configured,
            experimental=experimental,
        )
    status = await connector.get_status()
    return ConnectorStatus(
        type=connector_type,
        connected=status.get("connected", False),
        configured=status.get("configured", True),
        account_name=status.get("account_name"),
        account_email=status.get("account_email"),
        docs_indexed=status.get("docs_indexed", 0),
        last_sync=status.get("last_sync"),
        experimental=experimental,
    )


@router.get("/{connector_type}/auth", response_model=ConnectorAuthUrl)
async def get_auth_url(
    connector_type: ConnectorType,
    request: Request,
    response: Response,
    current_user: TokenData = require_permission("connectors.manage"),
) -> ConnectorAuthUrl:
    """Get OAuth authorization URL for a connector. Requires authentication."""
    BaseConnector.set_current_user(current_user.user_id)
    connector = get_connector(connector_type)
    await _require_connector_access(connector, current_user)

    # Check if connector has required credentials configured
    if not connector.is_configured:
        raise BadRequestError(
            f"{connector_type.value} is not configured. An administrator needs to set up OAuth credentials for this connector.",
        )

    state = str(uuid.uuid4())

    # Store state with user binding and expiry for validation in callback
    store_oauth_state(state, current_user.user_id, connector_type)

    # Bind the state to this browser as well: the provider's redirect to the
    # callback is unauthenticated, so without this a state handed to another
    # browser would store that browser's grant under this user. Lax, not
    # Strict — the redirect back from the provider is a cross-site top-level
    # GET, which is exactly what Lax still sends the cookie on.
    response.set_cookie(
        OAUTH_BINDING_COOKIE,
        oauth_binding_token(state, current_user.user_id),
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        max_age=_OAUTH_STATE_TTL_SECONDS,
        path=_oauth_cookie_path(),
    )

    auth_url = connector.get_auth_url(state)

    return ConnectorAuthUrl(auth_url=auth_url, state=state)


@router.get("/{connector_type}/callback", response_model=ConnectorCallbackResponse)
async def handle_callback(
    connector_type: ConnectorType, code: str, state: str, request: Request, response: Response
) -> ConnectorCallbackResponse:
    """Handle OAuth callback from connector.

    Security:
    - Validates state exists and is not expired
    - Validates state matches the connector type
    - State is single-use (deleted after validation to prevent replay)
    - The browser must present the signed binding cookie issued when the
      flow started, so only the browser of the user who began the flow can
      complete it
    - Credentials stored under the user who initiated the OAuth flow
    """
    # Validate OAuth state (provides CSRF protection and user binding)
    user_id = validate_oauth_state(state, connector_type)
    if not user_id:
        await audit_service.log_event(
            event_type=AuditEventType.SECURITY_ALERT,
            ip_address=get_client_ip(request),
            details={
                "action": "oauth_callback_invalid_state",
                "connector_type": connector_type.value,
                "reason": "state_invalid_expired_or_mismatched",
            },
            success=False,
        )
        raise BadRequestError(
            "Invalid or expired OAuth state. Please try connecting again.",
        )

    # The state is consumed either way; a mismatch burns it rather than
    # leaving it redeemable from the right browser later.
    if not oauth_binding_matches(request.cookies.get(OAUTH_BINDING_COOKIE), state, user_id):
        await audit_service.log_event(
            event_type=AuditEventType.SECURITY_ALERT,
            user_id=user_id,
            ip_address=get_client_ip(request),
            details={
                "action": "oauth_callback_browser_mismatch",
                "connector_type": connector_type.value,
                "reason": "binding_cookie_missing_or_for_another_user",
            },
            success=False,
        )
        raise ForbiddenError(
            "This connection was started from a different browser session. "
            "Please try connecting again.",
        )
    response.delete_cookie(OAUTH_BINDING_COOKIE, path=_oauth_cookie_path())

    # Set user context so credentials are stored for the correct user
    BaseConnector.set_current_user(user_id)
    connector = get_connector(connector_type)

    try:
        account_info = await connector.handle_callback(code, state)
    except _PROVIDER_ERRORS as e:
        # A rejected code, a provider outage or an unexpected token response:
        # a clean 400, never a 500 with a provider traceback.
        logger.error(
            "Connector callback failed for %s: %s", connector_type.value, _describe_error(e)
        )
        raise BadRequestError("Failed to connect. Please try again.")

    # Log the connection with user attribution
    await audit_service.log_event(
        event_type=AuditEventType.CONNECTOR_CONNECT,
        user_id=user_id,
        resource_type="connector",
        resource_id=connector_type.value,
        ip_address=get_client_ip(request),
        details={"account": account_info.get("email") or account_info.get("name")},
    )

    return {"status": "connected", "account": account_info}


@router.post("/{connector_type}/disconnect", response_model=StatusResponse)
async def disconnect_connector(
    connector_type: ConnectorType,
    request: Request,
    current_user: TokenData = require_permission("connectors.manage"),
) -> StatusResponse:
    """Disconnect a connector. Requires authentication."""
    BaseConnector.set_current_user(current_user.user_id)
    connector = get_connector(connector_type)
    await _require_connector_access(connector, current_user)
    await connector.disconnect()

    # Log the disconnection
    await audit_service.log_event(
        event_type=AuditEventType.CONNECTOR_DISCONNECT,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="connector",
        resource_id=connector_type.value,
        ip_address=get_client_ip(request),
    )

    return {"status": "disconnected"}


@router.get("/{connector_type}/folders", response_model=ConnectorFoldersResponse)
async def list_folders(
    connector_type: ConnectorType,
    parent_id: str | None = Query(default=None, max_length=_FOLDER_ID_MAX_LENGTH),
    page_token: str | None = Query(default=None, max_length=2048),
    current_user: TokenData = require_permission("connectors.manage"),
) -> ConnectorFoldersResponse:
    """One level of the connected account's folder tree, for choosing what to sync.

    Folders only (files are never listed here); ``parent_id`` omitted means
    the root. Refused for a connector that can only sync the whole account.
    """
    BaseConnector.set_current_user(current_user.user_id)
    connector = get_connector(connector_type)
    await _require_connector_access(connector, current_user)
    if not connector.is_connected:
        raise BadRequestError("Connector not connected")
    if not connector.supports_folder_sync:
        raise BadRequestError(
            f"{connector_type.value} cannot sync a single folder; only a full-account "
            "sync is available."
        )
    if parent_id and _CONTROL_CHARS.search(parent_id):
        raise BadRequestError("Invalid parent_id")
    try:
        files, next_token = await call_with_retry_after(
            lambda: connector.list_files(parent_id or None, page_token or None)
        )
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"{connector_type.value} refused the folder listing (HTTP {e.response.status_code})."
        )
    except (ConnectorError, httpx.HTTPError) as e:
        raise UpstreamError(_describe_error(e))
    folders = [
        ConnectorFolder(id=f.id, name=f.name, path=f.path)
        for f in files
        if f.mime_type in _FOLDER_MIME_TYPES
    ]
    return ConnectorFoldersResponse(
        connector=connector_type.value,
        parent_id=parent_id or None,
        folders=folders,
        next_page_token=next_token,
    )


@router.post("/{connector_type}/sync", response_model=SyncStartResponse)
async def start_sync(
    connector_type: ConnectorType,
    request: Request,
    body: SyncRequest | None = None,
    folder_id: str | None = Query(default=None, max_length=_FOLDER_ID_MAX_LENGTH),
    sync_all: bool = False,
    current_user: TokenData = require_permission("connectors.manage"),
) -> SyncStartResponse:
    """Start syncing documents from a connector. Requires authentication.

    The scope comes from the JSON body (``folder_id`` / ``sync_all``) or the
    equivalent query parameters. Without a folder the request must carry
    ``sync_all=true``; otherwise it is refused rather than importing — and
    sending to the embedding provider — the entire account.
    """
    BaseConnector.set_current_user(current_user.user_id)
    connector = get_connector(connector_type)
    await _require_connector_access(connector, current_user)

    if not connector.is_connected:
        raise BadRequestError("Connector not connected")

    folder_id = (body.folder_id if body else None) or folder_id or None
    sync_all = sync_all or bool(body and body.sync_all)

    if folder_id:
        if _CONTROL_CHARS.search(folder_id):
            raise BadRequestError("Invalid folder_id")
        if not connector.supports_folder_sync:
            raise BadRequestError(
                f"{connector_type.value} cannot sync a single folder. "
                "Request a full-account sync with sync_all=true instead.",
            )
    elif not sync_all:
        raise BadRequestError(
            "Choose a folder to sync (folder_id), or set sync_all=true to "
            "import every document in the connected account.",
        )

    user_id = current_user.user_id
    user_email = current_user.email
    client_ip = get_client_ip(request)

    # Log sync start
    await audit_service.log_event(
        event_type=AuditEventType.CONNECTOR_SYNC_START,
        user_id=user_id,
        user_email=user_email,
        resource_type="connector",
        resource_id=connector_type.value,
        ip_address=client_ip,
        details={"folder_id": folder_id, "sync_all": not folder_id},
    )

    async def do_sync():
        return await run_sync(
            connector=connector,
            connector_type=connector_type,
            folder_id=folder_id,
            user_id=user_id,
            user_email=user_email,
        )

    # Submit to job_manager (Redis-backed, in-memory without Redis). A replayed
    # sync is safe — unchanged files are skipped — but the job manager decides
    # whether connector syncs are retried at all.
    job_id = await job_manager.submit(
        do_sync,
        user_id=user_id,
        endpoint=f"/connectors/{connector_type.value}/sync",
        retry_policy=RETRY_POLICIES.get("connector_sync", RETRY_POLICIES["external_api"]),
    )

    return {"sync_id": job_id, "status": "started"}


@router.get("/{connector_type}/sync/{sync_id}")
async def get_sync_status_endpoint(
    connector_type: ConnectorType,
    sync_id: str,
    current_user: TokenData = require_permission("connectors.manage"),
) -> dict:
    """Get status of a sync operation. Requires authentication and ownership."""
    job_data = job_manager.get_job(sync_id, current_user.user_id)
    if not job_data:
        raise NotFoundError("Sync not found")
    return job_data


def _source_marker(file_info: FileInfo) -> dict:
    """What identifies this version of a remote file (stored on the document)."""
    return {
        "source_modified_at": file_info.modified_at.isoformat() if file_info.modified_at else None,
        "source_size": file_info.size,
    }


def _already_imported(user_id: str | None, connector_type: ConnectorType) -> dict[str, Document]:
    """Indexed documents this user already imported from this connector, by source id."""
    return {
        doc.source_id: doc
        for doc in document_service.documents.values()
        if doc.user_id == user_id
        and doc.source == connector_type
        and doc.source_id
        and doc.status == DocumentStatus.INDEXED
    }


def _is_unchanged(existing: Document | None, file_info: FileInfo) -> bool:
    """True when *file_info* is the exact version already imported."""
    if existing is None:
        return False
    marker = _source_marker(file_info)
    return (
        marker["source_modified_at"] is not None
        and existing.metadata.get("source_modified_at") == marker["source_modified_at"]
        and existing.metadata.get("source_size") == marker["source_size"]
    )


async def run_sync(
    connector,
    connector_type: ConnectorType,
    folder_id: str | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
) -> dict:
    """Background task to sync documents from a connector. Returns result dict.

    Crawls *folder_id* (the whole account only when it is None, which the
    endpoint allows solely on an explicit ``sync_all``). Each file is
    size-capped, validated and audited like a direct upload. A file that
    fails is recorded and skipped; files already imported unchanged are
    skipped too, so a retried job does not redo finished work.
    """
    # Restore user context in the background task (contextvars don't propagate)
    if user_id:
        BaseConnector.set_current_user(user_id)

    processed = 0
    skipped = 0
    # Per-file (and per-skipped-folder) failures: {"name", "reason"}.
    errors: list[dict[str, str]] = []

    try:
        # The user's stored BYOK keys, so embeddings work on BYOK-only instances.
        user_keys = UserAPIKeys.for_user(user_id) if user_id else None
        already = _already_imported(user_id, connector_type)

        files = []
        async for file_info in connector.crawl_all_files(folder_id=folder_id, errors=errors):
            files.append(file_info)

        logger.info(f"Sync: Found {len(files)} files to process")

        consecutive_failures = 0
        for file_info in files:
            display_name = sanitize_filename(file_info.name)
            try:
                if _is_unchanged(already.get(file_info.id), file_info):
                    skipped += 1
                    continue

                if file_info.size and file_info.size > settings.max_upload_size:
                    raise FileTooLargeError(
                        f"File exceeds the {settings.max_upload_size}-byte upload limit"
                    )

                # Size-capped while streaming, in case the listing under-reported.
                # A 429 is waited out as the provider asks (bounded), not failed.
                content = await call_with_retry_after(
                    lambda file_id=file_info.id: connector.download_file(file_id)
                )

                # Same allowlist + magic-byte validation as the direct upload route.
                filename, content_type = validate_import_file(
                    content, file_info.mime_type, file_info.name
                )

                # Index it (user_id ensures proper document scoping)
                doc = await document_service.upload_and_index(
                    file_content=content,
                    filename=filename,
                    content_type=content_type,
                    source=connector_type,
                    source_id=file_info.id,
                    metadata={**(file_info.metadata or {}), **_source_marker(file_info)},
                    user_id=user_id,
                    user_keys=user_keys,
                )
            except _PROVIDER_ERRORS as e:
                logger.warning(f"Sync: Error processing {display_name}: {_describe_error(e)}")
                errors.append({"name": display_name, "reason": _describe_error(e)})
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise ConnectorError(
                        f"Sync stopped after {consecutive_failures} consecutive failures "
                        f"(last: {_describe_error(e)})"
                    )
                continue

            consecutive_failures = 0

            if doc.status == DocumentStatus.FAILED:
                reason = doc.metadata.get("error") or "could not be indexed"
                errors.append({"name": display_name, "reason": reason})
                continue

            processed += 1
            await audit_service.log_event(
                event_type=AuditEventType.DOCUMENT_UPLOAD,
                user_id=user_id,
                user_email=user_email,
                resource_type="document",
                resource_id=doc.id,
                details={
                    "via": "connector_sync",
                    "source": connector_type.value,
                    "source_id": file_info.id,
                    "size": doc.size,
                },
            )

        # Update connector's last sync time
        connector.credentials["last_sync"] = datetime.now(UTC).isoformat()
        connector._save_credentials()

        # Log successful completion
        await audit_service.log_event(
            event_type=AuditEventType.CONNECTOR_SYNC_COMPLETE,
            user_id=user_id,
            user_email=user_email,
            resource_type="connector",
            resource_id=connector_type.value,
            details={
                "folder_id": folder_id,
                "docs_processed": processed,
                "docs_skipped_unchanged": skipped,
                "errors_count": len(errors),
            },
        )
        logger.info(
            f"Sync: Completed. Processed {processed} files, {skipped} unchanged, "
            f"{len(errors)} errors"
        )

        return {
            "status": "completed",
            "docs_processed": processed,
            "docs_skipped": skipped,
            "docs_total": len(files),
            "errors_count": len(errors),
            # First 20 {"name", "reason"} entries; errors_count is the full tally.
            "errors": errors[:MAX_REPORTED_ERRORS],
        }

    except Exception as e:
        # Every failure is audited before the job manager sees it — including
        # provider and SDK errors outside the usual tuple.
        reason = _describe_error(e)
        logger.error(f"Sync: Failed with error: {reason}")

        await audit_service.log_event(
            event_type=AuditEventType.CONNECTOR_SYNC_FAILURE,
            user_id=user_id,
            user_email=user_email,
            resource_type="connector",
            resource_id=connector_type.value,
            details={
                "error": reason,
                "folder_id": folder_id,
                "docs_processed": processed,
                "errors_count": len(errors),
            },
            success=False,
            error=reason,
        )
        raise
