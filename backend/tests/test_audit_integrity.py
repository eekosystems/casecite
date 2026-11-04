"""
Audit chain integrity — the HMAC must cover the whole stored entry, and each
stored entry must link to the one stored before it.

v1 signed seven scalar fields, so action details, IP, user agent and error
text could be edited on disk without breaking the chain. v2 signed the full
entry, but the service wrote every entry linked to its own provisional hash,
so a real log never chained. v3 links to the stored predecessor and signs the
version. Legacy entries keep verifying via their recorded hash_version, and a
rotated key stays verifiable through AUDIT_HMAC_KEY_PREVIOUS.
"""

import gzip
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.config import settings
from app.services.audit import (
    AuditEventType,
    AuditLog,
    AuditService,
    AuditWriteError,
    check_chain,
    compute_entry_hash,
    verify_entry_hash,
)


def _entries(n: int = 3, previous_hash: str | None = None) -> list[dict]:
    """A correctly linked run of n stored entries, as the JSONL file would hold them."""
    out = []
    for i in range(n):
        entry = AuditLog(
            event_type=AuditEventType.DOCUMENT_DELETE,
            user_id=f"user-{i}",
            user_email=f"person{i}@example.com",
            resource_type="document",
            resource_id=f"doc-{i}",
            action_details={"filename": f"contract-{i}.pdf", "size": 1024 * i},
            ip_address="10.20.30.40",
            user_agent="pytest/1.0",
            success=True,
        )
        previous_hash = entry.seal(previous_hash)
        # Round-trip through JSON exactly as the JSONL file would.
        out.append(json.loads(entry.to_json()))
    return out


def _unlinked_v2_entries(n: int = 3) -> list[dict]:
    """Entries as builds before v3 stored them: version 2, each one's
    previous_hash set to its own provisional hash instead of its predecessor's."""
    out = []
    for entry in _entries(n):
        entry["integrity"]["hash_version"] = 2
        entry["integrity"]["previous_hash"] = None
        provisional = compute_entry_hash(entry, AuditLog._hmac_key)
        entry["integrity"]["previous_hash"] = provisional
        entry["integrity"]["entry_hash"] = compute_entry_hash(entry, AuditLog._hmac_key)
        out.append(entry)
    return out


def _production(**overrides):
    # Settings is frozen; swap the module-level reference for a copy.
    prod = settings.model_copy(
        update={"debug": False, "audit_halt_on_tampering": True, **overrides}
    )
    return patch("app.services.audit.settings", prod)


def _restart(service) -> AuditService:
    """A fresh service over the same directory, running the real startup path."""
    AuditLog._last_hash = None
    restarted = AuditService.__new__(AuditService)
    restarted.log_dir = service.log_dir
    restarted._current_log_file = None
    restarted._current_date = None
    restarted._load_last_hash()
    restarted._verify_chain_on_startup()
    return restarted


class TestFullEntryCoverage:
    def setup_method(self):
        AuditLog._last_hash = None

    def test_new_entries_are_version_3(self):
        (entry,) = _entries(1)
        assert entry["integrity"]["hash_version"] == 3
        assert verify_entry_hash(entry)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda e: e["action"].__setitem__("filename", "something-else.pdf"),
            lambda e: e["action"].__setitem__("size", 999),
            lambda e: e["context"].__setitem__("ip_address", "192.168.0.***"),
            lambda e: e["context"].__setitem__("user_agent", "curl/8.0"),
            lambda e: e["outcome"].__setitem__("error", "fabricated"),
            lambda e: e["user"].__setitem__("email", "x***@example.com"),
            lambda e: e.__setitem__("correlation_id", "00000000-0000-0000-0000-000000000000"),
            lambda e: e["integrity"].__setitem__("hash_version", 2),
        ],
        ids=[
            "details",
            "details-number",
            "ip",
            "user_agent",
            "error",
            "email",
            "correlation",
            "version-downgrade",
        ],
    )
    def test_editing_any_field_breaks_the_signature(self, mutate):
        (entry,) = _entries(1)
        assert verify_entry_hash(entry)
        mutate(entry)
        assert not verify_entry_hash(entry)

    @pytest.mark.asyncio
    async def test_chain_verification_flags_edited_details(self, audit_service_isolated):
        logs = _entries(3)
        logs[1]["action"]["filename"] = "renamed-after-the-fact.pdf"
        is_valid, invalid_id = await audit_service_isolated.verify_chain_integrity(logs)
        assert is_valid is False
        assert invalid_id == logs[1]["id"]

    @pytest.mark.asyncio
    async def test_untouched_chain_verifies_after_json_roundtrip(self, audit_service_isolated):
        logs = _entries(5)
        is_valid, invalid_id = await audit_service_isolated.verify_chain_integrity(logs)
        assert is_valid is True
        assert invalid_id is None


