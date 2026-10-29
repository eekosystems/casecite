"""Validate a step list the way the editor needs it: every problem, with the
step and field it belongs to, rather than the first exception.

Saving still goes through the Pydantic models (``WorkflowCreateRequest`` and
``WorkflowUpdateRequest``), which refuse a bad workflow with 422; this module
runs the same rules over the raw steps and reports them all, so the editor
can show each error under the step it concerns before the user tries to save.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from app.models.schemas import (
    STEP_PARAM_MODELS,
    WorkflowStep,
    WorkflowStepIssue,
    WorkflowStepKind,
    referenced_steps_by_field,
)


def _loc_to_field(loc: tuple[Any, ...]) -> str:
    """Pydantic's location tuple as the dotted field the editor shows it under."""
    return ".".join(str(p) for p in loc) or "step"


_FIELD_PREFIX_RE = re.compile(r"^([a-z_][a-z0-9_]*): (.+)$", re.DOTALL)


def _issues_from_error(
    index: int, error: ValidationError, prefix: str = ""
) -> list[WorkflowStepIssue]:
    issues: list[WorkflowStepIssue] = []
    for err in error.errors():
        message = str(err.get("msg") or "Invalid value")
        # Pydantic prefixes a model validator's ValueError with "Value error, ".
        message = message.removeprefix("Value error, ")
        field = _loc_to_field(err["loc"]) if err["loc"] else ""
        # A model-level rule names its field as "field: message".
        named = _FIELD_PREFIX_RE.match(message) if not field else None
        if named:
            field, message = named.group(1), named.group(2)
        field = ".".join(p for p in (prefix, field) if p) or "step"
        issues.append(WorkflowStepIssue(index=index, field=field, message=message))
    return issues


def _params_issues(index: int, raw: dict[str, Any]) -> list[WorkflowStepIssue]:
    """The kind's own parameter errors, each under ``params.<field>``."""
    try:
        kind = WorkflowStepKind(str(raw.get("kind") or ""))
    except ValueError:
        return []
    try:
        STEP_PARAM_MODELS[kind].model_validate(raw.get("params") or {})
    except ValidationError as e:
        return _issues_from_error(index, e, prefix="params")
    return []


def _step_issues(index: int, raw: Any) -> tuple[WorkflowStep | None, list[WorkflowStepIssue]]:
    if not isinstance(raw, dict):
        return None, [
            WorkflowStepIssue(index=index, field="step", message="Each step is an object")
        ]
    issues = _params_issues(index, raw)
    try:
        return WorkflowStep.model_validate(raw), issues
    except ValidationError as e:
        # The step wrapper repeats a parameter problem as one opaque error;
        # report it only when the parameters themselves were fine.
        return None, issues or _issues_from_error(index, e)


def _kind_rules(index: int, step: WorkflowStep) -> list[WorkflowStepIssue]:
    """Rules a single step's params cannot express on their own."""
    issues: list[WorkflowStepIssue] = []
    if step.kind == WorkflowStepKind.COMPARE_CONTRACTS and (
        step.params.get("baseline") == "document" and not step.params.get("baseline_document_id")
    ):
        issues.append(
            WorkflowStepIssue(
                index=index,
                field="params.baseline_document_id",
                message="Pick the baseline document to compare against",
            )
        )
    if (
        step.kind == WorkflowStepKind.DRAFT_DOCUMENT
        and not str(step.params.get("instructions") or "").strip()
    ):
        issues.append(
            WorkflowStepIssue(
                index=index,
                field="params.instructions",
                message="Drafting instructions are required",
            )
        )
    return issues


def _reference_rules(
    index: int, step: WorkflowStep, earlier: dict[str, str]
) -> list[WorkflowStepIssue]:
    """Every reference must name an EARLIER step, never itself or a later one."""
    issues: list[WorkflowStepIssue] = []
    lowered = step.name.lower()
    for field, names in referenced_steps_by_field(step).items():
        for ref in sorted(names):
            if ref.lower() == lowered:
                issues.append(
                    WorkflowStepIssue(
                        index=index, field=field, message="A step cannot refer to itself"
                    )
                )
            elif ref.lower() not in earlier:
                issues.append(
                    WorkflowStepIssue(
                        index=index,
                        field=field,
                        message=f"'{ref}' is not an earlier step; only steps above this one can be used",
                    )
                )
    return issues


def validate_steps(raw_steps: list[Any], max_steps: int) -> list[WorkflowStepIssue]:
    """Every problem with ``raw_steps``, as (index, field, message). Empty means valid."""
    issues: list[WorkflowStepIssue] = []
    if not raw_steps:
        return [WorkflowStepIssue(index=-1, field="steps", message="Add at least one step")]
    if len(raw_steps) > max_steps:
        issues.append(
            WorkflowStepIssue(
                index=-1,
                field="steps",
                message=f"A workflow may have at most {max_steps} steps on this server",
            )
        )
    seen: dict[str, str] = {}
    for index, raw in enumerate(raw_steps):
        step, step_issues = _step_issues(index, raw)
        issues.extend(step_issues)
        if step is None:
            # Keep the name so later references to a broken step are not also blamed.
            name = str(raw.get("name") or "").strip().lower() if isinstance(raw, dict) else ""
            if name and name not in seen:
                seen[name] = name
            continue
        lowered = step.name.lower()
        if lowered in seen:
            issues.append(
                WorkflowStepIssue(
                    index=index,
                    field="name",
                    message=f"Another step is already named '{step.name}'",
                )
            )
        issues.extend(_kind_rules(index, step))
        issues.extend(_reference_rules(index, step, seen))
        seen[lowered] = step.name
    return issues
