#!/bin/bash
# CaseCite backup — PostgreSQL dump + the application data volume.
#
# Run this on the HOST, from the directory that holds docker-compose.prod.yml
# (the backend image does not ship pg_dump or this script). Daily cron:
#   0 2 * * * cd /opt/casecite && ./scripts/backup_database.sh >> /var/log/casecite-backup.log 2>&1
#
# Produces, under BACKUP_DIR:
#   casecite_<timestamp>.sql.gz          PostgreSQL (users, matters, chats, analyses…)
#   rag_data_<timestamp>.tar.gz          /app/data volume: uploaded documents (encrypted
#                                        at rest), vector index, audit logs, BYOK key
#                                        store, sessions, instance secrets
# Both get a .enc suffix when BACKUP_ENCRYPTION_KEY is set, and each is written
# with a <file>.sha256 checksum that restore_database.sh verifies. Restore the
# two from the same run: document records and their vectors drift out of sync
# otherwise.
#
# NOT IN THE BACKUP — escrow these separately (password manager / secrets vault):
#   SECRET_KEY, ENCRYPTION_SALT, AUDIT_HMAC_KEY   from the deployment's .env
#   BACKUP_ENCRYPTION_KEY                         the key for these archives
# Without SECRET_KEY + ENCRYPTION_SALT a restored instance cannot decrypt the
# uploaded documents, the BYOK key store, connector tokens or TOTP secrets, and
# without AUDIT_HMAC_KEY the restored audit log fails verification. They are
# deliberately not written into the archive: a backup that carries its own keys
# is plaintext to whoever obtains it.
#
# Encryption is required by default. The database dump holds chat history,
# analyses and password hashes, and the data archive holds the vector index
# (document text in cleartext) and audit logs. To write unencrypted archives
# anyway (e.g. onto an already-encrypted backup target) set
# BACKUP_ALLOW_UNENCRYPTED=true.
#
# Modes (BACKUP_MODE, default "compose"):
#   compose  — bundled stack: pg_dump runs inside the postgres container over its
#              local socket (no password needed) and the data volume is read from
#              the backend container's /app/data mount. Needs docker + compose.
#   direct   — external/managed Postgres: pg_dump on PATH, POSTGRES_HOST/PORT/DB/
#              USER + PGPASSWORD set. The data volume is still archived if
#              DATA_VOLUME is set (docker volume name) or DATA_DIR (a host path).
#
# Environment variables:
#   COMPOSE_FILE_PATH        (default: docker-compose.prod.yml)
#   BACKUP_DIR               (default: ./backups)
#   BACKUP_RETENTION_DAYS    (default: 30; applies to BACKUP_DIR and to S3)
#   BACKUP_ENCRYPTION_KEY    (AES-256-CBC with PBKDF2. Supply it in the cron
#                             environment or a root-only file you `source` —
#                             NOT in the deployment's .env, which is injected
#                             into the backend container. Keep a copy off this
#                             server; without it the .enc files are
#                             unrecoverable)
#   BACKUP_ALLOW_UNENCRYPTED (default: false; see above)
#   BACKUP_PAUSE_BACKEND     (compose mode ONLY, default: true — the backend is
#                             paused while the data volume is archived so the
#                             vector index's SQLite files are captured
#                             consistently; requests wait for the duration.
#                             Direct mode never pauses anything: do it yourself)
#   S3_BUCKET / S3_ENDPOINT / S3_SSE   (optional upload; needs the aws CLI)
#   POSTGRES_USER            (default: casecite; must match the compose stack)
#   POSTGRES_DB              (default: casecite)
#   POSTGRES_HOST/PORT, PGPASSWORD      (direct mode only)
#   DATA_VOLUME / DATA_DIR   (direct mode only)

set -euo pipefail

BACKUP_MODE="${BACKUP_MODE:-compose}"
COMPOSE_FILE_PATH="${COMPOSE_FILE_PATH:-docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
BACKUP_PAUSE_BACKEND="${BACKUP_PAUSE_BACKEND:-true}"
POSTGRES_DB="${POSTGRES_DB:-casecite}"
POSTGRES_USER="${POSTGRES_USER:-casecite}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

compose() { docker compose -f "$COMPOSE_FILE_PATH" "$@"; }

