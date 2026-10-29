"""Workflow endpoints: define a repeatable sequence of steps, validate it as
it is edited, run it over a set of documents as one background job, read,
resume, retry, approve and compare past runs, and keep every version.

Access follows the other work-product routers: the owner or a member of the
workflow's matter may read it and run it; the owner alone edits the steps,
archives or deletes it. Anyone else gets 404, never 403. Every input document
must be readable by the runner at that moment, so a colleague's private
document cannot be put through a shared workflow. Permission and document
checks happen here, once; the steps then call the service layer directly.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import BadRequestError, ForbiddenError, NotFoundError, UnprocessableError
from app.models.schemas import (
    WorkflowCreateRequest,
    WorkflowDecisionRequest,
    WorkflowDetailResponse,
    WorkflowDocumentRef,
    WorkflowDuplicateRequest,
    WorkflowListResponse,
    WorkflowRestoreRequest,
    WorkflowRunDetailResponse,
    WorkflowRunListResponse,
    WorkflowRunRequest,
    WorkflowRunStarted,
    WorkflowRunSummary,
    WorkflowStep,
    WorkflowStepRunResponse,
    WorkflowSummary,
    WorkflowTemplate,
    WorkflowTemplatesResponse,
    WorkflowUpdateRequest,
    WorkflowValidateRequest,
    WorkflowValidateResponse,
    WorkflowVersionDetail,
    WorkflowVersionListResponse,
    WorkflowVersionSummary,
)
from app.models.workflows import (
    RUN_AWAITING_APPROVAL,
    RUN_RUNNING,
    Workflow,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowVersion,
)
from app.routers._matter_deps import accessible_matter_ids
from app.services import matters as matters_service
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.documents import document_service
from app.services.job_queue import RetryPolicy, job_manager
from app.services.permissions import require_permission
from app.services.user_keys import UserAPIKeys
from app.services.workflows import WorkflowError, workflow_service
from app.services.workflows.templates import TEMPLATES, template_by_key, template_steps
from app.services.workflows.validation import validate_steps
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])


async def _audit(
    request: Request,
    current_user: TokenData,
    event_type: AuditEventType,
    action: str,
    workflow_id: str,
    **details: Any,
) -> None:
    """Ids and counts only — never step parameters, document text or outputs."""
    await audit_service.log_event(
        event_type=event_type,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="workflow",
        resource_id=workflow_id,
        ip_address=get_client_ip(request),
        details={"action": action, **details},
    )


async def _require_readable(
    db: AsyncSession, workflow_id: str, current_user: TokenData, matter_ids: set[str]
) -> Workflow:
    workflow = await workflow_service.get_accessible(
        db, workflow_id, current_user.user_id, matter_ids
    )
    if workflow is None:
        raise NotFoundError("Workflow not found")
    return workflow


async def _require_owned(
    db: AsyncSession, workflow_id: str, current_user: TokenData, matter_ids: set[str]
) -> Workflow:
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    if workflow.owner_id != current_user.user_id:
        raise NotFoundError("Workflow not found")
    return workflow


async def _require_run(db: AsyncSession, workflow: Workflow, run_id: str) -> WorkflowRun:
    run = await workflow_service.get_run(db, workflow.id, run_id)
    if run is None:
        raise NotFoundError("Workflow run not found")
    return run


def _require_active(workflow: Workflow) -> None:
    if workflow.archived_at is not None:
        raise BadRequestError("This workflow is archived. Unarchive it to run it.")


async def _check_documents(ids: list[str], current_user: TokenData, matter_ids: set[str]) -> None:
    """Every document a run reads must be readable by the runner right now."""
    for doc_id in dict.fromkeys(ids):
        if await document_service.get_document(doc_id, current_user.user_id, matter_ids) is None:
            raise NotFoundError("Document not found")


async def _document_refs(
    ids: list[str], current_user: TokenData, matter_ids: set[str]
) -> list[WorkflowDocumentRef]:
    """Names for ``ids`` as the caller may see them; unreadable ones are marked, not hidden."""
    refs: list[WorkflowDocumentRef] = []
    for doc_id in dict.fromkeys(ids):
        doc = await document_service.get_document(doc_id, current_user.user_id, matter_ids)
        refs.append(
            WorkflowDocumentRef(
                id=doc_id, filename=doc.filename if doc else None, accessible=doc is not None
            )
        )
    return refs


def _fixed_document_ids(steps: list[dict[str, Any]]) -> list[str]:
    """Document ids a workflow names in its steps (explicit inputs, baselines, references)."""
    ids: list[str] = []
    for step in steps:
        ids.extend((step.get("inputs") or {}).get("document_ids") or [])
        params = step.get("params") or {}
        if params.get("baseline_document_id"):
            ids.append(str(params["baseline_document_id"]))
        ids.extend(params.get("reference_document_ids") or [])
    return list(dict.fromkeys(ids))


def _check_step_count(steps: list[Any]) -> None:
    cap = workflow_service.caps()["max_steps"]
    if len(steps) > cap:
        raise UnprocessableError(f"A workflow may have at most {cap} steps on this server.")


async def _check_approvers(
    db: AsyncSession, steps: list[dict[str, Any]], matter_id: str | None, owner_id: str
) -> None:
    """Named approvers must be the owner or members of the workflow's matter."""
    named: set[str] = set()
    for step in steps:
        spec = (step.get("params") or {}).get("approvers")
        if isinstance(spec, list):
            named.update(str(s) for s in spec)
    if not named:
        return
    allowed = {owner_id}
    if matter_id:
        allowed.update(str(m.user_id) for m in await matters_service.list_members(db, matter_id))
    unknown = sorted(named - allowed)
    if unknown:
        raise UnprocessableError(
            "Approvers must be the workflow's owner or members of its matter "
            f"({len(unknown)} named user{'s' if len(unknown) != 1 else ''} not allowed)."
        )


