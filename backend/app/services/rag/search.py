"""Search pipeline for RAG - document search, direct chunk fetch, and case law search."""

import json
import logging
import math
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from app.config import settings
from app.services.courtlistener import courtlistener_service
from app.services.courtlistener_client import resolve_court_ids
from app.services.embeddings import embedding_service
from app.services.llm_clients import make_openai, openai_chat, utility_model
from app.services.provider_policy import enforce_openai_client
from app.services.rag.generation import chosen_provider_text, strip_json_fences
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block
from app.services.reranking import rerank as rerank_candidates
from app.services.reranking import reranker_available
from app.services.resilience import CircuitBreakerOpen, retry_with_backoff
from app.services.search import search_service

if TYPE_CHECKING:
    from app.services.user_keys import UserAPIKeys

logger = logging.getLogger(__name__)


class SearchUnavailableError(RuntimeError):
    """Search infrastructure failed (embeddings down, key rejected, index error).

    Results are UNKNOWN, not empty — callers must never present this as
    "no matching documents". Carries a user-actionable message.
    """


def tenant_scope(user_id: str = "", matter_ids: list[str] | None = None) -> dict | None:
    """Vector-store filter pinning results to what the caller may see.

    The caller can see chunks they own (user_id) OR chunks in a matter they
    belong to (matter_id in matter_ids); combined as $or so a shared-matter
    document is visible to every member while a user's own chunks stay visible
    regardless of matter tagging (back-compat with chunks indexed before
    matter_id existed). None when neither is given — the vector store refuses
    an unscoped query.
    """
    conditions: list[dict] = []
    if user_id:
        conditions.append({"user_id": user_id})
    if matter_ids:
        conditions.append({"matter_id": {"$in": list(matter_ids)}})
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else {"$or": conditions}


def _and(*conditions: dict | None) -> dict | None:
    present = [c for c in conditions if c]
    if not present:
        return None
    return present[0] if len(present) == 1 else {"$and": present}


def _is_request_error(error: Exception) -> bool:
    """True when the REQUEST is at fault (bad key, config), not the provider.

    Such errors are not retried and do not count against the circuit breaker:
    the upstream service is healthy, and retrying a rejected key three times
    only delays the error and can open the breaker.
    """
    if isinstance(error, ValueError):  # missing key, provider not on the allowlist
        return True
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status in (400, 401, 403, 404, 422)


async def embed_search_query(text: str, user_keys: Optional["UserAPIKeys"] = None) -> list[float]:
    """Embed a search query, or raise SearchUnavailableError with a usable message.

    The circuit breaker is scoped to the provider + credential in use, so one
    user's rejected BYOK key cannot block search for everyone else.
    """
    try:
        return await retry_with_backoff(
            lambda q: embedding_service.embed_query(q, user_keys=user_keys),
            text,
            circuit_breaker_name=f"embeddings:{embedding_service.credential_scope(user_keys)}",
            max_attempts=3,
            is_retryable=lambda e: not _is_request_error(e),
        )
    except CircuitBreakerOpen:
        logger.error("Embedding service circuit breaker open")
        raise SearchUnavailableError(
            "The embedding service is temporarily unavailable after repeated "
            "failures. Wait a minute and try again."
        )
    except Exception as e:  # every embed failure means "could not look"
        # An embedding failure means we could not LOOK, not that nothing was
        # found — swallowing this used to surface as "no relevant documents",
        # and provider SDK errors (openai.AuthenticationError etc.) used to
        # escape as raw 500s that leaked the upstream error payload.
        logger.error(f"Failed to generate query embedding: {type(e).__name__}: {e}")
        lowered = str(e).lower()
        if "not approved" in lowered or "allowlist" in lowered:
            reason = str(e)
        elif "api key" in lowered or "invalid_api_key" in lowered or "401" in lowered:
            reason = (
                "The embedding API key was rejected — check your key in Settings "
                "(and the server's OPENAI_API_KEY if you run the instance)."
            )
        else:
            reason = "Generating the search embedding failed. Check your API key in Settings and try again."
        raise SearchUnavailableError(reason) from e


