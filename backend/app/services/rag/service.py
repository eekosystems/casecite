"""RAG Service - thin orchestrator that delegates to focused modules."""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

from anthropic import AsyncAnthropic

from app.config import settings
from app.models.schemas import (
    AnalysisMode,
    ChatStats,
    Citation,
    QueryIntent,
    RAGSettings,
)
from app.services.case_law_research import research_authorities
from app.services.chunking import (
    CHUNKER_VERSION,
    chunk_document,
    document_context,
    embedding_text,
)
from app.services.embeddings import embedding_service
from app.services.rag.case_law_guard import redact_unverified_case_law
from app.services.rag.citation_reasoning import enrich_citations_with_reasoning
from app.services.rag.citations import (
    build_case_law_citations,
    build_document_citations,
    extract_cited_cases,
)
from app.services.rag.claim_grounding import ground_answer
from app.services.rag.enhancements import (
    annotate_source_locations,
    compress_context_results,
    expand_query,
    merge_search_results,
    verify_document_citations,
)
from app.services.rag.generation import (
    build_context_with_case_law,
    call_llm,
    get_model_max_tokens,
)
from app.services.rag.intent import detect_query_intent
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE
from app.services.rag.prompts import (
    DOCUMENT_READING_RULES,
    get_default_grounding_rules,
    get_system_prompt,
)
from app.services.rag.search import (
    SearchUnavailableError,
    case_law_search_filters,
    search_case_law,
    search_documents,
)
from app.services.search import search_service
from app.services.vectordb import get_vector_db

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from app.services.user_keys import UserAPIKeys

logger = logging.getLogger(__name__)

# --- Prompt injection guard ---
# Only phrasings that are unambiguous injection attempts. Legal text is full of
# "act as attorney-in-fact", "the system: ...", "stop work ... begin" and the
# like, so anything a contract or brief might legitimately say stays out of
# this list. Third-party document text is not filtered at all \u2014 it is
# delimited instead (see app.services.rag.prompt_safety).
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|above|prior|earlier|system)"
    r"\s+(instructions?|prompts?|rules?|directions?|context))"
    r"|(disregard\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|above|prior|earlier|system)"
    r"\s+(instructions?|prompts?|rules?|directions?|context))"
    r"|(forget\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|above|prior|earlier|system)"
    r"\s+(instructions?|prompts?|rules?|context))"
    r"|(override\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|above|prior|earlier|system)"
    r"\s+(instructions?|prompts?|rules?))"
    r"|(you\s+are\s+now\s+(a|an|in)\s)"
    r"|(new\s+instructions?\s*:)"
    r"|(```\s*(system|assistant))"
    r"|(\<\/?system\>)"
    r"|(\<\/?assistant\>)"
    r"|(\x00)"
    r"|([\u200b-\u200f\u2028-\u202f\u2060\ufeff])",
    re.IGNORECASE,
)


def _sanitize_prompt_input(text: str, max_length: int = 100_000) -> str:
    """Sanitize user-provided text before interpolation into LLM prompts.

    Applies to the USER'S query only. Retrieved chunks and other third-party
    text are wrapped with ``untrusted_block`` instead of being rewritten, so
    quote verification against the original still works.
    """
    if not text:
        return text
    text = text[:max_length]
    text = _INJECTION_PATTERNS.sub("[FILTERED]", text)
    text = re.sub(r"={3,}", "==", text)
    return text


def describe_llm_failure(error: Exception) -> str:
    """One user-readable sentence for a failed LLM call (no upstream payloads)."""
    if isinstance(error, ValueError):
        # Raised by call_llm / the provider allowlist with a message written
        # for the user (no key configured, provider not approved, ...).
        return str(error)
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    name = type(error).__name__
    if status in (401, 403) or "Authentication" in name or "PermissionDenied" in name:
        return (
            "The AI provider rejected the API key. Check the key in Settings "
            "(or the server's provider key if you run the instance)."
        )
    if status == 429 or "RateLimit" in name:
        return (
            "The AI provider is rate-limiting requests or the account is out of "
            "quota. Wait a moment and try again."
        )
    if isinstance(error, TimeoutError) or "Timeout" in name:
        return "The AI provider took too long to respond. Try again."
    if status == 404 or "NotFound" in name:
        return (
            "The AI provider does not recognise the selected model. Choose a "
            "different model in Settings."
        )
    if isinstance(status, int) and status >= 500:
        return f"The AI provider reported an internal error (HTTP {status}). Try again shortly."
    return f"The request to the AI provider failed ({name}). Try again."


class VectorWriteError(RuntimeError):
    """Replacing a document's vectors failed at the store.

    ``vectors_removed`` says whether the old vectors are already gone (the
    document is no longer searchable) or the store was left as it was.
    """

    def __init__(self, message: str, vectors_removed: bool):
        super().__init__(message)
        self.vectors_removed = vectors_removed


