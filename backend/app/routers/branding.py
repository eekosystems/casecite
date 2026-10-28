"""
Branding / White-Label Router

Manage per-installation branding configuration (firm name, logo, colors, CSS).
The GET endpoints (config + uploaded logo) are public so the frontend can
style itself on load, before sign-in.
All mutation endpoints require an admin role (branding is installation-wide)
and are written to the audit trail.
Delegates all persistence to the branding service.
"""

import logging
import re

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.exceptions import BadRequestError, NotFoundError
from app.models.responses.branding import BrandingLogoResponse
from app.models.schemas import BrandingResponse, BrandingUpdate, DemoLoginInfo
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.branding import ASSET_MEDIA_TYPES, resolve_asset_path
from app.services.branding import (
    get_branding as _get_branding,
)
from app.services.branding import (
    reset_branding as _reset_branding,
)
from app.services.branding import (
    update_branding as _update_branding,
)
from app.services.branding import (
    upload_logo as _upload_logo,
)
from app.services.permissions import require_permission
from app.utils.error_handler import handle_service_error
from app.utils.ip_resolution import get_client_ip
from app.utils.upload_validation import read_upload_capped

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/branding", tags=["branding"])


def _sanitize_custom_css(css: str | None) -> str | None:
    """Neutralize XSS vectors in operator-supplied custom CSS.

    custom_css is injected into a <style> block in the frontend, so an attacker
    who could set it might try to break out of the CSS context (``</style>...``)
    or use CSS-based script execution. We strip angle brackets (which have no
    legitimate use in CSS and enable </style> breakout) and known dangerous
    constructs. External resource loads are removed too: this CSS is served
    unauthenticated to the login page, so a ``url(https://...)`` would make
    every visitor's browser call a third party (tracking / data exfiltration
    via attribute selectors). Only same-origin paths and inline raster
    ``data:`` images survive. This runs even though only admins can now set
    branding, as defense in depth against an admin account compromise or CSRF.
    """
    if not css:
        return css
    # Remove anything that could close the style element or open a new tag.
    cleaned = css.replace("<", "").replace(">", "")
    # Strip constructs that can execute script or load external resources from
    # within CSS. Deleting a match can splice a new one together (e.g.
    # "javajavascript:script:"), so repeat until a full pass removes nothing
    # (fixpoint) instead of a single pass.
    dangerous = (
        r"(?i)javascript:",
        r"(?i)expression\s*\(",
        r"(?i)@import\b",
        r"(?i)image-set\s*\(",  # takes bare-string URLs, bypassing url()
    )
    while True:
        before = cleaned
        for pattern in dangerous:
            cleaned = re.sub(pattern, "", cleaned)
        cleaned = _CSS_URL_RE.sub(_filter_css_url, cleaned)
        if cleaned == before:
            return cleaned


_CSS_URL_RE = re.compile(r"(?is)url\s*\(\s*(.*?)\s*\)")
_CSS_DATA_IMAGE_RE = re.compile(r"(?i)^data:image/(png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$")


def _filter_css_url(match: re.Match) -> str:
    """Keep a CSS url() only when it is same-origin or an inline raster image."""
    target = match.group(1).strip().strip("\"'").strip()
    same_origin = target.startswith("/") and not target.startswith("//")
    # A backslash is a CSS escape — "\68ttps://" decodes to "https://" — so any
    # escaped target is rejected rather than decoded.
    if "\\" not in target and (same_origin or _CSS_DATA_IMAGE_RE.match(target)):
        return match.group(0)
    return "none"


