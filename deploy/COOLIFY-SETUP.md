# Deploy CaseCite on Coolify

Coolify builds the unified image (frontend + backend in one container) from the
root `docker-compose.yml` and puts its own Traefik proxy, with TLS, in front of
it. This guide uses the **Docker Compose** build pack on purpose: that compose
file declares the persistent volume for `/app/data`. Deploying the bare
`Dockerfile` instead gives the container an anonymous volume that is replaced on
every redeploy — uploads, the vector index, the audit log and the stored API
keys would be lost each time.

What you need before you start:

- A server running Coolify, and a DNS A record for the hostname you will use
  (e.g. `app.yourfirm.com`) pointing at it.
- The server's disk encrypted (LUKS or the provider's encrypted disk). CaseCite
  stores vector chunks, chat history and extracted text in cleartext and will
  not start until you confirm the disks under the app volume, PostgreSQL and
  Redis are encrypted.
- Optionally an AI provider key. Without a server-level key, each user saves
  their own under Settings.

---

## Step 1: Put the code where Coolify can read it

Either point Coolify at `https://github.com/thomasunise/casecite` directly, or
fork it (a private fork is fine for your own deployment under the license) and
connect the fork through **Sources → GitHub App**.

Do not `git add .` a working directory that contains a `.env`, a `backups/`
directory or a `data/` directory. The repository's `.gitignore` excludes them;
keep it that way.

---

## Step 2: Create PostgreSQL and Redis

The compose file does not include a database or Redis. In your Coolify project:

1. **+ New Resource → Database → PostgreSQL 16.** Note the internal connection
   URL Coolify shows.
2. **+ New Resource → Database → Redis 7.** Set a password. In its custom
   configuration set:

   ```
   maxmemory-policy noeviction
   ```

   Redis holds revoked-token markers, account-lockout counters and consumed MFA
   challenges. An LRU eviction policy drops them under memory pressure, which
   revives logged-out sessions and resets lockouts.

Both must be on the same Docker network as the app (Coolify's default project
network works) and must not be published to the internet.

---

## Step 3: Create the app

1. **+ New Resource → Public Repository** (or **Private Repository (with GitHub
   App)** for a fork) and pick the repository.
2. Build settings:

   | Setting | Value |
   |---|---|
   | Build Pack | **Docker Compose** |
   | Docker Compose Location | `/docker-compose.yml` |

3. Save. Coolify lists the `app` service and its `casecite_data` volume — that
   volume is the persistent storage for `/app/data`. Confirm it appears under
   **Storages** before you deploy.

---

## Step 4: Environment variables

Generate the secrets **once**, locally, and keep a copy in a password manager.
They are not in any backup, and losing `SECRET_KEY` or `ENCRYPTION_SALT` makes
every uploaded document and stored API key unreadable.

```bash
openssl rand -hex 32   # SECRET_KEY
openssl rand -hex 32   # AUDIT_HMAC_KEY
openssl rand -hex 16   # ENCRYPTION_SALT
openssl rand -hex 16   # REGISTRATION_BOOTSTRAP_TOKEN
```

Add these under **Environment Variables**. All are required; the app fails
closed on startup if one is missing or left as a placeholder.