class TestLinkage:
    def setup_method(self):
        AuditLog._last_hash = None

    def test_removed_entry_is_detected(self):
        logs = _entries(4)
        removed = logs.pop(1)
        result = check_chain(logs)
        assert result.valid is False
        assert result.first_invalid_id == logs[1]["id"]
        assert removed["integrity"]["entry_hash"] in result.reason

    def test_reordered_entries_are_detected(self):
        logs = _entries(4)
        logs[1], logs[2] = logs[2], logs[1]
        assert check_chain(logs).valid is False

    def test_window_may_start_mid_chain(self):
        """The first entry of a verified run has no predecessor in hand."""
        logs = _entries(5)
        result = check_chain(logs[2:])
        assert result.valid is True
        assert result.entries_checked == 3

    def test_empty_run_is_valid(self):
        result = check_chain([])
        assert result.valid is True
        assert result.entries_checked == 0


class TestLegacyEntries:
    def setup_method(self):
        AuditLog._last_hash = None

    def _legacy_entry(self, previous_hash: str | None) -> dict:
        entry = {
            "id": "legacy-1",
            "timestamp": "2026-01-01T00:00:00+00:00Z",
            "event_type": "login_success",
            "user": {"id": "u1", "email": "a***@example.com"},
            "resource": {"type": None, "id": None},
            "action": {"note": "original"},
            "context": {"ip_address": "10.0.0.***", "user_agent": "x", "environment": "production"},
            "outcome": {"success": True, "error": None},
            "integrity": {"previous_hash": previous_hash, "entry_hash": None},
        }
        message = (
            f"{previous_hash or 'GENESIS'}:legacy-1:{entry['timestamp']}:login_success:u1:None:True"
        )
        entry["integrity"]["entry_hash"] = hmac.new(
            AuditLog._hmac_key, message.encode(), hashlib.sha256
        ).hexdigest()
        return entry

    def test_v1_entry_without_version_still_verifies(self):
        entry = self._legacy_entry(None)
        assert "hash_version" not in entry["integrity"]
        assert verify_entry_hash(entry)
        assert compute_entry_hash(entry, AuditLog._hmac_key) == entry["integrity"]["entry_hash"]

    def test_mixed_chain_v1_then_v3(self):
        legacy = self._legacy_entry(None)
        (new,) = _entries(1, previous_hash=legacy["integrity"]["entry_hash"])
        result = check_chain([legacy, new])
        assert result.valid is True
        assert result.legacy_entries == 1

    def test_unlinked_v2_entries_pass_on_their_signatures(self):
        """What every pre-v3 deployment has on disk must not read as tampering."""
        legacy = _unlinked_v2_entries(4)
        assert legacy[1]["integrity"]["previous_hash"] != legacy[0]["integrity"]["entry_hash"]
        result = check_chain(legacy)
        assert result.valid is True
        assert result.legacy_entries == 4

    def test_edited_legacy_entry_is_still_caught(self):
        legacy = _unlinked_v2_entries(3)
        legacy[1]["action"]["filename"] = "edited.pdf"
        result = check_chain(legacy)
        assert result.valid is False
        assert result.first_invalid_id == legacy[1]["id"]

    def test_first_linked_entry_must_point_at_the_last_legacy_entry(self):
        legacy = _unlinked_v2_entries(2)
        linked = _entries(2, previous_hash=legacy[-1]["integrity"]["entry_hash"])
        assert check_chain(legacy + linked).valid is True

        # Dropping the last legacy entry leaves the first linked entry dangling.
        result = check_chain(legacy[:1] + linked)
        assert result.valid is False
        assert result.first_invalid_id == linked[0]["id"]

    def test_legacy_entry_after_a_linked_entry_is_rejected(self):
        """Otherwise old-format entries could be spliced in to hide a removal."""
        linked = _entries(2)
        legacy = _unlinked_v2_entries(1)
        result = check_chain(linked + legacy)
        assert result.valid is False
        assert result.first_invalid_id == legacy[0]["id"]
        assert "legacy-format" in result.reason


