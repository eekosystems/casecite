"""Workflows: persist them and their versions, start runs, execute the steps in order.

A run is one background job. Steps execute one after another; each step's
status, output and error are written as they happen, so a run that stops can
be read in full and resumed from the step that failed with every earlier
output reloaded. A step with a condition runs only when an earlier output
satisfies it; an approval step parks the run (``awaiting_approval``) and ends
the job, and an approver's decision re-submits it from the next step.
Permission checks happen once, at the top of the run, in the router; every
step then reads documents as the runner (owner or matter member) and calls
the service layer directly.

Every change to a workflow's steps is recorded as a version, and a run
records the version it executed. Access follows the other work-product
services: the owner, or a member of the workflow's matter, may read and run
it; the owner alone edits, archives or deletes it; anyone else is told it
does not exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.schemas import WorkflowStepKind
from app.models.workflows import (
    RUN_AWAITING_APPROVAL,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_REJECTED,
    RUN_RESUMABLE,
    RUN_RUNNING,
    STEP_AWAITING,
    STEP_COMPLETED,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_REJECTED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SKIPPED_CONDITION,
    Workflow,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowVersion,
)
from app.services import matters as matters_service
from app.services.job_queue import job_manager
from app.services.llm_clients import make_openai
from app.services.workflows import conditions, notifications
from app.services.workflows.outputs import cap_output
from app.services.workflows.steps import ADAPTERS, StepContext, StepError

logger = logging.getLogger(__name__)

APPROVAL_KIND = WorkflowStepKind.APPROVAL.value


class WorkflowError(Exception):
    """The run stopped at a step. Not a RuntimeError, so the job queue never
    retries a whole workflow because one step failed."""


def _steps_equal(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


class WorkflowService:
    # ------------------------------------------------------------------ access

    @staticmethod
    def can_read(workflow: Workflow, user_id: str, accessible_matter_ids: set[str] | None) -> bool:
        if workflow.owner_id == user_id:
            return True
        return bool(
            accessible_matter_ids
            and workflow.matter_id
            and workflow.matter_id in accessible_matter_ids
        )

    @staticmethod
    def role_for(workflow: Workflow, user_id: str) -> str:
        return "owner" if workflow.owner_id == user_id else "member"

    @staticmethod
    def _llm_client(user_keys: Any) -> Any:
        """The runner's OpenAI-compatible client (BYOK key, else the instance key)."""
        key = user_keys.openai if user_keys and getattr(user_keys, "openai", None) else None
        client = make_openai(key or settings.openai_api_key, async_=True)
        if client is None:
            raise ValueError(
                "An OpenAI API key or a custom LLM endpoint is required. Configure one in Settings."
            )
        return client

    @staticmethod
    def caps() -> dict[str, int]:
        return {
            "max_steps": max(1, int(settings.workflow_max_steps)),
            "max_documents_per_step": max(1, int(settings.workflow_max_documents_per_step)),
            "max_citations_per_step": max(1, int(settings.workflow_max_citations_per_step)),
            "max_output_bytes": max(4096, int(settings.workflow_max_output_bytes)),
        }

    # --------------------------------------------------------------- workflows

    @staticmethod
    def _version_row(
        workflow: Workflow, created_by: str, change_note: str | None
    ) -> WorkflowVersion:
        return WorkflowVersion(
            id=str(uuid.uuid4()),
            workflow_id=workflow.id,
            version=int(workflow.version or 1),
            name=workflow.name,
            description=workflow.description,
            steps=[dict(s) for s in (workflow.steps or [])],
            created_by=created_by,
            change_note=(change_note or "").strip()[:500] or None,
        )

    async def create(
        self,
        session: Any,
        *,
        owner_id: str,
        matter_id: str | None,
        name: str,
        description: str | None,
        steps: list[dict[str, Any]],
        template_key: str | None = None,
        change_note: str | None = None,
    ) -> Workflow:
        workflow = Workflow(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            matter_id=matter_id,
            name=name.strip(),
            description=(description or "").strip() or None,
            template_key=template_key,
            steps=steps,
            version=1,
        )
        session.add(workflow)
        session.add(self._version_row(workflow, owner_id, change_note or "Created"))
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def list_for_user(
        self,
        session: Any,
        user_id: str,
        accessible_matter_ids: set[str] | None,
        *,
        include_archived: bool = False,
    ) -> list[Workflow]:
        clause = Workflow.owner_id == user_id
        if accessible_matter_ids:
            clause = clause | Workflow.matter_id.in_(sorted(accessible_matter_ids))
        query = select(Workflow).where(clause)
        if not include_archived:
            query = query.where(Workflow.archived_at.is_(None))
        rows = await session.execute(query.order_by(Workflow.updated_at.desc()))
        return list(rows.scalars().all())

    async def get_accessible(
        self,
        session: Any,
        workflow_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
    ) -> Workflow | None:
        workflow: Workflow | None = await session.get(Workflow, workflow_id)
        if workflow is None or not self.can_read(workflow, user_id, accessible_matter_ids):
            return None
        return workflow

    async def update(
        self,
        session: Any,
        workflow: Workflow,
        *,
        editor_id: str,
        name: str | None = None,
        description: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        change_note: str | None = None,
    ) -> Workflow:
        """Apply the change; a change to the steps records a new version."""
        if name is not None:
            workflow.name = name.strip()
        if description is not None:
            workflow.description = description.strip() or None
        if steps is not None and not _steps_equal(steps, list(workflow.steps or [])):
            workflow.steps = steps
            workflow.version = int(workflow.version or 1) + 1
            session.add(self._version_row(workflow, editor_id, change_note))
        workflow.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def list_versions(self, session: Any, workflow_id: str) -> list[WorkflowVersion]:
        rows = await session.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
        )
        return list(rows.scalars().all())

    async def get_version(
        self, session: Any, workflow_id: str, version: int
    ) -> WorkflowVersion | None:
        rows = await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version
            )
        )
        row: WorkflowVersion | None = rows.scalars().first()
        return row

    async def restore_version(
        self,
        session: Any,
        workflow: Workflow,
        snapshot: WorkflowVersion,
        *,
        editor_id: str,
        change_note: str | None,
    ) -> Workflow:
        """Make ``snapshot``'s steps current as a NEW version (history is never rewritten)."""
        note = change_note or f"Restored version {snapshot.version}"
        return await self.update(
            session,
            workflow,
            editor_id=editor_id,
            steps=[dict(s) for s in (snapshot.steps or [])],
            change_note=note,
        )

    async def duplicate(
        self,
        session: Any,
        workflow: Workflow,
        *,
        owner_id: str,
        name: str | None,
        matter_id: str | None,
    ) -> Workflow:
        """A copy owned by ``owner_id`` with the same steps, at version 1."""
        return await self.create(
            session,
            owner_id=owner_id,
            matter_id=matter_id,
            name=(name or f"{workflow.name} (copy)")[:300],
            description=workflow.description,
            steps=[dict(s) for s in (workflow.steps or [])],
            template_key=workflow.template_key,
            change_note=f"Duplicated from '{workflow.name}' version {workflow.version}",
        )

    async def set_archived(self, session: Any, workflow: Workflow, archived: bool) -> Workflow:
        workflow.archived_at = datetime.utcnow() if archived else None
        workflow.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(workflow)
        return workflow

    async def delete(self, session: Any, workflow: Workflow) -> None:
        # Explicit so SQLite without foreign-key enforcement also drops the runs.
        run_ids = select(WorkflowRun.id).where(WorkflowRun.workflow_id == workflow.id)
        await session.execute(delete(WorkflowStepRun).where(WorkflowStepRun.run_id.in_(run_ids)))
        await session.execute(delete(WorkflowRun).where(WorkflowRun.workflow_id == workflow.id))
        await session.execute(
            delete(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow.id)
        )
        await session.delete(workflow)
        await session.commit()

    async def run_stats(self, session: Any, workflow_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Per workflow: how many runs, and when and how the latest one ended."""
        if not workflow_ids:
            return {}
        rows = await session.execute(
            select(WorkflowRun.workflow_id, WorkflowRun.status, WorkflowRun.created_at)
            .where(WorkflowRun.workflow_id.in_(workflow_ids))
            .order_by(WorkflowRun.created_at.desc())
        )
        stats: dict[str, dict[str, Any]] = {}
        for workflow_id, status, created_at in rows.all():
            entry = stats.setdefault(
                str(workflow_id),
                {"run_count": 0, "last_run_at": created_at, "last_run_status": status},
            )
            entry["run_count"] += 1
        return stats

    # -------------------------------------------------------------------- runs

    async def create_run(
        self,
        session: Any,
        workflow: Workflow,
        *,
        runner_id: str,
        document_ids: list[str],
    ) -> WorkflowRun:
        """A run with the workflow's steps frozen and one pending step run per step."""
        steps = [dict(s) for s in (workflow.steps or [])]
        run = WorkflowRun(
            id=str(uuid.uuid4()),
            workflow_id=workflow.id,
            runner_id=runner_id,
            matter_id=workflow.matter_id,
            steps=steps,
            workflow_version=int(workflow.version or 1),
            input_document_ids=list(dict.fromkeys(document_ids)),
            status=RUN_RUNNING,
        )
        session.add(run)
        for index, step in enumerate(steps):
            session.add(
                WorkflowStepRun(
                    run_id=run.id,
                    index=index,
                    name=str(step["name"]),
                    kind=str(step["kind"]),
                    status=STEP_PENDING,
                    input_document_ids=[],
                    produced_ids={},
                )
            )
        workflow.last_run_at = datetime.utcnow()
        await session.commit()
        await session.refresh(run)
        return run

    async def set_job(self, session: Any, run_id: str, job_id: str) -> None:
        await session.execute(
            update(WorkflowRun).where(WorkflowRun.id == run_id).values(job_id=job_id)
        )
        await session.commit()

    async def list_runs(self, session: Any, workflow_id: str, limit: int = 50) -> list[WorkflowRun]:
        rows = await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id)
            .order_by(WorkflowRun.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        return list(rows.scalars().all())

    async def list_recent_runs(
        self, session: Any, workflow_ids: list[str], *, limit: int = 20
    ) -> list[WorkflowRun]:
        """The newest runs across ``workflow_ids``."""
        if not workflow_ids:
            return []
        rows = await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id.in_(workflow_ids))
            .order_by(WorkflowRun.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        return list(rows.scalars().all())

    async def get_run(self, session: Any, workflow_id: str, run_id: str) -> WorkflowRun | None:
        run: WorkflowRun | None = await session.get(WorkflowRun, run_id)
        if run is None or run.workflow_id != workflow_id:
            return None
        return run

    async def step_runs(self, session: Any, run_id: str) -> list[WorkflowStepRun]:
        rows = await session.execute(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.run_id == run_id)
            .order_by(WorkflowStepRun.index)
        )
        return list(rows.scalars().all())

    async def completed_counts(self, session: Any, run_ids: list[str]) -> dict[str, int]:
        if not run_ids:
            return {}
        rows = await session.execute(
            select(WorkflowStepRun.run_id, WorkflowStepRun.status).where(
                WorkflowStepRun.run_id.in_(run_ids)
            )
        )
        counts: dict[str, int] = {}
        for run_id, status in rows.all():
            counts.setdefault(str(run_id), 0)
            if status in STEP_DONE:
                counts[str(run_id)] += 1
        return counts

    async def awaiting_info(
        self, session: Any, run_ids: list[str]
    ) -> dict[str, tuple[str, list[str]]]:
        """For runs parked at an approval: run id -> (step name, approver ids)."""
        if not run_ids:
            return {}
        rows = await session.execute(
            select(WorkflowStepRun).where(
                WorkflowStepRun.run_id.in_(run_ids), WorkflowStepRun.status == STEP_AWAITING
            )
        )
        info: dict[str, tuple[str, list[str]]] = {}
        for row in rows.scalars().all():
            data = (row.output or {}).get("data") or {}
            info[str(row.run_id)] = (row.name, [str(i) for i in data.get("approver_ids") or []])
        return info

    async def resolve_approvers(
        self, session: Any, workflow: Workflow, spec: str | list[str]
    ) -> list[str]:
        """The user ids an approval step's ``approvers`` names, at this moment."""
        if isinstance(spec, list):
            return [str(s) for s in spec]
        if spec == "any_member" and workflow.matter_id:
            members = await matters_service.list_members(session, workflow.matter_id)
            ids = [str(m.user_id) for m in members]
            return ids or [workflow.owner_id]
        return [workflow.owner_id]

    # ----------------------------------------------------------- run controls

    async def _reset_from(self, session: Any, run: WorkflowRun, start: int) -> None:
        await session.execute(
            update(WorkflowStepRun)
            .where(WorkflowStepRun.run_id == run.id, WorkflowStepRun.index >= start)
            .values(status=STEP_PENDING, output=None, error=None, started_at=None, finished_at=None)
        )
        run.status = RUN_RUNNING
        run.error = None
        run.completed_at = None
        run.job_id = None
        run.resumed_count = int(run.resumed_count or 0) + 1
        await session.commit()

    async def prepare_resume(self, session: Any, run: WorkflowRun) -> int:
        """Reset the failed step and everything after it to pending; the index to resume at.

        Raises ValueError when the run has nothing to resume (still running,
        awaiting approval, or completed).
        """
        if run.status not in RUN_RESUMABLE:
            raise ValueError("Only a failed or cancelled run can be resumed")
        rows = await self.step_runs(session, run.id)
        start = next(
            (
                r.index
                for r in rows
                if r.status in (STEP_FAILED, STEP_SKIPPED, STEP_PENDING, STEP_RUNNING)
            ),
            None,
        )
        if start is None:
            raise ValueError("Every step of this run has completed")
        await self._reset_from(session, run, start)
        return start

    async def prepare_retry(self, session: Any, run: WorkflowRun, index: int) -> int:
        """Re-run step ``index`` (whatever its state) and everything after it."""
        if run.status in (RUN_RUNNING, RUN_AWAITING_APPROVAL):
            raise ValueError("The run is in progress; wait for it to finish or cancel it")
        if index < 0 or index >= len(run.steps or []):
            raise ValueError("No such step")
        await self._reset_from(session, run, index)
        return index

    async def prepare_skip(
        self, session: Any, run: WorkflowRun, index: int, *, user_id: str
    ) -> int:
        """Mark the failed step ``index`` skipped and continue from the next one."""
        if run.status not in RUN_RESUMABLE:
            raise ValueError("Only a failed or cancelled run can skip a step")
        rows = await self.step_runs(session, run.id)
        target = next((r for r in rows if r.index == index), None)
        if target is None:
            raise ValueError("No such step")
        if target.status not in (STEP_FAILED, STEP_PENDING, STEP_SKIPPED):
            raise ValueError("Only a failed or not-yet-run step can be skipped")
        await self._reset_from(session, run, index)
        await session.execute(
            update(WorkflowStepRun)
            .where(WorkflowStepRun.run_id == run.id, WorkflowStepRun.index == index)
            .values(
                status=STEP_SKIPPED,
                output={
                    "summary": "Skipped by the user; later steps do not see this step's output.",
                    "data": {"skipped_by": user_id},
                    "document_ids": [],
                    "produced": {},
                },
                finished_at=datetime.utcnow(),
            )
        )
        await session.commit()
        return index + 1

    async def decide_approval(
        self,
        session: Any,
        run: WorkflowRun,
        *,
        approved: bool,
        user_id: str,
        user_email: str | None,
        note: str | None,
    ) -> int:
        """Record the decision on the awaiting step; the index to continue from (approve)."""
        if run.status != RUN_AWAITING_APPROVAL:
            raise ValueError("This run is not waiting for an approval")
        rows = await self.step_runs(session, run.id)
        step = next((r for r in rows if r.status == STEP_AWAITING), None)
        if step is None:
            raise ValueError("This run is not waiting for an approval")
        data = dict((step.output or {}).get("data") or {})
        data.update(
            {
                "decision": "approved" if approved else "rejected",
                "decided_by": user_id,
                "decided_by_email": user_email,
                "decided_at": datetime.utcnow().isoformat(),
                "note": (note or "").strip()[:2000] or None,
            }
        )
        title = str(data.get("title") or step.name)
        who = user_email or user_id
        summary = f"{'Approved' if approved else 'Rejected'} by {who}: {title}"
        await session.execute(
            update(WorkflowStepRun)
            .where(WorkflowStepRun.run_id == run.id, WorkflowStepRun.index == step.index)
            .values(
                status=STEP_COMPLETED if approved else STEP_REJECTED,
                output={**(step.output or {}), "summary": summary, "data": data},
                finished_at=datetime.utcnow(),
            )
        )
        if approved:
            run.status = RUN_RUNNING
            run.error = None
            run.job_id = None
            await session.commit()
            return step.index + 1
        await session.commit()
        await self._skip_rest(run.id, step.index + 1)
        error = f"Rejected at step {step.index + 1} '{step.name}' by {who}"
        if data.get("note"):
            error += f": {data['note']}"
        await self._finish(run.id, RUN_REJECTED, error[:1000])
        await self._notify("rejected", run.id, note=data.get("note"))
        return step.index

    # --------------------------------------------------------------- execution

    async def _mark_step(self, run_id: str, index: int, status: str, **values: Any) -> None:
        now = datetime.utcnow()
        if status == STEP_RUNNING:
            values["started_at"] = now
        elif status in (STEP_COMPLETED, STEP_FAILED, STEP_SKIPPED_CONDITION):
            values["finished_at"] = now
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(WorkflowStepRun)
                .where(WorkflowStepRun.run_id == run_id, WorkflowStepRun.index == index)
                .values(status=status, **values)
            )
            await session.commit()

    async def _skip_rest(self, run_id: str, from_index: int) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(WorkflowStepRun)
                .where(
                    WorkflowStepRun.run_id == run_id,
                    WorkflowStepRun.index >= from_index,
                    WorkflowStepRun.status == STEP_PENDING,
                )
                .values(status=STEP_SKIPPED)
            )
            await session.commit()

    async def _finish(self, run_id: str, status: str, error: str | None = None) -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(status=status, error=error, completed_at=datetime.utcnow())
            )
            await session.commit()

    async def _notify(
        self,
        event: str,
        run_id: str,
        *,
        extra_user_ids: list[str] | None = None,
        note: str | None = None,
    ) -> None:
        """Hand the event to the notifier; never lets a mail problem touch the run."""
        try:
            await notifications.notify_run_event(
                event, run_id, extra_user_ids=extra_user_ids, note=note
            )
        except Exception as e:  # the notifier already swallows; belt and braces
            logger.warning("Workflow notification failed (%s): %s", event, type(e).__name__)

    async def _fail(self, run_id: str, index: int, total: int, name: str, message: str) -> None:
        await self._mark_step(run_id, index, STEP_FAILED, error=message[:1000])
        await self._skip_rest(run_id, index + 1)
        await self._finish(
            run_id, RUN_FAILED, f"Step {index + 1}/{total} '{name}' failed: {message}"
        )
        await self._notify("failed", run_id)

    async def _park_for_approval(
        self, run_id: str, index: int, step: dict[str, Any], workflow: Workflow | None
    ) -> dict[str, Any]:
        """Record the awaiting step with its resolved approvers; the run waits."""
        params = step.get("params") or {}
        spec = params.get("approvers") or "owner"
        async with AsyncSessionLocal() as session:
            wf = workflow
            if wf is None:
                run = await session.get(WorkflowRun, run_id)
                wf = await session.get(Workflow, run.workflow_id) if run else None
            approver_ids = await self.resolve_approvers(session, wf, spec) if wf else []
        data = {
            "title": params.get("title") or step.get("name"),
            "instructions": params.get("instructions"),
            "approvers": params.get("approvers") or "owner",
            "approver_ids": approver_ids,
        }
        output = {
            "summary": f"Waiting for approval: {data['title']}",
            "data": data,
            "document_ids": [],
            "produced": {},
        }
        await self._mark_step(run_id, index, STEP_AWAITING, output=output)
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(status=RUN_AWAITING_APPROVAL, error=None, job_id=None)
            )
            await session.commit()
        await self._notify("awaiting_approval", run_id, extra_user_ids=approver_ids)
        return output

    async def _load_run(self, run_id: str) -> tuple[WorkflowRun, Workflow | None, dict[str, Any]]:
        async with AsyncSessionLocal() as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                raise LookupError("Workflow run not found")
            workflow = await session.get(Workflow, run.workflow_id)
            outputs = {
                r.name: r.output
                for r in await self.step_runs(session, run_id)
                if r.status in STEP_DONE and r.output
            }
            return run, workflow, outputs

    def _context(
        self,
        run: WorkflowRun,
        workflow: Workflow | None,
        outputs: dict[str, Any],
        *,
        user_id: str,
        accessible_matter_ids: set[str] | None,
        user_keys: Any,
        client: Any,
    ) -> StepContext:
        caps = self.caps()
        return StepContext(
            run_id=run.id,
            workflow_name=workflow.name if workflow else "Workflow",
            user_id=user_id,
            accessible_matter_ids=accessible_matter_ids,
            user_keys=user_keys,
            client=client,
            matter_id=run.matter_id,
            input_document_ids=list(run.input_document_ids or []),
            outputs=outputs,
            max_documents=caps["max_documents_per_step"],
            max_citations=caps["max_citations_per_step"],
        )

    async def _run_step(
        self, run_id: str, index: int, total: int, step: dict[str, Any], ctx: StepContext
    ) -> dict[str, Any]:
        """One step through its adapter; persists the outcome; raises WorkflowError on failure."""
        name = str(step["name"])
        adapter = ADAPTERS.get(str(step["kind"]))
        if adapter is None:
            await self._fail(run_id, index, total, name, f"Unknown step kind {step['kind']}")
            raise WorkflowError(f"Unknown step kind {step['kind']}")
        try:
            output = await adapter(step, ctx)
        except StepError as e:
            await self._fail(run_id, index, total, name, str(e))
            raise WorkflowError(f"Step '{name}' failed: {e}") from e
        except asyncio.CancelledError:
            await self._mark_step(run_id, index, STEP_FAILED, error="Cancelled")
            await self._skip_rest(run_id, index + 1)
            await self._finish(run_id, RUN_CANCELLED, "Cancelled before completion")
            raise
        except Exception as e:  # a library's own exception class: still one failed step
            logger.error("Workflow run %s step %s failed: %s", run_id, name, e, exc_info=True)
            await self._fail(run_id, index, total, name, f"{type(e).__name__}: {e}")
            raise WorkflowError(f"Step '{name}' failed unexpectedly") from e
        as_json = cap_output(output.as_json(), self.caps()["max_output_bytes"])
        await self._mark_step(
            run_id,
            index,
            STEP_COMPLETED,
            output=as_json,
            input_document_ids=list(output.document_ids),
            produced_ids={k: list(v) for k, v in output.produced.items()},
        )
        return as_json

    async def execute(
        self,
        *,
        run_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
        user_keys: Any,
        start_index: int = 0,
    ) -> dict[str, Any]:
        """Run the steps from ``start_index``; the job body. Persists every outcome.

        Returns the run's result; ``status`` is ``completed`` or
        ``awaiting_approval`` (the job ends while an approver decides).
        """
        run, workflow, outputs = await self._load_run(run_id)
        steps = [dict(s) for s in (run.steps or [])]
        total = len(steps)
        try:
            client = self._llm_client(user_keys)
        except ValueError as e:
            await self._finish(run_id, RUN_FAILED, str(e))
            raise WorkflowError(str(e)) from e
        ctx = self._context(
            run,
            workflow,
            outputs,
            user_id=user_id,
            accessible_matter_ids=accessible_matter_ids,
            user_keys=user_keys,
            client=client,
        )
        produced: dict[str, list[str]] = {}
        done = sum(1 for _ in outputs)

        for index in range(start_index, total):
            step = steps[index]
            name = str(step["name"])
            label = f"Step {index + 1}/{total} ({name})"
            fraction = index / total if total else 0.0

            def report(message: str, _label: str = label, _fraction: float = fraction) -> None:
                job_manager.report_progress(f"{_label}: {message}", _fraction)

            ctx.progress = report
            condition = step.get("condition")
            if condition:
                verdict = conditions.evaluate(condition, ctx.outputs)
                if not verdict["result"]:
                    output = {
                        "summary": "Skipped: its condition was not met.",
                        "data": {"condition": verdict},
                        "document_ids": [],
                        "produced": {},
                    }
                    await self._mark_step(run_id, index, STEP_SKIPPED_CONDITION, output=output)
                    ctx.outputs[name] = output
                    job_manager.report_progress(f"{label}: skipped (condition not met)", fraction)
                    done += 1
                    continue
            if str(step["kind"]) == APPROVAL_KIND:
                job_manager.report_progress(f"{label}: waiting for approval", fraction)
                await self._park_for_approval(run_id, index, step, workflow)
                return {
                    "run_id": run_id,
                    "status": RUN_AWAITING_APPROVAL,
                    "steps": total,
                    "completed_steps": done,
                    "documents": len(ctx.input_document_ids),
                    "produced": {k: len(v) for k, v in produced.items()},
                    "awaiting_step": name,
                }
            await self._mark_step(run_id, index, STEP_RUNNING)
            job_manager.report_progress(f"{label}: starting", fraction)
            as_json = await self._run_step(run_id, index, total, step, ctx)
            ctx.outputs[name] = as_json
            done += 1
            for key, ids in (as_json.get("produced") or {}).items():
                produced.setdefault(key, []).extend(ids)

        await self._finish(run_id, RUN_COMPLETED)
        job_manager.report_progress("Workflow complete", 1.0)
        await self._notify("completed", run_id)
        return {
            "run_id": run_id,
            "status": RUN_COMPLETED,
            "steps": total,
            "completed_steps": total,
            "documents": len(ctx.input_document_ids),
            "produced": {k: len(v) for k, v in produced.items()},
        }


workflow_service = WorkflowService()
