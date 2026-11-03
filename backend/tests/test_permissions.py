"""Role-permission service: defaults, overrides, guardrails, resolution."""

import json

import pytest
from app.services import permissions as perm


@pytest.fixture(autouse=True)
def _memory_store(monkeypatch):
    """Route instance-settings persistence to an in-memory dict."""
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
    perm.invalidate_cache()
    yield store
    perm.invalidate_cache()


@pytest.fixture(autouse=True)
def _snapshot_file(monkeypatch, tmp_path):
    """Each test gets its own last-known-good snapshot location."""
    path = tmp_path / ".role_permissions.snapshot"
    monkeypatch.setattr(perm, "_snapshot_path", lambda: path)
    return path


async def test_defaults_shape():
    matrix = await perm.get_role_permissions()
    assert set(matrix.keys()) == set(perm.ROLES)
    all_keys = {p["key"] for p in perm.PERMISSIONS}
    assert matrix["admin"] == all_keys
    assert matrix["attorney"] == {k for k in all_keys if not k.startswith("admin.")}
    # Viewer is read-and-research by default
    assert "documents.upload" not in matrix["viewer"]
    assert "chat.use" in matrix["viewer"]


async def test_override_set_and_resolve(_memory_store):
    await perm.set_role_permissions("viewer", ["chat.use"])
    matrix = await perm.get_role_permissions()
    assert matrix["viewer"] == {"chat.use"}
    # Other roles untouched
    assert "contracts.use" in matrix["attorney"]


async def test_admin_keeps_locked_permission(_memory_store):
    # Trying to strip admin.users from admin silently re-adds it.
    await perm.set_role_permissions("admin", ["chat.use"])
    matrix = await perm.get_role_permissions()
    assert "admin.users" in matrix["admin"]
    assert "chat.use" in matrix["admin"]


async def test_unknown_role_and_permission_rejected():
    with pytest.raises(ValueError):
        await perm.set_role_permissions("superuser", ["chat.use"])
    with pytest.raises(ValueError):
        await perm.set_role_permissions("viewer", ["not.a.permission"])


async def test_effective_union_across_roles(_memory_store):
    await perm.set_role_permissions("viewer", ["chat.use"])
    perms = await perm.effective_permissions(["viewer", "paralegal"])
    # Union: paralegal grants contracts even though viewer doesn't
    assert "contracts.use" in perms
    assert "chat.use" in perms
    # Case-insensitive role matching
    assert (
        await perm.effective_permissions(["Admin"]) == (await perm.get_role_permissions())["admin"]
    )


async def test_reset_restores_defaults(_memory_store):
    await perm.set_role_permissions("attorney", ["chat.use"])
    await perm.reset_role_permissions()
    matrix = await perm.get_role_permissions()
    assert matrix["attorney"] == perm.DEFAULT_ROLE_PERMISSIONS["attorney"]


async def test_corrupt_stored_json_without_snapshot_serves_locked_only(_memory_store):
    """A row that does not parse must not restore the shipped defaults: an
    admin may have removed permissions from them. With no snapshot, every
    role keeps only its locked permissions."""
    _memory_store["role_permissions"] = "not json {"
    perm.invalidate_cache()
    matrix = await perm.get_role_permissions()
    assert matrix["attorney"] == set()
    assert matrix["viewer"] == set()
    assert matrix["admin"] == {"admin.users"}  # the repair path stays open
    assert perm._loaded is False
    status = await perm.policy_status()
    assert status["healthy"] is False
    assert status["source"] == perm.SOURCE_LOCKED_ONLY
    assert "JSONDecodeError" in status["error"]


async def test_corrupt_stored_json_serves_last_known_good_snapshot(_memory_store):
    """A customized matrix that later becomes unreadable keeps the restriction
    the admin configured, from the snapshot written when it was last read."""
    await perm.set_role_permissions("attorney", ["chat.use"])
    assert (await perm.get_role_permissions())["attorney"] == {"chat.use"}

    _memory_store["role_permissions"] = "not json {"
    perm.invalidate_cache()
    matrix = await perm.get_role_permissions()
    assert matrix["attorney"] == {"chat.use"}
    # Roles that were never customized still resolve to their defaults.
    assert matrix["viewer"] == perm.DEFAULT_ROLE_PERMISSIONS["viewer"]
    status = await perm.policy_status()
    assert status["healthy"] is False
    assert status["source"] == perm.SOURCE_SNAPSHOT
    assert status["snapshot_present"] is True


async def test_undecryptable_row_is_not_treated_as_unset(_memory_store, monkeypatch):
    """get_secret() hides a decryption failure as None (= no override). The
    loader must use the strict getter so a row encrypted under a rotated key
    cannot read as "no customizations"."""
    import app.services.instance_settings as inst

    await perm.set_role_permissions("attorney", ["chat.use"])
    await perm.get_role_permissions()

    async def get_secret_strict(name):
        raise inst.SecretUnreadableError("cannot decrypt")

    monkeypatch.setattr(inst, "get_secret_strict", get_secret_strict)
    perm.invalidate_cache()
    assert (await perm.get_role_permissions())["attorney"] == {"chat.use"}
    assert (await perm.policy_status())["source"] == perm.SOURCE_SNAPSHOT


