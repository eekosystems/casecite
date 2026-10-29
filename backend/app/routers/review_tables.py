"""Tabular review endpoints: define a field schema, point it at many documents,
run quote-verified extraction as one background job, review the cells by
hand, and export cited results.

Access follows the other work-product routers: the owner or a member of the
table's matter may read, run, review and export; the owner alone edits the
schema or scope and deletes. Anyone else gets 404, never 403. Documents put in
scope must be readable by the caller at that moment (owner or matter member),
so a colleague's private document cannot be reviewed through a shared table.

Saved schemas (`/review-tables/schemas`) are personal or shared with a
matter; the built-in ones are read-only and come from code.
"""

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import BadRequestError, NotFoundError
from app.models.review_tables import ReviewCell, ReviewRun, ReviewSchema, ReviewTable
from app.models.schemas import (
    Document,
    ReviewCellBulkRequest,
    ReviewCellBulkResponse,
    ReviewCellResponse,
    ReviewCellReviewRequest,
    ReviewDocumentTextResponse,
    ReviewFieldSpec,
    ReviewRunListResponse,
    ReviewRunResponse,
    ReviewSchemaCreateRequest,
    ReviewSchemaListResponse,
    ReviewSchemaResponse,
    ReviewSchemaUpdateRequest,
    ReviewTableAddDocumentsRequest,
    ReviewTableCreateRequest,
    ReviewTableDetailResponse,
    ReviewTableDocument,
    ReviewTableDocumentsAdded,
    ReviewTableEstimateResponse,
    ReviewTableListResponse,
    ReviewTableRunRequest,
    ReviewTableRunStarted,
    ReviewTableSummary,
    ReviewTableUpdateRequest,
)
from app.routers._matter_deps import accessible_matter_ids
from app.services import matters as matters_service
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.documents import document_service
from app.services.job_queue import RETRY_POLICIES, job_manager
from app.services.permissions import require_permission
from app.services.review_tables import review_table_service
from app.services.review_tables.export import (
    EXPORT_STATUSES,
    render_csv,
    render_json,
    render_xlsx,
    select_for_export,
)
from app.services.review_tables.schemas import review_schema_service
from app.services.review_tables.service import UNSET
from app.services.review_tables.templates import builtin_schemas
from app.services.user_keys import UserAPIKeys
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/review-tables", tags=["review-tables"])

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MEDIA = {"xlsx": _XLSX, "csv": "text/csv", "json": "application/json"}


async def _audit(
    request: Request,
    current_user: TokenData,
    event_type: AuditEventType,
    action: str,
    table_id: str,
    **details: Any,
) -> None:
    """Audit a review-table action. Ids and counts only — never field values or quotes."""
    await audit_service.log_event(
        event_type=event_type,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="review_table",
        resource_id=table_id,
        ip_address=get_client_ip(request),
        details={"action": action, **details},
    )


async def _require_readable(
    db: AsyncSession, table_id: str, current_user: TokenData, matter_ids: set[str]
) -> ReviewTable:
    table = await review_table_service.get_accessible(
        db, table_id, current_user.user_id, matter_ids
    )
    if table is None:
        raise NotFoundError("Review table not found")
    return table


async def _require_owned(
    db: AsyncSession, table_id: str, current_user: TokenData, matter_ids: set[str]
) -> ReviewTable:
    table = await _require_readable(db, table_id, current_user, matter_ids)
    if table.owner_id != current_user.user_id:
        raise NotFoundError("Review table not found")
    return table


async def _check_documents(ids: list[str], current_user: TokenData, matter_ids: set[str]) -> None:
    """Every document put in scope must be readable by the caller right now."""
    for doc_id in dict.fromkeys(ids):
        if await document_service.get_document(doc_id, current_user.user_id, matter_ids) is None:
            raise NotFoundError("Document not found")


async def _default_scope(db: AsyncSession, matter_id: str, current_user: TokenData) -> list[str]:
    """Every document filed under the matter, for a table created without ids."""
    matter = await matters_service.get_matter(db, matter_id)
    if matter is None:
        raise NotFoundError("Matter not found")
    personal_owner = str(matter.owner_id) if matter.is_personal else None
    return [d.id for d in document_service.documents_in_matter(matter_id, personal_owner)]


