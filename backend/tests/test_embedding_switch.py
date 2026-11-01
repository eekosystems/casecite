"""
Tests for changing the embedding model at runtime (Settings → Embedding).

Vectors from two models can't be searched together, so each model gets its own
vector collection and a switch re-embeds the documents in the background. These
tests guard the parts that would lose or corrupt an index if they went wrong:
the server is checked before anything changes, each model reads and writes its
own collection, failures are retried rather than forgotten, and switching back
restores documents without re-embedding.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.enums import UserRole
from app.models.schemas import ConnectorType, Document, DocumentStatus

from tests.helpers import headers, make_user

LOCAL = {"base_url": "http://ollama:11434/v1", "model": "nomic-embed-text", "dimensions": 768}


def _service():
    from app.services.embeddings import EmbeddingService

    return EmbeddingService()


def _probe_client(vector=None, error=None):
    client = MagicMock()
    if error is not None:
        client.embeddings.create = AsyncMock(side_effect=error)
    else:
        response = MagicMock()
        response.data = [MagicMock(embedding=vector)]
        client.embeddings.create = AsyncMock(return_value=response)
    return client


class TestEmbeddingService:
    def test_instance_config_replaces_env_and_reverts(self):
        from app.services.embeddings import DEFAULT_COLLECTION

        svc = _service()
        env_model = svc.model
        svc.apply_instance_config(LOCAL)
        assert (svc.source, svc.base_url, svc.model, svc.dimensions) == (
            "instance",
            "http://ollama:11434/v1",
            "nomic-embed-text",
            768,
        )
        assert svc.collection_name() != DEFAULT_COLLECTION

        svc.apply_instance_config(None)
        assert svc.source == "env"
        assert svc.model == env_model
        assert svc.collection_name() == DEFAULT_COLLECTION

    def test_each_model_has_its_own_collection(self):
        svc = _service()
        svc.apply_instance_config(LOCAL)
        first = svc.collection_name()
        svc.apply_instance_config({**LOCAL, "model": "mxbai-embed-large", "dimensions": 1024})
        assert svc.collection_name() != first
        svc.apply_instance_config(LOCAL)
        assert svc.collection_name() == first  # same model, same collection

    def test_the_env_model_keeps_the_original_collection(self):
        """Choosing the .env model here must not orphan the existing index."""
        from app.services.embeddings import DEFAULT_COLLECTION

        svc = _service()
        model, dims = svc.signature()
        svc.apply_instance_config({"base_url": "http://gw/v1", "model": model, "dimensions": dims})
        assert svc.collection_name() == DEFAULT_COLLECTION

    def test_admin_server_never_gets_the_openai_key(self):
        svc = _service()
        svc.openai_api_key = "sk-server-openai-key-0000"
        svc.apply_instance_config(LOCAL)
        route = svc.resolve_route(None)
        assert route.provider == "self_hosted"
        assert route.api_key is None

        svc.apply_instance_config({**LOCAL, "api_key": "server-own-key"})
        assert svc.resolve_route(None).api_key == "server-own-key"

    @pytest.mark.asyncio
    async def test_probe_returns_the_vector_size(self):
        client = _probe_client(vector=[0.1] * 768)
        with patch("openai.AsyncOpenAI", return_value=client) as ctor:
            dims = await _service().probe("http://ollama:11434/v1", "nomic-embed-text")
        assert dims == 768
        assert ctor.call_args.kwargs["base_url"] == "http://ollama:11434/v1"
        assert ctor.call_args.kwargs["api_key"] == "not-needed"

    @pytest.mark.asyncio
    async def test_probe_failure_is_a_readable_error(self):
        client = _probe_client(error=RuntimeError("model 'nomic' not found"))
        with (
            patch("openai.AsyncOpenAI", return_value=client),
            pytest.raises(ValueError, match="did not return an embedding"),
        ):
            await _service().probe("http://ollama:11434/v1", "nomic")


class TestCollections:
    @pytest.mark.asyncio
    async def test_switching_collections_keeps_each_models_vectors(self, chroma_db):
        chunk = {
            "id": "d1_0",
            "embedding": [0.1] * 8,
            "text": "Termination for convenience on 30 days' notice.",
            "document_id": "d1",
            "user_id": "u1",
        }
        chroma_db.use_collection("model_a")
        await chroma_db.add_documents([chunk])
        assert await chroma_db.has_document("d1")

        chroma_db.use_collection("model_b")
        assert not await chroma_db.has_document("d1")
        # A different vector size is fine in its own collection.
        await chroma_db.add_documents([{**chunk, "embedding": [0.2] * 4}])
        assert (await chroma_db.get_stats())["collection_name"] == "model_b"

        chroma_db.use_collection("model_a")
        assert await chroma_db.has_document("d1")


def _doc(doc_id: str, status=DocumentStatus.INDEXED, metadata=None) -> Document:
    return Document(
        id=doc_id,
        user_id="u1",
        filename=f"{doc_id}.txt",
        content_type="text/plain",
        size=10,
        source=ConnectorType.LOCAL,
        status=status,
        created_at=datetime.now(UTC),
        chunk_count=3,
        metadata=metadata or {},
    )


def _documents(docs):
    from app.services.documents import DocumentService

    service = DocumentService.__new__(DocumentService)
    service.documents = {d.id: d for d in docs}
    service.folders = {}
    service._save_index = MagicMock()
    return service


class TestReembedding:
    @pytest.mark.asyncio
    async def test_switch_reembeds_with_replace_and_reports_progress(self):
        service = _documents([_doc("a"), _doc("b")])
        service._index_from_stored_file = AsyncMock(return_value=4)
        rag = MagicMock()
        rag.vector_db.has_document = AsyncMock(return_value=False)
        calls = []

        with (
            patch("app.services.documents.rag_service", rag),
            patch("app.services.user_keys.UserAPIKeys.for_user", return_value=MagicMock()),
        ):
            stats = await service.reconcile_index(
                after_model_switch=True, progress=lambda *a: calls.append(a)
            )

        assert stats["reindexed"] == 2
        assert calls == [(1, 2, 0), (2, 2, 0)]
        # replace=True swaps the keyword index's old chunks for the new ones.
        assert all(c.kwargs["replace"] for c in service._index_from_stored_file.await_args_list)

    @pytest.mark.asyncio
    async def test_failure_during_a_switch_is_flagged_for_retry(self):
        from app.services.documents import REINDEX_FLAG

        doc = _doc("a")
        service = _documents([doc])
        service._index_from_stored_file = AsyncMock(side_effect=RuntimeError("connection refused"))
        rag = MagicMock()
        rag.vector_db.has_document = AsyncMock(return_value=False)

        with (
            patch("app.services.documents.rag_service", rag),
            patch("app.services.user_keys.UserAPIKeys.for_user", return_value=MagicMock()),
        ):
            stats = await service.reconcile_index(after_model_switch=True)

        assert stats["failed"] == 1
        assert doc.status == DocumentStatus.FAILED
        assert doc.metadata[REINDEX_FLAG] is True
        assert "Settings → Embedding" in doc.metadata["error"]

        # The next run (Retry, or a restart) tries it again.
        service._index_from_stored_file = AsyncMock(return_value=2)
        with (
            patch("app.services.documents.rag_service", rag),
            patch("app.services.user_keys.UserAPIKeys.for_user", return_value=MagicMock()),
        ):
            stats = await service.reconcile_index()
        assert stats["reindexed"] == 1
        assert REINDEX_FLAG not in doc.metadata
        assert service._index_from_stored_file.await_args.kwargs["replace"] is True

    @pytest.mark.asyncio
    async def test_switching_back_restores_a_flagged_document(self):
        """The old model's collection still holds it: no re-embedding needed."""
        from app.services.documents import REINDEX_FLAG

        doc = _doc("a", DocumentStatus.FAILED, {REINDEX_FLAG: True, "error": "failed"})
        service = _documents([doc])
        service._index_from_stored_file = AsyncMock()
        rag = MagicMock()
        rag.vector_db.has_document = AsyncMock(return_value=True)

        with patch("app.services.documents.rag_service", rag):
            stats = await service.reconcile_index(after_model_switch=True)

        assert stats["restored"] == 1
        assert doc.status == DocumentStatus.INDEXED
        assert "error" not in doc.metadata
        service._index_from_stored_file.assert_not_awaited()


