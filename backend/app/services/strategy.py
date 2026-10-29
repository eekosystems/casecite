"""
Matter-level strategy brief synthesis.

Unlike chat's top-k retrieval (where a low-ranking document silently drops out
of the context), this service guarantees per-document coverage: every document
in the requested scope contributes at least one passage to the synthesis. The
working set is resolved via the document filter, each document's most relevant
chunks are gathered (global semantic search grouped per document, with a
first-chunk fallback for any document the search missed), and a single
utility-LLM call synthesizes a position with strengths, weaknesses and next
steps — each point citing server-assigned passage refs that are joined back to
real citations in code, so the model can never invent a source.

The brief is built from the most relevant passages of each document, not from
a full read of every document; the payload's ``scope`` says exactly how much
was read so the UI never has to imply otherwise.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

from app.config import settings
from app.services import case_law_research
from app.services.courtlistener import courtlistener_service
from app.services.documents import document_service
from app.services.embeddings import embedding_service
from app.services.llm_clients import make_openai, openai_chat, utility_model
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block
from app.services.rag.search import get_document_chunks_directly, tenant_scope
from app.services.vectordb import get_vector_db

logger = logging.getLogger(__name__)

# Bounds keep cost and latency predictable.
MAX_DOCUMENTS = 25
CHUNKS_PER_DOCUMENT = 3
# Shown with a citation. The synthesis itself reads the whole passage.
SNIPPET_MAX_CHARS = 400
# A stored chunk is ~512 tokens; this keeps all of it. The synthesis prompt
# gives each passage an equal share of SYNTHESIS_BUDGET_CHARS, up to this.
PASSAGE_MAX_CHARS = 4_000
SYNTHESIS_BUDGET_CHARS = 150_000
# Case-law support stage: every strength/weakness point is restated as a legal
# proposition, searched on CourtListener, and each candidate opinion is read
# window by window (up to case_law_research.MAX_WINDOWS_PER_OPINION windows —
# every ordinary opinion in full) until a supporting quote verifies verbatim
# against the real opinion text or the windows run out. An opinion too long to
# finish is counted as partially read. Case law is never trusted from the model.
MAX_CASE_LAW_POINTS = 8
CASE_LAW_CANDIDATES_PER_POINT = 4
CASE_LAW_KEPT_PER_POINT = 3
# Judge window size. Windows overlap so a holding spanning a boundary is still
# seen whole in one window; quotes are always verified against the FULL text.
CASE_LAW_WINDOW_CHARS = case_law_research.WINDOW_CHARS
CASE_LAW_CONCURRENCY = 6
# Wall-clock budget for the whole stage. Must stay under the reverse proxy's
# read timeout (nginx.unified.conf proxy_read_timeout).
CASE_LAW_TIME_BUDGET_SECONDS = 480.0


class StrategyServiceUnavailable(RuntimeError):
    """Vector search infrastructure is not available."""


class StrategySynthesisError(RuntimeError):
    """The synthesis LLM call failed. The brief IS the synthesis — there is no
    mechanical fallback that makes sense, so this surfaces as a 502."""


def scale_confidence(score: float) -> int:
    """Scale a 0-1 retrieval similarity to a 0-100 confidence integer."""
    return int(round(max(0.0, min(1.0, score)) * 100))


def group_chunks_by_document(
    results: list[dict[str, Any]],
    allowed_doc_ids: set[str],
    per_doc: int = CHUNKS_PER_DOCUMENT,
) -> dict[str, list[dict[str, Any]]]:
    """Group search results by document_id, keeping the top ``per_doc`` chunks each.

    ``results`` are assumed sorted by relevance (as returned by vector search).
    Results for documents outside ``allowed_doc_ids`` are discarded — the global
    search is user-scoped, not scope-filtered, so out-of-scope hits are expected.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        doc_id = (result.get("metadata") or {}).get("document_id") or ""
        if doc_id not in allowed_doc_ids:
            continue
        bucket = grouped.setdefault(doc_id, [])
        if len(bucket) < per_doc:
            bucket.append(result)
    return grouped


