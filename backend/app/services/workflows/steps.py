"""Step adapters: one thin function per step kind, each calling the service
layer directly (never an HTTP route) and returning a JSON-serialisable output.

Every adapter receives the step as stored, the run's context (who is running,
what they may read, their client) and returns a ``StepOutput``: a one-line
summary, kind-specific data, the documents it read and the work product it
created. Adapters raise ``StepError`` for a failure that must stop the run;
per-document failures inside a step are recorded in the output instead, so one
unreadable contract does not stop a twenty-contract review.

Access is the runner's: a document is read only if the runner owns it or can
reach it through a matter they belong to. Reading goes through the same chunk
reconstruction the review tables use, which honours a colleague's filed
document; the contract-analysis router's owner-only loader would not.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.database import AsyncSessionLocal
from app.models.schemas import Document, WorkflowInputSource, WorkflowStepKind
from app.services.contract_analysis.compare import compare_contracts
from app.services.contract_analysis.drafting import draft_document, reference_cap_chars
from app.services.contract_analysis.service import contract_analysis_service
from app.services.deep_research import DeepResearchError, PlanningError, deep_research_service
from app.services.deep_research.tools import check_citation
from app.services.documents import document_service
from app.services.rag.case_law_guard import _REPORTER_CITE_RE, _WL_CITE_RE
from app.services.review_tables import review_table_service
from app.services.user_settings import load_user_settings
from app.services.workflows.substitution import output_as_text, render_template

logger = logging.getLogger(__name__)

# How much of each attached output / reference document a draft may see, so
# four references and three prior outputs still fit the drafting budget.
_REFERENCE_PART_CHARS = 20_000
_WS_RE = re.compile(r"\s+")


class StepError(Exception):
    """A failure that stops the run at this step. Not a RuntimeError: the job
    queue retries those, and a workflow is far too expensive to re-run whole."""


@dataclass
class StepOutput:
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    document_ids: list[str] = field(default_factory=list)
    produced: dict[str, list[str]] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "data": self.data,
            "document_ids": list(self.document_ids),
            "produced": {k: list(v) for k, v in self.produced.items()},
        }


@dataclass
class StepContext:
    run_id: str
    workflow_name: str
    user_id: str
    accessible_matter_ids: set[str] | None
    user_keys: Any
    client: Any
    matter_id: str | None
    input_document_ids: list[str]
    # Completed earlier steps: name -> StepOutput.as_json().
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_documents: int = 25
    max_citations: int = 10
    progress: Callable[[str], None] = lambda _message: None

    def output_of(self, name: str) -> dict[str, Any] | None:
        lowered = name.lower()
        for key, value in self.outputs.items():
            if key.lower() == lowered:
                return value
        return None


# ------------------------------------------------------------------ inputs


def resolve_inputs(step: dict[str, Any], ctx: StepContext) -> list[str]:
    """The documents a step reads, in order and de-duplicated.

    ``run``: the run's input documents. ``documents``: ids fixed in the
    workflow. ``step``: the documents the named earlier step produced (none of
    the current kinds register new documents) or, failing that, the ones it
    read — so "analyse what the extraction step covered" works.
    """
    inputs = step.get("inputs") or {}
    source = inputs.get("source") or WorkflowInputSource.RUN.value
    if source == WorkflowInputSource.DOCUMENTS.value:
        ids = list(inputs.get("document_ids") or [])
    elif source == WorkflowInputSource.STEP.value:
        prior = ctx.output_of(str(inputs.get("step") or ""))
        if prior is None:
            raise StepError(
                f"Step '{step.get('name')}' reads documents from '{inputs.get('step')}', "
                "which has not completed"
            )
        produced = (prior.get("produced") or {}).get("documents") or []
        ids = list(produced) or list(prior.get("document_ids") or [])
    else:
        ids = list(ctx.input_document_ids)
    return list(dict.fromkeys(ids))


async def _read_document(doc_id: str, ctx: StepContext) -> tuple[Document, str]:
    doc = await document_service.get_document(doc_id, ctx.user_id, ctx.accessible_matter_ids)
    if doc is None:
        raise LookupError("Document is not accessible or no longer exists")
    text = await review_table_service.document_text(doc)
    return doc, text


async def _label(doc_id: str, ctx: StepContext) -> str:
    doc = await document_service.get_document(doc_id, ctx.user_id, ctx.accessible_matter_ids)
    return doc.filename if doc else doc_id


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ------------------------------------------------------------- extract_fields


async def run_extract_fields(step: dict[str, Any], ctx: StepContext) -> StepOutput:
    params = step["params"]
    doc_ids = resolve_inputs(step, ctx)
    if not doc_ids:
        raise StepError("No documents to extract from")
    fields = list(params["fields"])
    name = (params.get("table_name") or f"{ctx.workflow_name} — {step['name']}").strip()[:300]
    async with AsyncSessionLocal() as session:
        table = await review_table_service.create(
            session,
            owner_id=ctx.user_id,
            matter_id=ctx.matter_id,
            name=name,
            description=f"Created by workflow run {ctx.run_id}",
            fields=fields,
            document_ids=doc_ids,
        )
    ctx.progress(
        f"Extracting {_plural(len(fields), 'field')} from {_plural(len(doc_ids), 'document')}"
    )
    try:
        result = await review_table_service.run(
            table_id=table.id,
            user_id=ctx.user_id,
            accessible_matter_ids=ctx.accessible_matter_ids,
            user_keys=ctx.user_keys,
        )
    except ValueError as e:  # no model available
        raise StepError(str(e)) from e
    async with AsyncSessionLocal() as session:
        cells = await review_table_service.cells(session, table.id)

    rows: dict[str, dict[str, Any]] = {}
    values: dict[str, dict[str, str | None]] = {}
    for cell in cells:
        rows.setdefault(cell.document_id, {})[cell.field_key] = {
            "value": cell.value,
            "status": cell.status,
            "verified": bool(cell.verified),
            "quote": cell.quote,
        }
        values.setdefault(cell.document_id, {})[cell.field_key] = cell.value
    documents = [{"id": d, "filename": await _label(d, ctx)} for d in doc_ids]
    summary = (
        f"Extracted {_plural(len(fields), 'field')} from {_plural(len(doc_ids), 'document')}: "
        f"{result.get('extracted', 0)} verified, {result.get('unverified', 0)} unverified, "
        f"{result.get('not_found', 0)} not found, {result.get('errors', 0)} failed."
    )
    return StepOutput(
        summary=summary,
        data={
            "table_id": table.id,
            "fields": [{"key": f["key"], "name": f["name"]} for f in fields],
            "documents": documents,
            "rows": rows,
            "values": values,
            "counts": {
                k: result.get(k, 0) for k in ("extracted", "unverified", "not_found", "errors")
            },
        },
        document_ids=doc_ids,
        produced={"review_tables": [table.id]},
    )


# ----------------------------------------------------------- analyze_contract

# Issues kept per document in the step output (the full set stays on the analysis).
_ISSUES_PER_DOCUMENT = 25


def _issue_brief(issue: dict[str, Any]) -> dict[str, Any]:
    """The fields the run view renders for one issue, each bounded."""
    return {
        "ref": issue.get("ref"),
        "title": str(issue.get("title") or "")[:200],
        "severity": str(issue.get("severity") or "minor"),
        "why": str(issue.get("why") or "")[:600],
        "grounding": issue.get("grounding"),
        "matched_text": str(issue.get("matched_text") or "")[:300] or None,
        "suggested_language": str(issue.get("suggested_language") or "")[:400] or None,
    }


async def run_analyze_contract(step: dict[str, Any], ctx: StepContext) -> StepOutput:
    params = step["params"]
    doc_ids = resolve_inputs(step, ctx)
    if not doc_ids:
        raise StepError("No documents to analyse")
    skipped = doc_ids[ctx.max_documents :]
    doc_ids = doc_ids[: ctx.max_documents]
    analyses: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for i, doc_id in enumerate(doc_ids, start=1):
        ctx.progress(f"Analysing contract {i}/{len(doc_ids)}")
        try:
            doc, text = await _read_document(doc_id, ctx)
            result = await contract_analysis_service.analyze(
                text=text,
                contract_type=params.get("contract_type") or "auto",
                jurisdiction=params.get("jurisdiction"),
                user_id=ctx.user_id,
                document_id=doc_id,
                user_keys=ctx.user_keys,
                representing=params.get("representing"),
                posture=params.get("posture") or "balanced",
                instructions=params.get("instructions"),
            )
        except (LookupError, RuntimeError, ValueError) as e:
            failures.append({"document_id": doc_id, "error": str(e)[:300]})
            continue
        except Exception as e:  # provider and vector-store libraries raise their own classes
            logger.warning("Workflow %s: analysis of %s failed: %s", ctx.run_id, doc_id, e)
            failures.append({"document_id": doc_id, "error": f"{type(e).__name__}"})
            continue
        issues = result.get("issues") or []
        severities = Counter(str(i.get("severity") or "minor") for i in issues)
        analyses.append(
            {
                "document_id": doc_id,
                "filename": doc.filename,
                "analysis_id": result.get("analysis_id"),
                "contract_type": result.get("contract_type"),
                "issues": len(issues),
                "issues_by_severity": dict(severities),
                "issue_list": [_issue_brief(i) for i in issues[:_ISSUES_PER_DOCUMENT]],
                "executive_summary": result.get("executive_summary"),
                "parties": result.get("parties") or [],
                "key_terms": result.get("key_terms") or {},
            }
        )
    if not analyses:
        detail = failures[0]["error"] if failures else "no documents could be read"
        raise StepError(f"Contract analysis failed for every document: {detail}")
    total_issues = sum(a["issues"] for a in analyses)
    summary = (
        f"Analysed {_plural(len(analyses), 'contract')}, {_plural(total_issues, 'issue')} found."
    )
    if failures:
        summary += f" {_plural(len(failures), 'document')} could not be analysed."
    return StepOutput(
        summary=summary,
        data={"analyses": analyses, "failures": failures, "skipped_document_ids": skipped},
        document_ids=doc_ids,
        produced={"analyses": [a["analysis_id"] for a in analyses if a.get("analysis_id")]},
    )


# ---------------------------------------------------------- compare_contracts


async def run_compare_contracts(step: dict[str, Any], ctx: StepContext) -> StepOutput:
    params = step["params"]
    doc_ids = resolve_inputs(step, ctx)
    baseline = params.get("baseline_document_id") or (doc_ids[0] if doc_ids else None)
    if not baseline:
        raise StepError("No baseline document to compare against")
    others = [d for d in doc_ids if d != baseline][:3]
    if not others:
        raise StepError("Comparing needs the baseline and at least one other document")
    try:
        _doc, text_a = await _read_document(baseline, ctx)
        label_a = await _label(baseline, ctx)
    except (LookupError, RuntimeError) as e:
        raise StepError(f"Baseline document could not be read: {e}") from e

    comparisons: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for i, doc_id in enumerate(others, start=1):
        ctx.progress(f"Comparing document {i}/{len(others)} against {label_a}")
        try:
            _doc, text_b = await _read_document(doc_id, ctx)
        except (LookupError, RuntimeError) as e:
            failures.append({"document_id": doc_id, "error": str(e)[:300]})
            continue
        label_b = await _label(doc_id, ctx)
        try:
            result = await compare_contracts(
                ctx.client,
                text_a,
                text_b,
                label_a=label_a,
                label_b=label_b,
                focus=params.get("focus"),
            )
        except ValueError as e:
            raise StepError(str(e)) from e
        clauses = result.get("clauses") or []
        comparisons.append(
            {
                "document_id_a": baseline,
                "document_id_b": doc_id,
                "label_a": label_a,
                "label_b": label_b,
                "overall": result.get("overall"),
                "clauses": clauses,
                "differences": sum(1 for c in clauses if c.get("status") != "unchanged"),
            }
        )
    if not comparisons:
        raise StepError("No document could be compared against the baseline")
    differences = sum(c["differences"] for c in comparisons)
    summary = (
        f"Compared {_plural(len(comparisons), 'document')} against {label_a}: "
        f"{_plural(differences, 'clause difference')} flagged."
    )
    return StepOutput(
        summary=summary,
        data={
            "baseline_document_id": baseline,
            "focus": params.get("focus"),
            "comparisons": comparisons,
            "failures": failures,
        },
        document_ids=[baseline, *others],
    )


# ------------------------------------------------------------- draft_document


def _standing_instructions(user_id: str) -> str:
    """The user's practice profile and playbook, as the drafting routes attach them."""
    user_settings = load_user_settings(user_id)
    playbook = (user_settings.contract_playbook or "").strip()
    profile = (user_settings.practice_profile or "").strip()
    if profile:
        area = user_settings.practice_area or "their practice"
        profile = f"THE USER'S PRACTICE PROFILE ({area}):\n{profile}"
    return "\n\n".join(part for part in (profile, playbook) if part)


