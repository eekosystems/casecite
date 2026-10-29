"""Model-based reranking of fused search candidates.

Vector similarity and BM25 each score a passage against the question
independently of the other candidates; a reranker reads the question and each
candidate passage together and scores relevance directly, which is the
precision layer on top of hybrid retrieval.

Backends (``settings.reranker_provider``):

* ``"local"`` — a cross-encoder via the optional ``sentence-transformers``
  package (not in requirements.txt: it pulls PyTorch). Without the package
  this backend is unavailable and results keep their fused order.
* ``"cohere"`` — Cohere's hosted Rerank API. No local model, no PyTorch. The
  candidate passages are sent to Cohere, so ``cohere`` must be in
  ``APPROVED_AI_PROVIDERS``; the user's own Cohere key (BYOK) is preferred
  over the instance key.
* ``"none"`` — never rerank.

Reranking is best-effort: any failure (no key, provider not approved, HTTP
error, timeout) keeps the fused order and never fails the search.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.config import settings

if TYPE_CHECKING:
    from app.services.user_keys import UserAPIKeys

logger = logging.getLogger(__name__)

COHERE_RERANK_URL = "https://api.cohere.com/v2/rerank"
# Characters of each candidate sent to the reranker: the context header plus
# the passage, bounded so a long chunk cannot blow the request size.
RERANK_TEXT_LIMIT = 4_000


def _cohere_key(user_keys: UserAPIKeys | None) -> str | None:
    key = getattr(user_keys, "cohere", None) if user_keys else None
    return key or settings.cohere_api_key or None


def reranker_available(user_keys: UserAPIKeys | None = None) -> bool:
    """True when the configured backend can actually run for this request."""
    provider = (settings.reranker_provider or "none").lower()
    if provider == "local":
        from app.services.search import cross_encoder_available

        return cross_encoder_available()
    if provider == "cohere":
        return bool(_cohere_key(user_keys))
    return False


def rerank_text(result: dict[str, Any]) -> str:
    """What the reranker reads for a candidate: where it sits, then its text."""
    meta = result.get("metadata") or {}
    header = [str(meta[k]) for k in ("filename", "doc_context", "section") if meta.get(k)]
    text = result.get("text") or ""
    body = ("\n".join(header) + "\n\n" + text) if header else text
    return body[:RERANK_TEXT_LIMIT]


async def _cohere_rerank(
    query: str, results: list[dict[str, Any]], user_keys: UserAPIKeys | None
) -> list[float] | None:
    from app.services.provider_policy import enforce_provider_allowed

    key = _cohere_key(user_keys)
    if not key:
        return None
    enforce_provider_allowed("cohere", "rerank request")
    async with httpx.AsyncClient(timeout=settings.reranker_timeout) as client:
        response = await client.post(
            COHERE_RERANK_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": settings.reranker_model,
                "query": query,
                "documents": [rerank_text(r) for r in results],
                "top_n": len(results),
            },
        )
        response.raise_for_status()
        data = response.json()
    scores = [0.0] * len(results)
    seen = False
    for row in data.get("results") or []:
        index = row.get("index")
        if isinstance(index, int) and 0 <= index < len(results):
            scores[index] = float(row.get("relevance_score") or 0.0)
            seen = True
    return scores if seen else None


async def rerank(
    query: str,
    results: list[dict[str, Any]],
    top_k: int,
    user_keys: UserAPIKeys | None = None,
) -> list[dict[str, Any]]:
    """``results`` reordered by the configured reranker, cut to ``top_k``.

    Each reranked result gets ``rerank_score``. On any failure the input order
    is kept. ``similarity`` is never overwritten: it stays the measured cosine
    shown to the user.
    """
    if len(results) < 2:
        return results[:top_k]
    provider = (settings.reranker_provider or "none").lower()
    if provider == "local":
        from app.services.search import search_service

        reranked: list[dict[str, Any]] = await search_service.hybrid_searcher.reranker.rerank(
            query, results, top_k
        )
        return reranked
    if provider != "cohere":
        return results[:top_k]
    try:
        scores = await _cohere_rerank(query, results, user_keys)
    except ValueError as e:  # provider not approved, missing key
        logger.warning(f"Reranking skipped: {e}")
        return results[:top_k]
    except (httpx.HTTPError, KeyError, TypeError) as e:
        logger.warning(f"Reranking failed ({type(e).__name__}); keeping fused order")
        return results[:top_k]
    if scores is None:
        return results[:top_k]
    for r, score in zip(results, scores, strict=True):
        r["rerank_score"] = score
    # Stable sort: equal scores keep their fused order.
    ordered = sorted(results, key=lambda r: -r["rerank_score"])
    return ordered[:top_k]
