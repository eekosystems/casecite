"""LLM generation and context building for RAG pipeline."""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from app.config import settings
from app.models.schemas import QueryIntent
from app.services.llm_clients import SDK_MAX_RETRIES, model_family, openai_chat
from app.services.provider_policy import (
    enforce_openai_client,
    enforce_provider_allowed,
    openai_client_provider,
)
from app.services.rag.prompt_safety import untrusted_block

if TYPE_CHECKING:
    from app.services.user_keys import UserAPIKeys

logger = logging.getLogger(__name__)


def build_context_with_case_law(
    doc_results: list[dict[str, Any]], case_law_results: list[dict[str, Any]]
) -> str:
    """Build context string from both document and case law results.

    Every chunk and opinion excerpt is third-party text, so each is wrapped
    in an ``untrusted_block`` — the model is told (via the system prompt's
    UNTRUSTED_CONTENT_RULE) that whatever those blocks say is data.
    """
    context_parts = []

    if doc_results:
        context_parts.append("=== FROM YOUR DOCUMENTS ===\n")
        for i, result in enumerate(doc_results):
            source = result.get("metadata", {}).get("filename", "Unknown")
            text = result.get("text", "")
            label = f"Document {i + 1}: {source}"
            context_parts.append(f"[{label}]\n{untrusted_block(label, text)}\n")

    if case_law_results:
        context_parts.append("\n=== FROM CASE LAW (CourtListener) ===\n")
        for i, result in enumerate(case_law_results):
            metadata = result.get("metadata", {})
            case_name = metadata.get("filename", "Unknown Case")
            citation = metadata.get("citation", "")
            court = metadata.get("court", "")
            text = result.get("text", "")
            label = f"Case {i + 1}: {case_name}"
            context_parts.append(
                f"[{label}]\nCourt: {court}\nCitation: {citation}\n{untrusted_block(label, text)}\n"
            )

    return "\n---\n".join(context_parts)


# Same as the RAGSettings.max_tokens ceiling: no cap beyond the user's own setting.
UNKNOWN_MODEL_MAX_TOKENS = 128_000


def get_model_max_tokens(model_name: str, intent: QueryIntent | None = None) -> int:
    """Get maximum output tokens for a model, adjusted by query intent."""
    # Factual queries: cap at 500 tokens - they should be brief
    if intent == QueryIntent.FACTUAL:
        return 500

    model_lower = model_name.lower()
    if "gpt-5" in model_lower:
        return 32768
    elif "gpt-4o" in model_lower:
        return 16384
    elif "gpt-4-turbo" in model_lower or "gpt-4" in model_lower:
        return 4096
    elif "claude-opus" in model_lower or "claude-sonnet-4" in model_lower or "fable" in model_lower:
        return 32768
    elif (
        "claude-3-5" in model_lower or "claude-3.5" in model_lower or "claude-haiku" in model_lower
    ):
        return 8192
    elif "claude" in model_lower:
        return 4096
    elif "gemini" in model_lower:
        return 8192
    elif "o1" in model_lower or "o3" in model_lower:
        return 32768
    # A model the table doesn't know — usually a local or gateway model. Its
    # limit can't be read from the name, so the admin's LLM_MAX_OUTPUT_TOKENS
    # caps it when set; otherwise the user's own max-tokens setting decides.
    return settings.llm_max_output_tokens or UNKNOWN_MODEL_MAX_TOKENS


def provider_of_model(model: str | None) -> str:
    """Which provider family a model NAME belongs to ("anthropic"/"google"/"openai").

    Used only to pick a client; the allowlist check always keys on the client
    that actually carries the request, never on this.
    """
    return model_family(model)


