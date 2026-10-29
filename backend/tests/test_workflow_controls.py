"""Workflows beyond the linear run: the validation endpoint, conditions,
approval gates, versions, duplicate and archive, per-step retry and skip,
recent runs across workflows, output caps and email notifications.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import settings
from app.models.schemas import RAGSettings
from app.services.user_settings import save_user_settings
from app.services.workflows import conditions, notifications
from app.services.workflows import service as service_module
from app.services.workflows.outputs import cap_output
from app.services.workflows.service import WorkflowService
from app.services.workflows.steps import StepError, StepOutput

from tests.helpers import headers, insert_db_user, make_shared_matter, make_user, register_document

FIELDS = [{"name": "Governing law", "type": "text"}]
EXTRACT = {"name": "extract", "kind": "extract_fields", "params": {"fields": FIELDS}}
REVIEW = {"name": "review", "kind": "analyze_contract", "params": {}}
CITES = {"name": "cites", "kind": "citation_check", "params": {"from_steps": ["review"]}}


@contextmanager
def _override(**values):
    """Change frozen settings for one block (the model refuses plain assignment)."""
    old = {k: getattr(settings, k) for k in values}
    for k, v in values.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            object.__setattr__(settings, k, v)


@pytest.fixture(autouse=True)
def _no_rate_limit():
    with patch(
        "app.middleware.security.RateLimiter.is_allowed",
        return_value=(True, {"limit": 100, "remaining": 99}),
    ):
        yield


@pytest.fixture(autouse=True)
def _fake_client():
    with patch.object(WorkflowService, "_llm_client", return_value=MagicMock()):
        yield


def _create(client, user, *, steps, matter_id=None, name="Intake", expect=201):
    body = {"name": name, "steps": steps}
    if matter_id:
        body["matter_id"] = matter_id
    resp = client.post("/api/v1/workflows", headers=headers(user), json=body)
    assert resp.status_code == expect, resp.text
    return resp.json()


def _poll(client, user, job_id: str, attempts: int = 200) -> dict:
    for _ in range(attempts):
        job = client.get(f"/api/v1/jobs/{job_id}", headers=headers(user))
        assert job.status_code == 200, job.text
        if job.json()["status"] in ("completed", "failed", "cancelled"):
            return job.json()
        time.sleep(0.05)
    raise AssertionError("job did not settle")


def _fake_adapter(name: str, fail: bool = False, calls: list | None = None, data=None):
    async def adapter(step, ctx):
        if calls is not None:
            calls.append({"step": step["name"], "outputs": dict(ctx.outputs)})
        if fail:
            raise StepError(f"{name} broke")
        return StepOutput(
            summary=f"{name} done",
            data=data if data is not None else {"from": name, "issues": 3},
            document_ids=list(ctx.input_document_ids),
        )

    return adapter


FAKES = {
    "extract_fields": _fake_adapter("extract"),
    "analyze_contract": _fake_adapter("review"),
    "citation_check": _fake_adapter("cites"),
}


def _run(client, user, wid: str, document_ids):
    resp = client.post(
        f"/api/v1/workflows/{wid}/run", headers=headers(user), json={"document_ids": document_ids}
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def _run_detail(client, user, wid, rid):
    resp = client.get(f"/api/v1/workflows/{wid}/runs/{rid}", headers=headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ validation


def test_validate_reports_every_problem_with_its_step_and_field(client):
    owner = make_user("owner")
    steps = [
        {"name": "extract", "kind": "extract_fields", "params": {"fields": []}},
        {"name": "extract", "kind": "analyze_contract", "params": {}},
        {
            "name": "memo",
            "kind": "draft_document",
            "params": {"instructions": "Use {{later.summary}} and {{extract}}"},
        },
        {
            "name": "compare",
            "kind": "compare_contracts",
            "params": {"baseline": "document"},
        },
        {"name": "cites", "kind": "citation_check", "params": {"from_steps": ["nope"]}},
        {
            "name": "later",
            "kind": "analyze_contract",
            "params": {},
            "condition": {"step": "later", "op": "exists"},
        },
        {"name": "bad kind", "kind": "unknown", "params": {}},
    ]
    resp = client.post("/api/v1/workflows/validate", headers=headers(owner), json={"steps": steps})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    issues = {(i["index"], i["field"]): i["message"] for i in body["issues"]}
    assert (0, "params.fields") in issues or any(k[0] == 0 for k in issues)
    assert "already named" in issues[(1, "name")]
    assert "not an earlier step" in issues[(2, "params.instructions")]
    assert "baseline" in issues[(3, "params.baseline_document_id")].lower()
    assert "not an earlier step" in issues[(4, "params.from_steps")]
    assert "refer to itself" in issues[(5, "condition.step")]
    assert any(k[0] == 6 for k in issues)

    ok = client.post(
        "/api/v1/workflows/validate",
        headers=headers(owner),
        json={
            "steps": [
                EXTRACT,
                {**REVIEW, "condition": {"step": "extract", "op": "exists"}},
                {
                    "name": "memo",
                    "kind": "draft_document",
                    "params": {"instructions": "Summarise {{review.summary}}"},
                },
            ]
        },
    )
    assert ok.json() == {"valid": True, "issues": []}
    empty = client.post("/api/v1/workflows/validate", headers=headers(owner), json={"steps": []})
    assert empty.json()["issues"][0]["index"] == -1


def test_create_refuses_a_condition_value_that_does_not_fit_its_operator(client):
    owner = make_user("owner")
    bad = [EXTRACT, {**REVIEW, "condition": {"step": "extract", "op": "gt", "value": "many"}}]
    _create(client, owner, steps=bad, expect=422)
    unary = [EXTRACT, {**REVIEW, "condition": {"step": "extract", "op": "exists", "value": "x"}}]
    wf = _create(client, owner, steps=unary)
    assert wf["steps"][1]["condition"] == {
        "step": "extract",
        "path": "",
        "op": "exists",
        "value": None,
    }


# ------------------------------------------------------------------ conditions


@pytest.mark.parametrize(
    "op,path,value,expected",
    [
        ("exists", "data.issues", None, True),
        ("exists", "data.missing", None, False),
        ("exists", "data.empty", None, False),
        ("not_exists", "data.missing", None, True),
        ("not_exists", "data.issues", None, False),
        ("equals", "data.kind", "NDA", True),
        ("equals", "data.issues", "3", True),
        ("not_equals", "data.kind", "lease", True),
        ("contains", "data.tags", "risky", True),
        ("contains", "data.kind", "nd", True),
        ("contains", "data.tags", "safe", False),
        ("gt", "data.issues", 2, True),
        ("gt", "data.cap", "200000", True),
        ("lt", "data.issues", 2, False),
        ("gte", "data.issues", 3, True),
        ("lte", "data.issues", "3", True),
        ("count_gt", "data.tags", 1, True),
        ("count_eq", "data.tags", 2, True),
        ("count_eq", "data.kind", 3, True),
        ("gt", "data.kind", 1, False),
        ("equals", "data.missing", "x", False),
    ],
)
def test_every_condition_operator(op, path, value, expected):
    outputs = {
        "Review": {
            "summary": "done",
            "data": {
                "issues": 3,
                "kind": "NDA",
                "tags": ["risky", "long"],
                "empty": [],
                "cap": "USD 250,000",
            },
        }
    }
    verdict = conditions.evaluate(
        {"step": "review", "path": path, "op": op, "value": value}, outputs
    )
    assert verdict["result"] is expected, verdict
    assert verdict["step"] == "review" and verdict["op"] == op
    unknown = conditions.evaluate({"step": "nope", "op": "exists"}, outputs)
    assert unknown == {
        "result": False,
        "step": "nope",
        "path": "",
        "op": "exists",
        "value": None,
        "found": False,
        "actual": None,
    }


def test_a_false_condition_skips_the_step_and_the_run_goes_on(client):
    owner = make_user("owner")
    d1 = register_document(owner)
    steps = [
        EXTRACT,
        {**REVIEW, "condition": {"step": "extract", "path": "data.issues", "op": "gt", "value": 5}},
        {**CITES, "condition": {"step": "extract", "path": "data.issues", "op": "gt", "value": 1}},
    ]
    wf = _create(client, owner, steps=steps)
    calls: list = []
    fakes = {
        "extract_fields": _fake_adapter("extract", calls=calls),
        "analyze_contract": _fake_adapter("review", calls=calls),
        "citation_check": _fake_adapter("cites", calls=calls),
    }
    with patch.dict(service_module.ADAPTERS, fakes):
        started = _run(client, owner, wf["id"], [d1])
        job = _poll(client, owner, started["job_id"])
    assert job["status"] == "completed" and job["result"]["completed_steps"] == 3
    assert [c["step"] for c in calls] == ["extract", "cites"]
    run = _run_detail(client, owner, wf["id"], started["run_id"])
    assert [s["status"] for s in run["step_runs"]] == [
        "completed",
        "skipped_condition",
        "completed",
    ]
    recorded = run["step_runs"][1]["output"]["data"]["condition"]
    assert recorded["result"] is False and recorded["actual"] == "3" and recorded["found"]
    assert run["completed_steps"] == 3 and run["status"] == "completed"
    # The citation step saw the skipped step's placeholder output, not a failure.
    assert "review" in calls[1]["outputs"]


# --------------------------------------------------------------- approval gate


def _approval(approvers="owner", name="gate"):
    return {
        "name": name,
        "kind": "approval",
        "params": {"title": "Partner sign-off", "approvers": approvers},
    }


def test_an_approval_step_parks_the_run_until_an_approver_decides(client, audit_events):
    owner, member, stranger = make_user("owner"), make_user("member"), make_user("stranger")
    matter_id = make_shared_matter(owner, member)
    d1 = register_document(owner, matter_id=matter_id)
    wf = _create(
        client, owner, steps=[EXTRACT, _approval("any_member"), REVIEW], matter_id=matter_id
    )
    with patch.dict(service_module.ADAPTERS, FAKES):
        started = _run(client, owner, wf["id"], [d1])
        job = _poll(client, owner, started["job_id"])
    assert job["status"] == "completed"
    assert job["result"]["status"] == "awaiting_approval"
    assert job["result"]["awaiting_step"] == "gate" and job["result"]["completed_steps"] == 1

    url = f"/api/v1/workflows/{wf['id']}/runs/{started['run_id']}"
    run = client.get(url, headers=headers(member)).json()
    assert run["status"] == "awaiting_approval" and run["awaiting_step"] == "gate"
    assert sorted(run["approver_ids"]) == sorted([owner.id, member.id])
    assert run["can_approve"] is True and run["job_id"] is None
    assert [s["status"] for s in run["step_runs"]] == ["completed", "awaiting", "pending"]
    assert run["step_runs"][1]["output"]["data"]["title"] == "Partner sign-off"
    listed = client.get(f"/api/v1/workflows/{wf['id']}/runs", headers=headers(owner)).json()
    assert listed["runs"][0]["awaiting_step"] == "gate"

    # A run waiting for approval cannot be resumed, retried or re-decided by a stranger.
    assert client.post(f"{url}/resume", headers=headers(owner)).status_code == 400
    assert client.post(f"{url}/steps/1/retry", headers=headers(owner)).status_code == 400
    assert client.post(f"{url}/approve", headers=headers(stranger)).status_code == 404
    assert any(e["details"].get("action") == "workflow_run_awaiting_approval" for e in audit_events)

    with patch.dict(service_module.ADAPTERS, FAKES):
        approved = client.post(
            f"{url}/approve", headers=headers(member), json={"note": "Looks fine"}
        )
        assert approved.status_code == 202, approved.text
        assert _poll(client, member, approved.json()["job_id"])["status"] == "completed"
    run = client.get(url, headers=headers(owner)).json()
    assert run["status"] == "completed"
    assert [s["status"] for s in run["step_runs"]] == ["completed"] * 3
    decision = run["step_runs"][1]["output"]["data"]
    assert decision["decision"] == "approved" and decision["decided_by"] == member.id
    assert decision["note"] == "Looks fine"
    assert "Approved by" in run["step_runs"][1]["output"]["summary"]
    assert any(e["details"].get("action") == "workflow_run_approved" for e in audit_events)
    # Deciding again is refused: nothing is waiting.
    assert client.post(f"{url}/approve", headers=headers(owner)).status_code == 400


def test_a_rejection_ends_the_run_and_only_named_approvers_may_decide(client, audit_events):
    owner, member, other = make_user("owner"), make_user("member"), make_user("other")
    matter_id = make_shared_matter(owner, member, other)
    d1 = register_document(owner, matter_id=matter_id)
    # Only ``member`` may decide; ``other`` is a matter member but not an approver.
    wf = _create(
        client, owner, steps=[EXTRACT, _approval([member.id]), REVIEW], matter_id=matter_id
    )
    with patch.dict(service_module.ADAPTERS, FAKES):
        started = _run(client, owner, wf["id"], [d1])
        _poll(client, owner, started["job_id"])
    url = f"/api/v1/workflows/{wf['id']}/runs/{started['run_id']}"
    assert client.post(f"{url}/reject", headers=headers(other)).status_code == 403
    assert client.post(f"{url}/reject", headers=headers(owner)).status_code == 403
    run = client.get(url, headers=headers(other)).json()
    assert run["can_approve"] is False and run["approver_ids"] == [member.id]

    rejected = client.post(f"{url}/reject", headers=headers(member), json={"note": "Not yet"})
    assert rejected.status_code == 200, rejected.text
    run = rejected.json()
    assert run["status"] == "rejected" and "Rejected at step 2 'gate'" in run["error"]
    assert "Not yet" in run["error"]
    assert [s["status"] for s in run["step_runs"]] == ["completed", "rejected", "skipped"]
    assert run["step_runs"][1]["output"]["data"]["decision"] == "rejected"
    assert any(e["details"].get("action") == "workflow_run_rejected" for e in audit_events)
    # A rejected run is not resumable (it ended by decision, not by failure).
    assert client.post(f"{url}/resume", headers=headers(owner)).status_code == 400


def test_named_approvers_must_belong_to_the_matter(client):
    owner, member, outsider = make_user("owner"), make_user("member"), make_user("outsider")
    matter_id = make_shared_matter(owner, member)
    _create(client, owner, steps=[_approval([outsider.id])], matter_id=matter_id, expect=422)
    _create(client, owner, steps=[_approval([member.id])], matter_id=matter_id)
    # Without a matter only the owner can be named.
    _create(client, owner, steps=[_approval([member.id])], expect=422)
    _create(client, owner, steps=[_approval([owner.id])])


# ----------------------------------------------------------------- versions


def test_every_step_change_records_a_version_and_runs_record_theirs(client, audit_events):
    owner = make_user("owner")
    d1 = register_document(owner)
    wf = _create(client, owner, steps=[EXTRACT])
    assert wf["version"] == 1
    url = f"/api/v1/workflows/{wf['id']}"
    # Name only: no new version.
    renamed = client.patch(url, headers=headers(owner), json={"name": "Renamed"}).json()
    assert renamed["version"] == 1
    # Same steps resent: no new version either.
    same = client.patch(url, headers=headers(owner), json={"steps": [EXTRACT]}).json()
    assert same["version"] == 1
    changed = client.patch(
        url,
        headers=headers(owner),
        json={"steps": [EXTRACT, REVIEW], "change_note": "Add the review"},
    ).json()
    assert changed["version"] == 2
    with patch.dict(service_module.ADAPTERS, FAKES):
        started = _run(client, owner, wf["id"], [d1])
        _poll(client, owner, started["job_id"])
    third = client.patch(url, headers=headers(owner), json={"steps": [EXTRACT, REVIEW, CITES]})
    assert third.json()["version"] == 3

    versions = client.get(f"{url}/versions", headers=headers(owner)).json()
    assert versions["current"] == 3
    assert [v["version"] for v in versions["versions"]] == [3, 2, 1]
    assert versions["versions"][1]["change_note"] == "Add the review"
    assert versions["versions"][1]["step_names"] == ["extract", "review"]
    assert versions["versions"][2]["change_note"] == "Created"
    one = client.get(f"{url}/versions/1", headers=headers(owner)).json()
    assert [s["name"] for s in one["steps"]] == ["extract"] and one["name"] == "Intake"
    assert client.get(f"{url}/versions/9", headers=headers(owner)).status_code == 404

    run = _run_detail(client, owner, wf["id"], started["run_id"])
    assert run["workflow_version"] == 2 and [s["name"] for s in run["step_definitions"]] == [
        "extract",
        "review",
    ]

    restored = client.post(
        f"{url}/versions/1/restore", headers=headers(owner), json={"change_note": "Back to one"}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 4 and restored.json()["step_count"] == 1
    versions = client.get(f"{url}/versions", headers=headers(owner)).json()
    assert versions["versions"][0]["change_note"] == "Back to one"
    assert any(e["details"].get("action") == "workflow_version_restored" for e in audit_events)


def test_duplicate_and_archive(client, audit_events):
    owner, member = make_user("owner"), make_user("member")
    matter_id = make_shared_matter(owner, member)
    d1 = register_document(owner, matter_id=matter_id)
    wf = _create(client, owner, steps=[EXTRACT, REVIEW], matter_id=matter_id)
    url = f"/api/v1/workflows/{wf['id']}"

    # A member duplicates into a workflow of their own.
    copy = client.post(f"{url}/duplicate", headers=headers(member), json={})
    assert copy.status_code == 201, copy.text
    assert copy.json()["name"] == "Intake (copy)" and copy.json()["role"] == "owner"
    assert copy.json()["owner_id"] == member.id and copy.json()["matter_id"] is None
    assert copy.json()["version"] == 1 and copy.json()["step_count"] == 2
    named = client.post(
        f"{url}/duplicate", headers=headers(owner), json={"name": "Second", "matter_id": matter_id}
    ).json()
    assert named["name"] == "Second" and named["matter_id"] == matter_id
    assert any(e["details"].get("action") == "workflow_duplicated" for e in audit_events)

    # Only the owner archives; archived workflows leave the list and cannot run.
    assert client.post(f"{url}/archive", headers=headers(member)).status_code == 404
    archived = client.post(f"{url}/archive", headers=headers(owner))
    assert archived.status_code == 200 and archived.json()["archived_at"]
    listed = client.get("/api/v1/workflows", headers=headers(owner)).json()["workflows"]
    assert wf["id"] not in [w["id"] for w in listed]
    with_archived = client.get(
        "/api/v1/workflows?include_archived=true", headers=headers(owner)
    ).json()["workflows"]
    assert wf["id"] in [w["id"] for w in with_archived]
    run = client.post(f"{url}/run", headers=headers(owner), json={"document_ids": [d1]})
    assert run.status_code == 400 and "archived" in run.json()["detail"].lower()
    # Still readable, and back after unarchiving.
    assert client.get(url, headers=headers(member)).status_code == 200
    restored = client.post(f"{url}/unarchive", headers=headers(owner))
    assert restored.status_code == 200 and restored.json()["archived_at"] is None
    with patch.dict(service_module.ADAPTERS, FAKES):
        started = _run(client, owner, wf["id"], [d1])
        assert _poll(client, owner, started["job_id"])["status"] == "completed"


# -------------------------------------------------------------- run controls


def test_retry_reruns_one_step_and_skip_passes_over_a_failed_one(client, audit_events):
    owner = make_user("owner")
    d1 = register_document(owner)
    wf = _create(client, owner, steps=[EXTRACT, REVIEW, CITES])
    calls: list = []
    with patch.dict(
        service_module.ADAPTERS,
        {
            "extract_fields": _fake_adapter("extract", calls=calls),
            "analyze_contract": _fake_adapter("review", fail=True, calls=calls),
            "citation_check": _fake_adapter("cites", calls=calls),
        },
    ):
        started = _run(client, owner, wf["id"], [d1])
        assert _poll(client, owner, started["job_id"])["status"] == "failed"
    url = f"/api/v1/workflows/{wf['id']}/runs/{started['run_id']}"

    # Skip the failed step: the run continues from the next one.
    calls.clear()
    with patch.dict(
        service_module.ADAPTERS,
        {
            "extract_fields": _fake_adapter("extract", calls=calls),
            "citation_check": _fake_adapter("cites", calls=calls),
        },
    ):
        # Only a failed (or pending) step can be skipped.
        assert client.post(f"{url}/steps/0/skip", headers=headers(owner)).status_code == 400
        skipped = client.post(f"{url}/steps/1/skip", headers=headers(owner))
        assert skipped.status_code == 202, skipped.text
        assert _poll(client, owner, skipped.json()["job_id"])["status"] == "completed"
    run = _run_detail(client, owner, wf["id"], started["run_id"])
    assert [s["status"] for s in run["step_runs"]] == ["completed", "skipped", "completed"]
    assert run["step_runs"][1]["output"]["data"]["skipped_by"] == owner.id
    assert [c["step"] for c in calls] == ["cites"]
    assert any(e["details"].get("action") == "workflow_step_skipped" for e in audit_events)

    # Retry the first step: it and everything after re-run; the run ends completed.
    calls.clear()
    with patch.dict(
        service_module.ADAPTERS,
        {
            "extract_fields": _fake_adapter("extract", calls=calls),
            "analyze_contract": _fake_adapter("review", calls=calls),
            "citation_check": _fake_adapter("cites", calls=calls),
        },
    ):
        retried = client.post(f"{url}/steps/0/retry", headers=headers(owner))
        assert retried.status_code == 202, retried.text
        assert _poll(client, owner, retried.json()["job_id"])["status"] == "completed"
    run = _run_detail(client, owner, wf["id"], started["run_id"])
    assert [s["status"] for s in run["step_runs"]] == ["completed"] * 3
    assert [c["step"] for c in calls] == ["extract", "review", "cites"]
    assert run["resumed_count"] == 2
    assert client.post(f"{url}/steps/7/retry", headers=headers(owner)).status_code == 400
    assert any(e["details"].get("action") == "workflow_step_retried" for e in audit_events)


def test_only_the_owner_or_runner_controls_a_run(client):
    owner, member, other = make_user("owner"), make_user("member"), make_user("other")
    matter_id = make_shared_matter(owner, member, other)
    d1 = register_document(owner, matter_id=matter_id)
    wf = _create(client, owner, steps=[EXTRACT, REVIEW], matter_id=matter_id)
    with patch.dict(
        service_module.ADAPTERS,
        {
            "extract_fields": _fake_adapter("extract"),
            "analyze_contract": _fake_adapter("r", fail=True),
        },
    ):
        started = _run(client, member, wf["id"], [d1])
        _poll(client, member, started["job_id"])
    url = f"/api/v1/workflows/{wf['id']}/runs/{started['run_id']}"
    assert client.post(f"{url}/steps/1/retry", headers=headers(other)).status_code == 403
    assert client.post(f"{url}/steps/1/skip", headers=headers(other)).status_code == 403
    with patch.dict(
        service_module.ADAPTERS,
        {"extract_fields": _fake_adapter("extract"), "analyze_contract": _fake_adapter("review")},
    ):
        assert client.post(f"{url}/steps/1/retry", headers=headers(member)).status_code == 202


def test_recent_runs_span_workflows_and_carry_names_and_documents(client):
    owner, member = make_user("owner"), make_user("member")
    matter_id = make_shared_matter(owner, member)
    d1 = register_document(owner, filename="nda.pdf", matter_id=matter_id)
    shared = _create(client, owner, steps=[EXTRACT], matter_id=matter_id, name="Shared")
    private = _create(client, owner, steps=[EXTRACT], name="Private")
    with patch.dict(service_module.ADAPTERS, FAKES):
        for wid in (shared["id"], private["id"]):
            _poll(client, owner, _run(client, owner, wid, [d1])["job_id"])
    mine = client.get("/api/v1/workflows/runs", headers=headers(owner)).json()["runs"]
    assert sorted(r["workflow_name"] for r in mine) == ["Private", "Shared"]
    assert all(r["workflow_version"] == 1 for r in mine)
    theirs = client.get("/api/v1/workflows/runs", headers=headers(member)).json()["runs"]
    assert [r["workflow_name"] for r in theirs] == ["Shared"]
    scoped = client.get(
        f"/api/v1/workflows/runs?matter_id={matter_id}", headers=headers(owner)
    ).json()["runs"]
    assert [r["workflow_name"] for r in scoped] == ["Shared"]
    detail = _run_detail(client, owner, shared["id"], scoped[0]["id"])
    assert detail["document_refs"] == [{"id": d1, "filename": "nda.pdf", "accessible": True}]
    assert detail["workflow_name"] == "Shared"


# ------------------------------------------------------------------ outputs


def test_step_outputs_are_capped_with_the_cut_fields_listed(client):
    owner = make_user("owner")
    d1 = register_document(owner)
    wf = _create(client, owner, steps=[EXTRACT])
    big = {"text": "x" * 20_000, "rows": [{"n": i} for i in range(500)], "kind": "nda"}
    with (
        _override(workflow_max_output_bytes=8_192),
        patch.dict(service_module.ADAPTERS, {"extract_fields": _fake_adapter("e", data=big)}),
    ):
        started = _run(client, owner, wf["id"], [d1])
        assert _poll(client, owner, started["job_id"])["status"] == "completed"
    output = _run_detail(client, owner, wf["id"], started["run_id"])["step_runs"][0]["output"]
    assert output["truncated"] is True and output["summary"] == "e done"
    assert "text" in output["truncated_fields"] or "rows" in output["truncated_fields"]
    assert output["data"]["kind"] == "nda"
    import json

    assert len(json.dumps(output)) <= 8_192 + 512


def test_cap_output_keeps_small_outputs_untouched():
    small = {"summary": "s", "data": {"a": 1}, "document_ids": [], "produced": {}}
    assert cap_output(small, 4096) is small
    assert "truncated" not in cap_output(small, 0)


# ------------------------------------------------------------ notifications


@pytest.fixture
def _mail_on():
    with _override(
        workflow_notifications=True,
        sendgrid_api_key="sg-test",
        frontend_url="https://firm.example",
    ):
        yield


def _wait_for(predicate, attempts=100):
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_notifications_go_to_the_runner_and_approvers_and_respect_opt_out(client, _mail_on):
    owner, member, quiet = make_user("owner"), make_user("member"), make_user("quiet")
    for u in (owner, member, quiet):
        insert_db_user(u)
    save_user_settings(quiet.id, RAGSettings(workflow_email_notifications=False))
    matter_id = make_shared_matter(owner, member, quiet)
    d1 = register_document(owner, matter_id=matter_id)
    wf = _create(
        client, owner, steps=[EXTRACT, _approval("any_member"), REVIEW], matter_id=matter_id
    )
    sent: list[tuple[str, str, str]] = []

    async def deliver(to_email, subject, html_content):
        sent.append((to_email, subject, html_content))

    with (
        patch.object(notifications, "_deliver", side_effect=deliver),
        patch.dict(service_module.ADAPTERS, FAKES),
    ):
        started = _run(client, owner, wf["id"], [d1])
        _poll(client, owner, started["job_id"])
        assert _wait_for(lambda: len(sent) >= 2)
        # Awaiting approval: the runner (owner) and the approvers, minus the one who opted out.
        recipients = sorted(e for e, _s, _h in sent)
        assert recipients == sorted([owner.email, member.email])
        assert all("needs your approval" in s for _e, s, _h in sent)
        html = sent[0][2]
        assert "Partner sign-off" in html and "Intake" in html
        assert f"https://firm.example/workflows/{wf['id']}?run={started['run_id']}" in html
        assert "extract done" not in html  # no step output in the mail

        sent.clear()
        url = f"/api/v1/workflows/{wf['id']}/runs/{started['run_id']}"
        approved = client.post(f"{url}/approve", headers=headers(member), json={"note": "ok"})
        assert approved.status_code == 202
        _poll(client, member, approved.json()["job_id"])
        assert _wait_for(lambda: len(sent) >= 1)
        assert [e for e, _s, _h in sent] == [owner.email]
        assert "Workflow finished" in sent[0][1]


def test_a_failed_run_mails_the_failed_step_and_a_mail_failure_never_fails_the_run(
    client, _mail_on, caplog
):
    owner = make_user("owner")
    insert_db_user(owner)
    d1 = register_document(owner)
    wf = _create(client, owner, steps=[EXTRACT, REVIEW])
    sent: list = []

    async def deliver(to_email, subject, html_content):
        sent.append((to_email, subject, html_content))

    with (
        patch.object(notifications, "_deliver", side_effect=deliver),
        patch.dict(
            service_module.ADAPTERS,
            {
                "extract_fields": _fake_adapter("extract"),
                "analyze_contract": _fake_adapter("review", fail=True),
            },
        ),
    ):
        started = _run(client, owner, wf["id"], [d1])
        assert _poll(client, owner, started["job_id"])["status"] == "failed"
        assert _wait_for(lambda: len(sent) >= 1)
    assert "Workflow failed" in sent[0][1]
    assert "<strong>review</strong>" in sent[0][2] and "review broke" in sent[0][2]

    async def broken(to_email, subject, html_content):
        raise ConnectionError("smtp down")

    with (
        patch.object(notifications, "_deliver", side_effect=broken),
        patch.dict(service_module.ADAPTERS, FAKES),
    ):
        started = _run(client, owner, wf["id"], [d1])
        assert _poll(client, owner, started["job_id"])["status"] == "completed"
    assert _run_detail(client, owner, wf["id"], started["run_id"])["status"] == "completed"
    assert any("Workflow notification not sent" in r.message for r in caplog.records)


def test_notifications_are_off_by_default():
    assert notifications.enabled() is False
    with patch.object(notifications, "_deliver", new=AsyncMock()) as deliver:
        import asyncio

        assert asyncio.run(notifications.notify_run_event("completed", "nope")) == 0
        deliver.assert_not_called()
