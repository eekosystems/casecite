# ADR-001: Tenancy Model

**Status:** Accepted — revised 2026-10-04 to match the code as shipped
**Date:** Recorded 2026-05-03 (the decision predates the record)
**Decision makers:** Core maintainers

## Context

CaseCite is a self-hosted legal research platform used by attorneys and small firms. We needed to decide how to isolate user data — a critical decision for a platform handling privileged legal documents under attorney-client confidentiality obligations.

The main options considered:

1. **Database-per-tenant** — Each customer gets a separate PostgreSQL database
2. **Schema-per-tenant** — Shared database, separate PostgreSQL schemas per customer
3. **Row-level isolation** — Shared database, shared tables, `user_id` column on every row
4. **PostgreSQL RLS (Row-Level Security)** — Database-enforced row policies
5. **Organization-level multi-tenancy** — Shared tables scoped by `organization_id` with team/workspace hierarchy

## Decision

We chose **row-level isolation with application-enforced scoping** (option 3).

Every user-data row carries the owning `user_id`. Isolation is enforced at three layers:

1. **Authentication layer** — `get_current_user()` dependency extracts `user_id` from a verified JWT. It cannot be forged or overridden by the caller.
2. **Service layer** — Every function that touches user data requires an explicit `user_id` parameter and validates ownership before returning data (`if not row or row.user_id != user_id: return None`).
3. **Vector DB layer** — Semantic search queries include a mandatory `user_id` filter. The search function raises `ValueError` if the filter is missing.

System-wide reference data (the clause taxonomy, jurisdiction rules, the cached public judge data) has no `user_id` and is shared across all users.

**Matters** are the one sharing mechanism. A matter has an owner and named members; a document or chat session filed under a matter is visible to its members as well as its owner, and every lookup checks "owner, or member of the row's matter". There is no organization, workspace, or team abstraction beyond that, and no ethical-wall (deny-list) feature: an instance serves one firm, and the Azure AD `tenant_id` field on the user model is SSO metadata only — it is not an isolation boundary.

## Consequences

### Positive

- **Simple and auditable.** Every data path follows the same pattern: extract `user_id` from JWT, pass to service, filter query. A reviewer can trace the isolation chain in minutes.
- **No infrastructure complexity.** One database, one connection pool, one migration path. No tenant provisioning workflow.
- **Explicit deletion.** Deleting a user purges their documents, vectors, stored keys, connector tokens, chats, analyses and owned matters in application code (`routers/users.py`). Only some tables have database-level cascades (chat messages, matter members, workspace sessions), so a new user-data table must be added to that purge path — the database will not do it.
- **Fast onboarding.** New users are a single row insert. No schema creation, no database provisioning, no DNS configuration.
- **Works with both SQLite (dev) and PostgreSQL (prod)** without database-specific isolation features.

### Negative

- **Sharing is coarse.** Matters share documents and chats with all members of the matter; there are no per-document permissions, no ethical walls, and deleting a user deletes the matters they own rather than transferring them.
- **Application-enforced, not database-enforced.** A bug in a service function that omits the `user_id` filter could leak data across users. PostgreSQL RLS would provide defense-in-depth at the database layer.
- **Scaling ceiling.** All users share one set of tables. At very high user counts (10K+), table scans and index pressure increase. Partitioning by `user_id` or sharding would require architectural changes.
- **No per-tenant customization.** Cannot offer different schema versions, data retention policies, or storage locations per customer.

### Migration path

If firm-level multi-tenancy is needed in the future, the path is:
1. Add an `organization_id` column to user-data tables
2. Create an `organizations` table with firm metadata
3. Add `organization_id` to the JWT claims
4. Update service functions to scope by `organization_id` instead of (or in addition to) `user_id`

The current pattern makes this additive — existing `user_id` isolation continues to work during migration.