async def _default_scope(db: AsyncSession, matter_id: str) -> list[str]:
    matter = await matters_service.get_matter(db, matter_id)
    if matter is None:
        raise NotFoundError("Matter not found")
    personal_owner = str(matter.owner_id) if matter.is_personal else None
    return [d.id for d in document_service.documents_in_matter(matter_id, personal_owner)]


def _summary(workflow: Workflow, caller_id: str, stats: dict[str, Any] | None) -> WorkflowSummary:
    stats = stats or {}
    return WorkflowSummary(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        matter_id=workflow.matter_id,
        owner_id=workflow.owner_id,
        role=workflow_service.role_for(workflow, caller_id),
        template_key=workflow.template_key,
        version=int(workflow.version or 1),
        archived_at=workflow.archived_at,
        step_count=len(workflow.steps or []),
        step_kinds=[s["kind"] for s in (workflow.steps or [])],
        run_count=int(stats.get("run_count") or 0),
        last_run_at=stats.get("last_run_at") or workflow.last_run_at,
        last_run_status=stats.get("last_run_status"),
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


async def _detail(
    db: AsyncSession, workflow: Workflow, current_user: TokenData, matter_ids: set[str]
) -> WorkflowDetailResponse:
    stats = await workflow_service.run_stats(db, [workflow.id])
    steps = list(workflow.steps or [])
    return WorkflowDetailResponse(
        **_summary(workflow, current_user.user_id, stats.get(workflow.id)).model_dump(),
        steps=[WorkflowStep(**s) for s in steps],
        documents=await _document_refs(_fixed_document_ids(steps), current_user, matter_ids),
    )


def _version_summary(row: WorkflowVersion) -> WorkflowVersionSummary:
    steps = list(row.steps or [])
    return WorkflowVersionSummary(
        version=int(row.version),
        name=row.name,
        description=row.description,
        step_count=len(steps),
        step_names=[str(s.get("name")) for s in steps],
        created_by=row.created_by,
        created_at=row.created_at,
        change_note=row.change_note,
    )


def _step_run_response(row: WorkflowStepRun) -> WorkflowStepRunResponse:
    return WorkflowStepRunResponse(
        index=row.index,
        name=row.name,
        kind=row.kind,
        status=row.status,
        output=row.output,
        input_document_ids=list(row.input_document_ids or []),
        produced_ids=dict(row.produced_ids or {}),
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _run_summary(
    run: WorkflowRun,
    completed: int,
    *,
    caller_id: str,
    workflow_name: str | None = None,
    awaiting: tuple[str, list[str]] | None = None,
) -> WorkflowRunSummary:
    step_name, approver_ids = awaiting or (None, [])
    return WorkflowRunSummary(
        id=run.id,
        workflow_id=run.workflow_id,
        workflow_name=workflow_name,
        workflow_version=int(run.workflow_version or 1),
        runner_id=run.runner_id,
        matter_id=run.matter_id,
        status=run.status,
        steps=len(run.steps or []),
        completed_steps=completed,
        documents=len(run.input_document_ids or []),
        job_id=run.job_id,
        error=run.error,
        resumed_count=int(run.resumed_count or 0),
        awaiting_step=step_name,
        approver_ids=approver_ids,
        can_approve=run.status == RUN_AWAITING_APPROVAL and caller_id in approver_ids,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


async def _run_summaries(
    db: AsyncSession,
    runs: list[WorkflowRun],
    *,
    caller_id: str,
    names: dict[str, str] | None = None,
) -> list[WorkflowRunSummary]:
    ids = [r.id for r in runs]
    completed = await workflow_service.completed_counts(db, ids)
    awaiting = await workflow_service.awaiting_info(db, ids)
    return [
        _run_summary(
            r,
            completed.get(r.id, 0),
            caller_id=caller_id,
            workflow_name=(names or {}).get(r.workflow_id),
            awaiting=awaiting.get(r.id),
        )
        for r in runs
    ]


async def _run_detail(
    db: AsyncSession,
    workflow: Workflow,
    run: WorkflowRun,
    current_user: TokenData,
    matter_ids: set[str],
) -> WorkflowRunDetailResponse:
    rows = await workflow_service.step_runs(db, run.id)
    summary = (
        await _run_summaries(
            db, [run], caller_id=current_user.user_id, names={workflow.id: workflow.name}
        )
    )[0]
    steps = list(run.steps or [])
    doc_ids = list(run.input_document_ids or []) + _fixed_document_ids(steps)
    for row in rows:
        doc_ids.extend(row.input_document_ids or [])
    return WorkflowRunDetailResponse(
        **summary.model_dump(),
        step_definitions=[WorkflowStep(**s) for s in steps],
        step_runs=[_step_run_response(r) for r in rows],
        input_document_ids=list(run.input_document_ids or []),
        document_refs=await _document_refs(doc_ids, current_user, matter_ids),
    )


def _require_client(request: Request, current_user: TokenData) -> UserAPIKeys:
    """The runner's keys, refused now (400) rather than inside the job when no model is set."""
    user_keys = UserAPIKeys.from_request(request, user_id=current_user.user_id)
    try:
        workflow_service._llm_client(user_keys)
    except ValueError as e:
        raise BadRequestError(str(e))
    return user_keys


async def _submit(
    request: Request,
    db: AsyncSession,
    current_user: TokenData,
    matter_ids: set[str],
    workflow: Workflow,
    run: WorkflowRun,
    *,
    user_keys: UserAPIKeys,
    start_index: int,
    action: str,
) -> JSONResponse:
    """Submit the run (or its continuation) as one job; 202 + a job to poll."""
    user_id, user_email = current_user.user_id, current_user.email
    steps = len(run.steps or [])
    documents = len(run.input_document_ids or [])
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_ACCESS,
        action,
        workflow.id,
        run_id=run.id,
        steps=steps,
        start_index=start_index,
        documents=documents,
        step_kinds=[s["kind"] for s in (run.steps or [])],
    )

    async def do_work() -> dict[str, Any]:
        try:
            result = await workflow_service.execute(
                run_id=run.id,
                user_id=user_id,
                accessible_matter_ids=matter_ids,
                user_keys=user_keys,
                start_index=start_index,
            )
        except WorkflowError as e:
            await audit_service.log_event(
                event_type=AuditEventType.DATA_ACCESS,
                user_id=user_id,
                user_email=user_email,
                resource_type="workflow",
                resource_id=workflow.id,
                details={"action": "workflow_run_failed", "run_id": run.id, "error": str(e)[:200]},
            )
            raise
        finished = result.get("status") == RUN_AWAITING_APPROVAL
        await audit_service.log_event(
            event_type=AuditEventType.DATA_ACCESS,
            user_id=user_id,
            user_email=user_email,
            resource_type="workflow",
            resource_id=workflow.id,
            details={
                "action": "workflow_run_awaiting_approval"
                if finished
                else "workflow_run_completed",
                **result,
            },
        )
        return result

    # Steps are expensive and already persist their own outcome; a failed step
    # is resumed deliberately, never re-run by the queue.
    job_id = await job_manager.submit(
        do_work,
        user_id=user_id,
        endpoint=f"/workflows/{workflow.id}/run",
        retry_policy=RetryPolicy(max_retries=0),
    )
    await workflow_service.set_job(db, run.id, job_id)
    payload = WorkflowRunStarted(
        run_id=run.id,
        job_id=job_id,
        poll_url=f"/api/v1/jobs/{job_id}",
        steps=steps,
        documents=documents,
    )
    return JSONResponse(status_code=202, content=payload.model_dump())


async def _continue_run(
    request: Request,
    db: AsyncSession,
    current_user: TokenData,
    matter_ids: set[str],
    workflow: Workflow,
    run: WorkflowRun,
    *,
    start_index: int,
    action: str,
) -> JSONResponse:
    """Checks every continuation shares, then submit from ``start_index``."""
    _require_active(workflow)
    await _check_documents(list(run.input_document_ids or []), current_user, matter_ids)
    user_keys = _require_client(request, current_user)
    return await _submit(
        request,
        db,
        current_user,
        matter_ids,
        workflow,
        run,
        user_keys=user_keys,
        start_index=start_index,
        action=action,
    )


# ------------------------------------------------------------------- collection


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    include_archived: bool = Query(False),
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowListResponse:
    """Workflows the caller owns or can read through a matter, newest first.
    Archived ones only with ``include_archived=true``."""
    workflows = await workflow_service.list_for_user(
        db, current_user.user_id, matter_ids, include_archived=include_archived
    )
    stats = await workflow_service.run_stats(db, [w.id for w in workflows])
    return WorkflowListResponse(
        workflows=[_summary(w, current_user.user_id, stats.get(w.id)) for w in workflows]
    )


@router.post("", response_model=WorkflowDetailResponse, status_code=201)
async def create_workflow(
    body: WorkflowCreateRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    if body.matter_id and body.matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    _check_step_count(body.steps)
    steps = [s.model_dump(mode="json") for s in body.steps]
    await _check_documents(_fixed_document_ids(steps), current_user, matter_ids)
    await _check_approvers(db, steps, body.matter_id, current_user.user_id)
    workflow = await workflow_service.create(
        db,
        owner_id=current_user.user_id,
        matter_id=body.matter_id,
        name=body.name,
        description=body.description,
        steps=steps,
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "workflow_created",
        workflow.id,
        matter_id=body.matter_id,
        steps=len(steps),
        step_kinds=[s["kind"] for s in steps],
    )
    return await _detail(db, workflow, current_user, matter_ids)


@router.post("/validate", response_model=WorkflowValidateResponse)
async def validate_workflow(
    body: WorkflowValidateRequest,
    current_user: TokenData = require_permission("contracts.use"),
) -> WorkflowValidateResponse:
    """Check a step list without saving: every problem, with the step and field it concerns."""
    issues = validate_steps(body.steps, workflow_service.caps()["max_steps"])
    return WorkflowValidateResponse(valid=not issues, issues=issues)


@router.get("/templates", response_model=WorkflowTemplatesResponse)
async def list_templates(
    current_user: TokenData = require_permission("contracts.use"),
) -> WorkflowTemplatesResponse:
    """The built-in starter workflows. Instantiate one with POST /workflows/from-template/{key}."""
    return WorkflowTemplatesResponse(
        templates=[
            WorkflowTemplate(
                key=t["key"],
                name=t["name"],
                description=t["description"],
                steps=template_steps(t),
            )
            for t in TEMPLATES
        ]
    )


@router.get("/runs", response_model=WorkflowRunListResponse)
async def list_recent_runs(
    matter_id: str | None = Query(None, max_length=36),
    limit: int = Query(20, ge=1, le=200),
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowRunListResponse:
    """The newest runs across every workflow the caller can read, optionally one matter's."""
    workflows = await workflow_service.list_for_user(
        db, current_user.user_id, matter_ids, include_archived=True
    )
    if matter_id:
        workflows = [w for w in workflows if w.matter_id == matter_id]
    names = {w.id: w.name for w in workflows}
    runs = await workflow_service.list_recent_runs(db, list(names), limit=limit)
    return WorkflowRunListResponse(
        runs=await _run_summaries(db, runs, caller_id=current_user.user_id, names=names)
    )


@router.post("/from-template/{key}", response_model=WorkflowDetailResponse, status_code=201)
async def create_from_template(
    key: str,
    request: Request,
    matter_id: str | None = Query(None, max_length=36),
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    """A workflow of the caller's own, copied from a template; edit it freely afterwards."""
    template = template_by_key(key)
    if template is None:
        raise NotFoundError("Template not found")
    if matter_id and matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    steps = [s.model_dump(mode="json") for s in template_steps(template)]
    workflow = await workflow_service.create(
        db,
        owner_id=current_user.user_id,
        matter_id=matter_id,
        name=template["name"],
        description=template["description"],
        steps=steps,
        template_key=key,
        change_note=f"Created from the '{template['name']}' template",
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "workflow_created",
        workflow.id,
        matter_id=matter_id,
        template=key,
        steps=len(steps),
        step_kinds=[s["kind"] for s in steps],
    )
    return await _detail(db, workflow, current_user, matter_ids)


# ------------------------------------------------------------------ one workflow


@router.get("/{workflow_id}", response_model=WorkflowDetailResponse)
async def get_workflow(
    workflow_id: str,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    return await _detail(db, workflow, current_user, matter_ids)


@router.patch("/{workflow_id}", response_model=WorkflowDetailResponse)
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdateRequest,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    """Owner only. A change to the steps records a new version; past runs keep theirs."""
    workflow = await _require_owned(db, workflow_id, current_user, matter_ids)
    changed = body.model_dump(exclude_unset=True, exclude={"change_note"})
    if changed == {}:
        raise BadRequestError("Nothing to update.")
    steps = None
    if body.steps is not None:
        _check_step_count(body.steps)
        steps = [s.model_dump(mode="json") for s in body.steps]
        await _check_documents(_fixed_document_ids(steps), current_user, matter_ids)
        await _check_approvers(db, steps, workflow.matter_id, workflow.owner_id)
    before = int(workflow.version or 1)
    workflow = await workflow_service.update(
        db,
        workflow,
        editor_id=current_user.user_id,
        name=body.name,
        description=body.description,
        steps=steps,
        change_note=body.change_note,
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "workflow_updated",
        workflow.id,
        changed=sorted(changed),
        steps=len(workflow.steps or []),
        version=int(workflow.version or 1),
        new_version=int(workflow.version or 1) != before,
    )
    return await _detail(db, workflow, current_user, matter_ids)


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> dict[str, str]:
    """Owner only; the runs and versions go with it. Work product the runs
    created (review tables, analyses, research runs) stays."""
    workflow = await _require_owned(db, workflow_id, current_user, matter_ids)
    await workflow_service.delete(db, workflow)
    await _audit(
        request, current_user, AuditEventType.DATA_DELETION, "workflow_deleted", workflow_id
    )
    return {"status": "deleted", "id": workflow_id}


@router.post("/{workflow_id}/duplicate", response_model=WorkflowDetailResponse, status_code=201)
async def duplicate_workflow(
    workflow_id: str,
    request: Request,
    body: WorkflowDuplicateRequest | None = None,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    """A copy of the workflow owned by the caller (anyone who can read it), at version 1."""
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    body = body or WorkflowDuplicateRequest(name=None, matter_id=None)
    if body.matter_id and body.matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    steps = list(workflow.steps or [])
    await _check_documents(_fixed_document_ids(steps), current_user, matter_ids)
    copy = await workflow_service.duplicate(
        db, workflow, owner_id=current_user.user_id, name=body.name, matter_id=body.matter_id
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "workflow_duplicated",
        copy.id,
        source_workflow_id=workflow.id,
        matter_id=body.matter_id,
        steps=len(steps),
    )
    return await _detail(db, copy, current_user, matter_ids)


@router.post("/{workflow_id}/archive", response_model=WorkflowDetailResponse)
async def archive_workflow(
    workflow_id: str,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    """Owner only. Hidden from the default list and cannot be run; runs and versions stay."""
    workflow = await _require_owned(db, workflow_id, current_user, matter_ids)
    workflow = await workflow_service.set_archived(db, workflow, True)
    await _audit(
        request, current_user, AuditEventType.DATA_MODIFICATION, "workflow_archived", workflow.id
    )
    return await _detail(db, workflow, current_user, matter_ids)


@router.post("/{workflow_id}/unarchive", response_model=WorkflowDetailResponse)
async def unarchive_workflow(
    workflow_id: str,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    workflow = await _require_owned(db, workflow_id, current_user, matter_ids)
    workflow = await workflow_service.set_archived(db, workflow, False)
    await _audit(
        request, current_user, AuditEventType.DATA_MODIFICATION, "workflow_unarchived", workflow.id
    )
    return await _detail(db, workflow, current_user, matter_ids)


# --------------------------------------------------------------------- versions


@router.get("/{workflow_id}/versions", response_model=WorkflowVersionListResponse)
async def list_versions(
    workflow_id: str,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowVersionListResponse:
    """Every version of the steps, newest first."""
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    rows = await workflow_service.list_versions(db, workflow.id)
    return WorkflowVersionListResponse(
        current=int(workflow.version or 1), versions=[_version_summary(r) for r in rows]
    )


@router.get("/{workflow_id}/versions/{version}", response_model=WorkflowVersionDetail)
async def get_version(
    workflow_id: str,
    version: int,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowVersionDetail:
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    row = await workflow_service.get_version(db, workflow.id, version)
    if row is None:
        raise NotFoundError("Version not found")
    return WorkflowVersionDetail(
        **_version_summary(row).model_dump(),
        steps=[WorkflowStep(**s) for s in (row.steps or [])],
    )


@router.post("/{workflow_id}/versions/{version}/restore", response_model=WorkflowDetailResponse)
async def restore_version(
    workflow_id: str,
    version: int,
    request: Request,
    body: WorkflowRestoreRequest | None = None,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowDetailResponse:
    """Owner only. The chosen version's steps become current as a NEW version."""
    workflow = await _require_owned(db, workflow_id, current_user, matter_ids)
    row = await workflow_service.get_version(db, workflow.id, version)
    if row is None:
        raise NotFoundError("Version not found")
    steps = list(row.steps or [])
    await _check_documents(_fixed_document_ids(steps), current_user, matter_ids)
    workflow = await workflow_service.restore_version(
        db,
        workflow,
        row,
        editor_id=current_user.user_id,
        change_note=(body.change_note if body else None),
    )
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "workflow_version_restored",
        workflow.id,
        restored=version,
        version=int(workflow.version or 1),
    )
    return await _detail(db, workflow, current_user, matter_ids)


# ------------------------------------------------------------------------- runs


@router.post("/{workflow_id}/run", response_model=WorkflowRunStarted, status_code=202)
async def run_workflow(
    workflow_id: str,
    request: Request,
    body: WorkflowRunRequest | None = None,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> JSONResponse:
    """Run the workflow over the given documents as one job (202 + a job to poll).

    With no ids and a workflow filed under a matter, every document filed
    under that matter is the input. Owner or matter member may run; every
    input must be readable by the runner.
    """
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    _require_active(workflow)
    if not workflow.steps:
        raise BadRequestError("Add at least one step before running.")
    body = body or WorkflowRunRequest()
    document_ids = list(dict.fromkeys(body.document_ids))
    if not document_ids and workflow.matter_id:
        document_ids = await _default_scope(db, workflow.matter_id)
    if not document_ids:
        raise BadRequestError("Pick at least one document to run the workflow on.")
    await _check_documents(document_ids, current_user, matter_ids)
    await _check_documents(_fixed_document_ids(list(workflow.steps)), current_user, matter_ids)
    user_keys = _require_client(request, current_user)

    run = await workflow_service.create_run(
        db, workflow, runner_id=current_user.user_id, document_ids=document_ids
    )
    return await _submit(
        request,
        db,
        current_user,
        matter_ids,
        workflow,
        run,
        user_keys=user_keys,
        start_index=0,
        action="workflow_run_started",
    )


@router.get("/{workflow_id}/runs", response_model=WorkflowRunListResponse)
async def list_runs(
    workflow_id: str,
    limit: int = Query(50, ge=1, le=200),
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowRunListResponse:
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    runs = await workflow_service.list_runs(db, workflow.id, limit=limit)
    return WorkflowRunListResponse(
        runs=await _run_summaries(
            db, runs, caller_id=current_user.user_id, names={workflow.id: workflow.name}
        )
    )


@router.get("/{workflow_id}/runs/{run_id}", response_model=WorkflowRunDetailResponse)
async def get_run(
    workflow_id: str,
    run_id: str,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowRunDetailResponse:
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    run = await _require_run(db, workflow, run_id)
    return await _run_detail(db, workflow, run, current_user, matter_ids)


@router.post(
    "/{workflow_id}/runs/{run_id}/resume", response_model=WorkflowRunStarted, status_code=202
)
async def resume_run(
    workflow_id: str,
    run_id: str,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> JSONResponse:
    """Re-submit a failed or cancelled run from its first unfinished step, with
    every earlier step's output reloaded. The steps run are the ones the run
    started with, even if the workflow has been edited since."""
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    run = await _require_run(db, workflow, run_id)
    if run.status == RUN_RUNNING:
        raise BadRequestError("This run is still in progress.")
    _require_active(workflow)
    try:
        start_index = await workflow_service.prepare_resume(db, run)
    except ValueError as e:
        raise BadRequestError(str(e))
    return await _continue_run(
        request,
        db,
        current_user,
        matter_ids,
        workflow,
        run,
        start_index=start_index,
        action="workflow_run_resumed",
    )


@router.post(
    "/{workflow_id}/runs/{run_id}/steps/{index}/retry",
    response_model=WorkflowRunStarted,
    status_code=202,
)
async def retry_step(
    workflow_id: str,
    run_id: str,
    index: int,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> JSONResponse:
    """Re-run one step with the same inputs and continue from it. Owner or the runner."""
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    run = await _require_run(db, workflow, run_id)
    if current_user.user_id not in (workflow.owner_id, run.runner_id):
        raise ForbiddenError("Only the workflow's owner or the person who ran it can retry a step")
    _require_active(workflow)
    try:
        start_index = await workflow_service.prepare_retry(db, run, index)
    except ValueError as e:
        raise BadRequestError(str(e))
    return await _continue_run(
        request,
        db,
        current_user,
        matter_ids,
        workflow,
        run,
        start_index=start_index,
        action="workflow_step_retried",
    )


@router.post(
    "/{workflow_id}/runs/{run_id}/steps/{index}/skip",
    response_model=WorkflowRunStarted,
    status_code=202,
)
async def skip_step(
    workflow_id: str,
    run_id: str,
    index: int,
    request: Request,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> JSONResponse:
    """Mark a failed step skipped and continue from the next one. Owner or the runner."""
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    run = await _require_run(db, workflow, run_id)
    if current_user.user_id not in (workflow.owner_id, run.runner_id):
        raise ForbiddenError("Only the workflow's owner or the person who ran it can skip a step")
    _require_active(workflow)
    try:
        start_index = await workflow_service.prepare_skip(
            db, run, index, user_id=current_user.user_id
        )
    except ValueError as e:
        raise BadRequestError(str(e))
    return await _continue_run(
        request,
        db,
        current_user,
        matter_ids,
        workflow,
        run,
        start_index=start_index,
        action="workflow_step_skipped",
    )


async def _require_approver(
    db: AsyncSession, workflow: Workflow, run: WorkflowRun, current_user: TokenData
) -> None:
    """The caller must be one of the awaiting step's resolved approvers."""
    if run.status != RUN_AWAITING_APPROVAL:
        raise BadRequestError("This run is not waiting for an approval.")
    info = (await workflow_service.awaiting_info(db, [run.id])).get(run.id)
    approver_ids = info[1] if info else []
    if current_user.user_id not in approver_ids:
        raise ForbiddenError("You are not an approver for this step.")


@router.post(
    "/{workflow_id}/runs/{run_id}/approve", response_model=WorkflowRunStarted, status_code=202
)
async def approve_run(
    workflow_id: str,
    run_id: str,
    request: Request,
    body: WorkflowDecisionRequest | None = None,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> JSONResponse:
    """An approver's go-ahead: the run continues from the step after the approval."""
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    run = await _require_run(db, workflow, run_id)
    await _require_approver(db, workflow, run, current_user)
    _require_active(workflow)
    note = body.note if body else None
    try:
        start_index = await workflow_service.decide_approval(
            db,
            run,
            approved=True,
            user_id=current_user.user_id,
            user_email=current_user.email,
            note=note,
        )
    except ValueError as e:
        raise BadRequestError(str(e))
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "workflow_run_approved",
        workflow.id,
        run_id=run.id,
        step_index=start_index - 1,
        note_chars=len(note or ""),
    )
    return await _continue_run(
        request,
        db,
        current_user,
        matter_ids,
        workflow,
        run,
        start_index=start_index,
        action="workflow_run_resumed",
    )


@router.post("/{workflow_id}/runs/{run_id}/reject", response_model=WorkflowRunDetailResponse)
async def reject_run(
    workflow_id: str,
    run_id: str,
    request: Request,
    body: WorkflowDecisionRequest | None = None,
    current_user: TokenData = require_permission("contracts.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> WorkflowRunDetailResponse:
    """An approver's refusal: the run ends as rejected; later steps are skipped."""
    workflow = await _require_readable(db, workflow_id, current_user, matter_ids)
    run = await _require_run(db, workflow, run_id)
    await _require_approver(db, workflow, run, current_user)
    note = body.note if body else None
    try:
        index = await workflow_service.decide_approval(
            db,
            run,
            approved=False,
            user_id=current_user.user_id,
            user_email=current_user.email,
            note=note,
        )
    except ValueError as e:
        raise BadRequestError(str(e))
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_MODIFICATION,
        "workflow_run_rejected",
        workflow.id,
        run_id=run.id,
        step_index=index,
        note_chars=len(note or ""),
    )
    await db.refresh(run)
    return await _run_detail(db, workflow, run, current_user, matter_ids)
