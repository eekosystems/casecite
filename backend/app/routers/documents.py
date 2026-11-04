import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UpstreamError,
)
from app.models.responses.documents import (
    DocumentContentResponse,
    DocumentDeleteResponse,
    FailedDeletionItem,
    FailedDeletionsResponse,
)
from app.models.schemas import (
    ConnectorType,
    CreateFolderRequest,
    Document,
    DocumentList,
    DocumentStatus,
    DocumentTreeResponse,
    MoveDocumentRequest,
    SetLegalHoldRequest,
)
from app.routers._matter_deps import accessible_matter_ids as _accessible_matter_ids
from app.routers._matter_deps import owned_matter_ids as _owned_matter_ids
from app.services import matters as matters_service
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.documents import (
    DELETION_STEPS,
    DocumentDeletionError,
    DocumentOnHoldError,
    document_service,
)
from app.services.permissions import require_permission
from app.services.user_keys import UserAPIKeys
from app.utils.error_handler import handle_service_error
from app.utils.file_crypto import FileDecryptionError
from app.utils.http_headers import content_disposition
from app.utils.ip_resolution import get_client_ip

# Validation config and helpers live in app/utils/upload_validation.py so the
# cloud picker imports apply the same checks as this direct upload route.
from app.utils.upload_validation import (
    ALLOWED_MIME_TYPES,
    EXTENSION_TO_MIME,
    read_upload_capped,
    sanitize_filename,
    validate_file_content,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("", response_model=Document)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    matter_id: str | None = Form(default=None),
    current_user: TokenData = require_permission("documents.upload"),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """Upload and index a document. Requires authentication.

    Optional matter_id files the document under a matter the caller belongs to,
    making it visible to that matter's members. Omitted -> the caller's personal
    (owner-only) scope, i.e. the historical behavior.
    """
    # Reject filing into a matter the caller is not a member of (no cross-matter
    # writes). None stays personal/owner-only.
    if matter_id and not await matters_service.is_member(db, matter_id, current_user.user_id):
        raise ForbiddenError("You are not a member of the specified matter.")

    user_keys = UserAPIKeys.from_request(request, user_id=current_user.user_id)
    safe_filename = sanitize_filename(file.filename)

    # Chunked read with a hard cap (rejects early on Content-Length; never
    # buffers an oversized body into memory).
    content = await read_upload_capped(file, settings.max_upload_size, request=request)

    content_type = file.content_type
    if content_type not in ALLOWED_MIME_TYPES:
        ext = safe_filename.lower().rsplit(".", 1)[-1] if "." in safe_filename else ""
        content_type = EXTENSION_TO_MIME.get(ext, content_type)

    if content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {content_type}. "
            "Allowed: PDF, Word, Excel, PowerPoint, ODT, TXT, Markdown, CSV, TSV, "
            "HTML, JSON, EML, MSG, RTF",
        )

    is_valid, detected_type, error_msg = validate_file_content(content, content_type, safe_filename)
    if not is_valid:
        await audit_service.log_event(
            event_type=AuditEventType.SECURITY_ALERT,
            user_id=current_user.user_id,
            user_email=current_user.email,
            ip_address=get_client_ip(request),
            details={
                "action": "file_type_mismatch",
                "filename": safe_filename,
                "claimed_type": content_type,
                "detected_type": detected_type,
            },
            success=False,
        )
        raise HTTPException(
            status_code=415,
            detail="File content does not match file type. Please upload a valid file.",
        )

    type_config = ALLOWED_MIME_TYPES.get(content_type, {})
    max_size = type_config.get("max_size", settings.max_upload_size)
    if len(content) > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"File too large for this type. Maximum size for {content_type} is {max_size / 1024 / 1024}MB",
        )

    try:
        doc = await document_service.upload_and_index(
            file_content=content,
            filename=safe_filename,
            content_type=content_type,
            user_id=current_user.user_id,
            user_keys=user_keys,
            matter_id=matter_id,
        )
    except (HTTPException, AppError):
        raise
    except Exception as e:  # surface a clean error, never a bare 500
        raise handle_service_error(e, "Failed to process document", logger)

    await audit_service.log_event(
        event_type=AuditEventType.DOCUMENT_UPLOAD,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="document",
        resource_id=doc.id,
        ip_address=get_client_ip(request),
        details={
            "source": "upload",
            "content_type": content_type,
            "size": len(content),
            "matter_id": matter_id,
        },
    )
    return doc


