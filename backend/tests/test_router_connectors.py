"""
Integration tests for Connectors router (/connectors).
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import httpx
import pytest
from app.models.schemas import ConnectorType, Document, DocumentStatus
from app.services.audit import AuditEventType
from app.services.connectors.base import ConnectorError, FileInfo

PDF = b"%PDF-1.4 fake pdf content"
MODIFIED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _file(file_id: str, name: str | None = None, size: int = len(PDF)) -> FileInfo:
    return FileInfo(
        id=file_id,
        name=name or f"{file_id}.pdf",
        mime_type="application/pdf",
        size=size,
        modified_at=MODIFIED,
        metadata={},
    )


def _doc(doc_id: str, source_id: str, metadata: dict | None = None, **overrides) -> Document:
    fields = {
        "id": doc_id,
        "user_id": "user-1",
        "filename": f"{source_id}.pdf",
        "content_type": "application/pdf",
        "size": len(PDF),
        "source": ConnectorType.BOX,
        "source_id": source_id,
        "status": DocumentStatus.INDEXED,
        "created_at": MODIFIED,
        "metadata": metadata or {},
    }
    fields.update(overrides)
    return Document(**fields)


class _FakeConnector:
    """Stands in for a provider connector in run_sync tests."""

    supports_folder_sync = True
    requires_admin = False

    def __init__(self, files, failing=None, crawl_error=None):
        self.files = files
        self.failing = failing or {}
        self.crawl_error = crawl_error
        self.crawled_folder = "unset"
        self.downloaded: list[str] = []
        self.credentials: dict = {}
        self.saved = False

    async def crawl_all_files(self, folder_id=None, errors=None):
        self.crawled_folder = folder_id
        if self.crawl_error:
            raise self.crawl_error
        for file_info in self.files:
            yield file_info

    async def download_file(self, file_id):
        self.downloaded.append(file_id)
        if file_id in self.failing:
            raise self.failing[file_id]
        return PDF

    def _save_credentials(self):
        self.saved = True


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://provider.test/secret-path?sig=abc")
    return httpx.HTTPStatusError(
        f"{status} for url {request.url}", request=request, response=httpx.Response(status)
    )


@pytest.fixture
def sync_env():
    """Patch run_sync's collaborators; yields (upload mock, audit mock, documents dict)."""
    documents: dict[str, Document] = {}

    async def fake_upload(**kwargs):
        doc = _doc(f"doc-{kwargs['source_id']}", kwargs["source_id"], kwargs["metadata"])
        documents[doc.id] = doc
        return doc

    with (
        patch(
            "app.routers.connectors.document_service.upload_and_index",
            new=AsyncMock(side_effect=fake_upload),
        ) as upload,
        patch("app.routers.connectors.document_service.documents", documents),
        patch("app.routers.connectors.audit_service.log_event", new=AsyncMock()) as audit,
        patch("app.routers.connectors.UserAPIKeys.for_user", return_value="USER-KEYS") as for_user,
    ):
        yield SimpleNamespace(upload=upload, audit=audit, documents=documents, for_user=for_user)


def _events(audit, event_type):
    return [c.kwargs for c in audit.call_args_list if c.kwargs.get("event_type") == event_type]


