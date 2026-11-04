"""The Word add-in, served with the stack.

The task pane is static files built into the image and served at
``/addin/`` on the same origin as the web app (nginx in the single
container, Caddy in the bundled stack). What Word needs to find it is the
manifest, and the manifest must name the deployed origin — which is only
known once the instance runs. So the manifest is rendered here, from the
template shipped with the image, with every placeholder URL pointed at this
instance (or at ``WORD_ADDIN_ORIGIN`` when the pane is hosted elsewhere).

An admin uploads ``https://<site>/api/v1/addin/manifest.xml`` — or pastes
that URL — into Microsoft 365 admin center's Centralized Deployment, once.
The manifest contains nothing secret (URLs, an id and icons), so it is public.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.config import settings
from app.exceptions import NotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/addin", tags=["word-addin"])

# The origin the committed manifest.xml points at (local development).
PLACEHOLDER_ORIGIN = "https://localhost:3200"
# Where the Dockerfile puts the template; the repo path serves development.
_IMAGE_TEMPLATE = "/app/addin-manifest.xml"
_REPO_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "word-addin",
    "manifest.xml",
)
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def template_path() -> str | None:
    """The manifest template: configured path, the image's copy, or the repo's."""
    for candidate in (settings.word_addin_manifest_path, _IMAGE_TEMPLATE, _REPO_TEMPLATE):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def pane_origin(request: Request) -> str:
    """Where the pane's files are served from.

    ``WORD_ADDIN_ORIGIN`` when the pane is hosted separately; otherwise this
    instance's own address (``FRONTEND_URL``, else the request's base URL)
    plus ``/addin`` — the path the image serves the bundled pane at.
    """
    if settings.word_addin_origin:
        return settings.word_addin_origin.rstrip("/")
    site = (settings.frontend_url or str(request.base_url)).rstrip("/")
    return f"{site}/addin"


def render_manifest(template: str, origin: str, addin_id: str = "") -> str:
    out = template.replace(PLACEHOLDER_ORIGIN, origin)
    if addin_id:
        if not _GUID.match(addin_id):
            raise ValueError("WORD_ADDIN_ID must be a GUID")
        out = re.sub(r"<Id>[^<]*</Id>", f"<Id>{addin_id}</Id>", out, count=1)
    return out


@router.get("/manifest.xml")
async def get_manifest(request: Request) -> Response:
    """The add-in manifest for THIS deployment, ready for Centralized Deployment.

    Every URL points at the pane as served by this instance (``/addin/``),
    or at ``WORD_ADDIN_ORIGIN`` when it is hosted elsewhere. ``WORD_ADDIN_ID``
    overrides the add-in id for a second, independent deployment.
    """
    path = template_path()
    if path is None:
        raise NotFoundError("The Word add-in manifest template is not part of this installation.")
    with open(path, encoding="utf-8") as f:
        template = f.read()
    try:
        xml = render_manifest(template, pane_origin(request), settings.word_addin_id)
    except ValueError as e:
        logger.error("Word add-in manifest not rendered: %s", e)
        raise NotFoundError(str(e))
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Content-Disposition": 'inline; filename="casecite-word-addin.xml"'},
    )
