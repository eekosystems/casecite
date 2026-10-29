"""Schema-driven, quote-verified field extraction for tabular review.

The same grounding discipline as the contract key-terms table
(``contract_analysis/key_terms.py``), generalised to a field list the
reviewer defines: one utility-model call per document is constrained to
labelled excerpts of the text, and every value it returns must carry a quote
that verifies verbatim against the document via ``find_quote_offset``. A
value whose quote cannot be located is kept but marked unverified; a field
the model says the excerpts do not establish is recorded as not found. The
model proposes, the document disposes.

Excerpt selection: a document that fits the budget is sent whole. A longer
one gets its preamble, the clause spans the classifier tagged (when the
document has been through contract analysis) and then the paragraphs most
relevant to the field names and descriptions until the budget is spent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.models.review_tables import (
    CELL_ERROR,
    CELL_EXTRACTED,
    CELL_NOT_FOUND,
    CELL_UNVERIFIED,
)
from app.services.contract_analysis.key_terms import (
    EXCERPT_CHARS,
    PREAMBLE_CHARS,
    TOTAL_EXCERPT_BUDGET,
)
from app.services.contract_analysis.long_drafting import _chunk_text, _relevance, _terms
from app.services.llm_clients import openai_chat
from app.services.quote_verify import find_quote_offset
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

QUOTE_MAX_CHARS = 600
# Rough prompt overhead and per-field cost, in tokens, for the cost estimate.
_PROMPT_OVERHEAD_TOKENS = 350
_PER_FIELD_TOKENS = 40

# How each field type is phrased to the model.
TYPE_GUIDANCE: dict[str, str] = {
    "text": "a short phrase",
    "date": "an ISO date (YYYY-MM-DD) when the document gives a full date, else as written",
    "money": "the amount with its currency, e.g. USD 250,000",
    "number": "a plain number",
    "boolean": '"yes" or "no"',
    "duration": "a length of time, e.g. 12 months or 30 days",
}


class ExtractionError(RuntimeError):
    """The model call failed or returned something unusable."""


@dataclass
class CellResult:
    """One field's answer for one document, with its verification state."""

    status: str
    value: str | None = None
    quote: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    verified: bool = False
    error: str | None = None

    @classmethod
    def not_found(cls) -> CellResult:
        return cls(status=CELL_NOT_FOUND)

    @classmethod
    def failed(cls, message: str) -> CellResult:
        return cls(status=CELL_ERROR, error=message[:500])


def _field_focus(fields: list[dict[str, Any]]) -> str:
    return " ".join(f"{f.get('name', '')} {f.get('description', '')}" for f in fields)


def _tagged_excerpts(text: str, tags: list[dict[str, Any]], budget: int) -> list[tuple[str, str]]:
    """Labelled clause spans, one per canonical slug, within ``budget`` chars."""
    out: list[tuple[str, str]] = []
    used = 0
    seen: set[str] = set()
    for tag in tags:
        slug = tag.get("canonical_slug")
        start, end = tag.get("span_start"), tag.get("span_end")
        if not slug or slug in seen or start is None or end is None:
            continue
        seen.add(slug)
        snippet = text[start : min(end, start + EXCERPT_CHARS)]
        if used + len(snippet) > budget:
            break
        used += len(snippet)
        out.append((slug, snippet))
    return out


def select_excerpts(
    text: str, fields: list[dict[str, Any]], tags: list[dict[str, Any]] | None = None
) -> list[tuple[str, str]]:
    """Labelled excerpts of ``text`` for the model, within the excerpt budget.

    Whole document when it fits; otherwise the preamble, the tagged clause
    spans, then the paragraphs most relevant to the schema, in document
    order, until the budget is spent.
    """
    if len(text) <= TOTAL_EXCERPT_BUDGET:
        return [("document", text)]

    excerpts: list[tuple[str, str]] = [("preamble", text[:PREAMBLE_CHARS])]
    used = PREAMBLE_CHARS
    tagged = _tagged_excerpts(text, tags or [], TOTAL_EXCERPT_BUDGET - used)
    excerpts.extend(tagged)
    used += sum(len(snippet) for _label, snippet in tagged)

    focus_terms = _terms(_field_focus(fields))
    remaining = TOTAL_EXCERPT_BUDGET - used
    if remaining <= 0 or not focus_terms:
        return excerpts

    # A chunk already inside the preamble or a tagged span is not sent twice; a
    # chunk that merely contains a short tagged span still carries new text.
    covered = [snippet for _label, snippet in excerpts]
    scored: list[tuple[float, int, str]] = []
    for idx, chunk in enumerate(_chunk_text(text[PREAMBLE_CHARS:])):
        if any(chunk in c for c in covered):
            continue
        scored.append((_relevance(chunk, focus_terms) - idx * 1e-3, idx, chunk))
    scored.sort(key=lambda s: s[0], reverse=True)

    kept: list[tuple[int, str]] = []
    for score, idx, chunk in scored:
        if score <= 0:
            break
        if used + len(chunk) > TOTAL_EXCERPT_BUDGET:
            continue
        kept.append((idx, chunk))
        used += len(chunk)
    kept.sort()
    excerpts.extend((f"passage_{idx + 1}", chunk) for idx, chunk in kept)
    return excerpts