if [ -z "${BACKUP_ENCRYPTION_KEY:-}" ]; then
    if [ "${BACKUP_ALLOW_UNENCRYPTED:-false}" != "true" ]; then
        log "ERROR: BACKUP_ENCRYPTION_KEY is not set. The archives contain client data, chat"
        log "       history, password hashes and the vector index in cleartext, so this script"
        log "       refuses to write them unencrypted. Set BACKUP_ENCRYPTION_KEY (and keep a"
        log "       copy off this server), or set BACKUP_ALLOW_UNENCRYPTED=true if the backup"
        log "       target is itself encrypted."
        exit 1
    fi
    log "WARNING: ******************************************************************"
    log "WARNING: writing UNENCRYPTED backups (BACKUP_ALLOW_UNENCRYPTED=true)."
    log "WARNING: anyone who can read ${BACKUP_DIR} can read client data."
    log "WARNING: ******************************************************************"
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# Encrypt in place when a key is set, write the checksum; echoes the final path.
# Runs inside $(...), where `set -e` does not apply, so every failure is checked
# explicitly: the plaintext is removed only after openssl succeeded, and a
# failure returns non-zero so the caller aborts instead of logging success.
# Messages go to stderr — stdout is the captured path.
finalize() {
    local file="$1"
    if [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]; then
        if ! openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
                -in "$file" -out "${file}.enc" -pass env:BACKUP_ENCRYPTION_KEY; then
            rm -f "${file}.enc"
            chmod 600 "$file" 2>/dev/null || true
            log "ERROR: encrypting $file failed — the UNENCRYPTED archive was left at $file (mode 600);" >&2
            log "       encrypt or delete it by hand, fix the cause, and rerun the backup" >&2
            return 1
        fi
        rm -f "$file"
        file="${file}.enc"
    fi
    chmod 600 "$file"
    # CBC has no built-in integrity check; the checksum is what lets a restore
    # detect a truncated or corrupted archive before anything is dropped.
    echo "$(sha256 "$file")  $(basename "$file")" > "${file}.sha256"
    chmod 600 "${file}.sha256"
    echo "$file"
}

s3_args=()
[ -n "${S3_ENDPOINT:-}" ] && s3_args+=(--endpoint-url "$S3_ENDPOINT")

upload() {
    local file="$1"
    [ -n "${S3_BUCKET:-}" ] || return 0
    local -a args=(${s3_args[@]+"${s3_args[@]}"})
    [ -n "${S3_SSE:-}" ] && args+=(--sse "$S3_SSE")
    local f
    for f in "$file" "${file}.sha256"; do
        aws s3 cp "$f" "s3://${S3_BUCKET}/backups/$(basename "$f")" ${args[@]+"${args[@]}"}
    done
    log "Uploaded to s3://${S3_BUCKET}/backups/$(basename "$file")"
}

# Delete S3 copies older than the retention window (the local find below does
# the same for BACKUP_DIR). Skipped with a warning when `date -d` is missing.
prune_s3() {
    [ -n "${S3_BUCKET:-}" ] || return 0
    local cutoff
    cutoff=$(date -d "-${BACKUP_RETENTION_DAYS} days" +%Y-%m-%d 2>/dev/null) || {
        log "WARNING: cannot compute the retention cutoff (GNU date required) — S3 copies NOT pruned"
        return 0
    }
    local removed=0 day _time _size name
    while read -r day _time _size name; do
        [ -n "${name:-}" ] || continue
        if [[ "$day" < "$cutoff" ]]; then
            aws s3 rm "s3://${S3_BUCKET}/backups/${name}" ${s3_args[@]+"${s3_args[@]}"} >/dev/null
            removed=$((removed + 1))
        fi
    done < <(aws s3 ls "s3://${S3_BUCKET}/backups/" ${s3_args[@]+"${s3_args[@]}"})
    [ "$removed" -gt 0 ] && log "Removed $removed S3 object(s) older than ${BACKUP_RETENTION_DAYS} days"
    return 0
}

