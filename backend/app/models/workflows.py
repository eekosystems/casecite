"""Workflows — saved, repeatable sequences of the platform's own operations.

A ``Workflow`` is a named, ordered, linear list of typed steps (extract
fields, analyse a contract, compare contracts, draft, research, check
citations, wait for an approval). Each step names the service operation it
runs and the parameters it runs with; nothing in a workflow is code. A
``WorkflowRun`` is one execution over a set of input documents, and a
``WorkflowStepRun`` is one step of that execution with its status and output,
written as it happens so a run that stops can be read, and resumed from the
step that failed. A ``WorkflowVersion`` is a snapshot of the steps every time
they change, so a run can always say exactly which definition it executed.

Ownership follows the other work-product tables: the owner edits and deletes;
members of the workflow's matter may read it and run it; nobody else sees
that it exists. SQLAlchemy 2.0 ``Mapped[]`` columns like ``review_tables``.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
# Paused at an approval step; the job has finished and nothing runs until an
# approver decides.
RUN_AWAITING_APPROVAL = "awaiting_approval"
RUN_REJECTED = "rejected"
RUN_STATUSES = (
    RUN_RUNNING,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_CANCELLED,
    RUN_AWAITING_APPROVAL,
    RUN_REJECTED,
)
# Runs in one of these states can be resumed, retried from a step, or have a
# failed step skipped.
RUN_RESUMABLE = (RUN_FAILED, RUN_CANCELLED)

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
# The step's condition evaluated false; the run went on without it.
STEP_SKIPPED_CONDITION = "skipped_condition"
# An approval step waiting for a decision.
STEP_AWAITING = "awaiting"
STEP_REJECTED = "rejected"
STEP_STATUSES = (
    STEP_PENDING,
    STEP_RUNNING,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_SKIPPED_CONDITION,
    STEP_AWAITING,
    STEP_REJECTED,
)
# Statuses that count as "this step is done and the run may continue past it".
STEP_DONE = (STEP_COMPLETED, STEP_SKIPPED_CONDITION)


class Workflow(Base):
    """A saved workflow: its ordered steps, owned like other work product."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    matter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The built-in template this workflow was instantiated from, if any.
    template_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Ordered steps: [{name, kind, params: {...}, inputs: {source, document_ids, step},
    # condition: {...} | None}, ...]
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # Incremented every time the steps change; the matching WorkflowVersion row
    # holds the snapshot. Runs record the version they executed.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Archived workflows are hidden from the default list and cannot be run.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (Index("idx_workflows_owner_matter", "owner_id", "matter_id"),)


class WorkflowVersion(Base):
    """A snapshot of a workflow's steps at one version number."""

    __tablename__ = "workflow_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_workflow_versions_workflow_version", "workflow_id", "version", unique=True),
    )


class WorkflowRun(Base):
    """One execution of a workflow over a set of input documents."""

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Who ran it (the owner or a matter member); their key and their access rules apply.
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    matter_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # The steps exactly as they were when the run started, so editing the
    # workflow afterwards never changes what a past run means.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # The workflow version those steps came from.
    workflow_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    input_document_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=RUN_RUNNING)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many times the run has been resumed from a failed step.
    resumed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_workflow_runs_workflow_created", "workflow_id", "created_at"),)


class WorkflowStepRun(Base):
    """One step of one run: status, output and what it produced."""

    __tablename__ = "workflow_step_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STEP_PENDING)
    # The step's JSON output: a one-line summary plus kind-specific data.
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Documents the step read and work product it created, by id.
    input_document_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    produced_ids: Mapped[dict[str, list[str]]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_workflow_step_runs_run_index", "run_id", "index", unique=True),)
