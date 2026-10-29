"""Conversational contract review — natural language drives the machinery.

One endpoint, no form controls: the user's message is routed by intent —

- "review this" / "analyze" / "redline-worthy problems?"  -> the full
  analysis pipeline (``contract_analysis_service.analyze``), optionally
  steered by whatever review instructions the message itself contains.
- anything else ("is there a liability cap?", "check the termination
  rights", "summarize the indemnity") -> a grounded answer over the
  contract text, where every citation is a verbatim quote located in the
  document via ``find_quote_offset`` — unlocatable quotes are dropped, so
  the UI can highlight exactly what the answer relies on.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.case_law_research import redact_unverified_case_references
from app.services.llm_clients import openai_chat, utility_model
from app.services.quote_verify import find_quote_offset
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

CONTEXT_CHARS = 120_000
MAX_CITATIONS = 8


async def route_contract_message(client, message: str) -> dict[str, str]:
    """Classify a contract-chat message. Never raises; defaults to 'ask'."""
    fallback = {"intent": "ask", "instructions": ""}
    if client is None:
        return fallback
    try:
        prompt = (
            "Classify this message from a lawyer working on a specific contract.\n\n"
            '- intent "analyze": they want a full review/analysis of the whole '
            "contract (review this, analyze it, what are the risks overall, "
            "run your checks).\n"
            '- intent "redline": they want proposed edits/markup — redline it, '
            "mark it up, propose changes, fix the problems, revise the "
            "problematic clauses, prepare a turn of the document.\n"
            '- intent "draft": they want a NEW document written (draft a '
            "response letter, draft an amendment, write a termination notice).\n"
            '- intent "ask": a question or targeted request about the contract '
            "(is there a cap? check the termination clause; summarize the "
            "indemnity; who are the parties?).\n"
            "- instructions: if the message states HOW to review/draft (who "
            "they represent, what to flag, what to ignore, what the document "
            'should say), restate that guidance concisely; else "".\n\n'
            'Return STRICT JSON: {"intent":"analyze"|"redline"|"draft"|"ask",'
            '"instructions":"..."}\n\n'
            f"MESSAGE:\n{message[:2000]}"
        )
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        intent = data.get("intent")
        if intent not in ("analyze", "redline", "draft", "ask"):
            return fallback
        return {"intent": intent, "instructions": str(data.get("instructions") or "").strip()}
    except Exception as e:  # routing must never break contract chat
        logger.warning("Contract-chat routing failed, defaulting to ask: %s", e)
        return fallback


async def answer_contract_question(client, text: str, message: str) -> dict[str, Any]:
    """Answer a question about the contract with verified, highlightable quotes.

    Returns {"answer": str, "citations": [{quote, span_start, span_end}],
    "case_law_removed": [str]}. Citations the model offers but cannot ground
    verbatim are dropped, and any case the answer names that appears in
    neither the contract nor the lawyer's message is removed.
    """
    if client is None:
        raise ValueError(
            "An OpenAI API key or a custom LLM endpoint is required. Configure one in Settings."
        )
    prompt = (
        "You are reviewing a contract for a lawyer. Answer their request using "
        "ONLY the contract text below.\n\n"
        f"REQUEST: {message[:2000]}\n\n"
        "Rules:\n"
        "- Ground every claim in the contract. For each supporting passage, "
        "add a citation: ONE contiguous quote of 1-3 sentences copied "
        "EXACTLY, character for character, from the contract text.\n"
        "- If the contract does not address the request, say so plainly — "
        "never guess.\n"
        "- Reference citations inline as [1], [2] in the answer.\n\n"
        'Return STRICT JSON: {"answer":"...", "citations":["exact quote 1", '
        '"exact quote 2", ...]}\n\n'
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        "CONTRACT TEXT:\n" + untrusted_block("Contract text", text[:CONTEXT_CHARS])
    )
    resp = await openai_chat(
        client,
        model=utility_model(),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    answer = str(data.get("answer") or "").strip()
    raw_citations = data.get("citations")
    citations: list[dict[str, Any]] = []
    dropped = 0
    if isinstance(raw_citations, list):
        for quote in raw_citations[:MAX_CITATIONS]:
            quote = str(quote or "").strip()
            if not quote:
                continue
            offsets = find_quote_offset(text, quote)
            if offsets is None:
                dropped += 1
                continue
            citations.append(
                {
                    "quote": text[offsets[0] : offsets[1]][:600],
                    "span_start": offsets[0],
                    "span_end": offsets[1],
                }
            )
    if dropped:
        logger.info("Contract chat dropped %d unverifiable citations", dropped)
    answer, removed = redact_unverified_case_references(answer, sources=[text, message])
    if removed:
        logger.info("Contract chat removed %d unverified case reference(s)", len(removed))
    return {
        "answer": answer or "The contract does not address this.",
        "citations": citations,
        "case_law_removed": removed,
    }