# ---------------------------------------------------------------- database
DB_FILE="${BACKUP_DIR}/${POSTGRES_DB}_${TIMESTAMP}.sql.gz"
log "Dumping PostgreSQL database '${POSTGRES_DB}' (${BACKUP_MODE} mode)…"
if [ "$BACKUP_MODE" = "compose" ]; then
    [ -f "$COMPOSE_FILE_PATH" ] || { log "ERROR: $COMPOSE_FILE_PATH not found — run from the deployment directory"; exit 1; }
    # -T: no TTY, so the dump streams cleanly through the pipe.
    compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --no-owner --no-privileges --format=plain | gzip > "$DB_FILE"
else
    : "${POSTGRES_HOST:?POSTGRES_HOST is required in direct mode}"
    : "${PGPASSWORD:?PGPASSWORD is required in direct mode}"
    pg_dump -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --no-owner --no-privileges --format=plain | gzip > "$DB_FILE"
fi
[ -s "$DB_FILE" ] || { log "ERROR: database dump is empty"; exit 1; }
DB_FILE=$(finalize "$DB_FILE") || { log "ERROR: database backup FAILED"; exit 1; }
log "Database backup: $DB_FILE ($(du -h "$DB_FILE" | cut -f1))"
upload "$DB_FILE"

# ------------------------------------------------------------- data volume
DATA_FILE="${BACKUP_DIR}/rag_data_${TIMESTAMP}.tar.gz"
PAUSED="no"
unpause() {
    if [ "$PAUSED" = "yes" ]; then
        compose unpause backend >/dev/null 2>&1 || log "ERROR: could not unpause the backend — run: docker compose -f $COMPOSE_FILE_PATH unpause backend"
        PAUSED="no"
    fi
}
trap unpause EXIT

if [ "$BACKUP_MODE" = "compose" ]; then
    BACKEND_ID=$(compose ps -q backend)
    [ -n "$BACKEND_ID" ] || { log "ERROR: backend container not found"; exit 1; }
    DATA_VOLUME=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' "$BACKEND_ID")
    if [ "$BACKUP_PAUSE_BACKEND" = "true" ] && [ -n "${DATA_VOLUME:-}" ]; then
        log "Pausing backend while the data volume is archived…"
        compose pause backend >/dev/null
        PAUSED="yes"
    fi
elif [ -n "${DATA_VOLUME:-}${DATA_DIR:-}" ]; then
    # Direct mode has no handle on the backend (it may not even run under
    # this compose file), so nothing is paused here.
    log "NOTE: direct mode does not pause the backend — pause or stop it yourself for the"
    log "      duration if the vector index must be captured in a consistent state"
fi
if [ -n "${DATA_VOLUME:-}" ]; then
    log "Archiving data volume '${DATA_VOLUME}'…"
    docker run --rm -v "${DATA_VOLUME}:/data:ro" alpine tar czf - -C /data . > "$DATA_FILE"
elif [ -n "${DATA_DIR:-}" ]; then
    log "Archiving data directory '${DATA_DIR}'…"
    tar czf "$DATA_FILE" -C "$DATA_DIR" .
else
    log "WARNING: no data volume configured (set DATA_VOLUME or DATA_DIR) — documents, vectors and audit logs NOT backed up"
    DATA_FILE=""
fi
unpause
if [ -n "$DATA_FILE" ]; then
    [ -s "$DATA_FILE" ] || { log "ERROR: data archive is empty"; exit 1; }
    DATA_FILE=$(finalize "$DATA_FILE") || { log "ERROR: data backup FAILED"; exit 1; }
    log "Data backup: $DATA_FILE ($(du -h "$DATA_FILE" | cut -f1))"
    upload "$DATA_FILE"
fi

# ---------------------------------------------------------------- retention
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name "*.sql.gz" -o -name "*.sql.gz.enc" -o -name "*.sql.gz*.sha256" \
       -o -name "rag_data_*.tar.gz" -o -name "rag_data_*.tar.gz.enc" -o -name "rag_data_*.tar.gz*.sha256" \) \
    -mtime +"$BACKUP_RETENTION_DAYS" -print -delete | wc -l)
[ "$DELETED" -gt 0 ] && log "Removed $DELETED backup file(s) older than ${BACKUP_RETENTION_DAYS} days"
prune_s3

log "Backup complete. Reminder: SECRET_KEY, ENCRYPTION_SALT and AUDIT_HMAC_KEY are not in"
log "these archives — a restore needs the same values (see the header of this script)."