class TestSwitchJob:
    @pytest.mark.asyncio
    async def test_status_reports_a_finished_run(self):
        from app.services import embedding_switch

        async def reconcile(after_model_switch, progress):
            progress(1, 1, 0)
            return {"failed": 0}

        with (
            patch("app.services.documents.document_service.reconcile_index", reconcile),
            patch.object(embedding_switch, "_reembed_clause_references", AsyncMock()),
        ):
            embedding_switch.start_reindex()
            await embedding_switch._task
        status = embedding_switch.reindex_status()
        assert (status["state"], status["done"], status["total"]) == ("done", 1, 1)
        assert status["finished_at"]

    @pytest.mark.asyncio
    async def test_a_new_switch_cancels_the_old_run_without_mixing_status(self):
        from app.services import embedding_switch

        release = asyncio.Event()

        async def slow(after_model_switch, progress):
            await release.wait()
            progress(9, 9, 9)
            return {"failed": 9}

        async def quick(after_model_switch, progress):
            return {"failed": 0}

        with patch.object(embedding_switch, "_reembed_clause_references", AsyncMock()):
            with patch("app.services.documents.document_service.reconcile_index", slow):
                embedding_switch.start_reindex()
                first = embedding_switch._task
                await asyncio.sleep(0)
            with patch("app.services.documents.document_service.reconcile_index", quick):
                embedding_switch.start_reindex()
                await embedding_switch._task
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
        status = embedding_switch.reindex_status()
        assert (status["state"], status["failed"]) == ("done", 0)


