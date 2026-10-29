"""HTTP tests for the matter workspace endpoints: rename, ownership transfer,
the per-matter document / chat / analysis lists, bulk document moves and the
activity feed — including owner-only and member-only enforcement (404, never
403, for anyone outside the matter).
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.services.documents import document_service

from tests.helpers import db_add, headers, make_shared_matter, make_user, register_document


@pytest.fixture(autouse=True)
def _no_rate_limit():
    with patch(
        "app.middleware.security.RateLimiter.is_allowed",
        return_value=(True, {"limit": 100, "remaining": 99}),
    ):
        yield


def _personal_matter_id(client, user) -> str:
    listed = client.get("/api/v1/matters", headers=headers(user))
    assert listed.status_code == 200, listed.text
    return next(m["id"] for m in listed.json()["matters"] if m["is_personal"])


# ---------------------------------------------------------------- PATCH


def test_owner_renames_matter_and_changes_client(client, audit_events):
    owner, member = make_user("owner"), make_user("member")
    matter_id = make_shared_matter(owner, member)

    resp = client.patch(
        f"/api/v1/matters/{matter_id}",
        headers=headers(owner),
        json={"name": "  Acme v. Gamma  ", "client_name": "Acme Holdings"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Acme v. Gamma"
    assert resp.json()["client_name"] == "Acme Holdings"
    assert any(e["details"].get("action") == "matter_updated" for e in audit_events)

    # An empty client name clears it; an omitted name keeps it.
    resp = client.patch(
        f"/api/v1/matters/{matter_id}", headers=headers(owner), json={"client_name": ""}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Acme v. Gamma"
    assert resp.json()["client_name"] is None


def test_rename_is_owner_only_and_never_leaks_existence(client):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    for user in (member, stranger):
        resp = client.patch(
            f"/api/v1/matters/{matter_id}", headers=headers(user), json={"name": "Hijacked"}
        )
        assert resp.status_code == 404, resp.text
    assert client.get(f"/api/v1/matters/{matter_id}", headers=headers(owner)).json()["name"] == (
        "Acme v. Beta"
    )


def test_rename_rejects_empty_body_and_personal_matter(client):
    owner = make_user("owner")
    matter_id = make_shared_matter(owner)
    assert (
        client.patch(f"/api/v1/matters/{matter_id}", headers=headers(owner), json={}).status_code
        == 400
    )
    personal = _personal_matter_id(client, owner)
    resp = client.patch(
        f"/api/v1/matters/{personal}", headers=headers(owner), json={"name": "Mine"}
    )
    assert resp.status_code == 400


# ------------------------------------------------------------- transfer


def test_owner_transfers_matter_to_a_member(client, audit_events):
    owner, member = make_user("owner"), make_user("member")
    matter_id = make_shared_matter(owner, member)

    resp = client.post(
        f"/api/v1/matters/{matter_id}/transfer",
        headers=headers(owner),
        json={"new_owner_id": member.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owner_id"] == member.id
    assert body["role"] == "member"  # the caller's own role, after the hand-over
    roles = {m["user_id"]: m["role"] for m in body["members"]}
    assert roles == {member.id: "owner", owner.id: "member"}
    assert any(
        e["details"].get("action") == "matter_ownership_transferred"
        and e["details"].get("new_owner_id") == member.id
        for e in audit_events
    )

    # Owner-only actions now belong to the new owner; the old owner keeps access.
    assert (
        client.patch(
            f"/api/v1/matters/{matter_id}", headers=headers(owner), json={"name": "x"}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/v1/matters/{matter_id}", headers=headers(member), json={"name": "Renamed"}
        ).status_code
        == 200
    )
    assert client.get(f"/api/v1/matters/{matter_id}", headers=headers(owner)).status_code == 200


def test_transfer_requires_owner_and_an_existing_member(client):
    owner, member, outsider = make_user("owner"), make_user("member"), make_user("outsider")
    matter_id = make_shared_matter(owner, member)

    # A member cannot transfer (404: owner-only, no existence leak).
    resp = client.post(
        f"/api/v1/matters/{matter_id}/transfer",
        headers=headers(member),
        json={"new_owner_id": member.id},
    )
    assert resp.status_code == 404
    # The new owner must already be a member; the owner cannot transfer to themselves.
    for target in (outsider.id, owner.id):
        resp = client.post(
            f"/api/v1/matters/{matter_id}/transfer",
            headers=headers(owner),
            json={"new_owner_id": target},
        )
        assert resp.status_code == 400, resp.text
    assert (
        client.get(f"/api/v1/matters/{matter_id}", headers=headers(owner)).json()["owner_id"]
        == owner.id
    )


def test_personal_matter_cannot_be_transferred(client):
    owner = make_user("owner")
    personal = _personal_matter_id(client, owner)
    resp = client.post(
        f"/api/v1/matters/{personal}/transfer",
        headers=headers(owner),
        json={"new_owner_id": make_user("someone").id},
    )
    assert resp.status_code == 400


# ------------------------------------------------------------ documents


def test_matter_documents_listed_for_members_only(client):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    shared_doc = register_document(owner, filename="shared.pdf", matter_id=matter_id)
    register_document(owner, filename="private.pdf")  # personal scope, not in the matter

    for user in (owner, member):
        resp = client.get(f"/api/v1/matters/{matter_id}/documents", headers=headers(user))
        assert resp.status_code == 200, resp.text
        assert [d["id"] for d in resp.json()["documents"]] == [shared_doc]
        assert resp.json()["total"] == 1
    assert (
        client.get(f"/api/v1/matters/{matter_id}/documents", headers=headers(stranger)).status_code
        == 404
    )


def test_personal_matter_documents_include_untagged_ones(client):
    owner = make_user("owner")
    personal = _personal_matter_id(client, owner)
    untagged = register_document(owner, filename="old.pdf")
    tagged = register_document(owner, filename="new.pdf", matter_id=personal)
    other = register_document(make_user("other"), filename="theirs.pdf")

    resp = client.get(f"/api/v1/matters/{personal}/documents", headers=headers(owner))
    assert resp.status_code == 200
    ids = {d["id"] for d in resp.json()["documents"]}
    assert ids == {untagged, tagged}
    assert other not in ids


def test_move_documents_into_matter_and_back_to_personal(client, audit_events):
    owner = make_user("owner")
    matter_id = make_shared_matter(owner)
    doc_a = register_document(owner, filename="a.pdf")
    doc_b = register_document(owner, filename="b.pdf")

    resp = client.post(
        f"/api/v1/matters/{matter_id}/documents/move",
        headers=headers(owner),
        json={"document_ids": [doc_a, doc_b, doc_a]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] == [doc_a, doc_b]
    assert document_service.documents[doc_a].matter_id == matter_id
    assert document_service.documents[doc_b].matter_id == matter_id
    moved_events = [
        e for e in audit_events if e["details"].get("action") == "document_moved_to_matter"
    ]
    assert {e["resource_id"] for e in moved_events} == {doc_a, doc_b}
    assert any(e["details"].get("action") == "matter_documents_moved" for e in audit_events)

    # Back to the personal matter: owner-only visibility again (matter_id None).
    personal = _personal_matter_id(client, owner)
    resp = client.post(
        f"/api/v1/matters/{personal}/documents/move",
        headers=headers(owner),
        json={"document_ids": [doc_a]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] == [doc_a]
    assert document_service.documents[doc_a].matter_id is None
    assert document_service.documents[doc_b].matter_id == matter_id


def test_move_refuses_foreign_documents_and_foreign_destinations(client):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    owners_doc = register_document(owner, filename="owner.pdf")
    members_doc = register_document(member, filename="member.pdf")
    shared_by_member = register_document(member, filename="shared.pdf", matter_id=matter_id)

    # A member cannot re-file a colleague's personal document (404, nothing moves).
    resp = client.post(
        f"/api/v1/matters/{matter_id}/documents/move",
        headers=headers(member),
        json={"document_ids": [members_doc, owners_doc]},
    )
    assert resp.status_code == 404
    assert document_service.documents[members_doc].matter_id is None

    # A stranger cannot file into a matter they do not belong to.
    resp = client.post(
        f"/api/v1/matters/{matter_id}/documents/move",
        headers=headers(stranger),
        json={"document_ids": [register_document(stranger)]},
    )
    assert resp.status_code == 404

    # The matter OWNER may re-file a member's document that is filed under the
    # matter (same rule as delete); a plain member may not re-file the owner's.
    owner_personal = _personal_matter_id(client, owner)
    resp = client.post(
        f"/api/v1/matters/{owner_personal}/documents/move",
        headers=headers(owner),
        json={"document_ids": [shared_by_member]},
    )
    assert resp.status_code == 200, resp.text
    assert document_service.documents[shared_by_member].matter_id is None


def test_move_stops_when_vectors_cannot_be_retagged(client):
    owner = make_user("owner")
    matter_id = make_shared_matter(owner)
    doc = register_document(owner, filename="stuck.pdf")

    class _BrokenStore:
        async def update_document_matter(self, document_id, matter_id):
            return False

    with patch("app.services.vectordb.get_vector_db", return_value=_BrokenStore()):
        resp = client.post(
            f"/api/v1/matters/{matter_id}/documents/move",
            headers=headers(owner),
            json={"document_ids": [doc]},
        )
    assert resp.status_code == 503, resp.text
    assert document_service.documents[doc].matter_id is None


# ----------------------------------------------------------------- chats


def test_matter_chats_listed_for_members_with_author(client):
    from app.models.chat_sessions import ChatSessionDB

    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    now = datetime.now(UTC).replace(tzinfo=None)
    in_matter_id, elsewhere_id = str(uuid.uuid4()), str(uuid.uuid4())
    in_matter = ChatSessionDB(
        id=in_matter_id,
        user_id=member.id,
        matter_id=matter_id,
        title="Shared thread",
        created_at=now,
        updated_at=now,
    )
    elsewhere = ChatSessionDB(
        id=elsewhere_id,
        user_id=owner.id,
        matter_id=None,
        title="Private thread",
        created_at=now,
        updated_at=now,
    )
    db_add(in_matter, elsewhere)

    resp = client.get(f"/api/v1/matters/{matter_id}/chats", headers=headers(owner))
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["sessions"]
    assert [s["id"] for s in sessions] == [in_matter_id]
    assert sessions[0]["user_id"] == member.id
    assert sessions[0]["matter_id"] == matter_id
    assert (
        client.get(f"/api/v1/matters/{matter_id}/chats", headers=headers(stranger)).status_code
        == 404
    )

    # The owner's personal matter gathers their untagged threads.
    personal = _personal_matter_id(client, owner)
    resp = client.get(f"/api/v1/matters/{personal}/chats", headers=headers(owner))
    assert resp.status_code == 200
    assert elsewhere_id in {s["id"] for s in resp.json()["sessions"]}
    assert in_matter_id not in {s["id"] for s in resp.json()["sessions"]}


# -------------------------------------------------------------- analyses


def _run(user_id: str, matter_id: str | None, document_id: str | None, created_at):
    from app.models.clause_intel import ContractAnalysisRun

    return ContractAnalysisRun(
        id=uuid.uuid4().hex,
        user_id=user_id,
        matter_id=matter_id,
        document_id=document_id,
        contract_type="nda",
        document_length_chars=1000,
        summary={"issues": [{"title": "Missing cap"}, {"title": "Auto-renewal"}]},
        is_complete=True,
        created_at=created_at,
    )


def test_matter_analyses_listed_newest_first_with_filename(client):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    doc = register_document(owner, filename="msa.pdf", matter_id=matter_id)
    now = datetime.now(UTC).replace(tzinfo=None)
    older = _run(owner.id, matter_id, doc, now - timedelta(hours=1))
    newer = _run(member.id, matter_id, None, now)
    unrelated = _run(owner.id, None, None, now)
    older_id, newer_id = older.id, newer.id  # read before db_add expires the rows
    db_add(older, newer, unrelated)

    resp = client.get(f"/api/v1/matters/{matter_id}/analyses", headers=headers(member))
    assert resp.status_code == 200, resp.text
    items = resp.json()["analyses"]
    assert [a["analysis_id"] for a in items] == [newer_id, older_id]
    assert items[1]["filename"] == "msa.pdf"
    assert items[1]["issues"] == 2
    assert items[0]["user_id"] == member.id
    assert (
        client.get(f"/api/v1/matters/{matter_id}/analyses", headers=headers(stranger)).status_code
        == 404
    )


# -------------------------------------------------------------- activity


def test_matter_activity_feed_gathers_matter_and_filed_content_events(client):
    from app.models.audit import AuditLog as AuditLogDB

    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    other_matter = make_shared_matter(owner)
    doc = register_document(owner, filename="filed.pdf", matter_id=matter_id)
    other_doc = register_document(owner, filename="elsewhere.pdf", matter_id=other_matter)
    now = datetime.now(UTC).replace(tzinfo=None)

    def entry(resource_type, resource_id, action, when, success=True):
        return AuditLogDB(
            timestamp=when,
            event_type="data_modification",
            user_id=None,
            user_email="o***@casecite.legal",
            resource_type=resource_type,
            resource_id=resource_id,
            action_details={"action": action},
            success=success,
        )

    db_add(
        entry("matter", matter_id, "matter_created", now - timedelta(minutes=3)),
        entry("document", doc, "document_uploaded", now - timedelta(minutes=2)),
        entry("matter", matter_id, "matter_member_added", now - timedelta(minutes=1)),
        entry("matter", other_matter, "matter_created", now),
        entry("document", other_doc, "document_uploaded", now),
        entry("document", doc, "document_deleted", now, success=False),
    )

    resp = client.get(f"/api/v1/matters/{matter_id}/activity", headers=headers(member))
    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    assert [e["action"] for e in events] == [
        "document_deleted",
        "matter_member_added",
        "document_uploaded",
        "matter_created",
    ]
    assert events[0]["success"] is False
    assert events[0]["resource_id"] == doc
    assert all(e["resource_id"] in {matter_id, doc} for e in events)

    limited = client.get(f"/api/v1/matters/{matter_id}/activity?limit=2", headers=headers(owner))
    assert [e["action"] for e in limited.json()["events"]] == [
        "document_deleted",
        "matter_member_added",
    ]
    assert (
        client.get(f"/api/v1/matters/{matter_id}/activity", headers=headers(stranger)).status_code
        == 404
    )