@router.get("", response_model=DocumentList)
async def list_documents(
    source: ConnectorType | None = None,
    status: DocumentStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: TokenData = require_permission("documents.view"),
    accessible_matters: set[str] = Depends(_accessible_matter_ids),
) -> DocumentList:
    """List documents for the current user (own + shared matters). Requires auth."""
    docs = await document_service.list_documents(
        user_id=current_user.user_id,
        source=source,
        status=status,
        limit=limit,
        offset=offset,
        accessible_matter_ids=accessible_matters,
    )
    # ``total`` is the full match count across pages (what the pager needs);
    # ``count`` stays the page length for compatibility.
    total = await document_service.count_documents(
        user_id=current_user.user_id,
        source=source,
        status=status,
        accessible_matter_ids=accessible_matters,
    )
    return DocumentList(
        documents=docs,
        count=len(docs),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/tree", response_model=DocumentTreeResponse)
async def get_document_tree(
    current_user: TokenData = require_permission("documents.view"),
) -> DocumentTreeResponse:
    """Get document tree structure for filtering UI. Requires authentication."""
    tree = await document_service.get_document_tree(current_user.user_id)
    return tree


# ==================== Knowledge-base folders ====================
# NOTE: literal /folders routes MUST stay above the /{document_id} catch-alls.


@router.get("/folders")
async def list_folders(current_user: TokenData = require_permission("documents.view")) -> dict:
    """List the current user's knowledge-base folders."""
    return {"folders": document_service.list_folders(current_user.user_id)}


@router.post("/folders")
async def create_folder(
    body: CreateFolderRequest, current_user: TokenData = require_permission("documents.upload")
) -> dict:
    """Create a knowledge-base folder."""
    try:
        path = document_service.create_folder(current_user.user_id, body.name)
    except ValueError as e:
        raise BadRequestError(str(e))
    return {"path": path, "folders": document_service.list_folders(current_user.user_id)}


@router.delete("/folders")
async def delete_folder(
    request: Request,
    path: str = Query(..., description="Folder path to delete", max_length=500),
    current_user: TokenData = require_permission("documents.delete"),
) -> dict:
    """Delete a folder; its documents move back to General (root)."""
    try:
        document_service.delete_folder(current_user.user_id, path)
    except ValueError as e:
        raise BadRequestError(str(e))
    # Folder names are client-identifying, so the entry records the action only.
    await audit_service.log_event(
        event_type=AuditEventType.DATA_MODIFICATION,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="folder",
        ip_address=get_client_ip(request),
        details={"action": "folder_deleted"},
    )
    return {"status": "deleted", "folders": document_service.list_folders(current_user.user_id)}


# ── Failed deletions (literal path BEFORE the /{document_id} routes) ──


_STEP_LABELS = {
    "vectors": "search index entries",
    "derived": "analyses derived from it",
    "file": "stored file",
}


def _deletion_failure_detail(step: str | None) -> str:
    """Client-facing text for a delete that stopped at ``step``. Built here,
    never from the exception text, which may name internal paths."""
    what = _STEP_LABELS.get(step or "", "content")
    return (
        f"Could not remove the document's {what}; it was not deleted. It stays listed "
        "as a failed deletion and the next delete resumes from that step. Try again."
    )


def _failed_deletion_item(doc: Document) -> FailedDeletionItem:
    info = (doc.metadata or {}).get("deletion") or {}
    return FailedDeletionItem(
        id=doc.id,
        user_id=doc.user_id,
        matter_id=doc.matter_id,
        filename=doc.filename,
        failed_step=info.get("failed_step"),
        error=info.get("error"),
        failed_at=info.get("failed_at"),
        attempts=int(info.get("attempts") or 0),
        removed=document_service.deletion_progress(doc),
    )


@router.get("/failed-deletions", response_model=FailedDeletionsResponse)
async def list_failed_deletions(
    current_user: TokenData = require_permission("admin.settings"),
) -> FailedDeletionsResponse:
    """Every document, any owner, whose delete did not complete.

    An operator's view: a delete that stops at the search index, the derived
    rows or the stored file leaves the document listed here with the step
    that failed and the error, until a retry completes.
    """
    docs = sorted(document_service.failed_deletions(), key=lambda d: d.created_at)
    items = [_failed_deletion_item(d) for d in docs]
    return FailedDeletionsResponse(documents=items, count=len(items))


@router.post("/{document_id}/retry-delete", response_model=DocumentDeleteResponse)
async def retry_failed_deletion(
    document_id: str,
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
) -> DocumentDeleteResponse:
    """Resume a failed delete, whoever owns the document. Resumes at the step
    that failed; the document is removed from the list only when every step
    has completed."""
    try:
        ok = await document_service.retry_failed_deletion(document_id)
    except DocumentOnHoldError as e:
        raise ConflictError(str(e))
    except DocumentDeletionError as e:
        await audit_service.log_event(
            event_type=AuditEventType.DOCUMENT_DELETE,
            user_id=current_user.user_id,
            user_email=current_user.email,
            resource_type="document",
            resource_id=document_id,
            ip_address=get_client_ip(request),
            details={"action": "document_delete_retry_failed"},
            success=False,
            error=str(e),
        )
        retried = document_service.documents.get(document_id)
        info = ((retried.metadata if retried else None) or {}).get("deletion") or {}
        raise UpstreamError(_deletion_failure_detail(info.get("failed_step")))
    if not ok:
        raise NotFoundError("No failed deletion with that id")
    await audit_service.log_event(
        event_type=AuditEventType.DOCUMENT_DELETE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="document",
        resource_id=document_id,
        ip_address=get_client_ip(request),
        details={"action": "document_delete_retried"},
    )
    return DocumentDeleteResponse(
        status="deleted", id=document_id, removed=dict.fromkeys(DELETION_STEPS, True)
    )


@router.post("/{document_id}/hold", response_model=Document)
async def set_document_hold(
    document_id: str,
    body: SetLegalHoldRequest,
    request: Request,
    current_user: TokenData = require_permission("documents.delete"),
    owned_matters: set[str] = Depends(_owned_matter_ids),
) -> Document:
    """Place or lift a legal hold on one document (its owner, or the owner of
    the matter it is filed under, or an admin). While held, the document
    cannot be deleted through any path — including a purge of its owner's
    account and replacement by re-upload — until the hold is lifted."""
    from app.services.permissions import effective_permissions

    doc = document_service.documents.get(document_id)
    perms = await effective_permissions(current_user.roles or [])
    if doc is None or not (
        document_service._can_access(doc, current_user.user_id, owned_matters)
        or "admin.users" in perms
    ):
        raise NotFoundError("Document not found")
    updated = document_service.set_document_hold(
        document_id, on=body.on, reason=body.reason, set_by=current_user.user_id
    )
    await audit_service.log_event(
        event_type=AuditEventType.DATA_MODIFICATION,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="document",
        resource_id=document_id,
        ip_address=get_client_ip(request),
        details={"action": "legal_hold_set" if body.on else "legal_hold_lifted"},
    )
    return updated


@router.post("/{document_id}/move")
async def move_document(
    document_id: str,
    body: MoveDocumentRequest,
    request: Request,
    current_user: TokenData = require_permission("documents.upload"),
) -> dict:
    """Move a document into a folder (null/empty folder_path = General/root)."""
    try:
        ok = document_service.move_document(document_id, current_user.user_id, body.folder_path)
    except ValueError as e:
        raise BadRequestError(str(e))
    if not ok:
        raise NotFoundError("Document not found")
    await audit_service.log_event(
        event_type=AuditEventType.DATA_MODIFICATION,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="document",
        resource_id=document_id,
        ip_address=get_client_ip(request),
        details={"action": "document_moved"},
    )
    return {"status": "moved", "folder_path": body.folder_path or None}


@router.get("/{document_id}/content", response_model=DocumentContentResponse)
async def get_document_content(
    document_id: str,
    request: Request,
    current_user: TokenData = require_permission("documents.view"),
    accessible_matters: set[str] = Depends(_accessible_matter_ids),
) -> DocumentContentResponse:
    """Get the extracted text content of a document. Requires authentication."""
    import os

    doc = await document_service.get_document(
        document_id, current_user.user_id, accessible_matter_ids=accessible_matters
    )
    if not doc:
        raise NotFoundError("Document not found")

    await audit_service.log_event(
        event_type=AuditEventType.DOCUMENT_VIEW,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="document",
        resource_id=document_id,
        ip_address=get_client_ip(request),
    )

    # Resolve the file under the OWNER's dir (doc.user_id) so a member reading a
    # shared document finds the owner's stored file.
    file_ext = os.path.splitext(doc.filename)[1]
    user_file_path = os.path.join(
        document_service._get_user_upload_dir(doc.user_id),
        f"{document_id}{file_ext}",
    )
    legacy_file_path = os.path.join(settings.upload_dir, f"{document_id}{file_ext}")

    file_path = None
    if os.path.exists(user_file_path):
        file_path = user_file_path
    elif os.path.exists(legacy_file_path):
        file_path = legacy_file_path

    if not file_path:
        raise NotFoundError(
            "Document file not found on disk",
        )

    try:
        text = await document_service.extract_text(file_path, doc.content_type)
    except (ValueError, KeyError, OSError) as e:
        raise handle_service_error(e, "Failed to extract text from document", logger)

    return {
        "document_id": document_id,
        "text": text,
        "filename": doc.filename,
    }


# Content types that are safe to render inline in the browser. Anything else is
# served as a download so a document whose content-type is text/html (e.g. an
# imported file) can't execute script in the app origin (stored-XSS defense).
_INLINE_SAFE_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
}


