"""Deep case-law research: read full opinions, keep only verified authority.

Shared engine behind two features:

- The strategy brief (``strategy.py``) uses the windowing / quote-verification
  helpers here for its per-point authority stage.
- Research-intent chat ("find me case law on X") calls
  :func:`research_authorities` — the question is decomposed into legal
  propositions, CourtListener candidates are read window by window (in full,
  up to MAX_WINDOWS_PER_OPINION windows), and only opinions whose supporting
  quote verifies verbatim against the real opinion text are returned, each
  with a one-sentence explanation of why it matters.
- Drafting, contract chat and the strategy brief use
  :func:`redact_unverified_case_references` so model-written prose there is
  held to the same no-invented-authority rule as chat.

The hard rule everywhere: a citation the model endorses but cannot ground in
the opinion's own text is dropped, never shown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.services.courtlistener import courtlistener_service
from app.services.llm_clients import openai_chat, utility_model
from app.services.quote_verify import find_quote_offset

# app.services.rag imports this module (rag.service uses research_authorities),
# so nothing from that package is imported at module level here: the functions
# below import what they need when called. That keeps this module importable
# on its own, whichever of the two is loaded first.

logger = logging.getLogger(__name__)

WINDOW_CHARS = 80_000
WINDOW_OVERLAP = 2_000
MAX_CONCEPTS = 3
CANDIDATES_PER_CONCEPT = 4
CONCURRENCY = 6
# Judge windows read per opinion. Six 80k windows cover roughly 190 pages —
# every ordinary opinion in full — while bounding what one request can spend
# on a pathological one. An opinion longer than this is reported as partially
# read, never silently treated as read in full.
MAX_WINDOWS_PER_OPINION = max(1, int(getattr(settings, "research_max_windows_per_opinion", 6)))


def opinion_windows(text: str) -> list[str]:
    """Split an opinion into judge windows covering the ENTIRE text.

    Windows overlap by WINDOW_OVERLAP so a holding that straddles a boundary
    still appears whole in one window. Callers judge at most
    MAX_WINDOWS_PER_OPINION of them (see ``judge_opinion``).
    """
    if len(text) <= WINDOW_CHARS:
        return [text]
    windows: list[str] = []
    step = WINDOW_CHARS - WINDOW_OVERLAP
    for start in range(0, len(text), step):
        windows.append(text[start : start + WINDOW_CHARS])
        if start + WINDOW_CHARS >= len(text):
            break
    return windows


def verify_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Locate the judge's quote in the FULL opinion text.

    Models drift on punctuation across sentence boundaries; before giving up,
    try to verify the quote's longest sentence alone.
    """
    offsets = find_quote_offset(text, quote) if quote else None
    if offsets is None and quote:
        sentences = sorted(
            (part.strip() for part in re.split(r"(?<=[.;])\s+", quote)),
            key=len,
            reverse=True,
        )
        for sentence in sentences:
            if len(sentence) < 40:
                break
            offsets = find_quote_offset(text, sentence)
            if offsets:
                break
    return offsets


_VERSUS_RE = re.compile(r"\s+vs?\.?\s+")
# Where a reporter citation starts inside a "Name v. Name, 123 F.3d 456" span.
_CITE_START_RE = re.compile(r"[,;]?\s*\(?\b\d{1,4}\s+[A-Z]")
# The guard's span can open with the sentence's lead-in ("As in Doe v. Acme");
# those words, and the "v." itself, are not party names.
_NOT_PARTY_WORDS = frozenset(
    {
        "v", "vs", "in", "see", "under", "as", "per", "but", "and", "also", "cf", "unlike",
        "like", "following", "citing", "compare", "with", "accord", "contra", "although",
        "while", "since", "because", "the",
    }
)  # fmt: skip


def _caption_end(reference: str, source_words: set[str]) -> int:
    """How much of ``reference`` is a caption made only of the user's own words.

    Returns the length of the leading "A v. B" part when every word of both
    parties appears in the sources, else 0. Any reporter citation after it is
    not part of the caption.
    """
    if not _VERSUS_RE.search(reference):
        return 0
    cite = _CITE_START_RE.search(reference)
    name = reference[: cite.start()] if cite else reference
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _NOT_PARTY_WORDS]
    if words and all(w in source_words for w in words):
        return len(name)
    return 0