async def _audit_branding(request: Request, current_user: TokenData, action: str) -> None:
    await audit_service.log_event(
        event_type=AuditEventType.SETTINGS_CHANGE,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="branding",
        ip_address=get_client_ip(request),
        details={"action": action},
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.get("", response_model=BrandingResponse)
async def get_branding() -> BrandingResponse:
    """
    Get current branding configuration.

    This endpoint is **public** (no auth required) so the frontend can
    fetch brand assets on initial load before the user has logged in.
    Returns sensible defaults when no custom configuration has been saved.

    On a public demo instance (DEMO_LOGIN_*) the response also carries the
    demo account's credentials so the sign-in form can prefill them. They are
    public by design; the account is reset at every startup and cannot be
    changed by whoever signs in.
    """
    config = dict(await _get_branding())
    if settings.demo_login_enabled:
        config["demo_login"] = DemoLoginInfo(
            email=settings.demo_login_email, password=settings.demo_login_password
        )
    return BrandingResponse.model_validate(config)


@router.get("/assets/{filename}", include_in_schema=False)
async def get_branding_asset(filename: str) -> FileResponse:
    """Serve an uploaded logo. Public, like GET /branding (the login page shows it).

    Only server-generated ``brand_logo_<hex>.<ext>`` names resolve; anything
    else is a 404, so this cannot read other files off the data volume.
    """
    path = resolve_asset_path(filename)
    if path is None:
        raise NotFoundError("Not found")
    return FileResponse(
        path,
        media_type=ASSET_MEDIA_TYPES[path.suffix],
        headers={
            "X-Content-Type-Options": "nosniff",
            # An SVG opened directly is a document in the app origin; forbid
            # everything so it can only ever render as a static image.
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.put("", response_model=BrandingResponse)
async def update_branding(
    body: BrandingUpdate,
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
) -> BrandingResponse:
    """
    Update branding configuration.

    Admin only — branding is installation-wide, so a non-admin must not be able
    to reskin or inject CSS for every user. Fields that are omitted (None) remain
    unchanged.
    """
    try:
        result = await _update_branding(
            firm_name=body.firm_name,
            logo_url=body.logo_url,
            primary_color=body.primary_color,
            secondary_color=body.secondary_color,
            accent_color=body.accent_color,
            favicon_url=body.favicon_url,
            custom_css=_sanitize_custom_css(body.custom_css),
        )
    except (OSError, RuntimeError, ConnectionError, TimeoutError) as exc:
        raise handle_service_error(exc, "Failed to save branding", logger)
    await _audit_branding(request, current_user, "branding_updated")
    return result


def _validate_logo_bytes(content_type: str, contents: bytes) -> None:
    """Verify uploaded logo bytes match the declared type; sanitize SVG.

    Raises BadRequestError on a magic-byte mismatch or a script-bearing SVG.
    """
    if content_type == "image/png" and not contents.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BadRequestError("File is not a valid PNG image.")
    if content_type == "image/jpeg" and not contents.startswith(b"\xff\xd8\xff"):
        raise BadRequestError("File is not a valid JPEG image.")
    if content_type == "image/webp" and not (contents[:4] == b"RIFF" and contents[8:12] == b"WEBP"):
        raise BadRequestError("File is not a valid WebP image.")
    if content_type == "image/svg+xml":
        try:
            text = contents.decode("utf-8")
        except UnicodeDecodeError:
            raise BadRequestError("SVG must be valid UTF-8 text.")
        low = text.lower()
        if "<svg" not in low:
            raise BadRequestError("File is not a valid SVG image.")
        # Reject active content that would execute in the app origin.
        forbidden = (
            "<script",
            "javascript:",
            "<foreignobject",
            "<iframe",
            "<embed",
            "<object",
            "<!entity",
            "<!doctype",
            "onload=",
            "onerror=",
            "onclick=",
            "onmouseover=",
            "onmouseenter=",
            "onbegin=",
            "onabort=",
            "onactivate=",
        )
        if any(tok in low for tok in forbidden):
            raise BadRequestError(
                "SVG contains disallowed active content (scripts/handlers).",
            )


@router.post("/logo", response_model=BrandingLogoResponse)
async def upload_logo(
    request: Request,
    file: UploadFile = File(...),
    current_user: TokenData = require_permission("admin.settings"),
) -> BrandingLogoResponse:
    """
    Upload a logo image.

    Admin only. Accepts common image formats (PNG, JPEG, SVG, WebP).
    """
    allowed_types = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed_types:
        raise BadRequestError(
            f"Invalid file type '{file.content_type}'. Allowed: PNG, JPEG, SVG, WebP.",
        )

    # 2 MB logo cap, enforced pre-read (Content-Length) and during a chunked
    # read — an oversized body is never fully buffered into memory.
    contents = await read_upload_capped(file, 2 * 1024 * 1024, request=request)

    # Don't trust the client-declared content type — verify the bytes match, and
    # neutralize script-bearing SVGs (stored-XSS in the app origin).
    _validate_logo_bytes(file.content_type, contents)

    try:
        logo_url = await _upload_logo(contents, file.content_type)
    except OSError as exc:
        raise handle_service_error(exc, "Failed to save logo", logger)
    logger.info("Logo uploaded by user %s", current_user.user_id)
    await _audit_branding(request, current_user, "branding_logo_uploaded")
    return {"status": "uploaded", "logo_url": logo_url}


@router.post("/reset", response_model=BrandingResponse)
async def reset_branding(
    request: Request,
    current_user: TokenData = require_permission("admin.settings"),
) -> BrandingResponse:
    """
    Reset branding to platform defaults.

    Admin only. Clears all custom branding and restores built-in defaults.
    """
    result = await _reset_branding()
    await _audit_branding(request, current_user, "branding_reset")
    return result
