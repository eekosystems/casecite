"""
Embedding Service - text embeddings for indexing and retrieval.

Providers:
- OpenAI embeddings (text-embedding-3-small/large) — the default
- Voyage AI (voyage-law-2 is trained on legal text)
- Cohere
- Any OpenAI-compatible embeddings server you host (EMBEDDING_BASE_URL, or
  Settings → Embedding), so document text can be embedded on your own network

The settings come from .env and can be replaced at runtime by an admin
(``apply_instance_config``). Vectors from different models can't be mixed, so
each model has its own vector collection (``collection_name``); see
app/services/embedding_switch.py for how a switch re-embeds the documents.

Every provider call is checked against the AI provider allowlist first
(app/services/provider_policy.py).
"""

import functools
import hashlib
from typing import TYPE_CHECKING, NamedTuple, Optional

import httpx
import tiktoken

from app.config import is_placeholder_api_key, settings
from app.services.provider_policy import enforce_provider_allowed, provider_for_base_url

if TYPE_CHECKING:
    from app.services.user_keys import UserAPIKeys

# Default models used when a BYOK key routes embeddings to a provider other
# than the one the server model belongs to.
DEFAULT_VOYAGE_MODEL = "voyage-law-2"
DEFAULT_COHERE_MODEL = "embed-english-v3.0"

# Vector collection for the .env embedding model (the name every existing
# install already uses). Other models get their own collection.
DEFAULT_COLLECTION = "legal_documents"

# Instance secret holding the admin's embedding settings (encrypted at rest).
INSTANCE_KEY = "embedding_config"


def _real_key(value: str | None) -> str | None:
    """The key, or None when it is empty or an unfilled .env placeholder."""
    if not value or is_placeholder_api_key(value):
        return None
    return value


class EmbeddingRoute(NamedTuple):
    """Where one embedding request goes: provider, model, credential, endpoint."""

    provider: str  # "openai" | "voyage" | "cohere" | "self_hosted" | <hostname>
    kind: str  # "openai" (OpenAI-compatible API) | "voyage" | "cohere"
    model: str
    api_key: str | None
    base_url: str | None = None


