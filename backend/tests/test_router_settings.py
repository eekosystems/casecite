"""
Integration tests for the Settings router (/api/v1/settings).
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests.helpers import headers, make_user

pytestmark = pytest.mark.usefixtures("no_rate_limit")


class TestSettingsRouter:
    """Tests for /api/v1/settings endpoints."""

    def test_get_rag_settings(self, client, auth_headers):
        """GET /api/v1/settings/rag with auth returns current RAG settings."""
        response = client.get("/api/v1/settings/rag", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "top_k" in data
        assert "similarity_threshold" in data

    def test_get_rag_settings_no_auth(self, client):
        """GET /api/v1/settings/rag without auth returns 401."""
        response = client.get("/api/v1/settings/rag")
        assert response.status_code == 401

    def test_update_rag_settings(self, client, auth_headers):
        """PUT /api/v1/settings/rag with auth updates settings and returns them."""
        new_settings = {
            "top_k": 8,
            "similarity_threshold": 0.6,
        }
        response = client.put("/api/v1/settings/rag", json=new_settings, headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["top_k"] == 8
        assert data["similarity_threshold"] == 0.6

    def test_update_merges_into_stored_settings(self, client, audit_events):
        """Fields the body omits keep their stored value; explicit null clears."""
        user = make_user("set-merge")
        url = "/api/v1/settings/rag"
        first = client.put(
            url,
            json={"contract_playbook": "Net 30 only.", "practice_area": "Employment"},
            headers=headers(user),
        )
        assert first.status_code == 200

        second = client.put(url, json={"top_k": 7}, headers=headers(user))
        assert second.status_code == 200
        assert second.json()["top_k"] == 7
        assert second.json()["contract_playbook"] == "Net 30 only."
        assert second.json()["practice_area"] == "Employment"

        stored = client.get(url, headers=headers(user)).json()
        assert stored["contract_playbook"] == "Net 30 only."

        cleared = client.put(url, json={"contract_playbook": None}, headers=headers(user))
        assert cleared.json()["contract_playbook"] is None
        assert cleared.json()["practice_area"] == "Employment"

        # The audit trail names the fields that changed, never their contents.
        changes = [
            e["details"] for e in audit_events if (e.get("details") or {}).get("changed_fields")
        ]
        assert changes[0] == {"changed_fields": ["contract_playbook", "practice_area"]}
        assert "Net 30" not in str(audit_events)


class TestPromptDefaults:
    """GET /settings/prompts/defaults — backs the "reset to default" buttons."""

    def test_requires_auth(self, client):
        assert client.get("/api/v1/settings/prompts/defaults").status_code == 401

    def test_returns_every_default_prompt(self, client, auth_headers):
        from app.services.rag.prompts import (
            get_default_factual_prompt,
            get_default_grounding_rules,
            get_default_mode_prompt,
            get_default_system_prompt,
        )

        response = client.get("/api/v1/settings/prompts/defaults", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()

        assert data["system_prompt"] == get_default_system_prompt()
        assert data["grounding_rules"] == get_default_grounding_rules()
        assert data["factual_prompt"] == get_default_factual_prompt()
        assert set(data["mode_prompts"]) == {
            "research",
            "case",
            "document",
            "compliance",
            "strategy",
        }
        for mode, prompt in data["mode_prompts"].items():
            assert prompt == get_default_mode_prompt(mode)
        # The defaults are real prompt text, not empty placeholders.
        assert len(data["system_prompt"]) > 50
        assert len(data["grounding_rules"]) > 20


class TestReindexAndClear:
    """Reindex / clear act on the CALLER's documents, so they follow the
    document permissions (upload / delete), not an instance-admin permission."""

    def test_viewer_cannot_reindex_or_clear(self, client, non_admin_headers):
        assert client.post("/api/v1/settings/reindex", headers=non_admin_headers).status_code == 403
        assert client.post("/api/v1/settings/clear", headers=non_admin_headers).status_code == 403

    def test_no_auth(self, client):
        assert client.post("/api/v1/settings/reindex").status_code == 401
        assert client.post("/api/v1/settings/clear").status_code == 401

    def test_attorney_reindexes_only_their_own_documents(self, client, audit_events):
        attorney = make_user("set-attorney")
        with patch(
            "app.services.diagnostics.reindex_user_documents",
            new_callable=AsyncMock,
            return_value={"status": "ok", "reindexed": 0},
        ) as reindex:
            response = client.post("/api/v1/settings/reindex", headers=headers(attorney))
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "reindexed": 0}
        reindex.assert_awaited_once_with(attorney.id)
        assert [e["details"]["action"] for e in audit_events] == ["reindex_started"]

    def test_clear_reports_the_number_cleared(self, client):
        attorney = make_user("set-attorney")
        with patch(
            "app.services.documents.document_service.clear_all",
            new_callable=AsyncMock,
            return_value={"cleared_documents": 3},
        ) as clear_all:
            response = client.post("/api/v1/settings/clear", headers=headers(attorney))
        assert response.status_code == 200
        assert response.json()["status"] == "cleared"
        assert response.json()["cleared_documents"] == 3
        clear_all.assert_awaited_once_with(attorney.id)

    def test_clear_failure_is_an_error_status_not_a_200(self, client):
        attorney = make_user("set-attorney")
        with patch(
            "app.services.documents.document_service.clear_all",
            new_callable=AsyncMock,
            side_effect=OSError("disk gone: /app/data/uploads"),
        ):
            response = client.post("/api/v1/settings/clear", headers=headers(attorney))
        assert response.status_code == 500
        assert "/app/data" not in response.text

    def test_partial_clear_is_reported_as_partial(self, client):
        attorney = make_user("set-attorney")
        with patch(
            "app.services.documents.document_service.clear_all",
            new_callable=AsyncMock,
            return_value={"cleared_documents": 2, "failed_documents": 1, "status": "partial"},
        ):
            response = client.post("/api/v1/settings/clear", headers=headers(attorney))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "partial"
        assert body["cleared_documents"] == 2
        assert body["failed_documents"] == 1
        assert "kept" in body["message"]

    def test_clear_that_removes_nothing_is_an_error(self, client):
        attorney = make_user("set-attorney")
        with patch(
            "app.services.documents.document_service.clear_all",
            new_callable=AsyncMock,
            return_value={"cleared_documents": 0, "failed_documents": 4, "status": "partial"},
        ):
            response = client.post("/api/v1/settings/clear", headers=headers(attorney))
        assert response.status_code == 502
        assert "Nothing was deleted" in response.json()["detail"]


class TestInstanceModels:
    """GET /settings/models — feeds the model picker for every user."""

    def test_requires_auth(self, client):
        assert client.get("/api/v1/settings/models").status_code == 401

    def test_reports_local_models_without_the_endpoint_address(self, client, auth_headers):
        runtime = {
            "base_url": "http://ollama.internal:11434/v1",
            "chat_model": "qwen3:27b",
            "utility_model": "qwen3:4b",
            "research_model": None,
        }
        with patch.dict("app.services.llm_clients._runtime", runtime):
            response = client.get("/api/v1/settings/models", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {
            "custom_endpoint": True,
            "chat_model": "qwen3:27b",
            "utility_model": "qwen3:4b",
            "research_model": "qwen3:27b",
        }
