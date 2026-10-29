"""
Chatting with one, several or all matters.

The Matter Strategy workspace lets a user pick matters; the server resolves
them to documents. Only matters the caller belongs to may contribute, the
caller's personal matter brings their unfiled documents, a selection that
resolves to nothing stays empty (never widens to everything), and the
strategy brief reads a colleague's shared document like the caller's own.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.schemas import ConnectorType, Document, DocumentStatus
from app.services.documents import DocumentService

USER = "test-user-123"  # the identity behind the auth_headers fixture


def _doc(doc_id, user_id=USER, matter_id=None, status=DocumentStatus.INDEXED) -> Document:
    return Document(
        id=doc_id,
        user_id=user_id,
        matter_id=matter_id,
        filename=f"{doc_id}.txt",
        content_type="text/plain",
        size=10,
        source=ConnectorType.LOCAL,
        status=status,
        created_at=datetime.now(UTC),
        chunk_count=3,
        metadata={},
    )


def _service(*docs) -> DocumentService:
    service = DocumentService.__new__(DocumentService)
    service.documents = {d.id: d for d in docs}
    service.folders = {}
    return service


LIBRARY = (
    _doc("mine-unfiled"),
    _doc("mine-in-a", matter_id="matter-a"),
    _doc("colleague-in-a", user_id="colleague", matter_id="matter-a"),
    _doc("colleague-in-b", user_id="colleague", matter_id="matter-b"),
    _doc("stranger-in-z", user_id="stranger", matter_id="matter-z"),
    _doc("colleague-unfiled", user_id="colleague"),
    _doc("mine-still-indexing", matter_id="matter-a", status=DocumentStatus.PROCESSING),
)


class TestResolvingMatters:
    def test_a_shared_matter_brings_every_members_documents(self):
        ids = _service(*LIBRARY).document_ids_in_matters(
            ["matter-a"], USER, {"matter-a", "personal"}, "personal"
        )
        assert set(ids) == {"mine-in-a", "colleague-in-a"}  # indexed only

    def test_several_matters_union_without_duplicates(self):
        ids = _service(*LIBRARY).document_ids_in_matters(
            ["matter-a", "matter-b", "matter-a"], USER, {"matter-a", "matter-b"}, None
        )
        assert sorted(ids) == ["colleague-in-a", "colleague-in-b", "mine-in-a"]

    def test_the_personal_matter_is_the_callers_unfiled_documents(self):
        ids = _service(*LIBRARY).document_ids_in_matters(
            ["personal"], USER, {"personal"}, "personal"
        )
        assert ids == ["mine-unfiled"]  # never a colleague's unfiled file

    def test_a_matter_the_caller_is_not_in_contributes_nothing(self):
        ids = _service(*LIBRARY).document_ids_in_matters(["matter-z"], USER, {"matter-a"}, None)
        assert ids == []

    @pytest.mark.asyncio
    async def test_documents_by_id_honour_matter_membership(self):
        service = _service(*LIBRARY)
        owner_only = await service.get_documents_by_ids(["colleague-in-a"], USER)
        shared = await service.get_documents_by_ids(["colleague-in-a"], USER, {"matter-a"})
        assert owner_only == [] and [d.id for d in shared] == ["colleague-in-a"]


def _answer():
    return {
        "content": "ok",
        "citations": [],
        "stats": {"docs_searched": 1, "chunks_retrieved": 1, "processing_time": "0s"},
    }


def _route(intent: str):
    return patch(
        "app.routers.chat.route_query",
        new_callable=AsyncMock,
        return_value=(intent, False, None, False),
    )


class TestChatWithMatters:
    @pytest.fixture
    def scoped(self, app):
        from app.routers._matter_deps import accessible_matter_ids, personal_matter_id

        app.dependency_overrides[accessible_matter_ids] = lambda: {
            "matter-a",
            "matter-b",
            "personal",
        }
        app.dependency_overrides[personal_matter_id] = lambda: "personal"
        with patch("app.routers.chat.document_service", _service(*LIBRARY)):
            yield
        app.dependency_overrides.pop(accessible_matter_ids, None)
        app.dependency_overrides.pop(personal_matter_id, None)

    def _ask(self, client, auth_headers, document_filter):
        with (
            _route("ask"),
            patch(
                "app.routers.chat.rag_service.generate_response",
                new_callable=AsyncMock,
                return_value=_answer(),
            ) as generate,
        ):
            resp = client.post(
                "/api/v1/chat",
                json={
                    "query": "Which leases renew?",
                    "mode": "research",
                    "document_filter": document_filter,
                },
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text
        return generate.await_args.kwargs["document_ids"]

    def test_selected_matters_become_the_scope(self, client, auth_headers, scoped):
        ids = self._ask(client, auth_headers, {"matter_ids": ["matter-a", "matter-b"]})
        assert sorted(ids) == ["colleague-in-a", "colleague-in-b", "mine-in-a"]

    def test_matters_and_files_are_a_union(self, client, auth_headers, scoped):
        ids = self._ask(
            client, auth_headers, {"matter_ids": ["matter-b"], "document_ids": ["mine-unfiled"]}
        )
        assert sorted(ids) == ["colleague-in-b", "mine-unfiled"]

    def test_an_inaccessible_matter_is_an_empty_scope_not_everything(
        self, client, auth_headers, scoped
    ):
        assert self._ask(client, auth_headers, {"matter_ids": ["matter-z"]}) == []

    def test_no_filter_still_means_everything(self, client, auth_headers, scoped):
        assert self._ask(client, auth_headers, None) is None


class TestStrategyBriefReadsSharedMatters:
    @pytest.mark.asyncio
    async def test_coverage_search_is_scoped_to_the_selection_and_matters(self):
        from app.services import strategy as strategy_module

        vector_db = MagicMock()
        vector_db.search = AsyncMock(return_value=[])
        with (
            patch.object(strategy_module, "get_vector_db", return_value=vector_db),
            patch.object(
                strategy_module.embedding_service, "embed_query", AsyncMock(return_value=[0.1] * 4)
            ),
            patch.object(
                strategy_module, "get_document_chunks_directly", AsyncMock(return_value=[])
            ) as direct,
        ):
            await strategy_module.strategy_service._retrieve_coverage(
                "q", [("colleague-in-a", "a.txt")], USER, None, matter_ids=["matter-a"]
            )

        search_filter = vector_db.search.await_args.kwargs["filter"]
        assert search_filter == {
            "$and": [
                {"$or": [{"user_id": USER}, {"matter_id": {"$in": ["matter-a"]}}]},
                {"document_id": {"$in": ["colleague-in-a"]}},
            ]
        }
        assert direct.await_args.kwargs["matter_ids"] == ["matter-a"]


class TestDeepResearchWithMatters:
    @pytest.mark.asyncio
    async def test_picked_matters_scope_deep_research(self):
        from app.models.schemas import DeepResearchPlanRequest, DocumentFilter
        from app.routers import deep_research

        body = DeepResearchPlanRequest(
            question="What are our indemnity exposures?",
            document_filter=DocumentFilter(matter_ids=["matter-a", "matter-z"]),
        )
        user = MagicMock(user_id=USER)
        with (
            patch.object(deep_research, "document_service", _service(*LIBRARY)),
            patch.object(
                deep_research.matters_service,
                "get_personal_matter_id",
                AsyncMock(return_value="personal"),
            ),
        ):
            ids, count = await deep_research._resolve_scope(
                MagicMock(), body, user, {"matter-a", "personal"}
            )

        assert sorted(ids) == ["colleague-in-a", "mine-in-a"]  # matter-z is not the caller's
        assert count == 2
