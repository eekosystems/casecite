"""
Tests for persistent chat sessions: router (/chat/sessions), service tenant
isolation, and the chat flow persisting exchanges into sessions.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from unittest.mock import AsyncMock, patch

import pytest


def _create_session(client, headers, title="Lease review questions"):
    response = client.post("/api/v1/chat/sessions", json={"title": title}, headers=headers)
    assert response.status_code == 200
    return response.json()


class TestChatSessionsRouter:
    """Tests for /chat/sessions endpoints."""

    def test_list_requires_auth(self, client):
        response = client.get("/api/v1/chat/sessions")
        assert response.status_code in [401, 403]

    def test_create_requires_auth(self, client):
        response = client.post("/api/v1/chat/sessions", json={"title": "x"})
        assert response.status_code in [401, 403]

    def test_create_and_get_roundtrip(self, client, auth_headers):
        """POST create, then GET the full thread back."""
        created = _create_session(client, auth_headers, title="Indemnification survey")
        assert created["title"] == "Indemnification survey"
        assert created["id"]

        response = client.get(f"/api/v1/chat/sessions/{created['id']}", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == created["id"]
        assert data["title"] == "Indemnification survey"
        assert data["messages"] == []

    def test_created_session_appears_in_list_newest_first(self, client, auth_headers):
        older = _create_session(client, auth_headers, title="Older thread")
        newer = _create_session(client, auth_headers, title="Newer thread")

        response = client.get("/api/v1/chat/sessions", headers=auth_headers)
        assert response.status_code == 200
        sessions = response.json()["sessions"]
        ids = [s["id"] for s in sessions]
        assert older["id"] in ids
        assert newer["id"] in ids
        # Newest first: the session created last must come before the older one.
        assert ids.index(newer["id"]) < ids.index(older["id"])

    def test_get_unknown_session_404(self, client, auth_headers):
        response = client.get(
            "/api/v1/chat/sessions/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_delete_session(self, client, auth_headers):
        created = _create_session(client, auth_headers)
        response = client.delete(f"/api/v1/chat/sessions/{created['id']}", headers=auth_headers)
        assert response.status_code == 200

        response = client.get(f"/api/v1/chat/sessions/{created['id']}", headers=auth_headers)
        assert response.status_code == 404

    def test_delete_unknown_session_404(self, client, auth_headers):
        response = client.delete(
            "/api/v1/chat/sessions/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestChatSessionTenantIsolation:
    """User B must never be able to read, write, or delete user A's sessions."""

    def test_other_user_cannot_read_session(self, client, auth_headers, non_admin_headers):
        created = _create_session(client, auth_headers, title="User A private thread")

        response = client.get(f"/api/v1/chat/sessions/{created['id']}", headers=non_admin_headers)
        assert response.status_code == 404

    def test_other_user_cannot_delete_session(self, client, auth_headers, non_admin_headers):
        created = _create_session(client, auth_headers, title="User A keeps this")

        response = client.delete(
            f"/api/v1/chat/sessions/{created['id']}", headers=non_admin_headers
        )
        assert response.status_code == 404

        # Still visible to its owner.
        response = client.get(f"/api/v1/chat/sessions/{created['id']}", headers=auth_headers)
        assert response.status_code == 200

    def test_other_user_sessions_not_listed(self, client, auth_headers, non_admin_headers):
        created = _create_session(client, auth_headers, title="User A only in A list")

        response = client.get("/api/v1/chat/sessions", headers=non_admin_headers)
        assert response.status_code == 200
        ids = [s["id"] for s in response.json()["sessions"]]
        assert created["id"] not in ids

    async def test_append_to_foreign_session_raises_lookup_error(self, client, auth_headers):
        """Service level: append_message must refuse sessions the user doesn't own."""
        from app.services import chat_sessions as svc

        created = _create_session(client, auth_headers, title="Owned by test-user-123")

        with pytest.raises(LookupError):
            await svc.append_message(
                created["id"], "some-other-user", role="user", content="intruding"
            )


