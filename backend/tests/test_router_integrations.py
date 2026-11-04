"""HTTP-level tests for the admin integrations router (/api/v1/admin/integrations).

Instance-wide credentials: every route must be admin-only (``admin.settings``),
must never return a secret unmasked, and must audit each change.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.models.enums import UserRole
from app.services.audit import AuditEventType

from tests.helpers import headers, make_user

pytestmark = pytest.mark.usefixtures("no_rate_limit")

BASE = "/api/v1/admin/integrations"
ROUTER = "app.routers.integrations"

ROUTES = [
    ("get", "/courtlistener", None),
    ("post", "/courtlistener", {"api_token": "cl-token-0123456789"}),
    ("delete", "/courtlistener", None),
    ("get", "/connectors", None),
    ("post", "/connectors/google", {"values": {"google_client_id": "abc"}}),
    ("delete", "/connectors/google", None),
    ("get", "/local-llm", None),
    ("post", "/local-llm", {"base_url": "http://localhost:11434/v1", "chat_model": "llama3"}),
    ("delete", "/local-llm", None),
]


def _call(client, method, path, body, **kwargs):
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(f"{BASE}{path}", **kwargs)


@pytest.fixture
def admin():
    return make_user("int-admin", UserRole.ADMIN)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_requires_authentication(client, method, path, body):
    assert _call(client, method, path, body).status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_non_admin_is_forbidden(client, method, path, body):
    """An attorney has every workspace permission but not admin.settings."""
    attorney = make_user("int-attorney", UserRole.ATTORNEY)
    with (
        patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret,
        patch(f"{ROUTER}.delete_secret", new_callable=AsyncMock) as delete_secret,
    ):
        resp = _call(client, method, path, body, headers=headers(attorney))
    assert resp.status_code == 403
    set_secret.assert_not_awaited()
    delete_secret.assert_not_awaited()


class TestCourtListener:
    def test_status_masks_the_instance_token(self, client, admin):
        token = "cl-token-0123456789"
        with patch(f"{ROUTER}.get_secret", new_callable=AsyncMock, return_value=token):
            resp = client.get(f"{BASE}/courtlistener", headers=headers(admin))
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["source"] == "instance"
        assert token not in resp.text
        assert body["masked"].startswith("cl-t") and body["masked"].endswith("6789")

    def test_set_stores_applies_and_audits_without_the_token(self, client, admin, audit_events):
        token = "cl-token-0123456789"
        with (
            patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret,
            patch(f"{ROUTER}.apply_courtlistener_token") as apply,
        ):
            resp = client.post(
                f"{BASE}/courtlistener", json={"api_token": f"  {token}  "}, headers=headers(admin)
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "saved"
        # The save response carries the same status fields as GET, so the UI
        # can show "configured" straight away instead of "not configured".
        assert body["configured"] is True
        assert body["source"] == "instance"
        assert body["masked"].startswith("cl-t") and body["masked"].endswith("6789")
        assert token not in resp.text
        set_secret.assert_awaited_once_with("courtlistener_api_token", token)
        apply.assert_called_once_with(token)

        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["event_type"] == AuditEventType.SETTINGS_CHANGE
        assert event["user_id"] == admin.id
        assert event["details"] == {"action": "courtlistener_token_set"}
        assert token not in str(event)

    def test_short_token_is_rejected(self, client, admin):
        resp = client.post(
            f"{BASE}/courtlistener", json={"api_token": "short"}, headers=headers(admin)
        )
        assert resp.status_code == 422

    def test_clear_deletes_and_audits(self, client, admin, audit_events):
        with (
            patch(f"{ROUTER}.delete_secret", new_callable=AsyncMock) as delete_secret,
            patch(f"{ROUTER}.get_secret", new_callable=AsyncMock, return_value=None),
            patch(f"{ROUTER}.apply_courtlistener_token"),
            patch(
                f"{ROUTER}.settings", settings.model_copy(update={"courtlistener_api_token": None})
            ),
        ):
            resp = client.delete(f"{BASE}/courtlistener", headers=headers(admin))
        assert resp.status_code == 200
        # After clearing, the response states the effective (now empty) status.
        assert resp.json() == {
            "status": "cleared",
            "configured": False,
            "source": "none",
            "masked": None,
        }
        delete_secret.assert_awaited_once_with("courtlistener_api_token")
        assert [e["details"]["action"] for e in audit_events] == ["courtlistener_token_cleared"]


class TestConnectorCredentials:
    def test_unknown_provider_is_404(self, client, admin):
        resp = client.post(
            f"{BASE}/connectors/not-a-provider",
            json={"values": {"x": "y"}},
            headers=headers(admin),
        )
        assert resp.status_code == 404

    def test_unknown_fields_are_ignored(self, client, admin):
        """Only the provider's own settings fields may be written."""
        with patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret:
            resp = client.post(
                f"{BASE}/connectors/google",
                json={"values": {"secret_key": "overwrite-me", "debug": "true"}},
                headers=headers(admin),
            )
        assert resp.status_code == 400
        set_secret.assert_not_awaited()

    def test_oversized_value_is_rejected(self, client, admin):
        resp = client.post(
            f"{BASE}/connectors/google",
            json={"values": {"google_client_id": "x" * 2001}},
            headers=headers(admin),
        )
        assert resp.status_code == 422

    def test_saving_one_field_keeps_the_others(self, client, admin):
        """The UI sends only the fields the admin edited. A later save of one
        field must not wipe the stored (and live) values of the others; an
        explicit empty string clears just that field."""
        import json

        import app.services.instance_settings as inst
        from app.services import connector_credentials

        store: dict[str, str] = {}

        async def get_secret(name):
            return store.get(name)

        async def set_secret(name, value):
            store[name] = value

        async def delete_secret(name):
            store.pop(name, None)

        def save(values):
            return client.post(
                f"{BASE}/connectors/google", json={"values": values}, headers=headers(admin)
            )

        with (
            patch(f"{ROUTER}.set_secret", set_secret),
            patch(f"{ROUTER}.delete_secret", delete_secret),
            patch.object(inst, "get_secret", get_secret),
        ):
            try:
                first = save(
                    {
                        "google_client_id": "client-id-1234567890",
                        "google_client_secret": "secret-value-0123456789",
                    }
                )
                assert first.status_code == 200, first.text

                second = save({"google_api_key": "fake-picker-key"})
                assert second.status_code == 200, second.text
                assert json.loads(store["connector_credentials_google"]) == {
                    "google_client_id": "client-id-1234567890",
                    "google_client_secret": "secret-value-0123456789",
                    "google_api_key": "fake-picker-key",
                }
                assert connector_credentials.get("google_client_id") == "client-id-1234567890"
                assert connector_credentials.get("google_api_key") == "fake-picker-key"
                assert second.json()["masked"]["google_client_id"].startswith("clie")

                third = save({"google_client_secret": ""})
                assert third.status_code == 200, third.text
                assert json.loads(store["connector_credentials_google"]) == {
                    "google_client_id": "client-id-1234567890",
                    "google_api_key": "fake-picker-key",
                }
                assert connector_credentials.get(
                    "google_client_secret"
                ) == connector_credentials._ENV_DEFAULTS.get("google_client_secret")
                assert connector_credentials.get("google_client_id") == "client-id-1234567890"
            finally:
                connector_credentials.revert_to_env("google")


