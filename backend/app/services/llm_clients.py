"""
Central OpenAI-compatible client factory + runtime model config.

A single place that builds OpenAI SDK clients so the whole app can be pointed at
a custom endpoint — local/open models (Ollama, LM Studio, vLLM, llama.cpp) or
hosted OSS gateways (Together, Groq, Fireworks, OpenRouter) — instead of OpenAI.

The endpoint/model are instance-wide and editable at runtime (admin UI), so the
config lives in a small in-memory holder seeded from settings (.env) at import
and refreshed from encrypted instance storage at startup.
"""

import json
import logging

from openai import AsyncOpenAI, OpenAI

from app.config import settings
from app.services.provider_policy import enforce_openai_client

logger = logging.getLogger(__name__)

INSTANCE_KEY = "local_llm_config"

# Effective config. base_url=None -> default OpenAI. Models fall back to settings.
# research_model=None -> deep research uses the chat model.
_runtime: dict[str, str | None] = {
    "base_url": (settings.openai_base_url or "").strip() or None,
    "chat_model": settings.openai_chat_model,
    "utility_model": settings.openai_utility_model,
    "research_model": (settings.openai_research_model or "").strip() or None,
}


def set_runtime(
    base_url: str | None = None,
    chat_model: str | None = None,
    utility_model: str | None = None,
    research_model: str | None = None,
) -> None:
    """Update the effective config. Pass None to leave a field unchanged.

    base_url="" clears the endpoint (revert to OpenAI); research_model="" clears
    the research model so deep research follows the chat model again.
    """
    if base_url is not None:
        _runtime["base_url"] = base_url.strip() or None
    if chat_model:
        _runtime["chat_model"] = chat_model
    if utility_model:
        _runtime["utility_model"] = utility_model
    if research_model is not None:
        _runtime["research_model"] = research_model.strip() or None


def get_runtime() -> dict[str, str | None]:
    return dict(_runtime)


def is_custom_endpoint() -> bool:
    return bool(_runtime["base_url"])


def chat_model() -> str:
    return _runtime["chat_model"] or settings.openai_chat_model


def utility_model() -> str:
    return _runtime["utility_model"] or settings.openai_utility_model


def research_model() -> str:
    """Model for deep research: the research model if one is set, else the chat model."""
    return _runtime["research_model"] or chat_model()


def model_family(model: str | None) -> str:
    """Which provider family a model NAME belongs to ("anthropic"/"google"/"openai")."""
    lowered = (model or "").lower()
    if "claude" in lowered:
        return "anthropic"
    if "gemini" in lowered:
        return "google"
    return "openai"


def resolve_chat_model(requested: str | None) -> str:
    """The model a chat request should use, given the user's model setting.

    - No choice ("" / None): the instance chat model.
    - A Claude or Gemini model: kept. Those are served by their own clients.
    - An OpenAI model name while a custom endpoint is configured: the endpoint's
      chat model, unless the name is one of the models the endpoint was set up
      with. The OpenAI client points at that endpoint, so a name like "gpt-5.5"
      would be sent to the local server, which does not serve it.
    """
    name = (requested or "").strip()
    if not name:
        return chat_model()
    if model_family(name) != "openai" or not is_custom_endpoint():
        return name
    if name in {chat_model(), utility_model(), research_model()}:
        return name
    return chat_model()


# SDK-level retries (backoff on 429/5xx/connection errors). Pinned explicitly so
# every client has the same bounded budget; app-level retry/circuit-breaker
# logic handles anything beyond that.
SDK_MAX_RETRIES = 2


def make_openai(
    api_key: str | None = None,
    *,
    async_: bool = False,
    timeout: float | None = None,
    max_retries: int = SDK_MAX_RETRIES,
):
    """Build an OpenAI SDK client honoring the configured base_url.

    This is the single factory for OpenAI(-compatible) clients: every client it
    builds carries a request timeout (``settings.api_timeout`` unless
    ``timeout`` overrides it) and bounded retries, so a hung upstream can never
    pin a request forever.

    Returns None only when there's no usable key AND no custom endpoint. For a
    custom endpoint with no key (e.g. local Ollama), a placeholder is used since
    the SDK requires a non-empty key but local servers ignore it.
    """
    base_url = _runtime["base_url"]
    key = api_key or settings.openai_api_key
    if base_url and not key:
        key = "not-needed"
    if not key:
        return None
    kwargs: dict = {
        "api_key": key,
        "timeout": settings.api_timeout if timeout is None else timeout,
        "max_retries": max_retries,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs) if async_ else OpenAI(**kwargs)


def _adapt_unsupported_params(kwargs: dict, err_msg: str) -> bool:
    """Mutate ``kwargs`` to satisfy the API complaint in ``err_msg``.

    Newer OpenAI models reject the classic parameters: max_tokens must become
    max_completion_tokens, and some models accept only the default temperature.
    Rather than maintaining a model-capability table (stale the day a new model
    ships), try the classic call and adapt to whatever the API rejects.
    Returns True when an adaptation was made and the call should be retried.
    """
    lowered = err_msg.lower()
    if "max_tokens" in kwargs and "max_tokens" in lowered and "unsupported" in lowered:
        kwargs.setdefault("extra_body", {})["max_completion_tokens"] = kwargs.pop("max_tokens")
        return True
    if "temperature" in kwargs and "temperature" in lowered and "unsupported" in lowered:
        kwargs.pop("temperature")
        return True
    return False


async def openai_chat(client, **kwargs):
    """``chat.completions.create`` with automatic parameter negotiation.

    Every OpenAI chat call in the app goes through here so new models that
    rename/reject parameters keep working everywhere at once — and so the AI
    provider allowlist is enforced in one place: the client's endpoint is
    checked before anything is sent (ProviderNotAllowedError, a ValueError).
    """
    enforce_openai_client(client)
    attempts = 0
    while True:
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as e:  # adapt-and-retry, re-raised when not adaptable
            attempts += 1
            if attempts > 2 or not _adapt_unsupported_params(kwargs, str(e)):
                raise


def openai_chat_sync(client, **kwargs):
    """Sync variant of :func:`openai_chat` for code running via ``to_thread``."""
    enforce_openai_client(client)
    attempts = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:  # adapt-and-retry, re-raised when not adaptable
            attempts += 1
            if attempts > 2 or not _adapt_unsupported_params(kwargs, str(e)):
                raise


async def load_from_storage() -> None:
    """Refresh the runtime config from encrypted instance storage (admin-set)."""
    try:
        from app.services.instance_settings import get_secret

        raw = await get_secret(INSTANCE_KEY)
        if raw:
            cfg = json.loads(raw)
            set_runtime(
                base_url=cfg.get("base_url", None),
                chat_model=cfg.get("chat_model"),
                utility_model=cfg.get("utility_model"),
                research_model=cfg.get("research_model"),
            )
            if _runtime["base_url"]:
                logger.info("Applied instance custom LLM endpoint: %s", _runtime["base_url"])
    except Exception as e:
        logger.warning("Could not load instance LLM config: %s", e)
