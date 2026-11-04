"""Long-document drafting: plan → sections in parallel → reconcile.

A single chat completion tops out around ten pages and, asked for more, the
model writes thin. Long drafts are built the way a firm builds them: one plan
(outline, section briefs, shared definitions, style guide) produced from the
FULL reference material, then every section drafted in its own context window
against that same plan and the same full references, then one reconcile pass
over the assembled document for cross-references, numbering and defined-term
consistency.

Context awareness comes from artifacts, not from sections reading each other:
each section call carries the whole reference bundle (or, when it cannot fit,
the excerpts most relevant to that section plus the definitions sheet), the
brief of every other section — including what each must NOT cover — and the
definitions. Prompts put the shared material first so providers that cache
identical prefixes (OpenAI does automatically) reuse it across the parallel
section calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings
from app.services.case_law_research import redact_unverified_case_references
from app.services.llm_clients import drafting_model, openai_chat, utility_model
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)


class TruncatedOutputError(RuntimeError):
    """The model stopped because it hit its output limit (finish_reason "length").

    A truncated section or reconcile pass is never a usable result: the text
    ends mid-sentence, and a truncated reconcile silently drops every clause
    after the cut. Section drafts retry; the reconcile pass falls back to the
    assembled document.
    """


Progress = Callable[[str, float], Awaitable[None]] | None
Reference = tuple[str, str]  # (label, full text)

WORDS_PER_PAGE = 450  # dense legal prose; ~400–500 words per page
MIN_SECTION_WORDS = 150
MAX_SECTION_WORDS = 2500
MAX_SECTIONS = 60
# Below this size a draft is revised in one pass; above it, revision is scoped
# to the sections the instruction touches.
LONG_DRAFT_CHARS = 30_000
_TOKEN_CHARS = 4  # rough chars/token for English legal text; only used for budgeting

# Context windows by model-name fragment, first match wins. Overridable with
# LLM_CONTEXT_WINDOW for custom/local endpoints whose names say nothing.
_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-4", 128_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    ("claude", 200_000),
    ("gemini", 1_000_000),
    ("llama", 128_000),
    ("mistral", 128_000),
    ("qwen", 128_000),
)
_DEFAULT_WINDOW = 128_000


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // _TOKEN_CHARS)


def context_window_tokens(model: str | None = None) -> int:
    override = getattr(settings, "llm_context_window", None)
    if override:
        return int(override)
    name = (model or drafting_model() or "").lower()
    for fragment, window in _CONTEXT_WINDOWS:
        if fragment in name:
            return window
    return _DEFAULT_WINDOW


# ---------------------------------------------------------------------------
# Reference material: full text when it fits, focused excerpts when it can't
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z][a-z0-9'-]{2,}")
_STOP = frozenset(
    {
        "the", "and", "for", "that", "with", "this", "from", "shall", "have", "will", "any",
        "all", "are", "not", "such", "other", "than", "into", "under", "upon", "each", "been",
        "being", "their", "there", "which", "where", "when", "what", "these", "those",
    }
)  # fmt: skip


def _stem(word: str) -> str:
    """Crude stem so "termination" meets "terminate" and "fees" meets "fee".

    Long words compare on their first six letters; short plurals drop the "s".
    Good enough for scoring overlap between a section brief and contract text
    without pulling in an NLP dependency.
    """
    if len(word) > 6:
        return word[:6]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _term_list(text: str) -> list[str]:
    return [_stem(w) for w in _WORD_RE.findall(text.lower()) if w not in _STOP]


def _terms(text: str) -> set[str]:
    return set(_term_list(text))


def _relevance(chunk: str, focus_terms: set[str]) -> float:
    """How much a chunk is *about* the focus: focus-term hits, damped by length.

    Counting hits (not distinct terms) and dividing by sqrt(length) prefers a
    chunk that is wholly about termination over a long fees chunk that mentions
    termination twice at the end.
    """
    if not focus_terms:
        return 0.0
    words = _term_list(chunk)
    hits = sum(1 for w in words if w in focus_terms)
    return hits / math.sqrt(max(1, len(words)))


def _chunk_text(text: str, size: int = 1600) -> list[str]:
    """Split on blank lines and merge up to ~size chars, keeping paragraphs whole."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if current and length + len(para) > size:
            chunks.append("\n\n".join(current))
            current, length = [], 0
        current.append(para)
        length += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def build_reference_block(
    references: list[Reference],
    budget_tokens: int,
    focus: str | None = None,
) -> tuple[str, str]:
    """Return (text, mode) where mode is "full", "excerpts" or "none".

    Full mode includes every reference verbatim. Excerpt mode keeps the chunks
    that best match ``focus`` (a section's title + brief) within the budget,
    in document order, so a section still reads the parts of the contract it
    depends on when the whole bundle cannot fit alongside the plan.
    """
    references = [(label, text) for label, text in references if text and text.strip()]
    if not references:
        return "", "none"

    full = "\n\n".join(f"=== {label} ===\n{text.strip()}" for label, text in references)
    if estimate_tokens(full) <= budget_tokens:
        return full, "full"

    focus_terms = _terms(focus or "")
    scored: list[tuple[float, int, int, str, str]] = []  # (score, doc_idx, chunk_idx, label, chunk)
    for d, (label, text) in enumerate(references):
        for c, chunk in enumerate(_chunk_text(text)):
            # Earlier chunks (recitals, definitions) break ties: they carry the
            # terms every section leans on.
            scored.append((_relevance(chunk, focus_terms) - c * 1e-3, d, c, label, chunk))
    scored.sort(key=lambda s: s[0], reverse=True)

    used = 0
    kept: list[tuple[int, int, str, str]] = []
    for _score, d, c, label, chunk in scored:
        cost = estimate_tokens(chunk) + 8
        if used + cost > budget_tokens:
            continue
        kept.append((d, c, label, chunk))
        used += cost
    kept.sort()  # back into document order

    parts: list[str] = []
    current_label: str | None = None
    for _d, _c, label, chunk in kept:
        if label != current_label:
            parts.append(f"=== {label} (excerpts) ===")
            current_label = label
        parts.append(chunk)
    return "\n\n".join(parts), "excerpts"


