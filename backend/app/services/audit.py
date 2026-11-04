"""
Audit Logging Service

Tamper-evident log of security-relevant events: each entry is HMAC-signed
over its full stored form and chained to the previous entry, written to daily
JSONL files with a best-effort database mirror.

Chain format history
--------------------
v1  HMAC over seven scalar fields.
v2  HMAC over the full stored entry. Builds that wrote v1/v2 linked each entry
    to its own provisional hash rather than to the entry stored before it, so
    those entries carry a valid signature but no usable linkage.
v3  (this build) HMAC over the full stored entry and the hash version, linked
    to the entry stored immediately before it.

Verification checks every entry's own HMAC whatever its version, and enforces
linkage from the first v3 entry onward: a v3 entry must point at the entry
stored before it, including the last legacy entry at the upgrade boundary. A
v1/v2 entry that follows a v3 entry is reported as tampering, and because the
version is inside the v3 HMAC an entry cannot be relabelled as legacy to
escape the linkage check. Logs written before the upgrade therefore keep
verifying (edits are still caught; removal of a legacy entry is not, as it
never was), and an upgraded instance starts without disabling
AUDIT_HALT_ON_TAMPERING.
"""

import gzip
import hashlib
import hmac as hmac_mod
import json
import logging
import re
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from app.config import settings
from app.exceptions import ServiceUnavailableError

# audit_YYYY-MM-DD.jsonl, or the same name with .gz once archived
_LOG_FILE_RE = re.compile(r"^audit_(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$")

# Entries re-verified on every startup (the most recent ones, in write order)
_STARTUP_VERIFY_ENTRIES = 1000

# Upper bound on distinct ip/email keys tracked for brute-force detection
_FAILED_LOGIN_MAX_KEYS = 10_000


class AuditWriteError(ServiceUnavailableError):
    """The audit entry could not be written and AUDIT_FAIL_CLOSED is set."""


class AuditEventType(str, Enum):
    # Authentication (nosec B105 - these are event type identifiers, not passwords)
    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGOUT = "auth.logout"
    TOKEN_REFRESH = "auth.token.refresh"  # nosec B105
    MFA_CHALLENGE = "auth.mfa.challenge"
    MFA_SUCCESS = "auth.mfa.success"
    MFA_FAILURE = "auth.mfa.failure"
    MFA_ENABLED = "auth.mfa.enabled"
    MFA_DISABLED = "auth.mfa.disabled"
    PASSWORD_CHANGE = "auth.password.change"  # nosec B105
    PASSWORD_RESET_REQUEST = "auth.password.reset.request"  # nosec B105
    PASSWORD_RESET_COMPLETE = "auth.password.reset.complete"  # nosec B105
    # Device-code sign-in (Word add-in): a signed-in web user decides a code
    DEVICE_CODE_APPROVED = "auth.device.approved"
    DEVICE_CODE_DENIED = "auth.device.denied"

    # Document Access
    DOCUMENT_VIEW = "document.view"
    DOCUMENT_DOWNLOAD = "document.download"
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_DELETE = "document.delete"

    # RAG/Chat
    CHAT_QUERY = "chat.query"

    # Connectors
    CONNECTOR_CONNECT = "connector.connect"
    CONNECTOR_DISCONNECT = "connector.disconnect"
    CONNECTOR_SYNC_START = "connector.sync.start"
    CONNECTOR_SYNC_COMPLETE = "connector.sync.complete"
    CONNECTOR_SYNC_FAILURE = "connector.sync.failure"

    # Admin
    SETTINGS_CHANGE = "settings.change"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"
    USER_ROLE_CHANGE = "user.role.change"
    USER_DEACTIVATE = "user.deactivate"

    # API Keys
    API_KEY_CREATED = "apikey.created"
    API_KEY_DELETED = "apikey.deleted"

    # Security Events
    ACCESS_DENIED = "security.access.denied"
    RATE_LIMIT_EXCEEDED = "security.ratelimit.exceeded"
    SUSPICIOUS_ACTIVITY = "security.suspicious"
    DATA_EXPORT = "security.data.export"
    SECURITY_ALERT = "security.alert"
    BRUTE_FORCE_DETECTED = "security.bruteforce"
    UNAUTHORIZED_ACCESS_ATTEMPT = "security.unauthorized"
    CSRF_VIOLATION = "security.csrf.violation"
    SESSION_HIJACK_ATTEMPT = "security.session.hijack"

    # Compliance
    AUDIT_LOG_ACCESS = "compliance.audit.access"
    DATA_ACCESS = "compliance.data.access"
    DATA_MODIFICATION = "compliance.data.modification"
    DATA_DELETION = "compliance.data.deletion"