class TestLocalLLM:
    def test_set_applies_and_audits(self, client, admin, audit_events):
        with (
            patch(f"{ROUTER}.set_secret", new_callable=AsyncMock),
            patch(f"{ROUTER}.llm_clients.set_runtime") as set_runtime,
        ):
            resp = client.post(
                f"{BASE}/local-llm",
                json={"base_url": "http://localhost:11434/v1", "chat_model": "llama3"},
                headers=headers(admin),
            )
        assert resp.status_code == 200
        assert resp.json()["utility_model"] == "llama3"  # defaults to the chat model
        assert resp.json()["research_model"] is None  # deep research follows the chat model
        set_runtime.assert_called_once_with(
            base_url="http://localhost:11434/v1",
            models=[{"name": "llama3", "base_url": "http://localhost:11434/v1"}],
            chat_model="llama3",
            utility_model="llama3",
            research_model="",
            drafting_model="",
        )
        assert [e["details"]["action"] for e in audit_events] == ["local_llm_set"]

    def test_drafting_model_is_saved_and_must_be_listed(self, client, admin):
        models = [
            {"name": "muse-glimmer:30b", "base_url": "http://ollama:11434/v1"},
            {"name": "gemma4:31b", "base_url": "http://ollama:11434/v1"},
        ]
        with (
            patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret,
            patch(f"{ROUTER}.llm_clients.set_runtime") as set_runtime,
        ):
            resp = client.post(
                f"{BASE}/local-llm",
                json={
                    "models": models,
                    "chat_model": "muse-glimmer:30b",
                    "drafting_model": "gemma4:31b",
                },
                headers=headers(admin),
            )
            assert resp.status_code == 200
            assert resp.json()["drafting_model"] == "gemma4:31b"
            assert set_runtime.call_args.kwargs["drafting_model"] == "gemma4:31b"
            assert json.loads(set_secret.call_args.args[1])["drafting_model"] == "gemma4:31b"

            unknown = client.post(
                f"{BASE}/local-llm",
                json={"models": models, "chat_model": "muse-glimmer:30b", "drafting_model": "nope"},
                headers=headers(admin),
            )
        assert unknown.status_code == 422
        assert "drafting model" in unknown.text

    def test_models_on_several_servers_are_saved(self, client, admin, audit_events):
        models = [
            {"name": "qwen3:27b", "base_url": "http://ollama:11434/v1/"},
            {"name": "qwen3:4b", "base_url": "http://ollama:11434/v1"},
            {"name": "llama-3.3-70b", "base_url": "https://vllm.internal:8000/v1"},
        ]
        with (
            patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret,
            patch(f"{ROUTER}.llm_clients.set_runtime") as set_runtime,
        ):
            resp = client.post(
                f"{BASE}/local-llm",
                json={
                    "models": models,
                    "chat_model": "llama-3.3-70b",
                    "utility_model": "qwen3:4b",
                    "research_model": "qwen3:27b",
                },
                headers=headers(admin),
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The chat model's server is the endpoint clients are built for.
        assert body["base_url"] == "https://vllm.internal:8000/v1"
        assert [m["base_url"] for m in body["models"]] == [
            "http://ollama:11434/v1",
            "http://ollama:11434/v1",
            "https://vllm.internal:8000/v1",
        ]
        stored = json.loads(set_secret.call_args.args[1])
        assert stored["models"] == body["models"]
        assert set_runtime.call_args.kwargs["models"] == body["models"]
        assert audit_events[-1]["details"]["models"] == ["qwen3:27b", "qwen3:4b", "llama-3.3-70b"]

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (
                {
                    "models": [
                        {"name": "qwen3", "base_url": "http://a:11434/v1"},
                        {"name": "qwen3", "base_url": "http://b:11434/v1"},
                    ],
                    "chat_model": "qwen3",
                },
                "listed more than once",
            ),
            (
                {
                    "models": [{"name": "qwen3", "base_url": "http://a:11434/v1"}],
                    "chat_model": "qwen3",
                    "research_model": "llama3",
                },
                "research model 'llama3' is not in the model list",
            ),
            (
                {"models": [{"name": "qwen3", "base_url": "http://a:11434/v1"}], "chat_model": "x"},
                "chat model 'x' is not in the model list",
            ),
            ({"models": [], "chat_model": "qwen3"}, "at least one model"),
            ({"chat_model": "qwen3"}, "one server URL"),
            (
                {"models": [{"name": "qwen3", "base_url": "ftp://a/v1"}], "chat_model": "qwen3"},
                "http:// or https://",
            ),
        ],
    )
    def test_invalid_model_lists_are_rejected(self, client, admin, payload, message):
        with patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret:
            resp = client.post(f"{BASE}/local-llm", json=payload, headers=headers(admin))
        assert resp.status_code == 422
        assert message in resp.text
        set_secret.assert_not_awaited()

    def test_research_model_is_saved_separately(self, client, admin):
        with (
            patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret,
            patch(f"{ROUTER}.llm_clients.set_runtime") as set_runtime,
        ):
            resp = client.post(
                f"{BASE}/local-llm",
                json={
                    "base_url": "http://localhost:11434/v1",
                    "chat_model": "qwen3:27b",
                    "utility_model": "qwen3:4b",
                    "research_model": "qwen3:32b",
                },
                headers=headers(admin),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert (body["utility_model"], body["research_model"]) == ("qwen3:4b", "qwen3:32b")
        assert set_runtime.call_args.kwargs["research_model"] == "qwen3:32b"
        assert json.loads(set_secret.call_args.args[1])["research_model"] == "qwen3:32b"

    def test_status_reports_where_embeddings_go(self, client, admin):
        """Chat on a local server does not move embeddings; the admin sees where they go."""
        with patch(f"{ROUTER}._embeddings_provider", return_value="openai"):
            status = client.get(f"{BASE}/local-llm", headers=headers(admin)).json()
        assert status["embeddings_provider"] == "openai"

    def test_set_response_reports_configured_like_status(self, client, admin):
        """Right after saving, the response says the endpoint is configured — the
        same fields GET /local-llm returns, read from the applied runtime."""
        with patch(f"{ROUTER}.set_secret", new_callable=AsyncMock):
            try:
                resp = client.post(
                    f"{BASE}/local-llm",
                    json={"base_url": "http://localhost:11434/v1", "chat_model": "llama3"},
                    headers=headers(admin),
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["status"] == "saved"
                assert body["configured"] is True
                assert body["base_url"] == "http://localhost:11434/v1"
                assert body["chat_model"] == "llama3"
                assert body["models"] == [
                    {"name": "llama3", "base_url": "http://localhost:11434/v1"}
                ]
                status = client.get(f"{BASE}/local-llm", headers=headers(admin)).json()
                assert {k: body[k] for k in status} == status
            finally:
                from app.services import llm_clients

                llm_clients.set_runtime(
                    base_url=settings.openai_base_url or "",
                    chat_model=settings.openai_chat_model,
                    utility_model=settings.openai_utility_model,
                    research_model=settings.openai_research_model or "",
                )

    @pytest.mark.parametrize(
        ("env_base_url", "expected"),
        [("http://vllm.internal/v1", "http://vllm.internal/v1"), (None, "")],
    )
    def test_clear_reverts_to_env_endpoint(self, client, admin, env_base_url, expected):
        """Clearing the UI override restores OPENAI_BASE_URL from .env, not api.openai.com."""
        with (
            patch(f"{ROUTER}.delete_secret", new_callable=AsyncMock),
            patch(f"{ROUTER}.llm_clients.set_runtime") as set_runtime,
            patch(
                f"{ROUTER}.settings",
                settings.model_copy(update={"openai_base_url": env_base_url}),
            ),
        ):
            resp = client.delete(f"{BASE}/local-llm", headers=headers(admin))
        assert resp.status_code == 200
        assert set_runtime.call_args.kwargs["base_url"] == expected

    @pytest.mark.parametrize("base_url", ["file:///etc/passwd", "localhost:11434", "ftp://host/v1"])
    def test_non_http_base_url_is_rejected(self, client, admin, base_url):
        with patch(f"{ROUTER}.set_secret", new_callable=AsyncMock) as set_secret:
            resp = client.post(
                f"{BASE}/local-llm",
                json={"base_url": base_url, "chat_model": "llama3"},
                headers=headers(admin),
            )
        assert resp.status_code == 422
        set_secret.assert_not_awaited()