class TestKeyRotation:
    def setup_method(self):
        AuditLog._last_hash = None

    def test_previous_key_keeps_old_entries_verifiable(self):
        old_key, new_key = b"old-audit-key-0123456789abcdef", b"new-audit-key-0123456789abcdef"
        with patch.object(AuditLog, "_hmac_key", old_key):
            (entry,) = _entries(1)
        with patch.object(AuditLog, "_hmac_key", new_key):
            with patch.object(AuditLog, "_hmac_key_previous", None):
                assert not verify_entry_hash(entry)
            with patch.object(AuditLog, "_hmac_key_previous", old_key):
                assert verify_entry_hash(entry)

    def test_previous_key_does_not_accept_tampering(self):
        old_key, new_key = b"old-audit-key-0123456789abcdef", b"new-audit-key-0123456789abcdef"
        with patch.object(AuditLog, "_hmac_key", old_key):
            (entry,) = _entries(1)
        entry["action"]["filename"] = "edited.pdf"
        with (
            patch.object(AuditLog, "_hmac_key", new_key),
            patch.object(AuditLog, "_hmac_key_previous", old_key),
        ):
            assert not verify_entry_hash(entry)


class TestStartupVerification:
    def setup_method(self):
        AuditLog._last_hash = None

    def _write(self, service, entries: list[dict], day: str = "2026-08-26") -> None:
        path = service.log_dir / f"audit_{day}.jsonl"
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_clean_chain_passes(self, audit_service_isolated):
        self._write(audit_service_isolated, _entries(4))
        with _production():
            audit_service_isolated._verify_chain_on_startup()  # no raise

    def test_edited_details_halt_startup_in_production(self, audit_service_isolated):
        entries = _entries(4)
        entries[2]["action"]["filename"] = "edited-on-disk.pdf"
        self._write(audit_service_isolated, entries)
        with _production(), pytest.raises(RuntimeError, match="HMAC MISMATCH"):
            audit_service_isolated._verify_chain_on_startup()

    def test_rewritten_chain_without_key_halts_startup(self, audit_service_isolated):
        """An attacker who rewrites entries and re-links previous_hash pointers
        consistently used to pass the linkage-only check."""
        entries = _entries(3)
        forged_key = b"attacker-does-not-know-the-key"
        entries[1]["action"]["filename"] = "forged.pdf"
        entries[1]["integrity"]["entry_hash"] = compute_entry_hash(entries[1], forged_key)
        entries[2]["integrity"]["previous_hash"] = entries[1]["integrity"]["entry_hash"]
        entries[2]["integrity"]["entry_hash"] = compute_entry_hash(entries[2], forged_key)
        self._write(audit_service_isolated, entries)
        with _production(), pytest.raises(RuntimeError, match="HMAC MISMATCH"):
            audit_service_isolated._verify_chain_on_startup()

    def test_broken_linkage_still_halts(self, audit_service_isolated):
        entries = _entries(3)
        entries[2]["integrity"]["previous_hash"] = "not-the-previous-hash"
        self._write(audit_service_isolated, entries)
        with _production(), pytest.raises(RuntimeError, match="CHAIN INTEGRITY BROKEN"):
            audit_service_isolated._verify_chain_on_startup()

    def test_removed_entry_halts_startup(self, audit_service_isolated):
        entries = _entries(4)
        del entries[2]
        self._write(audit_service_isolated, entries)
        with _production(), pytest.raises(RuntimeError, match="CHAIN INTEGRITY BROKEN"):
            audit_service_isolated._verify_chain_on_startup()

    def test_debug_mode_logs_but_does_not_halt(self, audit_service_isolated):
        entries = _entries(2)
        entries[0]["action"]["filename"] = "edited.pdf"
        self._write(audit_service_isolated, entries)
        dev = settings.model_copy(update={"debug": True, "audit_halt_on_tampering": True})
        with patch("app.services.audit.settings", dev):
            audit_service_isolated._verify_chain_on_startup()  # no raise

    def test_chain_is_followed_across_daily_files(self, audit_service_isolated):
        """Three days, one chain: the files used to be concatenated newest-first,
        which reported a break at every file boundary."""
        entries = _entries(9)
        self._write(audit_service_isolated, entries[0:3], "2026-08-24")
        self._write(audit_service_isolated, entries[3:6], "2026-08-25")
        self._write(audit_service_isolated, entries[6:9], "2026-08-26")
        with _production():
            audit_service_isolated._verify_chain_on_startup()  # no raise

    def test_break_at_a_file_boundary_is_caught(self, audit_service_isolated):
        entries = _entries(6)
        self._write(audit_service_isolated, entries[0:2], "2026-08-24")  # entry 2 removed
        self._write(audit_service_isolated, entries[3:6], "2026-08-25")
        with _production(), pytest.raises(RuntimeError, match="CHAIN INTEGRITY BROKEN"):
            audit_service_isolated._verify_chain_on_startup()

    def test_only_the_most_recent_entries_are_verified(self, audit_service_isolated):
        """The window is the newest entries, so tampering just inside it is
        caught and tampering beyond it is not looked at."""
        entries = _entries(8)
        self._write(audit_service_isolated, entries[0:4], "2026-08-25")
        self._write(audit_service_isolated, entries[4:8], "2026-08-26")

        tail = audit_service_isolated._tail_entries(3)
        assert [e["id"] for e in tail] == [e["id"] for e in entries[5:8]]

        entries[1]["action"]["filename"] = "edited-old.pdf"
        self._write(audit_service_isolated, entries[0:4], "2026-08-25")
        with _production(), patch("app.services.audit._STARTUP_VERIFY_ENTRIES", 5):
            audit_service_isolated._verify_chain_on_startup()  # entry 1 is outside the window
        with _production(), pytest.raises(RuntimeError, match="HMAC MISMATCH"):
            audit_service_isolated._verify_chain_on_startup()

    def test_archived_files_are_verified(self, audit_service_isolated):
        entries = _entries(4)
        entries[0]["action"]["filename"] = "edited-in-archive.pdf"
        archive = audit_service_isolated.log_dir / "audit_2026-01-01.jsonl.gz"
        with gzip.open(archive, "wt", encoding="utf-8") as f:
            for e in entries[0:2]:
                f.write(json.dumps(e) + "\n")
        self._write(audit_service_isolated, entries[2:4], "2026-08-26")
        with _production(), pytest.raises(RuntimeError, match="HMAC MISMATCH"):
            audit_service_isolated._verify_chain_on_startup()


