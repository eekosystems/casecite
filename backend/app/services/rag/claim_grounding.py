"""Claim-level grounding and support checking for chat answers over the user's documents.

Chunk citations explain retrieval, not the answer — "this passage matched your
query" tells a lawyer nothing about where the $3,000 figure came from. This
pass makes the ANSWER's own claims the citation unit: extract each factual
claim (parties, amounts, dates, terms), find the exact verbatim contract
language that establishes it, verify it character-for-character against the
source file, and pin it to the file + char span so the UI can anchor it inside
the opened document.

A verbatim quote proves the words exist, not that they say what the answer
claims: "either party may terminate on 30 days' notice" quotes a real clause
whose cure right and Material Breach condition it dropped. So a second,
independent pass reads each claim against its quoted passage *in context* and
labels it supported, partial (a condition, exception or threshold is missing),
contradicted, or unsupported. Claims the extraction could not trace to any
file are reported as unsupported rather than silently dropped. The result
carries a notice listing every claim that is not fully supported, and what of
the answer was not checked at all, for the answer to show.

Whenever grounding succeeds, chunk-provenance citations are replaced with
these claim citations. It is strictly best-effort: any failure keeps the
chunk citations and never breaks the chat response.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.models.schemas import Citation, CitationLogic, CitationStatus, ReasoningStep
from app.services.llm_clients import make_openai, openai_chat, utility_model
from app.services.provider_policy import enforce_openai_client
from app.services.quote_verify import find_quote_offset
from app.services.rag.generation import chosen_provider_text, strip_json_fences
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

# Grounding reads the source files — keep the fan-in bounded. Beyond this many
# files, the best-matching ones are checked and the rest reported as unchecked.
GROUND_MAX_FILES = 6
# Chars of each file in the prompt: the opening (parties, definitions) plus
# the regions around the passages retrieval found, so a long document is read
# where the answer came from rather than only from its first pages.
GROUND_TEXT_LIMIT = 30_000
GROUND_LEAD_CHARS = 8_000
GROUND_WINDOW = 1_500
ANSWER_LIMIT = 12_000
MAX_CLAIMS = 15
PASSAGE_LIMIT = 600
# Source text either side of a quote that the support check sees: provisos,
# exceptions and cure periods usually sit right next to the operative words.
SUPPORT_CONTEXT = 700

SUPPORTED = "supported"
PARTIAL = "partial"
CONTRADICTED = "contradicted"
UNSUPPORTED = "unsupported"
SUPPORT_LABELS = (SUPPORTED, PARTIAL, CONTRADICTED, UNSUPPORTED)
_LABEL_TEXT = {
    PARTIAL: "the source adds a condition the answer leaves out",
    CONTRADICTED: "the source says something different",
    UNSUPPORTED: "no supporting passage was found",
}
_LABEL_TITLE = {
    SUPPORTED: "Supported by the source",
    PARTIAL: "Only partly supported",
    CONTRADICTED: "Contradicted by the source",
    UNSUPPORTED: "Not supported by the source",
}


def _api_key(user_keys) -> str | None:
    if user_keys and getattr(user_keys, "openai", None):
        return user_keys.openai
    return None


# PDF extraction turns form tables into pipe/dash soup ("| --- | --- |").
# Quotes must stay verbatim, but we can (a) trim markup off the EDGES of a
# matched span — the remainder is still character-exact — and (b) refuse
# quotes that are mostly markup rather than language.
_EDGE_JUNK = " \t\r\n|–—-:·.,;"
MIN_QUOTE_CHARS = 12
MIN_CONTENT_RATIO = 0.45


def tidy_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Shrink a span to drop table/divider junk at its edges; None if nothing left."""
    while start < end and text[start] in _EDGE_JUNK:
        start += 1
    while end > start and text[end - 1] in _EDGE_JUNK:
        end -= 1
    if end - start < MIN_QUOTE_CHARS:
        return None
    return start, end


def content_ratio(s: str) -> float:
    """Fraction of a quote that is actual language (alphanumerics) vs markup."""
    if not s:
        return 0.0
    return sum(ch.isalnum() for ch in s) / len(s)


def line_of(text: str, offset: int) -> int:
    """1-based line number of a char offset in the extracted text."""
    return text.count("\n", 0, offset) + 1


