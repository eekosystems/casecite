"""Process-local caches follow shared state once several processes run.

The role permission matrix and the set of matters on legal hold reload when
the shared version counter moves; a connector's cached credentials follow
the credentials file; and the audit hash chain stays one chain when two
writers append to the same log directory.
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from app.services import permissions as perm
from app.services import shared_versions
from app.services.audit import AuditEventType, AuditLog, AuditService, check_chain

from tests.helpers import make_shared_matter, make_user

pytestmark = pytest.mark.usefixtures("no_rate_limit")


# ---- shared version counters ------------------------------------------------------


def test_version_counters_round_trip(client):
    assert shared_versions.get_version("rls-test-counter") == 0
    assert shared_versions.bump("rls-test-counter") == 1
    assert shared_versions.bump("rls-test-counter") == 2
    assert shared_versions.get_version("rls-test-counter") == 2
    assert asyncio.run(shared_versions.get_version_async("rls-test-counter")) == 2
    assert asyncio.run(shared_versions.bump_async("rls-test-counter")) == 3


# ---- permissions ----------------------------------------------------------------


@pytest.fixture
def _memory_store(monkeypatch, tmp_path):
    store: dict[str, str] = {}

    async def get_secret(name):
        return store.get(name)

    async def set_secret(name, value):
        store[name] = value

    async def delete_secret(name):
        store.pop(name, None)

    import app.services.instance_settings as inst

    monkeypatch.setattr(inst, "get_secret", get_secret)
    monkeypatch.setattr(inst, "get_secret_strict", get_secret)
    monkeypatch.setattr(inst, "set_secret", set_secret)
    monkeypatch.setattr(inst, "delete_secret", delete_secret)
    monkeypatch.setattr(perm, "_snapshot_path", lambda: tmp_path / "snap")
    monkeypatch.setattr(perm, "_VERSION_CHECK_SECONDS", 0.0)
    perm.invalidate_cache()
    yield store
    perm.invalidate_cache()


async def test_a_matrix_saved_by_another_process_is_picked_up(client, _memory_store):
    assert "documents.upload" in (await perm.get_role_permissions())["attorney"]
    # "Another process": the store changes and the counter is bumped, while
    # this process's cache still says loaded.
    _memory_store["role_permissions"] = json.dumps({"attorney": ["chat.use"]})
    await shared_versions.bump_async(shared_versions.PERMISSIONS)
    assert (await perm.get_role_permissions())["attorney"] == {"chat.use"}


async def test_an_unchanged_counter_does_not_reload(client, _memory_store, monkeypatch):
    await perm.get_role_permissions()
    import app.services.instance_settings as inst

    calls = {"n": 0}

    async def counting(name):
        calls["n"] += 1
        return _memory_store.get(name)

    monkeypatch.setattr(inst, "get_secret_strict", counting)
    await perm.get_role_permissions()
    await perm.get_role_permissions()
    assert calls["n"] == 0


# ---- legal holds ----------------------------------------------------------------


async def test_a_hold_placed_by_another_process_is_enforced(client, auth_headers):
    from app.services.documents import document_service

    from tests.helpers import headers

    owner = make_user("hold-owner")
    matter_id = make_shared_matter(owner)
    doc = type("Doc", (), {"metadata": {}, "matter_id": matter_id})()
    document_service.held_matter_ids = set()
    document_service._holds_checked_at = 0.0
    document_service._holds_version = shared_versions.get_version(shared_versions.LEGAL_HOLDS)
    assert document_service.is_held(doc) is False

    # The hold is placed through the API (another process, as far as this
    # cache is concerned: wipe the local set the endpoint refreshed).
    resp = client.post(
        f"/api/v1/matters/{matter_id}/hold", json={"on": True}, headers=headers(owner)
    )
    assert resp.status_code == 200
    document_service.held_matter_ids = set()
    document_service._holds_checked_at = 0.0
    assert document_service.is_held(doc) is True
    document_service.held_matter_ids = set()


# ---- connector credentials ------------------------------------------------------


def test_connector_credentials_follow_the_file(tmp_path):
    from app.services.connectors.base import BaseConnector
    from app.services.connectors.box import BoxConnector

    box = BoxConnector()
    BaseConnector.set_current_user("u-creds")
    with patch.object(box, "_get_credentials_path", return_value=str(tmp_path / "box.json")):
        box.credentials = {"access_token": "first"}
        box._save_credentials()
        assert box.credentials["access_token"] == "first"

        # Another process refreshed the token: the file changed.
        time.sleep(0.01)
        box._write_credentials_file(str(tmp_path / "box.json"), {"access_token": "second"})
        os.utime(str(tmp_path / "box.json"), (time.time() + 5, time.time() + 5))
        assert box.credentials["access_token"] == "second"

        # An in-place edit survives until it is saved.
        box.credentials["access_token"] = "third"
        assert box.credentials["access_token"] == "third"
        box._save_credentials()
        assert box._read_credentials_file(str(tmp_path / "box.json"))["access_token"] == "third"


# ---- audit chain across writers -------------------------------------------------


def _isolated_audit(log_dir) -> AuditService:
    service = AuditService.__new__(AuditService)
    service.log_dir = log_dir
    service.log_dir.mkdir(parents=True, exist_ok=True)
    service._current_log_file = None
    service._current_date = None
    return service


async def test_two_writers_keep_one_chain(tmp_path):
    """Two services on one directory, as two worker processes would be,
    interleaving appends: every entry chains to the one before it on disk."""
    original = AuditLog._last_hash
    AuditLog._last_hash = None
    try:
        one, two = _isolated_audit(tmp_path / "logs"), _isolated_audit(tmp_path / "logs")
        with patch.object(AuditService, "_persist_to_db", return_value=None):
            for i in range(6):
                writer = one if i % 2 == 0 else two
                # Simulate a separate process: its class-level memory of the
                # last hash is whatever it last wrote, not what the other wrote.
                AuditLog._last_hash = getattr(writer, "_own_last", None)
                await writer.log(AuditLog(event_type=AuditEventType.DATA_ACCESS, user_id=f"u{i}"))
                writer._own_last = AuditLog._last_hash
        entries = one._tail_entries(10)
        assert len(entries) == 6
        result = check_chain(entries)
        assert result.valid, result.reason
    finally:
        AuditLog._last_hash = original


def test_last_hash_is_read_from_the_file_tail(tmp_path):
    service = _isolated_audit(tmp_path / "logs")
    assert service._last_hash_on_disk() is None
    log_file = service._get_log_file()
    log_file.write_text(
        json.dumps({"integrity": {"entry_hash": "aaa"}})
        + "\n"
        + json.dumps({"integrity": {"entry_hash": "bbb"}})
        + "\n",
        encoding="utf-8",
    )
    assert service._last_hash_on_disk() == "bbb"
    assert datetime.now(UTC) and defaultdict  # imports used by other helpers
