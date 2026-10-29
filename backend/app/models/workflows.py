"""Workflows — saved, repeatable sequences of the platform's own operations.

A ``Workflow`` is a named, ordered, linear list of typed steps (extract
fields, analyse a contract, compare contracts, draft, research, check
citations). Each step names the service operation it runs and the
parameters it runs with; nothing in a workflow is code. A ``WorkflowRun`` is
one execution over a set of input documents, and a ``WorkflowStepRun`` is one
step of that execution with its status and output, written as it happens so a
run that stops can be read, and resumed from the step that failed.

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
RUN_STATUSES = (RUN_RUNNING, RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED)

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_STATUSES = (STEP_PENDING, STEP_RUNNING, STEP_COMPLETED, STEP_FAILED, STEP_SKIPPED)


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
    # Ordered steps: [{name, kind, params: {...}, inputs: {source, document_ids, step}}, ...]
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (Index("idx_workflows_owner_matter", "owner_id", "matter_id"),)


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
