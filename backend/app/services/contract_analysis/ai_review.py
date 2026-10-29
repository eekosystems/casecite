"""AI-first contract review: the model reads THIS contract against THE USER'S
instructions and finds what matters — no fixed clause library required.

The seeded taxonomy/rules are supplementary detectors (cheap, deterministic,
well-grounded); this pass is the primary finder, because no canned library
covers every contract a firm sees. Same grounding bargain as everywhere else
in the pipeline: every finding must quote the offending text verbatim, the
quote must locate in the document, or the finding is discarded.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.contract_analysis.instructions import bounded_instructions
from app.services.llm_clients import openai_chat, utility_model
from app.services.quote_verify import find_quote_offset
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

WINDOW_CHARS = 90_000
WINDOW_OVERLAP = 3_000
MAX_FINDINGS_PER_WINDOW = 25
_MATCHED_TEXT_LIMIT = 200

_VALID_SEVERITIES = {"critical", "major", "minor"}


def _windows(text: str) -> list[tuple[int, str]]:
    if len(text) <= WINDOW_CHARS:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    step = WINDOW_CHARS - WINDOW_OVERLAP
    for start in range(0, len(text), step):
        out.append((start, text[start : start + WINDOW_CHARS]))
        if start + WINDOW_CHARS >= len(text):
            break
    return out


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < s_end and s_start < end for s_start, s_end in spans)


async def ai_review_findings(
    client,
    text: str,
    instructions: str | None,
    contract_type: str,
    existing_spans: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Model-driven findings, instruction-first, verbatim-grounded.

    Returns finding dicts in the pipeline's standard shape with refs "ai-N".
    Findings whose quote cannot be located are dropped; findings overlapping
    a span the rules already flagged are dropped (the AI adds what the
    detectors missed, it doesn't duplicate them). Never raises.
    """
    if client is None:
        return []
    goal = bounded_instructions(instructions) or (
        "Neutral review: flag anything risky, one-sided, or off-market."
    )
    findings: list[dict[str, Any]] = []
    taken = list(existing_spans)
    idx = 0
    for window_start, window in _windows(text):
        try:
            prompt = (
                "You are reviewing a contract for a lawyer. Read the contract "
                "text below and identify every issue that matters FOR THIS "
                f"GOAL:\n{goal}\n\n"
                f"Contract type (best guess, may be wrong): {contract_type}\n\n"
                "Judge the contract on its own terms — whatever kind of "
                "agreement it is. For each issue: a short title; severity "
                '("critical"|"major"|"minor") from the goal\'s perspective; '
                "one sentence on why it matters for the goal; ONE contiguous "
                "quote of the offending text copied EXACTLY, character for "
                "character, from the contract text below; and optionally the "
                "direction a fix should take.\n\n"
                f"Report at most {MAX_FINDINGS_PER_WINDOW} issues, most "
                "important first. Only issues grounded in actual text — no "
                "speculation about what might be elsewhere in the document.\n\n"
                'Return STRICT JSON: {"issues": [{"title": "...", '
                '"severity": "...", "why": "...", "quote": "...", '
                '"suggested_direction": "..." | null}, ...]}\n\n'
                f"{UNTRUSTED_CONTENT_RULE}\n\n"
                "CONTRACT TEXT:\n" + untrusted_block("Contract text", window)
            )
            resp = await openai_chat(
                client,
                model=utility_model(),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:  # one window failing never kills the review
            logger.warning("AI review window failed: %s", e)
            continue

        for raw in (data.get("issues") or [])[:MAX_FINDINGS_PER_WINDOW]:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            quote = str(raw.get("quote") or "").strip()
            if not title or not quote:
                continue
            # Ground within the window first (cheap), then translate to
            # document offsets; fall back to a whole-document search.
            offsets = find_quote_offset(window, quote)
            if offsets is not None:
                start, end = window_start + offsets[0], window_start + offsets[1]
            else:
                doc_offsets = find_quote_offset(text, quote)
                if doc_offsets is None:
                    continue  # unlocatable — exactly the fabrication we refuse to ship
                start, end = doc_offsets
            if _overlaps(start, end, taken):
                continue  # the rules already cover this text
            taken.append((start, end))
            severity = str(raw.get("severity") or "").strip().lower()
            direction = str(raw.get("suggested_direction") or "").strip() or None
            findings.append(
                {
                    "ref": f"ai-{idx}",
                    "kind": "ai",
                    "name": title[:_MATCHED_TEXT_LIMIT],
                    "severity": severity if severity in _VALID_SEVERITIES else "major",
                    "matched_text": text[start:end][:600],
                    "span_start": start,
                    "span_end": end,
                    "clause_slug": None,
                    "why": str(raw.get("why") or "").strip() or None,
                    "suggested_language": direction,
                }
            )
            idx += 1
    return findings
