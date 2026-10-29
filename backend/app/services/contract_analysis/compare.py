"""Clause-by-clause contract comparison — version-vs-version or contract-vs-template.

Both documents are segmented with the clause_intel segmenter (pure function, no
DB), the section lists go to ONE utility-model call that aligns them by topic
and describes the delta, and every quote the model returns must verify verbatim
against its source document via ``find_quote_offset``. A quote that cannot be
located is kept as a claim but stripped of its quote/spans and marked
``verified: False`` on that side — same grounding discipline as key_terms.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.clause_intel.classifier import segment
from app.services.llm_clients import openai_chat, utility_model
from app.services.quote_verify import find_quote_offset
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

SECTION_PREVIEW_CHARS = 600
TITLE_CHARS = 80
PROMPT_BUDGET_CHARS = 60_000
MAX_CLAUSES = 40

_VALID_STATUSES = {"changed", "added", "removed", "unchanged"}


def _sections(text: str) -> list[dict[str, Any]]:
    """Segment a document into (title, start, end, preview) sections."""
    out: list[dict[str, Any]] = []
    for seg in segment(text):
        first_line = seg.text.split("\n", 1)[0].strip()
        out.append(
            {
                "title": first_line[:TITLE_CHARS] or "(untitled)",
                "start": seg.start,
                "end": seg.end,
                "preview": seg.text[:SECTION_PREVIEW_CHARS],
            }
        )
    if not out and text.strip():
        # Segmenter found nothing usable (very short doc) — treat whole text as one section.
        out.append(
            {
                "title": text.strip().split("\n", 1)[0][:TITLE_CHARS],
                "start": 0,
                "end": len(text),
                "preview": text[:SECTION_PREVIEW_CHARS],
            }
        )
    return out


def _section_block(label: str, sections: list[dict[str, Any]], budget: int) -> str:
    """Render a document's section list, stopping when the char budget runs out."""
    lines: list[str] = [f"=== {label} ==="]
    used = len(lines[0])
    for i, sec in enumerate(sections, 1):
        entry = f"[{label} §{i}: {sec['title']}]\n{sec['preview']}"
        if used + len(entry) > budget:
            lines.append("[... remaining sections omitted for length ...]")
            break
        used += len(entry)
        lines.append(entry)
    return "\n\n".join(lines)


def _verify_side(text: str, raw_quote: Any) -> dict[str, Any]:
    """Verify one side's quote against its source document.

    Returns {quote, span_start, span_end, verified}. No quote proposed →
    everything null (nothing to verify). Proposed but unlocatable → nulls with
    verified False. Located → real span with verified True.
    """
    quote = str(raw_quote or "").strip()
    if not quote:
        return {"quote": None, "span_start": None, "span_end": None, "verified": None}
    offsets = find_quote_offset(text, quote)
    if offsets is None:
        return {"quote": None, "span_start": None, "span_end": None, "verified": False}
    return {
        "quote": text[offsets[0] : offsets[1]][:600],
        "span_start": offsets[0],
        "span_end": offsets[1],
        "verified": True,
    }


async def compare_contracts(
    client,
    text_a: str,
    text_b: str,
    label_a: str = "Version A",
    label_b: str = "Version B",
    focus: str | None = None,
) -> dict:
    """Compare two contracts clause by clause.

    ``focus`` is the user's own instruction for the comparison ("focus on
    payment terms and liability") — it steers which topics get attention
    without disabling the quote-verification discipline.

    Returns {"overall": str, "clauses": [...], "label_a": str, "label_b": str}.
    Each clause entry: {topic, status, summary, quote_a, span_a_start,
    span_a_end, verified_a, quote_b, span_b_start, span_b_end, verified_b}.

    Raises ValueError only when ``client`` is None; any other failure returns a
    shaped "Comparison failed." result with a logged warning — never raises.
    """
    if client is None:
        raise ValueError("No LLM client available for contract comparison")

    failed = {
        "overall": "Comparison failed.",
        "clauses": [],
        "label_a": label_a,
        "label_b": label_b,
    }

    sections_a = _sections(text_a)
    sections_b = _sections(text_b)
    per_doc_budget = PROMPT_BUDGET_CHARS // 2
    block_a = _section_block(label_a, sections_a, per_doc_budget)
    block_b = _section_block(label_b, sections_b, per_doc_budget)

    prompt = (
        "Compare these two contracts clause by clause. Align sections by TOPIC "
        "(e.g. Term, Termination, Limitation of Liability), not by number — the "
        "documents may order or title them differently.\n\n"
        f'Document A is "{label_a}"; Document B is "{label_b}".\n\n'
        "For each topic report:\n"
        '- "status": "changed" (present in both, materially different), '
        '"added" (in B but not A), "removed" (in A but not B), or "unchanged".\n'
        '- "summary": ONE sentence on what changed and which party it favors.\n'
        '- "quote_a": one contiguous passage copied EXACTLY, character for '
        "character, from Document A's sections (empty string if the topic is "
        "absent from A).\n"
        '- "quote_b": same for Document B (empty string if absent from B).\n'
        "Skip boilerplate-identical clauses; include an unchanged clause only "
        "when its stability is itself notable.\n\n"
        + (
            f"THE USER'S COMPARISON INSTRUCTIONS (prioritize these topics and "
            f"answer what they asked): {focus.strip()}\n\n"
            if focus and focus.strip()
            else ""
        )
        + f"{UNTRUSTED_CONTENT_RULE}\n\n"
        + f"{untrusted_block(label_a, block_a)}\n\n{untrusted_block(label_b, block_b)}\n\n"
        'Return STRICT JSON: {"clauses": [{"topic": "...", "status": "...", '
        '"summary": "...", "quote_a": "...", "quote_b": "..."}], '
        '"overall": "2-3 sentence summary of the delta"}.'
    )

    try:
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:  # comparison must degrade, never crash the caller
        logger.warning("Contract comparison failed; returning empty result: %s", e)
        return failed

    raw_clauses = data.get("clauses")
    if not isinstance(raw_clauses, list):
        raw_clauses = []

    clauses: list[dict[str, Any]] = []
    for raw in raw_clauses[:MAX_CLAUSES]:
        if not isinstance(raw, dict):
            continue
        topic = str(raw.get("topic") or "").strip()
        if not topic:
            continue
        status = str(raw.get("status") or "").strip().lower()
        if status not in _VALID_STATUSES:
            status = "changed"
        side_a = _verify_side(text_a, raw.get("quote_a"))
        side_b = _verify_side(text_b, raw.get("quote_b"))
        clauses.append(
            {
                "topic": topic,
                "status": status,
                "summary": str(raw.get("summary") or "").strip(),
                "quote_a": side_a["quote"],
                "span_a_start": side_a["span_start"],
                "span_a_end": side_a["span_end"],
                "verified_a": side_a["verified"],
                "quote_b": side_b["quote"],
                "span_b_start": side_b["span_start"],
                "span_b_end": side_b["span_end"],
                "verified_b": side_b["verified"],
            }
        )

    overall = str(data.get("overall") or "").strip()
    if not overall and not clauses:
        return failed
    return {
        "overall": overall or "No material differences identified.",
        "clauses": clauses,
        "label_a": label_a,
        "label_b": label_b,
    }
