# ruff: noqa: F811  (the `users` fixture is imported from test_router_users)
"""Offboarding: deleting a user never silently destroys the firm's work.

A delete with no decision is refused while the user owns work product and
the inventory is returned; mode=transfer hands everything to a colleague
first (documents, matters, conversations, analyses, review tables, schemas,
workflows); mode=purge destroys it knowingly. Personal data is purged either
way.
"""

import json
import os
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.chat_sessions import ChatSessionDB
from app.models.matters import Matter, MatterMember
from app.models.review_tables import ReviewSchema, ReviewTable
from app.models.schemas import ConnectorType, Document, DocumentStatus
from app.models.workflows import Workflow
from app.services import offboarding
from app.services.documents import DocumentService, document_service

from tests.helpers import db_add, db_delete, make_shared_matter, make_user, register_document
from tests.test_router_users import BASE, users  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("no_rate_limit")


# ---- service: documents --------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    with patch("app.services.documents.settings") as mock_settings:
        mock_settings.upload_dir = str(tmp_path)
        service = DocumentService()
        yield service


def _doc(doc_id: str, user_id: str) -> Document:
    return Document(
        id=doc_id,
        user_id=user_id,
        filename=f"{doc_id}.pdf",
        content_type="application/pdf",
        size=10,
        source=ConnectorType.LOCAL,
        status=DocumentStatus.INDEXED,
        created_at=datetime.now(UTC),
        chunk_count=3,
    )


def _vector_db(ok: bool = True) -> MagicMock:
    vdb = MagicMock()
    vdb.update_document_owner = AsyncMock(return_value=ok)
    return vdb


async def test_transfer_documents_moves_tags_file_and_registry(svc, tmp_path):
    svc.documents = {"a": _doc("a", "old"), "b": _doc("b", "old"), "c": _doc("c", "other")}
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "a.pdf").write_bytes(b"x")
    (tmp_path / "old" / "b.pdf").write_bytes(b"y")
    vdb = _vector_db()
    with (
        patch("app.services.documents.document_service", svc),
        patch("app.services.vectordb.get_vector_db", return_value=vdb),
    ):
        moved = await offboarding.transfer_documents("old", "new")
    assert moved == 2
    assert svc.documents["a"].user_id == "new" and svc.documents["b"].user_id == "new"
    assert svc.documents["c"].user_id == "other"
    assert sorted(os.listdir(tmp_path / "new")) == ["a.pdf", "b.pdf"]
    assert os.listdir(tmp_path / "old") == []
    assert {c.args for c in vdb.update_document_owner.await_args_list} == {
        ("a", "new"),
        ("b", "new"),
    }
    saved = {d["id"]: d["user_id"] for d in json.loads((tmp_path / "index.json").read_text())}
    assert saved == {"a": "new", "b": "new", "c": "other"}


async def test_transfer_stops_at_a_document_whose_tags_cannot_be_rewritten(svc, tmp_path):
    svc.documents = {"a": _doc("a", "old"), "b": _doc("b", "old")}
    vdb = MagicMock()
    vdb.update_document_owner = AsyncMock(side_effect=[True, False])
    with (
        patch("app.services.documents.document_service", svc),
        patch("app.services.vectordb.get_vector_db", return_value=vdb),
        pytest.raises(offboarding.TransferError, match="could not be re-owned"),
    ):
        await offboarding.transfer_documents("old", "new")
    # The first stays moved, the second stays with its owner, and that is on disk.
    assert svc.documents["a"].user_id == "new"
    assert svc.documents["b"].user_id == "old"
    saved = {d["id"]: d["user_id"] for d in json.loads((tmp_path / "index.json").read_text())}
    assert saved == {"a": "new", "b": "old"}


async def test_transfer_puts_tags_back_when_the_file_cannot_move(svc, tmp_path):
    svc.documents = {"a": _doc("a", "old")}
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "a.pdf").write_bytes(b"x")
    vdb = _vector_db()
    with (
        patch("app.services.documents.document_service", svc),
        patch("app.services.vectordb.get_vector_db", return_value=vdb),
        patch("app.services.offboarding.os.replace", side_effect=PermissionError(13, "busy")),
        pytest.raises(offboarding.TransferError, match="stored file could not be moved"),
    ):
        await offboarding.transfer_documents("old", "new")
    assert svc.documents["a"].user_id == "old"
    assert [c.args for c in vdb.update_document_owner.await_args_list] == [
        ("a", "new"),
        ("a", "old"),
    ]


# ---- HTTP: the decision ---------------------------------------------------------


