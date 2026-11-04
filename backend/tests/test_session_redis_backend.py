"""Sessions are shared through Redis when it is configured.

Two session managers (two worker processes) sharing one Redis see the same
sign-ins, rotations, terminations and idle clock; the list expires with its
longest session; and a Redis outage degrades to "no session" rather than to
a stale one.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.middleware.security import SessionManager
from app.redis_utils import RedisError


class FakeRedis:
    """The handful of commands the session store uses, with TTLs."""

    def __init__(self):
        self.store: dict[str, tuple[str, int]] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise RedisError("connection refused")

    def get(self, key):
        self._check()
        value = self.store.get(key)
        return value[0] if value else None

    def setex(self, key, ttl, value):
        self._check()
        self.store[key] = (value, int(ttl))

    def delete(self, key):
        self._check()
        self.store.pop(key, None)

    def ttl(self, key):
        return self.store[key][1] if key in self.store else -2


def _manager(tmp_path) -> SessionManager:
    sm = SessionManager.__new__(SessionManager)
    sm._sessions = defaultdict(list)
    sm.max_sessions_per_user = 5
    sm.session_timeout = timedelta(hours=8)
    sm.idle_timeout = timedelta(minutes=120)
    sm._last_cleanup = datetime.now(UTC)
    sm._cleanup_interval = timedelta(minutes=15)
    sm._session_file = tmp_path / "sessions.jsonl"
    return sm


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch("app.middleware.security.get_redis", return_value=fake):
        yield fake


def test_two_workers_share_sign_ins_and_sign_outs(tmp_path, redis):
    one, two = _manager(tmp_path / "a"), _manager(tmp_path / "b")
    assert one.create_session("u1", "10.0.0.1", "UA", "jti-1", session_id="sid-1")
    # The other worker validates a session it never created.
    assert two.validate_session("u1", "jti-1", "10.0.0.1") is True
    assert [s["jti"] for s in two.get_active_sessions_raw("u1")] == ["jti-1"]
    # Nothing was written to either process's file or dict.
    assert one._sessions == {} and not (tmp_path / "a" / "sessions.jsonl").exists()

    two.terminate_all_sessions("u1")
    assert one.validate_session("u1", "jti-1", "10.0.0.1") is False
    assert redis.store == {}


def test_rotation_on_one_worker_is_seen_by_another(tmp_path, redis):
    one, two = _manager(tmp_path / "a"), _manager(tmp_path / "b")
    one.create_session("u1", "10.0.0.1", "UA", "jti-1", session_id="sid-1")
    assert two.rotate_session("u1", "sid-1", "jti-2") == "jti-1"
    assert one.validate_session("u1", "jti-2") is True
    assert one.validate_session("u1", "jti-1") is False


def test_the_list_expires_with_its_longest_session(tmp_path, redis):
    sm = _manager(tmp_path)
    sm.create_session("u1", "10.0.0.1", "UA", "jti-1")
    ttl = redis.ttl("sessions:u1")
    assert 8 * 3600 - 5 <= ttl <= 8 * 3600
    stored = json.loads(redis.get("sessions:u1"))
    assert stored[0]["jti"] == "jti-1" and stored[0]["client"] == "web"


def test_idle_clock_is_shared(tmp_path, redis):
    one, two = _manager(tmp_path / "a"), _manager(tmp_path / "b")
    one.create_session("u1", "10.0.0.1", "UA", "jti-1")
    # Backdate the stored activity past the idle timeout...
    entries = json.loads(redis.get("sessions:u1"))
    entries[0]["last_activity"] = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    redis.setex("sessions:u1", 1000, json.dumps(entries))
    assert two.validate_session("u1", "jti-1") is False
    # ...and a fresh request on one worker would have kept it alive for the other.
    entries[0]["last_activity"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    redis.setex("sessions:u1", 1000, json.dumps(entries))
    assert one.validate_session("u1", "jti-1") is True
    assert two.validate_session("u1", "jti-1") is True


def test_session_cap_applies_across_workers(tmp_path, redis):
    one, two = _manager(tmp_path / "a"), _manager(tmp_path / "b")
    for i in range(5):
        (one if i % 2 else two).create_session("u1", "10.0.0.1", "UA", f"jti-{i}")
    one.create_session("u1", "10.0.0.1", "UA", "jti-5")
    jtis = {s["jti"] for s in two.get_active_sessions_raw("u1")}
    assert len(jtis) == 5 and "jti-0" not in jtis and "jti-5" in jtis


def test_a_redis_outage_means_no_session_not_a_stale_one(tmp_path, redis):
    sm = _manager(tmp_path)
    sm.create_session("u1", "10.0.0.1", "UA", "jti-1")
    redis.fail = True
    assert sm.validate_session("u1", "jti-1") is False
    assert sm.get_active_sessions("u1") == []


def test_without_redis_the_file_backed_store_is_unchanged(tmp_path):
    with patch("app.middleware.security.get_redis", return_value=None):
        sm = _manager(tmp_path)
        sm.create_session("u1", "10.0.0.1", "UA", "jti-1")
        assert [s["jti"] for s in sm._sessions["u1"]] == ["jti-1"]
        assert "jti-1" in (tmp_path / "sessions.jsonl").read_text()
        sm.terminate_session("u1", "jti-1")
        assert sm._sessions["u1"] == []
        assert "jti-1" not in (tmp_path / "sessions.jsonl").read_text()
