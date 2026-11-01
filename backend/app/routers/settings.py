import logging

from fastapi import APIRouter, Depends, HTTPException

from app.exceptions import UpstreamError
from app.models.responses.settings import (
    ClearResponse,
    EmbeddingInfoResponse,
    InstanceModelsResponse,
    PromptDefaultsResponse,
)
from app.models.schemas import RAGSettings
from app.services import llm_clients
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData, get_current_user
from app.services.permissions import require_permission
from app.services.user_settings import load_user_settings, save_user_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/rag", response_model=RAGSettings)
async def get_rag_settings(current_user: TokenData = Depends(get_current_user)) -> RAGSettings:
    """Get current user's RAG settings. Requires authentication."""
    return load_user_settings(current_user.user_id)


@router.put("/rag", response_model=RAGSettings)
async def update_rag_settings(
    new_settings: RAGSettings, current_user: TokenData = Depends(get_current_user)
) -> RAGSettings:
    """Update RAG settings for the current user. Requires authentication.

    The request is MERGED into the stored settings: a field the body omits
    keeps its stored value, and an explicit ``null`` clears it. A client that
    sends only the fields it changed can therefore never blank the playbook,
    practice profile or custom prompts by accident.
    """
    changes = new_settings.model_dump(exclude_unset=True)
    merged = RAGSettings(**{**load_user_settings(current_user.user_id).model_dump(), **changes})

    # Field names only: the values include the firm's playbook, practice profile
    # and custom prompts, which do not belong in the audit trail.
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="rag_settings",
        details={"changed_fields": sorted(changes)},
    )

    save_user_settings(current_user.user_id, merged)
    return merged


@router.get("/models", response_model=InstanceModelsResponse)
async def get_instance_models(
    current_user: TokenData = Depends(get_current_user),
) -> InstanceModelsResponse:
    """The models this instance is set up with, so the model picker can offer
    the instance default (an admin's local model, when one is configured)."""
    return InstanceModelsResponse(
        custom_endpoint=llm_clients.is_custom_endpoint(),
        chat_model=llm_clients.chat_model(),
        utility_model=llm_clients.utility_model(),
        research_model=llm_clients.research_model(),
        models=llm_clients.model_names(),
    )


@router.get("/prompts/defaults", response_model=PromptDefaultsResponse)
async def get_default_prompts(current_user: TokenData = Depends(get_current_user)) -> dict:
    """Get all default prompts for reset/customization functionality. Requires authentication."""
    from app.services.rag.prompts import (
        get_default_factual_prompt,
        get_default_grounding_rules,
        get_default_mode_prompt,
        get_default_system_prompt,
    )

    return {
        "system_prompt": get_default_system_prompt(),
        "grounding_rules": get_default_grounding_rules(),
        "factual_prompt": get_default_factual_prompt(),
        "mode_prompts": {
            mode: get_default_mode_prompt(mode)
            for mode in ("research", "case", "document", "compliance", "strategy")
        },
    }


@router.get("/embeddings", response_model=EmbeddingInfoResponse)
async def get_embedding_info(
    current_user: TokenData = Depends(get_current_user),
) -> EmbeddingInfoResponse:
    """Which embedding model indexes documents, and whether re-embedding is running."""
    from app.services.embedding_switch import reindex_status
    from app.services.embeddings import embedding_service

    status = reindex_status()
    try:
        provider = embedding_service.resolve_route(None).provider
    except Exception:  # status display only
        provider = "unknown"
    return EmbeddingInfoResponse(
        source=embedding_service.source,
        provider=provider,
        model=embedding_service.model,
        dimensions=embedding_service.dimensions,
        reindex_state=status["state"],
        reindex_done=status["done"],
        reindex_total=status["total"],
    )


@router.post("/reindex")
async def trigger_reindex(
    current_user: TokenData = require_permission("documents.upload"),
) -> dict:
    """Trigger a reindex of the current user's documents.

    Acts only on the caller's own documents, so it is gated by the same
    permission as uploading them — not an instance-admin permission.
    """
    from app.services.diagnostics import reindex_user_documents

    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="index",
        details={"action": "reindex_started"},
    )

    return await reindex_user_documents(current_user.user_id)


@router.post("/clear", response_model=ClearResponse)
async def clear_index(current_user: TokenData = require_permission("documents.delete")) -> dict:
    """Clear the current user's documents and vectors.

    Acts only on the caller's own documents, so it is gated by the same
    permission as deleting them — not an instance-admin permission.
    """
    # Log this critical action
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="index",
        details={"action": "index_cleared", "warning": "User documents deleted"},
    )

    from app.services.documents import document_service

    try:
        result = await document_service.clear_all(current_user.user_id)
    except (ValueError, KeyError, OSError) as e:
        logger.error(f"Failed to clear index: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to clear index. Check server logs for details.",
        )

    cleared = result.get("cleared_documents", 0)
    failed = result.get("failed_documents", 0)
    if failed and not cleared:
        # Nothing could be removed: that is a failure, not a "cleared" response.
        raise UpstreamError(
            f"None of your {failed} document(s) could be removed from the search "
            "index. Nothing was deleted; try again."
        )
    if failed:
        return {
            "status": "partial",
            "message": (
                f"Removed {cleared} document(s). {failed} could not be removed from the "
                "search index and were kept; try again."
            ),
            "cleared_documents": cleared,
            "failed_documents": failed,
        }
    return {
        "status": "cleared",
        "message": "Index cleared successfully. All your documents have been removed.",
        "cleared_documents": cleared,
        "failed_documents": 0,
    }
