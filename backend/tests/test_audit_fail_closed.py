"""Privileged actions fail without their audit record (app/services/audit.py).

AUDIT_FAIL_CLOSED decides what an ordinary event does when its entry cannot
be written. The events that record privileged and destructive actions do not
consult it: their request fails, and the permissions that gate those actions
probe the log for writability before the endpoint runs at all.
"""

from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.services.audit import (
    PRIVILEGED_EVENT_TYPES,
    AuditEventType,
    AuditWriteError,
    is_privileged,
)
from app.services.permissions import PRIVILEGED_PERMISSIONS


def _failing_open(target):
    """``open`` that refuses to append to ``target`` (a full volume)."""
    real_open = open

    def fake_open(file, mode="r", *args, **kwargs):
        if str(file) == str(target) and "a" in mode:
            raise OSError(28, "No space left on device")
        return real_open(file, mode, *args, **kwargs)

    return patch("builtins.open", fake_open)


def test_which_events_are_privileged():
    assert is_privileged(AuditEventType.USER_DELETE, None)
    assert is_privileged(AuditEventType.SETTINGS_CHANGE, {})
    assert is_privileged(AuditEventType.DOCUMENT_DELETE, {"action": "anything"})
    assert is_privileged(AuditEventType.DATA_EXPORT, None)
    # The shared modification type is privileged only for the destructive actions.
    assert is_privileged(AuditEventType.DATA_MODIFICATION, {"action": "ethical_wall_added"})
    assert is_privileged(AuditEventType.DATA_MODIFICATION, {"action": "legal_hold_set"})
    assert not is_privileged(AuditEventType.DATA_MODIFICATION, {"action": "matter_facts_updated"})
    assert not is_privileged(AuditEventType.DATA_MODIFICATION, None)
    # Routine events stay governed by the flag.
    assert not is_privileged(AuditEventType.LOGIN_SUCCESS, None)
    assert not is_privileged(AuditEventType.CHAT_QUERY, None)
    assert not is_privileged(AuditEventType.DOCUMENT_VIEW, None)
    assert "user.delete" in PRIVILEGED_EVENT_TYPES
    assert {"admin.users", "admin.settings", "documents.delete"} <= PRIVILEGED_PERMISSIONS


@pytest.mark.asyncio
async def test_a_privileged_event_fails_the_request_whatever_the_flag_says(
    audit_service_isolated,
):
    assert settings.audit_fail_closed is False
    with (
        _failing_open(audit_service_isolated._get_log_file()),
        pytest.raises(AuditWriteError) as exc,
    ):
        await audit_service_isolated.log_event(AuditEventType.USER_DELETE, user_id="admin")
    assert exc.value.status_code == 503
    assert "refused" in exc.value.detail


@pytest.mark.asyncio
async def test_an_ordinary_event_still_fails_open_by_default(audit_service_isolated):
    assert settings.audit_fail_closed is False
    with _failing_open(audit_service_isolated._get_log_file()):
        await audit_service_isolated.log_event(AuditEventType.LOGIN_SUCCESS, user_id="u1")


@pytest.mark.asyncio
async def test_the_writability_probe(audit_service_isolated):
    await audit_service_isolated.assert_writable()  # a writable log passes silently
    with (
        _failing_open(audit_service_isolated._get_log_file()),
        pytest.raises(AuditWriteError) as exc,
    ):
        await audit_service_isolated.assert_writable()
    assert exc.value.status_code == 503
    assert "before it started" in exc.value.detail


def test_a_privileged_endpoint_is_refused_before_it_runs(client, auth_headers):
    """The gate probes the log; the endpoint body never runs."""
    down = AsyncMock(side_effect=AuditWriteError("The audit log is not writable (test)."))
    with patch("app.services.audit.audit_service.assert_writable", new=down):
        refused = client.get("/api/v1/admin/users", headers=auth_headers)
        allowed = client.get("/api/v1/auth/me", headers=auth_headers)
    assert refused.status_code == 503, refused.text
    assert "audit log" in refused.json()["detail"]
    assert allowed.status_code == 200  # not a privileged action: unaffected
    assert down.await_count == 1


def test_a_privileged_endpoint_runs_when_the_log_is_writable(client, auth_headers):
    assert client.get("/api/v1/admin/users", headers=auth_headers).status_code == 200
