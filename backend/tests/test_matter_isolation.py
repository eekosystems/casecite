"""Isolation tests for matter-scoped access at the vector-store and
document-service layers. These are the security-critical guarantees: a matter
member can reach shared content, a non-member cannot, and the owner is never
affected by the new sharing path.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from app.models.enums import DocumentStatus
from app.models.schemas import ConnectorType, Document
from app.services.documents import DocumentService
from app.services.rag.search import search_documents
from app.services.vectordb import _has_tenant_scope

# ---------------------------------------------------------------------------
# The fail-closed tenant-scope guard
# ---------------------------------------------------------------------------


def test_tenant_scope_accepts_user_matter_and_nested():
    assert _has_tenant_scope({"user_id": "u1"}) is True
    assert _has_tenant_scope({"matter_id": {"$in": ["m1"]}}) is True
    # Shared-matter search shape: $or of owner + matters.
    assert (
        _has_tenant_scope({"$or": [{"user_id": "u1"}, {"matter_id": {"$in": ["m1", "m2"]}}]})
        is True
    )
    # Document-scoped shape: $and of doc + owner.
    assert _has_tenant_scope({"$and": [{"document_id": "d1"}, {"user_id": "u1"}]}) is True


def test_tenant_scope_rejects_missing_scope():
    assert _has_tenant_scope(None) is False
    assert _has_tenant_scope({}) is False
    # A filter with only a non-tenant key must NOT pass — this is what stops an
    # accidental unscoped (cross-tenant) search.
    assert _has_tenant_scope({"document_id": "d1"}) is False
    assert _has_tenant_scope({"$and": [{"document_id": "d1"}]}) is False


# ---------------------------------------------------------------------------
# The search filter fuses owner + matter membership as $or
# ---------------------------------------------------------------------------


class _FakeVDB:
    def __init__(self):
        self.last_filter = "UNSET"

    async def search(self, query_embedding, top_k, filter):
        self.last_filter = filter
        return []


async def test_search_filter_scopes_to_owner_or_member_matters():
    fake = _FakeVDB()
    # Patch the retry/circuit-breaker wrapper so the test is independent of the
    # shared "embeddings" breaker state (another test may leave it open) and
    # deterministically reaches the fake vector DB with our tenant filter.
    with patch(
        "app.services.rag.search.retry_with_backoff",
        new=AsyncMock(return_value=[0.1] * 8),
    ):
        await search_documents(
            fake,
            "indemnification",
            user_id="owner-1",
            matter_ids=["m-shared", "m-other"],
            use_hybrid=False,
            use_reranking=False,
        )
    f = fake.last_filter
    # Owner OR any member matter — expressed as $or so both are honored.
    assert "$or" in f
    branches = f["$or"]
    assert {"user_id": "owner-1"} in branches
    assert {"matter_id": {"$in": ["m-shared", "m-other"]}} in branches


async def test_search_filter_owner_only_when_no_matters():
    fake = _FakeVDB()
    # Patch the retry/circuit-breaker wrapper so the test is independent of the
    # shared "embeddings" breaker state (another test may leave it open) and
    # deterministically reaches the fake vector DB with our tenant filter.
    with patch(
        "app.services.rag.search.retry_with_backoff",
        new=AsyncMock(return_value=[0.1] * 8),
    ):
        await search_documents(fake, "q", user_id="owner-1", use_hybrid=False, use_reranking=False)
    # No matters supplied -> plain owner scope, identical to the historical path.
    assert fake.last_filter == {"user_id": "owner-1"}


# ---------------------------------------------------------------------------
# Document-service access: owner always; member only via a shared matter
# ---------------------------------------------------------------------------


def _doc(doc_id: str, owner: str, matter_id: str | None) -> Document:
    return Document(
        id=doc_id,
        user_id=owner,
        matter_id=matter_id,
        filename=f"{doc_id}.pdf",
        content_type="application/pdf",
        size=10,
        source=ConnectorType.LOCAL,
        status=DocumentStatus.INDEXED,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def svc():
    s = DocumentService.__new__(DocumentService)  # skip disk-loading __init__
    s.documents = {}
    s.folders = {}
    return s


async def test_owner_sees_own_doc_member_and_stranger_do_not(svc):
    d = _doc("d1", owner="alice", matter_id=None)  # personal (untagged) doc
    svc.documents["d1"] = d

    # Owner sees it regardless of matter args.
    assert (await svc.get_document("d1", "alice")) is d
    # A non-owner cannot, even when passing their (unrelated) matter set.
    assert (await svc.get_document("d1", "bob", accessible_matter_ids={"m-x"})) is None


async def test_shared_matter_grants_member_access_not_stranger(svc):
    d = _doc("d2", owner="alice", matter_id="m-shared")
    svc.documents["d2"] = d

    # Bob is a member of m-shared -> can access.
    assert (await svc.get_document("d2", "bob", accessible_matter_ids={"m-shared"})) is d
    # Carol is not a member -> cannot.
    assert (await svc.get_document("d2", "carol", accessible_matter_ids={"m-other"})) is None
    # And without opting into sharing (None), even a would-be member can't (owner-only default).
    assert (await svc.get_document("d2", "bob")) is None


async def test_list_documents_includes_shared_matter_docs_only_for_members(svc):
    svc.documents["own"] = _doc("own", owner="bob", matter_id=None)
    svc.documents["shared"] = _doc("shared", owner="alice", matter_id="m-shared")
    svc.documents["private-alice"] = _doc("private-alice", owner="alice", matter_id=None)

    ids = {d.id for d in await svc.list_documents("bob", accessible_matter_ids={"m-shared"})}
    assert ids == {"own", "shared"}  # bob's own + the shared-matter doc, never alice's private one


async def test_can_access_matrix():
    d = _doc("d", owner="alice", matter_id="m1")
    assert DocumentService._can_access(d, "alice", None) is True  # owner
    assert DocumentService._can_access(d, "bob", {"m1"}) is True  # member
    assert DocumentService._can_access(d, "bob", {"m2"}) is False  # wrong matter
    assert DocumentService._can_access(d, "bob", None) is False  # no sharing opt-in
    d_personal = _doc("d0", owner="alice", matter_id=None)
    assert DocumentService._can_access(d_personal, "bob", {"m1"}) is False  # untagged never shared