class TestIndexingGuard:
    @pytest.mark.asyncio
    async def test_a_switch_mid_embedding_does_not_store_old_model_vectors(self):
        from app.services.embeddings import embedding_service
        from app.services.rag.service import RAGService

        async def embed_then_switch(texts, user_keys=None):
            embedding_service.apply_instance_config(LOCAL)
            return [[0.1] * 4 for _ in texts]

        rag = RAGService(vector_db=MagicMock())
        rag.vector_db.add_documents = AsyncMock()
        try:
            with (
                patch.object(embedding_service, "embed_texts", embed_then_switch),
                patch.object(embedding_service, "count_tokens", lambda t: len(t.split())),
                pytest.raises(ValueError, match="embedding model was changed"),
            ):
                await rag.index_document(
                    document_id="d", text="Some contract text.", filename="a.txt", user_id="u1"
                )
        finally:
            embedding_service.apply_instance_config(None)
        rag.vector_db.add_documents.assert_not_awaited()


# ----- HTTP -----

BASE = "/api/v1/admin/integrations"
ROUTER = "app.services.embedding_switch"

ROUTES = [
    ("get", "/embeddings", None),
    ("post", "/embeddings", {"base_url": "http://ollama:11434/v1", "model": "nomic-embed-text"}),
    ("delete", "/embeddings", None),
    ("post", "/embeddings/reindex", None),
]


@pytest.fixture
def admin():
    return make_user("emb-admin", UserRole.ADMIN)


@pytest.fixture(autouse=True)
def _restore_env_embeddings():
    yield
    from app.services.embeddings import embedding_service

    embedding_service.apply_instance_config(None)


def _call(client, method, path, body, **kwargs):
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(f"{BASE}{path}", **kwargs)


