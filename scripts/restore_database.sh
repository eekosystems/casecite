#!/bin/bash
# CaseCite restore — PostgreSQL dump and, optionally, the application data volume.
#
# Run on the HOST from the directory that holds docker-compose.prod.yml.
#
# Usage:
#   ./scripts/restore_database.sh <casecite_<ts>.sql.gz[.enc]> [--data <rag_data_<ts>.tar.gz[.enc]>] --yes
#
#   --data   Also restore the /app/data volume (documents, vectors, audit logs,
#            BYOK key store). Use the archive from the SAME backup run.
#   --yes    Required. ALL data in the target database (and volume, with --data)
#            is dropped and replaced.
#
# Encrypted (.enc) backups need BACKUP_ENCRYPTION_KEY in the environment.
# Both archives are verified (checksum + full decrypt/gzip pass) before the
# database is dropped.
#
# The restored instance must run with the SAME SECRET_KEY, ENCRYPTION_SALT and
# AUDIT_HMAC_KEY as the one that was backed up (they are not in the archives):
# otherwise uploaded documents, the BYOK key store, connector tokens and TOTP
# secrets cannot be decrypted and the audit log fails verification.
#
# Modes (BACKUP_MODE, default "compose") mirror backup_database.sh:
#   compose — psql runs inside the postgres container; the backend is stopped
#             for the duration and started again afterwards (it runs Alembic
#             migrations on start, so a dump from an older version is upgraded).
#   direct  — psql on PATH with POSTGRES_HOST/PORT/DB/USER + PGPASSWORD; --data
#             needs DATA_VOLUME (docker volume) or DATA_DIR (host path).

set -euo pipefail

BACKUP_MODE="${BACKUP_MODE:-compose}"
COMPOSE_FILE_PATH="${COMPOSE_FILE_PATH:-docker-compose.prod.yml}"
POSTGRES_DB="${POSTGRES_DB:-casecite}"
POSTGRES_USER="${POSTGRES_USER:-casecite}"

