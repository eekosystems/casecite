"""Document management response models."""

from pydantic import BaseModel


class DocumentContentResponse(BaseModel):
    """GET /documents/{document_id}/content."""

    document_id: str
    text: str
    filename: str


class DocumentDeleteResponse(BaseModel):
    """DELETE /documents/{document_id}."""

    status: str
    id: str
    # What a completed delete removed: the search index entries, the analysis
    # rows derived from the document, and the stored file. A delete only
    # reports "deleted" once all three are gone.
    removed: dict[str, bool] | None = None


class FailedDeletionItem(BaseModel):
    """One document whose delete did not complete."""

    id: str
    user_id: str
    matter_id: str | None = None
    filename: str
    failed_step: str | None = None
    error: str | None = None
    failed_at: str | None = None
    attempts: int = 0
    removed: dict[str, bool]


class FailedDeletionsResponse(BaseModel):
    """GET /documents/failed-deletions."""

    documents: list[FailedDeletionItem]
    count: int
