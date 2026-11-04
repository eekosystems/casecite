# Deployment Guide

This guide covers the three supported deployment modes for CaseCite.

## Deployment Modes

| Mode | Use Case | Database | Reverse Proxy | Containers |
|------|----------|----------|---------------|------------|
| Development | Local dev | SQLite | None | 1 (backend) |
| Coolify | PaaS hosting | PostgreSQL (external) | Platform-managed | 1 (unified) |
| Production VPS | Self-hosted | PostgreSQL | Caddy (auto-HTTPS) | 4 |

---

## Development

```bash
# 1. Clone and configure
git clone https://github.com/thomasunise/casecite.git && cd casecite
cp .env.example .env
# Edit .env — set one provider key (e.g. OPENAI_API_KEY), or leave them all
# empty and save a personal key under Settings after signing in

# 2. Start backend (Docker)
docker compose -f docker-compose.dev.yml up -d

# 3. Start frontend (hot-reload)
cd frontend && npm install && npm run dev
```

- Frontend: http://localhost:3000
- Backend: http://localhost:8000
- API docs: http://localhost:8000/docs

---

## Coolify / Single Container

The unified `Dockerfile` builds both frontend and backend into one container with nginx serving static files and proxying API requests to uvicorn. It does **not** include PostgreSQL or Redis — provide both (managed services, or containers you run yourself) and terminate TLS in a reverse proxy in front of the container.

Production mode fails closed: every variable below is required, and the secrets must be **generated once and kept** — generating them inline on each `docker run` would make every document, stored API key and audit entry from the previous run unreadable.

```bash
# 1. Generate the secrets ONCE and keep the file (chmod 600; never commit it)
cat > casecite.env <<EOF
SECRET_KEY=$(openssl rand -hex 32)
ENCRYPTION_SALT=$(openssl rand -hex 16)
AUDIT_HMAC_KEY=$(openssl rand -hex 32)
REGISTRATION_BOOTSTRAP_TOKEN=$(openssl rand -hex 16)
DATABASE_URL=postgresql+asyncpg://user:pass@db-host:5432/casecite
DB_SSL=require
REDIS_URL=redis://:password@redis-host:6379/0
CORS_ORIGINS=https://app.yourfirm.com
ALLOWED_HOSTS=app.yourfirm.com
TRUSTED_PROXIES=<IP or CIDR of the reverse proxy in front of this container>
DISK_ENCRYPTION_ACKNOWLEDGED=true
EOF
chmod 600 casecite.env

# 2. Build and run. The named volume holds uploads, the vector index, audit
#    logs and the key store — without it they are lost when the container is
#    replaced.
docker build -t casecite .
docker run -d --name casecite -p 127.0.0.1:8080:80 \
  --env-file casecite.env \
  -v casecite_data:/app/data \
  casecite
```

`DISK_ENCRYPTION_ACKNOWLEDGED=true` is a statement that the disks behind the data volume, the Postgres database and the Redis instance are encrypted — set it only once that is true. `DB_SSL=require` is for a database reached over a network; use `DB_SSL=internal` only for a Postgres on the same isolated Docker network. Redis must run with `maxmemory-policy noeviction` (see `docker-compose.prod.yml` for why).

For Coolify, follow [deploy/COOLIFY-SETUP.md](../deploy/COOLIFY-SETUP.md): it uses the root `docker-compose.yml`, which declares the persistent data volume. The health check endpoint is `GET /health`.

---

## Production VPS (Auto-HTTPS)

### Server setup

Any provider works the same way (Hetzner, DigitalOcean, Linode, Vultr,
on-prem). Minimum 2 vCPU / 4 GB RAM, Ubuntu 22.04+ LTS, public IPv4, your SSH
key installed. Point a DNS A record (`casecite.yourfirm.com → SERVER_IP`) at it
before starting the stack so Caddy can obtain a certificate.

```bash
ssh root@SERVER_IP
curl -fsSL https://get.docker.com | sh          # Docker + Compose v2
apt-get install -y git
git clone https://github.com/thomasunise/casecite.git /opt/casecite
cd /opt/casecite
```

