"""Ethical walls: a walled user can never see a matter.

Admin-managed. Walling removes membership, hands the user's work in the
matter to its owner, blocks re-adding them (with the generic answer, so the
wall is not disclosed), hides the matter from a walled admin's oversight
list, and defeats a membership row created around it.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.chat_sessions import ChatSessionDB
from app.models.matters import Matter, MatterMember, MatterWall
from app.services.documents import document_service

from tests.helpers import (
    db_add,
    db_delete,
    headers,
    insert_db_user,
    make_shared_matter,
    make_user,
    register_document,
)

pytestmark = pytest.mark.usefixtures("no_rate_limit")

MATTERS = "/api/v1/matters"


def _vector_db() -> MagicMock:
    vdb = MagicMock()
    vdb.update_document_owner = AsyncMock(return_value=True)
    return vdb


def _members(matter_id: str) -> dict[str, str]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from tests.helpers import _sync_engine

    engine = _sync_engine()
    try:
        with Session(engine) as session:
            return {
                m.user_id: m.role
                for m in session.execute(
                    select(MatterMember).where(MatterMember.matter_id == matter_id)
                ).scalars()
            }
    finally:
        engine.dispose()


def _wall_rows(matter_id: str) -> list[MatterWall]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from tests.helpers import _sync_engine

    engine = _sync_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            return list(
                session.execute(
                    select(MatterWall).where(MatterWall.matter_id == matter_id)
                ).scalars()
            )
    finally:
        engine.dispose()


class TestWalls:
    def test_admin_walls_a_member_who_loses_access_and_their_work_passes_to_the_owner(
        self, client, auth_headers, audit_events
    ):
        owner = make_user("wall-owner")
        conflicted = make_user("wall-conflicted")
        insert_db_user(conflicted)
        matter_id = make_shared_matter(owner, conflicted)
        doc_id = register_document(conflicted, matter_id=matter_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        chat = ChatSessionDB(
            id=str(uuid.uuid4()),
            user_id=conflicted.id,
            matter_id=matter_id,
            title="t",
            created_at=now,
            updated_at=now,
        )
        db_add(chat)
        vdb = _vector_db()
        try:
            # Before: a member who can read the matter.
            assert (
                client.get(f"{MATTERS}/{matter_id}", headers=headers(conflicted)).status_code == 200
            )

            with (
                patch("app.services.vectordb.get_vector_db", return_value=vdb),
                patch.object(document_service, "_save_index"),
            ):
                resp = client.post(
                    f"{MATTERS}/{matter_id}/walls",
                    json={"email": conflicted.email, "reason": "Represented the other side"},
                    headers=auth_headers,
                )
            assert resp.status_code == 200, resp.text
            walls = resp.json()["walls"]
            assert [w["user_id"] for w in walls] == [conflicted.id]
            assert walls[0]["reason"] == "Represented the other side"
            assert walls[0]["email"] == conflicted.email

            # Membership is gone; the matter is a 404 to them; the owner keeps it.
            assert _members(matter_id) == {owner.id: "owner"}
            assert (
                client.get(f"{MATTERS}/{matter_id}", headers=headers(conflicted)).status_code == 404
            )
            assert client.get(f"{MATTERS}/{matter_id}", headers=headers(owner)).status_code == 200
            # Their document and conversation in the matter now belong to the owner.
            assert document_service.documents[doc_id].user_id == owner.id
            vdb.update_document_owner.assert_awaited_once_with(doc_id, owner.id)
            from sqlalchemy import select
            from sqlalchemy.orm import Session

            from tests.helpers import _sync_engine

            engine = _sync_engine()
            with Session(engine) as session:
                assert (
                    session.execute(
                        select(ChatSessionDB.user_id).where(ChatSessionDB.id == chat.id)
                    ).scalar_one()
                    == owner.id
                )
            engine.dispose()

            added = [
                e
                for e in audit_events
                if (e.get("details") or {}).get("action") == "ethical_wall_added"
            ]
            assert added and added[0]["details"]["reassigned_to_owner"]["documents"] == 1
            assert added[0]["details"]["reassigned_to_owner"]["chat_sessions"] == 1
        finally:
            document_service.documents.pop(doc_id, None)
            db_delete(chat)

    def test_owner_cannot_re_add_a_walled_user_and_is_not_told_why(
        self, client, auth_headers, audit_events
    ):
        owner = make_user("wall-owner")
        conflicted = make_user("wall-conflicted")
        insert_db_user(conflicted)
        matter_id = make_shared_matter(owner)
        with patch("app.services.vectordb.get_vector_db", return_value=_vector_db()):
            assert (
                client.post(
                    f"{MATTERS}/{matter_id}/walls",
                    json={"email": conflicted.email},
                    headers=auth_headers,
                ).status_code
                == 200
            )
        resp = client.post(
            f"{MATTERS}/{matter_id}/members",
            json={"email": conflicted.email},
            headers=headers(owner),
        )
        assert resp.status_code == 400
        assert "wall" not in resp.json()["detail"].lower()
        assert conflicted.id not in _members(matter_id)
        refused = [
            e
            for e in audit_events
            if (e.get("details") or {}).get("action") == "matter_member_add_refused_by_wall"
        ]
        assert refused and refused[0]["details"]["member_user_id"] == conflicted.id

    def test_a_membership_row_created_around_the_wall_grants_nothing(self, client, auth_headers):
        owner = make_user("wall-owner")
        conflicted = make_user("wall-conflicted")
        insert_db_user(conflicted)
        matter_id = make_shared_matter(owner)
        with patch("app.services.vectordb.get_vector_db", return_value=_vector_db()):
            client.post(
                f"{MATTERS}/{matter_id}/walls",
                json={"email": conflicted.email},
                headers=auth_headers,
            )
        db_add(
            MatterMember(
                id=str(uuid.uuid4()),
                matter_id=matter_id,
                user_id=conflicted.id,
                role="member",
                added_by=owner.id,
            )
        )
        listed = client.get(MATTERS, headers=headers(conflicted)).json()["matters"]
        assert matter_id not in {m["id"] for m in listed}
        doc_id = register_document(owner, matter_id=matter_id)
        try:
            assert (
                client.get(f"/api/v1/documents/{doc_id}", headers=headers(conflicted)).status_code
                == 404
            )
        finally:
            document_service.documents.pop(doc_id, None)

    def test_the_owner_cannot_be_walled_and_personal_matters_cannot_have_walls(
        self, client, auth_headers
    ):
        owner = make_user("wall-owner")
        insert_db_user(owner)
        matter_id = make_shared_matter(owner)
        resp = client.post(
            f"{MATTERS}/{matter_id}/walls", json={"email": owner.email}, headers=auth_headers
        )
        assert resp.status_code == 400
        assert "owner cannot be walled" in resp.json()["detail"]
        personal = next(
            m
            for m in client.get(MATTERS, headers=auth_headers).json()["matters"]
            if m["is_personal"]
        )
        resp = client.post(
            f"{MATTERS}/{personal['id']}/walls", json={"email": owner.email}, headers=auth_headers
        )
        assert resp.status_code == 400

    def test_walls_are_admin_only(self, client, non_admin_headers):
        owner = make_user("wall-owner")
        matter_id = make_shared_matter(owner)
        assert client.get(f"{MATTERS}/{matter_id}/walls", headers=headers(owner)).status_code == 403
        assert (
            client.get(f"{MATTERS}/{matter_id}/walls", headers=non_admin_headers).status_code == 403
        )
        assert (
            client.post(
                f"{MATTERS}/{matter_id}/walls", json={"email": "x@y.z"}, headers=headers(owner)
            ).status_code
            == 403
        )

    def test_a_walled_admin_does_not_see_the_matter_in_oversight(self, client, auth_headers):
        """Wall a second admin off a matter and their oversight list leaves it out."""
        from app.models.enums import UserRole

        owner = make_user("wall-owner")
        matter_id = make_shared_matter(owner)
        admin2 = make_user("wall-admin", UserRole.ADMIN)
        insert_db_user(admin2)

        before = {
            m["id"] for m in client.get(f"{MATTERS}/all", headers=headers(admin2)).json()["matters"]
        }
        assert matter_id in before
        with patch("app.services.vectordb.get_vector_db", return_value=_vector_db()):
            resp = client.post(
                f"{MATTERS}/{matter_id}/walls", json={"email": admin2.email}, headers=auth_headers
            )
        assert resp.status_code == 200, resp.text
        after = {
            m["id"] for m in client.get(f"{MATTERS}/all", headers=headers(admin2)).json()["matters"]
        }
        assert matter_id not in after
        # Lifting the wall restores the oversight view but not membership.
        resp = client.delete(f"{MATTERS}/{matter_id}/walls/{admin2.id}", headers=auth_headers)
        assert resp.status_code == 200 and resp.json()["walls"] == []
        assert matter_id in {
            m["id"] for m in client.get(f"{MATTERS}/all", headers=headers(admin2)).json()["matters"]
        }
        assert _wall_rows(matter_id) == []
        assert admin2.id not in _members(matter_id)

    def test_lifting_an_absent_wall_is_404(self, client, auth_headers):
        matter_id = make_shared_matter(make_user("wall-owner"))
        assert (
            client.delete(f"{MATTERS}/{matter_id}/walls/nobody", headers=auth_headers).status_code
            == 404
        )

    def test_walls_go_with_the_matter(self, client, auth_headers):
        owner = make_user("wall-owner")
        conflicted = make_user("wall-conflicted")
        insert_db_user(conflicted)
        matter_id = make_shared_matter(owner)
        with patch("app.services.vectordb.get_vector_db", return_value=_vector_db()):
            client.post(
                f"{MATTERS}/{matter_id}/walls",
                json={"email": conflicted.email},
                headers=auth_headers,
            )
        assert len(_wall_rows(matter_id)) == 1
        assert client.delete(f"{MATTERS}/{matter_id}", headers=headers(owner)).status_code == 200
        assert _wall_rows(matter_id) == []
        assert Matter is not None
