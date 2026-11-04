"""Startup refuses to run production without the database fence (app/rls.py).

PostgreSQL exempts superusers and BYPASSRLS roles from row-level security,
so an app connected as one has no second fence around user content. In
production that stops the start; RLS_ENFORCEMENT_REQUIRED=false turns it
back into a logged error, development only logs, and SQLite (no fence to
enforce) is silent.
"""

import pytest
from app import rls

ENFORCED = {"enforced": True, "role": "casecite_app", "superuser": False, "bypass_rls": False}
SUPERUSER = {"enforced": False, "role": "casecite", "superuser": True, "bypass_rls": False}
BYPASS = {"enforced": False, "role": "ops", "superuser": False, "bypass_rls": True}
SQLITE = {"enforced": None, "role": None, "superuser": None, "bypass_rls": None}


def test_an_enforced_role_and_sqlite_pass_silently():
    assert rls.require_enforced(ENFORCED, production=True, required=True) is None
    assert rls.require_enforced(SQLITE, production=True, required=True) is None


@pytest.mark.parametrize("status", [SUPERUSER, BYPASS])
def test_production_refuses_to_start_without_the_fence(status):
    with pytest.raises(rls.RLSNotEnforcedError) as exc:
        rls.require_enforced(status, production=True, required=True)
    message = str(exc.value)
    assert status["role"] in message
    assert "APP_DB_USER" in message  # the fix is named, not just the problem
    assert "RLS_ENFORCEMENT_REQUIRED=false" in message


def test_a_failed_probe_counts_as_not_enforced_in_production():
    with pytest.raises(rls.RLSNotEnforcedError) as exc:
        rls.require_enforced(None, production=True, required=True, error="OSError: down")
    assert "could not be verified" in str(exc.value)
    assert "OSError: down" in str(exc.value)


def test_the_override_turns_the_refusal_into_a_logged_error():
    problem = rls.require_enforced(SUPERUSER, production=True, required=False)
    assert problem is not None
    assert "RLS_ENFORCEMENT_REQUIRED=false" in problem
    assert "superuser" in problem


def test_development_only_logs():
    problem = rls.require_enforced(SUPERUSER, production=False, required=True)
    assert problem is not None and problem.endswith("(development: continuing)")


def test_the_setting_defaults_to_required():
    from app.config import Settings

    assert Settings.model_fields["rls_enforcement_required"].default is True
