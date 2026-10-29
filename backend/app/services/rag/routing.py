"""Per-message routing for the zero-controls composer.

The Matter Strategy chat has no mode toggles: the user just types. This module
decides, per message, whether the query is a grounded question (normal RAG),
a pure legal-research lookup (outside authority only — the user's documents
must NOT be read), or a strategy-type request (route to the full-coverage
strategy brief), and whether case-law authority would strengthen the answer.

The research/strategy line matters: "find case law supporting OUR case" needs
the matter read first; "find case law on DWI stops" does not — routing the
latter through the strategy brief drags every document into an answer the
user never asked them to be part of.

One tiny utility-LLM call; on any failure a keyword heuristic answers instead —
routing must never break a chat request.
"""

from __future__ import annotations

import json
import logging
import re

from app.config import settings
from app.services.llm_clients import make_openai, openai_chat, utility_model
from app.services.provider_policy import enforce_openai_client
from app.services.rag.generation import chosen_provider_text, strip_json_fences

logger = logging.getLogger(__name__)

# Heuristic fallback: phrases that read as strategy asks rather than lookups.
# "Find case law supporting our case" is a strategy ask, not a lookup: it
# requires reading the whole matter, deriving the position, and only then
# searching authority — that pipeline lives in the strategy brief.
_STRATEGY_PATTERN = re.compile(
    r"\b("
    r"strateg(?:y|ies|ize|ise)|strongest|weakest|arguments?|our position|"
    r"assess(?:ment)? (?:of )?(?:our|the) (?:case|matter|position)|exposure|"
    r"game ?plan|next steps|plan of action|"
    r"how should (?:we|i)|what should (?:we|i) do|build (?:a|our) case|"
    r"support\w* (?:my|our|the|this) (?:case|position|claims?|arguments?)|"
    r"(?:case ?law|precedents?|authorit(?:y|ies))\b.{0,40}?\bsupport\w*"
    r")\b",
    re.IGNORECASE,
)

# Asks to find outside authority. Alone this is a research lookup; it only
# becomes a strategy ask when the message ties it to THIS matter.
_FIND_AUTHORITY_PATTERN = re.compile(
    r"\b(?:find|pull|get|give me|identify|research|search)\b.{0,60}?"
    r"\b(?:case ?law|precedents?|authorit(?:y|ies))\b",
    re.IGNORECASE,
)

# Ties a request to the user's own matter/documents (allowing a couple of
# words in between, e.g. "this faith group contract").
_MATTER_LINK_PATTERN = re.compile(
    r"\b(?:my|our|this)\s+(?:\w+\s+){0,2}"
    r"(?:case|matter|position|claims?|arguments?|client|contract|documents?|files?|filings?|dispute)\b"
    r"|\bsupport\w*\s+(?:us|me)\b",
    re.IGNORECASE,
)

# Heuristic fallback: asks that plainly call for legal authority.
_AUTHORITY_PATTERN = re.compile(
    r"\b(case ?law|precedent|authority|authorities|court|ruling|holding|statute)\b",
    re.IGNORECASE,
)

# Exhaustive authority asks about the scoped files themselves: "ALL the case
# law for this file" / "map the citations" / "authority for everything". These
# are strategy asks — the brief reads every scoped document and attaches
# quote-verified authority to each point it derives.
_EXHAUSTIVE_AUTHORITY_PATTERN = re.compile(
    r"\b(?:all|every|each)\b.{0,50}\b(?:case ?law|authorit(?:y|ies)|precedents?|citations?)\b"
    r"|\bmap\b.{0,30}\b(?:case ?law|authorit(?:y|ies)|citations?)\b"
    r"|\b(?:case ?law|authorit(?:y|ies))\b.{0,40}\b(?:everything|entire|whole)\b",
    re.IGNORECASE,
)