@pytest.mark.usefixtures("no_rate_limit")
class TestAdminEndpoints:
    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_admin_only(self, client, method, path, body):
        attorney = make_user("emb-attorney", UserRole.ATTORNEY)
        with patch(f"{ROUTER}.save_and_apply", new_callable=AsyncMock) as save:
            assert _call(client, method, path, body).status_code == 401
            resp = _call(client, method, path, body, headers=headers(attorney))
        assert resp.status_code == 403
        save.assert_not_awaited()

    def test_a_server_that_fails_the_check_changes_nothing(self, client, admin):
        from app.services.embeddings import embedding_service

        with (
            patch.object(
                embedding_service, "probe", AsyncMock(side_effect=ValueError("did not respond"))
            ),
            patch(f"{ROUTER}.save_and_apply", new_callable=AsyncMock) as save,
        ):
            resp = _call(client, "post", "/embeddings", ROUTES[1][2], headers=headers(admin))
        assert resp.status_code == 400
        assert "did not respond" in resp.text
        save.assert_not_awaited()

    def test_dimensions_must_match_the_server(self, client, admin):
        from app.services.embeddings import embedding_service

        body = {**ROUTES[1][2], "dimensions": 1024}
        with (
            patch.object(embedding_service, "probe", AsyncMock(return_value=768)),
            patch(f"{ROUTER}.save_and_apply", new_callable=AsyncMock) as save,
        ):
            resp = _call(client, "post", "/embeddings", body, headers=headers(admin))
        assert resp.status_code == 400
        assert "768-dimension" in resp.text
        save.assert_not_awaited()

    def test_save_uses_the_measured_size_and_audits(self, client, admin, audit_events):
        from app.services.embeddings import embedding_service

        body = {**ROUTES[1][2], "api_key": "server-key-123"}
        with (
            patch.object(embedding_service, "probe", AsyncMock(return_value=768)),
            patch(f"{ROUTER}.save_and_apply", new_callable=AsyncMock) as save,
        ):
            resp = _call(client, "post", "/embeddings", body, headers=headers(admin))
        assert resp.status_code == 200
        save.assert_awaited_once_with(
            {
                "base_url": "http://ollama:11434/v1",
                "model": "nomic-embed-text",
                "dimensions": 768,
                "api_key": "server-key-123",
            }
        )
        assert "server-key-123" not in resp.text
        assert "server-key-123" not in str(audit_events)
        assert [e["details"]["action"] for e in audit_events] == ["embeddings_set"]

    def test_saved_key_is_kept_when_the_server_is_unchanged(self, client, admin):
        from app.services.embeddings import embedding_service

        embedding_service.apply_instance_config({**LOCAL, "api_key": "kept-key"})
        with (
            patch.object(embedding_service, "probe", AsyncMock(return_value=768)) as probe,
            patch(f"{ROUTER}.save_and_apply", new_callable=AsyncMock) as save,
        ):
            resp = _call(client, "post", "/embeddings", ROUTES[1][2], headers=headers(admin))
        assert resp.status_code == 200
        assert probe.await_args.args[2] == "kept-key"
        assert save.await_args.args[0]["api_key"] == "kept-key"
        assert resp.json()["api_key_set"] is True

    def test_pinecone_is_refused(self, client, admin):
        with (
            patch(f"{ROUTER}.can_switch", return_value=False),
            patch(f"{ROUTER}.save_and_apply", new_callable=AsyncMock) as save,
        ):
            resp = _call(client, "post", "/embeddings", ROUTES[1][2], headers=headers(admin))
        assert resp.status_code == 400
        assert "Pinecone" in resp.text
        save.assert_not_awaited()

    def test_clear_goes_back_to_env(self, client, admin):
        from app.services.embeddings import embedding_service

        embedding_service.apply_instance_config(LOCAL)
        with patch(f"{ROUTER}.save_and_apply", new_callable=AsyncMock) as save:
            resp = _call(client, "delete", "/embeddings", None, headers=headers(admin))
        assert resp.status_code == 200
        save.assert_awaited_once_with(None)

    def test_retry_starts_a_run(self, client, admin):
        with patch(f"{ROUTER}.start_reindex") as start:
            resp = _call(client, "post", "/embeddings/reindex", None, headers=headers(admin))
        assert resp.status_code == 200
        start.assert_called_once()


@pytest.mark.usefixtures("no_rate_limit")
class TestUserEndpoint:
    def test_requires_auth(self, client):
        assert client.get("/api/v1/settings/embeddings").status_code == 401

    def test_shows_the_model_but_not_the_server(self, client, auth_headers):
        from app.services.embeddings import embedding_service

        embedding_service.apply_instance_config(LOCAL)
        resp = client.get("/api/v1/settings/embeddings", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert (body["source"], body["provider"], body["model"], body["dimensions"]) == (
            "instance",
            "self_hosted",
            "nomic-embed-text",
            768,
        )
        assert "ollama" not in resp.text


class TestClauseReferences:
    """Startup seeding (and a switch) re-embed reference clauses from another model.
    The seeder itself is stubbed for the test session; its decision is tested here."""

    def test_reference_from_another_model_needs_embedding(self):
        from app.services.clause_intel.seeder import needs_embedding

        assert needs_embedding(MagicMock(embedding=None), "m")
        assert needs_embedding(MagicMock(embedding=[0.1], embedding_model="old"), "new")
        assert not needs_embedding(MagicMock(embedding=[0.1], embedding_model="new"), "new")
        assert not needs_embedding(MagicMock(embedding=[0.1], embedding_model="old"), None)
