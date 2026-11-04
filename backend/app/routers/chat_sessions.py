"""Persistent chat session endpoints.

Thin HTTP layer over app.services.chat_sessions — all business logic and
tenant isolation live in the service. Literal routes are defined before the
parameterized /{session_id} catch-alls.
"""

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import ForbiddenError, NotFoundError
from app.models.schemas import (
    ChatSessionDetail,
    ChatSessionListResponse,
    ChatSessionSummary,
    CreateChatSessionRequest,
)
from app.routers._matter_deps import accessible_matter_ids, owned_matter_ids
from app.services import chat_sessions as chat_session_service
from app.services import matters as matters_service
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.permissions import require_permission
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat/sessions", tags=["chat-sessions"])


@router.get("", response_model=ChatSessionListResponse)
async def list_chat_sessions(
    limit: int = Query(50, ge=1, le=200),
    current_user: TokenData = require_permission("chat.use"),
    accessible_matters: set[str] = Depends(accessible_matter_ids),
) -> ChatSessionListResponse:
    """List the caller's chat sessions (own + shared matters), newest first."""
    sessions = await chat_session_service.list_sessions(
        current_user.user_id, limit=limit, accessible_matter_ids=accessible_matters
    )
    return {"sessions": sessions}


@router.post("", response_model=ChatSessionSummary)
async def create_chat_session(
    request: CreateChatSessionRequest,
    current_user: TokenData = require_permission("chat.use"),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionSummary:
    """Create a new (empty) chat session, optionally filed under a shared matter."""
    if request.matter_id and not await matters_service.is_member(
        db, request.matter_id, current_user.user_id
    ):
        raise ForbiddenError("You are not a member of the specified matter.")
    return await chat_session_service.create_session(
        current_user.user_id, request.title, matter_id=request.matter_id
    )


@router.get("/{session_id}", response_model=ChatSessionDetail)
async def get_chat_session(
    session_id: str,
    request: Request,
    current_user: TokenData = require_permission("chat.use"),
    accessible_matters: set[str] = Depends(accessible_matter_ids),
) -> ChatSessionDetail:
    """Return the full conversation thread for a session the caller may access."""
    session = await chat_session_service.get_session_with_messages(
        session_id, current_user.user_id, accessible_matter_ids=accessible_matters
    )
    if session is None:
        raise NotFoundError("Chat session not found")
    await audit_service.log_event(
        event_type=AuditEventType.DATA_ACCESS,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="chat_session",
        resource_id=session_id,
        ip_address=get_client_ip(request),
    )
    return session


@router.delete("/{session_id}")
async def delete_chat_session(
    session_id: str,
    request: Request,
    current_user: TokenData = require_permission("chat.use"),
    owned_matters: set[str] = Depends(owned_matter_ids),
) -> dict:
    """Delete a session and all its messages.

    Allowed for the session's owner and for the OWNER of the matter it is filed
    under; plain matter members can read a shared session but not delete it.
    """
    from app.exceptions import ConflictError
    from app.services.chat_sessions import ChatSessionOnHoldError

    try:
        deleted = await chat_session_service.delete_session(
            session_id, current_user.user_id, accessible_matter_ids=owned_matters
        )
    except ChatSessionOnHoldError as e:
        raise ConflictError(str(e))
    if not deleted:
        raise NotFoundError("Chat session not found")
    await audit_service.log_event(
        event_type=AuditEventType.DATA_DELETION,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="chat_session",
        resource_id=session_id,
        ip_address=get_client_ip(request),
    )
    return {"status": "deleted", "id": session_id}
