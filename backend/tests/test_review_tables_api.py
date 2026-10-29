"""HTTP tests for tabular review: table CRUD and its owner / member / stranger
rules (404, never 403, outside the matter), the background run through the
job queue with a fake model, the cost estimate, and the cited exports.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

import io
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.review_tables import ReviewCell
from app.services.review_tables.service import ReviewTableService, review_table_service
from openpyxl import load_workbook

from tests.helpers import db_count, headers, make_shared_matter, make_user, register_document

CONTRACT = (
    'This Agreement is effective as of January 5, 2026 (the "Effective Date").\n'
    "GOVERNING LAW. This Agreement shall be governed by the laws of the State of Delaware.\n"
    "LIMITATION OF LIABILITY. Liability is capped at the fees paid in the twelve months "
    "preceding the claim.\n"
)
QUOTE = "This Agreement shall be governed by the laws of the State of Delaware."

FIELDS = [
    {"name": "Governing law", "description": "state or country", "type": "text"},
    {"name": "Effective date", "type": "date"},
]


@pytest.fixture(autouse=True)
def _no_rate_limit():
    with patch(
        "app.middleware.security.RateLimiter.is_allowed",
        return_value=(True, {"limit": 100, "remaining": 99}),
    ):
        yield


def _fake_model_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    return response


def _answer(doc_text: str = CONTRACT) -> dict:
    return {
        "fields": {
            "governing_law": {"value": "Delaware", "quote": QUOTE, "not_found": False},
            "effective_date": {
                "value": "2026-01-05",
                "quote": "not in the text",
                "not_found": False,
            },
        }
    }


def _create(client, user, *, document_ids=None, matter_id=None, fields=FIELDS, name="NDA review"):
    body = {"name": name, "fields": fields, "document_ids": document_ids or []}
    if matter_id:
        body["matter_id"] = matter_id
    resp = client.post("/api/v1/review-tables", headers=headers(user), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _poll(client, user, job_id: str, attempts: int = 200) -> dict:
    for _ in range(attempts):
        job = client.get(f"/api/v1/jobs/{job_id}", headers=headers(user))
        assert job.status_code == 200, job.text
        if job.json()["status"] in ("completed", "failed", "cancelled"):
            return job.json()
        time.sleep(0.05)
    raise AssertionError("job did not settle")


# ------------------------------------------------------------------ create / read


def test_create_derives_field_keys_and_lists_scope_documents(client, audit_events):
    owner = make_user("owner")
    d1 = register_document(owner, filename="a.pdf")
    d2 = register_document(owner, filename="b.pdf")

    table = _create(client, owner, document_ids=[d1, d2, d1])
    assert [f["key"] for f in table["fields"]] == ["governing_law", "effective_date"]
    assert table["fields"][1]["type"] == "date"
    assert [d["id"] for d in table["documents"]] == [d1, d2]  # de-duplicated, order kept
    assert table["documents"][0]["filename"] == "a.pdf"
    assert table["role"] == "owner" and table["cells"] == []
    assert table["document_count"] == 2 and table["field_count"] == 2
    assert any(e["details"].get("action") == "review_table_created" for e in audit_events)

    listed = client.get("/api/v1/review-tables", headers=headers(owner))
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()["tables"]] == [table["id"]]


def test_create_rejects_duplicate_fields_and_foreign_documents(client):
    owner, other = make_user("owner"), make_user("other")
    theirs = register_document(other)
    dup = [{"name": "Term"}, {"name": "term"}]
    resp = client.post(
        "/api/v1/review-tables",
        headers=headers(owner),
        json={"name": "x", "fields": dup, "document_ids": []},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/review-tables",
        headers=headers(owner),
        json={"name": "x", "fields": FIELDS, "document_ids": [theirs]},
    )
    assert resp.status_code == 404  # someone else's private document: no existence leak


def test_matter_table_defaults_to_the_matter_documents_and_is_shared(client):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    filed = register_document(owner, filename="filed.pdf", matter_id=matter_id)
    register_document(owner, filename="private.pdf")  # not in the matter

    table = _create(client, owner, matter_id=matter_id)
    assert [d["id"] for d in table["documents"]] == [filed]

    as_member = client.get(f"/api/v1/review-tables/{table['id']}", headers=headers(member))
    assert as_member.status_code == 200
    assert as_member.json()["role"] == "member"
    assert (
        client.get("/api/v1/review-tables", headers=headers(member)).json()["tables"][0]["id"]
        == (table["id"])
    )

    for user in (stranger,):
        assert (
            client.get(f"/api/v1/review-tables/{table['id']}", headers=headers(user)).status_code
            == 404
        )
        assert client.get("/api/v1/review-tables", headers=headers(user)).json()["tables"] == []

    # A matter the caller does not belong to cannot be named at all.
    resp = client.post(
        "/api/v1/review-tables",
        headers=headers(stranger),
        json={"name": "x", "fields": FIELDS, "matter_id": matter_id},
    )
    assert resp.status_code == 404


# ------------------------------------------------------------------ update / delete


def test_update_is_owner_only_and_drops_cells_of_removed_fields(client, audit_events):
    owner, member = make_user("owner"), make_user("member")
    matter_id = make_shared_matter(owner, member)
    doc = register_document(owner, matter_id=matter_id)
    table = _create(client, owner, matter_id=matter_id, document_ids=[doc])
    tid = table["id"]

    # Seed cells for both fields through a (fake) run.
    _run_to_completion(client, owner, tid)
    assert db_count(ReviewCell, ReviewCell.table_id == tid) == 2

    # A member can read but not edit: 404, not 403.
    resp = client.patch(
        f"/api/v1/review-tables/{tid}", headers=headers(member), json={"name": "Hijack"}
    )
    assert resp.status_code == 404
    assert (
        client.patch(f"/api/v1/review-tables/{tid}", headers=headers(owner), json={}).status_code
        == 400
    )

    resp = client.patch(
        f"/api/v1/review-tables/{tid}",
        headers=headers(owner),
        json={"name": "  Renamed  ", "fields": [{"name": "Governing law"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Renamed"
    assert [f["key"] for f in body["fields"]] == ["governing_law"]
    assert [c["field_key"] for c in body["cells"]] == ["governing_law"]
    assert db_count(ReviewCell, ReviewCell.table_id == tid) == 1
    assert any(
        e["details"].get("action") == "review_table_updated"
        and e["details"].get("changed") == ["fields", "name"]
        for e in audit_events
    )

    # Dropping the document drops its cells too.
    resp = client.patch(
        f"/api/v1/review-tables/{tid}", headers=headers(owner), json={"document_ids": []}
    )
    assert resp.status_code == 200
    assert resp.json()["documents"] == [] and resp.json()["cells"] == []
    assert db_count(ReviewCell, ReviewCell.table_id == tid) == 0


def test_delete_is_owner_only_and_removes_cells(client, audit_events):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    doc = register_document(owner, matter_id=matter_id)
    tid = _create(client, owner, matter_id=matter_id, document_ids=[doc])["id"]
    _run_to_completion(client, owner, tid)
    assert db_count(ReviewCell, ReviewCell.table_id == tid) == 2

    for user in (member, stranger):
        assert (
            client.delete(f"/api/v1/review-tables/{tid}", headers=headers(user)).status_code == 404
        )
    resp = client.delete(f"/api/v1/review-tables/{tid}", headers=headers(owner))
    assert resp.status_code == 200 and resp.json() == {"status": "deleted", "id": tid}
    assert client.get(f"/api/v1/review-tables/{tid}", headers=headers(owner)).status_code == 404
    assert db_count(ReviewCell, ReviewCell.table_id == tid) == 0
    assert any(e["details"].get("action") == "review_table_deleted" for e in audit_events)


# ---------------------------------------------------------------------- estimate


def test_estimate_counts_only_readable_documents(client):
    owner = make_user("owner")
    d1 = register_document(owner)
    d2 = register_document(owner)
    tid = _create(client, owner, document_ids=[d1, d2])["id"]
    # One document disappears from the registry after the table was made.
    from app.services.documents import document_service

    document_service.documents.pop(d2)

    resp = client.get(f"/api/v1/review-tables/{tid}/estimate", headers=headers(owner))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["documents"] == 1 and body["fields"] == 2 and body["cells"] == 2
    assert body["llm_calls"] == 1 and body["approx_input_tokens"] > 0 and body["model"]

    detail = client.get(f"/api/v1/review-tables/{tid}", headers=headers(owner)).json()
    assert [d["accessible"] for d in detail["documents"]] == [True, False]


# --------------------------------------------------------------------------- run


def _run_to_completion(
    client, user, tid: str, body: dict | None = None, answer: dict | None = None
):
    """POST a run with the model and document text faked; return (started, job)."""
    fake_chat = AsyncMock(return_value=_fake_model_response(answer or _answer()))
    with (
        patch.object(ReviewTableService, "_llm_client", return_value=MagicMock()),
        patch.object(review_table_service, "document_text", AsyncMock(return_value=CONTRACT)),
        patch("app.services.review_tables.extraction.openai_chat", fake_chat),
    ):
        started = client.post(
            f"/api/v1/review-tables/{tid}/run", headers=headers(user), json=body or {}
        )
        assert started.status_code == 202, started.text
        job = _poll(client, user, started.json()["job_id"])
    return started.json(), job, fake_chat


def test_run_writes_verified_and_unverified_cells_and_reports_progress(client, audit_events):
    owner = make_user("owner")
    d1 = register_document(owner, filename="a.pdf")
    d2 = register_document(owner, filename="b.pdf")
    tid = _create(client, owner, document_ids=[d1, d2])["id"]

    started, job, fake_chat = _run_to_completion(client, owner, tid)
    assert started["documents"] == 2 and started["fields"] == 2
    assert started["poll_url"].endswith(started["job_id"])
    assert job["status"] == "completed", job
    assert job["result"]["documents"] == 2 and job["result"]["cells"] == 4
    assert job["result"]["extracted"] == 2 and job["result"]["unverified"] == 2
    assert fake_chat.await_count == 2  # one model call per document

    detail = client.get(f"/api/v1/review-tables/{tid}", headers=headers(owner)).json()
    assert detail["last_run_at"] is not None and detail["cell_count"] == 4
    cells = {(c["document_id"], c["field_key"]): c for c in detail["cells"]}
    gl = cells[(d1, "governing_law")]
    assert gl["status"] == "extracted" and gl["verified"] is True
    assert CONTRACT[gl["span_start"] : gl["span_end"]] == QUOTE
    assert gl["value"] == "Delaware"
    eff = cells[(d2, "effective_date")]
    assert eff["status"] == "unverified" and eff["verified"] is False and eff["span_start"] is None

    actions = [e["details"].get("action") for e in audit_events]
    assert "review_table_run_started" in actions and "review_table_run_completed" in actions
    completed = next(
        e for e in audit_events if e["details"].get("action") == "review_table_run_completed"
    )
    assert completed["details"]["cells"] == 4
    assert "Delaware" not in json.dumps(completed["details"])  # ids and counts only


def test_rerun_scoped_to_one_cell_only_touches_that_cell(client):
    owner = make_user("owner")
    d1 = register_document(owner)
    d2 = register_document(owner)
    tid = _create(client, owner, document_ids=[d1, d2])["id"]
    _run_to_completion(client, owner, tid)

    changed = {
        "fields": {
            "governing_law": {"value": "New York", "quote": "nowhere", "not_found": False},
            "effective_date": {"value": "", "not_found": True},
        }
    }
    started, job, fake_chat = _run_to_completion(
        client,
        owner,
        tid,
        body={"document_ids": [d1], "field_keys": ["effective_date"]},
        answer=changed,
    )
    assert started["documents"] == 1 and started["fields"] == 1
    assert fake_chat.await_count == 1
    assert job["result"]["cells"] == 1 and job["result"]["not_found"] == 1

    cells = {
        (c["document_id"], c["field_key"]): c
        for c in client.get(f"/api/v1/review-tables/{tid}", headers=headers(owner)).json()["cells"]
    }
    assert cells[(d1, "effective_date")]["status"] == "not_found"
    assert cells[(d1, "governing_law")]["value"] == "Delaware"  # untouched column
    assert cells[(d2, "effective_date")]["status"] == "unverified"  # untouched row


def test_run_rejects_ids_outside_the_table_and_requires_a_model(client):
    owner = make_user("owner")
    doc = register_document(owner)
    tid = _create(client, owner, document_ids=[doc])["id"]
    with patch.object(ReviewTableService, "_llm_client", return_value=MagicMock()):
        resp = client.post(
            f"/api/v1/review-tables/{tid}/run",
            headers=headers(owner),
            json={"document_ids": ["nope"]},
        )
        assert resp.status_code == 400
        resp = client.post(
            f"/api/v1/review-tables/{tid}/run",
            headers=headers(owner),
            json={"field_keys": ["nope"]},
        )
        assert resp.status_code == 400
    with patch.object(ReviewTableService, "_llm_client", side_effect=ValueError("No key")):
        resp = client.post(f"/api/v1/review-tables/{tid}/run", headers=headers(owner), json={})
        assert resp.status_code == 400 and "No key" in resp.text

    empty = _create(client, owner, document_ids=[])["id"]
    with patch.object(ReviewTableService, "_llm_client", return_value=MagicMock()):
        resp = client.post(f"/api/v1/review-tables/{empty}/run", headers=headers(owner), json={})
    assert resp.status_code == 400


def test_member_can_run_a_shared_table_but_a_stranger_cannot(client):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    doc = register_document(owner, matter_id=matter_id)
    tid = _create(client, owner, matter_id=matter_id, document_ids=[doc])["id"]

    with patch.object(ReviewTableService, "_llm_client", return_value=MagicMock()):
        assert (
            client.post(
                f"/api/v1/review-tables/{tid}/run", headers=headers(stranger), json={}
            ).status_code
            == 404
        )
    _started, job, _chat = _run_to_completion(client, member, tid)
    assert job["status"] == "completed" and job["result"]["extracted"] == 1


def test_document_the_runner_cannot_read_becomes_error_cells(client):
    owner, member = make_user("owner"), make_user("member")
    matter_id = make_shared_matter(owner, member)
    filed = register_document(owner, matter_id=matter_id)
    tid = _create(client, owner, matter_id=matter_id, document_ids=[filed])["id"]
    # The document leaves the matter after the table was made.
    from app.services.documents import document_service

    document_service.documents[filed] = document_service.documents[filed].model_copy(
        update={"matter_id": None}
    )

    _started, job, fake_chat = _run_to_completion(client, member, tid)
    assert job["status"] == "completed"
    assert job["result"]["errors"] == 2 and fake_chat.await_count == 0
    cells = client.get(f"/api/v1/review-tables/{tid}", headers=headers(member)).json()["cells"]
    assert {c["status"] for c in cells} == {"error"}
    assert all("not accessible" in c["error"] for c in cells)


def test_model_failure_is_recorded_on_the_cells_not_the_job(client):
    owner = make_user("owner")
    doc = register_document(owner)
    tid = _create(client, owner, document_ids=[doc])["id"]
    failing = AsyncMock(side_effect=RuntimeError("provider down"))
    with (
        patch.object(ReviewTableService, "_llm_client", return_value=MagicMock()),
        patch.object(review_table_service, "document_text", AsyncMock(return_value=CONTRACT)),
        patch("app.services.review_tables.extraction.openai_chat", failing),
    ):
        started = client.post(f"/api/v1/review-tables/{tid}/run", headers=headers(owner), json={})
        assert started.status_code == 202
        job = _poll(client, owner, started.json()["job_id"])
    assert job["status"] == "completed" and job["result"]["errors"] == 2
    cells = client.get(f"/api/v1/review-tables/{tid}", headers=headers(owner)).json()["cells"]
    assert all(c["status"] == "error" and "provider down" in c["error"] for c in cells)


# ------------------------------------------------------------------------ export


def test_xlsx_export_links_results_to_citations(client, audit_events):
    owner = make_user("owner")
    doc = register_document(owner, filename="nda.pdf")
    tid = _create(client, owner, document_ids=[doc], name="Q3 NDA sweep")["id"]
    _run_to_completion(client, owner, tid)

    resp = client.get(f"/api/v1/review-tables/{tid}/export", headers=headers(owner))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="Q3-NDA-sweep.xlsx"' in resp.headers["content-disposition"]

    wb = load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames == ["Results", "Citations"]
    results, citations = wb["Results"], wb["Citations"]
    assert [c.value for c in results[1]] == ["Document", "Governing law", "Effective date"]
    assert results["A2"].value == "nda.pdf" and results["B2"].value == "Delaware"
    assert results["B2"].hyperlink.location == "'Citations'!A2"
    assert results["C2"].hyperlink.location == "'Citations'!A3"
    rows = list(citations.iter_rows(min_row=2, values_only=True))
    assert rows[0][:4] == ("nda.pdf", "Governing law", "Delaware", QUOTE)
    assert rows[0][6:8] == ("yes", "verified")
    assert rows[1][6:8] == ("no", "unverified")
    assert any(
        e["details"].get("action") == "review_table_export" and e["details"].get("format") == "xlsx"
        for e in audit_events
    )


def test_csv_export_and_access_rules(client):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    doc = register_document(owner, filename="lease.pdf", matter_id=matter_id)
    tid = _create(client, owner, matter_id=matter_id, document_ids=[doc])["id"]
    _run_to_completion(client, owner, tid)

    resp = client.get(f"/api/v1/review-tables/{tid}/export?format=csv", headers=headers(member))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("Document,Field,Value,Quote")
    assert len(lines) == 3
    assert lines[1].startswith("lease.pdf,Governing law,Delaware,")

    assert (
        client.get(f"/api/v1/review-tables/{tid}/export", headers=headers(stranger)).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/review-tables/{tid}/export?format=pdf", headers=headers(owner)
        ).status_code
        == 422
    )