def redact_unverified_case_references(
    text: str,
    *,
    sources: Iterable[str | None] = (),
    case_law_results: list[dict[str, Any]] | None = None,
    known_parties: bool = False,
) -> tuple[str, list[str]]:
    """Apply the case-law guard to generated text outside research chat.

    The rule is the same everywhere a model writes prose: a case name or
    reporter citation must be traceable to something real — a verified
    CourtListener authority (``case_law_results``) or text the user supplied
    (``sources``: their documents, their instructions, the draft being
    revised). Anything else came from the model's own weights and is replaced
    with the redaction marker.

    ``known_parties`` is for drafted documents, which legitimately carry the
    caption of the user's OWN matter ("Doe v. Acme Corp." in a complaint the
    user asked for by naming Doe and Acme). With it set, an "A v. B" name is
    kept when every word of both parties appears in the sources; a reporter
    citation attached to it is still removed unless it is itself in a source.

    Returns (clean_text, removed_references). Unlike the chat guard, no note is
    appended: callers report the removals in their own payload.
    """
    if not text:
        return text, []
    from app.services.rag.case_law_guard import REDACTION, find_unverified_case_references

    source_texts = [s for s in sources if s]
    # The guard's party pattern spans whitespace, so across a line break it
    # would read a heading and the sentence under it as one caption ("ARGUMENT
    # / Per Smith v. Jones"). A same-length non-space stand-in for each newline
    # keeps every match on its own line and every offset valid for ``text``.
    probe = text.replace("\n", "|")
    spans = find_unverified_case_references(
        probe, case_law_results, [{"text": s} for s in source_texts]
    )
    if known_parties and spans:
        source_words = set(re.findall(r"[a-z0-9]+", " ".join(source_texts).lower()))
        kept: list[tuple[int, int]] = []
        for start, end in spans:
            caption = _caption_end(text[start:end], source_words)
            if caption < end - start:
                # Not a known caption, or a known caption with an unverified
                # citation after it: remove what is left.
                tail = start + caption
                while tail < end and text[tail] in ", ;(":
                    tail += 1
                kept.append((tail, end))
        spans = [(s, e) for s, e in kept if e > s]

    removed = [text[start:end] for start, end in spans]
    for start, end in reversed(spans):
        text = text[:start] + REDACTION + text[end:]
    return text, removed


@dataclass
class OpinionVerdict:
    """Outcome of judging one opinion against a proposition."""

    # "attached", "unreadable", "unsupportive", "quote_unverified" or "error".
    reason: str
    quote: str = ""  # the verified verbatim passage (reason == "attached")
    data: dict[str, Any] = field(default_factory=dict)  # the endorsing judge reply
    text: str = ""  # the full opinion text the quote was verified against
    # The opinion was longer than MAX_WINDOWS_PER_OPINION windows; its tail was
    # not judged.
    partial: bool = False