usage() {
    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
    echo
    echo "Available backups in ${BACKUP_DIR:-./backups}:"
    ls -lh "${BACKUP_DIR:-./backups}"/*.sql.gz* "${BACKUP_DIR:-./backups}"/rag_data_* 2>/dev/null || echo "  (none)"
}

DB_BACKUP=""
DATA_BACKUP=""
CONFIRMED="no"
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) CONFIRMED="yes" ;;
        --data) shift; DATA_BACKUP="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *) DB_BACKUP="$1" ;;
    esac
    shift
done

[ -n "$DB_BACKUP" ] || { usage; exit 1; }
[ -f "$DB_BACKUP" ] || { echo "ERROR: backup file not found: $DB_BACKUP"; exit 1; }
if [ -n "$DATA_BACKUP" ] && [ ! -f "$DATA_BACKUP" ]; then
    echo "ERROR: data archive not found: $DATA_BACKUP"; exit 1
fi
if [ "$CONFIRMED" != "yes" ]; then
    echo "ERROR: this REPLACES all data in database '${POSTGRES_DB}'${DATA_BACKUP:+ and the data volume}."
    echo "Re-run with --yes to confirm."
    exit 1
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
compose() { docker compose -f "$COMPOSE_FILE_PATH" "$@"; }

# Stream a backup to stdout, decrypting .enc files first.
read_backup() {
    local file="$1"
    case "$file" in
        *.enc)
            : "${BACKUP_ENCRYPTION_KEY:?BACKUP_ENCRYPTION_KEY is required to read $file}"
            openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -in "$file" -pass env:BACKUP_ENCRYPTION_KEY
            ;;
        *) cat "$file" ;;
    esac
}

# Run a psql command against the maintenance database ("postgres").
psql_admin() {
    if [ "$BACKUP_MODE" = "compose" ]; then
        compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres "$@"
    else
        psql -v ON_ERROR_STOP=1 -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -d postgres "$@"
    fi
}
psql_target() {
    if [ "$BACKUP_MODE" = "compose" ]; then
        compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
    else
        psql -v ON_ERROR_STOP=1 -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
    fi
}

if [ "$BACKUP_MODE" = "direct" ]; then
    : "${POSTGRES_HOST:?POSTGRES_HOST is required in direct mode}"
    : "${PGPASSWORD:?PGPASSWORD is required in direct mode}"
fi

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# Prove an archive is intact and readable BEFORE anything is dropped:
#   1. the .sha256 written by backup_database.sh, when present;
#   2. a full decrypt + gzip integrity pass. The whole stream is read (no
#      `head`), so a large archive cannot fail with a broken pipe under
#      pipefail, and a wrong key shows up as a decrypt or gzip error.
verify_backup() {
    local file="$1"
    if [ -f "${file}.sha256" ]; then
        local expected actual
        expected=$(cut -d' ' -f1 "${file}.sha256")
        actual=$(sha256 "$file")
        [ "$expected" = "$actual" ] || { log "ERROR: checksum mismatch for $file — the archive is corrupted or truncated"; exit 1; }
    else
        log "WARNING: no ${file}.sha256 next to the archive — skipping the checksum"
    fi
    if ! read_backup "$file" | gunzip -t 2>/dev/null; then
        case "$file" in
            *.enc) log "ERROR: could not read $file — wrong BACKUP_ENCRYPTION_KEY, or the archive is corrupted" ;;
            *) log "ERROR: $file is not a readable gzip archive" ;;
        esac
        exit 1
    fi
}

# Everything that can fail is checked before the database is touched.
log "Verifying $DB_BACKUP…"
verify_backup "$DB_BACKUP"
if [ -n "$DATA_BACKUP" ]; then
    log "Verifying $DATA_BACKUP…"
    verify_backup "$DATA_BACKUP"
    if [ "$BACKUP_MODE" = "compose" ]; then
        BACKEND_ID=$(compose ps -aq backend)
        [ -n "$BACKEND_ID" ] || { log "ERROR: backend container not found — cannot locate the data volume"; exit 1; }
        DATA_VOLUME=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' "$BACKEND_ID")
    fi
    if [ -z "${DATA_VOLUME:-}" ] && [ -z "${DATA_DIR:-}" ]; then
        log "ERROR: --data given but no DATA_VOLUME/DATA_DIR to restore into"; exit 1
    fi
fi

if [ "$BACKUP_MODE" = "compose" ]; then
    log "Stopping backend so nothing writes during the restore…"
    compose stop backend
fi

log "Recreating database '${POSTGRES_DB}'…"
# Identifiers are quoted; the database name is also passed as a literal to the
# terminate query so unusual names cannot break the SQL.
psql_admin -v db="$POSTGRES_DB" <<'SQL'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname = :'db' AND pid <> pg_backend_pid();
SQL
psql_admin -c "DROP DATABASE IF EXISTS \"${POSTGRES_DB}\";"
psql_admin -c "CREATE DATABASE \"${POSTGRES_DB}\" OWNER \"${POSTGRES_USER}\";"

log "Restoring database from $DB_BACKUP…"
read_backup "$DB_BACKUP" | gunzip -c | psql_target -q

if [ -n "$DATA_BACKUP" ]; then
    if [ -n "${DATA_VOLUME:-}" ]; then
        log "Restoring data volume '${DATA_VOLUME}' from $DATA_BACKUP…"
        read_backup "$DATA_BACKUP" | docker run --rm -i -v "${DATA_VOLUME}:/data" alpine \
            sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf - -C /data'
    elif [ -n "${DATA_DIR:-}" ]; then
        log "Restoring data directory '${DATA_DIR}' from $DATA_BACKUP…"
        rm -rf "${DATA_DIR:?}"/* "${DATA_DIR:?}"/.[!.]* 2>/dev/null || true
        read_backup "$DATA_BACKUP" | tar xzf - -C "$DATA_DIR"
    fi
fi

if [ "$BACKUP_MODE" = "compose" ]; then
    log "Starting backend (runs pending Alembic migrations on start)…"
    compose start backend
else
    log "Restore complete. Start the application; it applies pending migrations on start (or run 'alembic upgrade head')."
fi
log "Restore complete. Confirm the application is running with the original SECRET_KEY, ENCRYPTION_SALT and AUDIT_HMAC_KEY."
