# ruff: noqa: F811  (the `users` fixture is imported from test_router_users)
"""Legal hold and retention.

A hold on a matter blocks every delete of what is filed under it — the
document, the conversation, the matter, a purge of the owner's account,
replacement by re-upload — until lifted. A hold on one document does the
same for that document. A retention policy removes content older than N
days through the normal delete path, audited, and a hold suspends it.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from app.models.chat_sessions import ChatSessionDB
from app.models.matters import Matter
from app.services import governance
from app.services.documents import DocumentOnHoldError, document_service

from tests.helpers import (
    db_add,
    db_delete,
    headers,
    make_shared_matter,
    make_user,
    register_document,
)
from tests.test_router_users import BASE, users  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("no_rate_limit")

MATTERS = "/api/v1/matters"


@pytest.fixture(autouse=True)
def _no_holds_between_tests():
    document_service.held_matter_ids = set()
    yield
    document_service.held_matter_ids = set()


def _rag():
    rag = AsyncMock()
    rag.delete_document = AsyncMock(return_value=True)
    return rag


def _read_matter(matter_id: str) -> Matter:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from tests.helpers import _sync_engine

    engine = _sync_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            return session.execute(select(Matter).where(Matter.id == matter_id)).scalar_one()
    finally:
        engine.dispose()


class TestMatterHold:
    def test_owner_places_and_lifts_a_hold_and_it_is_audited(self, client, audit_events):
        owner = make_user("hold-owner")
        matter_id = make_shared_matter(owner)
        resp = client.post(
            f"{MATTERS}/{matter_id}/hold",
            json={"on": True, "reason": "Litigation hold: Marlow v. Tidewater"},
            headers=headers(owner),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["legal_hold"] is True
        assert body["legal_hold_reason"] == "Litigation hold: Marlow v. Tidewater"
        assert body["legal_hold_set_at"]
        assert matter_id in document_service.held_matter_ids
        assert _read_matter(matter_id).legal_hold_set_by == owner.id

        resp = client.post(
            f"{MATTERS}/{matter_id}/hold", json={"on": False}, headers=headers(owner)
        )
        assert resp.status_code == 200
        assert resp.json()["legal_hold"] is False
        assert matter_id not in document_service.held_matter_ids
        actions = [
            (e.get("details") or {}).get("action")
            for e in audit_events
            if e.get("resource_id") == matter_id
        ]
        assert "legal_hold_set" in actions and "legal_hold_lifted" in actions

    def test_members_and_strangers_cannot_set_a_hold_but_admins_can(
        self, client, auth_headers, non_admin_headers
    ):
        owner = make_user("hold-owner")
        member = make_user("hold-member")
        matter_id = make_shared_matter(owner, member)
        assert (
            client.post(
                f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=headers(member)
            ).status_code
            == 404
        )

        # A viewer lacks matters.manage altogether: refused before the matter is looked at.
        assert (
            client.post(
                f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=non_admin_headers
            ).status_code
            == 403
        )
        resp = client.post(f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["legal_hold"] is True

    def test_a_held_matter_cannot_be_deleted(self, client):
        owner = make_user("hold-owner")
        matter_id = make_shared_matter(owner)
        client.post(f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=headers(owner))
        resp = client.delete(f"{MATTERS}/{matter_id}", headers=headers(owner))
        assert resp.status_code == 409
        assert "legal hold" in resp.json()["detail"]
        assert client.get(f"{MATTERS}/{matter_id}", headers=headers(owner)).status_code == 200

    def test_a_document_in_a_held_matter_cannot_be_deleted_by_anyone(self, client):
        owner = make_user("hold-owner")
        matter_id = make_shared_matter(owner)
        doc_id = register_document(owner, matter_id=matter_id)
        client.post(f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=headers(owner))
        try:
            with patch("app.services.documents.rag_service", _rag()):
                resp = client.delete(f"/api/v1/documents/{doc_id}", headers=headers(owner))
            assert resp.status_code == 409, resp.text
            assert "legal hold" in resp.json()["detail"]
            assert doc_id in document_service.documents
        finally:
            document_service.documents.pop(doc_id, None)

    def test_a_conversation_in_a_held_matter_cannot_be_deleted(self, client):
        owner = make_user("hold-owner")
        matter_id = make_shared_matter(owner)
        now = datetime.now(UTC).replace(tzinfo=None)
        session = ChatSessionDB(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            matter_id=matter_id,
            title="t",
            created_at=now,
            updated_at=now,
        )
        db_add(session)
        client.post(f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=headers(owner))
        try:
            resp = client.delete(f"/api/v1/chat/sessions/{session.id}", headers=headers(owner))
            assert resp.status_code == 409, resp.text
            assert "legal hold" in resp.json()["detail"]
        finally:
            db_delete(session)

    def test_a_purge_cannot_remove_held_work(self, client, auth_headers, users):
        uid, email = users.create()
        from tests.test_offboarding import make_user_like

        owner = make_user_like(uid, email)
        matter_id = make_shared_matter(owner)
        doc_id = register_document(owner, matter_id=matter_id)
        client.post(f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=headers(owner))
        try:
            resp = client.delete(f"{BASE}/{uid}", params={"mode": "purge"}, headers=auth_headers)
            assert resp.status_code == 409, resp.text
            assert "legal hold" in resp.json()["detail"]
            assert users.get(uid) is not None
            assert doc_id in document_service.documents
        finally:
            document_service.documents.pop(doc_id, None)

    async def test_a_held_old_copy_survives_replacement_by_re_upload(self):
        from tests.test_document_replace_and_reindex import _doc, _service

        old = _doc("old", matter_id="m-held")
        service = _service([old], None)
        service.held_matter_ids = {"m-held"}
        new = _doc("new", matter_id="m-held")
        await service._retire_replaced(old, new)
        assert "old" in service.documents

    def test_personal_matters_cannot_be_held(self, client, auth_headers):
        listed = client.get(MATTERS, headers=auth_headers).json()["matters"]
        personal = next(m for m in listed if m["is_personal"])
        resp = client.post(
            f"{MATTERS}/{personal['id']}/hold", json={"on": True}, headers=auth_headers
        )
        assert resp.status_code == 400


class TestDocumentHold:
    def test_owner_holds_one_document_and_it_cannot_be_deleted(self, client, audit_events):
        owner = make_user("dochold")
        doc_id = register_document(owner)
        try:
            with patch.object(document_service, "_save_index"):
                resp = client.post(
                    f"/api/v1/documents/{doc_id}/hold",
                    json={"on": True, "reason": "Subpoena"},
                    headers=headers(owner),
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["metadata"]["legal_hold"]["reason"] == "Subpoena"
                assert document_service.is_held(document_service.documents[doc_id])
                with patch("app.services.documents.rag_service", _rag()):
                    resp = client.delete(f"/api/v1/documents/{doc_id}", headers=headers(owner))
                assert resp.status_code == 409
                assert doc_id in document_service.documents
                resp = client.post(
                    f"/api/v1/documents/{doc_id}/hold", json={"on": False}, headers=headers(owner)
                )
                assert "legal_hold" not in resp.json()["metadata"]
            actions = [
                (e.get("details") or {}).get("action")
                for e in audit_events
                if e.get("resource_id") == doc_id
            ]
            assert actions == ["legal_hold_set", "legal_hold_lifted"]
        finally:
            document_service.documents.pop(doc_id, None)

    def test_a_stranger_cannot_hold_someone_elses_document(self, client):
        owner = make_user("dochold")
        doc_id = register_document(owner)
        try:
            resp = client.post(
                f"/api/v1/documents/{doc_id}/hold",
                json={"on": True},
                headers=headers(make_user("x")),
            )
            assert resp.status_code == 404
        finally:
            document_service.documents.pop(doc_id, None)

    async def test_clear_all_and_purge_keep_held_documents(self):
        from tests.test_document_replace_and_reindex import _doc, _service

        held = _doc("held")
        held.metadata["legal_hold"] = {"reason": "x", "set_by": "u1", "set_at": "now"}
        free = _doc("free")
        service = _service([held, free], None)
        with patch("app.services.documents.rag_service", _rag()):
            result = await service.clear_all("u1")
        assert result == {
            "cleared_documents": 1,
            "failed_documents": 0,
            "on_hold_documents": 1,
            "status": "partial",
        }
        assert "held" in service.documents and "free" not in service.documents

        service = _service([_doc("held2", metadata={"legal_hold": {"reason": "x"}})], None)
        with patch("app.services.documents.rag_service", _rag()), patch("shutil.rmtree"):
            result = await service.purge_user_data("u1")
        assert result["documents_on_hold"] == 1 and result["upload_dir_removed"] is False

    async def test_refuse_if_held_raises_before_anything_is_touched(self):
        from tests.test_document_replace_and_reindex import _doc, _service

        doc = _doc("held", matter_id="m1")
        service = _service([doc], None)
        service.held_matter_ids = {"m1"}
        rag = _rag()
        with patch("app.services.documents.rag_service", rag), pytest.raises(DocumentOnHoldError):
            await service.delete_document("held", "u1")
        rag.delete_document.assert_not_awaited()
        assert doc.status.value == "indexed"


class TestRetention:
    def test_owner_sets_a_policy_and_sees_what_is_due(self, client):
        owner = make_user("ret-owner")
        matter_id = make_shared_matter(owner)
        old_doc = register_document(owner, matter_id=matter_id)
        document_service.documents[old_doc].created_at = datetime.now(UTC) - timedelta(days=400)
        new_doc = register_document(owner, matter_id=matter_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        old_chat = ChatSessionDB(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            matter_id=matter_id,
            title="old",
            created_at=now - timedelta(days=400),
            updated_at=now - timedelta(days=400),
        )
        db_add(old_chat)
        try:
            resp = client.patch(
                f"{MATTERS}/{matter_id}", json={"retention_days": 365}, headers=headers(owner)
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["retention_days"] == 365
            due = client.get(f"{MATTERS}/{matter_id}/retention", headers=headers(owner)).json()
            assert due == {
                "matter_id": matter_id,
                "retention_days": 365,
                "legal_hold": False,
                "documents_due": 1,
                "chat_sessions_due": 1,
            }
            # A hold suspends the policy.
            client.post(f"{MATTERS}/{matter_id}/hold", json={"on": True}, headers=headers(owner))
            due = client.get(f"{MATTERS}/{matter_id}/retention", headers=headers(owner)).json()
            assert due["documents_due"] == 0 and due["chat_sessions_due"] == 0 and due["legal_hold"]
            # 0 clears the policy.
            resp = client.patch(
                f"{MATTERS}/{matter_id}", json={"retention_days": 0}, headers=headers(owner)
            )
            assert resp.json()["retention_days"] is None
        finally:
            document_service.documents.pop(old_doc, None)
            document_service.documents.pop(new_doc, None)
            db_delete(old_chat)

    def test_retention_bounds(self, client):
        owner = make_user("ret-owner")
        matter_id = make_shared_matter(owner)
        resp = client.patch(
            f"{MATTERS}/{matter_id}", json={"retention_days": 3}, headers=headers(owner)
        )
        assert resp.status_code == 400
        assert "between 7 and" in resp.json()["detail"]

    async def test_retention_run_removes_expired_content_through_the_delete_path(
        self, audit_events
    ):
        from app.database import get_db_context

        owner = make_user("ret-run")
        matter_id = make_shared_matter(owner)
        old_doc = register_document(owner, matter_id=matter_id)
        document_service.documents[old_doc].created_at = datetime.now(UTC) - timedelta(days=40)
        kept_doc = register_document(owner, matter_id=matter_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        old_chat = ChatSessionDB(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            matter_id=matter_id,
            title="old",
            created_at=now - timedelta(days=40),
            updated_at=now - timedelta(days=40),
        )
        db_add(old_chat)
        try:
            async with get_db_context() as db:
                matter = await db.get(Matter, matter_id)
                await governance.set_retention(db, matter, 30)
                await db.commit()
            with (
                patch("app.services.documents.rag_service", _rag()),
                patch.object(document_service, "_save_index"),
                patch.object(document_service, "_delete_derived_data", AsyncMock()),
            ):
                async with get_db_context() as db:
                    stats = await governance.run_retention(db)
            assert stats == {
                "matters": 1,
                "documents_deleted": 1,
                "chat_sessions_deleted": 1,
                "failed": 0,
            }
            assert old_doc not in document_service.documents
            assert kept_doc in document_service.documents
            expired = [
                e
                for e in audit_events
                if (e.get("details") or {}).get("action") == "retention_expired"
            ]
            assert {e["resource_id"] for e in expired} == {old_doc, old_chat.id}
            assert all(e["user_id"] == "system" for e in expired)
        finally:
            document_service.documents.pop(old_doc, None)
            document_service.documents.pop(kept_doc, None)
            db_delete(old_chat)