def file_excerpt(text: str, ranges: list[tuple[int, int]]) -> str:
    """What of a file the grounding prompt shows: all of it, if it fits.

    Otherwise the opening (where parties and definitions are) plus a window
    around each retrieved passage, in document order, separated by "[...]".
    Every piece is a verbatim substring, so quotes copied from the excerpt
    still verify against the full text.
    """
    if len(text) <= GROUND_TEXT_LIMIT:
        return text
    if not ranges:
        return text[:GROUND_TEXT_LIMIT]
    windows = [(0, GROUND_LEAD_CHARS)]
    for start, end in sorted(ranges):
        windows.append((max(0, start - GROUND_WINDOW), min(len(text), end + GROUND_WINDOW)))
    merged: list[list[int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts: list[str] = []
    budget = GROUND_TEXT_LIMIT
    for start, end in merged:
        if budget <= 0:
            break
        piece = text[start : min(end, start + budget)]
        parts.append(piece)
        budget -= len(piece)
    return "\n[...]\n".join(parts)


def build_grounding_prompt(query: str, answer: str, files: list[dict[str, Any]]) -> str:
    # File text is third-party content: delimited so nothing inside it can
    # be read as an instruction. The block passes the text through verbatim,
    # so quotes still verify character-for-character against the source.
    file_blocks = "\n\n".join(
        f'[{f["ref"]}] FILE "{f["name"]}":\n'
        + untrusted_block(
            f"{f['ref']}: {f['name']}", f.get("excerpt") or f["text"][:GROUND_TEXT_LIMIT]
        )
        for f in files
    )
    return (
        "You are grounding an AI answer in the user's source documents. List the "
        "answer's distinct factual claims — parties, amounts, dates, deadlines, "
        "terms, obligations, conditions: every concrete point a lawyer would want "
        "verified. For EACH claim, give the ref of the file it comes from and the "
        "single EXACT VERBATIM passage from that file that establishes it — copy the "
        "characters exactly as they appear, never paraphrase or stitch fragments. "
        "Quote the SHORTEST contiguous readable span that establishes the claim — "
        "a sentence or clause, never a whole table row. If a fact appears only in "
        "a table or form, quote just the label and value, not runs of empty cells "
        'or divider markup like "---" or "| | |". Never pick a passage that is '
        "mostly pipes, dashes, or blanks; if two facts sit far apart, make them "
        "two separate claims. If NO file supports a claim, still list it, with "
        '"file_ref": null and "quote": "" — the user must see which statements '
        "have no support.\n\n"
        "Also copy, for each claim, the sentence of the ANSWER that makes it "
        '("answer_quote", verbatim from the answer) — this is how the user '
        "traces answer → source.\n\n"
        "Return STRICT JSON: "
        '{"claims":[{"claim":"...","answer_quote":"...verbatim from the answer...",'
        '"file_ref":"f0","quote":"...verbatim from the file..."}]} '
        f"(at most {MAX_CLAIMS} claims, most important first)\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"QUESTION:\n{query}\n\n"
        f"ANSWER:\n{answer[:ANSWER_LIMIT]}\n\n"
        f"FILES:\n{file_blocks}"
    )


def build_support_prompt(query: str, items: list[dict[str, Any]]) -> str:
    """The independent check: does each quoted passage, read in context, support its claim?"""
    blocks = "\n\n".join(
        f"ITEM {it['n']}\nSTATEMENT: {it['claim']}\n"
        + (f"AS WRITTEN IN THE ANSWER: {it['answer_quote']}\n" if it.get("answer_quote") else "")
        + f"QUOTED PASSAGE: {it['quote']}\n"
        + "SOURCE AROUND THE PASSAGE:\n"
        + untrusted_block(f"item {it['n']}: {it['source']}", it["context"])
        for it in items
    )
    return (
        "You check statements an AI assistant made about a user's legal documents "
        "against the source text. For each item, read the statement, then the "
        "quoted passage within the surrounding source text, and label it:\n"
        '- "supported": the source says this, including every condition that matters.\n'
        '- "partial": the source supports it only subject to a condition, exception, '
        "threshold, deadline, notice or cure requirement, or carve-out that the "
        "statement leaves out or misstates, and that changes what a lawyer would "
        "conclude. Name it.\n"
        '- "contradicted": the source says something different.\n'
        '- "unsupported": the source shown does not address the statement.\n'
        "Judge only against the source shown, never outside knowledge. Do not flag "
        "wording differences, harmless rounding, or detail the statement did not "
        "need.\n\n"
        "Return STRICT JSON: "
        '{"items":[{"n":0,"label":"supported","note":"one sentence: what is missing '
        'or different; empty when supported"}]}\n\n'
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"QUESTION THE ANSWER ADDRESSED:\n{query}\n\n"
        f"{blocks}"
    )


def _retrieval_scores(search_results: list[dict[str, Any]]) -> dict[str, float]:
    """Best real retrieval similarity per document among this request's results."""
    best: dict[str, float] = {}
    for r in search_results:
        did = (r.get("metadata") or {}).get("document_id")
        score = r.get("similarity")
        if did and isinstance(score, (int, float)):
            best[did] = max(best.get(did, 0.0), float(score))
    return best


def _retrieved_ranges(search_results: list[dict[str, Any]], doc_id: str) -> list[tuple[int, int]]:
    ranges = []
    for r in search_results:
        meta = r.get("metadata") or {}
        if meta.get("document_id") != doc_id:
            continue
        start, end = meta.get("char_start"), meta.get("char_end")
        if isinstance(start, int) and isinstance(end, int) and end > start:
            ranges.append((start, end))
    return ranges


def _chunk_at(search_results: list[dict[str, Any]], doc_id: str, offset: int) -> dict[str, Any]:
    """Metadata of the retrieved chunk of ``doc_id`` that contains ``offset`` (or {})."""
    for r in search_results:
        meta = r.get("metadata") or {}
        start, end = meta.get("char_start"), meta.get("char_end")
        if (
            meta.get("document_id") == doc_id
            and isinstance(start, int)
            and isinstance(end, int)
            and start <= offset < end
        ):
            return meta
    return {}


async def _complete_json(prompt: str, *, user_keys, model: str | None, purpose: str) -> Any:
    """One JSON completion on the user's chosen provider, else the utility model."""
    raw = await chosen_provider_text(prompt, user_keys=user_keys, model=model)
    if raw is None:
        client = make_openai(_api_key(user_keys), async_=True)
        if client is None:
            return None
        enforce_openai_client(client, purpose)
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
    try:
        return json.loads(strip_json_fences(raw) or "{}")
    except (ValueError, TypeError):
        return None


@dataclass
class FlaggedClaim:
    """A statement in the answer that its sources do not fully support."""

    claim: str
    answer_quote: str
    label: str
    note: str = ""
    source: str | None = None


@dataclass
class GroundingResult:
    citations: list[Citation] = field(default_factory=list)
    flagged: list[FlaggedClaim] = field(default_factory=list)
    claims: int = 0
    files_checked: int = 0
    files_total: int = 0
    answer_truncated: bool = False
    support_checked: bool = False

    def notice(self) -> str | None:
        """Text to append to the answer: unsupported claims and unchecked parts."""
        lines: list[str] = []
        if self.flagged:
            n = len(self.flagged)
            lines.append(
                f"⚠ Source check: {n} statement{'s' if n != 1 else ''} in this answer "
                f"{'are' if n != 1 else 'is'} not fully supported by your documents:"
            )
            for f in self.flagged[:8]:
                said = (f.answer_quote or f.claim).strip()
                if len(said) > 160:
                    said = said[:157].rsplit(" ", 1)[0] + "..."
                detail = f.note.strip() or _LABEL_TEXT.get(f.label, "")
                lines.append(f'- "{said}" — {detail}')
            if n > 8:
                lines.append(f"- ...and {n - 8} more (see the citations).")
        gaps = []
        if self.answer_truncated:
            gaps.append(f"only the first {ANSWER_LIMIT:,} characters of the answer were checked")
        if self.files_checked < self.files_total:
            gaps.append(f"{self.files_checked} of the {self.files_total} source files were checked")
        if gaps:
            lines.append("Not everything was verified: " + "; ".join(gaps) + ".")
        return "\n".join(lines) if lines else None


async def _check_support(
    query: str,
    items: list[dict[str, Any]],
    *,
    user_keys,
    model: str | None,
) -> dict[int, dict[str, str]] | None:
    """Labels for each item by its ``n``, or None when the check could not run."""
    if not items:
        return {}
    data = await _complete_json(
        build_support_prompt(query, items),
        user_keys=user_keys,
        model=model,
        purpose="claim support check",
    )
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    out: dict[int, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_n = row.get("n")
        if raw_n is None:
            continue
        try:
            n = int(raw_n)
        except (TypeError, ValueError):
            continue
        label = str(row.get("label") or "").strip().lower()
        if label not in SUPPORT_LABELS:
            continue
        out[n] = {"label": label, "note": str(row.get("note") or "").strip()[:300]}
    return out


async def ground_answer(
    *,
    query: str,
    answer: str,
    search_results: list[dict[str, Any]],
    user_id: str | None,
    user_keys=None,
    model: str | None = None,
    accessible_matter_ids: set[str] | None = None,
    check_support: bool = True,
) -> GroundingResult | None:
    """Ground the answer's claims and check that their sources support them.

    Returns None when grounding could not run (no files, no model, unparsable
    output) — the caller then keeps the chunk citations. ``model`` is the
    user's chosen chat model; both passes run on that model's provider when
    it is not the OpenAI-compatible client. ``accessible_matter_ids`` are the
    shared matters the caller belongs to: retrieval searched them, so
    grounding must be able to read the same files. ``check_support=False``
    skips the support pass (claims are still grounded and verified verbatim).
    """
    # Lazy import: documents.py imports the rag package at module load.
    from app.services.documents import document_service

    if not user_id:
        return None

    retrieval = _retrieval_scores(search_results)
    doc_ids: list[str] = []
    for r in search_results:
        did = (r.get("metadata") or {}).get("document_id")
        if did and did not in doc_ids:
            doc_ids.append(did)
    if not doc_ids:
        return None
    # Beyond the cap, check the files that matched the question best.
    chosen = sorted(doc_ids, key=lambda d: -retrieval.get(d, 0.0))[:GROUND_MAX_FILES]

    files: list[dict[str, Any]] = []
    for i, did in enumerate(chosen):
        loaded = await document_service.get_document_text(did, user_id, accessible_matter_ids)
        if not loaded or not loaded[1].strip():
            continue
        text = loaded[1]
        files.append(
            {
                "ref": f"f{i}",
                "id": did,
                "name": loaded[0],
                "text": text,
                "excerpt": file_excerpt(text, _retrieved_ranges(search_results, did)),
            }
        )
    if not files:
        return None

    data = await _complete_json(
        build_grounding_prompt(query, answer, files),
        user_keys=user_keys,
        model=model,
        purpose="claim grounding",
    )
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return None

    result = GroundingResult(
        files_checked=len(files),
        files_total=len(doc_ids),
        answer_truncated=len(answer) > ANSWER_LIMIT,
    )
    by_ref = {f["ref"]: f for f in files}
    grounded: list[dict[str, Any]] = []
    for c in claims[:MAX_CLAIMS]:
        if not isinstance(c, dict):
            continue
        claim = str(c.get("claim") or "").strip()
        if not claim:
            continue
        result.claims += 1
        quote = str(c.get("quote") or "").strip()
        answer_quote = str(c.get("answer_quote") or "").strip()[:300]
        f = by_ref.get(str(c.get("file_ref") or "").strip())
        if not quote or not f:
            # Nothing in the files was offered as support.
            result.flagged.append(FlaggedClaim(claim, answer_quote, UNSUPPORTED))
            continue
        span = find_quote_offset(f["text"], quote)
        if span:
            span = tidy_span(f["text"], span[0], span[1])
        exact = f["text"][span[0] : span[1]] if span else quote.strip(_EDGE_JUNK)
        # A quote that is mostly table markup explains nothing — drop it
        # rather than showing the user pipe-and-dash soup.
        if content_ratio(exact) < MIN_CONTENT_RATIO or len(exact) < MIN_QUOTE_CHARS:
            continue
        grounded.append(
            {
                "claim": claim,
                "answer_quote": answer_quote,
                "file": f,
                "span": span,
                "exact": exact,
            }
        )

    labels: dict[int, dict[str, str]] | None = None
    if check_support and grounded:
        items = []
        for n, g in enumerate(grounded):
            text, span = g["file"]["text"], g["span"]
            if span:
                lo, hi = (
                    max(0, span[0] - SUPPORT_CONTEXT),
                    min(len(text), span[1] + SUPPORT_CONTEXT),
                )
                context = text[lo:hi]
            else:
                context = g["exact"]
            items.append(
                {
                    "n": n,
                    "claim": g["claim"],
                    "answer_quote": g["answer_quote"],
                    "quote": g["exact"][:PASSAGE_LIMIT],
                    "source": g["file"]["name"],
                    "context": context,
                }
            )
        try:
            labels = await _check_support(query, items, user_keys=user_keys, model=model)
        except Exception as e:  # the check is best-effort; claims stay grounded
            logger.warning(f"Claim support check failed: {type(e).__name__}: {e}")
            labels = None
    result.support_checked = labels is not None

    for n, g in enumerate(grounded):
        f, span, exact, claim = g["file"], g["span"], g["exact"], g["claim"]
        verified = span is not None
        verdict = (labels or {}).get(n)
        support = verdict["label"] if verdict else None
        support_note = verdict["note"] if verdict else ""
        if support in (PARTIAL, CONTRADICTED, UNSUPPORTED):
            result.flagged.append(
                FlaggedClaim(claim, g["answer_quote"], support, support_note, f["name"])
            )
        location = _chunk_at(search_results, f["id"], span[0]) if span else {}
        line = line_of(f["text"], span[0]) if span else None
        where = f'line {line} of "{f["name"]}"' if line else f'"{f["name"]}"'
        page_start, page_end = location.get("page_start"), location.get("page_end")
        if page_start:
            pages = (
                f"page {page_start}"
                if page_end in (None, page_start)
                else f"pages {page_start}-{page_end}"
            )
            where = f"{where} ({pages})"
        reasoning = [
            ReasoningStep(
                type="Fact in the answer", description=claim, evidence=g["answer_quote"] or None
            ),
            ReasoningStep(
                type="Where it comes from",
                description=(
                    f"Found at {where}, verified character-for-character:"
                    if verified
                    else f'Could not be located verbatim in "{f["name"]}" — '
                    "treat this point as unverified."
                ),
                evidence=exact[:PASSAGE_LIMIT],
            ),
        ]
        if support:
            reasoning.append(
                ReasoningStep(
                    type="Does the source support it",
                    description=_LABEL_TITLE[support]
                    + (f": {support_note}" if support_note else ""),
                    evidence=None,
                )
            )
        result.citations.append(
            Citation(
                id=f"claim-{uuid.uuid4().hex[:8]}",
                source=f["name"],
                type="document",
                # The only real score a claim citation has is how well its
                # source file matched the question at retrieval time. Whether
                # the quote was found verbatim is carried by ``verified``, and
                # whether it supports the claim by ``support``.
                confidence=round(retrieval.get(f["id"], 0.0) * 100, 1),
                status=CitationStatus.PENDING,
                similarity=retrieval.get(f["id"], 0.0),
                relevance_rank=len(result.citations) + 1,
                chunk_index=int(location.get("chunk_index") or 0),
                token_count=0,
                passage=exact[:PASSAGE_LIMIT],
                reasoning=reasoning,
                application=claim,
                logic=CitationLogic(
                    query_intent=query,
                    matching_criteria=(
                        f"Verified verbatim at {where}"
                        if verified
                        else f'Unverified — quote not found verbatim in "{f["name"]}"'
                    ),
                    application=claim,
                ),
                document_id=f["id"],
                was_cited_by_ai=True,
                verified=verified,
                doc_span_start=span[0] if span else None,
                doc_span_end=span[1] if span else None,
                support=support,
                support_note=support_note or None,
                section=location.get("section") or None,
                page_start=page_start or None,
                page_end=page_end or None,
            )
        )
    if not result.citations and not result.flagged:
        return None
    return result


async def ground_answer_claims(**kwargs) -> list[Citation] | None:
    """Claim citations for the answer, or None to keep chunk citations.

    Kept for callers that only need the citations; see :func:`ground_answer`.
    """
    result = await ground_answer(**kwargs)
    return (result.citations or None) if result else None
