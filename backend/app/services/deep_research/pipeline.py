"""Execute an approved research plan: retrieve, answer, verify, synthesise.

Stages
------
1. **Execute.** Sub-questions run concurrently under a semaphore. For each,
   the tools its plan entry allows are called (firm-document search, case-law
   search); every passage they return is given a server reference by the
   ``PassageStore``. One bounded "answer with refs" call then writes a short
   answer whose claims each cite references and copy a supporting quote. A
   sub-question may propose one follow-up query (depth 1); when the tool
   budget allows, the tools run again on it and the answer is re-written over
   the enlarged passage set.
2. **Verify.** Each claim's quote is located in the passages it cites
   (``verify_quote``); a quote that is not found is kept and flagged, never
   dropped. Claims citing references that do not exist are stripped and
   counted. Case-law authorities the final answer cites go through the
   citation check (treatment-language scan).
3. **Synthesise.** One call writes the final answer over the sub-answers and
   the cited passages; the references in it are joined back in code, model
   prose naming a case that appears in no passage is redacted by the existing
   case-law guard, and the result states exactly what was searched.

Every retrieved text reaches the model inside ``untrusted_block`` delimiters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.ai_notice import AI_REVIEW_NOTICE
from app.services.case_law_research import redact_unverified_case_references
from app.services.deep_research.passages import (
    PassageStore,
    extract_refs,
    join_refs_in_text,
    verify_claim_quote,
)
from app.services.deep_research.planner import TOOL_CASE_LAW, TOOL_DOCUMENTS
from app.services.deep_research.tools import (
    PASSAGE_MAX_CHARS,
    ToolBudget,
    ToolBudgetExhausted,
    check_citation,
    search_case_law,
    search_firm_documents,
)
from app.services.job_queue import job_manager
from app.services.llm_clients import openai_chat, research_model
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

# Prompt budgets. A sub-question answer reads its own passages in full (up
# to this many characters across them); the synthesis reads the cited ones.
ANSWER_BUDGET_CHARS = 60_000
SYNTHESIS_BUDGET_CHARS = 120_000
MAX_CLAIMS_PER_SUBQUESTION = 8
MAX_CITATIONS_CHECKED = 8
CITATION_CHECK_CONCURRENCY = 3


class SynthesisError(RuntimeError):
    """The final synthesis call failed; the run has no answer."""


@dataclass
class RunConfig:
    user_id: str
    user_keys: Any
    document_ids: list[str] | None  # None = everything the caller may read
    matter_ids: list[str] | None
    concurrency: int = 3
    max_tool_calls: int = 24
    max_passages: int = 60
    followup_depth: int = 1
    case_law_available: bool = True
    citation_check_available: bool = True


@dataclass
class StepLog:
    """What happened for one sub-question; carried verbatim in the result."""

    id: str
    question: str
    tools: list[str]
    status: str = "pending"  # pending | completed | failed
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    passages_found: int = 0
    followup_query: str | None = None
    answer: str = ""
    claims: list[dict[str, Any]] = field(default_factory=list)
    quotes_verified: int = 0
    quotes_unverified: int = 0
    invalid_refs_stripped: int = 0
    error: str | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "tools": list(self.tools),
            "status": self.status,
            "tool_calls": list(self.tool_calls),
            "passages_found": self.passages_found,
            "followup_query": self.followup_query,
            "answer": self.answer,
            "claims": list(self.claims),
            "quotes_verified": self.quotes_verified,
            "quotes_unverified": self.quotes_unverified,
            "invalid_refs_stripped": self.invalid_refs_stripped,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


def _passage_lines(passages: list[Any], budget: int) -> str:
    if not passages:
        return "(no passages were retrieved)"
    per_passage = max(300, min(PASSAGE_MAX_CHARS, budget // len(passages)))
    lines = []
    for p in passages:
        label = (
            p.title if p.source_type == "document" else f"{p.title} ({p.citation or 'no citation'})"
        )
        lines.append(f"[{p.ref}] ({p.source_type}: {label}) {p.text[:per_passage]}")
    return "\n\n".join(lines)


def build_answer_prompt(question: str, passages: list[Any], allow_followup: bool) -> str:
    followup = (
        '"followup": "ONE further search query that would materially improve the answer, or empty"'
        if allow_followup
        else '"followup": ""'
    )
    return (
        "You are a legal research assistant. Answer the sub-question using ONLY the "
        'passages below. Every claim must cite the refs (e.g. "P3") of the passages '
        "it relies on and copy ONE supporting quote EXACTLY, character for character, "
        "from one of those passages. If the passages do not answer the sub-question, "
        "say so in the answer and return no claims. Never cite a ref that does not "
        "appear below and never name a case that is not in the passages.\n\n"
        f"SUB-QUESTION: {question}\n\n"
        'Return STRICT JSON: {"answer": "2-6 sentences, each claim followed by its refs '
        'in square brackets like [P1] or [P2, P5]", '
        '"claims": [{"text": "one sentence", "refs": ["P1"], "quote": "verbatim passage"}], '
        f"{followup}}}\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        "PASSAGES:\n"
        + untrusted_block("Retrieved passages", _passage_lines(passages, ANSWER_BUDGET_CHARS))
    )


def build_synthesis_prompt(question: str, steps: list[StepLog], passages: list[Any]) -> str:
    findings = "\n\n".join(f"[{s.id}] {s.question}\n{s.answer or '(no answer)'}" for s in steps)
    return (
        "You are a senior legal researcher writing up the results of a multi-step "
        "research run for a lawyer. Using ONLY the sub-answers and the passages "
        "below, write the final answer to the question. Cite passages inline with "
        'their refs in square brackets, e.g. "[P2]" or "[P2, P7]", after each '
        "statement they support. Use only refs that appear below; never invent a "
        "ref and never name a case or document that is not in the passages. Where "
        "the research did not settle a point, say so plainly.\n\n"
        f"QUESTION: {question}\n\n"
        'Return STRICT JSON: {"answer": "markdown: a direct answer first, then '
        'headed sections as needed", "key_findings": [{"text": "one sentence", '
        '"refs": ["P1"]}], "open_questions": ["what the research could not settle"]}\n\n'
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        "SUB-ANSWERS:\n"
        + untrusted_block("Sub-answers from the research steps", findings)
        + "\n\nPASSAGES:\n"
        + untrusted_block("Cited passages", _passage_lines(passages, SYNTHESIS_BUDGET_CHARS))
    )


async def _json_chat(client: Any, prompt: str, temperature: float) -> dict[str, Any]:
    resp = await openai_chat(
        client,
        model=research_model(),
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    return data if isinstance(data, dict) else {}


class ResearchRunner:
    """Runs one approved plan. One instance per run; not shared."""

    def __init__(self, client: Any, config: RunConfig) -> None:
        self.client = client
        self.config = config
        self.store = PassageStore(config.max_passages)
        self.budget = ToolBudget(config.max_tool_calls)
        self.semaphore = asyncio.Semaphore(max(1, int(config.concurrency)))
        self.steps: list[StepLog] = []
        self._done = 0

    # ------------------------------------------------------------- retrieval

    async def _run_tools(self, step: StepLog, query: str, tools: list[str]) -> None:
        """Call each allowed tool for ``query``; record every call on the step."""
        for tool in tools:
            if tool == TOOL_CASE_LAW and not self.config.case_law_available:
                continue
            call: dict[str, Any] = {"tool": tool, "query": query, "passages": 0}
            try:
                self.budget.take()
                if tool == TOOL_DOCUMENTS:
                    call["passages"] = await self._documents_tool(step.id, query)
                elif tool == TOOL_CASE_LAW:
                    call["passages"] = await self._case_law_tool(step.id, query)
            except ToolBudgetExhausted as e:
                call["error"] = str(e)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # one failing tool never fails the step
                logger.warning("Deep research %s tool failed for %s: %s", tool, step.id, e)
                call["error"] = f"{type(e).__name__}: {e}"[:200]
            step.tool_calls.append(call)

    async def _documents_tool(self, step_id: str, query: str) -> int:
        results = await search_firm_documents(
            query,
            user_id=self.config.user_id,
            user_keys=self.config.user_keys,
            document_ids=self.config.document_ids,
            matter_ids=self.config.matter_ids,
        )
        added = 0
        for r in results:
            meta = r.get("metadata") or {}
            text = (r.get("text") or "").strip()
            doc_id = meta.get("document_id")
            if not text or not doc_id:
                continue
            chunk_index = meta.get("chunk_index")
            passage = self.store.add(
                f"document:{doc_id}:{chunk_index}",
                step_id,
                source_type="document",
                text=text[:PASSAGE_MAX_CHARS],
                title=meta.get("filename") or "Document",
                excerpt_source="index",
                document_id=str(doc_id),
                chunk_index=int(chunk_index) if isinstance(chunk_index, int) else None,
                score=float(r["similarity"]) if r.get("similarity") is not None else None,
            )
            if passage is not None:
                added += 1
        return added

    async def _case_law_tool(self, step_id: str, query: str) -> int:
        hits = await search_case_law(query)
        added = 0
        for h in hits:
            passage = self.store.add(
                f"case_law:{h['opinion_id']}",
                step_id,
                source_type="case_law",
                text=h["text"][:PASSAGE_MAX_CHARS],
                title=h["case_name"],
                excerpt_source=h["excerpt_source"],
                opinion_id=h["opinion_id"],
                cluster_id=h.get("cluster_id"),
                citation=h.get("citation"),
                url=h.get("url"),
                court=h.get("court"),
                date_filed=h.get("date_filed"),
                score=float(h["score"]) if h.get("score") is not None else None,
            )
            if passage is not None:
                added += 1
        return added

    # --------------------------------------------------------------- answers

    def _record_claims(self, step: StepLog, raw_claims: Any, allowed: set[str]) -> None:
        """Join a sub-answer's claims back to real passages and verify quotes."""
        claims: list[dict[str, Any]] = []
        for item in raw_claims if isinstance(raw_claims, list) else []:
            if not isinstance(item, dict) or len(claims) >= MAX_CLAIMS_PER_SUBQUESTION:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            wanted = extract_refs(item.get("refs"))
            refs = [r for r in wanted if r in allowed]
            # Every ref that names no real passage is stripped and counted; a
            # claim left with no source at all is dropped (and counted once).
            step.invalid_refs_stripped += (len(wanted) - len(refs)) or (0 if refs else 1)
            if not refs:
                continue
            quote = str(item.get("quote") or "").strip()
            check = verify_claim_quote(self.store, refs, quote)
            if not check["empty"]:
                if check["verified"]:
                    step.quotes_verified += 1
                else:
                    step.quotes_unverified += 1
            claims.append(
                {
                    "text": text,
                    "refs": refs,
                    "quote": quote[:600] or None,
                    "verified": bool(check["verified"]),
                    "quote_ref": check["ref"],
                }
            )
        step.claims = claims

    async def _answer(self, step: StepLog, allow_followup: bool) -> str | None:
        passages = self.store.for_sub_question(step.id)
        data = await _json_chat(
            self.client, build_answer_prompt(step.question, passages, allow_followup), 0.1
        )
        allowed = {p.ref for p in passages}
        answer, _cited, stripped = join_refs_in_text(str(data.get("answer") or ""), allowed)
        step.answer = answer
        step.invalid_refs_stripped += stripped
        self._record_claims(step, data.get("claims"), allowed)
        followup = str(data.get("followup") or "").strip()
        return followup[:300] if allow_followup and followup else None

    async def _run_step(self, step: StepLog, total: int) -> None:
        started = time.monotonic()
        async with self.semaphore:
            try:
                await self._run_tools(step, step.question, step.tools)
                allow_followup = self.config.followup_depth > 0 and self.budget.remaining > 0
                followup = await self._answer(step, allow_followup)
                if followup and followup.lower() != step.question.lower():
                    step.followup_query = followup
                    before = len(self.store.for_sub_question(step.id))
                    await self._run_tools(step, followup, step.tools)
                    if len(self.store.for_sub_question(step.id)) > before:
                        # New material: re-write the answer over the enlarged set.
                        step.quotes_verified = step.quotes_unverified = 0
                        step.invalid_refs_stripped = 0
                        await self._answer(step, allow_followup=False)
                step.passages_found = len(self.store.for_sub_question(step.id))
                step.status = "completed"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the answer call failed; other steps continue
                logger.warning("Deep research step %s failed: %s", step.id, e)
                step.status = "failed"
                step.error = f"{type(e).__name__}: {e}"[:300]
                step.passages_found = len(self.store.for_sub_question(step.id))
        step.duration_ms = int((time.monotonic() - started) * 1000)
        self._done += 1
        job_manager.report_progress(
            f"Researched {self._done}/{total} sub-questions", 0.1 + 0.7 * self._done / total
        )

    # ------------------------------------------------------------ synthesis

    async def _synthesise(self, question: str) -> dict[str, Any]:
        cited_refs: list[str] = []
        for step in self.steps:
            for claim in step.claims:
                for ref in claim["refs"]:
                    if ref not in cited_refs:
                        cited_refs.append(ref)
        # The synthesis may draw on any passage a claim cited, plus every
        # passage of a step that produced no claims (so a thin step is still
        # represented by what was found rather than by nothing).
        passages = [p for p in (self.store.get(r) for r in cited_refs) if p is not None]
        for step in self.steps:
            if not step.claims:
                for p in self.store.for_sub_question(step.id):
                    if p not in passages:
                        passages.append(p)
        try:
            data = await _json_chat(
                self.client, build_synthesis_prompt(question, self.steps, passages), 0.1
            )
        except Exception as e:
            logger.error("Deep research synthesis failed: %s", e)
            raise SynthesisError(
                "The research synthesis could not be completed. Please try again."
            ) from e

        allowed = {p.ref for p in passages}
        answer, cited, stripped = join_refs_in_text(str(data.get("answer") or ""), allowed)
        findings: list[dict[str, Any]] = []
        raw_findings = data.get("key_findings")
        for item in raw_findings if isinstance(raw_findings, list) else []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            wanted = extract_refs(item.get("refs"))
            refs = [r for r in wanted if r in allowed]
            stripped += len(wanted) - len(refs)
            if text:
                findings.append({"text": text, "refs": refs})
                for r in refs:
                    if r not in cited:
                        cited.append(r)
        raw_open = data.get("open_questions")
        open_questions = [
            str(q).strip()
            for q in (raw_open if isinstance(raw_open, list) else [])
            if str(q).strip()
        ][:10]

        # Model prose may only name authorities that appear in the passages.
        guard_sources = [question] + [p.text for p in passages]
        answer, removed = redact_unverified_case_references(answer, sources=guard_sources)
        for finding in findings:
            finding["text"], gone = redact_unverified_case_references(
                finding["text"], sources=guard_sources
            )
            removed += gone
        return {
            "answer": answer,
            "key_findings": findings,
            "open_questions": open_questions,
            "cited_refs": cited,
            "invalid_refs_stripped": stripped,
            "case_references_removed": removed,
        }

    async def _check_citations(self, cited_refs: list[str]) -> list[dict[str, Any]]:
        """Treatment scan for the case-law authorities the answer cites (bounded)."""
        if not self.config.citation_check_available:
            return []
        citations: list[tuple[str, str]] = []
        seen: set[str] = set()
        for ref in cited_refs:
            p = self.store.get(ref)
            if p is None or p.source_type != "case_law" or not p.citation:
                continue
            if p.citation in seen:
                continue
            seen.add(p.citation)
            citations.append((ref, p.citation))
        citations = citations[:MAX_CITATIONS_CHECKED]
        if not citations:
            return []
        gate = asyncio.Semaphore(CITATION_CHECK_CONCURRENCY)

        async def one(ref: str, citation: str) -> dict[str, Any]:
            async with gate:
                result = await check_citation(citation)
            return {"ref": ref, **result}

        return list(await asyncio.gather(*(one(ref, c) for ref, c in citations)))

    # ------------------------------------------------------------------ run

    async def run(self, question: str, plan: list[dict[str, Any]]) -> dict[str, Any]:
        self.steps = [
            StepLog(id=str(s["id"]), question=str(s["question"]), tools=list(s["tools"]))
            for s in plan
        ]
        total = len(self.steps)
        job_manager.report_progress(f"Researching 0/{total} sub-questions", 0.05)
        await asyncio.gather(*(self._run_step(step, total) for step in self.steps))

        job_manager.report_progress("Writing the answer", 0.85)
        synthesis = await self._synthesise(question)
        job_manager.report_progress("Checking cited authorities", 0.95)
        treatment = await self._check_citations(synthesis["cited_refs"])
        treatment_by_ref = {t["ref"]: t for t in treatment}

        sources = []
        for ref in synthesis["cited_refs"]:
            p = self.store.get(ref)
            if p is None:
                continue
            entry = p.source_entry()
            entry["quotes_verified"] = sum(
                1 for s in self.steps for c in s.claims if c["verified"] and c["quote_ref"] == ref
            )
            if ref in treatment_by_ref:
                entry["treatment"] = {
                    k: v for k, v in treatment_by_ref[ref].items() if k not in ("ref", "citation")
                }
            sources.append(entry)

        quotes_verified = sum(s.quotes_verified for s in self.steps)
        quotes_unverified = sum(s.quotes_unverified for s in self.steps)
        invalid_refs = (
            sum(s.invalid_refs_stripped for s in self.steps) + synthesis["invalid_refs_stripped"]
        )
        case_law_searched = any(
            c["tool"] == TOOL_CASE_LAW and "error" not in c
            for s in self.steps
            for c in s.tool_calls
        )
        return {
            "question": question,
            "answer": synthesis["answer"],
            "key_findings": synthesis["key_findings"],
            "open_questions": synthesis["open_questions"],
            "sub_questions": [s.as_dict() for s in self.steps],
            "sources": sources,
            "scope": {
                "matter_id": None,  # filled by the service, which knows the request
                "document_ids": self.config.document_ids,
                "documents_in_scope": (
                    len(self.config.document_ids) if self.config.document_ids is not None else None
                ),
                "case_law_searched": case_law_searched,
                "courtlistener_contacted": case_law_searched or bool(treatment),
                "tool_calls": self.budget.used,
                "tool_call_cap": self.budget.max_calls,
                "passages_collected": len(self.store),
                "passages_dropped_over_cap": self.store.dropped_over_cap,
                "passages_cited": len(sources),
                "steps_failed": sum(1 for s in self.steps if s.status == "failed"),
                "coverage_note": (
                    f"Answered from {len(sources)} cited passage"
                    f"{'' if len(sources) == 1 else 's'} out of {len(self.store)} retrieved "
                    f"across {total} sub-question{'' if total == 1 else 's'} and "
                    f"{self.budget.used} tool call{'' if self.budget.used == 1 else 's'}. "
                    "Documents were searched, not read in full; opinions were read in the "
                    "passage cited, not in full."
                ),
            },
            "verification": {
                "quotes_verified": quotes_verified,
                "quotes_unverified": quotes_unverified,
                "invalid_refs_stripped": invalid_refs,
                "case_references_removed": synthesis["case_references_removed"],
                "citations_checked": len(treatment),
            },
            "model": research_model(),
            "notice": AI_REVIEW_NOTICE,
        }
