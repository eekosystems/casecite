"""Deep research endpoints: draft a plan, run it as one job, read past runs.

``POST /deep-research/plan`` answers synchronously with the sub-questions the
planner proposes, so the user can edit them. ``POST /deep-research/run`` takes
the (edited) plan, persists the run and executes it as one background job
(202 + a job to poll). Runs are readable by their owner and by members of
their matter; anyone else gets 404, never 403. The document scope follows
research chat: an explicit document filter wins, else the active matter's
documents, else everything the caller may read.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.exceptions import BadRequestError, NotFoundError, UpstreamError
from app.models.deep_research import DeepResearchRun
from app.models.schemas import (
    DeepResearchListResponse,
    DeepResearchPlanRequest,
    DeepResearchPlanResponse,
    DeepResearchRunDetail,
    DeepResearchRunRequest,
    DeepResearchRunStarted,
    DeepResearchRunSummary,
    DeepResearchScope,
    DeepResearchSubQuestion,
)
from app.routers._matter_deps import accessible_matter_ids
from app.services import matters as matters_service
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.deep_research import PlanningError, deep_research_service
from app.services.documents import document_service
from app.services.job_queue import RETRY_POLICIES, job_manager
from app.services.llm_clients import utility_model
from app.services.permissions import require_permission
from app.services.user_keys import UserAPIKeys
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deep-research", tags=["deep-research"])


async def _audit(
    request: Request,
    current_user: TokenData,
    event_type: AuditEventType,
    action: str,
    run_id: str,
    **details: Any,
) -> None:
    """Ids and counts only — never the question, an answer or a quote."""
    await audit_service.log_event(
        event_type=event_type,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="deep_research_run",
        resource_id=run_id,
        ip_address=get_client_ip(request),
        details={"action": action, **details},
    )


async def _resolve_scope(
    db: AsyncSession,
    body: DeepResearchPlanRequest,
    current_user: TokenData,
    matter_ids: set[str],
) -> tuple[list[str] | None, int]:
    """(document ids to search or None for everything readable, count in scope)."""
    if body.matter_id and body.matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    document_ids: list[str] | None = None
    if body.document_filter and not body.document_filter.search_all:
        f = body.document_filter
        from_matters: list[str] = []
        if f.matter_ids:
            # Several matters picked in the workspace: their documents, for
            # matters the caller belongs to (personal = their unfiled files).
            personal = await matters_service.get_personal_matter_id(db, current_user.user_id)
            from_matters = document_service.document_ids_in_matters(
                f.matter_ids, current_user.user_id, matter_ids, personal
            )
            document_ids = from_matters
        if f.document_ids or f.folder_paths or f.sources or f.doc_types:
            picked = await document_service.get_documents_by_filter(
                user_id=current_user.user_id,
                document_ids=f.document_ids,
                folder_paths=f.folder_paths,
                sources=f.sources,
                doc_types=f.doc_types,
                include_subfolders=f.include_subfolders,
                accessible_matter_ids=matter_ids,
            )
            document_ids = list(dict.fromkeys([*from_matters, *picked]))
    if document_ids is None and body.matter_id:
        matter = await matters_service.get_matter(db, body.matter_id)
        if matter is None:
            raise NotFoundError("Matter not found")
        personal_owner = str(matter.owner_id) if matter.is_personal else None
        document_ids = [
            d.id for d in document_service.documents_in_matter(body.matter_id, personal_owner)
        ]
    if document_ids is not None:
        return document_ids, len(document_ids)
    readable = await document_service.get_documents_by_filter(
        user_id=current_user.user_id, accessible_matter_ids=matter_ids
    )
    return None, len(readable)


def _scope_response(
    matter_id: str | None, document_ids: list[str] | None, count: int
) -> DeepResearchScope:
    return DeepResearchScope(
        matter_id=matter_id,
        document_ids=document_ids,
        documents_in_scope=count,
        case_law_available=deep_research_service.tools_available()["case_law"],
        citation_check_available=deep_research_service.citation_check_available(),
    )


def _summary(run: DeepResearchRun, caller_id: str) -> DeepResearchRunSummary:
    result = run.result or {}
    return DeepResearchRunSummary(
        id=run.id,
        owner_id=run.owner_id,
        matter_id=run.matter_id,
        role=deep_research_service.role_for(run, caller_id),
        question=run.question,
        status=run.status,
        sub_questions=len(run.plan or []),
        sources_cited=len(result.get("sources") or []) if result else None,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def _detail(run: DeepResearchRun, caller_id: str) -> DeepResearchRunDetail:
    return DeepResearchRunDetail(
        **_summary(run, caller_id).model_dump(),
        plan=[DeepResearchSubQuestion(**s) for s in (run.plan or [])],
        scope=dict(run.scope or {}),
        job_id=run.job_id,
        result=run.result,
        error=run.error,
    )


async def _require_readable(
    db: AsyncSession, run_id: str, current_user: TokenData, matter_ids: set[str]
) -> DeepResearchRun:
    run = await deep_research_service.get_accessible(db, run_id, current_user.user_id, matter_ids)
    if run is None:
        raise NotFoundError("Research run not found")
    return run


# ------------------------------------------------------------------- planning


@router.post("/plan", response_model=DeepResearchPlanResponse)
async def plan_deep_research(
    body: DeepResearchPlanRequest,
    request: Request,
    current_user: TokenData = require_permission("chat.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> DeepResearchPlanResponse:
    """Propose the sub-questions and tools for a question; nothing is searched yet."""
    document_ids, count = await _resolve_scope(db, body, current_user, matter_ids)
    user_keys = UserAPIKeys.from_request(request, user_id=current_user.user_id)
    try:
        plan = await deep_research_service.plan(user_keys, body.question)
    except ValueError as e:
        raise BadRequestError(str(e))
    except PlanningError as e:
        raise UpstreamError(str(e))
    return DeepResearchPlanResponse(
        question=body.question.strip(),
        plan=[DeepResearchSubQuestion(**s) for s in plan],
        scope=_scope_response(body.matter_id, document_ids, count),
        caps=deep_research_service.caps(),
        model=utility_model(),
    )


@router.post("/run", response_model=DeepResearchRunStarted, status_code=202)
async def run_deep_research(
    body: DeepResearchRunRequest,
    request: Request,
    current_user: TokenData = require_permission("chat.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> JSONResponse:
    """Persist the approved plan and run it as one job (202 + a job to poll)."""
    caps = deep_research_service.caps()
    if len(body.plan) > caps["max_subquestions"]:
        raise BadRequestError(
            f"A plan may have at most {caps['max_subquestions']} sub-questions on this server."
        )
    document_ids, count = await _resolve_scope(db, body, current_user, matter_ids)
    user_keys = UserAPIKeys.from_request(request, user_id=current_user.user_id)
    try:
        deep_research_service._llm_client(user_keys)  # fail now, not inside the job
    except ValueError as e:
        raise BadRequestError(str(e))

    plan = [s.model_dump(mode="json") for s in body.plan]
    scope = {
        "matter_id": body.matter_id,
        "document_ids": document_ids,
        "documents_in_scope": count,
    }
    run = await deep_research_service.create_run(
        db,
        owner_id=current_user.user_id,
        matter_id=body.matter_id,
        question=body.question,
        plan=plan,
        scope=scope,
    )
    user_id, user_email = current_user.user_id, current_user.email
    scoped_matters = sorted(matter_ids) or None
    await _audit(
        request,
        current_user,
        AuditEventType.DATA_ACCESS,
        "deep_research_started",
        run.id,
        matter_id=body.matter_id,
        sub_questions=len(plan),
        tools=sorted({t for s in plan for t in s["tools"]}),
        documents_in_scope=count,
    )

    async def do_work() -> dict[str, Any]:
        result = await deep_research_service.run(
            run_id=run.id,
            user_id=user_id,
            user_keys=user_keys,
            document_ids=document_ids,
            matter_ids=scoped_matters,
        )
        await audit_service.log_event(
            event_type=AuditEventType.DATA_ACCESS,
            user_id=user_id,
            user_email=user_email,
            resource_type="deep_research_run",
            resource_id=run.id,
            details={
                "action": "deep_research_completed",
                **deep_research_service.summary_counts(result),
            },
        )
        return result

    job_id = await job_manager.submit(
        do_work,
        user_id=user_id,
        endpoint="/deep-research/run",
        retry_policy=RETRY_POLICIES["ai_analysis"],
    )
    await deep_research_service.set_job(db, run.id, job_id)
    payload = DeepResearchRunStarted(
        run_id=run.id,
        job_id=job_id,
        poll_url=f"/api/v1/jobs/{job_id}",
        sub_questions=len(plan),
    )
    return JSONResponse(status_code=202, content=payload.model_dump())


# ----------------------------------------------------------------- past runs


@router.get("", response_model=DeepResearchListResponse)
async def list_deep_research(
    matter_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: TokenData = require_permission("chat.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> DeepResearchListResponse:
    """Runs the caller owns or can read through a matter, newest first."""
    if matter_id and matter_id not in matter_ids:
        raise NotFoundError("Matter not found")
    runs = await deep_research_service.list_for_user(
        db, current_user.user_id, matter_ids, matter_id=matter_id, limit=limit
    )
    return DeepResearchListResponse(runs=[_summary(r, current_user.user_id) for r in runs])


@router.get("/{run_id}", response_model=DeepResearchRunDetail)
async def get_deep_research(
    run_id: str,
    current_user: TokenData = require_permission("chat.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> DeepResearchRunDetail:
    run = await _require_readable(db, run_id, current_user, matter_ids)
    return _detail(run, current_user.user_id)


@router.delete("/{run_id}")
async def delete_deep_research(
    run_id: str,
    request: Request,
    current_user: TokenData = require_permission("chat.use"),
    db: AsyncSession = Depends(get_db),
    matter_ids: set[str] = Depends(accessible_matter_ids),
) -> dict[str, str]:
    """Owner only; a member or stranger gets 404."""
    run = await _require_readable(db, run_id, current_user, matter_ids)
    if run.owner_id != current_user.user_id:
        raise NotFoundError("Research run not found")
    await deep_research_service.delete(db, run)
    await _audit(
        request, current_user, AuditEventType.DATA_DELETION, "deep_research_deleted", run_id
    )
    return {"status": "deleted", "id": run_id}