class RAGService:
    def __init__(self, vector_db=None):
        """``vector_db`` overrides the configured store (the eval harness indexes
        into an isolated one); by default the process-wide store is used."""
        self.vector_db = vector_db if vector_db is not None else get_vector_db()
        # Both clients carry settings.api_timeout and bounded retries; the
        # OpenAI one goes through the shared factory so OPENAI_BASE_URL /
        # the admin endpoint override apply here too.
        from app.services.llm_clients import SDK_MAX_RETRIES, make_openai

        self.openai = (
            make_openai(settings.openai_api_key, async_=True) if settings.openai_api_key else None
        )
        self.anthropic = (
            AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.api_timeout,
                max_retries=SDK_MAX_RETRIES,
            )
            if settings.anthropic_api_key
            else None
        )

    async def _build_chunk_documents(
        self,
        document_id: str,
        text: str,
        filename: str,
        source: str = "local",
        doc_type: str = "document",
        metadata: dict[str, Any] = None,
        user_keys: Optional["UserAPIKeys"] = None,
        folder_path: str = None,
        user_id: str = "",
        matter_id: str | None = None,
        page_starts: list[tuple[int, int]] | None = None,
    ) -> list[dict[str, Any]]:
        """Chunk and embed a document; returns the records to store (may be empty).

        This is the step that can fail on a provider error. It writes nothing,
        so callers can finish it before touching what is already stored.

        With structured chunking (the default) each chunk records its section
        path, character span and pages (``page_starts`` from the extractor),
        and is embedded together with a one-line document context (title and
        parties) so a clause is findable by the agreement it belongs to; the
        stored chunk text stays verbatim.
        """
        if self.vector_db is None:
            raise RuntimeError("Vector database not available. Check DB configuration and logs.")
        structured = settings.chunking_strategy == "structured"
        if structured:
            chunks = chunk_document(
                text,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                count_tokens=embedding_service.count_tokens,
                page_starts=page_starts,
            )
        else:
            chunks = embedding_service.chunk_text(text)
        if not chunks:
            return []

        context = document_context(text, filename) if structured else ""
        if structured and settings.contextual_chunk_headers:
            chunk_texts = [embedding_text(c, filename, context) for c in chunks]
        else:
            chunk_texts = [c["text"] for c in chunks]
        embeddings = await embedding_service.embed_texts(chunk_texts, user_keys=user_keys)

        if not folder_path and metadata:
            folder_path = (
                metadata.get("folder_path") or metadata.get("parent_folder") or f"/{source}/"
            )
        elif not folder_path:
            folder_path = f"/{source}/"

        # Sanitize folder_path to prevent directory traversal
        import posixpath

        folder_path = posixpath.normpath(folder_path)
        if ".." in folder_path.split("/"):
            logger.warning(f"Blocked directory traversal in folder_path: {folder_path}")
            folder_path = f"/{source}/"

        documents = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=False)):
            chunk_doc = {
                "id": f"{document_id}_{i}",
                "embedding": embedding,
                "text": chunk["text"],
                "document_id": document_id,
                "chunk_index": chunk["chunk_index"],
                "token_count": chunk["token_count"],
                "filename": filename,
                "source": source,
                "doc_type": doc_type,
                "folder_path": folder_path,
                "created_at": datetime.now(UTC).isoformat(),
                **(metadata or {}),
                # Trusted scope fields last so caller-supplied metadata can never
                # override the owner/matter that enforce tenant isolation.
                "user_id": user_id,
            }
            # Only tag matter_id when set — the vector store rejects None-valued
            # metadata, and an untagged chunk stays owner-scoped (by user_id).
            # Assigned after the metadata spread for the same reason as user_id:
            # a connector's own "matter_id" metadata is not an access-control tag.
            chunk_doc.pop("matter_id", None)
            if matter_id:
                chunk_doc["matter_id"] = matter_id
            # Where the chunk sits — set after the metadata spread so caller
            # metadata cannot misstate a passage's location.
            chunk_doc["chunker"] = CHUNKER_VERSION if structured else "fixed-v1"
            for key in ("section", "char_start", "char_end", "page_start", "page_end"):
                chunk_doc.pop(key, None)
                if chunk.get(key) not in (None, ""):
                    chunk_doc[key] = chunk[key]
            chunk_doc.pop("doc_context", None)
            if context:
                chunk_doc["doc_context"] = context
            documents.append(chunk_doc)
        return documents

    async def index_document(
        self,
        document_id: str,
        text: str,
        filename: str,
        source: str = "local",
        doc_type: str = "document",
        metadata: dict[str, Any] = None,
        user_keys: Optional["UserAPIKeys"] = None,
        folder_path: str = None,
        user_id: str = "",
        matter_id: str | None = None,
        page_starts: list[tuple[int, int]] | None = None,
    ) -> int:
        """Index a document by chunking and embedding it."""
        documents = await self._build_chunk_documents(
            document_id=document_id,
            text=text,
            filename=filename,
            source=source,
            doc_type=doc_type,
            metadata=metadata,
            user_keys=user_keys,
            folder_path=folder_path,
            user_id=user_id,
            matter_id=matter_id,
            page_starts=page_starts,
        )
        if not documents:
            return 0
        await self.vector_db.add_documents(documents)
        return len(documents)

    async def replace_document(self, document_id: str, **index_kwargs) -> int:
        """Re-index a document, replacing its stored chunks.

        Takes the same arguments as :meth:`index_document`. The new chunks are
        embedded FIRST; the existing vectors are deleted only once that has
        succeeded, so a provider failure leaves the document searchable as it
        was. Raises if embedding fails (nothing changed) or if the store write
        fails after the old vectors were removed (the document then has no
        vectors and must be marked failed by the caller).
        """
        documents = await self._build_chunk_documents(document_id=document_id, **index_kwargs)
        if not documents:
            raise ValueError("no text to index")
        if not await self.vector_db.delete_by_document(document_id):
            raise VectorWriteError(
                "could not remove the existing vectors; the document was left unchanged",
                vectors_removed=False,
            )
        try:
            await self.vector_db.add_documents(documents)
        except Exception as e:  # store errors share no base class
            raise VectorWriteError(
                f"storing the re-embedded chunks failed ({type(e).__name__}); "
                "the document currently has no vectors",
                vectors_removed=True,
            ) from e
        return len(documents)

    async def search(
        self,
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
    ) -> list[dict[str, Any]]:
        """Search for relevant documents. Delegates to search module."""
        return await search_documents(
            vector_db=self.vector_db,
            query=query,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            filter=filter,
            use_hybrid=use_hybrid,
            use_reranking=use_reranking,
            user_keys=user_keys,
            document_ids=document_ids,
            user_id=user_id,
            matter_ids=matter_ids,
        )

    async def generate_response(
        self,
        query: str,
        mode: AnalysisMode = AnalysisMode.RESEARCH,
        model: str = None,
        top_k: int = None,
        similarity_threshold: float = None,
        include_documents: bool = True,
        include_case_law: bool = True,
        case_law_limit: int = 5,
        case_law_query: str | None = None,
        deep_case_law: bool = False,
        user_keys: Optional["UserAPIKeys"] = None,
        document_ids: list[str] | None = None,
        user_id: str = "",
        rag_settings: Any | None = None,
        matter_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Generate a RAG response with citations from documents and case law.

        ``matter_ids`` are the shared matters the caller belongs to: retrieval
        then spans the caller's own documents OR documents filed under those
        matters (see ``tenant_scope``), so matter-shared files are searchable.
        """
        start_time = time.time()

        # Resolve effective per-user settings up front so the model choice and
        # retrieval toggles apply to every code path below (including no-results).
        effective_settings = rag_settings if rag_settings else RAGSettings()
        model = model or effective_settings.llm_model or settings.openai_chat_model

        openai_client, anthropic_client, gemini_model = self._resolve_llm_clients(user_keys, model)

        # A scope (selected files / folder) that resolved to no indexed
        # documents is answered as exactly that. It must never widen to the
        # whole knowledge base: the user asked about Client A's folder, and
        # an answer drawn from Client B's files would be wrong and unseen.
        if include_documents and document_ids is not None and not document_ids:
            return _empty_response(
                "There are no indexed documents in the scope you selected, so "
                "there was nothing to search. The folder or files may be empty, "
                "or still being indexed — check their status in the knowledge "
                "base, or widen the scope and ask again.",
                start_time,
            )

        # Search for relevant documents. Infrastructure failures surface as an
        # honest "search is down" answer — never as "no relevant documents".
        try:
            search_results = await self._retrieve_documents(
                query=query,
                include_documents=include_documents,
                document_ids=document_ids,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                effective_settings=effective_settings,
                user_keys=user_keys,
                user_id=user_id,
                matter_ids=matter_ids,
                openai_client=openai_client,
                anthropic_client=anthropic_client,
                gemini_model=gemini_model,
                model=model,
            )
        except SearchUnavailableError as e:
            logger.error(f"[RAG] Search unavailable for user {user_id}: {e}")
            return _empty_response(
                f"⚠ I couldn't search your documents: {e}\n\n"
                "No answer was attempted — this is an infrastructure problem, "
                "not a gap in your documents.",
                start_time,
            )

        case_law_results = await _search_case_law_within_budget(
            query=query,
            include_case_law=include_case_law,
            case_law_query=case_law_query,
            case_law_limit=case_law_limit,
            deep_case_law=deep_case_law,
            openai_client=openai_client,
            user_keys=user_keys,
            model=model,
        )

        threshold_response = await self._explain_threshold_misses(
            query=query,
            search_results=search_results,
            case_law_results=case_law_results,
            include_documents=include_documents,
            document_ids=document_ids,
            similarity_threshold=similarity_threshold,
            user_keys=user_keys,
            user_id=user_id,
            matter_ids=matter_ids,
            start_time=start_time,
        )
        if threshold_response is not None:
            return threshold_response

        # If no RAG sources requested or found, generate direct LLM response
        if not search_results and not case_law_results:
            return await self._handle_no_results(
                query,
                mode,
                model,
                include_documents,
                include_case_law,
                openai_client,
                anthropic_client,
                gemini_model,
                start_time,
                effective_settings,
                scoped=bool(document_ids),
            )

        return await self._answer_with_sources(
            query=query,
            mode=mode,
            model=model,
            search_results=search_results,
            case_law_results=case_law_results,
            include_documents=include_documents,
            include_case_law=include_case_law,
            document_ids=document_ids,
            user_id=user_id,
            user_keys=user_keys,
            effective_settings=effective_settings,
            openai_client=openai_client,
            anthropic_client=anthropic_client,
            gemini_model=gemini_model,
            start_time=start_time,
            matter_ids=matter_ids,
        )

    async def _answer_with_sources(
        self,
        *,
        query: str,
        mode: AnalysisMode,
        model: str,
        search_results: list[dict[str, Any]],
        case_law_results: list[dict[str, Any]],
        include_documents: bool,
        include_case_law: bool,
        document_ids: list[str] | None,
        user_id: str,
        user_keys: Optional["UserAPIKeys"],
        effective_settings: RAGSettings,
        openai_client: "AsyncOpenAI | None",
        anthropic_client: AsyncAnthropic | None,
        gemini_model: Any,
        start_time: float,
        matter_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Generate the grounded answer from retrieved sources and assemble its citations and stats."""
        system_prompt, user_prompt, query_intent = self._build_prompts(
            query, mode, search_results, case_law_results, effective_settings
        )

        # Generate LLM response (model already resolved from user settings
        # above). The provider allowlist is enforced inside call_llm, against
        # the client that actually carries the request.
        max_tokens = _resolve_max_tokens(model, query_intent, effective_settings)

        try:
            content = await call_llm(
                openai_client,
                anthropic_client,
                gemini_model,
                model,
                system_prompt,
                user_prompt,
                max_tokens,
                effective_settings.temperature,
            )
        except Exception as e:  # provider SDK errors share no builtin base class
            return self._llm_failure_response(e, start_time)

        content = _redact_unverified_references(content, case_law_results, search_results)

        citations, cited_cases, grounding_notice = await _build_answer_citations(
            content=content,
            query=query,
            mode=mode,
            search_results=search_results,
            case_law_results=case_law_results,
            include_documents=include_documents,
            effective_settings=effective_settings,
            user_id=user_id,
            user_keys=user_keys,
            model=model,
            matter_ids=matter_ids,
        )
        if grounding_notice:
            # Statements the source check could not confirm are shown with the
            # answer, not only inside citation details a reader may never open.
            content = f"{content.rstrip()}\n\n{grounding_notice}"

        processing_time = time.time() - start_time

        docs_searched = await _count_docs_searched(
            include_documents, document_ids, user_id, search_results
        )

        return {
            "content": content,
            "citations": citations,
            "stats": ChatStats(
                docs_searched=docs_searched,
                chunks_retrieved=len(search_results),
                processing_time=f"{processing_time:.2f}s",
                case_law_searched=len(case_law_results) if include_case_law else 0,
                case_law_included=len(cited_cases),
                query_intent=query_intent.value,
            ),
        }

    def _build_prompts(
        self,
        query: str,
        mode: AnalysisMode,
        search_results: list[dict[str, Any]],
        case_law_results: list[dict[str, Any]],
        effective_settings: RAGSettings,
    ) -> tuple[str, str, QueryIntent]:
        """Build the system and user prompts over the retrieved context; returns them with the intent."""
        # Build context from both sources. Context compression trims each chunk to
        # its most query-relevant sentences so more documents fit the token budget;
        # citations below still use the full, uncompressed passages.
        context_results = (
            compress_context_results(search_results, query)
            if effective_settings.context_compression
            else search_results
        )
        context = build_context_with_case_law(context_results, case_law_results)
        self._log_debug_context(context_results, case_law_results, context)

        query_intent = _resolve_query_intent(mode, query)

        # Get system prompt and build user prompt. The context below carries
        # third-party text in delimited blocks; the rule (once per prompt)
        # tells the model those blocks are data, not instructions.
        system_prompt = (
            get_system_prompt(mode, query_intent=query_intent, rag_settings=effective_settings)
            + "\n\n"
            + UNTRUSTED_CONTENT_RULE
        )
        user_prompt = self._build_user_prompt(
            query, context, query_intent, case_law_results, effective_settings
        )
        return system_prompt, user_prompt, query_intent

    def _resolve_llm_clients(
        self, user_keys: Optional["UserAPIKeys"], model: str
    ) -> tuple["AsyncOpenAI | None", AsyncAnthropic | None, Any]:
        """Pick the LLM clients for this request: user keys (BYOK) first, server keys as fallback."""
        # Get LLM clients - prefer user keys (BYOK), fall back to server keys
        openai_client = None
        anthropic_client = None
        gemini_model = None

        if user_keys:
            openai_client = user_keys.get_async_openai_client()
            anthropic_client = user_keys.get_async_anthropic_client()
            if user_keys.google:
                # The Gemini model the user selected, not a fixed default.
                gemini_model = user_keys.get_google_model(
                    model if "gemini" in model.lower() else None
                )

        if not openai_client:
            openai_client = self.openai
        if not anthropic_client:
            anthropic_client = self.anthropic
        return openai_client, anthropic_client, gemini_model

    async def _explain_threshold_misses(
        self,
        *,
        query: str,
        search_results: list[dict[str, Any]],
        case_law_results: list[dict[str, Any]],
        include_documents: bool,
        document_ids: list[str] | None,
        similarity_threshold: float | None,
        user_keys: Optional["UserAPIKeys"],
        user_id: str,
        matter_ids: list[str] | None,
        start_time: float,
    ) -> dict[str, Any] | None:
        """Probe below the similarity floor; return an explanatory answer when matches exist there."""
        if search_results or case_law_results or not include_documents or document_ids is not None:
            return None
        # Threshold transparency: if document search came back empty, probe
        # once WITHOUT the similarity floor. If matches exist below the
        # threshold, say so with numbers — "no relevant documents" while the
        # threshold silently discards real matches is indistinguishable from
        # data loss to the user.
        effective_threshold = similarity_threshold or settings.similarity_threshold
        try:
            probe = await self.search(
                query,
                top_k=3,
                similarity_threshold=0.001,
                use_hybrid=False,
                use_reranking=False,
                user_keys=user_keys,
                user_id=user_id,
                matter_ids=matter_ids,
            )
        except SearchUnavailableError:
            probe = []
        if not probe:
            return None
        best = max(r.get("similarity", 0.0) for r in probe)
        best_file = (probe[0].get("metadata") or {}).get("filename", "a document")
        return _empty_response(
            f"⚠ Your documents ARE indexed, but nothing cleared your "
            f"similarity threshold. The closest match — in "
            f"“{best_file}” — scored {best:.2f}, and your threshold "
            f"of {effective_threshold:.2f} discarded it. Lower the "
            f"Similarity Threshold in Settings → Retrieval "
            f"(0.25 recommended) and ask again.",
            start_time,
        )

    async def _retrieve_documents(
        self,
        *,
        query,
        include_documents,
        document_ids,
        top_k,
        similarity_threshold,
        effective_settings,
        user_keys,
        user_id,
        openai_client,
        anthropic_client,
        gemini_model,
        model,
        matter_ids=None,
    ) -> list[dict[str, Any]]:
        """Retrieve the passages most relevant to the query.

        ``document_ids`` None searches everything the user can see; a list
        restricts the SAME semantic search to those documents (spread across
        the selected files, with real similarity scores) — selecting files
        narrows where we look, it does not replace looking. An empty list
        retrieves nothing.

        Raises SearchUnavailableError when the search infrastructure fails —
        the caller turns that into an honest error answer instead of "no
        relevant documents".
        """
        if not include_documents:
            return []
        if document_ids is not None and not document_ids:
            return []
        if document_ids:
            logger.info(f"[RAG] Scoped retrieval over {len(document_ids)} selected document(s)")

        # Query expansion: search the original query plus LLM-generated
        # paraphrases, then merge by best-similarity per chunk (multi-query).
        queries = [query]
        if effective_settings.query_expansion:
            queries = await expand_query(
                query, openai_client, anthropic_client, gemini_model, model
            )
            logger.info(f"[RAG] Query expansion -> {len(queries)} queries")

        result_lists = []
        for q in queries:
            result_lists.append(
                await self.search(
                    q,
                    top_k=top_k,
                    similarity_threshold=similarity_threshold,
                    use_hybrid=effective_settings.hybrid_search,
                    use_reranking=effective_settings.enable_reranking,
                    user_keys=user_keys,
                    document_ids=document_ids,
                    user_id=user_id,
                    matter_ids=matter_ids,
                )
            )
        return (
            merge_search_results(result_lists, top_k or settings.top_k)
            if len(result_lists) > 1
            else result_lists[0]
        )

    async def _handle_no_results(
        self,
        query,
        mode,
        model,
        include_documents,
        include_case_law,
        openai_client,
        anthropic_client,
        gemini_model,
        start_time,
        effective_settings=None,
        scoped: bool = False,
    ) -> dict[str, Any]:
        """Handle case when no search results are found."""
        effective_settings = effective_settings or RAGSettings()
        if not include_documents and not include_case_law:
            # RAG was disabled intentionally, just use LLM directly
            drafting_keywords = [
                "draft",
                "generate a",
                "write a",
                "motion to",
                "complaint",
                "document drafter",
                "begin the document",
                "ready-to-file",
            ]
            is_drafting = any(kw in query.lower() for kw in drafting_keywords)

            system_prompt = get_system_prompt(mode, is_drafting=is_drafting)
            model = model or effective_settings.llm_model or settings.openai_chat_model
            # Respect the user's max-tokens setting, capped by the model ceiling.
            max_tokens = min(effective_settings.max_tokens, get_model_max_tokens(model))
            # Drafting needs a little more creativity; treat 0.3 as a floor there.
            temperature = (
                max(effective_settings.temperature, 0.3)
                if is_drafting
                else effective_settings.temperature
            )

            try:
                content = await call_llm(
                    openai_client,
                    anthropic_client,
                    gemini_model,
                    model,
                    system_prompt,
                    query,
                    max_tokens,
                    temperature,
                )
            except Exception as e:  # provider SDK errors share no builtin base class
                return self._llm_failure_response(e, start_time)

            # Direct-LLM path has no retrieved sources at all, so no case
            # reference the model produces here can be verified — remove all.
            content, removed_refs = redact_unverified_case_law(content)
            if removed_refs:
                logger.warning(
                    f"[CaseLawGuard] Removed {len(removed_refs)} unverified case "
                    f"reference(s) from direct answer: {removed_refs}"
                )

            return {
                "content": content,
                "citations": [],
                "stats": ChatStats(
                    docs_searched=0,
                    chunks_retrieved=0,
                    processing_time=f"{time.time() - start_time:.2f}s",
                    case_law_searched=0,
                    case_law_included=0,
                ),
            }

        # RAG was enabled but nothing was found. An EMPTY index and "no match"
        # are different truths — never report an empty library as a bad query.
        content = (
            "I couldn't find any relevant documents in the knowledge base or "
            "case law for your query. Please try rephrasing or ensure relevant "
            "documents have been indexed."
        )
        if scoped:
            content = (
                "I searched the files you selected and found no passages to answer "
                "from. They may have no extractable text (for example a scanned PDF "
                "without OCR) — check their status in the knowledge base."
            )
        elif include_documents and self.vector_db is not None:
            try:
                db_stats = await self.vector_db.get_stats()
                if db_stats.get("total_chunks", 0) == 0:
                    content = (
                        "⚠ Your document search index is empty — there is nothing "
                        "to search yet. If you previously uploaded documents, the "
                        "index may have been reset; re-indexing runs automatically "
                        "at startup, or you can re-upload. Check each document's "
                        "status in the knowledge base."
                    )
            except Exception:  # nosec B110 - stats are best-effort; keep the generic message
                pass
        return {
            "content": content,
            "citations": [],
            "stats": ChatStats(
                docs_searched=0,
                chunks_retrieved=0,
                processing_time=f"{time.time() - start_time:.2f}s",
                case_law_searched=0,
                case_law_included=0,
            ),
        }

    @staticmethod
    def _llm_failure_response(error: Exception, start_time: float) -> dict[str, Any]:
        """An honest answer-shaped response for a failed generation call.

        Provider failures (rate limit, rejected key, timeout, outage) and
        configuration problems are things the user can act on; a bare 500
        tells them nothing. The upstream error body is logged, never shown.
        """
        logger.error(
            f"LLM generation failed: {type(error).__name__}: {error}",
            exc_info=not isinstance(error, ValueError),
        )
        return {
            "content": (
                f"⚠ I couldn't generate an answer: {describe_llm_failure(error)}\n\n"
                "No answer was attempted — this is a problem reaching the AI "
                "provider, not a gap in your documents."
            ),
            "citations": [],
            "stats": ChatStats(
                docs_searched=0,
                chunks_retrieved=0,
                processing_time=f"{time.time() - start_time:.2f}s",
                case_law_searched=0,
                case_law_included=0,
            ),
        }

    def _build_user_prompt(
        self, query, context, query_intent, case_law_results, effective_settings
    ) -> str:
        """Build the user prompt based on query intent."""
        if query_intent == QueryIntent.FACTUAL:
            return f"""Answer this question using the documents below:

Question: {_sanitize_prompt_input(query)}

=== DOCUMENTS ===
{context}
=== END ===

{DOCUMENT_READING_RULES}

Answer directly and concisely. Reference the source document."""
        else:
            case_law_instruction = ""
            if case_law_results:
                case_law_instruction = """

IMPORTANT - Case Law Citation Rules:
- Only cite a case if it DIRECTLY supports or is relevant to your analysis
- Do NOT cite cases just because they were provided - only cite if genuinely applicable
- When you cite a case, use the full case name (e.g., "Smith v. Jones" or "In re Smith")
- If none of the provided cases are relevant to the specific question, do not cite any
- Quality over quantity - it's better to cite 1-2 highly relevant cases than 10 tangentially related ones
- NEVER cite a case from memory. Any case reference not present in the provided context is automatically removed from your answer before the user sees it"""
            else:
                case_law_instruction = """

IMPORTANT: No case-law context was retrieved for this message. Do NOT cite any case law, precedent, or reporter citations from memory — any such reference is automatically removed from your answer before the user sees it."""

            if effective_settings.custom_grounding_rules:
                grounding_rules = effective_settings.custom_grounding_rules
            else:
                grounding_rules = get_default_grounding_rules()

            return f"""Using the documents below as context, answer this query:

Query: {_sanitize_prompt_input(query)}

=== DOCUMENT CONTEXT ===
{context}
=== END OF CONTEXT ===
{case_law_instruction}

{grounding_rules}

{DOCUMENT_READING_RULES}"""

    def _log_debug_context(self, search_results, case_law_results, context):
        """Log debug info about search results and context."""
        logger.debug(f"[RAG Debug] Document results: {len(search_results)}")
        for i, r in enumerate(search_results[:3]):
            logger.debug(
                f"  Doc {i + 1}: {r.get('metadata', {}).get('filename', 'Unknown')} (sim: {r.get('similarity', 0):.3f})"
            )
        logger.debug(f"[RAG Debug] Case law results: {len(case_law_results)}")
        for i, r in enumerate(case_law_results[:3]):
            logger.debug(f"  Case {i + 1}: {r.get('metadata', {}).get('case_name', 'Unknown')}")
        logger.debug(f"[RAG Debug] Context length: {len(context)} chars")

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document and all its chunks from the index."""
        if self.vector_db is None:
            logger.warning(f"Vector DB unavailable, skipping vector deletion for {document_id}")
            return True
        return await self.vector_db.delete_by_document(document_id)


def _empty_response(content: str, start_time: float) -> dict[str, Any]:
    """An answer-shaped response with no citations and zeroed retrieval stats."""
    return {
        "content": content,
        "citations": [],
        "stats": ChatStats(
            docs_searched=0,
            chunks_retrieved=0,
            processing_time=f"{time.time() - start_time:.2f}s",
            case_law_searched=0,
            case_law_included=0,
        ),
    }


async def _search_case_law_within_budget(
    *,
    query: str,
    include_case_law: bool,
    case_law_query: str | None,
    case_law_limit: int,
    deep_case_law: bool,
    openai_client: "AsyncOpenAI | None",
    user_keys: Optional["UserAPIKeys"],
    model: str,
) -> list[dict[str, Any]]:
    """Find case law for the question under a hard time budget, degrading to no results on failure.

    Explicit ``court:`` / ``jurisdiction:`` / ``after:`` / ``before:`` filters
    typed in the question restrict the CourtListener search itself, on both
    the deep and the snippet path.
    """
    if not include_case_law:
        return []
    parsed = search_service.parse_query(query)
    courts = [*parsed.courts, *parsed.jurisdictions]
    filters = case_law_search_filters(courts, parsed.date_from, parsed.date_to)
    # Search CourtListener for case law. The keyword term is the distilled
    # topic (never the raw message) and every hit is judged against the
    # question before it can be cited. Hard time budget: full-opinion
    # fetches are real network reads — an unlucky sequence of retries must
    # degrade to "no case law", never stall the whole chat request.
    case_law_results: list[dict[str, Any]] = []
    # Research-intent chat gets the deep engine automatically: decompose
    # the question into doctrines, read candidate opinions IN FULL, and
    # keep only authorities whose quote verifies verbatim against the
    # opinion text. Falls back to the fast snippet search whenever the
    # deep pass yields nothing (no OpenAI client, timeout, all dropped)
    # so a research question is never answered worse than before.
    if deep_case_law and openai_client is not None:
        try:
            deep_results, deep_status = await asyncio.wait_for(
                research_authorities(
                    question=query,
                    search_query=case_law_query,
                    client=openai_client,
                    limit=case_law_limit,
                    search_filters=filters,
                ),
                timeout=140.0,
            )
            case_law_results = deep_results
            logger.info(f"Deep case-law research status: {deep_status}")
        except TimeoutError:
            logger.warning(
                "Deep case-law research exceeded its 140s budget; falling back to snippet search"
            )
        except Exception as e:  # deep pass must never kill chat
            logger.warning(f"Deep case-law research failed, falling back: {e}")
    if not case_law_results:
        try:
            case_law_results = await asyncio.wait_for(
                search_case_law(
                    case_law_query or query,
                    limit=case_law_limit,
                    date_from=parsed.date_from,
                    date_to=parsed.date_to,
                    courts=courts,
                    question=query,
                    user_keys=user_keys,
                    model=model,
                ),
                timeout=60.0,
            )
        except TimeoutError:
            logger.warning("Case-law search exceeded its 60s budget; answering without it")
        except Exception as e:  # a CourtListener outage must not fail the answer
            logger.warning(f"Case-law search failed ({type(e).__name__}); answering without it")
    return case_law_results


def _resolve_query_intent(mode: AnalysisMode, query: str) -> QueryIntent:
    """Detect the query intent, forcing ANALYTICAL in strategy mode."""
    if mode == AnalysisMode.STRATEGY:
        query_intent = QueryIntent.ANALYTICAL
        logger.debug("[RAG] Strategy mode selected - using ANALYTICAL intent")
    else:
        query_intent = detect_query_intent(query)
        # The query text itself is client-confidential — never logged.
        logger.info(f"[RAG] Query intent detected: {query_intent.value}")
    return query_intent


def _resolve_max_tokens(
    model: str, query_intent: QueryIntent, effective_settings: RAGSettings
) -> int:
    """The user's max-tokens setting, capped by what the model/intent allows."""
    # Honor the user's max-tokens setting, capped by what the model/intent allows.
    model_token_cap = get_model_max_tokens(model, query_intent)
    if query_intent == QueryIntent.FACTUAL:
        return model_token_cap
    return min(effective_settings.max_tokens, model_token_cap)


def _redact_unverified_references(
    content: str,
    case_law_results: list[dict[str, Any]],
    search_results: list[dict[str, Any]],
) -> str:
    """Strip case references the model produced that trace to neither retrieved source."""
    # Case law is never trusted from the model: redact any case reference
    # not traceable to this request's CourtListener results or the user's
    # own retrieved passages.
    content, removed_refs = redact_unverified_case_law(content, case_law_results, search_results)
    if removed_refs:
        logger.warning(
            f"[CaseLawGuard] Removed {len(removed_refs)} unverified case "
            f"reference(s) from chat answer: {removed_refs}"
        )
    return content


async def _build_answer_citations(
    *,
    content: str,
    query: str,
    mode: AnalysisMode,
    search_results: list[dict[str, Any]],
    case_law_results: list[dict[str, Any]],
    include_documents: bool,
    effective_settings: RAGSettings,
    user_id: str,
    user_keys: Optional["UserAPIKeys"],
    model: str,
    matter_ids: list[str] | None = None,
) -> tuple[list[Citation], list[dict[str, Any]], str | None]:
    """Build, verify and enrich the document and case-law citations for an answer.

    Returns ``(citations, cited_cases, notice)``; ``notice`` lists the answer's
    statements the source check could not fully confirm (None when there are
    none or the check did not run).
    """
    # Build citations
    cited_cases = extract_cited_cases(content, case_law_results) if case_law_results else []
    citations = build_document_citations(search_results, query, mode)
    # Citation verification: only keep `was_cited_by_ai` for citations whose
    # content is actually reflected in the answer (otherwise reset to PENDING).
    if effective_settings.citation_verification:
        verify_document_citations(citations, content)
    # Source tracking: record precise chunk/page/char location for each citation.
    if effective_settings.source_tracking:
        annotate_source_locations(citations, search_results)

    # Claim-level grounding: the answer's OWN claims become the document
    # citations — each with exact contract language verified verbatim and
    # pinned to a char span in its source file. Replaces chunk-provenance
    # citations whenever it succeeds; any failure keeps the chunk ones.
    grounded = None
    grounding = None
    if include_documents and search_results:
        try:
            grounding = await ground_answer(
                query=query,
                answer=content,
                search_results=search_results,
                user_id=user_id,
                user_keys=user_keys,
                model=model,
                # Retrieval searched the caller's shared matters; grounding
                # must be able to read those same files or it silently drops
                # a colleague's document from verification.
                accessible_matter_ids=set(matter_ids) if matter_ids else None,
                # The support check is the stronger form of citation verification.
                check_support=effective_settings.citation_verification,
            )
        except Exception as e:  # grounding is best-effort
            logger.warning(f"Claim grounding failed, keeping chunk citations: {e}")
    if grounding and grounding.citations:
        grounded = grounding.citations
        citations = grounded

    case_law_citations = build_case_law_citations(
        case_law_results, cited_cases, len(search_results), query
    )
    citations.extend(case_law_citations)
    # Auditable per-citation reasoning (ONE utility-LLM call). Grounded
    # claim citations already carry their own Claim→Evidence chains, so the
    # pass then only needs to cover the case-law citations.
    await enrich_citations_with_reasoning(
        case_law_citations if grounded else citations,
        query=query,
        answer=content,
        user_keys=user_keys,
        model=model,
    )
    return citations, cited_cases, (grounding.notice() if grounding else None)


async def _count_docs_searched(
    include_documents: bool,
    document_ids: list[str] | None,
    user_id: str,
    search_results: list[dict[str, Any]],
) -> int:
    """Number of documents in this query's scope, for the stats line."""
    # "Docs searched" = documents in THIS query's scope — the scoped file
    # count when a filter applies, else the user's indexed documents.
    # (Never the vector store's chunk count: one file = many chunks, and
    # the collection total spans every user.)
    if not include_documents:
        return 0
    if document_ids is not None:
        return len(document_ids)
    # Lazy import: documents.py imports rag_service at module load.
    from app.services.documents import document_service

    docs_searched = 0
    if user_id:
        try:
            doc_stats = await document_service.get_stats(user_id)
            docs_searched = doc_stats.get("by_status", {}).get("indexed", 0)
        except (ValueError, KeyError, RuntimeError):
            docs_searched = len(
                {
                    (c.get("metadata") or {}).get("document_id")
                    for c in search_results
                    if (c.get("metadata") or {}).get("document_id")
                }
            )
    return docs_searched
