import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

import anthropic
import httpx
import openai
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.exceptions import ServiceUnavailableError, UpstreamError
from app.models.schemas import ChatRequest, ChatResponse, ConversationExportRequest, RAGSettings
from app.routers._matter_deps import accessible_matter_ids as _accessible_matter_ids
from app.routers._matter_deps import personal_matter_id as _personal_matter_id
from app.services import chat_sessions as chat_session_service
from app.services import conversation_export
from app.services.audit import AuditEventType, audit_service
from app.services.auth import TokenData
from app.services.documents import document_service
from app.services.job_queue import RETRY_POLICIES, job_manager
from app.services.permissions import require_permission
from app.services.rag import rag_service
from app.services.rag.per_file import PER_FILE_MAX_FILES, per_file_answer
from app.services.rag.routing import route_query
from app.services.rag.service import describe_llm_failure
from app.services.strategy import strategy_service
from app.services.user_keys import UserAPIKeys
from app.services.user_settings import load_user_settings
from app.utils.ip_resolution import get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


def _opt_str(value) -> str | None:
    return str(value) if value not in (None, "") else None


@router.post("/export")
async def export_conversation(
    request: ConversationExportRequest,
    current_user: TokenData = require_permission("chat.use"),
) -> Response:
    """Render the transcript the client is showing as Word, PDF or Markdown.

    The transcript is supplied by the client because every chat page keeps its
    own message shape; nothing here re-runs retrieval or touches documents.
    """
    mime, ext = conversation_export.FORMATS[request.format]
    try:
        payload = conversation_export.render(
            request.format,
            request.title,
            [m.model_dump() for m in request.messages],
        )
    except conversation_export.PdfUnavailable as e:
        raise HTTPException(status_code=501, detail=str(e))
    filename = conversation_export.safe_filename(request.title, ext)
    return Response(
        content=payload,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@dataclass
class _ChatContext:
    """Everything a chat message needs from the request, captured eagerly.

    Built before any background execution (``?async=true``) so the job never
    touches the request object or the caller's session after the 202.
    """

    user_id: str
    user_email: str | None
    user_keys: UserAPIKeys
    user_settings: RAGSettings
    client_ip: str | None
    accessible_matters: set[str]
    # Matters the caller belongs to: documents shared through them are part of
    # what chat may search and read, alongside the caller's own files.
    matter_ids: list[str] | None
    # None means "no scope — search everything the user can see". A list is an
    # explicit scope and stays one even when EMPTY (a folder with nothing
    # indexed yet): downstream code answers "nothing in that scope" rather
    # than widening the search to the rest of the knowledge base.
    document_ids: list[str] | None


async def _chat_context(
    request: ChatRequest,
    http_request: Request,
    current_user: TokenData,
    accessible_matters: set[str],
    personal_matter_id: str | None = None,
) -> _ChatContext:
    """Capture the caller's context and resolve the document scope.

    A filter's ``matter_ids`` resolve to the indexed documents of those
    matters (only matters the caller belongs to), unioned with any files and
    folders it also names. A selection that resolves to nothing stays an
    empty scope; it never widens to everything.
    """
    # Eagerly capture context before potential background execution
    user_keys = UserAPIKeys.from_request(http_request, user_id=current_user.user_id)
    user_settings = load_user_settings(current_user.user_id)
    client_ip = get_client_ip(http_request)
    user_id = current_user.user_id
    matter_ids = sorted(accessible_matters) or None

    # Pre-resolve document IDs (requires await, must happen before returning 202).
    document_ids = None
    doc_filter = request.document_filter
    if doc_filter and request.include_documents and not doc_filter.search_all:
        from_matters: list[str] = []
        if doc_filter.matter_ids:
            from_matters = document_service.document_ids_in_matters(
                doc_filter.matter_ids, user_id, accessible_matters, personal_matter_id
            )
        if doc_filter.matter_ids and not (doc_filter.document_ids or doc_filter.folder_paths):
            document_ids = from_matters
        else:
            picked = await document_service.get_documents_by_filter(
                user_id=user_id,
                document_ids=doc_filter.document_ids,
                folder_paths=doc_filter.folder_paths,
                sources=doc_filter.sources,
                doc_types=doc_filter.doc_types,
                include_subfolders=doc_filter.include_subfolders,
                accessible_matter_ids=accessible_matters,
            )
            document_ids = list(dict.fromkeys([*from_matters, *picked]))

    return _ChatContext(
        user_id=user_id,
        user_email=current_user.email,
        user_keys=user_keys,
        user_settings=user_settings,
        client_ip=client_ip,
        accessible_matters=accessible_matters,
        matter_ids=matter_ids,
        document_ids=document_ids,
    )


def _brief_citation(c: dict, idx: int) -> dict:
    # Map a strategy-brief citation into the full Citation model shape.
    conf = float(c.get("confidence") or 0)
    return {
        "id": c.get("id") or f"strat-{idx}",
        "source": c.get("source") or f"Source {idx + 1}",
        "type": c.get("type") or "document",
        "confidence": conf,
        "similarity": conf / 100 if conf > 1 else conf,
        "relevance_rank": idx + 1,
        "chunk_index": idx,
        "token_count": 0,
        "passage": c.get("passage") or "",
        # Strategy authorities carry the judge's one-line explanation of
        # how a lawyer would use them — surface it as the case summary.
        "case_summary": c.get("explanation"),
        "reasoning": [],
        "logic": {
            "query_intent": "",
            "matching_criteria": "Strategy brief coverage retrieval",
            "application": "",
        },
        "document_id": c.get("document_id"),
        "url": c.get("url"),
        # Verification state and CourtListener ids must survive the
        # mapping: they drive the Verified badge and "Verify in Source
        # Opinion" in the UI.
        "opinion_id": _opt_str(c.get("opinion_id") or c.get("opinionId")),
        "cluster_id": _opt_str(c.get("cluster_id")),
        "verified": c.get("verified"),
        "was_cited_by_ai": True,
    }


async def _research_answer(request: ChatRequest, case_law_query, ctx: _ChatContext) -> dict:
    """Pure legal-research lookup ("find me case law on DWI"): outside authority only.

    The user's documents are irrelevant here and must not be dragged into the
    answer. Research questions get the deep engine automatically — candidate
    opinions are read in full and only quote-verified authorities are cited.
    """
    return await rag_service.generate_response(
        query=request.query,
        mode=request.mode,
        top_k=ctx.user_settings.top_k,
        similarity_threshold=ctx.user_settings.similarity_threshold,
        include_documents=False,
        include_case_law=(
            request.include_case_law if request.include_case_law is not None else True
        ),
        case_law_limit=request.case_law_limit,
        case_law_query=case_law_query,
        deep_case_law=True,
        user_keys=ctx.user_keys,
        document_ids=None,
        user_id=ctx.user_id,
        rag_settings=ctx.user_settings,
        matter_ids=ctx.matter_ids,
    )


async def _strategy_answer(
    request: ChatRequest, include_case_law: bool, ctx: _ChatContext
) -> tuple[dict | None, dict | None]:
    """Answer with a strategy brief; ``(None, None)`` when the brief fails.

    Returns ``(strategy_payload, result)``.
    """
    try:
        brief_document_ids = None
        if request.document_filter and not request.document_filter.search_all:
            # The scope as resolved for this request: selected matters, files
            # and folders together, including documents shared via a matter.
            brief_document_ids = ctx.document_ids
        brief = await strategy_service.generate_brief(
            question=request.query,
            document_ids=brief_document_ids,
            include_case_law=include_case_law,
            user_id=ctx.user_id,
            user_keys=ctx.user_keys,
            accessible_matter_ids=ctx.accessible_matters,
        )
        result = {
            "content": brief.get("position") or "Strategy brief generated.",
            "citations": [
                _brief_citation(c, i) for i, c in enumerate(brief.get("citations") or [])
            ],
            "stats": {
                "docs_searched": brief.get("scope", {}).get("documents_considered", 0),
                "chunks_retrieved": len(brief.get("citations") or []),
                "processing_time": "",
                "case_law_searched": 0,
                "case_law_included": sum(
                    1 for c in (brief.get("citations") or []) if c.get("type") == "case_law"
                ),
                "query_intent": "strategy",
            },
        }
        return brief, result
    except Exception as e:  # strategy routing must never kill chat
        logger.warning(f"Strategy routing failed, answering normally: {e}")
        return None, None


async def _per_file_answer(request: ChatRequest, ctx: _ChatContext) -> dict | None:
    """Per-file map-reduce over the scoped files; None when it fails."""
    try:
        return await per_file_answer(
            query=request.query,
            document_ids=ctx.document_ids,
            user_id=ctx.user_id,
            user_keys=ctx.user_keys,
            model=ctx.user_settings.llm_model,
            accessible_matter_ids=ctx.accessible_matters,
        )
    except Exception as e:  # per-file must never kill chat
        logger.warning(f"Per-file answering failed, answering normally: {e}")
        return None


async def _pooled_answer(
    request: ChatRequest, include_case_law: bool, case_law_query, ctx: _ChatContext
) -> dict:
    """The ordinary pooled RAG answer over the scoped documents and case law."""
    return await rag_service.generate_response(
        query=request.query,
        mode=request.mode,
        top_k=ctx.user_settings.top_k,
        similarity_threshold=ctx.user_settings.similarity_threshold,
        include_documents=request.include_documents,
        include_case_law=include_case_law,
        case_law_limit=request.case_law_limit,
        case_law_query=case_law_query,
        user_keys=ctx.user_keys,
        document_ids=ctx.document_ids,
        user_id=ctx.user_id,
        rag_settings=ctx.user_settings,
        matter_ids=ctx.matter_ids,
    )


async def _retrieve_answer(request: ChatRequest, ctx: _ChatContext) -> tuple[dict, dict | None]:
    """Route the message and produce its answer.

    Returns ``(result, strategy_payload)``.
    """
    # Zero-controls composer: decide per message whether this is a grounded
    # question or a strategy ask, and whether authority would help. An
    # explicit include_case_law from the client always wins. The router
    # also distills the case-law SEARCH TERM — the raw message ("find me
    # case law for a dwi") is never a usable keyword query.
    intent, wants_authority, case_law_query, per_file = await route_query(
        request.query, ctx.user_keys, model=ctx.user_settings.llm_model
    )
    include_case_law = (
        request.include_case_law if request.include_case_law is not None else wants_authority
    )

    strategy_payload = None
    result = None
    if intent == "research":
        result = await _research_answer(request, case_law_query, ctx)
    # The strategy brief reads the documents by design — never route to it
    # when the client explicitly asked for a documents-off answer.
    elif intent == "strategy" and request.include_documents:
        strategy_payload, result = await _strategy_answer(request, include_case_law, ctx)

    # Per-file map-reduce: the question is about the scoped files
    # individually/comparatively and the scope is a small multi-file set —
    # give every file its own context window, then synthesize. Any failure
    # falls back to the pooled RAG answer below.
    if (
        result is None
        and intent == "ask"
        and per_file
        and request.include_documents
        and ctx.document_ids
        and 2 <= len(ctx.document_ids) <= PER_FILE_MAX_FILES
    ):
        result = await _per_file_answer(request, ctx)

    if result is None:
        result = await _pooled_answer(request, include_case_law, case_law_query, ctx)

    return result, strategy_payload


async def _audit_chat_query(
    request: ChatRequest, response_id: str, result: dict, ctx: _ChatContext
) -> None:
    await audit_service.log_event(
        event_type=AuditEventType.CHAT_QUERY,
        user_id=ctx.user_id,
        user_email=ctx.user_email,
        resource_type="chat",
        resource_id=response_id,
        ip_address=ctx.client_ip,
        details={
            "mode": request.mode.value if request.mode else "research",
            "include_documents": request.include_documents,
            "include_case_law": request.include_case_law,
            "query_length": len(request.query),
            "citations_count": len(result["citations"]),
        },
    )


def _response_payload(
    request: ChatRequest,
    response_id: str,
    result: dict,
    strategy_payload: dict | None,
) -> dict:
    return ChatResponse(
        id=response_id,
        content=result["content"],
        citations=result["citations"] if request.include_citations else [],
        stats=result["stats"],
        mode=request.mode,
        timestamp=datetime.now(UTC),
        strategy=strategy_payload,
    ).model_dump(mode="json")


async def _persist_exchange(
    request: ChatRequest, response_payload: dict, ctx: _ChatContext
) -> str | None:
    """Persist the exchange to a chat session so conversations survive reloads.

    Best-effort: storage problems must never fail the response. Returns the
    session id, or None when persistence failed.
    """
    session_id = request.session_id
    try:
        if session_id is None:
            created = await chat_session_service.create_session(
                user_id=ctx.user_id, title=request.query.strip()[:60]
            )
            session_id = created["id"]
        await chat_session_service.append_message(
            session_id, ctx.user_id, role="user", content=request.query
        )
        await chat_session_service.append_message(
            session_id,
            ctx.user_id,
            role="assistant",
            content=response_payload["content"],
            citations=response_payload["citations"],
            strategy=response_payload.get("strategy"),
            stats=response_payload["stats"],
        )
    except (
        LookupError,
        ValueError,
        KeyError,
        SQLAlchemyError,
        ConnectionError,
        TimeoutError,
        OSError,
        RuntimeError,
    ) as persist_err:
        logger.warning(f"Chat session persistence failed (non-fatal): {persist_err}")
        session_id = None
    return session_id


async def _answer_chat(request: ChatRequest, ctx: _ChatContext) -> dict:
    """One chat message end to end: route, answer, audit, persist, assemble."""
    # Query text is client-confidential: log its size, never its content.
    logger.debug(f"Chat query from user {ctx.user_id} ({len(request.query)} chars)")

    result, strategy_payload = await _retrieve_answer(request, ctx)

    response_id = str(uuid.uuid4())
    await _audit_chat_query(request, response_id, result, ctx)
    response_payload = _response_payload(request, response_id, result, strategy_payload)
    response_payload["session_id"] = await _persist_exchange(request, response_payload, ctx)
    return response_payload


def _raise_chat_error(e: Exception, user_id: str) -> NoReturn:
    """Map a failure that escaped the per-stage handling to the client response."""
    if isinstance(e, openai.OpenAIError | anthropic.AnthropicError | httpx.HTTPError):
        # A provider/network failure that escaped the per-stage handling: say
        # what happened instead of a bare 500 (the upstream body is only logged).
        logger.error(f"Chat provider error for user {user_id}: {type(e).__name__}: {e}")
        raise UpstreamError(describe_llm_failure(e))
    # Outside DEBUG log only the exception type: a parse or validation
    # error can quote the input it rejected, and chat input is privileged
    # text that must not land in long-retained logs (see error_handlers.py).
    if settings.debug:
        logger.error(f"Chat error for user {user_id}: {e}", exc_info=True)
    else:
        logger.error(f"Chat error for user {user_id}: {type(e).__name__}")
    raise HTTPException(
        status_code=500,
        detail="An error occurred processing your request. Please try again.",
    )


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    http_request: Request,
    current_user: TokenData = require_permission("chat.use"),
    async_mode: bool = Query(False, alias="async"),
    accessible_matters: set[str] = Depends(_accessible_matter_ids),
    personal_matter: str | None = Depends(_personal_matter_id),
) -> ChatResponse:
    """Send a message and get a RAG-powered response with case law integration. Requires authentication."""
    if rag_service is None:
        raise ServiceUnavailableError(
            "RAG service is temporarily unavailable. Please try again later.",
        )

    ctx = await _chat_context(
        request, http_request, current_user, accessible_matters, personal_matter
    )

    async def do_work():
        return await _answer_chat(request, ctx)

    if async_mode:
        job_id = await job_manager.submit(
            do_work,
            user_id=ctx.user_id,
            endpoint="/chat",
            retry_policy=RETRY_POLICIES["ai_analysis"],
        )
        return JSONResponse(
            status_code=202,
            content={"job_id": job_id, "poll_url": f"/api/v1/jobs/{job_id}"},
        )

    # Synchronous mode — unchanged behaviour
    try:
        result_data = await do_work()
        return result_data
    except (openai.OpenAIError, anthropic.AnthropicError, httpx.HTTPError) as e:
        _raise_chat_error(e, ctx.user_id)
    except (ValueError, KeyError, ConnectionError, TimeoutError, OSError, RuntimeError) as e:
        _raise_chat_error(e, ctx.user_id)
