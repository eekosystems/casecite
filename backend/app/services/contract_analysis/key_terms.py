"""Structured key-terms extraction — the scannable table at the top of a review.

Clause tags (which carry real offsets) tell us WHERE each concept lives; a
single schema'd LLM call constrained to those tagged excerpts extracts the
VALUE; and every extracted value must carry a quote that verifies verbatim
against the document via ``find_quote_offset``. A value whose quote cannot be
located is kept but marked ``verified: False`` — the UI de-emphasizes it and
never presents it as sourced.

This is the same grounding discipline as the case-law pipeline: the model
proposes, the document disposes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.llm_clients import openai_chat, utility_model
from app.services.quote_verify import find_quote_offset
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

PREAMBLE_CHARS = 3_000
EXCERPT_CHARS = 4_000
TOTAL_EXCERPT_BUDGET = 30_000

# field -> (label shown to the model, clause slugs whose tagged spans contain it)
KEY_TERM_FIELDS: dict[str, tuple[str, list[str]]] = {
    "effective_date": ("Effective date of the agreement", []),
    "term": ("Initial term length", ["term"]),
    "renewal": ("Renewal mechanics (auto-renewal? notice window?)", ["term"]),
    "termination": (
        "Termination rights (who may terminate, for what, on how much notice)",
        ["termination_for_convenience", "termination_for_cause", "effects_of_termination"],
    ),
    "payment_terms": ("Payment terms (amounts, net days, late fees)", ["payment_terms"]),
    "governing_law": ("Governing law (state/country)", ["governing_law"]),
    "liability_cap": (
        "Limitation of liability (cap amount or formula, carve-outs)",
        ["limitation_of_liability"],
    ),
    "indemnification": ("Indemnification (who indemnifies whom, for what)", ["indemnification"]),
    "ip_ownership": ("IP ownership / license grants", ["ip_assignment", "ip_license"]),
    "confidentiality": ("Confidentiality obligations and duration", ["confidentiality"]),
    "assignment": ("Assignment (permitted? consent required?)", ["assignment"]),
    "dispute_resolution": (
        "Dispute resolution (arbitration? venue? jury waiver?)",
        ["arbitration", "venue_jurisdiction", "jury_waiver"],
    ),
}


def _collect_excerpts(text: str, tags: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Labeled excerpts: the preamble plus every tagged clause span."""
    excerpts: list[tuple[str, str]] = [("preamble", text[:PREAMBLE_CHARS])]
    used = PREAMBLE_CHARS
    seen_slugs: set[str] = set()
    for tag in tags:
        slug = tag.get("canonical_slug")
        start, end = tag.get("span_start"), tag.get("span_end")
        if not slug or slug in seen_slugs or start is None or end is None:
            continue
        seen_slugs.add(slug)
        snippet = text[start : min(end, start + EXCERPT_CHARS)]
        if used + len(snippet) > TOTAL_EXCERPT_BUDGET:
            break
        used += len(snippet)
        excerpts.append((slug, snippet))
    return excerpts


async def extract_key_terms(
    client, text: str, tags: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Extract the key-terms table. Returns {} when no LLM client is available.

    Each returned field: {value, quote, span_start, span_end, verified}.
    Fields the model marks not-found are omitted entirely.
    """
    if client is None:
        return {}

    excerpts = _collect_excerpts(text, tags)
    excerpt_block = "\n\n".join(f"[{label}]\n{snippet}" for label, snippet in excerpts)
    field_lines = "\n".join(
        f'- "{field}": {label}' for field, (label, _slugs) in KEY_TERM_FIELDS.items()
    )
    prompt = (
        "Extract the key terms of this contract from the labeled excerpts "
        "below. For each field: the concise value (one line, specific — "
        'amounts, dates, notice windows), and "quote" — ONE contiguous '
        "passage copied EXACTLY, character for character, from the excerpts "
        "that states it. If an excerpt does not establish the field, set "
        '"not_found": true for it.\n\n'
        f"FIELDS:\n{field_lines}\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"EXCERPTS:\n{untrusted_block('Contract excerpts', excerpt_block)}\n\n"
        'Return STRICT JSON: {"fields": {"<field>": {"value": "...", '
        '"quote": "...", "not_found": false}, ...}} with an entry for every field.'
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
    except Exception as e:  # key terms are an enhancement, never fatal
        logger.warning("Key-terms extraction failed; continuing without: %s", e)
        return {}

    fields = data.get("fields")
    if not isinstance(fields, dict):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for field in KEY_TERM_FIELDS:
        raw = fields.get(field)
        if not isinstance(raw, dict) or raw.get("not_found"):
            continue
        value = str(raw.get("value") or "").strip()
        if not value:
            continue
        quote = str(raw.get("quote") or "").strip()
        offsets = find_quote_offset(text, quote) if quote else None
        out[field] = {
            "value": value,
            "quote": text[offsets[0] : offsets[1]][:600] if offsets else (quote[:600] or None),
            "span_start": offsets[0] if offsets else None,
            "span_end": offsets[1] if offsets else None,
            "verified": offsets is not None,
        }
    return out