def _seed_work(owner, colleague):
    """A shared matter, a document, a conversation, a review table, a schema
    and a workflow owned by ``owner``; the colleague is a member of the matter."""
    matter_id = make_shared_matter(owner, colleague)
    doc_id = register_document(owner, matter_id=matter_id)
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        ChatSessionDB(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            matter_id=matter_id,
            title="t",
            created_at=now,
            updated_at=now,
        ),
        ReviewTable(
            id=str(uuid.uuid4()),
            owner_id=owner.id,
            matter_id=matter_id,
            name="Leases",
            fields=[],
            document_ids=[],
        ),
        ReviewSchema(id=str(uuid.uuid4()), owner_id=owner.id, name="Lease fields", fields=[]),
        Workflow(
            id=str(uuid.uuid4()), owner_id=owner.id, matter_id=matter_id, name="Intake", steps=[]
        ),
    ]
    db_add(*rows)
    return matter_id, doc_id, rows


def _cleanup(doc_id, rows, matter_id):
    document_service.documents.pop(doc_id, None)
    db_delete(*rows)
    db_delete_matter(matter_id)


def db_delete_matter(matter_id: str) -> None:
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from tests.helpers import _sync_engine

    engine = _sync_engine()
    try:
        with Session(engine) as session:
            session.execute(delete(MatterMember).where(MatterMember.matter_id == matter_id))
            session.execute(delete(Matter).where(Matter.id == matter_id))
            session.commit()
    finally:
        engine.dispose()