class TestChatServiceRoundtrip:
    """Service-level create + append + list + get roundtrip."""

    async def test_roundtrip(self, client):
        # `client` ensures app startup ran create_all before direct service calls.
        from app.services import chat_sessions as svc

        user_id = "roundtrip-user-1"
        session = await svc.create_session(user_id, "Auto-renewal clauses")

        await svc.append_message(session["id"], user_id, role="user", content="Which renew?")
        await svc.append_message(
            session["id"],
            user_id,
            role="assistant",
            content="Two agreements auto-renew.",
            citations=[{"id": "c1", "source": "lease.pdf"}],
            strategy=None,
            stats={"docs_searched": 3},
        )

        sessions = await svc.list_sessions(user_id)
        assert sessions[0]["id"] == session["id"]
        assert sessions[0]["message_count"] == 2

        thread = await svc.get_session_with_messages(session["id"], user_id)
        assert thread is not None
        assert [m["role"] for m in thread["messages"]] == ["user", "assistant"]
        assert thread["messages"][1]["citations"] == [{"id": "c1", "source": "lease.pdf"}]
        assert thread["messages"][1]["stats"] == {"docs_searched": 3}

        assert await svc.get_session_with_messages(session["id"], "someone-else") is None


class TestChatFlowPersistence:
    """POST /chat persists the exchange and returns session_id."""

    def _mock_result(self):
        return {
            "content": "This is a persisted legal response.",
            "citations": [],
            "stats": {
                "docs_searched": 4,
                "chunks_retrieved": 2,
                "processing_time": "0.4s",
            },
        }

    def test_chat_response_carries_session_id(self, app, client, auth_headers):
        with (
            patch(
                "app.routers.chat.rag_service.generate_response",
                new_callable=AsyncMock,
                return_value=self._mock_result(),
            ),
            patch(
                "app.routers.chat.route_query",
                new_callable=AsyncMock,
                return_value=("ask", False, None, False),
            ),
        ):
            response = client.post(
                "/api/v1/chat",
                json={"query": "Do our leases allow subletting?", "mode": "research"},
                headers=auth_headers,
            )
            assert response.status_code == 200
            data = response.json()
            assert data["session_id"]

            # The persisted thread holds the user + assistant exchange.
            thread = client.get(f"/api/v1/chat/sessions/{data['session_id']}", headers=auth_headers)
            assert thread.status_code == 200
            messages = thread.json()["messages"]
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[0]["content"] == "Do our leases allow subletting?"
            assert messages[1]["content"] == "This is a persisted legal response."

            # A follow-up with session_id appends to the same thread.
            response2 = client.post(
                "/api/v1/chat",
                json={
                    "query": "And what notice is required?",
                    "mode": "research",
                    "session_id": data["session_id"],
                },
                headers=auth_headers,
            )
            assert response2.status_code == 200
            assert response2.json()["session_id"] == data["session_id"]

            thread2 = client.get(
                f"/api/v1/chat/sessions/{data['session_id']}", headers=auth_headers
            )
            assert len(thread2.json()["messages"]) == 4

    def test_persistence_failure_never_fails_chat(self, app, client, auth_headers):
        """If session storage errors, the chat response still succeeds (session_id None)."""
        with (
            patch(
                "app.routers.chat.rag_service.generate_response",
                new_callable=AsyncMock,
                return_value=self._mock_result(),
            ),
            patch(
                "app.routers.chat.route_query",
                new_callable=AsyncMock,
                return_value=("ask", False, None, False),
            ),
            patch(
                "app.routers.chat.chat_session_service.create_session",
                new_callable=AsyncMock,
                side_effect=RuntimeError("storage down"),
            ),
        ):
            response = client.post(
                "/api/v1/chat",
                json={"query": "Still answer me please", "mode": "research"},
                headers=auth_headers,
            )
            assert response.status_code == 200
            data = response.json()
            assert data["content"] == "This is a persisted legal response."
            assert data["session_id"] is None