# Per-file map-reduce asks: the question is about the scoped files
# individually or comparatively — each file deserves its own context window
# ("summarize each of these", "compare these agreements", "which of these
# auto-renews"). Only consulted for intent "ask" with a small multi-file scope.
_PER_FILE_PATTERN = re.compile(
    r"\b(?:each|every)\b.{0,40}\b(?:files?|documents?|agreements?|contracts?|leases?|these|them)\b"
    r"|\bcompare\b"
    r"|\b(?:all|both)\s+(?:of\s+)?(?:these|the)\s+(?:files?|documents?|agreements?|contracts?)\b"
    r"|\bwhich\s+(?:of\s+these|one\s+of)\b"
    r"|\b(?:file|document)\s+by\s+(?:file|document)\b",
    re.IGNORECASE,
)


def heuristic_per_file(query: str) -> bool:
    """Keyword fallback for the per-file map-reduce flag."""
    return bool(_PER_FILE_PATTERN.search(query))


# ...but only when the ask is anchored to the user's own file(s)/folder —
# "all the case law on DWI stops" is research, not a document audit.
_DOC_TARGET_PATTERN = re.compile(
    r"\b(?:this|these|those|the|my|our)\s+(?:\w+\s+){0,3}"
    r"(?:files?|documents?|folders?|filings?|briefs?|motions?|contracts?|agreements?|leases?)\b"
    r"|\beverything in\b",
    re.IGNORECASE,
)

# Filler that must never reach a keyword search. "find me case law for a dwi"
# pasted verbatim into CourtListener matches on "case"/"law"/"find" and
# returns contract disputes; the search term is "dwi".
_SEARCH_FILLER_RE = re.compile(
    r"\b(?:find|get|pull|give|show|search|look\s?up|research|fetch|list|"
    r"me|us|please|can\s+you|could\s+you|i\s+(?:need|want)|"
    r"case\s?law|cases?|precedents?|authorit(?:y|ies)|opinions?|rulings?|"
    r"for|about|on|regarding|related\s+to|concerning|involving|"
    r"a|an|the|some|any|all|relevant|good)\b",
    re.IGNORECASE,
)


