"""Claim-level grounding for chat answers over the user's documents.

Chunk citations explain retrieval, not the answer — "this passage matched your
query" tells a lawyer nothing about where the $3,000 figure came from. This
pass makes the ANSWER's own claims the citation unit: extract each factual
claim (parties, amounts, dates, terms), find the exact verbatim contract
language that establishes it, verify it character-for-character against the
source file, and pin it to the file + char span so the UI can anchor it inside
the opened document.

Whenever grounding succeeds, chunk-provenance citations are replaced with
these claim citations. It is strictly best-effort: any failure keeps the
chunk citations and never breaks the chat response.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.models.schemas import Citation, CitationLogic, CitationStatus, ReasoningStep
from app.services.llm_clients import make_openai, openai_chat, utility_model
from app.services.provider_policy import enforce_openai_client
from app.services.quote_verify import find_quote_offset
from app.services.rag.generation import chosen_provider_text, strip_json_fences
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

# Grounding reads the full source files — keep the fan-in bounded.
GROUND_MAX_FILES = 4
GROUND_TEXT_LIMIT = 40_000  # chars per file in the prompt
ANSWER_LIMIT = 4000
MAX_CLAIMS = 10
PASSAGE_LIMIT = 600


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


def build_grounding_prompt(query: str, answer: str, files: list[dict[str, Any]]) -> str:
    # File text is third-party content: delimited so nothing inside it can
    # be read as an instruction. The block passes the text through verbatim,
    # so quotes still verify character-for-character against the source.
    file_blocks = "\n\n".join(
        f'[{f["ref"]}] FILE "{f["name"]}":\n'
        + untrusted_block(f"{f['ref']}: {f['name']}", f["text"][:GROUND_TEXT_LIMIT])
        for f in files
    )
    return (
        "You are grounding an AI answer in the user's source documents. List the "
        "answer's distinct factual claims — parties, amounts, dates, deadlines, "
        "terms, obligations: every concrete point a lawyer would want verified. "
        "For EACH claim, give the ref of the file it comes from and the single "
        "EXACT VERBATIM passage from that file that establishes it — copy the "
        "characters exactly as they appear, never paraphrase or stitch fragments. "
        "Quote the SHORTEST contiguous readable span that establishes the claim — "
        "a sentence or clause, never a whole table row. If a fact appears only in "
        "a table or form, quote just the label and value, not runs of empty cells "
        'or divider markup like "---" or "| | |". Never pick a passage that is '
        "mostly pipes, dashes, or blanks; if two facts sit far apart, make them "
        "two separate claims. Skip claims that have no supporting language in the "
        "files.\n\n"
        "Also copy, for each claim, the sentence of the ANSWER that makes it "
        '("answer_quote", verbatim from the answer) — this is how the user '
        "traces answer → source.\n\n"
        "Return STRICT JSON: "
        '{"claims":[{"claim":"...","answer_quote":"...verbatim from the answer...",'
        '"file_ref":"f0","quote":"...verbatim from the file..."}]} '
        f"(at most {MAX_CLAIMS} claims)\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"QUESTION:\n{query}\n\n"
        f"ANSWER:\n{answer[:ANSWER_LIMIT]}\n\n"
        f"FILES:\n{file_blocks}"
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


async def ground_answer_claims(
    *,
    query: str,
    answer: str,
    search_results: list[dict[str, Any]],
    user_id: str | None,
    user_keys=None,
    model: str | None = None,
) -> list[Citation] | None:
    """Return claim citations for the answer, or None to keep chunk citations.

    ``model`` is the user's chosen chat model; the grounding call runs on that
    model's provider when it is not the OpenAI-compatible client.
    """
    # Lazy import: documents.py imports the rag package at module load.
    from app.services.documents import document_service

    if not user_id:
        return None

    doc_ids: list[str] = []
    for r in search_results:
        did = (r.get("metadata") or {}).get("document_id")
        if did and did not in doc_ids:
            doc_ids.append(did)
    if not doc_ids or len(doc_ids) > GROUND_MAX_FILES:
        return None

    files: list[dict[str, Any]] = []
    for i, did in enumerate(doc_ids):
        loaded = await document_service.get_document_text(did, user_id)
        if not loaded or not loaded[1].strip():
            continue
        files.append({"ref": f"f{i}", "id": did, "name": loaded[0], "text": loaded[1]})
    if not files:
        return None

    prompt = build_grounding_prompt(query, answer, files)
    raw = await chosen_provider_text(prompt, user_keys=user_keys, model=model)
    if raw is None:
        client = make_openai(_api_key(user_keys), async_=True)
        if client is None:
            return None
        enforce_openai_client(client, "claim grounding")
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
    try:
        data = json.loads(strip_json_fences(raw) or "{}")
    except (ValueError, TypeError):
        return None
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return None

    by_ref = {f["ref"]: f for f in files}
    # The only real score a claim citation has is how well its source file
    # matched the question at retrieval time. Whether the quote was found
    # verbatim is carried by ``verified`` — it is a fact, not a percentage.
    retrieval = _retrieval_scores(search_results)
    citations: list[Citation] = []
    rank = 0
    for c in claims[:MAX_CLAIMS]:
        if not isinstance(c, dict):
            continue
        claim = str(c.get("claim") or "").strip()
        quote = str(c.get("quote") or "").strip()
        answer_quote = str(c.get("answer_quote") or "").strip()[:300]
        f = by_ref.get(str(c.get("file_ref") or "").strip())
        if not claim or not quote or not f:
            continue
        span = find_quote_offset(f["text"], quote)
        if span:
            span = tidy_span(f["text"], span[0], span[1])
        verified = span is not None
        exact = f["text"][span[0] : span[1]] if span else quote.strip(_EDGE_JUNK)
        # A quote that is mostly table markup explains nothing — drop it
        # rather than showing the user pipe-and-dash soup.
        if content_ratio(exact) < MIN_CONTENT_RATIO or len(exact) < MIN_QUOTE_CHARS:
            continue
        rank += 1
        line = line_of(f["text"], span[0]) if span else None
        where = f'line {line} of "{f["name"]}"' if line else f'"{f["name"]}"'
        citations.append(
            Citation(
                id=f"claim-{uuid.uuid4().hex[:8]}",
                source=f["name"],
                type="document",
                confidence=round(retrieval.get(f["id"], 0.0) * 100, 1),
                status=CitationStatus.PENDING,
                similarity=retrieval.get(f["id"], 0.0),
                relevance_rank=rank,
                chunk_index=0,
                token_count=0,
                passage=exact[:PASSAGE_LIMIT],
                reasoning=[
                    ReasoningStep(
                        type="Fact in the answer",
                        description=claim,
                        evidence=answer_quote or None,
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
                ],
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
            )
        )
    return citations or None