async def run_draft_document(step: dict[str, Any], ctx: StepContext) -> StepOutput:
    params = step["params"]
    rendered = render_template(params["instructions"], ctx.outputs)
    instructions = rendered.text.strip()
    if len(instructions) < 3:
        raise StepError("The drafting instructions are empty after substitution")
    standing = _standing_instructions(ctx.user_id)
    if standing:
        instructions = f"{standing}\n\n{instructions}"

    parts: list[str] = []
    references: list[dict[str, str]] = []
    for name in params.get("include_outputs_from") or []:
        prior = ctx.output_of(str(name))
        if prior is None:
            raise StepError(f"Step '{name}' has no output to attach")
        parts.append(f"[Output of step {name}]\n{output_as_text(prior, _REFERENCE_PART_CHARS)}")
        references.append({"kind": "step", "name": str(name)})
    for doc_id in list(dict.fromkeys(params.get("reference_document_ids") or []))[:4]:
        try:
            doc, text = await _read_document(doc_id, ctx)
        except (LookupError, RuntimeError) as e:
            raise StepError(f"Reference document could not be read: {e}") from e
        parts.append(f"[{doc.filename}]\n{text[:_REFERENCE_PART_CHARS]}")
        references.append({"kind": "document", "id": doc_id, "filename": doc.filename})
    reference_text = "\n\n".join(parts)[: reference_cap_chars()] or None

    ctx.progress("Drafting the document")
    try:
        result = await draft_document(ctx.client, instructions, reference_text)
    except (RuntimeError, ValueError) as e:
        raise StepError(str(e)) from e
    text = result.get("text") or ""
    words = len(text.split())
    return StepOutput(
        summary=f'Drafted "{result.get("title") or "Draft"}" ({_plural(words, "word")}).',
        data={
            "title": result.get("title"),
            "text": text,
            "case_law_removed": result.get("case_law_removed") or [],
            "references": references,
            "template_values_used": rendered.used,
            "template_values_missing": rendered.missing,
        },
        document_ids=[r["id"] for r in references if r.get("kind") == "document"],
    )


