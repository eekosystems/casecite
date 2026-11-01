"""
Admin integrations router.

Instance-wide integration credentials (currently the CourtListener / Free Law
Project API token) editable from the UI instead of only via .env. Admin-only.
The token is persisted AES-256-GCM encrypted and applied to the running service
immediately (no restart). Resolution order: instance token (DB) -> .env -> anonymous.
"""

import json
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.exceptions import BadRequestError, NotFoundError
from app.services import connector_credentials, llm_clients
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.courtlistener import apply_courtlistener_token
from app.services.instance_settings import delete_secret, get_secret, set_secret
from app.services.permissions import require_permission
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/integrations", tags=["admin-integrations"])

CL_SECRET = "courtlistener_api_token"


class CourtListenerTokenRequest(BaseModel):
    api_token: str = Field(..., min_length=10, max_length=200)


def _mask(token: str | None) -> str | None:
    if not token:
        return None
    if len(token) < 10:
        return "•" * 8
    return token[:4] + "•" * 8 + token[-4:]


async def _courtlistener_status() -> dict:
    """configured / source / masked — the same shape from GET, POST and DELETE,
    so the UI can render the saved state straight from the save response."""
    instance_token = await get_secret(CL_SECRET)
    env_token = (settings.courtlistener_api_token or "").strip() or None
    if instance_token:
        return {"configured": True, "source": "instance", "masked": _mask(instance_token)}
    if env_token:
        return {"configured": True, "source": "env", "masked": _mask(env_token)}
    return {"configured": False, "source": "none", "masked": None}


@router.get("/courtlistener")
async def courtlistener_status(current_user: TokenData = require_permission("admin.settings")):
    """Report whether a CourtListener token is configured, and where it comes from."""
    return await _courtlistener_status()


@router.post("/courtlistener")
async def set_courtlistener_token(
    body: CourtListenerTokenRequest,
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
):
    """Save an instance-wide CourtListener token and apply it immediately."""
    token = body.api_token.strip()
    await set_secret(CL_SECRET, token)
    apply_courtlistener_token(token)
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="integration",
        resource_id="courtlistener",
        ip_address=get_client_ip(request),
        details={"action": "courtlistener_token_set"},
    )
    return {"status": "saved", "configured": True, "source": "instance", "masked": _mask(token)}


@router.delete("/courtlistener")
async def clear_courtlistener_token(
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
):
    """Clear the instance token and revert the running service to the .env fallback."""
    await delete_secret(CL_SECRET)
    apply_courtlistener_token((settings.courtlistener_api_token or "").strip() or None)
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="integration",
        resource_id="courtlistener",
        ip_address=get_client_ip(request),
        details={"action": "courtlistener_token_cleared"},
    )
    # After clearing, the effective state is whatever .env provides (or none).
    return {"status": "cleared", **(await _courtlistener_status())}


# ----- Connector (file picker) credentials: Google Drive / OneDrive+SharePoint / Box / Dropbox -----
# Same dual model as the CourtListener token: the open-source install sets
# .env variables; desktop / private installs save them here (encrypted,
# instance-wide, applied immediately). Instance values beat .env.


class ConnectorCredentialsRequest(BaseModel):
    values: dict[str, str] = Field(
        ..., max_length=20, description="settings-field -> value for this provider"
    )

    @field_validator("values")
    @classmethod
    def _bounded_values(cls, v: dict[str, str]) -> dict[str, str]:
        if any(len(k) > 100 or len(val) > 2000 for k, val in v.items()):
            raise ValueError("a credential value is too long")
        return v


@router.get("/connectors")
async def connector_credentials_status(
    current_user: TokenData = require_permission("admin.settings"),
):
    """Per-provider status: configured, from where, masked values."""
    providers = [
        await connector_credentials.provider_status(p)
        for p in connector_credentials.CONNECTOR_PROVIDERS
    ]
    return {"providers": providers}


@router.post("/connectors/{provider}")
async def set_connector_credentials(
    provider: str,
    body: ConnectorCredentialsRequest,
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
):
    """Save a provider's credentials instance-wide and apply them immediately."""
    meta = connector_credentials.CONNECTOR_PROVIDERS.get(provider)
    if not meta:
        raise NotFoundError("Unknown connector provider")
    allowed = set(meta["fields"])
    provided = {k: (v or "").strip() for k, v in body.values.items() if k in allowed}
    if not provided:
        raise BadRequestError("No credential values provided")
    # Merge into what is already stored: a field the request leaves out keeps
    # its current value (the UI only sends the fields the admin edited); a
    # field sent as an empty string is cleared and falls back to .env.
    values = await connector_credentials.stored_values(provider)
    for field, value in provided.items():
        if value:
            values[field] = value
        else:
            values.pop(field, None)
    if values:
        await set_secret(connector_credentials.secret_name(provider), json.dumps(values))
    else:
        await delete_secret(connector_credentials.secret_name(provider))
    connector_credentials.apply_values(provider, values)
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="integration",
        resource_id=f"connector_{provider}",
        ip_address=get_client_ip(request),
        details={
            "action": "connector_credentials_set",
            "fields": sorted(k for k, v in provided.items() if v),
            "cleared": sorted(k for k, v in provided.items() if not v),
        },
    )
    return await connector_credentials.provider_status(provider)