async def judge_opinion(
    client,
    semaphore: asyncio.Semaphore,
    opinion,
    *,
    build_prompt: Callable[[str, str], str],
    endorse_key: str,
    label: str,
) -> OpinionVerdict:
    """Read one opinion window by window until a verifiable quote endorses it.

    ``build_prompt(part, block)`` returns the judge prompt for one window:
    ``part`` is "part 2 of 5" and ``block`` the delimited window text. The
    judge must answer JSON carrying ``endorse_key`` (truthy to endorse) and a
    verbatim ``quote``. An opinion the judge endorses but whose quote cannot be
    located in the real text is NOT attached — that is the hallucination this
    stage exists to stop. Never raises.
    """
    from app.services.rag.prompt_safety import untrusted_block

    try:
        full = await courtlistener_service.get_opinion(opinion.id)
    except Exception as e:  # per-candidate, never fails the caller
        logger.warning(f"{label} opinion fetch failed ({opinion.id}): {e}")
        return OpinionVerdict("unreadable")
    text = (getattr(full, "text", None) or "") if full else ""
    if not text.strip():
        logger.warning(f"{label} candidate unreadable — opinion {opinion.id} has no usable text")
        return OpinionVerdict("unreadable")

    windows = opinion_windows(text)
    partial = len(windows) > MAX_WINDOWS_PER_OPINION
    if partial:
        logger.info(
            f"{label}: opinion {opinion.id} has {len(windows)} windows; judging the first "
            f"{MAX_WINDOWS_PER_OPINION}"
        )
        windows = windows[:MAX_WINDOWS_PER_OPINION]

    endorsed_but_unverified = False
    for index, window in enumerate(windows):
        part = f"part {index + 1} of {len(windows)}"
        prompt = build_prompt(part, untrusted_block(f"Opinion text, {part}", window))
        try:
            async with semaphore:
                resp = await openai_chat(
                    client,
                    model=utility_model(),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:  # per-candidate, never fails the caller
            logger.warning(f"{label} judge call failed ({opinion.id}): {e}")
            return OpinionVerdict("error", partial=partial)
        if not (isinstance(data, dict) and data.get(endorse_key)):
            continue

        offsets = verify_quote(text, str(data.get("quote") or "").strip())
        if offsets is None:
            # Endorsed but ungrounded in THIS window — keep scanning: a later
            # part of the opinion may yield a verifiable quote.
            endorsed_but_unverified = True
            continue
        return OpinionVerdict(
            "attached",
            quote=text[offsets[0] : offsets[1]],
            data=data,
            text=text,
            partial=partial,
        )

    return OpinionVerdict(
        "quote_unverified" if endorsed_but_unverified else "unsupportive", partial=partial
    )


def _relevance(value: Any) -> float | None:
    """The judge's own 0-1 relevance rating, or None when it gave none."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if 0.0 <= score <= 1.0 else None


async def _craft_research_targets(
    client, question: str, search_query: str | None
) -> list[dict[str, str]]:
    """Decompose a research question into 1-3 doctrine propositions + queries.

    Falls back to a single target built from the question / distilled query on
    any failure — research must never die on the decomposition step.
    """
    fallback = [{"proposition": question[:400], "query": (search_query or question)[:80]}]
    try:
        prompt = (
            "A lawyer is researching case law. Break their request into the "
            "distinct legal propositions worth searching for (1 for a simple "
            f"request, up to {MAX_CONCEPTS} for a compound one).\n\n"
            'For each: (a) "proposition" — ONE sentence stating the rule of '
            "law or doctrine the research should establish (no party names); "
            '(b) "query" — 3-7 keywords naming that doctrine for a case-law '
            "keyword search.\n\n"
            f"REQUEST: {question[:1500]}\n\n"
            'Return STRICT JSON: {"targets": [{"proposition": "...", "query": "..."}, ...]}'
        )
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        targets = data.get("targets")
        if isinstance(targets, list) and targets:
            cleaned = [
                {
                    "proposition": str(t.get("proposition") or "").strip(),
                    "query": str(t.get("query") or "").strip(),
                }
                for t in targets[:MAX_CONCEPTS]
                if isinstance(t, dict)
            ]
            cleaned = [t for t in cleaned if t["proposition"] and t["query"]]
            if cleaned:
                return cleaned
    except Exception as e:  # decomposition is an optimization, never fatal
        logger.warning(f"Case-law research target crafting failed, using question: {e}")
    return fallback


async def _read_and_judge(
    client,
    semaphore: asyncio.Semaphore,
    question: str,
    proposition: str,
    opinion,
    rank: int,
) -> tuple[dict[str, Any] | None, OpinionVerdict]:
    """Read one opinion and decide whether it answers the research.

    Returns (result, verdict): result is a search-result-shaped dict (the same
    shape ``search_case_law`` produces, so every downstream stage — context
    building, citation building, the unverified-reference guard — works
    unchanged) or None. Never raises.
    """
    from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE

    citation_str = opinion.citation[0] if opinion.citation else "No citation"

    def build_prompt(part: str, block: str) -> str:
        return (
            "You are selecting legal authority for a lawyer's research request.\n\n"
            f"RESEARCH REQUEST: {question[:600]}\n\n"
            f"PROPOSITION BEING RESEARCHED: {proposition[:400]}\n\n"
            f"CASE: {opinion.case_name}"
            f"{' (' + citation_str + ')' if opinion.citation else ''}\n"
            f"{UNTRUSTED_CONTENT_RULE}\n\n"
            f"OPINION TEXT ({part}):\n"
            f"{block}\n\n"
            "Does this part of the opinion state a rule of law, holding, or "
            "reasoning that genuinely bears on the proposition — something a "
            "lawyer doing this research would want to read? Off-topic "
            "keyword matches do not count.\n\n"
            'Return STRICT JSON: {"relevant": true|false, '
            '"quote": "ONE contiguous passage of 1-3 sentences copied EXACTLY, '
            "character for character, from the opinion text above, stating the "
            'relevant rule or holding — or empty", '
            '"why": "one sentence on why this case matters for the research '
            'request, or empty", '
            '"relevance": 0.0-1.0 (how directly the passage bears on the proposition)}'
        )

    verdict = await judge_opinion(
        client,
        semaphore,
        opinion,
        build_prompt=build_prompt,
        endorse_key="relevant",
        label="Case-law research",
    )
    if verdict.reason != "attached":
        return None, verdict

    why = str(verdict.data.get("why") or "").strip() or None
    if why:
        # The judge's explanation is model prose: it may only name this case.
        why, _ = redact_unverified_case_references(
            why,
            sources=[verdict.text],
            case_law_results=[{"metadata": {"case_name": opinion.case_name}}],
        )
    date_filed = getattr(opinion, "date_filed", None)
    result: dict[str, Any] = {
        "id": f"courtlistener_{opinion.id}",
        "text": verdict.quote[:1500],
        "metadata": {
            "filename": f"{opinion.case_name} ({citation_str})",
            "source": "courtlistener",
            "doc_type": "case_law",
            "court": getattr(opinion, "court", None),
            "date_filed": str(date_filed) if date_filed else None,
            "citation": citation_str,
            "url": getattr(opinion, "absolute_url", None),
            "opinion_id": opinion.id,
            "cluster_id": getattr(opinion, "cluster_id", None),
            "chunk_index": 0,
            "case_name": opinion.case_name,
            # The judge's reason this case matters — surfaced in the
            # Citations tab as the case summary.
            "case_summary": why,
            # The quote verified verbatim against the full opinion — this is
            # a confirmed authority, not a search-rank guess.
            "quote_verified": True,
        },
        "rank": rank,
    }
    # The score shown beside the citation is the judge's own rating of this
    # passage. When it gives none, no score is reported — never a constant.
    relevance = _relevance(verdict.data.get("relevance"))
    if relevance is not None:
        result["similarity"] = relevance
    return result, verdict


async def research_authorities(
    question: str,
    search_query: str | None,
    client,
    limit: int = 5,
    search_filters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Full deep-research pass: concepts -> search -> full reads -> verified quotes.

    Returns (results, status). Results are search-result-shaped dicts (see
    ``_read_and_judge``); status carries per-reason drop counters, plus
    ``partially_read`` for opinions too long to judge in full. Never raises —
    an empty list with counters is the worst case.

    ``search_filters`` are extra ``search_opinions`` keyword arguments (court
    ids, filing-date window, citation floor) — see
    ``app.services.rag.search.case_law_search_filters``.
    """
    opinion_filters = {"cited_gt": 2, **(search_filters or {})}
    status = {
        "concepts": 0,
        "opinions_read": 0,
        "partially_read": 0,
        "attached": 0,
        "unreadable": 0,
        "unsupportive": 0,
        "quote_unverified": 0,
        "errors": 0,
        "searches_failed": 0,
    }
    targets = await _craft_research_targets(client, question, search_query)
    status["concepts"] = len(targets)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    # Search all concepts, dedupe opinions across them (a case can hit
    # multiple doctrine queries; read it once, for its first concept).
    candidates: list[tuple[dict[str, str], Any]] = []
    seen_ids: set[int] = set()
    for target in targets:
        try:
            opinions = await courtlistener_service.search_opinions(
                query=target["query"], limit=CANDIDATES_PER_CONCEPT, **opinion_filters
            )
        except Exception as e:  # per-concept, other concepts continue
            logger.warning(f"Case-law research search failed ({target['query']!r}): {e}")
            status["searches_failed"] += 1
            status.setdefault("search_error", f"{type(e).__name__}: {e}"[:200])
            continue
        for opinion in (opinions or [])[:CANDIDATES_PER_CONCEPT]:
            if opinion.id in seen_ids:
                continue
            seen_ids.add(opinion.id)
            candidates.append((target, opinion))

    if not candidates:
        return [], status

    judged = await asyncio.gather(
        *(
            _read_and_judge(client, semaphore, question, target["proposition"], op, i + 1)
            for i, (target, op) in enumerate(candidates)
        )
    )
    results: list[dict[str, Any]] = []
    for result, verdict in judged:
        if verdict.reason != "unreadable":
            status["opinions_read"] += 1
        if verdict.partial:
            status["partially_read"] += 1
        if result is not None:
            results.append(result)
        elif verdict.reason == "error":
            status["errors"] += 1
        else:
            status[verdict.reason] += 1

    results = results[:limit]
    status["attached"] = len(results)
    return results, status
