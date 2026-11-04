"""
Central OpenAI-compatible client factory + runtime model config.

A single place that builds OpenAI SDK clients so the whole app can be pointed at
a custom endpoint — local/open models (Ollama, LM Studio, vLLM, llama.cpp) or
hosted OSS gateways (Together, Groq, Fireworks, OpenRouter) — instead of OpenAI.

The endpoint/model are instance-wide and editable at runtime (admin UI), so the
config lives in a small in-memory holder seeded from settings (.env) at import
and refreshed from encrypted instance storage at startup.

Admins can register several models, each on its own server (Ollama on one box,
vLLM serving one model per server on others). Clients are built for the chat
model's server; a call naming a model that lives on another server is sent
there (see ``openai_chat``).
"""

import json
import logging
from typing import Any

from openai import AsyncOpenAI, OpenAI

from app.config import settings
from app.services.provider_policy import enforce_openai_client

logger = logging.getLogger(__name__)

INSTANCE_KEY = "local_llm_config"


def _norm_url(url: Any) -> str:
    """A server URL in comparable form (no surrounding space or trailing slash)."""
    return str(url or "").strip().rstrip("/")


def _single_server_models(base_url: str | None, *names: str | None) -> list[dict[str, str]]:
    """The registry .env and older saved configs describe: the role models on one server."""
    if not base_url:
        return []
    unique = dict.fromkeys(n.strip() for n in names if n and n.strip())
    return [{"name": n, "base_url": _norm_url(base_url)} for n in unique]


def _env_research_model() -> str:
    return (settings.openai_research_model or settings.openai_deep_research_model or "").strip()


# Effective config. base_url=None -> default OpenAI. Models fall back to settings.
# research_model=None -> deep research uses the utility model; drafting_model=None
# -> drafting uses the chat model. base_url is the chat model's server; "models"
# is the registry of every model and its server.
_runtime: dict[str, Any] = {
    "base_url": (settings.openai_base_url or "").strip() or None,
    "chat_model": settings.openai_chat_model,
    "utility_model": settings.openai_utility_model,
    "research_model": _env_research_model() or None,
    "drafting_model": (settings.openai_drafting_model or "").strip() or None,
}
_ROLES = ("chat_model", "utility_model", "research_model", "drafting_model")
_runtime["models"] = _single_server_models(_runtime["base_url"], *(_runtime[r] for r in _ROLES))


def set_runtime(
    base_url: str | None = None,
    chat_model: str | None = None,
    utility_model: str | None = None,
    research_model: str | None = None,
    models: list[dict[str, str]] | None = None,
    drafting_model: str | None = None,
) -> None:
    """Update the effective config. Pass None to leave a field unchanged.

    base_url="" clears the endpoint (revert to OpenAI); research_model="" clears
    the research model so deep research follows the utility model again, and
    drafting_model="" lets drafting follow the chat model again.

    ``models`` replaces the registry ([{"name", "base_url"}, ...]); base_url then
    becomes the chat model's server. Without ``models`` the registry is rebuilt
    as the role models on base_url (the single-server shape).
    """
    if base_url is not None:
        _runtime["base_url"] = base_url.strip() or None
    if chat_model:
        _runtime["chat_model"] = chat_model
    if utility_model:
        _runtime["utility_model"] = utility_model
    if research_model is not None:
        _runtime["research_model"] = research_model.strip() or None
    if drafting_model is not None:
        _runtime["drafting_model"] = drafting_model.strip() or None
    if models is None:
        _runtime["models"] = _single_server_models(
            _runtime["base_url"], *(_runtime[r] for r in _ROLES)
        )
        return
    registry = [
        {"name": str(m.get("name") or "").strip(), "base_url": _norm_url(m.get("base_url"))}
        for m in models
    ]
    registry = [m for m in registry if m["name"] and m["base_url"]]
    _runtime["models"] = registry
    chat_server = next(
        (m["base_url"] for m in registry if m["name"] == _runtime["chat_model"]), None
    )
    _runtime["base_url"] = chat_server or (registry[0]["base_url"] if registry else None)


def get_runtime() -> dict[str, Any]:
    cfg = dict(_runtime)
    cfg["models"] = [dict(m) for m in _runtime.get("models") or []]
    return cfg


def model_names() -> list[str]:
    """Names of the registered models (empty when no custom endpoint is set)."""
    return [m["name"] for m in _runtime.get("models") or []]