def _strip_fences(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def _parse_json(content: str) -> dict[str, Any]:
    text = _strip_fences(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


async def _chat(client, *, system: str, user: str, model: str | None = None, **kwargs) -> str:
    resp = await openai_chat(
        client,
        model=model or drafting_model(),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
        **kwargs,
    )
    choice = resp.choices[0]
    text = (choice.message.content or "").strip()
    finish_reason = getattr(choice, "finish_reason", None)
    if isinstance(finish_reason, str) and finish_reason.lower() == "length":
        raise TruncatedOutputError(
            f"the model's output was cut off at its length limit after {len(text)} characters"
        )
    return text


async def _report(progress: Progress, message: str, fraction: float) -> None:
    if progress is None:
        return
    try:
        await progress(message, fraction)
    except Exception:  # progress is best-effort, never fails the draft
        logger.debug("progress callback failed", exc_info=True)


# ---------------------------------------------------------------------------
# Phase 1 — the plan
# ---------------------------------------------------------------------------

_PLAN_SYSTEM = (
    """You are a senior legal drafter planning a long document before it is written. \
You read the reference material in full and design a section plan that other drafters \
will execute in parallel, each writing one section without seeing the others' text. \
The plan is therefore the only thing that keeps the document coherent: every section \
brief must say exactly what the section covers, what it must NOT cover because another \
section owns it, and which sections it should cross-reference.

Return STRICT JSON only, no prose, in this shape:
{
  "title": "<document title>",
  "document_type": "<e.g. Master Services Agreement>",
  "sections": [
    {
      "number": 1,
      "title": "<section heading>",
      "brief": "<what this section must contain: clauses, terms, positions, cross-references>",
      "exclude": "<topics this section must leave to other sections, naming them>",
      "target_words": <integer>
    }
  ],
  "definitions": "<the defined terms, party names, dates, amounts, governing law and \
other facts every section must use consistently — pulled from the reference material \
where available, [BRACKETED PLACEHOLDERS] where not>",
  "style_guide": "<numbering scheme, heading style, formality, clause conventions and \
any house style visible in the reference material>"
}

"""
    + UNTRUSTED_CONTENT_RULE
)


async def plan_document(
    client,
    *,
    instructions: str,
    references: list[Reference],
    target_pages: int,
    progress: Progress = None,
) -> dict[str, Any]:
    """Produce the shared plan for a long draft from the FULL reference bundle."""
    if client is None:
        raise ValueError("No LLM client available for document drafting")
    target_pages = max(1, int(target_pages))
    target_words = target_pages * WORDS_PER_PAGE

    await _report(progress, "Reading the reference documents and planning the sections…", 0.05)
    window = context_window_tokens()
    ref_budget = window - estimate_tokens(instructions) - 12_000
    ref_block, mode = build_reference_block(references, ref_budget, focus=instructions)

    user = ""
    if ref_block:
        user += f"REFERENCE DOCUMENTS:\n{untrusted_block('Reference documents', ref_block)}\n\n"
    user += (
        f"INSTRUCTIONS:\n{instructions.strip()}\n\n"
        f"TARGET LENGTH: about {target_pages} pages (~{target_words} words). Choose a "
        f"section count that suits the document (typically {max(3, target_pages // 3)}–"
        f"{max(6, target_pages)}), and set target_words per section so they sum to "
        f"roughly {target_words}. Sections must be ordered as they will appear."
    )

    try:
        content = await _chat(
            client, system=_PLAN_SYSTEM, user=user, response_format={"type": "json_object"}
        )
        data = _parse_json(content)
    except Exception as e:  # surfaced as one clean error
        logger.warning("Draft planning failed: %s", e)
        raise RuntimeError(f"Draft planning failed: {e}") from e

    plan = normalize_plan(data, target_words)
    plan["references_mode"] = mode
    await _report(progress, f"Plan ready: {len(plan['sections'])} sections.", 0.1)
    return plan


def normalize_plan(data: dict[str, Any], target_words: int | None = None) -> dict[str, Any]:
    """Coerce a model- or user-supplied plan into the shape the generator needs."""
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise RuntimeError("Draft planning failed: the plan has no sections")
    sections: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_sections[:MAX_SECTIONS], start=1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip() or f"Section {i}"
        try:
            words = int(raw.get("target_words") or 0)
        except (TypeError, ValueError):
            words = 0
        sections.append(
            {
                "number": i,
                "title": title,
                "brief": str(raw.get("brief") or "").strip(),
                "exclude": str(raw.get("exclude") or "").strip(),
                "target_words": words,
            }
        )
    if not sections:
        raise RuntimeError("Draft planning failed: the plan has no usable sections")

    if target_words:
        total = sum(s["target_words"] for s in sections)
        if total <= 0:
            each = target_words // len(sections)
            for s in sections:
                s["target_words"] = each
        elif abs(total - target_words) > target_words * 0.15:
            scale = target_words / total
            for s in sections:
                s["target_words"] = int(s["target_words"] * scale)
    for s in sections:
        s["target_words"] = min(MAX_SECTION_WORDS, max(MIN_SECTION_WORDS, s["target_words"]))

    return {
        "title": str(data.get("title") or "").strip() or "Draft",
        "document_type": str(data.get("document_type") or "").strip(),
        "sections": sections,
        "definitions": str(data.get("definitions") or "").strip(),
        "style_guide": str(data.get("style_guide") or "").strip(),
        "target_words": sum(s["target_words"] for s in sections),
    }


# ---------------------------------------------------------------------------
# Phase 2 — sections, each in its own context window
# ---------------------------------------------------------------------------

_SECTION_SYSTEM = (
    """You are an expert legal document drafter writing ONE section of a longer \
document. Other drafters are writing the other sections at the same time from the same \
plan; you will not see their text, so the plan, the definitions and the reference \
documents are your only means of staying consistent with them.

RULES:
- Write only your section's body text, complete and final, at the target length. No \
preamble, no commentary, no notes to the reader, no closing remarks — the product adds \
its own review notice to the finished document.
- Begin with the section heading exactly as numbered and titled in the plan.
- Use the defined terms, party names and facts from DEFINITIONS exactly; never redefine \
them or invent alternatives.
- Do not draft anything listed under EXCLUDE — cross-reference the owning section by \
number instead ("subject to Section 11").
- Follow the STYLE GUIDE for numbering, headings and tone.
- Use [BRACKETED PLACEHOLDERS] for facts you do not have.

"""
    + UNTRUSTED_CONTENT_RULE
)

_RECONCILE_SYSTEM = """You are the senior reviewer of a long legal document whose sections were \
drafted in parallel by different drafters. Your job is consistency, not rewriting.

Fix only: cross-references that point to the wrong section number; inconsistent \
numbering of sections and clauses; defined terms used inconsistently or redefined; \
material duplicated between sections (keep it in the section the plan assigns it to and \
replace the duplicate with a cross-reference); contradictions between sections; \
missing transitions where a section clearly expects one. Keep every other sentence \
verbatim. Never shorten, summarise or omit content.

Output the FULL corrected document, then a line containing exactly
=== RECONCILE NOTES ===
followed by one bullet per change you made (or "- No changes." if none)."""


def _plan_block(plan: dict[str, Any]) -> str:
    lines = [f"DOCUMENT: {plan.get('title') or 'Draft'}"]
    if plan.get("document_type"):
        lines.append(f"TYPE: {plan['document_type']}")
    lines.append("SECTION PLAN (every section, so cross-references resolve):")
    for s in plan["sections"]:
        lines.append(f"{s['number']}. {s['title']} — {s['brief']}")
        if s.get("exclude"):
            lines.append(f"   Leaves to other sections: {s['exclude']}")
    return "\n".join(lines)


def _shared_block(plan: dict[str, Any], instructions: str) -> str:
    parts = [_plan_block(plan)]
    if plan.get("definitions"):
        parts.append(f"DEFINITIONS (use exactly):\n{plan['definitions']}")
    if plan.get("style_guide"):
        parts.append(f"STYLE GUIDE:\n{plan['style_guide']}")
    parts.append(f"DRAFTING INSTRUCTIONS FROM THE USER:\n{instructions.strip()}")
    return "\n\n".join(parts)


def _section_task_block(section: dict[str, Any]) -> str:
    block = (
        f"YOUR SECTION: {section['number']}. {section['title']}\n"
        f"BRIEF: {section['brief']}\n"
        f"TARGET LENGTH: about {section['target_words']} words."
    )
    if section.get("exclude"):
        block += f"\nEXCLUDE (owned by other sections): {section['exclude']}"
    return block


def _ensure_heading(text: str, section: dict[str, Any]) -> str:
    text = text.strip()
    if re.match(rf"^\s*{section['number']}\.\s", text):
        return text
    return f"{section['number']}. {section['title']}\n\n{text}"


async def _draft_section(
    client, *, system: str, prefix: str, section: dict[str, Any], semaphore: asyncio.Semaphore
) -> str:
    user = f"{prefix}\n\n{_section_task_block(section)}"
    last: Exception | None = None
    async with semaphore:
        for attempt in range(3):
            try:
                text = await _chat(client, system=system, user=user)
                if text:
                    return _ensure_heading(text, section)
                last = RuntimeError("empty section")
            except Exception as e:  # retried, then surfaced
                last = e
            await asyncio.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"Section {section['number']} ({section['title']}) failed: {last}")


async def generate_from_plan(
    client,
    *,
    plan: dict[str, Any],
    instructions: str,
    references: list[Reference],
    progress: Progress = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """Draft every section of ``plan`` in parallel, then reconcile the assembly."""
    if client is None:
        raise ValueError("No LLM client available for document drafting")
    plan = normalize_plan(plan)
    sections = plan["sections"]
    n = len(sections)

    window = context_window_tokens()
    shared = _shared_block(plan, instructions)
    shared_tokens = estimate_tokens(shared) + estimate_tokens(_SECTION_SYSTEM)
    widest_output = max(int(s["target_words"] * 1.7) + 1000 for s in sections)
    ref_budget = window - shared_tokens - widest_output - 3000

    # One identical prefix for every section when the bundle fits (cache-friendly);
    # otherwise a per-section excerpt set focused on that section's brief.
    full_block, mode = build_reference_block(references, ref_budget)
    prefixes: dict[int, str] = {}
    for s in sections:
        if mode == "excerpts":
            block, _ = build_reference_block(
                references, ref_budget, focus=f"{s['title']} {s['brief']}"
            )
        else:
            block = full_block
        prefixes[s["number"]] = (
            f"REFERENCE DOCUMENTS:\n{untrusted_block('Reference documents', block)}\n\n"
            if block
            else ""
        ) + shared

    limit = concurrency or int(getattr(settings, "draft_section_concurrency", 4))
    semaphore = asyncio.Semaphore(max(1, limit))
    done = 0
    results: dict[int, str] = {}

    async def run(section: dict[str, Any]) -> None:
        nonlocal done
        text = await _draft_section(
            client,
            system=_SECTION_SYSTEM,
            prefix=prefixes[section["number"]],
            section=section,
            semaphore=semaphore,
        )
        results[section["number"]] = text
        done += 1
        await _report(
            progress,
            f"Drafted section {done} of {n}: {section['title']}",
            0.1 + 0.7 * done / n,
        )

    await _report(
        progress,
        f"Drafting {n} sections in parallel ({'full references' if mode != 'excerpts' else 'focused excerpts'})…",
        0.1,
    )
    await asyncio.gather(*(run(s) for s in sections))

    body = "\n\n".join(results[s["number"]] for s in sections)
    title = plan["title"]
    assembled = f"{title}\n\n{body}"

    drafted = [
        {"number": s["number"], "title": s["title"], "text": results[s["number"]]} for s in sections
    ]

    await _report(progress, "Reconciling cross-references, numbering and defined terms…", 0.85)
    text, notes = await _reconcile(client, assembled, window, sections=drafted)

    # Same rule as every other model-written text: a case the draft names must
    # come from the user's instructions or their reference documents.
    text, removed = redact_unverified_case_references(
        text,
        sources=[instructions, title, *(ref_text for _label, ref_text in references)],
        known_parties=True,
    )
    if removed:
        logger.info("Long drafting removed %d unverified case reference(s)", len(removed))

    return {
        "title": title,
        "text": text,
        "case_law_removed": removed,
        "sections": drafted,
        "reconcile_notes": notes,
        "references_mode": mode,
        "plan": plan,
    }


# A reconciled section may differ from its drafted version by this much: enough
# for cross-reference fixes and de-duplication, not enough to lose a clause.
RECONCILE_SECTION_TOLERANCE = 0.15


def _norm_heading(line: str) -> str:
    return " ".join(line.lower().replace("*", "").replace("#", "").split()).rstrip(" .:")


def _find_heading(doc: str, section: dict[str, Any], after: int) -> tuple[int, int] | None:
    """Locate ``section``'s heading line in ``doc`` at or after ``after``.

    Accepts either the heading the drafter actually wrote (first line of the
    drafted section) or the plan's own "N. Title" — reconcile may normalise
    one into the other. Returns (line_start, line_end) or None.
    """
    wanted = {_norm_heading(f"{section['number']}. {section['title']}")}
    first_line = (section.get("text") or "").strip().split("\n", 1)[0]
    if first_line:
        wanted.add(_norm_heading(first_line))
    for m in re.finditer(r"^[^\n]*$", doc[after:], re.M):
        if _norm_heading(m.group(0)) in wanted:
            return after + m.start(), after + m.end()
    return None


def check_reconciled_sections(
    doc: str, sections: list[dict[str, Any]], tolerance: float = RECONCILE_SECTION_TOLERANCE
) -> str | None:
    """Explain why a reconciled document must be rejected, or None if it passes.

    Every section heading from the plan must still appear, in order, and each
    section's text must stay within ±``tolerance`` of its drafted length. A
    reconcile pass that quietly drops or shortens a clause fails here and the
    caller keeps the assembled document instead.
    """
    positions: list[tuple[int, int]] = []
    cursor = 0
    for s in sections:
        found = _find_heading(doc, s, cursor)
        if found is None:
            return f"section {s['number']} ({s['title']}) heading is missing"
        positions.append(found)
        cursor = found[1]
    for i, s in enumerate(sections):
        start = positions[i][0]
        end = positions[i + 1][0] if i + 1 < len(positions) else len(doc)
        drafted_len = len((s.get("text") or "").strip())
        if drafted_len == 0:
            continue
        actual_len = len(doc[start:end].strip())
        ratio = actual_len / drafted_len
        if ratio < 1 - tolerance:
            return (
                f"section {s['number']} ({s['title']}) came back {round((1 - ratio) * 100)}% "
                "shorter than drafted"
            )
        if ratio > 1 + tolerance:
            return (
                f"section {s['number']} ({s['title']}) came back {round((ratio - 1) * 100)}% "
                "longer than drafted"
            )
    return None


async def _reconcile(
    client, assembled: str, window: int, sections: list[dict[str, Any]] | None = None
) -> tuple[str, list[str]]:
    needed = estimate_tokens(assembled) * 2 + estimate_tokens(_RECONCILE_SYSTEM) + 2000
    if needed > window:
        return assembled, [
            "Reconcile pass skipped: the document exceeds the model's context window."
        ]
    try:
        content = await _chat(client, system=_RECONCILE_SYSTEM, user=f"DOCUMENT:\n{assembled}")
    except TruncatedOutputError as e:
        logger.warning("Reconcile pass truncated: %s", e)
        return assembled, [
            "Reconcile pass discarded: the model's output was cut off before the end of "
            "the document. The assembled sections are shown as drafted."
        ]
    except Exception as e:  # the assembled draft is still a valid result
        logger.warning("Reconcile pass failed: %s", e)
        return assembled, [
            f"Reconcile pass failed ({e}); the assembled sections are shown as drafted."
        ]

    doc, _, tail = content.partition("=== RECONCILE NOTES ===")
    doc = doc.strip()
    notes = [
        line.lstrip("-•* ").strip()
        for line in tail.strip().splitlines()
        if line.strip() and line.strip() not in ("-", "•")
    ]
    if len(doc) < len(assembled) * 0.8:
        return assembled, [
            "Reconcile pass discarded: it returned a shortened document. "
            "The assembled sections are shown as drafted."
        ]
    if sections:
        problem = check_reconciled_sections(doc, sections)
        if problem:
            logger.warning("Reconcile pass discarded: %s", problem)
            return assembled, [
                f"Reconcile pass discarded: {problem}. The assembled sections are shown as drafted."
            ]
    return doc, notes or ["No changes."]


# ---------------------------------------------------------------------------
# Revision — scoped to the sections an instruction touches
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^[ \t]*(\d{1,2})\.[ \t]+(\S[^\n]{0,140}?)[ \t]*$", re.M)
_QUOTE_RE = re.compile(r"[\"“]([^\"”]{20,})[\"”]")
# What follows the last numbered section — the signature block, schedules,
# exhibits — is not part of it. The all-caps words are matched as written:
# "Exhibit A is incorporated" in body text is not a heading.
_TRAILER_RE = re.compile(
    r"^[ \t]*(?:(?i:in\s+witness\s+whereof|\[?\s*signature\s+pages?\s+(?:follows?|to\s+follow)\s*\]?)"
    r"|SIGNATURES?\b|SCHEDULE\b|EXHIBIT\b|ANNEX\b|APPENDIX\b|ATTACHMENT\b)",
    re.M,
)

_REVISION_SYSTEM = """You are an expert legal document drafter revising ONE section of a long \
document. The full document is provided for context; every other section stays \
untouched. Return only the complete revised section, beginning with its heading, with \
no preamble or commentary. Keep every sentence that the instructions do not affect \
verbatim. Use the document's defined terms exactly."""

_TARGET_SYSTEM = """You route a revision instruction to the sections of a long legal document it \
affects. Return STRICT JSON: {"sections": [<section numbers>]} — or {"sections": "all"} \
only when the change genuinely touches the whole document (e.g. renaming a party)."""


def split_sections(text: str) -> list[dict[str, Any]]:
    """Top-level numbered sections ("3. TERM") as (number, title, start, end) spans."""
    matches = list(_HEADING_RE.finditer(text))
    sections: list[dict[str, Any]] = []
    expected = 1
    for m in matches:
        number = int(m.group(1))
        # Only a monotonically numbered top-level sequence counts as structure;
        # "2. the Supplier shall…" inside a clause list does not restart it.
        if number != expected:
            continue
        sections.append(
            {"number": number, "title": m.group(2).strip(), "start": m.start(), "end": len(text)}
        )
        expected += 1
    for i in range(len(sections) - 1):
        sections[i]["end"] = sections[i + 1]["start"]
    if sections:
        # The last section ends where the trailer begins, or revising it would
        # replace the signature block and schedules with that section alone.
        last = sections[-1]
        heading_end = text.find("\n", last["start"])
        trailer = _TRAILER_RE.search(text, heading_end if heading_end > 0 else last["start"])
        if trailer:
            last["end"] = trailer.start()
    return sections if len(sections) >= 3 else []


def _sections_for_quote(instructions: str, text: str, sections: list[dict[str, Any]]) -> list[int]:
    """A selection-based rewrite quotes the clause: find the section containing it."""
    hits: list[int] = []
    for m in _QUOTE_RE.finditer(instructions):
        fragment = m.group(1).rstrip("…").strip()[:200]
        pos = text.find(fragment)
        if pos < 0:
            continue
        for s in sections:
            if s["start"] <= pos < s["end"] and s["number"] not in hits:
                hits.append(s["number"])
    return hits


async def _target_sections(
    client, instructions: str, text: str, sections: list[dict[str, Any]]
) -> list[int] | str:
    quoted = _sections_for_quote(instructions, text, sections)
    if quoted:
        return quoted
    listing = "\n".join(
        f"{s['number']}. {s['title']}: {text[s['start'] : s['end']][:220].replace(chr(10), ' ')}…"
        for s in sections
    )
    user = f"SECTIONS:\n{listing}\n\nREVISION INSTRUCTIONS:\n{instructions.strip()}"
    try:
        data = _parse_json(
            await _chat(
                client,
                system=_TARGET_SYSTEM,
                user=user,
                model=utility_model(),
                response_format={"type": "json_object"},
            )
        )
    except Exception as e:  # fall back to a whole-document revision
        logger.warning("Revision targeting failed (%s); revising the whole draft", e)
        return "all"
    chosen = data.get("sections")
    if chosen == "all":
        return "all"
    valid = {s["number"] for s in sections}
    numbers = [n for n in (chosen or []) if isinstance(n, int) and n in valid]
    return numbers or "all"


async def revise_draft(
    client,
    *,
    draft_text: str,
    instructions: str,
    progress: Progress = None,
) -> dict[str, Any]:
    """Revise a draft; long structured drafts are revised section by section.

    Returns {"text": str, "revised_sections": [int] | "all"}.
    """
    if client is None:
        raise ValueError("No LLM client available for document drafting")
    from app.services.contract_analysis.drafting import draft_document

    sections = split_sections(draft_text) if len(draft_text) >= LONG_DRAFT_CHARS else []
    if not sections:
        result = await draft_document(client, instructions, revision_of=draft_text)
        return {
            "text": result["text"],
            "title": result.get("title"),
            "revised_sections": "all",
            "case_law_removed": result.get("case_law_removed", []),
        }

    await _report(progress, "Working out which sections the change touches…", 0.1)
    targets = await _target_sections(client, instructions, draft_text, sections)
    window = context_window_tokens()
    if targets == "all" or len(targets) > len(sections) / 2:
        if estimate_tokens(draft_text) * 2 + 3000 > window:
            raise RuntimeError(
                "This change touches most of a draft that is too long to revise in one "
                "pass — select the clause or section to change and ask again."
            )
        result = await draft_document(client, instructions, revision_of=draft_text)
        return {
            "text": result["text"],
            "title": result.get("title"),
            "revised_sections": "all",
            "case_law_removed": result.get("case_law_removed", []),
        }

    text = draft_text
    removed: list[str] = []
    # Splice from the last section backwards so earlier spans stay valid.
    ordered = sorted((s for s in sections if s["number"] in targets), key=lambda s: -s["start"])
    total = len(ordered)
    for i, s in enumerate(ordered, start=1):
        await _report(
            progress, f"Revising section {s['number']}: {s['title']}", 0.2 + 0.7 * i / total
        )
        user = (
            f"FULL DOCUMENT (context — do not return it):\n{draft_text}\n\n"
            f"SECTION TO REVISE: {s['number']}. {s['title']}\n\n"
            f"REVISION INSTRUCTIONS:\n{instructions.strip()}"
        )
        try:
            revised = await _chat(client, system=_REVISION_SYSTEM, user=user)
        except Exception as e:  # surfaced as one clean error
            raise RuntimeError(f"Revising section {s['number']} failed: {e}") from e
        if not revised:
            raise RuntimeError(f"Revising section {s['number']} returned nothing")
        revised = _ensure_heading(revised, s)
        # Only the rewritten section is model output; the existing draft and the
        # instructions are the sources a case reference may come from.
        revised, gone = redact_unverified_case_references(
            revised, sources=[instructions, draft_text], known_parties=True
        )
        removed += gone
        text = text[: s["start"]] + revised.rstrip() + "\n\n" + text[s["end"] :].lstrip("\n")
    return {
        "text": text,
        "revised_sections": [s["number"] for s in ordered][::-1],
        "case_law_removed": removed,
    }
