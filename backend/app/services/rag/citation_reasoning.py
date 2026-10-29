"""Post-answer citation reasoning for the chat RAG pipeline.

After the answer is generated, ONE utility-LLM call (per chat request, not per
citation) explains WHY each cited source — internal document or case law —
supports the answer. The result is joined back onto the response citations as
an auditable `reasoning` chain plus a one-sentence `application`.

The prompt-building / parsing / joining helpers are pure functions so they can
be unit-tested without the app or a live LLM. The enrichment pass is strictly
best-effort: any failure leaves citations with empty reasoning and never breaks
the chat response.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from app.services.llm_clients import make_openai, openai_chat, utility_model
from app.services.provider_policy import enforce_openai_client
from app.services.rag.generation import chosen_provider_text, strip_json_fences
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

if TYPE_CHECKING:
    from app.models.schemas import Citation
    from app.services.user_keys import UserAPIKeys

logger = logging.getLogger(__name__)

# Bounds keep the single utility call's cost and latency predictable.
# The snippet must be long enough for the model to quote the SPECIFIC clause
# language the answer relied on — 300 chars starved it into generic
# "this document is relevant" filler.
ANSWER_TEXT_LIMIT = 4000
SNIPPET_LIMIT = 1200
MAX_TYPE_LEN = 40
MAX_REASONING_STEPS = 5


def build_citation_refs(citations: list[Citation]) -> list[dict[str, str]]:
    """Assign server-side refs ("c0", "c1", ...) and compact each citation.

    The ref order matches the citations list, so joining back is positional and
    never trusts LLM-invented identifiers.
    """
    refs = []
    for i, citation in enumerate(citations):
        snippet = " ".join((citation.passage or "").split())[:SNIPPET_LIMIT]
        refs.append(
            {
                "ref": f"c{i}",
                "source": citation.source,
                "type": citation.type,
                "snippet": snippet,
            }
        )
    return refs


def build_reasoning_prompt(query: str, answer: str, refs: list[dict[str, str]]) -> str:
    """Build the single utility-LLM prompt covering ALL citations at once."""
    # Snippets are passages from uploaded documents and court opinions —
    # third-party text, so each is delimited as data, never instructions.
    source_lines = "\n".join(
        f"[{r['ref']}] ({r['type']}) {r['source']}:\n"
        + untrusted_block(f"{r['ref']}: {r['source']}", r["snippet"])
        for r in refs
    )
    return (
        "You are writing the audit trail for an AI legal research answer. A lawyer will "
        "click a cited source and read this to understand HOW the answer was reached from "
        "that source. For EACH cited source below, reconstruct the reasoning FROM the "
        "source's specific content TO the answer's specific conclusions.\n\n"
        "For each source, produce 2-5 steps that trace the derivation:\n"
        '- "Fact used": a specific term, number, clause, or holding the answer relies on. '
        "Put the exact language from the snippet in evidence — quote only, never invent "
        "or paraphrase quoted text.\n"
        '- "Inference": how that fact produces a specific conclusion stated in the '
        'answer, naming the conclusion (e.g. "a $3,000 flat fee for the defined scope is '
        'why the answer calls the price commercially reasonable").\n'
        '- "Limitation" (when true): what this source could NOT establish and how that '
        "shaped the answer's hedges (e.g. \"the MSA with warranties and liability terms "
        "is not included, which is why the answer says fairness cannot be fully "
        'assessed").\n\n'
        "Every step must map to an actual claim in the answer — a reader should be able "
        "to match each step to a sentence of the answer. NEVER write generic relevance "
        'statements ("this document is the agreement being assessed", "this source sets '
        'out the deal structure") — those explain nothing. Finish each source with '
        '"application": one sentence naming exactly which conclusion(s) in the answer '
        "this source produced. Use each source's ref exactly as given. If a source does "
        "not actually support the answer, say so in its reasoning rather than inventing "
        "support.\n\n"
        "Return STRICT JSON: "
        '{"citations":[{"ref":"c0","reasoning":[{"type":"...","description":"...",'
        '"evidence":"..."}],"application":"..."}]}\n\n'
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"QUESTION:\n{query}\n\n"
        f"ANSWER:\n{answer[:ANSWER_TEXT_LIMIT]}\n\n"
        f"CITED SOURCES:\n{source_lines}"
    )


def normalize_reasoning_steps(raw: Any) -> list[dict[str, str | None]]:
    """Normalize one citation's LLM reasoning steps.

    Same discipline as the strategy brief's reasoning cleanup: type capped at
    40 chars, steps without a description dropped, evidence optional.
    """
    steps: list[dict[str, str | None]] = []
    if not isinstance(raw, list):
        return steps
    for step in raw:
        if not isinstance(step, dict):
            continue
        description = str(step.get("description") or "").strip()
        if not description:
            continue
        step_type = str(step.get("type") or "").strip()[:MAX_TYPE_LEN] or "Analysis"
        evidence = step.get("evidence")
        steps.append(
            {
                "type": step_type,
                "description": description,
                "evidence": (str(evidence).strip() or None) if evidence else None,
            }
        )
    return steps[:MAX_REASONING_STEPS]


def join_reasoning_by_ref(payload: Any, ref_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Join the LLM payload back to the server-assigned refs.

    Every server ref gets an entry. Refs the LLM omitted (or garbled) get
    ``{"reasoning": [], "application": None}``; refs the LLM invented are
    ignored. Tolerates arbitrarily malformed payloads.
    """
    joined: dict[str, dict[str, Any]] = {
        ref: {"reasoning": [], "application": None} for ref in ref_ids
    }
    items = payload.get("citations") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return joined
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if ref not in joined:
            continue
        application = item.get("application")
        joined[ref] = {
            "reasoning": normalize_reasoning_steps(item.get("reasoning")),
            "application": (str(application).strip() or None) if application else None,
        }
    return joined


