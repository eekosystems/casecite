"""
Tests for SessionManager — concurrent sessions, expiration, IP binding.

Covers:
- Session creation with JTI
- Max 5 concurrent sessions enforced
- Oldest session evicted when limit exceeded
- Session validation (valid, expired, wrong IP)
- Session termination (single and all)
- IP binding enforcement
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.middleware.security import SessionManager


@pytest.fixture(autouse=True)
def _process_local_sessions():
    """These tests cover the process-local store (dict mirrored to a file).
    CI runs the suite with a Redis service configured, which would route
    sessions through it; tests/test_session_redis_backend.py covers that."""
    with patch("app.middleware.security.get_redis", return_value=None):
        yield


def _make_session_manager(tmp_path) -> SessionManager:
    """Create an isolated SessionManager with a temp session file."""
    sm = SessionManager.__new__(SessionManager)
    from collections import defaultdict

    sm._sessions = defaultdict(list)
    sm.max_sessions_per_user = 5
    sm.session_timeout = timedelta(hours=8)
    sm.idle_timeout = timedelta(minutes=120)
    sm._last_cleanup = datetime.now(UTC)
    sm._cleanup_interval = timedelta(minutes=15)
    sm._session_file = tmp_path / "sessions.jsonl"
    return sm


class TestSessionCreation:
    """Tests for creating sessions."""

    def test_create_session_returns_true(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        result = sm.create_session("user-1", "192.168.1.1", "Mozilla/5.0", "jti-001")
        assert result is True

    def test_create_session_stores_session(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "192.168.1.1", "Mozilla/5.0", "jti-001")
        sessions = sm.get_active_sessions("user-1")
        assert len(sessions) == 1

    def test_create_multiple_sessions(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        for i in range(3):
            sm.create_session("user-1", f"192.168.1.{i}", "Mozilla/5.0", f"jti-{i}")
        sessions = sm.get_active_sessions("user-1")
        assert len(sessions) == 3


class TestSessionLimits:
    """Tests for concurrent session enforcement."""

    def test_max_5_sessions_enforced(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        for i in range(7):
            sm.create_session("user-1", f"10.0.0.{i}", "Agent", f"jti-{i}")
        sessions = sm.get_active_sessions("user-1")
        assert len(sessions) == 5

    def test_oldest_session_evicted(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        for i in range(5):
            sm.create_session("user-1", f"10.0.0.{i}", "Agent", f"jti-{i}")

        # Add a 6th — should evict jti-0
        sm.create_session("user-1", "10.0.0.99", "Agent", "jti-new")
        raw = sm.get_active_sessions_raw("user-1")
        jtis = {s["jti"] for s in raw}
        assert "jti-0" not in jtis
        assert "jti-new" in jtis

    def test_sessions_isolated_per_user(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-a", "10.0.0.1", "Agent", "jti-a")
        sm.create_session("user-b", "10.0.0.2", "Agent", "jti-b")
        assert len(sm.get_active_sessions("user-a")) == 1
        assert len(sm.get_active_sessions("user-b")) == 1


class TestSessionValidation:
    """Tests for session validation."""

    def test_validate_valid_session(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        assert sm.validate_session("user-1", "jti-1") is True

    def test_validate_nonexistent_jti(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        assert sm.validate_session("user-1", "jti-unknown") is False

    def test_validate_nonexistent_user(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        assert sm.validate_session("no-such-user", "jti-1") is False

    def test_validate_expired_session(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        # Manually expire the session
        for s in sm._sessions["user-1"]:
            if s["jti"] == "jti-1":
                s["expires_at"] = datetime.now(UTC) - timedelta(hours=1)
        assert sm.validate_session("user-1", "jti-1") is False

    def test_validate_expired_jwt(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        expired_time = datetime.now(UTC) - timedelta(hours=1)
        assert sm.validate_session("user-1", "jti-1", token_exp=expired_time) is False

    def test_validate_updates_last_activity(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        before = sm._sessions["user-1"][0]["last_activity"]
        # Small delay to ensure timestamp difference
        sm.validate_session("user-1", "jti-1")
        after = sm._sessions["user-1"][0]["last_activity"]
        assert after >= before

    def test_validate_ip_binding_same_ip(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        with patch("app.middleware.security.settings") as mock_settings:
            mock_settings.enforce_session_ip_binding = True
            assert sm.validate_session("user-1", "jti-1", ip_address="10.0.0.1") is True

    def test_validate_ip_binding_different_ip_rejected(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        with patch("app.middleware.security.settings") as mock_settings:
            mock_settings.enforce_session_ip_binding = True
            assert sm.validate_session("user-1", "jti-1", ip_address="99.99.99.99") is False


class TestSessionTermination:
    """Tests for terminating sessions."""

    def test_terminate_single_session(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        sm.create_session("user-1", "10.0.0.2", "Agent", "jti-2")
        sm.terminate_session("user-1", "jti-1")
        raw = sm.get_active_sessions_raw("user-1")
        jtis = {s["jti"] for s in raw}
        assert "jti-1" not in jtis
        assert "jti-2" in jtis

    def test_terminate_all_sessions(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        for i in range(3):
            sm.create_session("user-1", f"10.0.0.{i}", "Agent", f"jti-{i}")
        sm.terminate_all_sessions("user-1")
        assert len(sm.get_active_sessions("user-1")) == 0

    def test_terminate_all_leaves_other_users(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-a", "10.0.0.1", "Agent", "jti-a")
        sm.create_session("user-b", "10.0.0.2", "Agent", "jti-b")
        sm.terminate_all_sessions("user-a")
        assert len(sm.get_active_sessions("user-a")) == 0
        assert len(sm.get_active_sessions("user-b")) == 1


class TestSessionTerminationCompaction:
    """Terminating a session must compact the on-disk file immediately."""

    def test_terminate_session_compacts_file(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        sm.create_session("user-1", "10.0.0.2", "Agent", "jti-2")
        sm.terminate_session("user-1", "jti-1")
        content = sm._session_file.read_text()
        assert "jti-1" not in content
        assert "jti-2" in content

    def test_terminate_all_sessions_compacts_file(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-a", "10.0.0.1", "Agent", "jti-a")
        sm.create_session("user-b", "10.0.0.2", "Agent", "jti-b")
        sm.terminate_all_sessions("user-a")
        content = sm._session_file.read_text()
        assert "jti-a" not in content
        assert "jti-b" in content


class TestSessionPersistence:
    """Tests for file-based session persistence."""

    def test_session_persisted_to_file(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        assert sm._session_file.exists()
        content = sm._session_file.read_text()
        assert "jti-1" in content

    def test_sessions_loaded_from_file(self, tmp_path):
        sm1 = _make_session_manager(tmp_path)
        sm1.create_session("user-1", "10.0.0.1", "Agent", "jti-1")

        # Create a new SessionManager that reads the same file
        sm2 = _make_session_manager(tmp_path)
        sm2._load_sessions_from_file()
        assert len(sm2.get_active_sessions("user-1")) == 1

    def test_expired_sessions_not_loaded(self, tmp_path):
        sm1 = _make_session_manager(tmp_path)
        sm1.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        # Manually expire in file by overwriting
        for s in sm1._sessions["user-1"]:
            s["expires_at"] = datetime.now(UTC) - timedelta(hours=1)
        sm1._rewrite_session_file()

        sm2 = _make_session_manager(tmp_path)
        sm2._load_sessions_from_file()
        assert len(sm2.get_active_sessions("user-1")) == 0


class TestIdleTimeout:
    """A session with no authenticated request for idle_timeout is ended."""

    def test_idle_session_is_invalid(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        sm._sessions["user-1"][0]["last_activity"] = datetime.now(UTC) - timedelta(minutes=121)
        assert sm.validate_session("user-1", "jti-1") is False
        assert sm.get_active_sessions("user-1") == []

    def test_activity_keeps_session_alive(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        sm._sessions["user-1"][0]["last_activity"] = datetime.now(UTC) - timedelta(minutes=119)
        assert sm.validate_session("user-1", "jti-1") is True
        # The request above reset the idle clock.
        assert sm.validate_session("user-1", "jti-1") is True

    def test_idle_check_can_be_disabled(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.idle_timeout = None
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        sm._sessions["user-1"][0]["last_activity"] = datetime.now(UTC) - timedelta(hours=7)
        assert sm.validate_session("user-1", "jti-1") is True

    def test_restart_does_not_count_as_idleness(self, tmp_path):
        sm1 = _make_session_manager(tmp_path)
        sm1.create_session("user-1", "10.0.0.1", "Agent", "jti-1")
        sm1._sessions["user-1"][0]["last_activity"] = datetime.now(UTC) - timedelta(hours=3)
        sm1._rewrite_session_file()

        sm2 = _make_session_manager(tmp_path)
        sm2._load_sessions_from_file()
        assert sm2.validate_session("user-1", "jti-1") is True


class TestSessionRotation:
    """A token refresh moves the SAME session onto the new access token."""

    def test_rotate_replaces_jti_without_adding_a_session(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1", session_id="sid-1")
        assert sm.rotate_session("user-1", "sid-1", "jti-2") == "jti-1"
        assert len(sm.get_active_sessions_raw("user-1")) == 1
        assert sm.validate_session("user-1", "jti-2") is True
        assert sm.validate_session("user-1", "jti-1") is False

    def test_rotate_never_extends_the_absolute_lifetime(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1", session_id="sid-1")
        before = dict(sm._sessions["user-1"][0])
        sm.rotate_session("user-1", "sid-1", "jti-2")
        after = sm._sessions["user-1"][0]
        assert after["expires_at"] == before["expires_at"]
        assert after["created_at"] == before["created_at"]

    def test_rotate_unknown_session_returns_none(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1", session_id="sid-1")
        assert sm.rotate_session("user-1", "sid-other", "jti-2") is None
        assert sm.rotate_session("user-1", None, "jti-2") is None
        assert sm.rotate_session("nobody", "sid-1", "jti-2") is None

    def test_rotate_expired_session_returns_none(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1", session_id="sid-1")
        sm._sessions["user-1"][0]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
        assert sm.rotate_session("user-1", "sid-1", "jti-2") is None
        assert sm._sessions["user-1"] == []

    def test_rotate_idle_session_returns_none(self, tmp_path):
        sm = _make_session_manager(tmp_path)
        sm.create_session("user-1", "10.0.0.1", "Agent", "jti-1", session_id="sid-1")
        sm._sessions["user-1"][0]["last_activity"] = datetime.now(UTC) - timedelta(hours=3)
        assert sm.rotate_session("user-1", "sid-1", "jti-2") is None

    def test_rotation_is_persisted(self, tmp_path):
        sm1 = _make_session_manager(tmp_path)
        sm1.create_session("user-1", "10.0.0.1", "Agent", "jti-1", session_id="sid-1")
        sm1.rotate_session("user-1", "sid-1", "jti-2")

        sm2 = _make_session_manager(tmp_path)
        sm2._load_sessions_from_file()
        assert sm2.validate_session("user-1", "jti-2") is True
        assert sm2.validate_session("user-1", "jti-1") is False
        assert sm2.rotate_session("user-1", "sid-1", "jti-3") == "jti-2"

    def test_evicted_session_does_not_return_after_restart(self, tmp_path):
        sm1 = _make_session_manager(tmp_path)
        for i in range(6):
            sm1.create_session("user-1", "10.0.0.1", "Agent", f"jti-{i}", session_id=f"sid-{i}")

        sm2 = _make_session_manager(tmp_path)
        sm2._load_sessions_from_file()
        assert sm2.rotate_session("user-1", "sid-0", "jti-x") is None
        assert len(sm2.get_active_sessions_raw("user-1")) == 5