async def test_tampered_snapshot_is_rejected(_memory_store, _snapshot_file):
    await perm.set_role_permissions("attorney", ["chat.use"])
    _snapshot_file.write_text("garbage")
    _memory_store["role_permissions"] = "not json {"
    perm.invalidate_cache()
    matrix = await perm.get_role_permissions()
    assert matrix["attorney"] == set()
    assert (await perm.policy_status())["source"] == perm.SOURCE_LOCKED_ONLY


async def test_saving_one_role_keeps_others_when_row_is_corrupt(_memory_store):
    """A save seeds from the snapshot, not from an empty matrix, so repairing
    one role does not silently drop every other role's customization."""
    await perm.set_role_permissions("attorney", ["chat.use"])
    await perm.set_role_permissions("viewer", ["chat.use"])
    _memory_store["role_permissions"] = "not json {"
    perm.invalidate_cache()
    await perm.set_role_permissions("paralegal", ["chat.use"])
    stored = json.loads(_memory_store["role_permissions"])
    assert stored["attorney"] == ["chat.use"]
    assert stored["viewer"] == ["chat.use"]
    assert stored["paralegal"] == ["chat.use"]
    assert (await perm.policy_status())["source"] == perm.SOURCE_STORED


async def test_successful_load_reports_stored(_memory_store):
    status = await perm.policy_status()
    assert status == {
        "healthy": True,
        "source": perm.SOURCE_STORED,
        "error": None,
        "snapshot_present": True,
    }


async def test_unknown_keys_in_storage_dropped(_memory_store):
    _memory_store["role_permissions"] = json.dumps(
        {"viewer": ["chat.use", "ghost.permission"], "badrole": ["chat.use"]}
    )
    perm.invalidate_cache()
    matrix = await perm.get_role_permissions()
    assert matrix["viewer"] == {"chat.use"}


def _failing_store(monkeypatch):
    """Make the instance-settings read raise; returns the call counter."""
    import app.services.instance_settings as inst

    calls = {"n": 0}

    async def get_secret(name):
        calls["n"] += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(inst, "get_secret_strict", get_secret)
    return calls


async def test_unreadable_store_fails_closed_and_is_not_cached(_memory_store, monkeypatch):
    """With no snapshot, an unreachable store grants nothing beyond the locked
    permissions — never the shipped defaults — and the failure is not pinned:
    once the store is readable again the stored matrix takes over, with no
    manual invalidation."""
    import app.services.instance_settings as inst

    calls = _failing_store(monkeypatch)
    monkeypatch.setattr(perm, "_FAILED_LOAD_RETRY_SECONDS", 0.0)

    matrix = await perm.get_role_permissions()
    assert matrix["attorney"] == set()
    assert matrix["admin"] == {"admin.users"}
    assert calls["n"] == 1
    assert perm._loaded is False
    assert (await perm.policy_status())["source"] == perm.SOURCE_LOCKED_ONLY

    _memory_store["role_permissions"] = json.dumps({"attorney": ["chat.use"]})

    async def get_secret(name):
        return _memory_store.get(name)

    monkeypatch.setattr(inst, "get_secret_strict", get_secret)
    assert (await perm.get_role_permissions())["attorney"] == {"chat.use"}
    assert perm._loaded is True
    assert await perm.effective_permissions(["attorney"]) == {"chat.use"}
    assert (await perm.policy_status())["source"] == perm.SOURCE_STORED


async def test_unreachable_store_serves_snapshot_of_uncustomized_matrix(_memory_store, monkeypatch):
    """An instance that never customized roles has a snapshot of "no
    overrides" after its first read, so an outage serves the defaults — from
    the verified snapshot, not as a blind fallback."""
    await perm.get_role_permissions()  # writes the snapshot
    _failing_store(monkeypatch)
    perm.invalidate_cache()
    matrix = await perm.get_role_permissions()
    assert matrix["attorney"] == perm.DEFAULT_ROLE_PERMISSIONS["attorney"]
    assert (await perm.policy_status())["source"] == perm.SOURCE_SNAPSHOT


async def test_unreadable_store_is_retried_after_a_short_backoff(_memory_store, monkeypatch):
    calls = _failing_store(monkeypatch)
    monkeypatch.setattr(perm, "_FAILED_LOAD_RETRY_SECONDS", 60.0)

    for _ in range(3):
        matrix = await perm.get_role_permissions()
        assert matrix["viewer"] == set()
    assert calls["n"] == 1  # within the back-off: no re-query
    assert perm._loaded is False

    perm.invalidate_cache()  # a save/reset clears the back-off too
    await perm.get_role_permissions()
    assert calls["n"] == 2