| Variable | Value |
|----------|-------|
| `SECRET_KEY` | generated above (never change after first run) |
| `ENCRYPTION_SALT` | generated above (never change after first run) |
| `AUDIT_HMAC_KEY` | generated above |
| `REGISTRATION_BOOTSTRAP_TOKEN` | generated above — needed to create the first (admin) account |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@<postgres-host>:5432/<db>` (from Step 2; note the `+asyncpg`) |
| `DB_SSL` | `internal` for the Coolify-managed Postgres on the private Docker network; `require` for a database reached over a network |
| `REDIS_URL` | `redis://:password@<redis-host>:6379/0` (from Step 2) |
| `CORS_ORIGINS` | `https://app.yourfirm.com` |
| `ALLOWED_HOSTS` | `app.yourfirm.com` |
| `TRUSTED_PROXIES` | the Docker subnet Coolify's proxy connects from — find it with `docker network inspect coolify -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'` |
| `DISK_ENCRYPTION_ACKNOWLEDGED` | `true`, once the server disk (app volume, Postgres, Redis) is encrypted |

Optional:

| Variable | Value |
|----------|-------|
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | a server-level provider key shared by all users |
| `COURTLISTENER_API_TOKEN` | free token; needed for citation check and usable rate limits on case-law tools |
| `FRONTEND_URL` | `https://app.yourfirm.com` — used in password-reset emails |
| `SENDGRID_API_KEY` | enables password-reset emails |
| `DEMO_LOGIN_EMAIL` / `DEMO_LOGIN_PASSWORD` | **public demo instances only** — see "Public demo instance" in [docs/deployment.md](../docs/deployment.md) |

`TRUSTED_PROXIES` matters: if the proxy's address is not trusted, every user
appears to come from one IP, so one person's failed logins lock out the whole
firm.

---

## Step 5: Domain and deploy

1. On the `app` service set the domain to `https://app.yourfirm.com` (port 80).
   Coolify issues the certificate.
2. Click **Deploy** and watch the log. The container reports healthy once
   database migrations have run and `GET /health` answers — allow a couple of
   minutes on the first deploy.
3. Open `https://app.yourfirm.com/health`. It should return
   `{"status": "healthy", ...}`.

---

## Step 6: Create the admin account

The first account becomes the admin, and in production it can only be created
with the bootstrap token from Step 4:

```bash
DOMAIN=app.yourfirm.com
CSRF=$(curl -s -c /tmp/casecite.jar "https://$DOMAIN/api/v1/csrf-token" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["csrf_token"])')
curl -s -b /tmp/casecite.jar -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"email": "you@yourfirm.com", "name": "Your Name",
       "password": "<12+ chars, upper, lower, number, symbol>",
       "bootstrap_token": "<REGISTRATION_BOOTSTRAP_TOKEN>"}' \
  "https://$DOMAIN/api/v1/auth/register"
rm /tmp/casecite.jar
```

Sign in, enrol MFA, then invite colleagues from Settings → Users.

---

## Backups

Coolify's own database backups cover PostgreSQL only. The `casecite_data`
volume (uploaded documents, vector index, audit log, key store) must be backed
up as well, from the same point in time — see "Backups" in
[docs/deployment.md](../docs/deployment.md) (`BACKUP_MODE=direct` with
`DATA_VOLUME=<the casecite_data volume name>`).

---

## Troubleshooting

**Container restarts or never turns healthy** — open the app's logs. Startup
names the missing or placeholder setting (`SECRET_KEY`, `AUDIT_HMAC_KEY`,
`ENCRYPTION_SALT`, `REDIS_URL`, `DATABASE_URL`, `CORS_ORIGINS`,
`DISK_ENCRYPTION_ACKNOWLEDGED`).

**"Invalid host header"** — `ALLOWED_HOSTS` does not match the hostname in the
browser.

**Everyone is rate-limited or locked out together** — `TRUSTED_PROXIES` does
not cover the proxy's address.

**Signed in, but the session does not stick** — the app must see the original
`https` scheme. Coolify's proxy sends `X-Forwarded-Proto`, and the in-container
nginx passes it through; check that nothing between the browser and Coolify
terminates TLS without forwarding that header.

**Site loads but AI answers fail** — no provider key is configured (server
level or under the user's Settings), or the key is invalid.

**Data disappeared after a redeploy** — the app was deployed with the
Dockerfile build pack instead of Docker Compose (Step 3), so `/app/data` was
not on a persistent volume.

---

## Updating

Push to the repository (or pull upstream into your fork) and redeploy from
Coolify. Migrations run automatically at container start. Take a backup first.
