"""HTTP tests for the Matter API: create, list, get, invite/remove members,
delete — including owner-only enforcement and access scoping.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from app.models.enums import UserRole
from app.services.auth import User


@pytest.fixture(autouse=True)
def _no_rate_limit():
    with patch(
        "app.middleware.security.RateLimiter.is_allowed",
        return_value=(True, {"limit": 100, "remaining": 99}),
    ):
        yield


def _user(uid: str, email: str) -> User:
    return User(
        id=uid, email=email, name=uid, roles=[UserRole.ATTORNEY], last_login=datetime.now(UTC)
    )


def _headers(user: User) -> dict:
    from tests.conftest import make_auth_headers

    return make_auth_headers(user)


def _insert_db_user(uid: str, email: str, name: str) -> None:
    """Insert a users row via a direct synchronous SQLite write.

    The app resolves invitees by email against the users table; a direct
    file-committed write is immediately visible to the app's connection,
    avoiding cross-event-loop async-engine visibility flakiness in tests.
    """
    import json
    import sqlite3

    from app.database import async_engine

    path = async_engine.url.database
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute(
            "INSERT INTO users (id, email, name, password_hash, roles, is_active, "
            "email_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                email,
                name,
                "x",
                json.dumps(["attorney"]),
                1,
                1,
                datetime.now(UTC).replace(tzinfo=None).isoformat(),
            ),
        )
        con.commit()
    finally:
        con.close()


def test_matter_lifecycle_create_invite_access_remove(client):
    suffix = uuid.uuid4().hex[:8]
    owner = _user(f"mowner-{suffix}", f"mowner-{suffix}@casecite.legal")
    # The invitee must exist in the users table for email resolution.
    colleague_id = f"mcolleague-{suffix}"
    colleague_email = f"mcolleague-{suffix}@casecite.legal"
    _insert_db_user(colleague_id, colleague_email, "Colleague")
    colleague = _user(colleague_id, colleague_email)
    stranger = _user(f"mstranger-{suffix}", f"mstranger-{suffix}@casecite.legal")

    # Create a matter.
    created = client.post(
        "/api/v1/matters",
        headers=_headers(owner),
        json={"name": "Acme v. Beta", "client_name": "Acme Corp"},
    )
    assert created.status_code == 201, created.text
    matter = created.json()
    matter_id = matter["id"]
    assert matter["role"] == "owner"
    assert matter["is_personal"] is False
    assert matter["member_count"] == 1

    # It shows in the owner's list (alongside their auto-created personal matter).
    listed = client.get("/api/v1/matters", headers=_headers(owner))
    assert listed.status_code == 200
    names = {m["id"]: m for m in listed.json()["matters"]}
    assert matter_id in names
    assert any(m["is_personal"] for m in listed.json()["matters"])

    # A stranger can't see it.
    assert client.get(f"/api/v1/matters/{matter_id}", headers=_headers(stranger)).status_code == 404

    # Invite the colleague by email.
    invited = client.post(
        f"/api/v1/matters/{matter_id}/members",
        headers=_headers(owner),
        json={"email": colleague_email},
    )
    assert invited.status_code == 200, invited.text
    member_ids = {m["user_id"] for m in invited.json()["members"]}
    assert colleague_id in member_ids
    assert invited.json()["member_count"] == 2

    # The colleague can now see the matter; the stranger still cannot.
    assert (
        client.get(f"/api/v1/matters/{matter_id}", headers=_headers(colleague)).status_code == 200
    )
    assert client.get(f"/api/v1/matters/{matter_id}", headers=_headers(stranger)).status_code == 404

    # Non-owner cannot invite or remove (owner-only), and it 404s (no existence leak).
    assert (
        client.post(
            f"/api/v1/matters/{matter_id}/members",
            headers=_headers(colleague),
            json={"email": colleague_email},
        ).status_code
        == 404
    )

    # Owner removes the colleague.
    removed = client.delete(
        f"/api/v1/matters/{matter_id}/members/{colleague_id}", headers=_headers(owner)
    )
    assert removed.status_code == 200
    assert colleague_id not in {m["user_id"] for m in removed.json()["members"]}
    # Access revoked.
    assert (
        client.get(f"/api/v1/matters/{matter_id}", headers=_headers(colleague)).status_code == 404
    )


def test_invite_does_not_reveal_whether_an_account_exists(client, audit_events):
    """An unknown address and a deactivated account get the same generic 400,
    so the endpoint cannot be used to probe which emails have accounts."""
    from app.models.auth import User as DBUser

    from tests.helpers import db_add

    suffix = uuid.uuid4().hex[:8]
    owner = _user(f"mo2-{suffix}", f"mo2-{suffix}@casecite.legal")
    inactive_email = f"departed-{suffix}@casecite.legal"
    db_add(
        DBUser(
            id=f"departed-{suffix}",
            email=inactive_email,
            name="Departed",
            password_hash="x",
            roles=["attorney"],
            is_active=False,
        )
    )
    created = client.post("/api/v1/matters", headers=_headers(owner), json={"name": "Solo"})
    matter_id = created.json()["id"]

    responses = [
        client.post(
            f"/api/v1/matters/{matter_id}/members", headers=_headers(owner), json={"email": email}
        )
        for email in ("nobody-here@example.com", inactive_email)
    ]
    assert [r.status_code for r in responses] == [400, 400]
    assert responses[0].json() == responses[1].json()
    assert "No user" not in responses[0].text

    detail = client.get(f"/api/v1/matters/{matter_id}", headers=_headers(owner)).json()
    assert detail["member_count"] == 1  # the deactivated account was not added

    failed = [e for e in audit_events if e["details"].get("action") == "matter_member_add_failed"]
    assert len(failed) == 2
    assert all(e["success"] is False and e["resource_id"] == matter_id for e in failed)


def test_delete_matter_owner_only(client):
    owner = _user(f"mo3-{uuid.uuid4().hex[:8]}", f"mo3-{uuid.uuid4().hex[:8]}@casecite.legal")
    stranger = _user(f"ms3-{uuid.uuid4().hex[:8]}", f"ms3-{uuid.uuid4().hex[:8]}@casecite.legal")
    matter_id = client.post("/api/v1/matters", headers=_headers(owner), json={"name": "X"}).json()[
        "id"
    ]

    assert (
        client.delete(f"/api/v1/matters/{matter_id}", headers=_headers(stranger)).status_code == 404
    )
    assert client.delete(f"/api/v1/matters/{matter_id}", headers=_headers(owner)).status_code == 200
    assert client.get(f"/api/v1/matters/{matter_id}", headers=_headers(owner)).status_code == 404


def test_create_and_share_require_the_matters_permission(client):
    """Deciding who can read privileged documents is not a viewer/paralegal action."""
    suffix = uuid.uuid4().hex[:8]
    for role in (UserRole.VIEWER, UserRole.PARALEGAL):
        user = User(
            id=f"mnp-{role.value}-{suffix}",
            email=f"mnp-{role.value}-{suffix}@casecite.legal",
            name="No Permission",
            roles=[role],
            last_login=datetime.now(UTC),
        )
        resp = client.post("/api/v1/matters", headers=_headers(user), json={"name": "Nope"})
        assert resp.status_code == 403, role
        # Listing the matters you belong to stays available to every role.
        assert client.get("/api/v1/matters", headers=_headers(user)).status_code == 200


def test_matter_changes_are_audited(client, audit_events):
    suffix = uuid.uuid4().hex[:8]
    owner = _user(f"mau-{suffix}", f"mau-{suffix}@casecite.legal")
    colleague_id, colleague_email = f"mauc-{suffix}", f"mauc-{suffix}@casecite.legal"
    _insert_db_user(colleague_id, colleague_email, "Colleague")

    matter_id = client.post(
        "/api/v1/matters", headers=_headers(owner), json={"name": "Confidential Name"}
    ).json()["id"]
    client.post(
        f"/api/v1/matters/{matter_id}/members",
        headers=_headers(owner),
        json={"email": colleague_email},
    )
    client.delete(f"/api/v1/matters/{matter_id}/members/{colleague_id}", headers=_headers(owner))
    client.delete(f"/api/v1/matters/{matter_id}", headers=_headers(owner))

    events = [e for e in audit_events if e.get("resource_type") == "matter"]
    assert [e["details"]["action"] for e in events] == [
        "matter_created",
        "matter_member_added",
        "matter_member_removed",
        "matter_deleted",
    ]
    assert all(e["resource_id"] == matter_id and e["user_id"] == owner.id for e in events)
    assert events[1]["details"]["member_user_id"] == colleague_id
    # Matter and client names are confidential and stay out of the audit trail.
    assert "Confidential Name" not in str(events)


def test_admin_can_list_all_shared_matters(client):
    suffix = uuid.uuid4().hex[:8]
    owner = _user(f"mal-{suffix}", f"mal-{suffix}@casecite.legal")
    matter_id = client.post(
        "/api/v1/matters", headers=_headers(owner), json={"name": "Walled Matter"}
    ).json()["id"]
    client.get("/api/v1/matters", headers=_headers(owner))  # creates the personal matter

    admin = User(
        id=f"madmin-{suffix}",
        email=f"madmin-{suffix}@casecite.legal",
        name="Admin",
        roles=[UserRole.ADMIN],
        last_login=datetime.now(UTC),
    )
    # A non-admin cannot use the oversight listing.
    assert client.get("/api/v1/matters/all", headers=_headers(owner)).status_code == 403

    resp = client.get("/api/v1/matters/all", headers=_headers(admin))
    assert resp.status_code == 200
    matters = {m["id"]: m for m in resp.json()["matters"]}
    assert matters[matter_id]["owner_id"] == owner.id
    assert matters[matter_id]["role"] == "none"  # oversight is not membership
    assert not any(m["is_personal"] for m in matters.values())
    # ...and it grants no access to the matter itself.
    assert client.get(f"/api/v1/matters/{matter_id}", headers=_headers(admin)).status_code == 404