@router.delete("/connectors/{provider}")
async def clear_connector_credentials(
    provider: str,
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
):
    """Clear the instance credentials and revert the provider to its .env values."""
    if provider not in connector_credentials.CONNECTOR_PROVIDERS:
        raise NotFoundError("Unknown connector provider")
    await delete_secret(connector_credentials.secret_name(provider))
    connector_credentials.revert_to_env(provider)
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="integration",
        resource_id=f"connector_{provider}",
        ip_address=get_client_ip(request),
        details={"action": "connector_credentials_cleared"},
    )
    return await connector_credentials.provider_status(provider)


# ----- Custom / local OpenAI-compatible model endpoint (Ollama, vLLM, OpenRouter, ...) -----

LLM_SECRET = "local_llm_config"


class LocalLLMRequest(BaseModel):
    base_url: str = Field(..., min_length=4, max_length=400)  # e.g. http://localhost:11434/v1
    chat_model: str = Field(..., min_length=1, max_length=120)
    utility_model: str | None = Field(None, max_length=120)
    # Deep research. Blank: follows the chat model.
    research_model: str | None = Field(None, max_length=120)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip()
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v


def _embeddings_provider() -> str:
    """Where document text goes for embeddings. The model endpoint above does not
    cover embeddings (EMBEDDING_BASE_URL does), so the UI can warn when chat is
    local but embeddings still go to a hosted provider."""
    from app.services.embeddings import embedding_service

    try:
        return embedding_service.resolve_route(None).provider
    except Exception as e:  # status display must never fail on this
        logger.warning("Could not resolve the embeddings provider: %s", e)
        return "unknown"


def _local_llm_status() -> dict:
    """Effective endpoint config — one shape for GET, POST and DELETE."""
    cfg = llm_clients.get_runtime()
    return {
        "configured": bool(cfg.get("base_url")),
        "base_url": cfg.get("base_url"),
        "chat_model": cfg.get("chat_model"),
        "utility_model": cfg.get("utility_model"),
        "research_model": cfg.get("research_model"),
        "embeddings_provider": _embeddings_provider(),
    }


@router.get("/local-llm")
async def local_llm_status(current_user: TokenData = require_permission("admin.settings")):
    """Current effective model endpoint config (custom endpoint or OpenAI default)."""
    return _local_llm_status()


@router.post("/local-llm")
async def set_local_llm(
    body: LocalLLMRequest,
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
):
    """Point the instance at a custom OpenAI-compatible endpoint and apply immediately."""
    payload = {
        "base_url": body.base_url.strip(),
        "chat_model": body.chat_model.strip(),
        "utility_model": (body.utility_model or "").strip() or body.chat_model.strip(),
        # "" (not None) so a cleared field is stored as cleared and survives a restart.
        "research_model": (body.research_model or "").strip(),
    }
    await set_secret(LLM_SECRET, json.dumps(payload))
    llm_clients.set_runtime(**payload)
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="integration",
        resource_id="local_llm",
        ip_address=get_client_ip(request),
        details={"action": "local_llm_set", "base_url": payload["base_url"]},
    )
    # Same fields as GET /local-llm (configured + the applied values), so the
    # UI shows "configured" right after saving instead of "not configured".
    return {
        "status": "saved",
        "configured": True,
        **payload,
        "research_model": payload["research_model"] or None,
        "embeddings_provider": _embeddings_provider(),
    }


@router.delete("/local-llm")
async def clear_local_llm(
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
):
    """Revert to the OpenAI defaults from settings (.env)."""
    await delete_secret(LLM_SECRET)
    llm_clients.set_runtime(
        # Back to the .env endpoint, not api.openai.com: an install that set
        # OPENAI_BASE_URL to keep document text on its own network must not
        # start sending it to OpenAI because an admin cleared a UI override.
        base_url=settings.openai_base_url or "",
        chat_model=settings.openai_chat_model,
        utility_model=settings.openai_utility_model,
        research_model=settings.openai_research_model or "",
    )
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="integration",
        resource_id="local_llm",
        ip_address=get_client_ip(request),
        details={"action": "local_llm_cleared"},
    )
    return {"status": "cleared", **_local_llm_status()}
