"""Matter facts: the case team's account of a matter, read by chat and strategy.

Members edit them (anyone who may add content to the matter), non-members get
404, viewers can read but not change them, and the audit trail never carries
the text.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

import uuid
from datetime import UTC, datetime

import pytest
from app.models.enums import UserRole
from app.services.auth import User

from tests.test_matters_api import _headers, _insert_db_user

pytestmark = pytest.mark.usefixtures("no_rate_limit")

FACTS = "Client: Acme. Lease signed 1 March 2024. Landlord withheld the deposit on 2 June 2025."


def _user(uid: str, role=UserRole.ATTORNEY) -> User:
    return User(
        id=uid, email=f"{uid}@casecite.legal", name=uid, roles=[role], last_login=datetime.now(UTC)
    )


def _matter_with_member(client, member_role=UserRole.ATTORNEY):
    suffix = uuid.uuid4().hex[:8]
    owner = _user(f"fowner-{suffix}")
    member_id = f"fmember-{suffix}"
    _insert_db_user(member_id, f"{member_id}@casecite.legal", "Member")
    member = _user(member_id, member_role)
    matter = client.post("/api/v1/matters", headers=_headers(owner), json={"name": "Acme v. Beta"})
    matter_id = matter.json()["id"]
    invited = client.post(
        f"/api/v1/matters/{matter_id}/members",
        headers=_headers(owner),
        json={"email": f"{member_id}@casecite.legal"},
    )
    assert invited.status_code == 200, invited.text
    return matter_id, owner, member


def test_a_member_sets_facts_and_every_member_reads_them(client, audit_events):
    matter_id, owner, member = _matter_with_member(client)

    saved = client.put(
        f"/api/v1/matters/{matter_id}/facts", headers=_headers(member), json={"facts": FACTS}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["facts"] == FACTS
    assert saved.json()["facts_updated_by"] == member.id
    assert saved.json()["facts_updated_at"]

    detail = client.get(f"/api/v1/matters/{matter_id}", headers=_headers(owner)).json()
    assert detail["facts"] == FACTS

    events = [
        e for e in audit_events if (e.get("details") or {}).get("action") == "matter_facts_updated"
    ]
    assert events and events[-1]["details"]["length"] == len(FACTS)
    assert "Landlord" not in str(audit_events)  # the text never enters the audit log


def test_an_empty_string_clears_the_facts(client):
    matter_id, owner, _ = _matter_with_member(client)
    client.put(f"/api/v1/matters/{matter_id}/facts", headers=_headers(owner), json={"facts": FACTS})
    cleared = client.put(
        f"/api/v1/matters/{matter_id}/facts", headers=_headers(owner), json={"facts": "   "}
    )
    assert cleared.status_code == 200
    assert cleared.json()["facts"] is None


def test_non_members_get_404_and_viewers_cannot_edit(client):
    matter_id, owner, viewer = _matter_with_member(client, UserRole.VIEWER)
    stranger = _user(f"fstranger-{uuid.uuid4().hex[:8]}")
    url = f"/api/v1/matters/{matter_id}/facts"

    assert client.put(url, headers=_headers(stranger), json={"facts": FACTS}).status_code == 404
    assert client.put(url, headers=_headers(viewer), json={"facts": FACTS}).status_code == 403
    client.put(url, headers=_headers(owner), json={"facts": FACTS})
    # Viewers still read them with the matter.
    assert (
        client.get(f"/api/v1/matters/{matter_id}", headers=_headers(viewer)).json()["facts"]
        == FACTS
    )


def test_facts_are_capped(client):
    from app.models.schemas import MAX_MATTER_FACTS_CHARS

    matter_id, owner, _ = _matter_with_member(client)
    resp = client.put(
        f"/api/v1/matters/{matter_id}/facts",
        headers=_headers(owner),
        json={"facts": "x" * (MAX_MATTER_FACTS_CHARS + 1)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_matter_facts_returns_only_matters_with_facts():
    from app.database import get_db_context
    from app.services import matters as matters_service

    suffix = uuid.uuid4().hex[:8]
    async with get_db_context() as db:
        with_facts = await matters_service.create_matter(db, f"o-{suffix}", "With facts")
        without = await matters_service.create_matter(db, f"o-{suffix}", "Without facts")
        await matters_service.set_matter_facts(db, with_facts, f"  {FACTS}  ", f"o-{suffix}")
        await db.commit()
        rows = await matters_service.get_matter_facts(db, [with_facts.id, without.id, ""])
    assert rows == [{"matter_id": with_facts.id, "name": "With facts", "facts": FACTS}]