Before going live, have to hand: the domain and DNS control; each provider
API key the firm will use (the install brings its own keys); for SSO, the
Azure AD tenant ID and app registration; for connectors, the OAuth client
credentials of each service; the firm's data- and audit-retention policy; and
host-level encryption on the disk that holds Docker's volumes.
`DISK_ENCRYPTION_ACKNOWLEDGED` is a promise you are making about **three**
volumes, not one: `rag_data` (uploads, vector index, audit logs),
`postgres_data` (chat history, analyses, clause text) and `redis_data` (job
results). Encrypting `/var/lib/docker` (LUKS, or the provider's encrypted
disk) covers all three.

```bash
# 1. Generate .env with fresh secrets (written with mode 0600; refuses to
#    overwrite an existing .env)
python3 scripts/setup_production.py --create-env --domain app.yourfirm.com
# Review .env. Once the disk is encrypted, set DISK_ENCRYPTION_ACKNOWLEDGED=true
# (or pass --disk-encrypted above).

# 2. Validate configuration
python3 scripts/setup_production.py --validate

# 3. Start all services
docker compose -f docker-compose.prod.yml up -d
```

Copy `SECRET_KEY`, `ENCRYPTION_SALT` and `AUDIT_HMAC_KEY` from `.env` into a
password manager or secrets vault now. They are not in any backup, and a
restore without them cannot decrypt the documents, the stored API keys or the
MFA secrets.

### Create the admin account

The first account registered on a fresh instance becomes the admin. In
production that registration is refused unless it carries the
`REGISTRATION_BOOTSTRAP_TOKEN` from `.env`, so nobody else who reaches the new
instance can claim the admin role:

```bash
DOMAIN=app.yourfirm.com
CSRF=$(curl -s -c /tmp/casecite.jar "https://$DOMAIN/api/v1/csrf-token" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["csrf_token"])')
curl -s -b /tmp/casecite.jar -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"email": "you@yourfirm.com", "name": "Your Name",
       "password": "<12+ chars, upper, lower, number, symbol>",
       "bootstrap_token": "<REGISTRATION_BOOTSTRAP_TOKEN from .env>"}' \
  "https://$DOMAIN/api/v1/auth/register"
rm /tmp/casecite.jar
```

Then sign in at `https://$DOMAIN`, enrol MFA, and invite the rest of the firm
from Settings → Users. Self-service registration stays off
(`ALLOW_REGISTRATION=false`) unless you turn it on.

This starts 5 services:
- **caddy** — Reverse proxy with automatic Let's Encrypt certificates
- **postgres** — PostgreSQL 16 database
- **redis** — Redis 7 for sessions, rate limiting, and token revocation
- **backend** — Application server (nginx + uvicorn)
- **frontend-build** — one-shot publisher: copies the SPA out of the backend
  image into the volume Caddy serves, then exits (it shows as "Exited" in
  `docker compose ps`; that is normal)

### DNS Setup

Point your domain's A record to your server's IP. Caddy handles certificate provisioning automatically once DNS resolves.

Set the `DOMAIN` environment variable in `.env`:
```
DOMAIN=app.yourfirm.com
```

### Resource Requirements

- **Minimum**: 2 CPU, 4 GB RAM, 20 GB SSD
- **Recommended**: 4 CPU, 8 GB RAM, 50 GB SSD (for larger document corpora)

---

## Word Add-in

The task-pane add-in in `word-addin/` ships with the image: it is built into
the container and served at `https://<your-site>/addin/` by nginx (single
container) or Caddy (bundled stack), on the same origin as the web app, so
there is nothing separate to host and no CORS entry to add.

What Word needs is the manifest, and the manifest must name your origin, so
the running instance renders it:

```
https://casecite.yourfirm.com/api/v1/addin/manifest.xml
```

Publish that once through Centralized Deployment (Microsoft 365 admin center →
*Settings → Integrated apps → Upload custom apps* → paste the URL, or upload
the file it serves) and choose the users or groups. Word picks it up within a
day. Set `FRONTEND_URL=https://casecite.yourfirm.com` so the manifest and the
pane's approval links are absolute regardless of how the request reached the
backend. Optional: `WORD_ADDIN_ID=<new GUID>` gives a second, independent
deployment its own identity in the tenant's catalog.