def distill_search_query(query: str) -> str | None:
    """Strip request filler down to searchable topic keywords.

    Returns None when nothing substantive remains — the caller then keeps the
    original text rather than searching for an empty string.
    """
    out = _SEARCH_FILLER_RE.sub(" ", query)
    out = re.sub(r"[^\w\s'.-]", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out or None


def heuristic_route(query: str) -> tuple[str, bool]:
    """Keyword-based fallback classification: (intent, use_case_law)."""
    if _EXHAUSTIVE_AUTHORITY_PATTERN.search(query) and _DOC_TARGET_PATTERN.search(query):
        return "strategy", True
    if _STRATEGY_PATTERN.search(query):
        return "strategy", bool(_AUTHORITY_PATTERN.search(query))
    if _FIND_AUTHORITY_PATTERN.search(query):
        return ("strategy" if _MATTER_LINK_PATTERN.search(query) else "research"), True
    return "ask", bool(_AUTHORITY_PATTERN.search(query))


def parse_route_payload(raw: str | None, query: str) -> tuple[str, bool]:
    """Normalize the classifier's JSON; fall back to heuristics on garbage."""
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return heuristic_route(query)
    if not isinstance(data, dict):
        return heuristic_route(query)
    intent = str(data.get("intent") or "").strip().lower()
    if intent == "authority_map":  # retired label: the brief does that job now
        intent = "strategy"
    if intent not in ("ask", "research", "strategy"):
        return heuristic_route(query)
    return intent, bool(data.get("use_case_law"))


def parse_search_query(raw: str | None, query: str) -> str | None:
    """Extract the classifier's distilled search query; heuristic on garbage."""
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return distill_search_query(query)
    candidate = data.get("search_query") if isinstance(data, dict) else None
    candidate = str(candidate or "").strip()
    if 2 <= len(candidate) <= 100:
        return candidate
    return distill_search_query(query)


def _api_key(user_keys) -> str | None:
    if user_keys and getattr(user_keys, "openai", None):
        return user_keys.openai
    return settings.openai_api_key


async def route_query(
    query: str, user_keys=None, model: str | None = None
) -> tuple[str, bool, str | None, bool]:
    """Return (intent, use_case_law, search_query, per_file) for a chat message.

    ``model`` is the user's chosen chat model: a Claude/Gemini choice routes the
    classifier call to that provider instead of the OpenAI-compatible client.

    intent: "ask" (grounded Q&A), "research" (outside-authority lookup, do not
    read the documents), or "strategy" (full-coverage matter brief, which also
    covers "all the case law for these files": the brief attaches quote-verified
    authority to every point it derives from the scoped documents).
    use_case_law: whether outside authority would materially help the answer.
    search_query: distilled keywords for a case-law keyword search — the raw
    message is NEVER a usable search term.
    per_file: the question is about the scoped files individually/comparatively,
    so each file should get its own analysis pass (map-reduce) when the scope
    is a small multi-file set.
    """
    prompt = (
        "Classify this message from a lawyer working across their own document "
        "collection.\n\n"
        '- intent "strategy": they want strategic synthesis across THEIR matter — '
        "position, arguments, strengths/weaknesses, recommendations, a plan. "
        "Requests to find/pull case law or precedent supporting THEIR case, "
        'position, or client are intent "strategy" (they require reading the '
        "whole matter and deriving the position first), with use_case_law true. "
        "So are asks for exhaustive supporting case law for their selected "
        'file(s) or folder as a whole (e.g. "give me all the case law for this '
        'file", "map the citations in this brief", "find authority for everything '
        'in this document"): use_case_law true.\n'
        '- intent "research": a pure legal-research lookup — case law, precedent, '
        "or statutes about a topic, offense, or doctrine, NOT tied to their own "
        'matter (e.g. "find me case law on DWI stops"). Their documents are '
        "irrelevant to the answer and must not be brought into it. "
        "use_case_law true.\n"
        '- intent "ask": a factual or analytical question answerable from the '
        "documents (summaries, terms, dates, obligations, comparisons).\n"
        "- use_case_law: true only if outside legal authority (case law, "
        "statutes, precedent) would materially strengthen the answer.\n"
        "- per_file: true when the question asks about the selected files "
        'individually or comparatively ("summarize each", "compare these '
        'agreements", "which of these auto-renews") so each file should be '
        "analyzed separately; false for pinpoint lookups answerable from "
        "passages.\n"
        "- search_query: when case law would help, 2-6 keywords naming the "
        'legal topic, offense, or doctrine for a keyword search (e.g. "DWI '
        'driving while intoxicated suppression") — never request filler like '
        '"find me case law"; null otherwise.\n\n'
        'Return STRICT JSON: {"intent":"ask"|"research"|"strategy",'
        '"use_case_law":true|false,"per_file":true|false,"search_query":"..."|null}\n\n'
        f"MESSAGE:\n{query[:2000]}"
    )
    try:
        content = await chosen_provider_text(
            prompt, user_keys=user_keys, model=model, max_tokens=300
        )
        if content is None:
            client = make_openai(_api_key(user_keys), async_=True)
            if client is None:
                return (
                    *heuristic_route(query),
                    distill_search_query(query),
                    heuristic_per_file(query),
                )
            enforce_openai_client(client, "query routing")
            resp = await openai_chat(
                client,
                model=utility_model(),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
        content = strip_json_fences(content)
        intent, use_case_law = parse_route_payload(content, query)
        try:
            per_file = bool(json.loads(content or "{}").get("per_file"))
        except (ValueError, TypeError):
            per_file = heuristic_per_file(query)
        return intent, use_case_law, parse_search_query(content, query), per_file
    except Exception as e:  # routing must never break chat
        logger.warning(f"Query routing failed, using heuristic: {e}")
        return (*heuristic_route(query), distill_search_query(query), heuristic_per_file(query))
