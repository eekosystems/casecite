# Security Policy

## Supported Versions

Security fixes are made on `main` and shipped in the next tagged release. Only
the latest release is supported; there are no back-ports to older versions.

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No — upgrade to the latest |

## Reporting a Vulnerability

Please report vulnerabilities privately, not in a public issue or pull request.

1. Use GitHub's private vulnerability reporting for this repository:
   <https://github.com/thomasunise/casecite/security/advisories/new>
2. Include a description, the affected version or commit, steps to reproduce,
   the impact you expect, and a suggested fix if you have one.
3. You will get an acknowledgement in the advisory thread. The project is
   maintained by a small team, so allow a few business days; the aim is an
   initial assessment within a week.
4. Please give us the chance to ship a fix before disclosing publicly. You will
   be credited in the advisory unless you ask not to be.

If you operate a CaseCite instance and suspect it has been compromised, that is
an incident in *your* deployment — follow [docs/incident-response.md](docs/incident-response.md).

## Security Architecture

This section describes what the code does. It is not a certification: CaseCite
is self-hosted software, and the operator is responsible for the host, the
network, disk encryption, backups and the firm's own policies. No SOC 2, ISO
27001 or HIPAA attestation is claimed or implied.

### Authentication
- Email + password with PBKDF2-SHA256 (600,000 iterations, per-password salt).
  Passwords must be 12+ characters with upper, lower, number and symbol.
- TOTP multi-factor authentication with recovery codes. Optional per user by
  default; `REQUIRE_MFA=true` makes it mandatory for every password account. An
  admin can reset a user's MFA (lost device); the user's sessions are ended.
  Disabling MFA requires the account password as well as a valid code.
- Accounts created by an admin get a temporary password and must change it at
  first sign-in.
- Azure AD single sign-on (RS256, JWKS validation, tenant-pinned) is available
  as a backend token exchange. There is no SAML, generic OIDC or SCIM support.
- JWT access tokens (HS256, 60-minute expiry) and rotating refresh tokens,
  delivered in httpOnly, `SameSite=Strict` cookies.
- Token revocation by JTI in Redis (required in production). "Sign out
  everywhere", password change, password reset, role change and deactivation
  invalidate every outstanding access and refresh token for the account.
- Sessions: at most 5 concurrent per user, an absolute lifetime of 8 hours
  (`SESSION_ABSOLUTE_TIMEOUT_HOURS`) and an idle timeout of 2 hours
  (`SESSION_IDLE_TIMEOUT_MINUTES`). Refreshing a token does not extend the
  absolute lifetime.
- Account lockout after 5 failed attempts in 15 minutes, with backoff.
- Session IP binding is available (`ENFORCE_SESSION_IP_BINDING`) and off by
  default.

### Authorization
- Four roles (admin, attorney, paralegal, viewer) mapped to permissions that an
  admin can adjust per role. Admin-only operations are gated by
  `require_permission("admin.*")`.
- The permission matrix fails closed. Every successful read of the saved
  matrix is mirrored to an encrypted last-known-good snapshot on the data
  volume. If the saved matrix cannot be read, decrypted or parsed, the
  snapshot is enforced; with no snapshot, every role is reduced to its locked
  permissions (admins keep user management so they can repair it). The shipped
  defaults are never restored silently. `/health` reports the policy source
  and the Users tab shows an alert while a degraded source is in force.
- Every document, chat, analysis and job is scoped to its owner (or to the
  members of the matter it is filed under). The vector store refuses a query
  that is not pinned to named tenants: every branch of an `$or` must carry a
  `user_id` or `matter_id` equality, and the server builds that scope and
  ANDs it outside any optional condition.
- On PostgreSQL, row-level security is a second fence (migration 024,
  `app/rls.py`): every transaction carries the authenticated user's id, and
  forced policies on the content tables (conversations, analyses, review
  tables, workflows, deep research runs, workspace sessions and their child
  rows) admit a row only when the caller owns it or belongs to its matter
  and is not walled off. Endpoints gated by an `admin.*` permission lift the
  fence, and so does system work that must see every matter (the retention
  task, data migrations), each with a logged reason; a session that merely
  has no user set sees nothing, so a code path that forgets to say who is
  asking gets no rows rather than all of them. A query that forgets its
  user filter still cannot return another user's rows. PostgreSQL exempts superusers from RLS, so in
  production the app refuses to start unless its database role has the
  fence in force. SQLite has no RLS.
- A workflow run re-checks the runner's matter membership (walls
  subtracted) and account status before every step and stops, as failed
  with the reason, when either is gone.
- Ethical walls: an admin can wall a user off a matter
  (`POST /matters/{id}/walls`). The wall removes their membership, refuses to
  add them back (with the same generic answer as an unknown address, so the
  wall is not disclosed to the inviter), hands any work they filed under the
  matter to its owner, and is subtracted from every membership lookup, so a
  membership row created around it grants nothing. A walled admin does not
  see the matter in the oversight list either. There are no per-document
  ACLs. See [ROADMAP.md](ROADMAP.md).

### Deletion
- Deleting a document is a tracked operation: search index entries, then the
  analysis rows derived from it, then the encrypted original. The document is
  reported deleted only when all three are gone. A step that fails leaves the
  document listed with status `delete_failed`, the step and error recorded;
  deleting it again resumes at that step, and admins can list and retry every
  failed deletion (`GET /documents/failed-deletions`,
  `POST /documents/{id}/retry-delete`).