@router.get("/{document_id}/file")
async def get_document_file(
    document_id: str,
    request: Request,
    current_user: TokenData = require_permission("documents.view"),
    accessible_matters: set[str] = Depends(_accessible_matter_ids),
) -> Response:
    """Serve the original uploaded file as binary for visual rendering."""
    import os

    doc = await document_service.get_document(
        document_id, current_user.user_id, accessible_matter_ids=accessible_matters
    )
    if not doc:
        raise NotFoundError("Document not found")

    # Resolve the file under the OWNER's dir (doc.user_id) so a shared document
    # is served from the owner's storage.
    file_ext = os.path.splitext(doc.filename)[1]
    user_file_path = os.path.join(
        document_service._get_user_upload_dir(doc.user_id),
        f"{document_id}{file_ext}",
    )
    legacy_file_path = os.path.join(settings.upload_dir, f"{document_id}{file_ext}")

    file_path = None
    if os.path.exists(user_file_path):
        file_path = user_file_path
    elif os.path.exists(legacy_file_path):
        file_path = legacy_file_path

    if not file_path:
        raise NotFoundError("Document file not found on disk")

    try:
        # Decrypts the at-rest encryption transparently (legacy plaintext passes through).
        file_bytes = await document_service.read_file(file_path)
    except (OSError, FileDecryptionError) as e:
        logger.error(f"Failed to read file {file_path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to read document file")

    await audit_service.log_event(
        event_type=AuditEventType.DOCUMENT_DOWNLOAD,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="document",
        resource_id=document_id,
        ip_address=get_client_ip(request),
    )

    # Only render inline for a strict allowlist of safe types; everything else is
    # forced to download with a neutral type so an attacker-chosen content-type
    # (e.g. text/html from an import) can't run script in the app origin.
    declared_type = doc.content_type or "application/octet-stream"
    if declared_type in _INLINE_SAFE_CONTENT_TYPES:
        media_type = declared_type
        disposition = "inline"
    else:
        media_type = "application/octet-stream"
        disposition = "attachment"

    return Response(
        content=file_bytes,
        media_type=media_type,
        headers={
            # ASCII fallback + RFC 5987 filename* — a raw non-Latin-1 name
            # (en dash, curly quotes, CJK) would fail header encoding with a 500.
            "Content-Disposition": content_disposition(disposition, doc.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{document_id}", response_model=Document)
async def get_document(
    document_id: str,
    current_user: TokenData = require_permission("documents.view"),
    accessible_matters: set[str] = Depends(_accessible_matter_ids),
) -> Document:
    """Get a specific document (own or shared via a matter). Requires auth."""
    doc = await document_service.get_document(
        document_id, current_user.user_id, accessible_matter_ids=accessible_matters
    )
    if not doc:
        raise NotFoundError("Document not found")
    return doc


@router.delete("/{document_id}", response_model=DocumentDeleteResponse)
async def delete_document(
    document_id: str,
    request: Request,
    current_user: TokenData = require_permission("documents.delete"),
    owned_matters: set[str] = Depends(_owned_matter_ids),
) -> DocumentDeleteResponse:
    """Delete a document. Requires authentication.

    Allowed for the document's owner and for the OWNER of the matter it is
    filed under. Plain matter members can read a shared document but not
    delete a colleague's work (404, same as a document they cannot see).
    """
    try:
        success = await document_service.delete_document(
            document_id, current_user.user_id, accessible_matter_ids=owned_matters
        )
        if not success:
            raise NotFoundError("Document not found")
    except (HTTPException, AppError):
        raise
    except DocumentOnHoldError as e:
        raise ConflictError(str(e))
    except DocumentDeletionError as e:
        # The search index (or the analyses derived from the document) could not
        # be cleaned up, so the service left the document in place. Say so
        # plainly: a generic 500 would read as "maybe deleted".
        logger.error(f"Document {document_id} could not be deleted: {e}")
        doc = document_service.documents.get(document_id)
        info = ((doc.metadata if doc else None) or {}).get("deletion") or {}
        await audit_service.log_event(
            event_type=AuditEventType.DOCUMENT_DELETE,
            user_id=current_user.user_id,
            user_email=current_user.email,
            resource_type="document",
            resource_id=document_id,
            ip_address=get_client_ip(request),
            details={
                "action": "document_delete_failed",
                "failed_step": info.get("failed_step"),
                "removed": document_service.deletion_progress(doc) if doc else None,
            },
            success=False,
            error=f"{info.get('failed_step') or 'deletion'} step failed",
        )
        raise UpstreamError(_deletion_failure_detail(info.get("failed_step")))
    except (ValueError, KeyError, OSError) as e:
        raise handle_service_error(e, "Failed to delete document", logger)

    try:
        await audit_service.log_event(
            event_type=AuditEventType.DOCUMENT_DELETE,
            user_id=current_user.user_id,
            user_email=current_user.email,
            resource_type="document",
            resource_id=document_id,
            ip_address=get_client_ip(request),
            details={"action": "document_deleted"},
        )
    except (ValueError, KeyError, OSError) as e:
        logger.error(f"Audit log error for document delete {document_id}: {e}")

    return {"status": "deleted", "id": document_id, "removed": dict.fromkeys(DELETION_STEPS, True)}