class TestRunSync:
    """run_sync: scoping, per-file isolation, validation, audit, idempotency."""

    async def _run(self, connector, folder_id="folder-1"):
        from app.routers.connectors import run_sync

        return await run_sync(
            connector=connector,
            connector_type=ConnectorType.BOX,
            folder_id=folder_id,
            user_id="user-1",
            user_email="u@example.com",
        )

    async def test_crawl_is_scoped_to_the_folder(self, sync_env):
        connector = _FakeConnector([_file("a")])
        await self._run(connector, folder_id="matter-42")
        assert connector.crawled_folder == "matter-42"

    async def test_imports_with_byok_keys_and_audits_each_document(self, sync_env):
        connector = _FakeConnector([_file("a"), _file("b")])
        result = await self._run(connector)

        assert result["status"] == "completed"
        assert result["docs_processed"] == 2
        assert result["errors_count"] == 0
        sync_env.for_user.assert_called_once_with("user-1")
        for call in sync_env.upload.call_args_list:
            assert call.kwargs["user_keys"] == "USER-KEYS"
            assert call.kwargs["user_id"] == "user-1"

        uploads = _events(sync_env.audit, AuditEventType.DOCUMENT_UPLOAD)
        assert [e["resource_id"] for e in uploads] == ["doc-a", "doc-b"]
        assert uploads[0]["details"]["source_id"] == "a"
        assert len(_events(sync_env.audit, AuditEventType.CONNECTOR_SYNC_COMPLETE)) == 1
        assert connector.saved is True

    async def test_one_failed_download_does_not_abort_the_sync(self, sync_env):
        connector = _FakeConnector(
            [_file("a"), _file("b"), _file("c")], failing={"b": _http_error(403)}
        )
        result = await self._run(connector)

        assert result["status"] == "completed"
        assert result["docs_processed"] == 2
        assert result["errors_count"] == 1
        assert connector.downloaded == ["a", "b", "c"]
        # The reason is reported per file, without the provider URL or its signature.
        assert result["errors"] == [{"name": "b.pdf", "reason": "provider returned HTTP 403"}]

    async def test_rate_limited_download_waits_out_retry_after(self, sync_env):
        """A 429 with Retry-After is retried after the (bounded) wait, not failed."""
        throttled = _http_error(429)
        throttled.response.headers["Retry-After"] = "7"
        connector = _FakeConnector([_file("a")], failing={"a": throttled})
        attempts = {"n": 0}
        original = connector.download_file

        async def flaky(file_id):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return await original(file_id)  # raises the 429 once
            return PDF

        connector.download_file = flaky
        with patch("app.services.connectors.base.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await self._run(connector)

        assert result["docs_processed"] == 1
        assert result["errors_count"] == 0
        assert attempts["n"] == 2
        sleep.assert_awaited_once_with(7.0)

    async def test_retry_after_is_bounded_and_eventually_reported(self, sync_env):
        """An absurd Retry-After is capped; a file still throttled after the
        bounded retries is recorded as that file's error — the sync goes on."""
        throttled = _http_error(429)
        throttled.response.headers["Retry-After"] = "86400"
        connector = _FakeConnector([_file("a"), _file("b")], failing={"a": throttled})
        with patch("app.services.connectors.base.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await self._run(connector)

        assert all(call.args[0] <= 30.0 for call in sleep.await_args_list)
        assert result["docs_processed"] == 1
        assert result["errors"] == [{"name": "a.pdf", "reason": "provider returned HTTP 429"}]

    async def test_error_list_is_capped_but_count_is_complete(self, sync_env):
        files = [_file(f"f{i}") for i in range(25)]
        # Alternate failures so the consecutive-failure brake never trips.
        failing = {f.id: _http_error(404) for i, f in enumerate(files) if i % 2 == 0}
        ok = [_file(f"g{i}") for i in range(25)]
        interleaved = [x for pair in zip(files, ok, strict=True) for x in pair]
        connector = _FakeConnector(interleaved, failing=failing)
        result = await self._run(connector)

        assert result["errors_count"] == 13
        assert len(result["errors"]) == 13
        assert result["errors"][0] == {"name": "f0.pdf", "reason": "provider returned HTTP 404"}

    async def test_skipped_subfolder_errors_from_the_crawl_are_reported(self, sync_env):
        """The crawl appends {"name", "reason"} for folders it could not list."""

        class _CrawlWithFolderError(_FakeConnector):
            async def crawl_all_files(self, folder_id=None, errors=None):
                errors.append({"name": "Restricted/", "reason": "folder skipped: HTTP 403"})
                for file_info in self.files:
                    yield file_info

        result = await self._run(_CrawlWithFolderError([_file("a")]))
        assert result["docs_processed"] == 1
        assert result["errors_count"] == 1
        assert result["errors"] == [{"name": "Restricted/", "reason": "folder skipped: HTTP 403"}]

    async def test_disallowed_content_is_rejected_per_file(self, sync_env):
        """Same magic-byte validation as a direct upload: a mislabelled file is skipped."""
        connector = _FakeConnector([_file("a"), _file("evil", name="evil.pdf")])
        connector.download_file = AsyncMock(side_effect=[PDF, b"MZ\x90\x00\x03\x00\xff\xfe binary"])
        result = await self._run(connector)

        assert result["docs_processed"] == 1
        assert result["errors_count"] == 1
        assert sync_env.upload.call_count == 1

    async def test_filename_is_sanitized(self, sync_env):
        connector = _FakeConnector([_file("a", name="../../etc/pass:wd.pdf")])
        await self._run(connector)
        assert sync_env.upload.call_args.kwargs["filename"] == "pass_wd.pdf"

    async def test_oversized_file_is_never_downloaded(self, sync_env):
        from app.config import settings

        connector = _FakeConnector([_file("big", size=settings.max_upload_size + 1), _file("a")])
        result = await self._run(connector)

        assert connector.downloaded == ["a"]
        assert result["docs_processed"] == 1
        assert result["errors"][0]["name"] == "big.pdf"
        assert "upload limit" in result["errors"][0]["reason"]

    async def test_unchanged_files_are_skipped_on_rerun(self, sync_env):
        """A retried or repeated sync does not re-download finished files."""
        first = _FakeConnector([_file("a"), _file("b")])
        await self._run(first)

        second = _FakeConnector([_file("a"), _file("b"), _file("c")])
        result = await self._run(second)

        assert second.downloaded == ["c"]
        assert result["docs_processed"] == 1
        assert result["docs_skipped"] == 2

    async def test_changed_file_is_reimported(self, sync_env):
        await self._run(_FakeConnector([_file("a")]))

        changed = _file("a")
        changed.modified_at = datetime(2026, 9, 2, tzinfo=UTC)
        second = _FakeConnector([changed])
        result = await self._run(second)

        assert second.downloaded == ["a"]
        assert result["docs_skipped"] == 0

    async def test_failed_index_is_reported_not_counted(self, sync_env):
        failed = _doc("doc-a", "a", {"error": "No embedding key"}, status=DocumentStatus.FAILED)
        sync_env.upload.side_effect = None
        sync_env.upload.return_value = failed

        result = await self._run(_FakeConnector([_file("a")]))

        assert result["docs_processed"] == 0
        assert result["errors"] == [{"name": "a.pdf", "reason": "No embedding key"}]
        assert _events(sync_env.audit, AuditEventType.DOCUMENT_UPLOAD) == []

    async def test_crawl_failure_is_audited_and_raised(self, sync_env):
        connector = _FakeConnector([], crawl_error=_http_error(429))
        with pytest.raises(httpx.HTTPStatusError):
            await self._run(connector)

        failures = _events(sync_env.audit, AuditEventType.CONNECTOR_SYNC_FAILURE)
        assert len(failures) == 1
        assert failures[0]["success"] is False
        assert failures[0]["error"] == "provider returned HTTP 429"
        assert "sig=abc" not in str(failures[0])

    async def test_lost_grant_is_audited(self, sync_env):
        """A connector-level error (not in the old except tuple) is audited too."""
        connector = _FakeConnector([], crawl_error=ConnectorError("Failed to refresh token"))
        with pytest.raises(ConnectorError):
            await self._run(connector)
        assert len(_events(sync_env.audit, AuditEventType.CONNECTOR_SYNC_FAILURE)) == 1

    async def test_repeated_failures_stop_the_sync(self, sync_env):
        files = [_file(f"f{i}") for i in range(15)]
        connector = _FakeConnector(files, failing={f.id: _http_error(429) for f in files})
        with (
            patch("app.services.connectors.base.asyncio.sleep", new=AsyncMock()),
            pytest.raises(ConnectorError, match="consecutive failures"),
        ):
            await self._run(connector)

        # Ten distinct files were attempted (each a bounded number of times).
        assert len(dict.fromkeys(connector.downloaded)) == 10
        assert len(_events(sync_env.audit, AuditEventType.CONNECTOR_SYNC_FAILURE)) == 1


def _attorney_headers() -> dict:
    """A user with connectors.manage but without admin.settings."""
    from app.services.auth import User, UserRole

    from tests.conftest import make_auth_headers

    return make_auth_headers(
        User(
            id="test-attorney-789",
            email="attorney@casecite.legal",
            name="Test Attorney",
            roles=[UserRole.ATTORNEY],
            last_login=datetime.now(UTC),
        )
    )


def _connected(connector_cls):
    return patch.object(connector_cls, "is_connected", new_callable=PropertyMock, return_value=True)


class TestConnectorsRouter:
    """Tests for /connectors endpoints."""

    def test_list_connectors(self, client, auth_headers):
        """GET /connectors returns connector list."""
        response = client.get("/api/v1/connectors", headers=auth_headers)
        assert response.status_code == 200
        types = {c["type"] for c in response.json()["connectors"]}
        assert {"google_drive", "onedrive", "box", "dropbox", "clio", "filevine"} <= types

    def test_list_connectors_no_auth(self, client):
        """GET /connectors without auth returns 401/403."""
        response = client.get("/api/v1/connectors")
        assert response.status_code in [401, 403]

    def test_connector_status(self, client, auth_headers):
        """GET /connectors/{type}/status reports an unconnected connector."""
        response = client.get("/api/v1/connectors/google_drive/status", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["connected"] is False

    def test_disconnect_no_auth(self, client):
        """POST /connectors/{type}/disconnect without auth returns 401/403."""
        response = client.post("/api/v1/connectors/google_drive/disconnect")
        assert response.status_code in [401, 403]

    def test_sync_status_unknown_id_is_404(self, client, auth_headers):
        response = client.get("/api/v1/connectors/box/sync/no-such-job", headers=auth_headers)
        assert response.status_code == 404


class TestSyncScope:
    """POST /connectors/{type}/sync never imports a whole account by default."""

    def _post(self, client, headers, connector="box", **kwargs):
        from app.services.connectors.box import BoxConnector

        with (
            _connected(BoxConnector),
            patch(
                "app.routers.connectors.job_manager.submit", new=AsyncMock(return_value="job-1")
            ) as submit,
            patch("app.routers.connectors.run_sync", new=AsyncMock()) as run_sync,
            patch("app.routers.connectors.audit_service.log_event", new=AsyncMock()),
        ):
            response = client.post(
                f"/api/v1/connectors/{connector}/sync", headers=headers, **kwargs
            )
            folder = None
            if submit.called:
                # Run the submitted job (run_sync is mocked) to see its scope.
                asyncio.run(submit.call_args.args[0]())
                folder = run_sync.call_args.kwargs["folder_id"]
            return response, submit, folder

    def test_no_folder_and_no_sync_all_is_refused(self, client, auth_headers):
        response, submit, _ = self._post(client, auth_headers, json={})
        assert response.status_code == 400
        assert "sync_all" in response.json()["detail"]
        submit.assert_not_called()

    def test_no_body_is_refused(self, client, auth_headers):
        response, submit, _ = self._post(client, auth_headers)
        assert response.status_code == 400
        submit.assert_not_called()

    def test_explicit_sync_all_is_accepted(self, client, auth_headers):
        response, submit, folder = self._post(client, auth_headers, json={"sync_all": True})
        assert response.status_code == 200
        assert response.json() == {"sync_id": "job-1", "status": "started"}
        submit.assert_called_once()
        assert folder is None

    def test_folder_in_body_reaches_the_sync(self, client, auth_headers):
        response, _, folder = self._post(client, auth_headers, json={"folder_id": "matter-42"})
        assert response.status_code == 200
        assert folder == "matter-42"

    def test_folder_in_query_reaches_the_sync(self, client, auth_headers):
        response, _, folder = self._post(client, auth_headers, params={"folder_id": "matter-7"})
        assert response.status_code == 200
        assert folder == "matter-7"

    def test_folder_wins_over_sync_all(self, client, auth_headers):
        """Both given: the narrower scope is the one that runs."""
        response, _, folder = self._post(
            client, auth_headers, json={"folder_id": "matter-42", "sync_all": True}
        )
        assert response.status_code == 200
        assert folder == "matter-42"

    def test_control_characters_in_folder_are_refused(self, client, auth_headers):
        response, submit, _ = self._post(client, auth_headers, json={"folder_id": "a\nb"})
        assert response.status_code == 400
        submit.assert_not_called()

    def test_flat_connector_refuses_a_folder(self, client, auth_headers):
        """Clio cannot scope to a folder: say so instead of crawling everything."""
        from app.services.connectors.clio import ClioConnector

        with (
            _connected(ClioConnector),
            patch("app.routers.connectors.job_manager.submit", new=AsyncMock()) as submit,
        ):
            response = client.post(
                "/api/v1/connectors/clio/sync", headers=auth_headers, json={"folder_id": "123"}
            )
        assert response.status_code == 400
        assert "cannot sync a single folder" in response.json()["detail"]
        submit.assert_not_called()

    def test_not_connected_is_refused(self, client, auth_headers):
        response = client.post(
            "/api/v1/connectors/box/sync", headers=auth_headers, json={"sync_all": True}
        )
        assert response.status_code == 400


class TestFilevineAdminGate:
    """The firm-wide Filevine key is usable only by an administrator."""

    FAKE = SimpleNamespace(
        filevine_enabled=True,
        filevine_api_key="k",
        filevine_api_secret="s",
        filevine_base_url="https://api.filevine.io",
    )

    def test_non_admin_cannot_sync(self, client):
        with (
            patch("app.services.connectors.filevine.settings", self.FAKE),
            patch("app.routers.connectors.job_manager.submit", new=AsyncMock()) as submit,
        ):
            response = client.post(
                "/api/v1/connectors/filevine/sync",
                headers=_attorney_headers(),
                json={"sync_all": True},
            )
        assert response.status_code == 403
        assert "administrator" in response.json()["detail"]
        submit.assert_not_called()

    def test_non_admin_cannot_start_auth_or_disconnect(self, client):
        headers = _attorney_headers()
        with patch("app.services.connectors.filevine.settings", self.FAKE):
            assert (
                client.get("/api/v1/connectors/filevine/auth", headers=headers).status_code == 403
            )
            assert (
                client.post("/api/v1/connectors/filevine/disconnect", headers=headers).status_code
                == 403
            )

    def test_non_admin_sees_it_as_not_connected(self, client):
        """...and no Filevine session is opened on their behalf to find out."""
        from app.services.connectors.filevine import FilevineConnector

        headers = _attorney_headers()
        with (
            patch("app.services.connectors.filevine.settings", self.FAKE),
            patch.object(FilevineConnector, "get_status", new=AsyncMock()) as get_status,
        ):
            listing = client.get("/api/v1/connectors", headers=headers)
            status = client.get("/api/v1/connectors/filevine/status", headers=headers)

        assert listing.status_code == 200
        filevine = next(c for c in listing.json()["connectors"] if c["type"] == "filevine")
        assert filevine["connected"] is False
        assert filevine["admin_only"] is True
        assert status.json()["connected"] is False
        get_status.assert_not_called()

    def test_admin_can_sync(self, client, auth_headers):
        with (
            patch("app.services.connectors.filevine.settings", self.FAKE),
            patch("app.routers.connectors.job_manager.submit", new=AsyncMock(return_value="job-9")),
            patch("app.routers.connectors.audit_service.log_event", new=AsyncMock()),
        ):
            response = client.post(
                "/api/v1/connectors/filevine/sync",
                headers=auth_headers,
                json={"folder_id": "project-1"},
            )
        assert response.status_code == 200
        assert response.json()["sync_id"] == "job-9"


class TestOAuthCallback:
    """The callback turns provider failures into a clean 400, and only the
    browser that started the flow may complete it."""

    CALLBACK = "/api/v1/connectors/box/callback"

    def _bind(self, client, state="state-xyz", user_id="test-user-123", cookie_user=None):
        """Issue *state* for *user_id* and give the client the binding cookie
        (for *cookie_user*, defaulting to the same user)."""
        from app.services.connectors.sync_state import (
            OAUTH_BINDING_COOKIE,
            oauth_binding_token,
            store_oauth_state,
        )

        store_oauth_state(state, user_id, ConnectorType.BOX)
        client.cookies.clear()
        client.cookies.set(OAUTH_BINDING_COOKIE, oauth_binding_token(state, cookie_user or user_id))

    def _callback(self, client, error):
        from app.services.connectors.box import BoxConnector

        self._bind(client)
        with (
            patch.object(BoxConnector, "handle_callback", new=AsyncMock(side_effect=error)),
            patch("app.routers.connectors.audit_service.log_event", new=AsyncMock()) as audit,
        ):
            response = client.get(self.CALLBACK, params={"code": "c", "state": "state-xyz"})
        return response, audit

    def test_provider_http_error_is_a_400(self, client):
        response, audit = self._callback(client, _http_error(400))
        assert response.status_code == 400
        assert response.json()["detail"] == "Failed to connect. Please try again."
        assert _events(audit, AuditEventType.CONNECTOR_CONNECT) == []

    def test_connector_error_is_a_400(self, client):
        response, _ = self._callback(client, ConnectorError("Not authenticated"))
        assert response.status_code == 400

    def test_invalid_state_is_a_400(self, client):
        response = client.get(self.CALLBACK, params={"code": "c", "state": "never-issued"})
        assert response.status_code == 400

    def _complete(self, client, state):
        from app.services.connectors.box import BoxConnector

        with (
            patch.object(
                BoxConnector,
                "handle_callback",
                new=AsyncMock(return_value={"email": "box@example.com"}),
            ) as handle,
            patch("app.routers.connectors.audit_service.log_event", new=AsyncMock()) as audit,
        ):
            response = client.get(self.CALLBACK, params={"code": "c", "state": state})
        return response, handle, audit

    def test_missing_binding_cookie_is_403(self, client):
        """A state redeemed by a browser that never started the flow (the
        attacker handed the victim's — or their own — state to it) is refused
        before any token is exchanged or stored."""
        from app.services.connectors.sync_state import store_oauth_state

        store_oauth_state("state-unbound", "test-user-123", ConnectorType.BOX)
        client.cookies.clear()
        response, handle, audit = self._complete(client, "state-unbound")
        assert response.status_code == 403
        handle.assert_not_awaited()
        alerts = _events(audit, AuditEventType.SECURITY_ALERT)
        assert [a["details"]["action"] for a in alerts] == ["oauth_callback_browser_mismatch"]
        assert alerts[0]["user_id"] == "test-user-123"
        # The state was consumed: it cannot be retried from the right browser either.
        from app.services.connectors.sync_state import OAUTH_BINDING_COOKIE, oauth_binding_token

        client.cookies.set(
            OAUTH_BINDING_COOKIE, oauth_binding_token("state-unbound", "test-user-123")
        )
        assert self._complete(client, "state-unbound")[0].status_code == 400

    def test_binding_cookie_of_another_user_is_403(self, client):
        """User B's browser cannot complete a state issued to user A: the
        grant would otherwise be stored under A."""
        self._bind(client, state="state-a", user_id="user-a", cookie_user="user-b")
        response, handle, _ = self._complete(client, "state-a")
        assert response.status_code == 403
        handle.assert_not_awaited()

    def test_browser_that_started_the_flow_completes_it(self, client, auth_headers):
        """End to end: /auth sets the HttpOnly Lax cookie and the callback from
        the same browser is accepted and clears it."""
        from app.services.connectors.box import BoxConnector
        from app.services.connectors.sync_state import OAUTH_BINDING_COOKIE

        client.cookies.clear()
        with patch.object(
            BoxConnector, "is_configured", new_callable=PropertyMock, return_value=True
        ):
            start = client.get("/api/v1/connectors/box/auth", headers=auth_headers)
        assert start.status_code == 200, start.text
        state = start.json()["state"]
        set_cookie = start.headers["set-cookie"]
        assert set_cookie.startswith(f"{OAUTH_BINDING_COOKIE}=")
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Path=/api/v1/connectors" in set_cookie
        assert client.cookies.get(OAUTH_BINDING_COOKIE)

        response, handle, audit = self._complete(client, state)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "connected"
        handle.assert_awaited_once()
        assert len(_events(audit, AuditEventType.CONNECTOR_CONNECT)) == 1
        # Single use: the binding cookie is cleared with the state.
        assert 'connector_oauth_binding=""' in response.headers["set-cookie"]


class TestExperimentalConnectors:
    """iManage and NetDocuments are unregistered until EXPERIMENTAL_CONNECTORS=true.

    Settings is frozen, so the registry built for the flag value is swapped in
    with `build_registry` instead of patching the setting itself.
    """

    PROVIDERS = ("imanage", "netdocuments")

    @staticmethod
    def _registry(experimental: bool):
        from app.services.connectors import build_registry

        return patch("app.services.connectors.CONNECTORS", build_registry(experimental))

    def test_flag_off_hides_them_from_the_list(self, client, auth_headers):
        with self._registry(False):
            response = client.get("/api/v1/connectors", headers=auth_headers)
        assert response.status_code == 200
        connectors = response.json()["connectors"]
        types = {c["type"] for c in connectors}
        assert not types & set(self.PROVIDERS)
        assert {"google_drive", "onedrive", "box", "dropbox", "clio", "filevine"} <= types
        assert not any(c.get("experimental") for c in connectors)

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_flag_off_refuses_every_route(self, client, auth_headers, provider):
        """Same answer an unknown provider gets today: 400 naming the available set,
        plus how to enable the experimental one."""
        with self._registry(False):
            status = client.get(f"/api/v1/connectors/{provider}/status", headers=auth_headers)
            auth = client.get(f"/api/v1/connectors/{provider}/auth", headers=auth_headers)
            sync = client.post(
                f"/api/v1/connectors/{provider}/sync",
                json={"sync_all": True},
                headers=auth_headers,
            )
            disconnect = client.post(
                f"/api/v1/connectors/{provider}/disconnect", headers=auth_headers
            )
            callback = client.get(
                f"/api/v1/connectors/{provider}/callback",
                params={"code": "c", "state": "no-such-state"},
            )
        for response in (status, auth, sync, disconnect):
            assert response.status_code == 400, response.text
            assert "EXPERIMENTAL_CONNECTORS=true" in response.json()["detail"]
            assert provider not in response.json()["detail"].split("Available connectors: ")[1]
        # No auth flow can start, so no state exists for the callback to redeem.
        assert callback.status_code == 400

    def test_flag_on_lists_them_as_experimental(self, client, auth_headers):
        with self._registry(True):
            response = client.get("/api/v1/connectors", headers=auth_headers)
        assert response.status_code == 200
        by_type = {c["type"]: c for c in response.json()["connectors"]}
        for provider in self.PROVIDERS:
            assert by_type[provider]["experimental"] is True
            assert by_type[provider]["connected"] is False
        # Other connectors are unaffected by the field.
        assert by_type["box"].get("experimental", False) is False

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_flag_on_status_route_flags_them(self, client, auth_headers, provider):
        with self._registry(True):
            response = client.get(f"/api/v1/connectors/{provider}/status", headers=auth_headers)
            box = client.get("/api/v1/connectors/box/status", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json()["experimental"] is True
        assert response.json()["configured"] is False
        assert box.json()["experimental"] is False

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_flag_on_auth_route_reaches_the_connector(self, client, auth_headers, provider):
        """With the flag on the routes behave as before: an unconfigured
        connector is refused for lack of credentials, not for being unknown."""
        with self._registry(True):
            response = client.get(f"/api/v1/connectors/{provider}/auth", headers=auth_headers)
        assert response.status_code == 400, response.text
        assert "EXPERIMENTAL_CONNECTORS" not in response.json()["detail"]


class TestFolderBrowse:
    """GET /connectors/{type}/folders lists one level of folders for the picker."""

    @staticmethod
    def _listing():
        from app.services.connectors.base import FileInfo

        now = datetime.now(UTC)
        return (
            [
                FileInfo(
                    id="f1",
                    name="Clients",
                    mime_type="folder",
                    size=0,
                    modified_at=now,
                    path="/Clients",
                ),
                FileInfo(
                    id="d1", name="MSA.pdf", mime_type="application/pdf", size=10, modified_at=now
                ),
                FileInfo(
                    id="f2",
                    name="Archive",
                    mime_type="folder",
                    size=0,
                    modified_at=now,
                    path="/Archive",
                ),
            ],
            "page-2",
        )

    def test_lists_folders_only_with_paging(self, client, auth_headers):
        from app.services.connectors.box import BoxConnector

        with (
            _connected(BoxConnector),
            patch.object(
                BoxConnector, "list_files", new=AsyncMock(return_value=self._listing())
            ) as lf,
        ):
            response = client.get("/api/v1/connectors/box/folders", headers=auth_headers)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["connector"] == "box" and body["parent_id"] is None
            assert [f["name"] for f in body["folders"]] == ["Clients", "Archive"]
            assert body["next_page_token"] == "page-2"
            lf.assert_awaited_once_with(None, None)

            response = client.get(
                "/api/v1/connectors/box/folders?parent_id=f1&page_token=page-2",
                headers=auth_headers,
            )
            assert response.status_code == 200
            assert response.json()["parent_id"] == "f1"
            assert lf.await_args.args == ("f1", "page-2")

    def test_disconnected_and_whole_account_only_connectors_are_refused(self, client, auth_headers):
        from app.services.connectors.box import BoxConnector
        from app.services.connectors.clio import ClioConnector

        response = client.get("/api/v1/connectors/box/folders", headers=auth_headers)
        assert response.status_code == 400
        assert "not connected" in response.json()["detail"]
        with _connected(ClioConnector):
            response = client.get("/api/v1/connectors/clio/folders", headers=auth_headers)
        assert response.status_code == 400
        assert "cannot sync a single folder" in response.json()["detail"]
        assert BoxConnector is not None

    def test_provider_errors_are_a_502(self, client, auth_headers):
        from app.services.connectors.box import BoxConnector

        request = httpx.Request("GET", "https://api.box.com/2.0/folders/0/items")
        error = httpx.HTTPStatusError(
            "forbidden", request=request, response=httpx.Response(403, request=request)
        )
        with (
            _connected(BoxConnector),
            patch.object(BoxConnector, "list_files", new=AsyncMock(side_effect=error)),
        ):
            response = client.get("/api/v1/connectors/box/folders", headers=auth_headers)
        assert response.status_code == 502
        assert "HTTP 403" in response.json()["detail"]

    def test_status_says_whether_folder_sync_is_supported(self, client, auth_headers):
        response = client.get("/api/v1/connectors", headers=auth_headers)
        by_type = {c["type"]: c for c in response.json()["connectors"]}
        assert by_type["box"]["supports_folder_sync"] is True
        assert by_type["clio"]["supports_folder_sync"] is False