- Deleting a user stops, with the account intact and signed out everywhere,
  if any of their documents could not be removed.
- Deleting a user who owns work product (shared matters, documents,
  conversations, analyses, review tables, saved schemas, workflows) is refused
  until the admin decides: `mode=transfer&transfer_to=<user>` hands all of it
  to an active colleague first (index tags and stored files included),
  `mode=purge` destroys it. Personal data (API keys, connector credentials,
  settings) is never transferred. Both paths are audited with what moved.
- The document registry never fails silently: a write that fails is a 503 and
  the operation rolls back; a registry that cannot be read at startup is fatal
  rather than replaced by an empty one.
- Legal hold: a matter's owner or an admin can place a hold (with a reason,
  audited). While held, the matter and everything filed under it — documents,
  conversations — cannot be deleted through any path: a member's delete, a
  purge of a departing owner's account, replacement by re-upload, or
  retention. A single document can be held on its own. Holds are enforced in
  the services that delete, so new delete paths inherit the check.
- Retention: a matter can keep documents and conversations for N days (7 to
  30 years); a daily run removes older content through the normal delete
  path and audits each removal as `retention_expired`. A hold suspends it.

### Data Protection
- Application-layer AES-256-GCM encryption for uploaded document originals,
  stored API keys (BYOK), connector OAuth tokens and TOTP secrets, with
  per-record keys derived via HKDF.
- **Not** application-encrypted: the vector index (document text chunks), the
  database (chat history, analyses, extracted clause text) and Redis (job
  results). These rely on disk encryption that the operator provides; the app
  refuses to start in production until `DISK_ENCRYPTION_ACKNOWLEDGED=true`.
- BYOK keys are stored server-side, shown to users only in masked form, and
  never returned to the browser.
- Document text and questions are sent to the AI providers the operator
  configures. See [docs/subprocessors.md](docs/subprocessors.md) for every
  outbound data flow and how to keep inference on your own network.
- `SECRET_KEY` cannot be rotated without re-encrypting or re-uploading stored
  documents; see [docs/secret-rotation.md](docs/secret-rotation.md).

### Web Security
- CSRF protection via double-submit cookies with HMAC signatures
- Content Security Policy (CSP) headers
- HSTS with 1-year max-age
- X-Frame-Options, X-Content-Type-Options, Permissions-Policy
- Host-header allowlist (`ALLOWED_HOSTS`) and an explicit CORS origin list

### Rate Limiting
Per client IP, resolved through `TRUSTED_PROXIES`:
- Default: 100 requests/minute
- Chat: 20 requests/minute
- Login: 3 attempts per 5 minutes
- Registration: 3 per 5 minutes
- MFA verify / enable / disable: 5 per 5 minutes

### Deployment Recommendations
- `REGISTRATION_BOOTSTRAP_TOKEN` is required in production to create the first
  (admin) account, so an attacker who finds a fresh instance cannot claim it.
- Keep `ALLOW_REGISTRATION=false` (the default) unless the instance runs on a
  trusted network.
- Set `TRUSTED_PROXIES` to the reverse proxy's address so rate limits, lockout
  and audit entries see real client IPs.
- Run exactly one backend process (the app enforces this) and run Redis with
  `maxmemory-policy noeviction`.
- Encrypt backups and keep `BACKUP_ENCRYPTION_KEY`, `SECRET_KEY`,
  `ENCRYPTION_SALT` and `AUDIT_HMAC_KEY` off the server.

### Audit Trail
- 43 event types covering authentication, user administration, documents,
  exports, matter membership, connectors, settings and security events are
  recorded as JSONL, one file per UTC day. Admins can read, verify and export
  the log (Settings → Audit Log; `GET /api/v1/admin/audit/export`).
- By default a failed audit write is logged at CRITICAL and the request
  proceeds; `AUDIT_FAIL_CLOSED=true` fails the request instead, so nothing
  happens without an audit record.
- Each entry carries an HMAC-SHA256 hash (keyed with `AUDIT_HMAC_KEY`) chained
  to the previous entry. The chain is verified at startup and on demand
  (`GET /api/v1/admin/audit/verify`); in production the app refuses to start if
  verification fails.
- The key lives on the same host as the log, so the chain detects edits,
  deletions and reordering by anyone without the key, not by a host
  administrator. Forward the log to external storage if you need that
  (not built in — see [ROADMAP.md](ROADMAP.md)).
- Retention: files are compressed after `AUDIT_RETENTION_DAYS` (default 90) and
  deleted after twice that. Raise it if your policy requires longer.

## Dependencies

- Python dependencies are installed from `backend/requirements.lock`, an exact,
  hash-pinned lock generated from `backend/requirements.txt`.
- CI runs `pip-audit` against that lock, `npm audit` on production frontend
  dependencies, Bandit, gitleaks, CodeQL, and a blocking Trivy scan of the
  built image. Dependabot opens update PRs weekly.
- Advisories that are knowingly accepted are listed, with the reason, in the
  `pip-audit` step of `.github/workflows/ci.yml` and in `.trivyignore`.
