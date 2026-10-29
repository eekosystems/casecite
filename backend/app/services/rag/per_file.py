"""Per-file map-reduce answering for folder-scoped chat.

When the user chats with a small set of files and the question is about the
files individually ("summarize each of these", "compare these agreements",
"which of these auto-renews?"), pooled retrieval is the wrong tool: the files
share one context window and a verbose file crowds out the rest.

This path gives EVERY scoped file its own context window: one map pass per
file (in parallel) answers the question for that file alone with exact
quotes, then a single reduce pass synthesizes the final answer with per-file
attribution. Same discipline as the strategy brief: hard file cap, quotes
come from the documents, and any failure falls back to the normal RAG answer
rather than breaking chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.config import settings
from app.services.documents import document_service
from app.services.llm_clients import chat_model, make_openai, openai_chat
from app.services.provider_policy import enforce_openai_client
from app.services.quote_verify import find_quote_offset
from app.services.rag.case_law_guard import redact_unverified_case_law
from app.services.rag.claim_grounding import MIN_CONTENT_RATIO, content_ratio, line_of, tidy_span
from app.services.rag.generation import chosen_provider_text, provider_of_model, strip_json_fences
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

# One map call per file — keep the fan-out and each file's share bounded.
PER_FILE_MAX_FILES = 8
PER_FILE_TEXT_LIMIT = 60_000  # chars per file (~15k tokens)
MAP_CONCURRENCY = 4
MAX_QUOTES_PER_FILE = 3


def _api_key(user_keys) -> str | None:
    if user_keys and getattr(user_keys, "openai", None):
        return user_keys.openai
    return None


def _chosen_provider_available(user_keys, model: str | None) -> bool:
    """True when the user's chosen model is Claude/Gemini and a client for it exists."""
    family = provider_of_model(model)
    if family == "anthropic":
        return bool((user_keys and user_keys.anthropic) or settings.anthropic_api_key)
    if family == "google":
        return bool(user_keys and user_keys.google)
    return False


def build_map_prompt(query: str, filename: str, text: str) -> str:
    truncated = len(text) > PER_FILE_TEXT_LIMIT
    body = text[:PER_FILE_TEXT_LIMIT]
    note = (
        "\n\n[NOTE: the document was truncated at the length limit — say so if "
        "the answer may depend on the omitted remainder.]"
        if truncated
        else ""
    )
    return (
        "You are reading ONE document from a lawyer's folder. Answer their "
        "question AS IT APPLIES TO THIS DOCUMENT ONLY — other files are handled "
        "separately. Be specific: name the terms, numbers, and clauses that "
        "answer the question, and copy the exact language you relied on. If the "
        "document does not bear on the question, say so in one sentence.\n\n"
        "Return STRICT JSON: "
        '{"answer":"...", "key_quotes":[{"quote":"...verbatim from the document...",'
        '"why":"one line on what this quote establishes"}]}'
        f" (at most {MAX_QUOTES_PER_FILE} quotes)\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"QUESTION:\n{query}\n\n"
        f'DOCUMENT "{filename}":\n{untrusted_block(filename, body)}{note}'
    )


def build_reduce_prompt(query: str, file_answers: list[dict[str, Any]]) -> str:
    sections = "\n\n".join(
        f"FILE: {fa['filename']}\nANSWER FOR THIS FILE:\n{fa['answer']}" for fa in file_answers
    )
    return (
        "A lawyer asked one question across several of their files. Each file was "
        "analyzed separately; the per-file findings are below. Write the final "
        "answer to the question, synthesizing across files. Attribute every claim "
        "to its file by name, call out differences between files explicitly, and "
        "do not invent anything not present in the findings. Answer in plain "
        "prose (short paragraphs or a compact list — no headings).\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"QUESTION:\n{query}\n\n"
        # The findings were extracted from third-party documents, so they can
        # carry whatever those documents said — delimited like the originals.
        f"PER-FILE FINDINGS:\n{untrusted_block('Per-file findings', sections)}"
    )


