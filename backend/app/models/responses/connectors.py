"""Connector response models — OAuth and sync."""

from typing import Any

from pydantic import BaseModel


class ConnectorListResponse(BaseModel):
    """GET /connectors."""

    connectors: list[dict[str, Any]]


class ConnectorFolder(BaseModel):
    id: str
    name: str
    path: str | None = None


class ConnectorFoldersResponse(BaseModel):
    """GET /connectors/{type}/folders: one level of the account's folder tree."""

    connector: str
    parent_id: str | None = None
    folders: list[ConnectorFolder]
    next_page_token: str | None = None


class ConnectorCallbackResponse(BaseModel):
    """GET /connectors/{type}/callback."""

    status: str
    account: dict[str, Any]


class SyncStartResponse(BaseModel):
    """POST /connectors/{type}/sync."""

    sync_id: str
    status: str


class SyncStatusResponse(BaseModel):
    """GET /connectors/{type}/sync/{sync_id}."""

    sync_id: str
    status: str
    progress: dict[str, Any] | None = None
    error: str | None = None
    details: dict[str, Any] | None = None