def build_passages(
    doc_order: list[tuple[str, str]],
    grouped: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Assign server-side refs ("d0", "d1", ...) to each (document, chunk) passage.

    ``doc_order`` is the ordered working set as (document_id, filename) pairs, so
    refs are stable for a given scope regardless of retrieval ranking.
    """
    passages: list[dict[str, Any]] = []
    for doc_id, filename in doc_order:
        for chunk in grouped.get(doc_id, []):
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            passages.append(
                {
                    "ref": f"d{len(passages)}",
                    "document_id": doc_id,
                    "filename": filename,
                    "snippet": text[:SNIPPET_MAX_CHARS],
                    "text": text[:PASSAGE_MAX_CHARS],
                    "score": float(chunk.get("similarity") or 0.0),
                }
            )
    return passages


def join_citations(
    llm_data: dict[str, Any], passages: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Join the LLM's refs back to the server-side passages.

    Unknown refs are dropped (the model cannot invent a citation). Returns
    (sections, citations): sections carry position/strengths/weaknesses/
    next_steps with each point's "citation_ids"; citations contains only the
    passages that were actually cited.
    """
    by_ref = {p["ref"]: p for p in passages}
    cited: dict[str, dict[str, Any]] = {}

    def resolve(refs: Any) -> list[str]:
        ids: list[str] = []
        for ref in refs if isinstance(refs, list) else []:
            ref = str(ref)
            passage = by_ref.get(ref)
            if passage is None:
                continue  # unknown ref — dropped
            if ref not in cited:
                cited[ref] = {
                    "id": ref,
                    "source": passage["filename"],
                    "type": "document",
                    "document_id": passage["document_id"],
                    "passage": passage["snippet"],
                    "confidence": scale_confidence(passage["score"]),
                }
            if ref not in ids:
                ids.append(ref)
        return ids

    def clean_points(items: Any, text_key: str) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            text = str(item.get(text_key) or "").strip()
            if not text:
                continue
            points.append({text_key: text, "citation_ids": resolve(item.get("refs"))})
        return points

    sections = {
        "position": str(llm_data.get("position") or "").strip(),
        "strengths": clean_points(llm_data.get("strengths"), "point"),
        "weaknesses": clean_points(llm_data.get("weaknesses"), "point"),
        "next_steps": clean_points(llm_data.get("next_steps"), "step"),
    }
    citations = [cited[p["ref"]] for p in passages if p["ref"] in cited]
    return sections, citations


class StrategyService:
    def _api_key(self, user_keys) -> str | None:
        if user_keys and getattr(user_keys, "openai", None):
            return user_keys.openai
        return settings.openai_api_key

    async def _retrieve_coverage(
        self,
        question: str,
        doc_order: list[tuple[str, str]],
        user_id: str,
        user_keys,
        matter_ids: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Retrieve each document's most relevant chunks for the question.

        One search over the documents in scope, within the caller's tenant
        scope (their own chunks and those of matters they belong to), grouped
        by document — then any document the search did not cover contributes
        its first stored chunk, so every document in scope is represented.
        """
        vector_db = get_vector_db()
        if vector_db is None:
            raise StrategyServiceUnavailable(
                "Vector search is temporarily unavailable. Please try again later."
            )

        allowed = {doc_id for doc_id, _ in doc_order}
        grouped: dict[str, list[dict[str, Any]]] = {}
        try:
            query_embedding = await embedding_service.embed_query(question, user_keys=user_keys)
            top_k = min(200, max(50, CHUNKS_PER_DOCUMENT * len(doc_order) * 2))
            scope = tenant_scope(user_id, matter_ids)
            results = await vector_db.search(
                query_embedding=query_embedding,
                top_k=top_k,
                filter={"$and": [scope, {"document_id": {"$in": sorted(allowed)}}]},
            )
            grouped = group_chunks_by_document(results, allowed)
        except (ValueError, KeyError, ConnectionError, TimeoutError, OSError, RuntimeError) as e:
            logger.warning(
                f"Strategy brief semantic retrieval failed, using first-chunk fallback: {e}"
            )

        # Coverage guarantee: every uncovered document contributes its first chunk.
        for doc_id, _filename in doc_order:
            if grouped.get(doc_id):
                continue
            try:
                chunks = await get_document_chunks_directly(
                    vector_db, [doc_id], limit=1, user_id=user_id, matter_ids=matter_ids
                )
            except Exception as e:  # fallback is best-effort per document
                logger.warning(f"Strategy brief fallback chunk fetch failed for {doc_id}: {e}")
                chunks = []
            if chunks:
                chunk = dict(chunks[0])
                # Not a semantic match — do not inherit the direct-fetch 1.0 score.
                chunk["similarity"] = 0.0
                grouped[doc_id] = [chunk]
        return grouped

    async def _synthesize(self, client, question: str, passages: list[dict[str, Any]]) -> dict:
        # The model reads each passage in full (a 400-character opening is not
        # enough to assess a clause); only when the scope is very large does
        # every passage shrink, equally, to fit the budget.
        per_passage = max(
            SNIPPET_MAX_CHARS, min(PASSAGE_MAX_CHARS, SYNTHESIS_BUDGET_CHARS // len(passages))
        )
        lines = [
            f"[{p['ref']}] ({p['filename']}) {(p.get('text') or p['snippet'])[:per_passage]}"
            for p in passages
        ]
        prompt = (
            "You are a senior litigation strategist. Using ONLY the passages below from the "
            "client's own documents, assess the client's position on the question.\n\n"
            f"QUESTION: {question}\n\n"
            'Every strength, weakness and next step MUST list the refs (e.g. "d0") of the '
            "passages it relies on. Use only refs that appear below — never invent refs.\n\n"
            'Return STRICT JSON: {"position":"2-4 sentence assessment",'
            '"strengths":[{"point":"...","refs":["d0"]}],'
            '"weaknesses":[{"point":"...","refs":["d1"]}],'
            '"next_steps":[{"step":"...","refs":["d2"]}]}\n\n'
            f"{UNTRUSTED_CONTENT_RULE}\n\n"
            "PASSAGES:\n"
            + untrusted_block("Passages from the client's documents", "\n".join(lines))
        )
        resp = await openai_chat(
            client,
            model=utility_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content or "{}")

    async def _read_and_judge_candidate(
        self,
        client,
        semaphore: asyncio.Semaphore,
        position: str,
        point_text: str,
        proposition: str,
        opinion,
    ) -> tuple[dict[str, Any] | None, case_law_research.OpinionVerdict]:
        """Read one opinion and decide whether it supports the proposition.

        The text is judged window by window until the judge endorses the
        opinion with a verbatim quote that verifies against the real opinion
        text (see ``case_law_research.judge_opinion``). Never raises.

        Returns (authority, verdict): authority is None unless the verdict's
        reason is "attached".
        """

        def build_prompt(part: str, block: str) -> str:
            return (
                "You are selecting legal authority for a litigation strategy brief.\n\n"
                f"OUR POSITION: {position[:600]}\n\n"
                f"POINT IN THE BRIEF: {point_text[:400]}\n\n"
                f"PROPOSITION TO SUPPORT: {proposition[:400]}\n\n"
                f"CASE: {opinion.case_name}"
                f"{' (' + opinion.citation[0] + ')' if opinion.citation else ''}\n"
                f"{UNTRUSTED_CONTENT_RULE}\n\n"
                f"OPINION TEXT ({part}):\n"
                f"{block}"
                "\n\n"
                "Does this part of the opinion state a rule of law, holding, or "
                "reasoning that a lawyer could legitimately cite in support of "
                "the proposition? The case does NOT need to involve similar "
                "parties or facts — general doctrine that favors the "
                "proposition counts. An opinion that is off-topic, or whose "
                "holding favors the OPPOSING side, does not count.\n\n"
                'Return STRICT JSON: {"supports": true|false, '
                '"quote": "ONE contiguous passage of 1-3 sentences copied EXACTLY, character for character, from the opinion text above, stating the rule or holding — or empty", '
                '"how": "one sentence on how a lawyer would use this authority for the proposition, or empty"}'
            )

        verdict = await case_law_research.judge_opinion(
            client,
            semaphore,
            opinion,
            build_prompt=build_prompt,
            endorse_key="supports",
            label="Strategy case-law",
        )
        if verdict.reason != "attached":
            return None, verdict

        explanation = str(verdict.data.get("how") or "").strip() or None
        if explanation:
            # Model prose about this authority may only name this authority.
            explanation, _ = case_law_research.redact_unverified_case_references(
                explanation,
                sources=[verdict.text],
                case_law_results=[{"metadata": {"case_name": opinion.case_name}}],
            )
        return {
            "type": "case_law",
            "source": opinion.case_name,
            "reference": opinion.citation[0] if opinion.citation else None,
            "opinionId": str(opinion.id),
            "cluster_id": str(opinion.cluster_id) if getattr(opinion, "cluster_id", None) else None,
            "url": opinion.absolute_url,
            "passage": verdict.quote[:600],
            "explanation": explanation,
            "verified": True,
        }, verdict

    async def _craft_point_targets(
        self, client, position: str, points: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """For each point: the legal proposition to support, and a search query.

        Strategy points are often factual or commercial observations about THIS
        matter ("the proposal describes a production-ready system") — no opinion
        in any reporter 'supports' those, so judging candidates against the raw
        point text yields nothing. The proposition restates the point as the
        general rule of law a lawyer would actually cite authority for; both
        the search and the judge run against it. Falls back to the point text
        on any failure.
        """
        fallback = [{"proposition": p["point"], "query": p["point"][:80]} for p in points]
        try:
            numbered = "\n".join(f"{i}: {p['point'][:200]}" for i, p in enumerate(points))
            prompt = (
                "For each numbered litigation point, provide:\n"
                '(a) "proposition" — ONE sentence restating the point as the '
                "general rule of law or legal doctrine a lawyer would cite "
                "authority for (no party names, no facts specific to this "
                "matter). If the point is purely factual or commercial, state "
                "the closest legal proposition that would make it matter in "
                "litigation.\n"
                '(b) "query" — a short case-law search query: 3-7 keywords '
                "naming that doctrine.\n\n"
                f"OUR POSITION: {position[:400]}\n\n"
                f"POINTS:\n{numbered}\n\n"
                'Return STRICT JSON: {"targets": [{"proposition": "...", "query": "..."}, ...]} '
                "with exactly one entry per numbered point, in order."
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
            if isinstance(targets, list) and len(targets) == len(points):
                return [
                    {
                        "proposition": str((t or {}).get("proposition") or "").strip()
                        or fallback[i]["proposition"],
                        "query": str((t or {}).get("query") or "").strip() or fallback[i]["query"],
                    }
                    if isinstance(t, dict)
                    else fallback[i]
                    for i, t in enumerate(targets)
                ]
            logger.warning("Case-law target crafting returned a mismatched list; using point text")
        except Exception as e:  # crafting is an optimization, never fatal
            logger.warning(f"Case-law target crafting failed, using point text: {e}")
        return fallback

    async def _attach_case_law(
        self, sections: dict[str, Any], citations: list[dict[str, Any]], client
    ) -> dict[str, Any]:
        """Attach verified supporting case law to every strength/weakness point.

        For each point: restate it as a legal proposition with a doctrine
        search query, search CourtListener, read each candidate opinion
        window by window (bounded; see ``partially_read``), judge whether it supports the
        proposition, and attach only authorities whose supporting quote
        verifies verbatim against the opinion text. Best-effort enhancement:
        any failure is logged and skipped — case law never blocks the brief.

        Returns a status dict for the brief payload with per-reason drop
        counters — when nothing survives, the user must be TOLD exactly why,
        never shown silence or a guessed reason.
        """
        position = str(sections.get("position") or "")
        points = list(sections["strengths"]) + list(sections["weaknesses"])
        # Best-documented points first, in case the point cap bites.
        points.sort(key=lambda p: len(p["citation_ids"]), reverse=True)
        points = points[:MAX_CASE_LAW_POINTS]

        status = {
            "requested": True,
            "points_searched": len(points),
            "opinions_read": 0,
            "partially_read": 0,
            "attached": 0,
            "unreadable": 0,
            "unsupportive": 0,
            "quote_unverified": 0,
            "errors": 0,
            "searches_failed": 0,
        }
        if not points:
            return status

        targets = await self._craft_point_targets(client, position, points)
        semaphore = asyncio.Semaphore(CASE_LAW_CONCURRENCY)
        # The concrete reason searches failed (rate limit, network, bad
        # request) — surfaced to the user instead of a generic "unreachable".
        search_errors: list[str] = []

        async def support_point(
            point: dict[str, Any], target: dict[str, str]
        ) -> tuple[dict, list[dict], dict[str, int]]:
            counters = {
                "opinions_read": 0,
                "partially_read": 0,
                "unreadable": 0,
                "unsupportive": 0,
                "quote_unverified": 0,
                "errors": 0,
                "searches_failed": 0,
            }
            try:
                opinions = await courtlistener_service.search_opinions(
                    query=target["query"], limit=CASE_LAW_CANDIDATES_PER_POINT, cited_gt=2
                )
            except Exception as e:  # enhancement only, skipped but counted
                logger.warning(f"Strategy brief case-law lookup skipped: {e}")
                counters["searches_failed"] = 1
                search_errors.append(f"{type(e).__name__}: {e}"[:200])
                return point, [], counters

            candidates = (opinions or [])[:CASE_LAW_CANDIDATES_PER_POINT]
            results = await asyncio.gather(
                *(
                    self._read_and_judge_candidate(
                        client,
                        semaphore,
                        position,
                        point["point"],
                        target["proposition"],
                        op,
                    )
                    for op in candidates
                )
            )
            kept: list[dict] = []
            for authority, verdict in results:
                if verdict.reason != "unreadable":
                    counters["opinions_read"] += 1
                if verdict.partial:
                    counters["partially_read"] += 1
                if authority is not None:
                    kept.append(authority)
                elif verdict.reason == "error":
                    counters["errors"] += 1
                else:
                    counters[verdict.reason] += 1
            return point, kept[:CASE_LAW_KEPT_PER_POINT], counters

        # Attach as each point completes (not after ALL complete): if the stage
        # hits its time budget mid-flight, every already-verified authority is
        # still on the brief instead of being thrown away with the gather.
        id_counter = itertools.count()

        async def support_and_attach(point: dict[str, Any], target: dict[str, str]) -> None:
            point, kept, counters = await support_point(point, target)
            for key, value in counters.items():
                status[key] += value
            case_refs: list[str] = []
            for authority in kept:
                authority["id"] = f"c{next(id_counter)}"
                citations.append(authority)
                case_refs.append(authority["id"])
            if case_refs:
                point["case_refs"] = case_refs
                status["attached"] += len(case_refs)

        await asyncio.gather(
            *(support_and_attach(p, t) for p, t in zip(points, targets, strict=False))
        )
        if search_errors:
            status["search_error"] = search_errors[0]
        return status

    async def generate_brief(
        self,
        *,
        question: str,
        folder_path: str | None = None,
        folder_paths: list[str] | None = None,
        document_ids: list[str] | None = None,
        include_case_law: bool = False,
        user_id: str,
        user_keys=None,
        accessible_matter_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """``accessible_matter_ids`` are the matters the caller belongs to: a
        colleague's document filed under one of them is read like the caller's
        own (owner-only when None)."""
        client = make_openai(self._api_key(user_keys), async_=True)
        if client is None:
            raise ValueError(
                "An OpenAI API key or a custom LLM endpoint is required. Configure one in Settings."
            )

        # Every selected folder restricts the scope — a multi-folder selection
        # is their union, never just the first one. ``folder_path`` stays as
        # the single-folder form for existing callers.
        scope_folders = [p for p in [folder_path, *(folder_paths or [])] if p]
        scope_folders = list(dict.fromkeys(scope_folders)) or None
        matched_ids = await document_service.get_documents_by_filter(
            user_id=user_id,
            document_ids=document_ids,
            folder_paths=scope_folders,
            accessible_matter_ids=accessible_matter_ids,
        )
        documents_total = len(matched_ids)
        if documents_total == 0:
            raise LookupError("No indexed documents matched the requested scope.")
        if documents_total > MAX_DOCUMENTS:
            logger.info(
                f"Strategy brief scope truncated from {documents_total} to "
                f"{MAX_DOCUMENTS} documents for user {user_id}"
            )
        docs = await document_service.get_documents_by_ids(
            matched_ids[:MAX_DOCUMENTS], user_id, accessible_matter_ids
        )
        doc_order = [(doc.id, doc.filename) for doc in docs]

        grouped = await self._retrieve_coverage(
            question,
            doc_order,
            user_id,
            user_keys,
            matter_ids=sorted(accessible_matter_ids) if accessible_matter_ids else None,
        )
        passages = build_passages(doc_order, grouped)
        if not passages:
            raise LookupError("No indexed content is available for the selected documents.")

        try:
            llm_data = await self._synthesize(client, question, passages)
        except Exception as e:  # any LLM/parse failure maps to one clean 502
            logger.error(f"Strategy brief synthesis failed for user {user_id}: {e}")
            raise StrategySynthesisError(
                "The strategy synthesis could not be completed. Please try again."
            ) from e

        sections, citations = join_citations(llm_data, passages)
        # The synthesis is told to use only the client's passages, but it is
        # still a model: any case it names that is not in those passages (or
        # the question) came from its own weights and is removed. Verified
        # authority is attached separately, below.
        guard_sources = [question] + [p.get("text") or p["snippet"] for p in passages]
        removed: list[str] = []
        sections["position"], gone = case_law_research.redact_unverified_case_references(
            sections["position"], sources=guard_sources
        )
        removed += gone
        for key, text_key in (
            ("strengths", "point"),
            ("weaknesses", "point"),
            ("next_steps", "step"),
        ):
            for item in sections[key]:
                item[text_key], gone = case_law_research.redact_unverified_case_references(
                    item[text_key], sources=guard_sources
                )
                removed += gone
        if removed:
            logger.info(f"Strategy brief: removed {len(removed)} unverified case reference(s)")
        case_law_status = None
        if include_case_law:
            # Hard time budget: reading candidate opinions in full is real
            # network + LLM work. If it overruns, ship the brief with whatever
            # verified in time — the brief itself never stalls on authority.
            try:
                case_law_status = await asyncio.wait_for(
                    self._attach_case_law(sections, citations, client),
                    timeout=CASE_LAW_TIME_BUDGET_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    f"Strategy case-law stage exceeded its "
                    f"{CASE_LAW_TIME_BUDGET_SECONDS:.0f}s budget"
                )
                case_law_status = {
                    "requested": True,
                    "points_searched": 0,
                    "opinions_read": 0,
                    "partially_read": 0,
                    "attached": sum(
                        len(p.get("case_refs") or [])
                        for p in list(sections["strengths"]) + list(sections["weaknesses"])
                    ),
                    "unreadable": 0,
                    "unsupportive": 0,
                    "quote_unverified": 0,
                    "errors": 0,
                    "searches_failed": 0,
                    "timed_out": True,
                }

        return {
            "question": question,
            "scope": {
                "folder_path": scope_folders[0] if scope_folders else None,
                "folder_paths": scope_folders or [],
                "documents_considered": len(docs),
                "documents_total": documents_total,
                # What the synthesis actually read: the most relevant passages
                # of each document, not every document in full.
                "passages_read": len(passages),
                "passages_per_document": CHUNKS_PER_DOCUMENT,
                "truncated": documents_total > len(docs),
                "coverage_note": (
                    f"Synthesised from the {len(passages)} most relevant passages "
                    f"(up to {CHUNKS_PER_DOCUMENT} per document) of {len(docs)} "
                    + ("document" if len(docs) == 1 else "documents")
                    + (
                        f"; {documents_total - len(docs)} more in scope were not read "
                        f"(limit of {MAX_DOCUMENTS} per brief)."
                        if documents_total > len(docs)
                        else "."
                    )
                    + " The documents were not read in full."
                ),
            },
            "case_law_removed": removed,
            "position": sections["position"],
            "strengths": sections["strengths"],
            "weaknesses": sections["weaknesses"],
            "next_steps": sections["next_steps"],
            "citations": citations,
            "case_law": case_law_status,
        }


strategy_service = StrategyService()