def _parse_map_payload(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {"answer": "", "key_quotes": []}
    if not isinstance(data, dict):
        return {"answer": "", "key_quotes": []}
    answer = str(data.get("answer") or "").strip()
    quotes = []
    for q in data.get("key_quotes") or []:
        if not isinstance(q, dict):
            continue
        quote = str(q.get("quote") or "").strip()
        if not quote:
            continue
        quotes.append({"quote": quote, "why": str(q.get("why") or "").strip()})
    return {"answer": answer, "key_quotes": quotes[:MAX_QUOTES_PER_FILE]}


def build_per_file_citations(query: str, file_answers: list[dict[str, Any]]) -> list[dict]:
    """One citation per key quote, pinned to its file, in the chat Citation shape."""
    citations = []
    rank = 0
    for fa in file_answers:
        for q in fa["key_quotes"]:
            rank += 1
            span = q.get("span")
            verified = span is not None
            citations.append(
                {
                    "id": f"pf-{fa['document_id']}-{rank}",
                    "source": fa["filename"],
                    "type": "document",
                    # Every file was read in full — nothing was ranked, so
                    # there is no retrieval score to report. ``verified`` (the
                    # quote was found verbatim in the file) is the signal.
                    "confidence": 0.0,
                    "similarity": 0.0,
                    "relevance_rank": rank,
                    "chunk_index": 0,
                    "token_count": 0,
                    "passage": q["quote"],
                    "case_summary": None,
                    "reasoning": [],
                    "application": q["why"] or None,
                    "logic": {
                        "query_intent": query,
                        "matching_criteria": (
                            f'Read in full — verified verbatim at line {q["line"]} of "{fa["filename"]}"'
                            if verified and q.get("line")
                            else f'Read in full — quote verified verbatim in "{fa["filename"]}"'
                            if verified
                            else "Read in full — this file got its own analysis pass"
                        ),
                        "application": q["why"] or "",
                    },
                    "document_id": fa["document_id"],
                    "url": None,
                    "was_cited_by_ai": True,
                    "verified": verified,
                    "doc_span_start": span[0] if span else None,
                    "doc_span_end": span[1] if span else None,
                }
            )
    return citations


async def per_file_answer(
    *,
    query: str,
    document_ids: list[str],
    user_id: str,
    user_keys=None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Map-reduce answer across the scoped files, or None to fall back to RAG.

    Returns the same {content, citations, stats} shape the RAG service
    produces so the chat router can use it interchangeably.

    ``model`` is the user's chosen chat model. A Claude/Gemini choice runs the
    map and reduce passes on that provider; otherwise they run on the
    OpenAI-compatible client with the instance chat model.
    """
    # Full file text goes to whichever provider runs these passes, so honour
    # the user's choice: only fall back to the OpenAI-compatible client when
    # their chosen model is served by it (or its own provider has no key).
    use_chosen = _chosen_provider_available(user_keys, model)
    client = None
    if not use_chosen:
        client = make_openai(_api_key(user_keys), async_=True)
        if client is None:
            return None
        enforce_openai_client(client, "per-file analysis")

    async def _complete(prompt: str, temperature: float, json_mode: bool) -> str:
        if use_chosen:
            text = await chosen_provider_text(prompt, user_keys=user_keys, model=model)
            return strip_json_fences(text) if json_mode else (text or "")
        kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = await openai_chat(
            client,
            model=chat_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    # Load every file's text first — a file we can't read is reported, not
    # silently skipped into a wrong "these 4 files" answer.
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    for doc_id in document_ids[:PER_FILE_MAX_FILES]:
        loaded = await document_service.get_document_text(doc_id, user_id)
        if not loaded or not loaded[1].strip():
            doc = await document_service.get_document(doc_id, user_id)
            missing.append(doc.filename if doc else doc_id)
            continue
        filename, text = loaded
        files.append({"document_id": doc_id, "filename": filename, "text": text})
    if not files:
        return None

    semaphore = asyncio.Semaphore(MAP_CONCURRENCY)

    async def _map_one(f: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            try:
                raw = await _complete(
                    build_map_prompt(query, f["filename"], f["text"]), 0.1, json_mode=True
                )
                parsed = _parse_map_payload(raw)
                if not parsed["answer"]:
                    return None
                return {**f, **parsed}
            except Exception as e:  # one bad file must not kill the run
                logger.warning(f"Per-file map pass failed for {f['filename']}: {e}")
                return None

    mapped = [m for m in await asyncio.gather(*(_map_one(f) for f in files)) if m]
    if not mapped:
        return None

    # Pin every quote to its exact char span in the source file (verbatim
    # verification) so the UI can anchor it inside the opened document. Trim
    # table/divider junk off span edges and drop quotes that are mostly
    # markup — pipe-and-dash soup from PDF form tables explains nothing.
    for m in mapped:
        kept = []
        for q in m["key_quotes"]:
            span = find_quote_offset(m["text"], q["quote"])
            if span:
                span = tidy_span(m["text"], span[0], span[1])
            q["span"] = span
            q["line"] = line_of(m["text"], span[0]) if span else None
            if span:
                q["quote"] = m["text"][span[0] : span[1]]
            if content_ratio(q["quote"]) >= MIN_CONTENT_RATIO:
                kept.append(q)
        m["key_quotes"] = kept

    content = (await _complete(build_reduce_prompt(query, mapped), 0.2, json_mode=False)).strip()
    if not content:
        return None

    # Case law is never trusted from the model, here as in the pooled answer:
    # a case the files themselves cite may stay; anything else is redacted.
    content, removed_refs = redact_unverified_case_law(
        content, None, [{"text": f["text"]} for f in files]
    )
    if removed_refs:
        logger.warning(
            f"[CaseLawGuard] Removed {len(removed_refs)} unverified case "
            f"reference(s) from per-file answer: {removed_refs}"
        )

    failed = [f["filename"] for f in files if f["filename"] not in {m["filename"] for m in mapped}]
    notes = []
    if missing:
        notes.append(f"Could not read: {', '.join(missing)}.")
    if failed:
        notes.append(f"Analysis failed for: {', '.join(failed)}.")
    if notes:
        content += "\n\n⚠ " + " ".join(notes)

    return {
        "content": content,
        "citations": build_per_file_citations(query, mapped),
        "stats": {
            "docs_searched": len(mapped),
            "chunks_retrieved": sum(len(m["key_quotes"]) for m in mapped),
            "processing_time": "",
            "case_law_searched": 0,
            "case_law_included": 0,
            "query_intent": "per_file",
        },
    }