def _field_lines(fields: list[dict[str, Any]]) -> str:
    lines = []
    for f in fields:
        guidance = TYPE_GUIDANCE.get(str(f.get("type") or "text"), TYPE_GUIDANCE["text"])
        desc = f" — {f['description']}" if f.get("description") else ""
        lines.append(f'- "{f["key"]}": {f.get("name", f["key"])} (answer as {guidance}){desc}')
    return "\n".join(lines)


def build_prompt(fields: list[dict[str, Any]], excerpts: list[tuple[str, str]]) -> str:
    excerpt_block = "\n\n".join(f"[{label}]\n{snippet}" for label, snippet in excerpts)
    return (
        "Extract the fields below from the labelled excerpts of a legal document. "
        "For each field give the concise value (one line, specific — amounts, dates, "
        'notice windows), and "quote" — ONE contiguous passage copied EXACTLY, '
        "character for character, from the excerpts that states it. If the excerpts "
        'do not establish a field, set "not_found": true for it. Never infer a value '
        "the text does not state.\n\n"
        f"FIELDS:\n{_field_lines(fields)}\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"EXCERPTS:\n{untrusted_block('Document excerpts', excerpt_block)}\n\n"
        'Return STRICT JSON: {"fields": {"<key>": {"value": "...", "quote": "...", '
        '"not_found": false}, ...}} with an entry for every field key.'
    )


def _cell_from_raw(text: str, raw: Any) -> CellResult:
    if not isinstance(raw, dict) or raw.get("not_found"):
        return CellResult.not_found()
    value = str(raw.get("value") or "").strip()
    if not value:
        return CellResult.not_found()
    quote = str(raw.get("quote") or "").strip()
    offsets = find_quote_offset(text, quote) if quote else None
    if offsets is None:
        return CellResult(
            status=CELL_UNVERIFIED,
            value=value[:1000],
            quote=quote[:QUOTE_MAX_CHARS] or None,
            verified=False,
        )
    return CellResult(
        status=CELL_EXTRACTED,
        value=value[:1000],
        quote=text[offsets[0] : offsets[1]][:QUOTE_MAX_CHARS],
        span_start=offsets[0],
        span_end=offsets[1],
        verified=True,
    )


async def extract_fields(
    client: Any,
    text: str,
    fields: list[dict[str, Any]],
    *,
    model: str,
    tags: list[dict[str, Any]] | None = None,
) -> dict[str, CellResult]:
    """One model call for every field of one document; a CellResult per field key.

    Raises ExtractionError when the call fails or the answer is not the
    expected JSON — the caller records that as an error on every cell rather
    than silently leaving the row empty.
    """
    excerpts = select_excerpts(text, fields, tags)
    prompt = build_prompt(fields, excerpts)
    try:
        resp = await openai_chat(
            client,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError as e:
        raise ExtractionError(f"Model returned invalid JSON: {e}") from e
    except Exception as e:  # provider SDKs raise their own base classes
        raise ExtractionError(f"{type(e).__name__}: {e}") from e

    answers = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise ExtractionError("Model answer had no 'fields' object")
    return {f["key"]: _cell_from_raw(text, answers.get(f["key"])) for f in fields}


def estimate_input_tokens(text_chars: int, field_count: int) -> int:
    """Approximate prompt tokens for one document: excerpts plus instructions."""
    excerpt_chars = min(max(text_chars, 0), TOTAL_EXCERPT_BUDGET)
    return excerpt_chars // 4 + _PROMPT_OVERHEAD_TOKENS + _PER_FIELD_TOKENS * field_count