async def _vector_search(
    vector_db, query_embedding: list[float], top_k: int, search_filter: dict | None
) -> list[dict[str, Any]]:
    """Query the vector store; store failures become SearchUnavailableError."""
    if vector_db is None:
        raise SearchUnavailableError(
            "The document index is unavailable. Ask your administrator to check "
            "the vector database."
        )
    try:
        return await vector_db.search(
            query_embedding=query_embedding, top_k=top_k, filter=search_filter
        )
    except ValueError:
        # Missing tenant scope — a caller bug, never to be masked as an outage.
        raise
    except Exception as e:  # chromadb / pinecone / sqlite errors share no base class
        logger.error(f"Vector store query failed: {type(e).__name__}: {e}", exc_info=True)
        raise SearchUnavailableError(
            "The document index could not be queried. Try again; if it keeps "
            "failing, ask your administrator to check the vector database."
        ) from e


def _doc_id(result: dict[str, Any]) -> str | None:
    return (result.get("metadata") or {}).get("document_id")


async def _search_selected_documents(
    vector_db,
    query_embedding: list[float],
    document_ids: list[str],
    top_k: int,
    scope: dict | None,
    extra_filter: dict | None,
) -> list[dict[str, Any]]:
    """Semantic retrieval restricted to the selected documents, spread across them.

    One pooled query over all selected files ranks passages by true
    similarity. A pooled query alone lets one long or on-topic file take every
    slot, so when several files are selected each one is also guaranteed a
    share of the results: half the budget is split evenly between the files
    (their best-matching passages), the rest goes to the best passages overall.
    With more files than slots, the pooled ranking is used as is.
    """
    pooled = await _vector_search(
        vector_db,
        query_embedding,
        top_k,
        _and(scope, {"document_id": {"$in": list(document_ids)}}, extra_filter),
    )
    n_docs = len(document_ids)
    if n_docs == 1 or n_docs > top_k:
        return pooled

    floor = max(1, top_k // (2 * n_docs))
    by_doc: dict[str, list[dict[str, Any]]] = {doc_id: [] for doc_id in document_ids}
    for r in pooled:
        if _doc_id(r) in by_doc:
            by_doc[_doc_id(r)].append(r)

    for doc_id in document_ids:
        if len(by_doc[doc_id]) >= floor:
            continue
        # Under-represented in the pooled ranking: fetch this file's own best
        # passages (a file smaller than the floor simply contributes all of it).
        by_doc[doc_id] = await _vector_search(
            vector_db,
            query_embedding,
            floor,
            _and(scope, {"document_id": doc_id}, extra_filter),
        )

    chosen: dict[str, dict[str, Any]] = {}
    for doc_id in document_ids:
        for r in by_doc[doc_id][:floor]:
            chosen[r["id"]] = r
    for r in pooled:  # already best-first
        if len(chosen) >= top_k:
            break
        chosen.setdefault(r["id"], r)

    return sorted(chosen.values(), key=lambda r: r.get("similarity", 0), reverse=True)[:top_k]


def _spread_across_documents(
    ranked: list[dict[str, Any]], document_ids: list[str], top_k: int
) -> list[dict[str, Any]]:
    """Keep every selected file's share of a fused ranking (see above).

    Half the budget goes to each file's best passages, the rest to the best
    passages overall, preserving the fused order.
    """
    n_docs = len(document_ids)
    if n_docs <= 1 or n_docs > top_k:
        return ranked[:top_k]
    floor = max(1, top_k // (2 * n_docs))
    taken: dict[str, int] = {}
    chosen: set[str] = set()
    for r in ranked:
        did = _doc_id(r)
        if did in document_ids and taken.get(did, 0) < floor:
            taken[did] = taken.get(did, 0) + 1
            chosen.add(r["id"])
    for r in ranked:
        if len(chosen) >= top_k:
            break
        chosen.add(r["id"])
    return [r for r in ranked if r["id"] in chosen][:top_k]


# Reciprocal rank fusion constant (Cormack et al., 2009). Larger values flatten
# the advantage of the very top ranks; 60 is the customary default.
RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, Any]]],
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Merge independently ranked result lists by summed weight / (k + rank).

    A passage found by both retrievers keeps every field from both (the
    vector hit's similarity, the keyword hit's BM25 score). Each result gets
    ``fusion_score`` and ``retrieved_by``. Ranks, not raw scores, are fused:
    cosine similarity and BM25 live on unrelated scales.
    """
    weights = weights or [1.0] * len(ranked_lists)
    fused: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    sources: dict[str, list[str]] = {}
    for weight, results in zip(weights, ranked_lists, strict=True):
        for rank, r in enumerate(results, start=1):
            key = r["id"]
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
            source = "keyword" if "lexical_rank" in r else "vector"
            sources.setdefault(key, [])
            if source not in sources[key]:
                sources[key].append(source)
            if key in fused:
                for field, value in r.items():
                    fused[key].setdefault(field, value)
            else:
                fused[key] = dict(r)
    ordered = sorted(fused.values(), key=lambda r: scores[r["id"]], reverse=True)
    for r in ordered:
        r["fusion_score"] = scores[r["id"]]
        # The reranker's no-model path and downstream sorting read this key.
        r["hybrid_score"] = scores[r["id"]]
        r["retrieved_by"] = sources[r["id"]]
    return ordered


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _keyword_candidates(
    vector_db,
    text: str,
    phrases: list[str],
    limit: int,
    search_filter: dict | None,
    query_embedding: list[float],
) -> list[dict[str, Any]] | None:
    """Full-corpus keyword hits within ``search_filter``, or None when unavailable.

    Each hit gets its true cosine similarity to the query from its stored
    vector, so thresholds, citations and confidence read the same measured
    quantity for every passage. A hit with no stored vector is a stale
    keyword entry (its document is gone from the vector store) and is dropped.
    """
    from app.services.vectordb import VectorDBBase

    if not isinstance(vector_db, VectorDBBase):
        return None
    hits = await vector_db.keyword_search(text, limit, search_filter, phrases)
    if not hits:
        return hits
    vectors = await vector_db.get_embeddings([h["id"] for h in hits])
    kept = []
    for h in hits:
        vector = vectors.get(h["id"])
        if vector is None:
            continue
        h["similarity"] = _cosine(query_embedding, vector)
        kept.append(h)
    return kept


async def search_documents(
    vector_db,
    query: str,
    top_k: int = None,
    similarity_threshold: float = None,
    filter: dict | None = None,
    use_hybrid: bool = None,
    use_reranking: bool = None,
    user_keys: Optional["UserAPIKeys"] = None,
    document_ids: list[str] | None = None,
    user_id: str = "",
    matter_ids: list[str] | None = None,
    use_lexical: bool | None = None,
) -> list[dict[str, Any]]:
    """
    Hybrid search over the caller's documents.

    - The question is embedded as typed. Explicit search syntax only —
      uppercase AND/OR/NOT, ``-term``, known ``field:value`` filters — is
      parsed out; plain English is never treated as Boolean.
    - ``document_ids`` scopes the search: ``None`` searches everything the
      caller may see; a list restricts retrieval to those documents (spread
      across them); an EMPTY list returns nothing — a scope that matched no
      documents must never widen to the whole knowledge base.
    - Hybrid: vector search and a full-corpus keyword search (BM25 over the
      keyword index) run independently over the same scope and are merged by
      reciprocal rank fusion, so a passage can be found by its exact words
      even when the vector search ranks it low. ``use_lexical=False`` (or an
      unavailable keyword index) falls back to BM25 rescoring of the vector
      candidates. Cross-encoder reranking applies only when
      sentence-transformers is installed.
    - Tenant isolation: every query carries a user_id / matter_id filter.

    Raises SearchUnavailableError when embeddings or the vector store fail.
    """
    top_k = top_k or settings.top_k
    similarity_threshold = similarity_threshold or settings.similarity_threshold
    use_hybrid = use_hybrid if use_hybrid is not None else settings.hybrid_search_enabled
    use_lexical = bool(use_hybrid) and (
        use_lexical if use_lexical is not None else settings.lexical_search_enabled
    )
    use_reranking = use_reranking if use_reranking is not None else settings.rerank_enabled
    # The flag alone does nothing without a usable reranker backend (the
    # optional local model package, or a hosted reranker with a key).
    use_reranking = bool(use_reranking) and reranker_available(user_keys)
    pool = max(top_k, settings.hybrid_candidate_pool)

    if document_ids is not None and not document_ids:
        logger.info("[Search] Scope resolved to zero documents; returning no results")
        return []

    # Parse explicit operators and filters; the semantic text is the question
    # as typed unless such syntax was present.
    parsed_query = search_service.parse_query(query)
    semantic_query = parsed_query.get_semantic_query() or query

    query_embedding = await embed_search_query(semantic_query, user_keys)

    # Always enforce a tenant scope (see tenant_scope).
    scope = tenant_scope(user_id, matter_ids)
    extra_filter = _and(*({k: v} for k, v in filter.items())) if filter else None

    keyword: list[dict[str, Any]] | None = None
    if document_ids:
        # Explicit selection: retrieve within those files and keep what was
        # found — the user chose the files, so a low score is not a reason to
        # discard their passages. Weak matches are flagged, not dropped.
        results = await _search_selected_documents(
            vector_db,
            query_embedding,
            document_ids,
            pool if use_lexical else top_k,
            scope,
            extra_filter,
        )
        if use_lexical:
            keyword = await _keyword_candidates(
                vector_db,
                semantic_query,
                parsed_query.phrases,
                pool,
                _and(scope, {"document_id": {"$in": list(document_ids)}}, extra_filter),
                query_embedding,
            )
        if keyword is not None:
            results = _spread_across_documents(
                reciprocal_rank_fusion(
                    [results, keyword],
                    k=settings.hybrid_rrf_k,
                    weights=[1.0, settings.hybrid_keyword_weight],
                ),
                document_ids,
                top_k,
            )
        elif use_lexical:
            # Keyword index unavailable: the larger pool must still leave
            # every selected file its share at the final size.
            results = _spread_across_documents(results, document_ids, top_k)
        for r in results:
            if r["similarity"] < similarity_threshold:
                r.setdefault("metadata", {})["below_threshold"] = True
        filtered_results = results
    else:
        # Retrieve a wider candidate pool when it will be fused or rescored.
        if use_lexical:
            initial_top_k = pool
        elif use_hybrid or use_reranking:
            initial_top_k = max(top_k, settings.rerank_top_k)
        else:
            initial_top_k = top_k
        results = await _vector_search(
            vector_db, query_embedding, initial_top_k, _and(scope, extra_filter)
        )
        logger.debug(f"[Search] Vector DB returned {len(results)} results before threshold filter")
        if use_lexical:
            keyword = await _keyword_candidates(
                vector_db,
                semantic_query,
                parsed_query.phrases,
                pool,
                _and(scope, extra_filter),
                query_embedding,
            )
        if keyword is not None:
            results = reciprocal_rank_fusion(
                [results, keyword],
                k=settings.hybrid_rrf_k,
                weights=[1.0, settings.hybrid_keyword_weight],
            )
            logger.debug(f"[Search] Fused with {len(keyword)} keyword hits -> {len(results)}")

        # Filter by similarity threshold
        filtered_results = [r for r in results if r["similarity"] >= similarity_threshold]
        logger.debug(
            f"[Search] After threshold filter ({similarity_threshold}): {len(filtered_results)} results"
        )
        if results and not filtered_results:
            # Best-effort fallback: an absolute cosine floor is a junk filter for
            # large corpora, not an oracle. Analytical questions ("is anything
            # illegal in here?") legitimately score low against their own
            # documents — answering from the closest passages beats refusing.
            # The below_threshold flag lets the UI show these as weak matches.
            best = max(r["similarity"] for r in results)
            logger.info(
                f"[Search] Nothing cleared threshold {similarity_threshold} "
                f"(best {best:.3f}) — falling back to top {min(3, top_k)} passages."
            )
            filtered_results = results[: min(3, top_k)]
            for r in filtered_results:
                r.setdefault("metadata", {})["below_threshold"] = True

    if keyword is not None:
        # Already in fused order; the configured reranker (if any) refines it.
        if use_reranking:
            filtered_results = await rerank_candidates(
                semantic_query, filtered_results, top_k, user_keys
            )
    elif use_hybrid or use_reranking:
        # Fallback: keyword rescoring of the vector candidates, then the
        # configured reranker (if any)
        if use_hybrid:
            filtered_results = await search_service.search(
                query,
                filtered_results,
                apply_filters=False,
                rerank=False,
                top_k=max(top_k, len(filtered_results)),
            )
        if use_reranking:
            filtered_results = await rerank_candidates(
                semantic_query, filtered_results, top_k, user_keys
            )

    # Apply explicit query filters (date, court, type, excluded terms)
    filtered_results = search_service.apply_filters(filtered_results, parsed_query)

    filtered_results = filtered_results[:top_k]
    # Add rank to results
    for i, result in enumerate(filtered_results):
        result["rank"] = i + 1

    return filtered_results


async def get_document_chunks_directly(
    vector_db,
    document_ids: list[str],
    limit: int = 20,
    user_id: str = "",
    matter_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Read documents' stored chunks in order, without any search.

    For callers that need a document's TEXT (rebuilding the full indexed text,
    or guaranteeing a file is represented) — not for answering a question:
    nothing is ranked, so results carry ``similarity`` 0.0 and
    ``direct_fetch: True`` rather than a score. Question answering over
    selected files goes through :func:`search_documents` with ``document_ids``.

    Raises ValueError without a tenant scope (a document_id alone must never
    read another tenant's chunks) and SearchUnavailableError when the vector
    store fails — a failed read is not an empty document.
    """
    scope = tenant_scope(user_id, matter_ids)
    if scope is None:
        raise ValueError("a user_id or matter_id scope is required to fetch document chunks")
    if vector_db is None:
        raise SearchUnavailableError("The document index is unavailable.")

    results: list[dict[str, Any]] = []
    for doc_id in document_ids:
        try:
            chunks = await vector_db.get_document_chunks(doc_id, scope, limit=limit)
        except ValueError:
            raise
        except Exception as e:  # store errors share no base class
            logger.error(f"[DirectFetch] Error fetching chunks for {doc_id}: {e}", exc_info=True)
            raise SearchUnavailableError(
                "The document index could not be read. Try again; if it keeps "
                "failing, ask your administrator to check the vector database."
            ) from e
        chunks.sort(key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))
        for i, chunk in enumerate(chunks):
            results.append({**chunk, "similarity": 0.0, "rank": i + 1, "direct_fetch": True})
        if len(results) >= limit:
            break
    logger.debug(f"[DirectFetch] Total chunks fetched: {len(results)}")
    return results[:limit]


