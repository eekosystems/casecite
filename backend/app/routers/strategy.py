"""
Strategy brief router.

Matter-level strategy synthesis over the user's documents with guaranteed
per-document coverage — unlike chat's top-k retrieval, every document in scope
contributes at least one passage. Consumed by the chat-style frontend.
"""

import logging

from fastapi import APIRouter, Depends, Request

from app.exceptions import BadRequestError, NotFoundError, ServiceUnavailableError, UpstreamError
from app.models.schemas import StrategyBriefRequest
from app.routers._matter_deps import accessible_matter_ids
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.permissions import require_permission
from app.services.strategy import (
    StrategyServiceUnavailable,
    StrategySynthesisError,
    strategy_service,
)
from app.services.user_keys import UserAPIKeys
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/strategy", tags=["strategy"])


@router.post("/brief")
async def strategy_brief(
    request: StrategyBriefRequest,
    http_request: Request,
    current_user: TokenData = require_permission("chat.use"),
    accessible_matters: set[str] = Depends(accessible_matter_ids),
) -> dict:
    """Synthesize a strategy brief with guaranteed per-document coverage."""
    user_keys = UserAPIKeys.from_request(http_request, user_id=current_user.user_id)

    try:
        result = await strategy_service.generate_brief(
            question=request.question,
            folder_path=request.folder_path,
            document_ids=request.document_ids,
            include_case_law=request.include_case_law,
            user_id=current_user.user_id,
            user_keys=user_keys,
            accessible_matter_ids=accessible_matters,
        )
    except StrategyServiceUnavailable as e:
        raise ServiceUnavailableError(str(e))
    except StrategySynthesisError as e:
        raise UpstreamError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))

    await audit_service.log_event(
        event_type=AuditEventType.DATA_ACCESS,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="strategy",
        ip_address=get_client_ip(http_request),
        details={
            "action": "strategy_brief",
            "documents_considered": result["scope"]["documents_considered"],
            "documents_total": result["scope"]["documents_total"],
            "include_case_law": request.include_case_law,
        },
    )
    return result