class TestServiceWritesAVerifiableChain:
    """The end-to-end property the old tests never exercised: entries written
    through the service, then verified, then a restart over the same files."""

    def setup_method(self):
        AuditLog._last_hash = None

    @pytest.mark.asyncio
    async def test_logged_entries_verify_and_survive_a_production_restart(
        self, audit_service_isolated
    ):
        for i in range(5):
            await audit_service_isolated.log_event(
                AuditEventType.DOCUMENT_VIEW, user_id=f"u{i}", resource_id=f"doc-{i}"
            )

        stored = await audit_service_isolated.get_logs(newest_first=False)
        assert len(stored) == 5
        for earlier, later in zip(stored, stored[1:], strict=False):
            assert later["integrity"]["previous_hash"] == earlier["integrity"]["entry_hash"]
        is_valid, invalid_id = await audit_service_isolated.verify_chain_integrity(stored)
        assert (is_valid, invalid_id) == (True, None)

        with _production():
            restarted = _restart(audit_service_isolated)  # must not refuse to start
            assert AuditLog._last_hash == stored[-1]["integrity"]["entry_hash"]

            # The chain continues from the stored tail after the restart...
            await restarted.log_event(AuditEventType.LOGIN_SUCCESS, user_id="after-restart")
            # ...and a second restart still verifies.
            _restart(restarted)

        stored = await restarted.get_logs(newest_first=False)
        assert len(stored) == 6
        assert check_chain(stored).valid is True

    @pytest.mark.asyncio
    async def test_upgrade_over_legacy_logs_starts_and_chains_on(self, audit_service_isolated):
        """An instance upgraded from a build that wrote unlinked entries boots
        with tamper detection on, and links its first entry to the legacy tail."""
        legacy = _unlinked_v2_entries(3)
        path = audit_service_isolated.log_dir / "audit_2026-08-26.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in legacy))

        with _production():
            upgraded = _restart(audit_service_isolated)
            await upgraded.log_event(AuditEventType.LOGIN_SUCCESS, user_id="first-v3")
            await upgraded.log_event(AuditEventType.LOGOUT, user_id="second-v3")
            _restart(upgraded)

        stored = await upgraded.get_logs(newest_first=False)
        assert [e["integrity"].get("hash_version") for e in stored] == [2, 2, 2, 3, 3]
        assert stored[3]["integrity"]["previous_hash"] == legacy[-1]["integrity"]["entry_hash"]
        result = check_chain(stored)
        assert result.valid is True
        assert result.legacy_entries == 3

    @pytest.mark.asyncio
    async def test_torn_last_line_does_not_break_the_chain(self, audit_service_isolated):
        """A crash mid-write leaves a partial line; the next entry links to the
        last complete one, which is also what verification reads."""
        await audit_service_isolated.log_event(AuditEventType.LOGIN_SUCCESS, user_id="u0")
        log_file = audit_service_isolated._get_log_file()
        with open(log_file, "a") as f:
            f.write('{"id": "torn", "timestamp": "2026-\n')

        with _production():
            restarted = _restart(audit_service_isolated)
            await restarted.log_event(AuditEventType.LOGIN_SUCCESS, user_id="u1")
            _restart(restarted)