# -------------------------------------------------------------- deep_research


async def run_deep_research(step: dict[str, Any], ctx: StepContext) -> StepOutput:
    params = step["params"]
    rendered = render_template(params["question"], ctx.outputs)
    question = _WS_RE.sub(" ", rendered.text).strip()
    if len(question) < 3:
        raise StepError("The research question is empty after substitution")
    doc_ids = resolve_inputs(step, ctx)
    ctx.progress("Planning the research")
    try:
        plan = await deep_research_service.plan(ctx.user_keys, question)
    except ValueError as e:
        raise StepError(str(e)) from e
    except PlanningError as e:
        raise StepError(f"Research planning failed: {e}") from e
    allowed = [str(t) for t in (params.get("tools") or [])]
    if allowed:
        narrowed = []
        for sub in plan:
            tools = [t for t in sub.get("tools", []) if t in allowed] or list(allowed)
            narrowed.append({**sub, "tools": tools})
        plan = narrowed

    document_ids = doc_ids or None  # None: everything the runner may read
    async with AsyncSessionLocal() as session:
        run = await deep_research_service.create_run(
            session,
            owner_id=ctx.user_id,
            matter_id=ctx.matter_id,
            question=question,
            plan=plan,
            scope={
                "matter_id": ctx.matter_id,
                "document_ids": document_ids,
                "documents_in_scope": len(doc_ids) if doc_ids else None,
                "workflow_run_id": ctx.run_id,
            },
        )
    ctx.progress(f"Researching {_plural(len(plan), 'sub-question')}")
    try:
        result = await deep_research_service.run(
            run_id=run.id,
            user_id=ctx.user_id,
            user_keys=ctx.user_keys,
            document_ids=document_ids,
            matter_ids=sorted(ctx.accessible_matter_ids or []) or None,
        )
    except (DeepResearchError, ValueError, LookupError) as e:
        raise StepError(str(e)) from e
    sources = result.get("sources") or []
    verification = result.get("verification") or {}
    summary = (
        f"Researched {_plural(len(plan), 'sub-question')}; "
        f"{_plural(len(sources), 'source')} cited, "
        f"{verification.get('quotes_verified', 0)} quotes verified."
    )
    return StepOutput(
        summary=summary,
        data={
            "run_id": run.id,
            "question": question,
            "answer": result.get("answer"),
            "key_findings": result.get("key_findings") or [],
            "open_questions": result.get("open_questions") or [],
            "sources": [
                {k: s.get(k) for k in ("ref", "title", "citation", "kind", "url")} for s in sources
            ],
            "verification": verification,
            "template_values_used": rendered.used,
            "template_values_missing": rendered.missing,
        },
        document_ids=doc_ids,
        produced={"research_runs": [run.id]},
    )