# Events that record a privileged or destructive action. Their audit entry is
# part of the action: when it cannot be written the action fails, regardless
# of AUDIT_FAIL_CLOSED (which extends the same rule to every event). The
# compliance.data.modification type is shared by ordinary edits, so for it the
# recorded ``action`` decides.
PRIVILEGED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "document.delete",
        "settings.change",
        "user.create",
        "user.update",
        "user.delete",
        "user.role.change",
        "user.deactivate",
        "apikey.created",
        "apikey.deleted",
        "auth.mfa.disabled",
        "security.data.export",
        "compliance.data.deletion",
    }
)
PRIVILEGED_ACTIONS: frozenset[str] = frozenset(
    {
        "ethical_wall_added",
        "ethical_wall_removed",
        "legal_hold_set",
        "matter_deleted",
        "matter_member_added",
        "matter_member_removed",
        "matter_ownership_transferred",
        "retention_set",
        "retention_expired",
    }
)


def is_privileged(event_type: str, details: dict[str, Any] | None) -> bool:
    """Whether an event's audit record is a precondition of its action."""
    value = getattr(event_type, "value", event_type)  # the enum or its string
    if str(value) in PRIVILEGED_EVENT_TYPES:
        return True
    action = (details or {}).get("action")
    return isinstance(action, str) and action in PRIVILEGED_ACTIONS