# Broad (unfiltered) searches keep a small citation floor so the handful of
# results is not filled with unpublished trial orders. A search the user has
# narrowed by court or date drops it: within a court or a recent window the
# newest authority is often the most important, and it has had no time to be
# cited yet.
UNFILTERED_MIN_CITATIONS = 2


def _as_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).strip()[:10]).date()
    except ValueError:
        return None


def case_law_search_filters(
    courts: list[str] | None = None,
    date_from: str | date | None = None,
    date_to: str | date | None = None,
) -> dict[str, Any]:
    """Keyword arguments for ``search_opinions`` from the user's filters.

    Courts are resolved to CourtListener ids (unknown names are dropped, not
    sent); dates accept ISO strings or ``date`` objects.
    """
    court_ids = resolve_court_ids(courts)
    after = _as_date(date_from)
    before = _as_date(date_to)
    narrowed = bool(court_ids or after or before)
    return {
        "court_ids": court_ids or None,
        "date_after": after,
        "date_before": before,
        "cited_gt": None if narrowed else UNFILTERED_MIN_CITATIONS,
    }


async def search_case_law(
    query: str,
    limit: int = 5,
    date_from: str | date | None = None,
    date_to: str | date | None = None,
    courts: list[str] | None = None,
    question: str | None = None,
    user_keys: Optional["UserAPIKeys"] = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Search CourtListener for relevant case law with retry logic.

    Never raises: a CourtListener outage, rate limit or HTTP error means "no
    case law this time" — it must not take down an answer that can still be
    given from the user's documents.

    ``query`` is the keyword search term (distilled topic keywords, never the
    raw chat message). ``question`` is the user's actual question; when a
    utility LLM is available, every candidate is judged against it and only
    genuinely relevant cases survive — each with an honest 0-1 relevance and
    a short case summary in ``metadata.case_summary``. A keyword hit is not a
    citation.

    ``courts`` / ``date_from`` / ``date_to`` restrict the CourtListener search
    itself (see :func:`case_law_search_filters`).
    """
    filters = case_law_search_filters(courts, date_from, date_to)

    async def _search():
        opinions = await courtlistener_service.search_opinions(
            query=query,
            limit=limit,
            **filters,
        )
        return opinions

    try:
        opinions = await retry_with_backoff(
            _search,
            circuit_breaker_name="courtlistener",
            max_attempts=3,
            retryable_exceptions=(Exception,),
        )
    except CircuitBreakerOpen:
        logger.warning("CourtListener circuit breaker open - case law search unavailable")
        return []
    except Exception as e:  # httpx.HTTPError and friends share no builtin base
        logger.error(f"CourtListener search failed after retries: {type(e).__name__}: {e}")
        return []

    results = []
    for i, opinion in enumerate((opinions or [])[:limit]):
        text = opinion.snippet or ""
        if i < 3 and opinion.id:
            try:

                async def _get_full():
                    return await courtlistener_service.get_opinion(opinion.id)

                full_opinion = await retry_with_backoff(
                    _get_full, circuit_breaker_name="courtlistener", max_attempts=2
                )
                if full_opinion and full_opinion.text:
                    text = full_opinion.text[:2000]
            except Exception as e:  # full text is an optional enhancement
                logger.debug(f"Full opinion fetch failed for {opinion.id}: {type(e).__name__}")

        citation_str = opinion.citation[0] if opinion.citation else "No citation"
        raw_score = opinion.score or 0

        results.append(
            {
                "id": f"courtlistener_{opinion.id}",
                "text": text,
                "metadata": {
                    "filename": f"{opinion.case_name} ({citation_str})",
                    "source": "courtlistener",
                    "doc_type": "case_law",
                    "court": opinion.court,
                    "date_filed": str(opinion.date_filed) if opinion.date_filed else None,
                    "citation": citation_str,
                    "url": opinion.absolute_url,
                    # Both ids travel with the citation so the UI opens the
                    # exact opinion that was read, not a same-numbered cluster.
                    "opinion_id": opinion.id,
                    "cluster_id": opinion.cluster_id,
                    "chunk_index": 0,
                    "raw_score": raw_score,
                    "case_name": opinion.case_name,
                },
                # Set honestly by the relevance judge below; never invented
                # from search rank.
                "similarity": 0.0,
                "rank": i + 1,
            }
        )

    return await _judge_case_law_relevance(question or query, results, user_keys, model)


def _parse_case_verdicts(raw: str | None, count: int) -> list[dict[str, Any]] | None:
    """Normalize the judge's JSON: one verdict per candidate, by index."""
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    items = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    verdicts: list[dict[str, Any]] = [
        {"relevant": False, "confidence": 0.0, "summary": ""} for _ in range(count)
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < count:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        verdicts[idx] = {
            "relevant": bool(item.get("relevant")),
            "confidence": confidence,
            "summary": str(item.get("summary") or "").strip(),
        }
    return verdicts


async def _judge_case_law_relevance(
    question: str,
    results: list[dict[str, Any]],
    user_keys: Optional["UserAPIKeys"] = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """ONE utility-LLM call judging every search hit against the question.

    ``model`` is the user's chosen chat model: when it belongs to Anthropic or
    Google the screening runs there, otherwise on the OpenAI-compatible client.

    Irrelevant cases are dropped — a lawyer must never be shown a contract
    dispute as authority for a DWI question. Survivors carry the judge's
    confidence as their relevance and a short case summary. On any failure
    the results pass through unjudged with similarity 0 (unknown), never a
    fabricated score.
    """
    if not results:
        return results

    case_lines = "\n\n".join(
        f"[{i}] {r['metadata']['case_name']} ({r['metadata']['citation']}, "
        f"{r['metadata'].get('court') or 'court unknown'}):\n"
        f'"{" ".join((r.get("text") or "").split())[:700]}"'
        for i, r in enumerate(results)
    )
    prompt = (
        "You are screening case-law search results for a legal research "
        "question. Keyword search returns noise; your job is to keep only "
        "cases a lawyer would accept as on-topic authority.\n\n"
        f"QUESTION: {question[:500]}\n\n"
        "For EACH numbered case below, using ONLY the text provided:\n"
        '- "relevant": true only if the case genuinely concerns the legal '
        "topic of the question (same offense, doctrine, or issue). A case "
        "about a different subject is false, no matter how it was retrieved.\n"
        '- "confidence": 0-1, how strongly the case bears on the question.\n'
        '- "summary": 2-3 sentences on what the case is about and what the '
        "court held, from the text provided — never invent facts.\n\n"
        'Return STRICT JSON: {"cases":[{"i":0,"relevant":true,'
        '"confidence":0.9,"summary":"..."}]}\n\n'
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"CASES:\n{untrusted_block('Case-law search results', case_lines)}"
    )
    try:
        raw = await chosen_provider_text(prompt, user_keys=user_keys, model=model)
        if raw is None:
            api_key = user_keys.openai if user_keys and getattr(user_keys, "openai", None) else None
            client = make_openai(api_key, async_=True)
            if client is None:
                return results
            enforce_openai_client(client, "case-law screening")
            resp = await openai_chat(
                client,
                model=utility_model(),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content
        verdicts = _parse_case_verdicts(strip_json_fences(raw), len(results))
    except Exception as e:  # screening must never break the search
        logger.warning(f"Case-law relevance screening failed, passing results unjudged: {e}")
        return results
    if verdicts is None:
        logger.warning("Case-law relevance screening returned garbage; passing results unjudged")
        return results

    kept: list[dict[str, Any]] = []
    for result, verdict in zip(results, verdicts, strict=False):
        if not verdict["relevant"]:
            continue
        result["similarity"] = verdict["confidence"]
        if verdict["summary"]:
            result["metadata"]["case_summary"] = verdict["summary"]
        kept.append(result)
    dropped = len(results) - len(kept)
    if dropped:
        logger.info(f"Case-law screening dropped {dropped} of {len(results)} off-topic results")
    for rank, result in enumerate(kept, start=1):
        result["rank"] = rank
    return kept
