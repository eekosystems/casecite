"""
Tests for the central OpenAI client factory (app/services/llm_clients.py).

Every client the app builds must carry a request timeout and bounded retries so
a hung upstream can never pin a request (or a worker thread) forever.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from unittest.mock import patch

import pytest
from app.config import settings
from app.services.llm_clients import SDK_MAX_RETRIES, make_openai


class TestMakeOpenAI:
    def test_sync_client_carries_timeout_and_bounded_retries(self):
        with patch.dict("app.services.llm_clients._runtime", {"base_url": None}):
            client = make_openai("sk-test-ci-dummy-key-not-real", async_=False)
        assert client.timeout == settings.api_timeout
        assert client.max_retries == SDK_MAX_RETRIES == 2

    def test_async_client_carries_timeout_and_bounded_retries(self):
        with patch.dict("app.services.llm_clients._runtime", {"base_url": None}):
            client = make_openai("sk-test-ci-dummy-key-not-real", async_=True)
        assert client.timeout == settings.api_timeout
        assert client.max_retries == 2

    def test_timeout_override_is_honored(self):
        with patch.dict("app.services.llm_clients._runtime", {"base_url": None}):
            client = make_openai("sk-test-ci-dummy-key-not-real", timeout=120.0, max_retries=0)
        assert client.timeout == 120.0
        assert client.max_retries == 0

    def test_custom_endpoint_keeps_bounds(self):
        with patch.dict(
            "app.services.llm_clients._runtime", {"base_url": "http://localhost:11434/v1"}
        ):
            client = make_openai(None, async_=True)
        assert str(client.base_url).startswith("http://localhost:11434/v1")
        assert client.timeout == settings.api_timeout
        assert client.max_retries == 2

    def test_no_key_and_no_endpoint_returns_none(self):
        with (
            patch.dict("app.services.llm_clients._runtime", {"base_url": None}),
            patch("app.services.llm_clients.settings") as mock_settings,
        ):
            mock_settings.openai_api_key = None
            assert make_openai(None) is None


class TestProviderAllowlist:
    """openai_chat / openai_chat_sync are the single exit for every
    OpenAI-compatible chat call (contract analysis, drafting,
    strategy, clause intelligence, document summaries, research chat), so the
    provider allowlist is enforced here."""

    @staticmethod
    def _client(base_url, sync=False):
        from unittest.mock import AsyncMock, MagicMock

        client = MagicMock()
        client.base_url = base_url
        client.chat.completions.create = (MagicMock if sync else AsyncMock)(return_value="ok")
        return client

    @staticmethod
    def _allowlist(*approved):
        from unittest.mock import patch

        ctx = patch("app.services.provider_policy.settings")
        mock_settings = ctx.start()
        mock_settings.hipaa_enforcement_enabled = True
        mock_settings.approved_ai_providers = list(approved)
        return ctx

    @pytest.mark.asyncio
    async def test_disallowed_provider_is_refused_before_anything_is_sent(self):
        from app.services.llm_clients import openai_chat
        from app.services.provider_policy import ProviderNotAllowedError

        client = self._client("https://api.openai.com/v1/")
        ctx = self._allowlist("anthropic", "self_hosted")
        try:
            with pytest.raises(ProviderNotAllowedError, match="'openai' is not approved"):
                await openai_chat(client, model="gpt-5.5", messages=[])
        finally:
            ctx.stop()
        client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_approved_self_hosted_endpoint_is_allowed(self):
        from app.services.llm_clients import openai_chat

        client = self._client("http://ollama:11434/v1")
        ctx = self._allowlist("self_hosted")
        try:
            assert await openai_chat(client, model="llama3.1", messages=[]) == "ok"
        finally:
            ctx.stop()

    def test_sync_variant_is_refused_too(self):
        from app.services.llm_clients import openai_chat_sync
        from app.services.provider_policy import ProviderNotAllowedError

        client = self._client("https://api.groq.com/openai/v1", sync=True)
        ctx = self._allowlist("openai")
        try:
            with pytest.raises(ProviderNotAllowedError, match="api.groq.com"):
                openai_chat_sync(client, model="llama", messages=[])
        finally:
            ctx.stop()
        client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_check_while_the_allowlist_is_off(self):
        from app.services.llm_clients import openai_chat

        client = self._client("https://api.groq.com/openai/v1")
        assert await openai_chat(client, model="llama", messages=[]) == "ok"


class TestModelSlots:
    """Chat, utility and research models resolve independently, and a chat
    request never names an OpenAI model on a custom (local) endpoint."""

    LOCAL = {
        "base_url": "http://localhost:11434/v1",
        "chat_model": "qwen3:27b",
        "utility_model": "qwen3:4b",
        "research_model": None,
    }

    def test_research_model_follows_chat_model_when_unset(self):
        from app.services.llm_clients import research_model

        with patch.dict("app.services.llm_clients._runtime", self.LOCAL):
            assert research_model() == "qwen3:27b"

    def test_research_model_is_independent_of_utility_model(self):
        from app.services.llm_clients import research_model, utility_model

        runtime = {**self.LOCAL, "research_model": "qwen3:32b"}
        with patch.dict("app.services.llm_clients._runtime", runtime):
            assert research_model() == "qwen3:32b"
            assert utility_model() == "qwen3:4b"

    def test_set_runtime_clears_research_model_with_empty_string(self):
        from app.services.llm_clients import research_model, set_runtime

        runtime = {**self.LOCAL, "research_model": "qwen3:32b"}
        with patch.dict("app.services.llm_clients._runtime", runtime):
            set_runtime(research_model=None)  # None leaves it unchanged
            assert research_model() == "qwen3:32b"
            set_runtime(research_model="")
            assert research_model() == "qwen3:27b"

    @pytest.mark.parametrize("requested", ["", None, "  "])
    def test_no_choice_uses_instance_chat_model(self, requested):
        from app.services.llm_clients import resolve_chat_model

        with patch.dict("app.services.llm_clients._runtime", self.LOCAL):
            assert resolve_chat_model(requested) == "qwen3:27b"
        with patch.dict(
            "app.services.llm_clients._runtime",
            {"base_url": None, "chat_model": "gpt-5.5"},
        ):
            assert resolve_chat_model(requested) == "gpt-5.5"

    @pytest.mark.parametrize("requested", ["gpt-5.5", "gpt-5.4-mini"])
    def test_openai_name_on_local_endpoint_uses_local_chat_model(self, requested):
        """The old default ("gpt-5.5") must not be sent to an Ollama server."""
        from app.services.llm_clients import resolve_chat_model

        with patch.dict("app.services.llm_clients._runtime", self.LOCAL):
            assert resolve_chat_model(requested) == "qwen3:27b"

    @pytest.mark.parametrize("requested", ["qwen3:27b", "qwen3:4b"])
    def test_endpoint_models_are_kept(self, requested):
        from app.services.llm_clients import resolve_chat_model

        with patch.dict("app.services.llm_clients._runtime", self.LOCAL):
            assert resolve_chat_model(requested) == requested

    @pytest.mark.parametrize("requested", ["claude-opus-4-8", "gemini-3.1-pro-preview"])
    def test_claude_and_gemini_choices_are_kept(self, requested):
        from app.services.llm_clients import resolve_chat_model

        with patch.dict("app.services.llm_clients._runtime", self.LOCAL):
            assert resolve_chat_model(requested) == requested

    def test_openai_choice_is_kept_without_custom_endpoint(self):
        from app.services.llm_clients import resolve_chat_model

        with patch.dict(
            "app.services.llm_clients._runtime",
            {"base_url": None, "chat_model": "gpt-5.5", "utility_model": "gpt-5.4-mini"},
        ):
            assert resolve_chat_model("gpt-5.4-mini") == "gpt-5.4-mini"


class TestUnknownModelOutputTokens:
    """A model the token table doesn't know (local, gateway) is not held to 4096."""

    def test_unknown_model_defers_to_the_users_setting(self):
        from app.services.rag.generation import UNKNOWN_MODEL_MAX_TOKENS, get_model_max_tokens

        with patch("app.services.rag.generation.settings") as mock_settings:
            mock_settings.llm_max_output_tokens = None
            assert get_model_max_tokens("qwen3:27b") == UNKNOWN_MODEL_MAX_TOKENS

    def test_admin_override_caps_unknown_models(self):
        from app.services.rag.generation import get_model_max_tokens

        with patch("app.services.rag.generation.settings") as mock_settings:
            mock_settings.llm_max_output_tokens = 2048
            assert get_model_max_tokens("qwen3:27b") == 2048
            assert get_model_max_tokens("gpt-5.5") == 32768  # known models unaffected