def _summary(table: ReviewTable, caller_id: str, counts: dict[str, int]) -> ReviewTableSummary:
    return ReviewTableSummary(
        id=table.id,
        name=table.name,
        description=table.description,
        matter_id=table.matter_id,
        owner_id=table.owner_id,
        role=review_table_service.role_for(table, caller_id),
        field_count=len(table.fields or []),
        document_count=len(table.document_ids or []),
        cell_count=counts.get("cells", 0),
        approved_count=counts.get("approved", 0),
        flagged_count=counts.get("flagged", 0),
        last_run_at=table.last_run_at,
        created_at=table.created_at,
        updated_at=table.updated_at,
    )


def _cell_response(cell: ReviewCell) -> ReviewCellResponse:
    return ReviewCellResponse(
        document_id=cell.document_id,
        field_key=cell.field_key,
        value=cell.value,
        quote=cell.quote,
        span_start=cell.span_start,
        span_end=cell.span_end,
        verified=bool(cell.verified),
        status=cell.status,
        error=cell.error,
        model_used=cell.model_used,
        updated_at=cell.updated_at,
        override_value=cell.override_value,
        effective_value=cell.effective_value,
        review_status=cell.review_status or "pending",
        note=cell.note,
        reviewed_by=cell.reviewed_by,
        reviewed_at=cell.reviewed_at,
        history=list(cell.history or []),
    )