def format_audit_timestamp(moment: datetime) -> str:
    """UTC timestamp as written to new entries: 2026-10-04T12:00:00.000000Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_audit_timestamp(raw: str) -> datetime:
    """Parse a stored entry timestamp into an aware UTC datetime.

    Accepts the current ``...Z`` form and the ``...+00:00Z`` form written by
    earlier builds. Raises ValueError for anything else unparseable.
    """
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1]
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_utc(moment: datetime | None) -> datetime | None:
    """Normalise a filter bound to aware UTC; naive values are taken as UTC."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class AuditLog:
    """Structured audit log entry with correlation ID and chain hashing for tamper detection."""

    # Hash of the last entry written to disk. Advanced only by
    # AuditService.log() after a successful write, so the chain always
    # describes what is actually stored.
    _last_hash: str | None = None
    _chain_lock = threading.Lock()

    # HMAC key for signing log entries (derived from app secret)
    _hmac_key: bytes = (settings.audit_hmac_key or settings.secret_key).encode()[:32]
    # Previous key, if the operator rotated AUDIT_HMAC_KEY: entries written
    # under it still verify, so rotation doesn't read as tampering at startup.
    _hmac_key_previous: bytes | None = (
        settings.audit_hmac_key_previous.encode()[:32] if settings.audit_hmac_key_previous else None
    )

    # Hash-input format written by this build; see the module docstring.
    HASH_VERSION = 3
    # First version whose previous_hash is the entry stored before it.
    LINKED_HASH_VERSION = 3

    @classmethod
    def verification_keys(cls) -> list[bytes]:
        keys = [cls._hmac_key]
        if cls._hmac_key_previous and cls._hmac_key_previous != cls._hmac_key:
            keys.append(cls._hmac_key_previous)
        return keys

    def __init__(
        self,
        event_type: AuditEventType,
        user_id: str | None = None,
        user_email: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        action_details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        success: bool = True,
        error_message: str | None = None,
        correlation_id: str | None = None,
        request_id: str | None = None,
    ):
        self.id = str(uuid.uuid4())
        self.timestamp = format_audit_timestamp(datetime.now(UTC))
        self.event_type = event_type.value
        self.user_id = user_id
        self.user_email = user_email
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.action_details = action_details or {}
        self.ip_address = ip_address
        self.user_agent = user_agent
        self.success = success
        self.error_message = error_message
        self.environment = "production" if not settings.debug else "development"

        # Correlation ID for request tracing across services
        self.correlation_id = correlation_id or str(uuid.uuid4())

        # Request ID for tracing within a single request
        self.request_id = request_id

        # Provisional seal so a standalone entry is well-formed. The chain
        # itself is not advanced here: AuditService.log() re-seals the entry
        # against the last stored hash at write time.
        self.hash_version = AuditLog.HASH_VERSION
        self.previous_hash: str | None = None
        self.entry_hash: str | None = None
        self.seal(AuditLog._last_hash)

    def seal(self, previous_hash: str | None) -> str:
        """Link this entry to `previous_hash` and sign it. Returns the entry hash."""
        self.previous_hash = previous_hash
        self.entry_hash = None
        self.entry_hash = self._compute_hash()
        return self.entry_hash

    def _compute_hash(self) -> str:
        """Compute HMAC-SHA256 over the full stored entry, chained to the previous one.

        Using HMAC instead of plain SHA-256 prevents an attacker with
        filesystem access from recomputing valid hashes after tampering,
        since they would also need the application secret key. The input is
        exactly what lands on disk (see `to_dict`), so verification can be
        replayed from the JSONL files alone.
        """
        return compute_entry_hash(self.to_dict(), AuditLog._hmac_key)

    @staticmethod
    def _mask_email(email: str | None) -> str | None:
        """Mask email for audit logs: u***@example.com"""
        if not email or "@" not in email:
            return email
        local, domain = email.split("@", 1)
        return f"{local[0]}***@{domain}" if local else f"***@{domain}"

    @staticmethod
    def _mask_ip(ip: str | None) -> str | None:
        """Mask IP for audit logs: 192.168.1.*** / partial IPv6"""
        if not ip:
            return ip
        if "." in ip:
            parts = ip.split(".")
            if len(parts) == 4:
                return f"{parts[0]}.{parts[1]}.{parts[2]}.***"
        elif ":" in ip:
            parts = ip.split(":")
            if len(parts) > 2:
                return ":".join(parts[:3]) + ":***"
        return ip

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "user": {"id": self.user_id, "email": self._mask_email(self.user_email)},
            "resource": {"type": self.resource_type, "id": self.resource_id},
            "action": self.action_details,
            "context": {
                "ip_address": self._mask_ip(self.ip_address),
                "user_agent": self.user_agent,
                "environment": self.environment,
            },
            "outcome": {"success": self.success, "error": self.error_message},
            "integrity": {
                "previous_hash": self.previous_hash,
                "entry_hash": self.entry_hash,
                "hash_version": self.hash_version,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _canonical_payload(entry: dict[str, Any]) -> bytes:
    """Serialize everything in a stored entry except its integrity block.

    The payload is round-tripped through JSON first so the bytes hashed at
    write time are identical to the bytes recomputed from the file later
    (tuples become lists, non-string keys become strings, etc.).
    """
    payload = {k: v for k, v in entry.items() if k != "integrity"}
    normalized = json.loads(json.dumps(payload))
    return json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()


def _hash_version(entry: dict[str, Any]) -> int:
    """An entry's recorded hash version; anything without a usable one is v1."""
    version = (entry.get("integrity") or {}).get("hash_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        return version
    return 1


def compute_entry_hash(entry: dict[str, Any], key: bytes) -> str:
    """Recompute an entry's chain HMAC from its stored form under `key`.

    Honors the entry's own `integrity.hash_version` so legacy entries keep
    verifying after an upgrade; anything without a version is treated as v1.
    """
    integrity = entry.get("integrity") or {}
    previous = integrity.get("previous_hash") or "GENESIS"
    version = _hash_version(entry)
    if version >= 3:
        # The version is part of the signed input, so a linked entry cannot be
        # relabelled as a legacy one without invalidating its signature.
        message = f"v{version}\n{previous}\n".encode() + _canonical_payload(entry)
    elif version == 2:
        message = previous.encode() + b"\n" + _canonical_payload(entry)
    else:
        user = entry.get("user") or {}
        resource = entry.get("resource") or {}
        outcome = entry.get("outcome") or {}
        message = (
            f"{previous}:{entry.get('id')}:{entry.get('timestamp')}:{entry.get('event_type')}:"
            f"{user.get('id')}:{resource.get('id')}:{outcome.get('success')}"
        ).encode()
    return hmac_mod.new(key, message, hashlib.sha256).hexdigest()


def verify_entry_hash(entry: dict[str, Any]) -> bool:
    """True if the stored entry_hash matches a recomputation under any accepted key."""
    stored = (entry.get("integrity") or {}).get("entry_hash")
    if not isinstance(stored, str) or not stored:
        return False
    return any(
        hmac_mod.compare_digest(compute_entry_hash(entry, key), stored)
        for key in AuditLog.verification_keys()
    )


@dataclass
class ChainCheck:
    """Outcome of verifying a run of stored entries in write order."""

    valid: bool
    entries_checked: int
    first_invalid_id: str | None = None
    reason: str | None = None
    # Entries written before linkage was enforced (signature checked only)
    legacy_entries: int = 0


def check_chain(entries: list[dict[str, Any]]) -> ChainCheck:
    """Verify `entries` (consecutive stored entries, oldest first).

    Every entry's HMAC is recomputed. Linkage is enforced for v3+ entries and
    a legacy entry after a linked one is rejected; see the module docstring.
    The first entry of the run has no predecessor in hand, so only its
    signature is checked.
    """
    previous: dict[str, Any] | None = None
    linked_seen = False
    legacy = 0

    for entry in entries:
        integrity = entry.get("integrity") or {}
        entry_id = entry.get("id")
        linked = _hash_version(entry) >= AuditLog.LINKED_HASH_VERSION

        reason = None
        if linked:
            if previous is not None:
                expected = (previous.get("integrity") or {}).get("entry_hash")
                found = integrity.get("previous_hash")
                if found != expected:
                    reason = (
                        f"AUDIT CHAIN INTEGRITY BROKEN at entry {entry_id}. "
                        f"Expected previous_hash={expected}, got {found}"
                    )
        elif linked_seen:
            reason = (
                f"AUDIT CHAIN INTEGRITY BROKEN at entry {entry_id}: a legacy-format "
                "entry follows a linked entry."
            )

        if reason is None and not verify_entry_hash(entry):
            reason = (
                f"AUDIT ENTRY HMAC MISMATCH at entry {entry_id}: "
                "the stored entry does not match its signature. Either the "
                "entry was modified after it was written, or AUDIT_HMAC_KEY "
                "was rotated without setting AUDIT_HMAC_KEY_PREVIOUS."
            )

        if reason is not None:
            return ChainCheck(
                valid=False,
                entries_checked=len(entries),
                first_invalid_id=entry_id,
                reason=reason,
                legacy_entries=legacy,
            )

        if linked:
            linked_seen = True
        else:
            legacy += 1
        previous = entry

    return ChainCheck(valid=True, entries_checked=len(entries), legacy_entries=legacy)


@contextmanager
def _chain_file_lock(log_dir: Path):
    """An exclusive advisory lock on ``<log_dir>/.chain.lock`` for the duration
    of one append, so several worker processes writing the same daily file
    never interleave or fork the hash chain. Best effort: if the lock cannot
    be taken the append proceeds as a single process would."""
    handle = None
    locked = False
    try:
        handle = open(log_dir / ".chain.lock", "a+")  # noqa: SIM115 - held for the block
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
    except OSError as e:
        logging.getLogger(__name__).warning(f"Audit chain lock unavailable: {e}")
    try:
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


class AuditService:
    """Writes, queries, verifies and rotates the audit trail."""

    def __init__(self):
        self.log_dir = Path(settings.upload_dir).parent / "audit_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._current_log_file = None
        self._current_date = None
        self._load_last_hash()
        self._verify_chain_on_startup()

    # ==================== Log files ====================

    def _log_files(self) -> list[tuple[date, Path]]:
        """Daily log files, oldest first, including gzip archives.

        When a day has both the plain file and its archive (rotation was
        interrupted between compressing and unlinking), the plain file wins.
        """
        by_date: dict[date, Path] = {}
        for path in self.log_dir.glob("audit_*.jsonl*"):
            match = _LOG_FILE_RE.match(path.name)
            if not match:
                continue
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date not in by_date or not match.group(2):
                by_date[file_date] = path
        return sorted(by_date.items())

    @staticmethod
    def _read_entries(path: Path) -> list[dict[str, Any]]:
        """All parseable entries of one log file, in write order."""
        opener = gzip.open if path.suffix == ".gz" else open
        entries = []
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
        return entries

    def _tail_entries(self, count: int) -> list[dict[str, Any]]:
        """The most recent `count` stored entries, oldest first, across files."""
        chunks: list[list[dict[str, Any]]] = []
        total = 0
        for _, path in reversed(self._log_files()):
            entries = self._read_entries(path)
            chunks.append(entries)
            total += len(entries)
            if total >= count:
                break
        ordered = [entry for chunk in reversed(chunks) for entry in chunk]
        return ordered[-count:]

    def _load_last_hash(self):
        """Load the hash of the last stored entry on startup.

        This ensures the chain is preserved across process restarts.
        """
        try:
            tail = self._tail_entries(1)
            if tail:
                last_hash = (tail[-1].get("integrity") or {}).get("entry_hash")
                if last_hash:
                    AuditLog._last_hash = last_hash
        except (OSError, EOFError, ValueError):
            pass  # Start fresh chain if we can't load

    def _verify_chain_on_startup(self):
        """Verify integrity of recent audit entries on startup.

        Verifies the most recent entries (up to _STARTUP_VERIFY_ENTRIES) in the
        order they were written, across daily file boundaries.
        """

        logger = logging.getLogger(__name__)
        try:
            entries = self._tail_entries(_STARTUP_VERIFY_ENTRIES)
            if not entries:
                return

            # Linkage between neighbours AND each entry's own HMAC. Linkage
            # alone only proves the previous_hash pointers agree; recomputing
            # the HMAC is what catches an edited entry (or a whole chain
            # rewritten by someone without the key).
            result = check_chain(entries)

            if not result.valid:
                msg = result.reason
                logger.critical(msg)
                # Only halt in true production (stable HMAC key).
                # In debug (local development) the HMAC key may be
                # auto-generated, so chain breaks are expected and not a
                # sign of tampering.
                _is_production = not settings.debug
                if settings.audit_halt_on_tampering and _is_production:
                    raise RuntimeError(
                        f"FATAL: {msg} — Refusing to start. "
                        "Investigate audit log tampering before restarting. "
                        "Set AUDIT_HALT_ON_TAMPERING=false to override (not recommended)."
                    )
                return

            logger.info(
                "Audit chain integrity verified (%d entries, %d written before chain linkage)",
                result.entries_checked,
                result.legacy_entries,
            )
        except (OSError, EOFError, KeyError, ValueError) as e:
            logger.warning(f"Could not verify audit chain on startup: {e}")

    async def assert_writable(self) -> None:
        """Refuse a privileged action before it starts if its record could not be written.

        Most endpoints audit after they act, so a write that fails then has
        nothing left to refuse. This probe opens today's log file for append
        under the chain locks, exactly as :meth:`log` will, and raises
        :class:`AuditWriteError` (a 503) if that cannot be done. The
        permission dependencies call it for the permissions that gate
        privileged actions (app/services/permissions.py).
        """
        try:
            with AuditLog._chain_lock, _chain_file_lock(self.log_dir):
                log_file = self._get_log_file()
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8"):
                    pass
        except OSError as exc:
            logging.getLogger(__name__).critical(
                "AUDIT LOG NOT WRITABLE — refusing a privileged action: %s", exc
            )
            raise AuditWriteError(
                "The audit log is not writable, so this action was refused before it "
                "started. The audit log directory is full or not writable; check the "
                "data volume."
            ) from exc

    def _get_log_file(self) -> Path:
        """Get current log file, rotating daily."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self._current_date != today:
            self._current_date = today
            self._current_log_file = self.log_dir / f"audit_{today}.jsonl"
        return self._current_log_file

    # ==================== Writing ====================

    def _last_hash_on_disk(self) -> str | None:
        """The hash of the last entry in today's file, read from its tail
        (a few KB, however large the file), or of the last entry at all when
        today's file is empty. None when nothing is stored."""
        log_file = self._get_log_file()
        try:
            size = log_file.stat().st_size
        except OSError:
            size = 0
        if size:
            try:
                with open(log_file, "rb") as f:
                    f.seek(max(0, size - 65536))
                    chunk = f.read().decode("utf-8", "ignore")
                for line in reversed(chunk.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    found = (entry.get("integrity") or {}).get("entry_hash")
                    if found:
                        return found
                    return None
            except OSError:
                return None
        try:
            tail = self._tail_entries(1)
        except (OSError, EOFError, ValueError):
            return None
        return (tail[-1].get("integrity") or {}).get("entry_hash") if tail else None

    async def log(self, entry: AuditLog):
        """Write an audit log entry to both JSONL file and database.

        The entry is linked to the last stored entry, signed and appended
        under one lock with no await in between, so concurrent coroutines and
        threads cannot interleave; a lock file beside the logs serialises
        worker processes on the same data volume the same way, and the last
        hash is re-read from disk under it so an entry another process just
        appended is what this one chains to. The chain only advances once
        the line is on disk: a failed write leaves no gap for the next entry
        to trip over.
        """
        import os
        import stat

        written = False
        with AuditLog._chain_lock, _chain_file_lock(self.log_dir):
            on_disk = self._last_hash_on_disk()
            if on_disk is not None and on_disk != AuditLog._last_hash:
                AuditLog._last_hash = on_disk  # another process wrote since we last did
            entry.seal(AuditLog._last_hash)
            log_file = self._get_log_file()

            # Append to JSONL file (one JSON object per line)
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(entry.to_json() + "\n")
            except OSError as exc:
                logging.getLogger(__name__).critical(
                    "AUDIT FILE WRITE FAILED — audit entry %s (%s) lost from JSONL: %s",
                    entry.id,
                    entry.event_type,
                    exc,
                    exc_info=True,
                )
            else:
                AuditLog._last_hash = entry.entry_hash
                written = True

        if written:
            # Restrict file permissions to owner only (0o600)
            try:
                os.chmod(log_file, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Windows may not support chmod

        # Persist to database (best-effort — JSONL is the primary record). On a
        # failed file write this row is the only trace of the event.
        await self._persist_to_db(entry)

        if not written and (
            settings.audit_fail_closed or is_privileged(entry.event_type, entry.action_details)
        ):
            # The entry is logged at CRITICAL above and mirrored to the database
            # when that works; the request that produced it still fails, so a
            # privileged action never completes without its record.
            raise AuditWriteError(
                "The audit record for this action could not be written, so the action "
                "was refused. The audit log directory is full or not writable; "
                f"check the data volume (entry {entry.id})."
            )

        # Log security events with higher priority
        if entry.event_type.startswith("security."):
            await self._handle_security_event(entry)

    async def _persist_to_db(self, entry: AuditLog):
        """Persist audit entry to database for SQL-queryable compliance reporting.

        Best-effort: failures are logged but do not block the primary JSONL write.
        """
        try:
            from sqlalchemy.exc import IntegrityError

            from app.database import AsyncSessionLocal
            from app.models.audit import AuditLog as AuditLogDB

            row = {
                # The column is a naive DateTime holding UTC
                "timestamp": parse_audit_timestamp(entry.timestamp).replace(tzinfo=None),
                "event_type": entry.event_type,
                "user_id": entry.user_id,
                "user_email": AuditLog._mask_email(entry.user_email),
                "resource_type": entry.resource_type,
                "resource_id": entry.resource_id,
                "action_details": entry.action_details,
                "ip_address": AuditLog._mask_ip(entry.ip_address),
                "user_agent": entry.user_agent,
                "success": entry.success,
                "error_message": entry.error_message,
                "environment": entry.environment,
                "entry_hash": entry.entry_hash,
                "previous_hash": entry.previous_hash,
                "correlation_id": entry.correlation_id,
            }
            try:
                async with AsyncSessionLocal() as session:
                    session.add(AuditLogDB(**row))
                    await session.commit()
            except IntegrityError:
                # audit_logs.user_id references users.id, but an actor need not
                # have a users row (the DEBUG dev login, a just-deleted user).
                # Mirror the event unlinked, keeping the id in the details,
                # instead of dropping it from the queryable copy.
                if row["user_id"] is None:
                    raise
                row["action_details"] = {
                    **(entry.action_details or {}),
                    "unlinked_user_id": row["user_id"],
                }
                row["user_id"] = None
                async with AsyncSessionLocal() as session:
                    session.add(AuditLogDB(**row))
                    await session.commit()
        except (OSError, ValueError, KeyError, ImportError) as exc:
            logging.getLogger(__name__).warning(
                "Audit DB write failed (JSONL is the primary record): %s", exc, exc_info=True
            )
        except Exception as exc:
            # Catch SQLAlchemy and other DB errors that escape the above tuple

            logging.getLogger(__name__).warning(
                "Audit DB write failed (unexpected — JSONL is the primary record): %s",
                exc,
                exc_info=True,
            )

    async def _handle_security_event(self, entry: AuditLog):
        """Handle security-relevant events (alerts, etc.)."""

        logger = logging.getLogger("security")

        # Log security events with high priority, masked the same way as the
        # stored entry so application logs don't carry more PII than the trail.
        who = entry.user_id or AuditLog._mask_email(entry.user_email) or "unknown"
        log_message = (
            f"SECURITY EVENT: {entry.event_type} | "
            f"User: {who} | "
            f"IP: {AuditLog._mask_ip(entry.ip_address) or 'unknown'} | "
            f"Success: {entry.success} | "
            f"Details: {entry.action_details}"
        )

        if entry.success:
            logger.warning(log_message)
        else:
            logger.error(log_message)

        # Track failed login attempts for brute force detection
        if entry.event_type == AuditEventType.LOGIN_FAILURE.value:
            await self._track_failed_login(entry.ip_address, entry.user_email)

    async def _track_failed_login(self, ip_address: str, email: str):
        """Track failed login attempts for brute force detection.

        In-memory and per-process. The key includes a caller-supplied email,
        so the table is capped: the least recently seen key is dropped first.
        """
        if not hasattr(self, "_failed_logins"):
            self._failed_logins: OrderedDict[str, list[datetime]] = OrderedDict()

        key = f"{ip_address}:{email[:254]}" if email else str(ip_address)

        # Keep only last hour
        now = datetime.now(UTC)
        one_hour_ago = now - timedelta(hours=1)
        attempts = [t for t in self._failed_logins.get(key, []) if t > one_hour_ago]
        attempts.append(now)
        self._failed_logins[key] = attempts
        self._failed_logins.move_to_end(key)
        while len(self._failed_logins) > _FAILED_LOGIN_MAX_KEYS:
            self._failed_logins.popitem(last=False)

        # Alert on brute force (5+ failures in 1 hour)
        if len(attempts) >= 5:
            await self.log_event(
                event_type=AuditEventType.BRUTE_FORCE_DETECTED,
                user_email=email,
                ip_address=ip_address,
                details={
                    "failed_attempts": len(attempts),
                    "window": "1 hour",
                },
                success=False,
            )

    async def log_event(
        self,
        event_type: AuditEventType,
        user_id: str = None,
        user_email: str = None,
        resource_type: str = None,
        resource_id: str = None,
        details: dict[str, Any] = None,
        ip_address: str = None,
        user_agent: str = None,
        success: bool = True,
        error: str = None,
        correlation_id: str = None,
        request_id: str = None,
    ):
        """Convenience method to create and log an event."""
        entry = AuditLog(
            event_type=event_type,
            user_id=user_id,
            user_email=user_email,
            resource_type=resource_type,
            resource_id=resource_id,
            action_details=details,
            ip_address=ip_address,
            user_agent=user_agent,
            success=success,
            error_message=error,
            correlation_id=correlation_id,
            request_id=request_id,
        )
        await self.log(entry)
        return entry.id

    # ==================== Verification & querying ====================

    def check_chain(self, logs: list) -> ChainCheck:
        """Verify consecutive stored entries (oldest first); see `check_chain`."""
        return check_chain(logs)

    async def verify_chain_integrity(self, logs: list) -> tuple[bool, str | None]:
        """
        Verify the integrity of a chain of audit logs (oldest first).

        Returns: (is_valid, first_invalid_id)
        """
        result = check_chain(logs)
        return result.valid, result.first_invalid_id

    @staticmethod
    def _matches(
        entry: dict[str, Any],
        start: datetime | None,
        end: datetime | None,
        event_type: AuditEventType | None,
        user_id: str | None,
    ) -> bool:
        if event_type and entry.get("event_type") != event_type.value:
            return False
        if user_id and (entry.get("user") or {}).get("id") != user_id:
            return False
        if start or end:
            try:
                entry_time = parse_audit_timestamp(entry["timestamp"])
            except (KeyError, ValueError, TypeError, AttributeError):
                return False
            if start and entry_time < start:
                return False
            if end and entry_time > end:
                return False
        return True

    def _iter_file_entries(
        self,
        start_date: datetime | None,
        end_date: datetime | None,
        event_type: AuditEventType | None,
        user_id: str | None,
        newest_first: bool,
    ) -> Iterator[dict[str, Any]]:
        """Matching entries, one daily file at a time, in the requested order."""
        start, end = _as_utc(start_date), _as_utc(end_date)
        files = self._log_files()
        if newest_first:
            files.reverse()

        for file_date, path in files:
            # Skip files outside the date range based on filename
            if start and file_date < start.date():
                continue
            if end and file_date > end.date():
                continue
            try:
                entries = self._read_entries(path)
            except (OSError, EOFError) as exc:
                logging.getLogger(__name__).warning(
                    "Could not read audit log file %s: %s", path.name, exc
                )
                continue
            if newest_first:
                entries.reverse()
            for entry in entries:
                if self._matches(entry, start, end, event_type, user_id):
                    yield entry

    def iter_logs(
        self,
        start_date: datetime = None,
        end_date: datetime = None,
        event_type: AuditEventType = None,
        user_id: str = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream matching entries oldest first (for export), archives included."""
        return self._iter_file_entries(
            start_date, end_date, event_type, user_id, newest_first=False
        )

    async def get_logs(
        self,
        start_date: datetime = None,
        end_date: datetime = None,
        event_type: AuditEventType = None,
        user_id: str = None,
        limit: int = 100,
        newest_first: bool = True,
    ) -> list:
        """Query audit logs (for compliance reporting).

        Returns the newest `limit` matching entries, gzip archives included.
        `newest_first=False` returns that same set in write order, which is
        what chain verification needs. Naive date bounds are taken as UTC.
        """
        logs = []
        for entry in self._iter_file_entries(
            start_date, end_date, event_type, user_id, newest_first=True
        ):
            logs.append(entry)
            if len(logs) >= limit:
                break
        if not newest_first:
            logs.reverse()
        return logs

    # ==================== Log Rotation & Archival ====================

    async def rotate_old_logs(
        self,
        retention_days: int = 90,
        archive: bool = True,
    ) -> dict[str, Any]:
        """Compress logs older than retention_days and remove logs older than 2x retention.

        Designed to be called from a scheduled task (e.g., daily cron or APScheduler).

        Args:
            retention_days: Days to keep uncompressed JSONL files. Files older than
                this are gzip-compressed. Files (plain or compressed) older than
                2x this are deleted.
            archive: If True, compress files past retention_days. If False, leave
                them uncompressed until the 2x deletion horizon.

        Returns:
            Summary dict with counts of compressed, deleted, and skipped files.
        """
        import shutil

        today = datetime.now(UTC).date()
        compress_cutoff = today - timedelta(days=retention_days)
        delete_cutoff = today - timedelta(days=retention_days * 2)

        stats: dict[str, Any] = {"compressed": 0, "deleted": 0, "skipped": 0, "errors": []}

        for log_file in sorted(self.log_dir.glob("audit_*.jsonl*")):
            match = _LOG_FILE_RE.match(log_file.name)
            if not match:
                stats["skipped"] += 1
                continue
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                stats["skipped"] += 1
                continue
            is_archive = bool(match.group(2))

            # Never touch today's active log
            if file_date >= today:
                stats["skipped"] += 1
                continue

            # Delete anything past the deletion horizon, archives included
            if file_date < delete_cutoff:
                try:
                    log_file.unlink()
                    stats["deleted"] += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    stats["errors"].append(f"Delete {log_file.name}: {e}")
                continue

            # Compress files past retention that aren't already compressed
            if not is_archive and file_date < compress_cutoff and archive:
                gz_path = log_file.with_name(log_file.name + ".gz")
                if gz_path.exists():
                    # Already compressed — remove uncompressed original
                    try:
                        log_file.unlink()
                    except OSError:
                        pass
                    stats["skipped"] += 1
                    continue
                # Write under a temporary name so an interrupted run never
                # leaves a truncated archive that readers would pick up.
                tmp_path = log_file.with_name(log_file.name + ".gz.tmp")
                try:
                    with open(log_file, "rb") as f_in, gzip.open(tmp_path, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    tmp_path.replace(gz_path)
                    log_file.unlink()
                    stats["compressed"] += 1
                except OSError as e:
                    stats["errors"].append(f"Compress {log_file.name}: {e}")
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                continue

            stats["skipped"] += 1

        # Prune audit_logs DB rows past the delete cutoff too, so the queryable
        # table doesn't grow unbounded alongside the rotated JSONL files. The
        # tamper-evident JSONL chain is the archival record; the DB table is the
        # query surface and follows the same 2x-retention deletion horizon.
        stats["db_rows_deleted"] = await self._prune_db_rows(delete_cutoff)

        return stats

    @staticmethod
    async def _prune_db_rows(delete_cutoff) -> int:
        """Delete audit_logs rows older than *delete_cutoff* (a date). Returns count."""
        from datetime import datetime as dt
        from datetime import time as dt_time

        try:
            from sqlalchemy import delete as sa_delete

            from app.database import AsyncSessionLocal
            from app.models.audit import AuditLog

            cutoff_ts = dt.combine(delete_cutoff, dt_time.min)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    sa_delete(AuditLog).where(AuditLog.timestamp < cutoff_ts)
                )
                await session.commit()
                return int(result.rowcount or 0)
        except Exception as e:  # retention must not crash the scheduler
            logging.getLogger(__name__).warning("Audit DB-row pruning failed: %s", e)
            return 0


# Global audit service instance
audit_service = AuditService()