# ------------------------------------------------------------ citation_check


def find_citations(text: str) -> list[str]:
    """Reporter and Westlaw citations in ``text``, whitespace-normalised, in order."""
    found = [m.group(0) for m in _REPORTER_CITE_RE.finditer(text)]
    found += [m.group(0) for m in _WL_CITE_RE.finditer(text)]
    return list(dict.fromkeys(_WS_RE.sub(" ", c).strip() for c in found))


async def run_citation_check(step: dict[str, Any], ctx: StepContext) -> StepOutput:
    params = step["params"]
    citations = [_WS_RE.sub(" ", str(c)).strip() for c in params.get("citations") or []]
    from_steps = params.get("from_steps")
    names = [str(n) for n in from_steps] if from_steps is not None else list(ctx.outputs)
    found_in: dict[str, list[str]] = {}
    for name in names:
        prior = ctx.output_of(name)
        if prior is None:
            raise StepError(f"Step '{name}' has no output to scan for citations")
        found = find_citations(json.dumps(prior, ensure_ascii=False, default=str))
        if found:
            found_in[name] = found
            citations.extend(found)
    citations = [c for c in dict.fromkeys(citations) if c]
    skipped = citations[ctx.max_citations :]
    citations = citations[: ctx.max_citations]
    if not citations:
        return StepOutput(
            summary="No citations to check.",
            data={"citations": [], "found_in_steps": found_in, "skipped": []},
        )
    results: list[dict[str, Any]] = []
    for i, citation in enumerate(citations, start=1):
        ctx.progress(f"Checking citation {i}/{len(citations)}")
        results.append(await check_citation(citation))
    flagged = sum(1 for r in results if r.get("status") == "checked" and r.get("negative"))
    cautioned = sum(
        1
        for r in results
        if r.get("status") == "checked" and r.get("caution") and not r.get("negative")
    )
    unavailable = sum(1 for r in results if r.get("status") != "checked")
    summary = (
        f"Checked {_plural(len(results), 'citation')}: {flagged} with negative treatment language, "
        f"{cautioned} with cautionary language, {unavailable} could not be checked."
    )
    return StepOutput(
        summary=summary,
        data={"citations": results, "found_in_steps": found_in, "skipped": skipped},
    )


StepAdapter = Callable[[dict[str, Any], StepContext], Awaitable[StepOutput]]

ADAPTERS: dict[str, StepAdapter] = {
    WorkflowStepKind.EXTRACT_FIELDS.value: run_extract_fields,
    WorkflowStepKind.ANALYZE_CONTRACT.value: run_analyze_contract,
    WorkflowStepKind.COMPARE_CONTRACTS.value: run_compare_contracts,
    WorkflowStepKind.DRAFT_DOCUMENT.value: run_draft_document,
    WorkflowStepKind.DEEP_RESEARCH.value: run_deep_research,
    WorkflowStepKind.CITATION_CHECK.value: run_citation_check,
}