Hosting the pane elsewhere still works: build it, host `dist/`, and set
`WORD_ADDIN_ORIGIN=https://addin.yourfirm.com` — the manifest then points
there and the origin is added to the CORS allow list.

Optional: `DEVICE_CODE_TTL` (seconds a pending sign-in code lives, default
600) and `DEVICE_CODE_INTERVAL` (minimum seconds between polls, default 5).
Pending codes are kept in Redis under a hash of the code, so a restart does
not lose them; with no Redis they live in process memory.

Build and publish steps, including Centralized Deployment through the
Microsoft 365 admin center, are in [`word-addin/README.md`](../word-addin/README.md).
Each add-in sign-in appears in the web app under Settings → Security →
Sessions as "Word add-in" and can be ended there; "log out everywhere" and an
admin force sign-out end it too.

## Scanned PDFs (OCR)

Image-only PDFs — scans of filings, signed agreements, historical records —
have no text layer. The Docker image OCRs them by default: `pypdfium2`
renders each sparse page and Tesseract (installed in the image with the
English pack) reads it, up to `EXTRACTION_MAX_OCR_PAGES` pages per document.
`/health` (admin view) reports `ocr.available`, the rasterizer and the
Tesseract version, and the startup log says the same. When OCR is
unavailable, a scanned upload is listed as failed with the reason and the
document carries `needs_ocr`, so the file is never silently indexed as empty.

- Other languages: add the pack to the image (`tesseract-ocr-deu`, ...) or
  install it on the host for a bare-metal run.
- Bare metal: `apt-get install tesseract-ocr tesseract-ocr-eng`; the Python
  side (`pypdfium2`, `pytesseract`) is in `requirements.txt`.
- PyMuPDF is AGPL and therefore not bundled; if you install it yourself it is
  used as the rasterizer instead of pypdfium2.

## Health Checks

All deployment modes expose these endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Application health (returns `{"status": "healthy"}`; per-component detail only for an authenticated admin) |
| `GET /ready` | Readiness check (database connectivity) |
| `GET /metrics` | Basic metrics (uptime, request counts) — requires an admin bearer token in production |

The container healthchecks probe these over loopback (`http://localhost/...`).
`localhost` and `127.0.0.1` are always accepted as `Host` values for that
reason, in addition to whatever `ALLOWED_HOSTS` lists.

---

## Scaling Considerations

**One process by default; several when the shared footing is in place.**
Before migration 024 the session store, the document registry and the connector
credential caches were process-local, and the audit hash chain was continued
from each process's memory, so the app refused `UVICORN_WORKERS` above 1 and
held an exclusive lock on `data/.instance.lock`. They are shared now:

| Store | Where it lives now | How other processes see a change |
|-------|--------------------|----------------------------------|
| Document registry | `document_registry` / `document_folders` tables (migration 024; `index.json` imported once and kept as `index.json.migrated-*`) | A `shared_versions` counter, checked at most twice a second; a three-way merge keeps a process's unsaved edit |
| Session store | Redis (`sessions:<user>`; one JSON list per user, expiring with its longest session) | Every validation reads it; a terminate on one worker ends the session everywhere |
| Role permission matrix, legal holds | The database, as before | `shared_versions` counters bumped on change (the hold counter in the same transaction as the hold) |
| Connector credentials | Encrypted files on the data volume, as before | The cache reloads when the file's modification time changed |
| Audit chain | Daily JSONL files on the data volume, as before | Each append takes `audit_logs/.chain.lock` and re-reads the last hash from the file tail under it |

What remains is the infrastructure those rely on. `UVICORN_WORKERS` above 1
(read by `start.sh`) is accepted only when all three hold, and refused with
the missing ones named otherwise:

- a PostgreSQL `DATABASE_URL` (SQLite is single-process);
- `REDIS_URL` (sessions, token revocations, lockouts and rate limits);
- `VECTOR_DB=pinecone` (the embedded Chroma store is opened by one process
  at a time; `PINECONE_API_KEY`, `PINECONE_ENVIRONMENT`, `PINECONE_INDEX_NAME`).

Replicas must share the data volume (`rag_data`): the encrypted originals,
connector credentials and audit logs are files on it. In multi-process mode
each worker takes a *shared* lock on `data/.instance.lock`; a single-process
start against the same volume still takes it exclusively, so the two modes
never mix. Put a load balancer in front of the replicas; nothing is sticky.

Background work (connector syncs, deep research, workflows, retention) runs
in whichever worker accepted the job; job state is in Redis, so another
worker can report it.

---

## Rollback Procedure

### Application (image tag)

`docker-compose.prod.yml` runs `CASECITE_IMAGE:CASECITE_TAG` (default: the
image CI publishes to GHCR, tagged with each commit's full SHA and `latest`).
Rolling back is choosing an earlier tag — the frontend is copied out of the
same image, so it rolls back with the backend.

```bash
# 1. Find the tag you were running before (the CD workflow records it in .env;
#    otherwise: docker images ghcr.io/thomasunise/casecite)
grep CASECITE_TAG .env

# 2. Start the previous build. Postgres, Redis and the data volume are untouched.
CASECITE_TAG=<previous-sha> docker compose -f docker-compose.prod.yml pull backend frontend-build
CASECITE_TAG=<previous-sha> docker compose -f docker-compose.prod.yml up -d

# 3. Make it stick for later `up -d` runs
sed -i 's/^CASECITE_TAG=.*/CASECITE_TAG=<previous-sha>/' .env

# 4. Verify
curl -sf https://app.yourfirm.com/health
```

If the release you are leaving added a database migration, decide whether to
keep it (the older code ignores tables and columns it does not know about —
usually fine) or roll it back too (below, **before** starting the old image).
When in doubt, restore the pre-deploy backup instead.

### Row-level security needs a non-superuser app role

Migration 023 enables and **forces** PostgreSQL row-level security on the
user-content tables (`app/rls.py`, SECURITY.md): a query that forgets its
user filter still returns only the caller's rows. PostgreSQL exempts
**superusers** from it, and the bundled stack's `POSTGRES_USER` is one. The
app logs an error at startup and `/health` reports
`row_level_security.enforced: false` (admin view) while it connects as a
superuser.

**Fresh install:** set `APP_DB_USER=casecite_app` and an `APP_DB_PASSWORD` in
`.env` before the first `up`. `deploy/postgres-init/10-app-role.sh` creates
that non-superuser role as owner of the database on first start, and the app
connects as it. Alembic runs as the app role, so it creates and owns every
table and the policies apply to the app.

**Existing install** (the data volume already exists, so the init script
does not run): create the role and hand it the schema, then set the two
variables and restart.

```sh
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U "${POSTGRES_USER:-casecite}" -d casecite -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE casecite_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '<APP_DB_PASSWORD>';
GRANT CONNECT ON DATABASE casecite TO casecite_app;
ALTER DATABASE casecite OWNER TO casecite_app;
ALTER SCHEMA public OWNER TO casecite_app;
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO casecite_app', r.tablename);
  END LOOP;
  FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO casecite_app', r.sequencename);
  END LOOP;
  FOR r IN SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
           FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'public' LOOP
    EXECUTE format('ALTER FUNCTION public.%I(%s) OWNER TO casecite_app', r.proname, r.args);
  END LOOP;
END $$;
SQL
```

(`REASSIGN OWNED BY` cannot be used for the bootstrap superuser, hence the
loop.) Backups keep using `POSTGRES_USER`.

Two more consequences:

- FORCE means even the table owner is subject to the policies, which is the
  point; never run the app as a superuser or a role with `BYPASSRLS`.
- Ad-hoc SQL run in a session with no `app.user_id` set sees every row
  (unscoped), exactly as before 023. To look at the data as a particular
  user, `SET app.user_id = '<id>'` first.

### Database migration


```bash
# Roll back the most recent migration (runs inside the backend container)
docker compose -f docker-compose.prod.yml exec -u appuser backend alembic downgrade -1

# Or to a specific revision
docker compose -f docker-compose.prod.yml exec -u appuser backend alembic downgrade <revision_id>
```

### Backup before every deploy

```bash
./scripts/backup_database.sh                                    # database + data volume
BACKUP_ENCRYPTION_KEY=… ./scripts/restore_database.sh \         # if the deploy goes wrong
    backups/casecite_<ts>.sql.gz.enc --data backups/rag_data_<ts>.tar.gz.enc --yes
```

---

## Backups

**The restore is exercised on every push.** CI's "Backup and Restore Drill"
job boots the app in production mode, registers the first admin, backs up the
database and the data directory with `scripts/backup_database.sh`, wipes the
directory and deletes the users, restores both with
`scripts/restore_database.sh`, boots again and signs in as that admin — so a
change to the app, the schema or the scripts that would break a recovery
fails the build rather than a real recovery.

`scripts/backup_database.sh` runs on the **host**, from the directory that
holds `docker-compose.prod.yml` (the backend image ships neither `pg_dump` nor
the script). It produces two files per run, encrypts both with
`BACKUP_ENCRYPTION_KEY`, and writes a `.sha256` checksum next to each. It
**refuses to run without `BACKUP_ENCRYPTION_KEY`** — the archives contain
client text, chat history and password hashes — unless you set
`BACKUP_ALLOW_UNENCRYPTED=true` because the backup target is itself encrypted:

| File | Contents |
|---|---|
| `casecite_<ts>.sql.gz.enc` | PostgreSQL: users, matters, chat history, analyses, audit-log mirror |
| `rag_data_<ts>.tar.gz.enc` | the `/app/data` volume: uploaded documents (encrypted at rest), ChromaDB vector index, audit-log chain, BYOK key store, sessions, instance secrets |

(Only with `BACKUP_ALLOW_UNENCRYPTED=true` are the files written without the
`.enc` suffix.)

```bash
# Daily at 02:00 — put BACKUP_ENCRYPTION_KEY (and S3_* if used) in the cron
# environment or a file sourced by it, not on the command line.
0 2 * * * cd /opt/casecite && ./scripts/backup_database.sh >> /var/log/casecite-backup.log 2>&1
```

Keep `BACKUP_ENCRYPTION_KEY` somewhere other than this server (password
manager, KMS). Without it the `.enc` files are unrecoverable, and with it on
the same disk the encryption protects nothing. Do not put it in the
deployment's `.env`: that file is injected into the backend container.

**What a backup does not contain.** `.env` is not backed up. `SECRET_KEY`,
`ENCRYPTION_SALT` and `AUDIT_HMAC_KEY` must be escrowed separately (password
manager, secrets vault): without the first two, a restored instance cannot
decrypt uploaded documents, stored API keys, connector tokens or MFA secrets;
without the third, the restored audit log fails verification at startup.

In the default compose mode the backend container is paused while the data
volume is archived (requests wait) so the vector index is captured in a
consistent state; set `BACKUP_PAUSE_BACKEND=false` to skip that. `direct` mode
(below) never pauses anything — the script has no handle on the backend there —
so pause or stop it yourself for the duration if that consistency matters to
you. Local files and S3 copies older
than `BACKUP_RETENTION_DAYS` (default 30) are deleted. Content a user deletes
therefore remains recoverable from backups for up to that long — set the
retention to match the firm's deletion policy.

Managed/external Postgres: set `BACKUP_MODE=direct` with `POSTGRES_HOST`,
`POSTGRES_USER`, `PGPASSWORD` and `pg_dump` on the host, plus `DATA_VOLUME`
(docker volume name) or `DATA_DIR` (host path) so the data volume is still
archived.

**Restore** — always from the same run, otherwise document records and their
vectors drift out of sync. The script first verifies both archives (checksum,
then a full decrypt and integrity pass) and only then stops the backend,
recreates the database, restores the dump and (with `--data`) the volume, and
starts the backend, which applies any pending migrations. The instance must be
running with the original `SECRET_KEY`, `ENCRYPTION_SALT` and `AUDIT_HMAC_KEY`:

```bash
BACKUP_ENCRYPTION_KEY=… ./scripts/restore_database.sh \
    backups/casecite_<ts>.sql.gz.enc --data backups/rag_data_<ts>.tar.gz.enc --yes
```

Test a restore onto a scratch host at least quarterly; a backup that has
never been restored is a hope, not a backup.

---

## TLS on Internal Networks

Firms deploying on a LAN or VPN without public DNS can't use Let's Encrypt.
Three options, each a small `Caddyfile` change inside the site block:

**Option 1 — Caddy's internal CA (zero config, self-signed):**

```caddy
{$DOMAIN:localhost} {
    tls internal
    # ... rest of the site block unchanged
}
```

Caddy creates a local root CA and issues certificates from it. Browsers warn
until you install that root CA on client machines; export it from the caddy
container at `/data/caddy/pki/authorities/local/root.crt`. This is also what
happens automatically for a `DOMAIN=localhost` trial — the certificate warning
on first visit is expected.

**Option 2 — bring your own certificate (corporate CA or purchased):**

```caddy
{$DOMAIN} {
    tls /etc/caddy/certs/casecite.crt /etc/caddy/certs/casecite.key
    # ... rest of the site block unchanged
}
```

Mount the files in `docker-compose.prod.yml` under the `caddy` service:

```yaml
    volumes:
      - ./certs:/etc/caddy/certs:ro
```

**Option 3 — internal ACME server (e.g. smallstep, corporate ACME):**

```caddy
{$DOMAIN} {
    tls {
        ca https://ca.internal.example.com/acme/acme/directory
    }
}
```

---

## Troubleshooting

**The app won't start** — `docker compose -f docker-compose.prod.yml logs backend`.
Production fails closed on purpose: the log names the missing or placeholder
setting (`SECRET_KEY`, `AUDIT_HMAC_KEY`, `ENCRYPTION_SALT`, `POSTGRES_PASSWORD`,
`REDIS_PASSWORD`, `DISK_ENCRYPTION_ACKNOWLEDGED`, `CORS_ORIGINS`). A provider
key is not required to boot — users can each save their own under Settings. An
`AUDIT ENTRY HMAC MISMATCH` at boot means the audit chain failed verification —
see `docs/secret-rotation.md` before overriding it.

**No certificate / browser warning** — Caddy needs DNS to resolve to this
server and ports 80/443 reachable from the internet. `docker compose -f
docker-compose.prod.yml logs caddy` shows the ACME exchange. On a LAN with no
public DNS, see "TLS on Internal Networks" above.

**Users see 429 or get locked out together** — `TRUSTED_PROXIES` does not
match the proxy in front of the backend, so every client resolves to one IP.
The bundled stack pins it; behind Coolify/Traefik set it to the proxy's subnet.

**Out of memory** — `docker stats`; the backend is capped at 4 GB in the
compose file. OCR and large PDF extraction are the usual culprits; raise the
limit or the VPS size.

---

## Monitoring

### Logs

```bash
# All services
docker compose -f docker-compose.prod.yml logs -f

# Backend only
docker compose -f docker-compose.prod.yml logs -f backend

# Caddy access logs
docker compose -f docker-compose.prod.yml exec caddy cat /data/access.log
```

### Audit Logs

Audit logs are stored in `/app/data/audit_logs/` inside the backend container, one `audit_YYYY-MM-DD.jsonl` file per UTC day. Each entry carries an HMAC-SHA256 hash chained to the previous entry, so an edited, removed or reordered entry is detectable. Admins can also read and verify the log in the product (Settings → Audit Log) or over the API (`GET /api/v1/admin/audit/logs`, `GET /api/v1/admin/audit/verify`).

```bash
# View today's audit log
docker compose -f docker-compose.prod.yml exec backend \
  sh -c 'head -20 /app/data/audit_logs/audit_$(date -u +%Y-%m-%d).jsonl'
```
