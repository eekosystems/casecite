"""Settings response models — prompt defaults, instance models and index clearing."""

from pydantic import BaseModel


class ModePrompts(BaseModel):
    """Mode-specific prompt defaults."""

    research: str
    case: str
    document: str
    compliance: str
    strategy: str


class PromptDefaultsResponse(BaseModel):
    """GET /settings/prompts/defaults."""

    system_prompt: str
    grounding_rules: str
    factual_prompt: str
    mode_prompts: ModePrompts


class EmbeddingInfoResponse(BaseModel):
    """GET /settings/embeddings — which embedding model indexes documents (read-only).

    ``provider`` says where document text goes to be embedded ("self_hosted",
    "openai", "voyage", "cohere" or a hostname). The server address is admin-only.
    """

    source: str
    provider: str
    model: str
    dimensions: int
    reindex_state: str
    reindex_done: int
    reindex_total: int


class ClearResponse(BaseModel):
    """POST /settings/clear."""

    status: str
    message: str
    cleared_documents: int | None = None
    failed_documents: int | None = None


class InstanceModelsResponse(BaseModel):
    """GET /settings/models — the instance's configured models, for the model picker.

    ``custom_endpoint`` is True when the OpenAI-compatible client points at a
    custom endpoint (a local server or gateway), so OpenAI's own models are not
    reachable through it. The endpoint address itself is admin-only.
    """

    custom_endpoint: bool
    chat_model: str
    utility_model: str
    research_model: str