def apply_citation_reasoning(citations: list[Citation], joined: dict[str, dict[str, Any]]) -> None:
    """Attach the joined reasoning/application onto the response citations in place."""
    from app.models.schemas import ReasoningStep

    for i, citation in enumerate(citations):
        entry = joined.get(f"c{i}") or {"reasoning": [], "application": None}
        citation.reasoning = [ReasoningStep(**step) for step in entry["reasoning"]]
        citation.application = entry["application"]


async def enrich_citations_with_reasoning(
    citations: list[Citation],
    query: str,
    answer: str,
    user_keys: UserAPIKeys | None = None,
    model: str | None = None,
) -> None:
    """One utility-LLM pass attaching auditable reasoning to every citation.

    ``model`` is the user's chosen chat model; the pass runs on that model's
    provider when it is not the OpenAI-compatible client.

    Skipped entirely when there are no citations. Never raises: on any failure
    (no client, bad JSON, network error) every citation gets empty reasoning
    and the chat response is returned unchanged otherwise.
    """
    if not citations:
        return
    refs = build_citation_refs(citations)
    ref_ids = [r["ref"] for r in refs]
    try:
        prompt = build_reasoning_prompt(query, answer, refs)
        raw = await chosen_provider_text(prompt, user_keys=user_keys, model=model)
        if raw is None:
            api_key = user_keys.openai if user_keys and getattr(user_keys, "openai", None) else None
            client = make_openai(api_key, async_=True)
            if client is None:
                raise ValueError("no OpenAI-compatible client available for citation reasoning")
            enforce_openai_client(client, "citation reasoning")
            resp = await openai_chat(
                client,
                model=utility_model(),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content
        payload = json.loads(strip_json_fences(raw) or "{}")
        joined = join_reasoning_by_ref(payload, ref_ids)
    except Exception as e:  # this pass must never break the chat response
        logger.warning(f"Citation reasoning pass failed: {e}")
        joined = {ref: {"reasoning": [], "application": None} for ref in ref_ids}
    apply_citation_reasoning(citations, joined)