class TestDeleteDecision:
    def test_ownership_inventory(self, client, auth_headers, users):
        uid, email = users.create()
        owner = make_user_like(uid, email)
        colleague = make_user("colleague")
        matter_id, doc_id, rows = _seed_work(owner, colleague)
        try:
            resp = client.get(f"{BASE}/{uid}/ownership", headers=auth_headers)
            assert resp.status_code == 200, resp.text
            owned = resp.json()["owned"]
            assert owned["shared_matters"] == 1
            assert owned["documents"] == 1
            assert owned["chat_sessions"] == 1
            assert owned["review_tables"] == 1
            assert owned["review_schemas"] == 1
            assert owned["workflows"] == 1
            assert owned["total"] == 6
        finally:
            _cleanup(doc_id, rows, matter_id)

    def test_delete_without_a_decision_is_refused_while_work_is_owned(
        self, client, auth_headers, users
    ):
        uid, email = users.create()
        owner = make_user_like(uid, email)
        matter_id, doc_id, rows = _seed_work(owner, make_user("c"))
        try:
            resp = client.delete(f"{BASE}/{uid}", headers=auth_headers)
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert "owns work product" in detail
            assert "mode=transfer" in detail and "mode=purge" in detail
            assert "1 documents" in detail
            assert users.get(uid) is not None
            assert doc_id in document_service.documents
        finally:
            _cleanup(doc_id, rows, matter_id)

    def test_delete_without_a_decision_proceeds_when_nothing_is_owned(
        self, client, auth_headers, users
    ):
        uid, _ = users.create()
        resp = client.delete(f"{BASE}/{uid}", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == "none"
        assert users.get(uid) is None

    def test_transfer_hands_everything_to_the_colleague_then_deletes(
        self, client, auth_headers, users, audit_events
    ):
        uid, email = users.create()
        cid, cemail = users.create()
        owner = make_user_like(uid, email)
        colleague = make_user_like(cid, cemail)
        matter_id, doc_id, rows = _seed_work(owner, colleague)
        vdb = _vector_db()
        try:
            with (
                patch("app.services.vectordb.get_vector_db", return_value=vdb),
                patch.object(document_service, "_save_index"),
                patch.object(
                    document_service,
                    "purge_user_data",
                    AsyncMock(
                        return_value={
                            "documents_deleted": 0,
                            "documents_failed": 0,
                            "upload_dir_removed": True,
                        }
                    ),
                ),
            ):
                resp = client.delete(
                    f"{BASE}/{uid}",
                    params={"mode": "transfer", "transfer_to": cid},
                    headers=auth_headers,
                )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["mode"] == "transfer" and body["transferred_to"] == cid
            assert body["transferred"] == {
                "documents": 1,
                "shared_matters": 1,
                "chat_sessions": 1,
                "contract_analyses": 0,
                "review_tables": 1,
                "review_schemas": 1,
                "workflows": 1,
            }
            assert users.get(uid) is None
            # The document is now the colleague's, in the same matter.
            assert document_service.documents[doc_id].user_id == cid
            assert document_service.documents[doc_id].matter_id == matter_id
            vdb.update_document_owner.assert_awaited_once_with(doc_id, cid)
            # The matter is owned by the colleague, who is its owner-member.
            from sqlalchemy import select
            from sqlalchemy.orm import Session

            from tests.helpers import _sync_engine

            engine = _sync_engine()
            with Session(engine) as session:
                matter = session.execute(select(Matter).where(Matter.id == matter_id)).scalar_one()
                assert matter.owner_id == cid
                members = {
                    m.user_id: m.role
                    for m in session.execute(
                        select(MatterMember).where(MatterMember.matter_id == matter_id)
                    ).scalars()
                }
                assert members == {cid: "owner"}  # the departed user's membership is gone
                for model, column in (
                    (ChatSessionDB, ChatSessionDB.user_id),
                    (ReviewTable, ReviewTable.owner_id),
                    (ReviewSchema, ReviewSchema.owner_id),
                    (Workflow, Workflow.owner_id),
                ):
                    owners = set(
                        session.execute(
                            select(column).where(model.id.in_([r.id for r in rows]))
                        ).scalars()
                    )
                    assert owners <= {cid}, model
            engine.dispose()
            transfers = [
                e
                for e in audit_events
                if (e.get("details") or {}).get("action") == "work_product_transferred"
            ]
            assert len(transfers) == 1 and transfers[0]["details"]["to_user_id"] == cid
        finally:
            _cleanup(doc_id, rows, matter_id)

    def test_transfer_needs_a_valid_active_receiver(self, client, auth_headers, users):
        uid, email = users.create()
        inactive, _ = users.create(is_active=False)
        matter_id, doc_id, rows = _seed_work(make_user_like(uid, email), make_user("c"))
        try:
            for params, text in (
                ({"mode": "transfer"}, "needs transfer_to"),
                ({"mode": "transfer", "transfer_to": uid}, "their own work"),
                ({"mode": "transfer", "transfer_to": "nobody"}, "not found"),
                ({"mode": "transfer", "transfer_to": inactive}, "deactivated"),
                ({"mode": "shred"}, "mode must be one of"),
            ):
                resp = client.delete(f"{BASE}/{uid}", params=params, headers=auth_headers)
                assert resp.status_code == 400, (params, resp.text)
                assert text in resp.json()["detail"]
            assert users.get(uid) is not None
        finally:
            _cleanup(doc_id, rows, matter_id)

    def test_transfer_that_fails_on_a_document_keeps_the_account(self, client, auth_headers, users):
        uid, email = users.create()
        cid, cemail = users.create()
        matter_id, doc_id, rows = _seed_work(
            make_user_like(uid, email), make_user_like(cid, cemail)
        )
        try:
            with (
                patch("app.services.vectordb.get_vector_db", return_value=_vector_db(ok=False)),
                patch.object(document_service, "_save_index"),
            ):
                resp = client.delete(
                    f"{BASE}/{uid}",
                    params={"mode": "transfer", "transfer_to": cid},
                    headers=auth_headers,
                )
            assert resp.status_code == 503, resp.text
            assert "could not be re-owned" in resp.json()["detail"]
            assert users.get(uid) is not None
            assert document_service.documents[doc_id].user_id == uid
        finally:
            _cleanup(doc_id, rows, matter_id)

    def test_purge_destroys_the_work_knowingly(self, client, auth_headers, users, audit_events):
        uid, email = users.create()
        matter_id, doc_id, rows = _seed_work(make_user_like(uid, email), make_user("c"))
        try:
            with patch.object(
                document_service,
                "purge_user_data",
                AsyncMock(
                    return_value={
                        "documents_deleted": 1,
                        "documents_failed": 0,
                        "upload_dir_removed": True,
                    }
                ),
            ):
                resp = client.delete(
                    f"{BASE}/{uid}", params={"mode": "purge"}, headers=auth_headers
                )
            assert resp.status_code == 200, resp.text
            assert resp.json()["mode"] == "purge"
            assert users.get(uid) is None
            from sqlalchemy import select
            from sqlalchemy.orm import Session

            from tests.helpers import _sync_engine

            engine = _sync_engine()
            with Session(engine) as session:
                assert (
                    session.execute(
                        select(Matter).where(Matter.id == matter_id)
                    ).scalar_one_or_none()
                    is None
                )
                for model in (ChatSessionDB, ReviewTable, ReviewSchema, Workflow):
                    left = (
                        session.execute(select(model.id).where(model.id.in_([r.id for r in rows])))
                        .scalars()
                        .all()
                    )
                    assert left == [], model
            engine.dispose()
            deletes = [
                e
                for e in audit_events
                if e.get("resource_id") == uid and e["event_type"].name == "USER_DELETE"
            ]
            assert deletes and deletes[-1]["details"]["mode"] == "purge"
        finally:
            document_service.documents.pop(doc_id, None)


def make_user_like(uid: str, email: str):
    """A token-subject User matching a seeded DB row (for the helpers that
    take a User)."""
    from app.services.auth import User, UserRole

    return User(
        id=uid,
        email=email,
        name="Managed User",
        roles=[UserRole.ATTORNEY],
        last_login=datetime.now(UTC),
    )