def server_for(model: str | None) -> str | None:
    """The server a registered model lives on, or None when it isn't registered."""
    name = (model or "").strip()
    for entry in _runtime.get("models") or []:
        if entry["name"] == name:
            return str(entry["base_url"])
    return None


def is_custom_endpoint() -> bool:
    return bool(_runtime["base_url"])


def chat_model() -> str:
    return _runtime["chat_model"] or settings.openai_chat_model


def utility_model() -> str:
    return _runtime["utility_model"] or settings.openai_utility_model


def research_model() -> str:
    """Model for deep research: the research model if one is set, else the utility model."""
    return _runtime["research_model"] or utility_model()


def drafting_model() -> str:
    """Model for drafting documents: the drafting model if one is set, else the chat model."""
    return _runtime.get("drafting_model") or chat_model()


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
      chat model, unless the name is a registered model. The OpenAI client points
      at the admin's servers, so a name like "gpt-5.5" would be sent to a local
      server, which does not serve it.
    """
    name = (requested or "").strip()
    if not name:
        return chat_model()
    if model_family(name) != "openai" or not is_custom_endpoint():
        return name
    if name in {*model_names(), chat_model(), utility_model(), research_model(), drafting_model()}:
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


def uses_server_key(client) -> bool:
    """True when the client carries the instance's own key (not the user's)."""
    key = getattr(client, "api_key", None)
    return bool(key) and key == settings.openai_api_key


def client_for_model(client, model: str | None):
    """The client that should carry a call naming ``model``.

    Clients are built for the chat model's server. A registered model on a
    different server gets a client of the same kind pointed at its server, with
    the same key, timeout and retry budget. Anything else uses ``client``.
    """
    target = server_for(model)
    if not target or _norm_url(getattr(client, "base_url", None)) == target:
        return client
    if isinstance(client, AsyncOpenAI):
        cls: type = AsyncOpenAI
    elif isinstance(client, OpenAI):
        cls = OpenAI
    else:  # not an OpenAI SDK client (e.g. a test double): leave it alone
        return client
    return cls(
        api_key=client.api_key,
        base_url=target,
        timeout=client.timeout,
        max_retries=client.max_retries,
    )


async def openai_chat(client, *, purpose: str = "chat", **kwargs):
    """``chat.completions.create`` with automatic parameter negotiation.

    Every OpenAI chat call in the app goes through here so new models that
    rename/reject parameters keep working everywhere at once — and so the AI
    provider allowlist is enforced in one place: the endpoint that will carry
    the call is checked before anything is sent (ProviderNotAllowedError, a
    ValueError). A registered model on another server is sent to that server.
    The response's token counts are metered against the current user
    (``purpose`` says what the call was for).
    """
    client = client_for_model(client, kwargs.get("model"))
    enforce_openai_client(client)
    attempts = 0
    while True:
        try:
            response = await client.chat.completions.create(**kwargs)
            break
        except Exception as e:  # adapt-and-retry, re-raised when not adaptable
            attempts += 1
            if attempts > 2 or not _adapt_unsupported_params(kwargs, str(e)):
                raise
    from app.services.usage import record_usage

    await record_usage(
        "openai",
        response,
        model=kwargs.get("model"),
        purpose=purpose,
        server_key=uses_server_key(client),
    )
    return response


def openai_chat_sync(client, *, purpose: str = "chat", **kwargs):
    """Sync variant of :func:`openai_chat` for code running via ``to_thread``."""
    client = client_for_model(client, kwargs.get("model"))
    enforce_openai_client(client)
    attempts = 0
    while True:
        try:
            response = client.chat.completions.create(**kwargs)
            break
        except Exception as e:  # adapt-and-retry, re-raised when not adaptable
            attempts += 1
            if attempts > 2 or not _adapt_unsupported_params(kwargs, str(e)):
                raise
    from app.services.usage import record_usage_sync

    record_usage_sync(
        "openai",
        response,
        model=kwargs.get("model"),
        purpose=purpose,
        server_key=uses_server_key(client),
    )
    return response


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
                drafting_model=cfg.get("drafting_model"),
                # Configs saved before the model list existed have no "models":
                # they describe one server, which set_runtime rebuilds.
                models=cfg.get("models"),
            )
            if _runtime["base_url"]:
                logger.info(
                    "Applied instance custom LLM endpoint: %s (%d models)",
                    _runtime["base_url"],
                    len(_runtime["models"]),
                )
    except Exception as e:
        logger.warning("Could not load instance LLM config: %s", e)