def _run_response(run: ReviewRun) -> ReviewRunResponse:
    return ReviewRunResponse(
        id=run.id,
        table_id=run.table_id,
        runner_id=run.runner_id,
        scope=dict(run.scope or {}),
        status=run.status,
        job_id=run.job_id,
        counts=run.counts,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _schema_response(schema: ReviewSchema, caller_id: str) -> ReviewSchemaResponse:
    return ReviewSchemaResponse(
        id=schema.id,
        name=schema.name,
        description=schema.description,
        matter_id=schema.matter_id,
        owner_id=schema.owner_id,
        role=review_schema_service.role_for(schema, caller_id),
        is_builtin=False,
        fields=[ReviewFieldSpec(**f) for f in (schema.fields or [])],
        created_at=schema.created_at,
        updated_at=schema.updated_at,
    )


def _document_entries(resolved: list[tuple[str, Document | None]]) -> list[ReviewTableDocument]:
    return [
        ReviewTableDocument(
            id=doc_id,
            filename=doc.filename if doc else "Unavailable document",
            accessible=doc is not None,
        )
        for doc_id, doc in resolved
    ]


async def _detail(
    db: AsyncSession, table: ReviewTable, current_user: TokenData, matter_ids: set[str]
) -> ReviewTableDetailResponse:
    cells = await review_table_service.cells(db, table.id)
    counts = (await review_table_service.cell_counts(db, [table.id])).get(table.id, {})
    resolved = await review_table_service.resolve_documents(table, current_user.user_id, matter_ids)
    return ReviewTableDetailResponse(
        **_summary(table, current_user.user_id, counts).model_dump(),
        fields=[ReviewFieldSpec(**f) for f in (table.fields or [])],
        documents=_document_entries(resolved),
        cells=[_cell_response(c) for c in cells],
    )


# ------------------------------------------------------------- saved schemas
# Literal routes first: "/schemas" must never be read as a table id.


@router.get("/schemas", response_model=ReviewSchemaListResponse)
async def list_review_schemas(
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewSchemaListResponse:
    """The built-in schemas, then the caller's own and the ones shared with their matters."""
    saved = await review_schema_service.list_for_user(db, current_user.user_id, matter_ids)
    return ReviewSchemaListResponse(
        schemas=[
            *(ReviewSchemaResponse(**b) for b in builtin_schemas()),
            *(_schema_response(s, current_user.user_id) for s in saved),
        ]
    )


@router.post("/schemas", response_model=ReviewSchemaResponse, status_code=201)
async def create_review_schema(
    body: ReviewSchemaCreateRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewSchemaResponse:
    if body.matter_id and body.matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    schema = await review_schema_service.create(
        db,
        owner_id=current_user.user_id,
        matter_id=body.matter_id,
        name=body.name,
        description=body.description,
        fields=[f.model_dump() for f in body.fields],
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_schema_created",
        schema.id,
        matter_id=body.matter_id,
        fields=len(body.fields),
    )
    return _schema_response(schema, current_user.user_id)


async def _require_owned_schema(
    db: AsyncSession, schema_id: str, current_user: TokenData, matter_ids: set[str]
) -> ReviewSchema:
    schema = await review_schema_service.get_accessible(
        db, schema_id, current_user.user_id, matter_ids
    )
    if schema is None or schema.owner_id != current_user.user_id:
        raise NotFoundError("Schema not found")
    return schema


@router.patch("/schemas/{schema_id}", response_model=ReviewSchemaResponse)
async def update_review_schema(
    schema_id: str,
    body: ReviewSchemaUpdateRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewSchemaResponse:
    """Owner only. Built-in schemas cannot be edited (404)."""
    schema = await _require_owned_schema(db, schema_id, current_user, matter_ids)
    given = body.model_dump(exclude_unset=True)
    if not given:
        raise BadRequestError("Nothing to update.")
    if body.matter_id and body.matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    schema = await review_schema_service.update(
        db,
        schema,
        name=body.name,
        description=body.description,
        fields=[f.model_dump() for f in body.fields] if body.fields is not None else None,
        matter_id=body.matter_id,
        matter_given="matter_id" in given,
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_schema_updated",
        schema.id,
        changed=sorted(given),
    )
    return _schema_response(schema, current_user.user_id)


@router.delete("/schemas/{schema_id}")
async def delete_review_schema(
    schema_id: str,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> dict[str, str]:
    schema = await _require_owned_schema(db, schema_id, current_user, matter_ids)
    await review_schema_service.delete(db, schema)
    await _audit(
        request, current_user, AuditEventType.DATA_DELETION, "review_schema_deleted", schema_id
    )
    return {"status": "deleted", "id": schema_id}


# ------------------------------------------------------------------- collection


@router.get("", response_model=ReviewTableListResponse)
async def list_review_tables(
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewTableListResponse:
    """Tables the caller owns or can read through a matter, newest first."""
    tables = await review_table_service.list_for_user(db, current_user.user_id, matter_ids)
    counts = await review_table_service.cell_counts(db, [t.id for t in tables])
    return ReviewTableListResponse(
        tables=[_summary(t, current_user.user_id, counts.get(t.id, {})) for t in tables]
    )


@router.post("", response_model=ReviewTableDetailResponse, status_code=201)
async def create_review_table(
    body: ReviewTableCreateRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewTableDetailResponse:
    """Create a table. With a matter and no document ids, the scope is the matter's documents."""
    if body.matter_id and body.matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    document_ids = list(body.document_ids)
    if not document_ids and body.matter_id:
        document_ids = await _default_scope(db, body.matter_id, current_user)
    await _check_documents(document_ids, current_user, matter_ids)
    table = await review_table_service.create(
        db,
        owner_id=current_user.user_id,
        matter_id=body.matter_id,
        name=body.name,
        description=body.description,
        fields=[f.model_dump() for f in body.fields],
        document_ids=document_ids,
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_table_created",
        table.id,
        matter_id=body.matter_id,
        fields=len(body.fields),
        documents=len(document_ids),
    )
    return await _detail(db, table, current_user, matter_ids)


# ------------------------------------------------------------------ one table


@router.get("/{table_id}", response_model=ReviewTableDetailResponse)
async def get_review_table(
    table_id: str,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewTableDetailResponse:
    table = await _require_readable(db, table_id, current_user, matter_ids)
    return await _detail(db, table, current_user, matter_ids)


@router.patch("/{table_id}", response_model=ReviewTableDetailResponse)
async def update_review_table(
    table_id: str,
    body: ReviewTableUpdateRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewTableDetailResponse:
    """Owner only. Dropping a field or a document also drops its cells."""
    table = await _require_owned(db, table_id, current_user, matter_ids)
    if body.model_dump(exclude_unset=True) == {}:
        raise BadRequestError("Nothing to update.")
    if body.document_ids is not None:
        await _check_documents(body.document_ids, current_user, matter_ids)
    table = await review_table_service.update(
        db,
        table,
        name=body.name,
        description=body.description,
        fields=[f.model_dump() for f in body.fields] if body.fields is not None else None,
        document_ids=body.document_ids,
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_table_updated",
        table.id,
        changed=sorted(body.model_dump(exclude_unset=True)),
    )
    return await _detail(db, table, current_user, matter_ids)


@router.delete("/{table_id}")
async def delete_review_table(
    table_id: str,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> dict[str, str]:
    table = await _require_owned(db, table_id, current_user, matter_ids)
    await review_table_service.delete(db, table)
    await _audit(
        request, current_user, AuditEventType.DATA_DELETION, "review_table_deleted", table_id
    )
    return {"status": "deleted", "id": table_id}


@router.get("/{table_id}/estimate", response_model=ReviewTableEstimateResponse)
async def estimate_review_table(
    table_id: str,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewTableEstimateResponse:
    """Documents × fields and the approximate prompt tokens a full run would send."""
    table = await _require_readable(db, table_id, current_user, matter_ids)
    resolved = await review_table_service.resolve_documents(table, current_user.user_id, matter_ids)
    estimate = review_table_service.estimate(
        [doc for _id, doc in resolved], len(table.fields or [])
    )
    return ReviewTableEstimateResponse(**estimate)


# ------------------------------------------------------------------------ runs


def _run_scope(
    table: ReviewTable, body: ReviewTableRunRequest
) -> tuple[list[str] | None, list[str] | None]:
    """The documents and fields a run covers; None means all of them."""
    scope_docs = set(table.document_ids or [])
    scope_keys = {f["key"] for f in (table.fields or [])}
    document_ids = [d for d in dict.fromkeys(body.document_ids or []) if d in scope_docs] or None
    field_keys = [k for k in dict.fromkeys(body.field_keys or []) if k in scope_keys] or None
    if body.document_ids and not document_ids:
        raise BadRequestError("None of the given documents is in this table.")
    if body.field_keys and not field_keys:
        raise BadRequestError("None of the given fields is in this table.")
    if not scope_docs or not scope_keys:
        raise BadRequestError("Add at least one document and one field before running.")
    return document_ids, field_keys


async def _start_run(
    db: AsyncSession,
    request: Request,
    table: ReviewTable,
    current_user: TokenData,
    matter_ids: set[str],
    document_ids: list[str] | None,
    field_keys: list[str] | None,
) -> ReviewTableRunStarted:
    """Record the run, submit the job, audit the start."""
    user_keys = UserAPIKeys.from_request(request, user_id=current_user.user_id)
    try:
        review_table_service._llm_client(user_keys)  # fail now, not inside the job
    except ValueError as e:
        raise BadRequestError(str(e))

    documents = len(document_ids or table.document_ids or [])
    fields = len(field_keys or table.fields or [])
    run = await review_table_service.create_run(
        db,
        table,
        runner_id=current_user.user_id,
        scope={"document_ids": document_ids, "field_keys": field_keys},
    )
    table_id, user_id, user_email, run_id = (
        table.id,
        current_user.user_id,
        current_user.email,
        run.id,
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_ACCESS,
        "review_table_run_started",
        table_id,
        run_id=run_id,
        documents=documents,
        fields=fields,
    )

    async def do_work() -> dict[str, Any]:
        result = await review_table_service.run(
            table_id=table_id,
            user_id=user_id,
            accessible_matter_ids=matter_ids,
            user_keys=user_keys,
            document_ids=document_ids,
            field_keys=field_keys,
            run_id=run_id,
        )
        await audit_service.log_event(
            event_type=AuditEventType.DATA_ACCESS,
            user_id=user_id,
            user_email=user_email,
            resource_type="review_table",
            resource_id=table_id,
            details={"action": "review_table_run_completed", "run_id": run_id, **result},
        )
        return result

    job_id = await job_manager.submit(
        do_work,
        user_id=user_id,
        endpoint=f"/review-tables/{table_id}/run",
        retry_policy=RETRY_POLICIES["ai_analysis"],
    )
    await review_table_service.set_run_job(db, run_id, job_id)
    return ReviewTableRunStarted(
        job_id=job_id,
        poll_url=f"/api/v1/jobs/{job_id}",
        documents=documents,
        fields=fields,
        run_id=run_id,
    )


@router.post("/{table_id}/run", response_model=ReviewTableRunStarted, status_code=202)
async def run_review_table(
    table_id: str,
    request: Request,
    body: ReviewTableRunRequest | None = None,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> JSONResponse:
    """Start extraction as one job (202 + a job to poll). Optional ids and
    field keys narrow it to a re-run of some rows or columns."""
    table = await _require_readable(db, table_id, current_user, matter_ids)
    document_ids, field_keys = _run_scope(table, body or ReviewTableRunRequest())
    started = await _start_run(
        db, request, table, current_user, matter_ids, document_ids, field_keys
    )
    return JSONResponse(status_code=202, content=started.model_dump())


@router.get("/{table_id}/runs", response_model=ReviewRunListResponse)
async def list_review_runs(
    table_id: str,
    limit: int = Query(50, ge=1, le=200),
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewRunListResponse:
    """The table's extraction runs, newest first, with their scope and counts."""
    table = await _require_readable(db, table_id, current_user, matter_ids)
    runs = await review_table_service.list_runs(db, table.id, limit=limit)
    return ReviewRunListResponse(runs=[_run_response(r) for r in runs])


# --------------------------------------------------------------------- review


def _review_kwargs(body: ReviewCellReviewRequest) -> dict[str, Any]:
    given = body.model_fields_set
    return {
        "override_value": body.override_value if "override_value" in given else UNSET,
        "review_status": body.review_status,
        "note": body.note if "note" in given else UNSET,
    }


@router.patch("/{table_id}/cells/{document_id}/{field_key}", response_model=ReviewCellResponse)
async def review_cell(
    table_id: str,
    document_id: str,
    field_key: str,
    body: ReviewCellReviewRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewCellResponse:
    """Override, approve, flag or annotate one cell. Owner and matter members
    are reviewers alike; a re-run never undoes an override or an approval."""
    table = await _require_readable(db, table_id, current_user, matter_ids)
    try:
        cell = await review_table_service.review_cell(
            db, table, document_id, field_key, reviewer=current_user.user_id, **_review_kwargs(body)
        )
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_cell_reviewed",
        table_id,
        cells=1,
        changed=sorted(body.model_fields_set),
    )
    return _cell_response(cell)


@router.post("/{table_id}/cells/bulk", response_model=ReviewCellBulkResponse)
async def bulk_review_cells(
    table_id: str,
    body: ReviewCellBulkRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewCellBulkResponse:
    """The same review change on many cells (approve a column, flag a selection)."""
    table = await _require_readable(db, table_id, current_user, matter_ids)
    refs = [(c.document_id, c.field_key) for c in body.cells]
    try:
        cells = await review_table_service.bulk_review(
            db, table, refs, reviewer=current_user.user_id, **_review_kwargs(body)
        )
    except ValueError as e:
        raise BadRequestError(str(e))
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_cells_bulk_reviewed",
        table_id,
        cells=len(cells),
        changed=sorted(body.model_fields_set - {"cells"}),
    )
    return ReviewCellBulkResponse(updated=len(cells), cells=[_cell_response(c) for c in cells])


# ------------------------------------------------------------------ documents


@router.post("/{table_id}/documents", response_model=ReviewTableDocumentsAdded)
async def add_review_documents(
    table_id: str,
    body: ReviewTableAddDocumentsRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewTableDocumentsAdded:
    """Owner only. Appends documents the caller can read; ``run`` extracts just the new rows."""
    table = await _require_owned(db, table_id, current_user, matter_ids)
    await _check_documents(body.document_ids, current_user, matter_ids)
    added = await review_table_service.add_documents(db, table, body.document_ids)
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_table_documents_added",
        table_id,
        documents=len(added),
    )
    started = None
    if body.run and added and table.fields:
        started = await _start_run(db, request, table, current_user, matter_ids, added, None)
    return ReviewTableDocumentsAdded(
        added=added, table=await _detail(db, table, current_user, matter_ids), run=started
    )


@router.delete("/{table_id}/documents/{document_id}", response_model=ReviewTableDetailResponse)
async def remove_review_document(
    table_id: str,
    document_id: str,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewTableDetailResponse:
    """Owner only. Drops the row and every cell extracted for it."""
    table = await _require_owned(db, table_id, current_user, matter_ids)
    if not await review_table_service.remove_document(db, table, document_id):
        raise NotFoundError("Document is not in this table")
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "review_table_document_removed",
        table_id,
        documents=1,
    )
    return await _detail(db, table, current_user, matter_ids)


@router.get("/{table_id}/documents/{document_id}/text", response_model=ReviewDocumentTextResponse)
async def review_document_text(
    table_id: str,
    document_id: str,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> ReviewDocumentTextResponse:
    """The document's indexed text, the text every cell's offsets refer to, so the
    source panel can highlight the exact quote."""
    table = await _require_readable(db, table_id, current_user, matter_ids)
    if document_id not in (table.document_ids or []):
        raise NotFoundError("Document is not in this table")
    doc = await document_service.get_document(document_id, current_user.user_id, matter_ids)
    if doc is None:
        raise NotFoundError("Document not found")
    try:
        text = await review_table_service.document_text(doc)
    except (LookupError, RuntimeError, ValueError) as e:
        raise NotFoundError(str(e))
    return ReviewDocumentTextResponse(
        document_id=document_id, filename=doc.filename, text=text, length=len(text)
    )


# --------------------------------------------------------------------- export


def _csv_list(value: str | None) -> list[str] | None:
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    return items or None


@router.get("/{table_id}/export")
async def export_review_table(
    table_id: str,
    request: Request,
    format: str = Query("xlsx", pattern="^(xlsx|csv|json)$"),
    fields: str | None = Query(None, description="Comma-separated field keys to include"),
    reviewed_only: bool = Query(False, description="Only cells a reviewer approved"),
    include_quotes: bool = Query(True),
    status: str | None = Query(
        None,
        description="Comma-separated: extracted, unverified, not_found, error, approved, flagged",
    ),
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> Response:
    """Results with citations and the review layer, as a workbook (Results,
    Citations, Review, Summary), a flat CSV or JSON, narrowed by the options."""
    table = await _require_readable(db, table_id, current_user, matter_ids)
    statuses = _csv_list(status)
    if statuses and any(s not in EXPORT_STATUSES for s in statuses):
        raise BadRequestError(f"Unknown status filter; use one of {', '.join(EXPORT_STATUSES)}.")
    all_cells = [
        _cell_response(c).model_dump() for c in await review_table_service.cells(db, table.id)
    ]
    chosen_fields, cells = select_for_export(
        list(table.fields or []),
        all_cells,
        field_keys=_csv_list(fields),
        reviewed_only=reviewed_only,
        statuses=statuses,
    )
    if not chosen_fields:
        raise BadRequestError("None of the given fields is in this table.")
    resolved = await review_table_service.resolve_documents(table, current_user.user_id, matter_ids)
    documents = [d.model_dump() for d in _document_entries(resolved)]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", table.name).strip("-")[:60] or "review"
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_EXPORT,
        "review_table_export",
        table_id,
        format=format,
        cells=len(cells),
        fields=len(chosen_fields),
        reviewed_only=reviewed_only,
        include_quotes=include_quotes,
    )
    if format == "csv":
        content: bytes | str = render_csv(
            chosen_fields, documents, cells, include_quotes=include_quotes
        )
    elif format == "json":
        meta = {"id": table.id, "name": table.name, "description": table.description}
        content = render_json(meta, chosen_fields, documents, cells, include_quotes=include_quotes)
    else:
        content = render_xlsx(
            table.name, chosen_fields, documents, cells, include_quotes=include_quotes
        )
    return Response(
        content=content,
        media_type=_MEDIA[format],
        headers={"Content-Disposition": f'attachment; filename="{stem}.{format}"'},
    )