class EmbeddingService:
    """
    Multi-provider embedding service.

    For legal applications voyage-law-2 is a good choice — it is trained on
    legal text.
    """

    # Model dimensions
    MODEL_DIMENSIONS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
        "voyage-law-2": 1024,
        "voyage-4": 1024,
        "voyage-4-lite": 1024,
        "voyage-3.5": 1024,
        "voyage-large-2": 1536,
        "embed-v4.0": 1536,
        "embed-english-v3.0": 1024,
        "embed-multilingual-v3.0": 1024,
    }

    def __init__(self):
        self.openai_api_key = _real_key(settings.openai_api_key)
        self.voyage_api_key = _real_key(getattr(settings, "voyage_api_key", None))
        self.cohere_api_key = _real_key(getattr(settings, "cohere_api_key", None))
        self._apply_env()
        self._env_signature = self.signature()

    def _apply_env(self) -> None:
        """Embedding settings from .env (the default until an admin changes them)."""
        self.source = "env"
        self._dimensions: int | None = getattr(settings, "embedding_dimensions", None)
        self._instance_api_key: str | None = None
        # Self-hosted OpenAI-compatible embeddings endpoint (None = hosted providers).
        self.base_url = (getattr(settings, "embedding_base_url", None) or "").strip() or None

        # Select model based on available API keys
        preferred_model = getattr(settings, "embedding_model", settings.openai_embedding_model)
        if self.base_url:
            # The operator's own server serves whatever model they named.
            self.model = preferred_model
        elif (
            preferred_model.startswith("voyage")
            and not self.voyage_api_key
            or preferred_model.startswith("embed-")
            and not self.cohere_api_key
        ):
            self.model = settings.openai_embedding_model  # Fallback to OpenAI
        else:
            self.model = preferred_model

    def apply_instance_config(self, cfg: dict | None) -> None:
        """Use an admin's embedding server, or go back to .env with ``None``.

        ``cfg``: ``{"base_url", "model", "dimensions", "api_key"?}``, already
        checked with :meth:`probe` (so ``dimensions`` is the real vector size).
        """
        if not cfg:
            self._apply_env()
            return
        self.source = "instance"
        self.base_url = str(cfg["base_url"]).strip()
        self.model = str(cfg["model"]).strip()
        self._dimensions = int(cfg["dimensions"])
        self._instance_api_key = cfg.get("api_key") or None

    @property
    def server_api_key(self) -> str | None:
        """Credential for a self-hosted server. From .env it is the OpenAI key, as
        it always has been; an admin-set server only ever gets its own key."""
        key: str | None = (
            self._instance_api_key if self.source == "instance" else self.openai_api_key
        )
        return key

    def signature(self) -> tuple[str, int]:
        """What makes vectors comparable: the model and its vector size."""
        return (self.model, self.dimensions)

    def collection_name(self) -> str:
        """Vector collection for the active model.

        The .env model keeps the original collection, so existing installs see
        no change. Any other model gets its own, so switching back to a model
        used before needs no re-embedding.
        """
        if self.source == "env" or self.signature() == self._env_signature:
            return DEFAULT_COLLECTION
        model, dims = self.signature()
        digest = hashlib.sha256(f"{model}|{dims}".encode()).hexdigest()[:12]
        return f"{DEFAULT_COLLECTION}_{digest}"

    def is_usable(self) -> bool:
        """True when an embedding request has somewhere to go (server or key)."""
        return bool(self.base_url or self.resolve_route(None).api_key)

    async def probe(self, base_url: str, model: str, api_key: str | None = None) -> int:
        """Embed a test string on a server before switching to it; return the vector size.

        Raises ValueError with a message fit to show an admin.
        """
        from openai import AsyncOpenAI

        enforce_provider_allowed(provider_for_base_url(base_url), "embedding request")
        client = AsyncOpenAI(
            api_key=api_key or "not-needed",
            base_url=base_url,
            timeout=min(settings.api_timeout, 60),
            max_retries=0,
        )
        try:
            response = await client.embeddings.create(model=model, input=["embedding check"])
            vector = response.data[0].embedding
        except Exception as e:  # SDK / network errors share no useful base class
            raise ValueError(
                f"The embedding server at {base_url} did not return an embedding for '{model}': {e}"
            ) from e
        if not vector:
            raise ValueError(f"The embedding server returned an empty vector for '{model}'.")
        return len(vector)

    @functools.cached_property
    def tokenizer(self) -> tiktoken.Encoding:
        """Tokenizer for chunking, loaded on first use.

        tiktoken downloads its encoding table the first time it is needed (the
        Docker image ships it pre-fetched). Loading it lazily keeps that
        download out of import, so the app still starts when it is unreachable.
        """
        try:
            return tiktoken.encoding_for_model("gpt-4")
        except (KeyError, ValueError):
            return tiktoken.get_encoding("cl100k_base")

    @property
    def dimensions(self) -> int:
        """Get embedding dimensions for current model."""
        if self._dimensions:
            return int(self._dimensions)
        return self.MODEL_DIMENSIONS.get(self.model, 1536)

    def resolve_route(self, user_keys: Optional["UserAPIKeys"] = None) -> EmbeddingRoute:
        """Decide provider, model and credential for a request.

        Documents and queries MUST resolve to the same route, or query vectors
        land in a different space than the indexed chunks — both
        ``embed_texts`` and ``embed_query`` go through here.
        """
        # Get API keys - prefer user keys (BYOK), fall back to server keys
        openai_key = user_keys.openai if user_keys and user_keys.openai else self.openai_api_key
        voyage_key = user_keys.voyage if user_keys and user_keys.voyage else self.voyage_api_key
        cohere_key = user_keys.cohere if user_keys and user_keys.cohere else self.cohere_api_key

        if self.base_url:
            # Self-hosted endpoint: never route around it to a hosted provider,
            # and never hand a user's personal OpenAI key to it — only the
            # instance's own credential (if the server needs one at all).
            return EmbeddingRoute(
                provider=provider_for_base_url(self.base_url),
                kind="openai",
                model=self.model,
                api_key=self.server_api_key,
                base_url=self.base_url,
            )

        model = self.model
        # If the server model is OpenAI's but the only usable key is for
        # another provider, use that provider's default model (never the
        # OpenAI model name, which the other provider would reject).
        if not model.startswith("voyage") and voyage_key and not openai_key:
            model = DEFAULT_VOYAGE_MODEL
        elif not model.startswith("embed-") and cohere_key and not openai_key and not voyage_key:
            model = DEFAULT_COHERE_MODEL

        if model.startswith("voyage"):
            return EmbeddingRoute("voyage", "voyage", model, voyage_key)
        if model.startswith("embed-"):
            return EmbeddingRoute("cohere", "cohere", model, cohere_key)
        return EmbeddingRoute("openai", "openai", model, openai_key)

    def credential_scope(self, user_keys: Optional["UserAPIKeys"] = None) -> str:
        """Stable, non-reversible id of the provider + credential a request uses.

        Used to scope the embeddings circuit breaker per credential, so a user
        with a bad BYOK key trips only their own breaker.
        """
        route = self.resolve_route(user_keys)
        digest = hashlib.sha256((route.api_key or "").encode()).hexdigest()[:12]
        return f"{route.provider}:{digest}"

    async def embed_text(self, text: str, user_keys: Optional["UserAPIKeys"] = None) -> list[float]:
        """Generate embedding for a single text."""
        embeddings = await self.embed_texts([text], user_keys=user_keys)
        return embeddings[0]

    async def embed_texts(
        self, texts: list[str], user_keys: Optional["UserAPIKeys"] = None
    ) -> list[list[float]]:
        """Generate document embeddings for multiple texts.

        Args:
            texts: List of texts to embed.
            user_keys: Optional user-provided API keys for BYOK.
        """
        if not texts:
            return []
        return await self._embed(texts, self.resolve_route(user_keys), is_query=False)

    async def embed_query(
        self, query: str, user_keys: Optional["UserAPIKeys"] = None
    ) -> list[float]:
        """
        Generate embedding for a search query.

        Uses the same provider/model as document embeddings; Voyage and Cohere
        additionally distinguish query from document inputs.
        """
        embeddings = await self._embed([query], self.resolve_route(user_keys), is_query=True)
        return embeddings[0]

    async def _embed(
        self, texts: list[str], route: EmbeddingRoute, is_query: bool
    ) -> list[list[float]]:
        if route.kind == "voyage":
            return await self._embed_voyage(
                texts,
                api_key=route.api_key,
                model=route.model,
                input_type="query" if is_query else "document",
            )
        if route.kind == "cohere":
            return await self._embed_cohere(
                texts,
                api_key=route.api_key,
                model=route.model,
                input_type="search_query" if is_query else "search_document",
            )
        return await self._embed_openai(texts, api_key=route.api_key, model=route.model)

    async def _embed_openai(
        self, texts: list[str], api_key: str | None = None, model: str | None = None
    ) -> list[list[float]]:
        """Generate embeddings using the OpenAI API or a self-hosted compatible server."""
        from openai import AsyncOpenAI

        # A self-hosted server gets only the credential its route carries.
        key = api_key or (None if self.base_url else self.openai_api_key)
        if self.base_url and not key:
            # Local servers ignore the key, but the SDK requires a non-empty one.
            key = "not-needed"
        if not key:
            raise ValueError(
                "OpenAI API key not configured. Please provide your API key in Settings."
            )
        enforce_provider_allowed(provider_for_base_url(self.base_url), "embedding request")

        # Embeddings go to api.openai.com unless EMBEDDING_BASE_URL names a
        # self-hosted server (the chat OPENAI_BASE_URL is deliberately not
        # reused: a chat gateway rarely serves embeddings). Built directly
        # rather than via llm_clients.make_openai — but with the same
        # timeout/retry bounds so a hung request cannot pin an indexing job
        # forever.
        client_kwargs: dict = {"api_key": key, "timeout": settings.api_timeout, "max_retries": 2}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = AsyncOpenAI(**client_kwargs)
        batch_size = 100
        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = await client.embeddings.create(model=model or self.model, input=batch)
            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)

        return all_embeddings

    async def _embed_voyage(
        self,
        texts: list[str],
        api_key: str | None = None,
        model: str | None = None,
        input_type: str = "document",
    ) -> list[list[float]]:
        """
        Generate embeddings using Voyage AI.

        ``input_type`` is "document" when indexing and "query" when searching —
        Voyage embeds the two differently for better retrieval.

        API: https://docs.voyageai.com/
        """
        key = api_key or self.voyage_api_key
        if not key:
            raise ValueError(
                "Voyage API key not configured. Please provide your Voyage API key in Settings."
            )
        enforce_provider_allowed("voyage", "embedding request")
        voyage_model = model or (
            self.model if self.model.startswith("voyage") else DEFAULT_VOYAGE_MODEL
        )

        all_embeddings = []
        batch_size = 128  # Voyage allows larger batches

        async with httpx.AsyncClient(timeout=60.0) as client:
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]

                response = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": voyage_model,
                        "input": batch,
                        "input_type": input_type,
                    },
                )
                response.raise_for_status()
                data = response.json()

                # Extract embeddings in order
                batch_embeddings = [item["embedding"] for item in data["data"]]
                all_embeddings.extend(batch_embeddings)

        return all_embeddings

    async def _embed_cohere(
        self,
        texts: list[str],
        api_key: str | None = None,
        model: str | None = None,
        input_type: str = "search_document",
    ) -> list[list[float]]:
        """Generate embeddings using Cohere API."""
        key = api_key or self.cohere_api_key
        if not key:
            raise ValueError(
                "Cohere API key not configured. Please provide your Cohere API key in Settings."
            )
        enforce_provider_allowed("cohere", "embedding request")
        cohere_model = model or (
            self.model if self.model.startswith("embed-") else DEFAULT_COHERE_MODEL
        )

        all_embeddings = []
        batch_size = 96  # Cohere limit

        async with httpx.AsyncClient(timeout=60.0) as client:
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]

                response = await client.post(
                    "https://api.cohere.ai/v1/embed",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": cohere_model,
                        "texts": batch,
                        "input_type": input_type,
                        "truncate": "END",
                    },
                )
                response.raise_for_status()
                data = response.json()
                all_embeddings.extend(data["embeddings"])

        return all_embeddings

    def count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        return len(self.tokenizer.encode(text))

    def chunk_text(
        self, text: str, chunk_size: int = None, chunk_overlap: int = None
    ) -> list[dict]:
        """Split text into overlapping chunks."""
        chunk_size = chunk_size or getattr(settings, "chunk_size", 512)
        chunk_overlap = chunk_overlap or getattr(settings, "chunk_overlap", 128)

        # The window must always advance: an overlap >= the chunk size would
        # otherwise never move past the first chunk (half-overlap instead).
        if chunk_overlap < chunk_size:
            step = chunk_size - chunk_overlap
        else:
            step = max(1, chunk_size // 2)

        tokens = self.tokenizer.encode(text)
        chunks = []
        start = 0
        chunk_index = 0

        while start < len(tokens):
            end = min(start + chunk_size, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = self.tokenizer.decode(chunk_tokens)

            chunks.append(
                {
                    "text": chunk_text,
                    "chunk_index": chunk_index,
                    "token_count": len(chunk_tokens),
                    "start_token": start,
                    "end_token": end,
                }
            )

            start += step
            chunk_index += 1

        return chunks


# Singleton instance
embedding_service = EmbeddingService()
