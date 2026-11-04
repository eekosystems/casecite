import logging

from fastapi import HTTPException

from app.config import settings
from app.models.schemas import ConnectorType
from app.services.connectors.base import BaseConnector
from app.services.connectors.box import box_connector
from app.services.connectors.clio import clio_connector
from app.services.connectors.dropbox import dropbox_connector
from app.services.connectors.filevine import filevine_connector
from app.services.connectors.google_drive import google_drive_connector
from app.services.connectors.imanage import imanage_connector
from app.services.connectors.microsoft import microsoft_connector
from app.services.connectors.netdocuments import netdocuments_connector

logger = logging.getLogger(__name__)

# Connectors that have been exercised against a live provider.
_STABLE_CONNECTORS: dict[ConnectorType, BaseConnector] = {
    ConnectorType.GOOGLE_DRIVE: google_drive_connector,
    ConnectorType.ONEDRIVE: microsoft_connector,
    ConnectorType.BOX: box_connector,
    ConnectorType.DROPBOX: dropbox_connector,
    ConnectorType.FILEVINE: filevine_connector,
    ConnectorType.CLIO: clio_connector,
}

# Implemented against the vendors' published APIs but never validated against a
# live tenant. They stay out of the registry — absent from GET /connectors and
# refused on every route — until EXPERIMENTAL_CONNECTORS=true.
EXPERIMENTAL_CONNECTORS: dict[ConnectorType, BaseConnector] = {
    ConnectorType.NETDOCUMENTS: netdocuments_connector,
    ConnectorType.IMANAGE: imanage_connector,
}


def build_registry(experimental: bool) -> dict[ConnectorType, BaseConnector]:
    """The connectors the app exposes, with the experimental pair only on opt-in."""
    registry = dict(_STABLE_CONNECTORS)
    if experimental:
        registry.update(EXPERIMENTAL_CONNECTORS)
    return registry


# Registry of all exposed connectors
CONNECTORS = build_registry(settings.experimental_connectors)


def is_experimental(connector_type: ConnectorType) -> bool:
    return connector_type in EXPERIMENTAL_CONNECTORS


def registry_summary() -> str:
    """One line for the startup log: what is registered and what is held back."""
    registered = ", ".join(c.value for c in CONNECTORS)
    disabled = [c.value for c in EXPERIMENTAL_CONNECTORS if c not in CONNECTORS]
    if disabled:
        return (
            f"Connectors registered: {registered}. Disabled as experimental "
            f"(untested against a live tenant; set EXPERIMENTAL_CONNECTORS=true to enable): "
            f"{', '.join(disabled)}."
        )
    enabled = ", ".join(c.value for c in EXPERIMENTAL_CONNECTORS)
    return f"Connectors registered: {registered}. Experimental connectors enabled: {enabled}."


def get_connector(connector_type: ConnectorType):
    """Get a connector by type."""
    connector = CONNECTORS.get(connector_type)
    if not connector:
        detail = (
            f"Connector type '{connector_type.value}' is not available. "
            f"Available connectors: {', '.join(c.value for c in CONNECTORS)}"
        )
        if is_experimental(connector_type):
            detail += (
                ". This connector is experimental (untested against a live tenant) "
                "and is enabled with EXPERIMENTAL_CONNECTORS=true"
            )
        raise HTTPException(status_code=400, detail=detail)
    return connector


async def get_all_connector_statuses(include_admin_only: bool = True):
    """Get status of all connectors.

    With ``include_admin_only=False`` a connector backed by a firm-wide
    credential (``requires_admin``) is reported as not connected without
    touching the provider, so a non-admin never opens a session with it.
    """
    statuses = []
    for connector_type, connector in CONNECTORS.items():
        if connector.requires_admin and not include_admin_only:
            status = {
                "connected": False,
                "configured": connector.is_configured,
                "type": connector_type.value,
                "admin_only": True,
            }
        else:
            status = await connector.get_status()
            if connector.requires_admin:
                status["admin_only"] = True
        if is_experimental(connector_type):
            status["experimental"] = True
        # Whether a sync can be scoped to one folder (the UI offers a folder
        # picker) or only ever covers the whole account.
        status["supports_folder_sync"] = bool(connector.supports_folder_sync)
        statuses.append(status)
    return statuses