class TestWriteFailure:
    def setup_method(self):
        AuditLog._last_hash = None

    @staticmethod
    def _failing_open(target):
        real_open = open

        def fake_open(file, mode="r", *args, **kwargs):
            if str(file) == str(target) and "a" in mode:
                raise OSError(28, "No space left on device")
            return real_open(file, mode, *args, **kwargs)

        return patch("builtins.open", fake_open)

    @pytest.mark.asyncio
    async def test_failed_write_leaves_no_gap_in_the_chain(self, audit_service_isolated):
        await audit_service_isolated.log_event(AuditEventType.LOGIN_SUCCESS, user_id="before")
        tail = AuditLog._last_hash

        with self._failing_open(audit_service_isolated._get_log_file()):
            await audit_service_isolated.log_event(AuditEventType.LOGIN_SUCCESS, user_id="lost")
        assert AuditLog._last_hash == tail  # the lost entry never became a predecessor

        await audit_service_isolated.log_event(AuditEventType.LOGIN_SUCCESS, user_id="after")
        stored = await audit_service_isolated.get_logs(newest_first=False)
        assert [e["user"]["id"] for e in stored] == ["before", "after"]
        assert check_chain(stored).valid is True
        with _production():
            audit_service_isolated._verify_chain_on_startup()  # no raise

    @pytest.mark.asyncio
    async def test_failed_write_is_logged_critical_and_request_continues(
        self, audit_service_isolated, caplog
    ):
        with (
            self._failing_open(audit_service_isolated._get_log_file()),
            caplog.at_level("CRITICAL", logger="app.services.audit"),
        ):
            # An ordinary event: a privileged one (document.delete, user.*)
            # fails the request instead (tests/test_audit_fail_closed.py).
            entry_id = await audit_service_isolated.log_event(
                AuditEventType.DOCUMENT_VIEW, user_id="u1"
            )
        assert entry_id
        assert any("AUDIT FILE WRITE FAILED" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_fail_closed_raises(self, audit_service_isolated):
        closed = settings.model_copy(update={"audit_fail_closed": True})
        with (
            patch("app.services.audit.settings", closed),
            self._failing_open(audit_service_isolated._get_log_file()),
            pytest.raises(AuditWriteError),
        ):
            await audit_service_isolated.log_event(AuditEventType.DOCUMENT_DELETE, user_id="u1")


class TestDatabaseMirror:
    def setup_method(self):
        AuditLog._last_hash = None

    @pytest.mark.asyncio
    async def test_mirror_row_gets_a_naive_utc_timestamp(self, audit_service_isolated):
        """The column is a naive DateTime; an aware value is rejected by asyncpg."""
        captured = []

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def add(self, row):
                captured.append(row)

            async def commit(self):
                pass

        with patch("app.database.AsyncSessionLocal", lambda: _Session()):
            await audit_service_isolated.log_event(AuditEventType.LOGIN_SUCCESS, user_id="u1")

        (row,) = captured
        assert row.timestamp.tzinfo is None
        now = datetime.now(UTC).replace(tzinfo=None)
        assert abs(now - row.timestamp) < timedelta(minutes=1)
        assert row.entry_hash == AuditLog._last_hash


class TestRetention:
    def setup_method(self):
        AuditLog._last_hash = None

    @staticmethod
    def _day(days_ago: int) -> str:
        return (datetime.now(UTC).date() - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    def _plain(self, service, days_ago: int, entries: list[dict]):
        path = service.log_dir / f"audit_{self._day(days_ago)}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    def _archive(self, service, days_ago: int, entries: list[dict]):
        path = service.log_dir / f"audit_{self._day(days_ago)}.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    @pytest.mark.asyncio
    async def test_rotation_compresses_then_deletes_archives(self, audit_service_isolated):
        entries = _entries(4)
        expired_archive = self._archive(audit_service_isolated, 250, entries[0:1])
        expired_plain = self._plain(audit_service_isolated, 200, entries[1:2])
        to_compress = self._plain(audit_service_isolated, 100, entries[2:3])
        recent = self._plain(audit_service_isolated, 5, entries[3:4])

        with patch.object(AuditService, "_prune_db_rows", return_value=0):
            stats = await audit_service_isolated.rotate_old_logs(retention_days=90)

        assert stats["errors"] == []
        assert stats["deleted"] == 2
        assert stats["compressed"] == 1
        assert not expired_archive.exists()  # archives used to be kept forever
        assert not expired_plain.exists()
        assert not to_compress.exists()
        assert to_compress.with_name(to_compress.name + ".gz").exists()
        assert recent.exists()

    @pytest.mark.asyncio
    async def test_archived_entries_stay_queryable_and_verifiable(self, audit_service_isolated):
        entries = _entries(4)
        self._plain(audit_service_isolated, 100, entries[0:2])
        self._plain(audit_service_isolated, 5, entries[2:4])
        with patch.object(AuditService, "_prune_db_rows", return_value=0):
            stats = await audit_service_isolated.rotate_old_logs(retention_days=90)
        assert stats["compressed"] == 1

        stored = await audit_service_isolated.get_logs(newest_first=False)
        assert [e["id"] for e in stored] == [e["id"] for e in entries]
        assert check_chain(stored).valid is True
        assert [e["id"] for e in audit_service_isolated.iter_logs()] == [e["id"] for e in entries]

    def test_plain_file_wins_over_its_archive(self, audit_service_isolated):
        entries = _entries(2)
        self._archive(audit_service_isolated, 100, entries[0:1])
        plain = self._plain(audit_service_isolated, 100, entries)
        assert [p for _, p in audit_service_isolated._log_files()] == [plain]