async def call_llm(
    openai_client,
    anthropic_client,
    gemini_model,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float = 0.1,
    history: list[dict[str, str]] | None = None,
) -> str:
    """Call the provider that serves ``model`` and return the response text.

    ``history`` holds earlier turns of the conversation (``{"role", "content"}``,
    oldest first, starting with the user); they are sent before ``user_prompt``.

    The model name picks the provider and the request never silently moves to
    a different one: a Claude model needs an Anthropic client, a Gemini model
    a Google key. The only exception is a custom OpenAI-compatible endpoint
    (OPENAI_BASE_URL — e.g. a gateway or local server that itself serves a
    model named "claude-…"), which is used as configured.

    Every branch is checked against the AI provider allowlist using the client
    that will carry the request. Raises ValueError (with a message fit to show
    the user) when no suitable client is configured or the provider is not
    allowed.
    """
    family = provider_of_model(model)
    turns = _alternating(history or [])

    if family == "anthropic" and anthropic_client:
        enforce_provider_allowed("anthropic", "AI request")
        response = await anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=_alternating([*turns, {"role": "user", "content": user_prompt}]),
        )
        return response.content[0].text
    if family == "google" and gemini_model:
        enforce_provider_allowed("google", "AI request")
        transcript = "\n\n".join(
            f"{'User' if t['role'] == 'user' else 'Assistant'}: {t['content']}" for t in turns
        )
        earlier = f"CONVERSATION SO FAR:\n{transcript}\n\n" if transcript else ""
        full_prompt = f"{system_prompt}\n\n{earlier}{user_prompt}"
        response = await asyncio.to_thread(gemini_model.generate_content, full_prompt)
        return response.text

    if family != "openai":
        # No native client for the selected model. Sending a "claude-…" /
        # "gemini-…" request to api.openai.com would only fail there — and
        # would route the user's text to a provider they did not choose.
        on_gateway = openai_client is not None and openai_client_provider(openai_client) != "openai"
        if not on_gateway:
            needed = "an Anthropic" if family == "anthropic" else "a Google (Gemini)"
            raise ValueError(
                f"The selected model '{model}' needs {needed} API key, and none is "
                "configured. Add the key in Settings, or choose a model from a "
                "provider you have a key for."
            )

    if openai_client:
        enforce_openai_client(openai_client, "AI request")
        response = await openai_chat(
            openai_client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                *turns,
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content
    raise ValueError(
        "No LLM configured. Please provide an API key in Settings, "
        "or set OPENAI_API_KEY or ANTHROPIC_API_KEY environment variable."
    )


def _alternating(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """Turns that start with the user and alternate roles (Anthropic requires it):
    leading assistant turns are dropped, consecutive same-role turns joined."""
    out: list[dict[str, str]] = []
    for t in turns:
        if t.get("role") not in ("user", "assistant") or not t.get("content"):
            continue
        if not out and t["role"] != "user":
            continue
        if out and out[-1]["role"] == t["role"]:
            out[-1] = {"role": t["role"], "content": f"{out[-1]['content']}\n\n{t['content']}"}
        else:
            out.append({"role": t["role"], "content": t["content"]})
    return out


_server_anthropic_client = None


def _server_anthropic():
    """The instance-level Anthropic client (ANTHROPIC_API_KEY), or None."""
    global _server_anthropic_client
    if _server_anthropic_client is None and settings.anthropic_api_key:
        from anthropic import AsyncAnthropic

        _server_anthropic_client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.api_timeout,
            max_retries=SDK_MAX_RETRIES,
        )
    return _server_anthropic_client


async def chosen_provider_text(
    prompt: str,
    *,
    user_keys: Optional["UserAPIKeys"] = None,
    model: str | None = None,
    max_tokens: int = 4096,
) -> str | None:
    """Run a utility prompt on the provider of the user's chosen chat model.

    The helper passes around the answer (routing, case-law screening, claim
    grounding, citation reasoning, per-file analysis) carry the user's question
    and document text. When the user selected a Claude or Gemini model, those
    passes must not quietly go to OpenAI instead — so they run on the chosen
    provider here.

    Returns the response text, or None when the chosen model is served by the
    OpenAI-compatible client (the caller then uses its normal OpenAI path) or
    when no client for the chosen provider is configured.
    """
    family = provider_of_model(model)
    if family == "anthropic":
        client = user_keys.get_async_anthropic_client() if user_keys else None
        client = client or _server_anthropic()
        if client is None:
            return None
        enforce_provider_allowed("anthropic", "AI request")
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    if family == "google":
        gemini = user_keys.get_google_model(model) if user_keys else None
        if gemini is None:
            return None
        enforce_provider_allowed("google", "AI request")
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        return response.text
    return None


_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def strip_json_fences(text: str | None) -> str:
    """Unwrap a ```json … ``` fence some models put around a JSON answer."""
    raw = text or ""
    match = _JSON_FENCE_RE.match(raw)
    return match.group(1) if match else raw
