# Architecture Overview

## System Design

CaseCite is a single-page React app in front of a FastAPI backend, with a
relational database, a vector index, Redis and an encrypted file store behind
it:

```
┌─────────────────────────────────────────────────┐
│                  React SPA                       │
│  Views → Zustand stores → API client             │
├─────────────────────────────────────────────────┤
│        Reverse proxy (Caddy, or nginx in the      │
│        unified image behind a platform proxy)     │
├─────────────────────────────────────────────────┤
│                 Middleware stack                  │
│  CORS → TrustedHost → Security → RequestId → CSRF │
├─────────────────────────────────────────────────┤
│              FastAPI routers                      │
│  HTTP concerns only: auth, validation, response   │
├─────────────────────────────────────────────────┤
│                   Services                        │
│  Business logic, AI orchestration, integrations   │
├─────────────────────────────────────────────────┤
│                  Data layer                       │
│  PostgreSQL │ ChromaDB/Pinecone │ Redis │ Files   │
└─────────────────────────────────────────────────┘
```

The backend runs as **one process**. Sessions, the document registry and the
connector caches are process-local, and the app refuses to start with more
than one worker (see "Scaling Considerations" in [deployment.md](deployment.md)).

## Frontend Architecture

### Data Flow

```
User interaction
  → View (rendering; reads state and handlers from a store)
  → Zustand store (state, handlers, side effects; calls the API layer)
  → API client (HTTP with cookie auth + CSRF header)
  → Backend
```

Provider API keys are never sent from the browser on requests: users save them
once, and the backend stores them encrypted and uses them server-side.

### Key Patterns

- **Views** render. Data fetching, handlers and derived state live in the store for that feature; a view's own effects are lifecycle wiring only.
- **Stores** (Zustand) — one per feature (contracts, drafting, judge intel, tools, documents, …) plus global ones (auth, UI, settings).
- **Hooks** — app-level concerns (`useAppEffects`, `useAppUIState`, `useKeyboardShortcuts`) and `useResearchState`, which holds the research chat's state and is provided through context so it survives navigation.
- **Components** are reusable UI pieces. They receive a parent's CSS module via an `s` prop when they need its classes.
- **API modules** are one-per-domain and attach typed methods to a singleton `api` object.

### Directory Map

| Directory | Purpose |
|-----------|---------|
| `views/` | Page-level components, one per route, lazy-loaded |
| `stores/` | Zustand stores |
| `hooks/` | App-level hooks and `useResearchState` |
| `components/shared/` | Reusable UI components |
| `components/modals/` | Modal dialogs |
| `api/` | Backend API client modules |
| `layout/` | App shell (header, sidebar, right panel, routes) |

Authentication uses httpOnly cookies; the access token is never readable by
page scripts and nothing auth-related is kept in `localStorage`.

## Backend Architecture

### Request Lifecycle

```
HTTP request
  → Middleware stack (host check, rate limit, security headers, CSRF)
  → Router (authentication, permission check, input validation)
  → Service (business logic, AI calls, DB operations)
  → Database / vector index / external API
  → Response
```

### Key Patterns

- **Routers** are thin. They authenticate (`Depends(get_current_user)`), check permissions (`require_permission("area.action")`), validate input (Pydantic), call a service, and return a response.
- **Services** contain the business logic. They never import routers.
- **Ownership is resolved server-side.** Any lookup by id checks that the caller owns the object (or is a member of its matter) and answers 404 otherwise.
- **Route ordering**: literal paths are defined before parameterized catch-alls to prevent shadowing.
- **Error handling**: specific exceptions only (never bare `except:`). `HTTPException` for client errors; production responses carry a reference id, not internals.
- **Async throughout** for database operations, external API calls, and I/O.

### Service Organization

Complex domains are packages; the rest are single modules:

```
services/
  rag/                    # Research chat pipeline
    service.py            # Orchestrator
    search.py             # Vector retrieval + keyword rescoring
    generation.py         # LLM calls
    citations.py          # Citation assembly
    case_law_guard.py     # Flags case names not found in retrieved text
    claim_grounding.py    # Checks claims against the selected files
    intent.py, routing.py # Query classification and routing
    prompts.py, prompt_safety.py
  contract_analysis/      # Analysis, redlines, drafting, long drafting
  clause_intel/           # Clause taxonomy and classification
  judge_intel/            # CourtListener judge data + metrics
  legal_tools/            # Case lookup, dockets, citation check
  connectors/             # Drive, OneDrive, Box, Dropbox, Filevine, Clio; iManage and NetDocuments
                          # are experimental (untested against a live tenant), registered only
                          # with EXPERIMENTAL_CONNECTORS=true
  provider_policy.py      # AI provider allowlist (every embedding and LLM call)
  llm_clients.py          # Provider client construction
  embeddings.py           # Embedding providers
  vectordb.py             # ChromaDB / Pinecone abstraction
  documents.py            # Document registry, upload, delete
  text_extraction.py      # PDF, Office, email, RTF, text
  auth.py, mfa.py, passwords.py, permissions.py
  audit.py                # HMAC-chained audit log
  encryption.py, key_storage.py   # AES-256-GCM at rest
  job_queue.py            # Background jobs
  ...
```

## Security Architecture

### Middleware Stack (outermost first)

1. **CORSMiddleware** — explicit origin list, credentials allowed only for those origins
2. **HTTPSRedirectMiddleware** — only when `FORCE_HTTPS=true` (normally the reverse proxy handles TLS)
3. **TrustedHostMiddleware** — `Host` header allowlist (`ALLOWED_HOSTS` plus loopback for healthchecks)
4. **SecurityMiddleware** — per-IP rate limiting, security headers (CSP, HSTS)
5. **RequestIdMiddleware** — generates or propagates `X-Request-ID`
6. **CSRFMiddleware** — double-submit cookie with HMAC signatures

### Authentication

- **JWT** (HS256, 60-minute access tokens) with rotating refresh tokens and JTI-based revocation in Redis
- **TOTP MFA** with recovery codes; `REQUIRE_MFA` makes it mandatory
- **Azure AD SSO** (RS256, JWKS validation) as a backend token exchange
- **Roles**: admin, attorney, paralegal, viewer, mapped to adjustable permissions
- **Sessions**: max 5 concurrent per user, 8-hour absolute lifetime, 2-hour idle timeout

### Data Protection

- AES-256-GCM at rest for uploaded originals, stored API keys, connector tokens and TOTP secrets (per-record keys via HKDF)
- The vector index, database and Redis hold derived text in cleartext and rely on operator-provided disk encryption
- Tenant isolation: every query is scoped by `user_id` (or matter membership); the vector store refuses unscoped queries
- File upload validation with magic bytes, a type allowlist and a size cap

### Audit Trail

- Authentication, user administration, document, export, connector, settings and security events
- HMAC-SHA256 chain hashing (keyed with `AUDIT_HMAC_KEY`) for tamper detection, verified at startup and on demand
- JSONL, one file per UTC day; compressed after `AUDIT_RETENTION_DAYS`, deleted after twice that
- Correlation IDs for request tracking

## Database Schema

The primary database (PostgreSQL in production, SQLite in development) holds:

| Domain | Tables |
|--------|--------|
| Auth | users, instance_secrets |
| Matters | matters, matter_members |
| Chat & workspace | chat_sessions, chat_messages, workspace_sessions |
| Contract intelligence | contract_analysis_runs, contract_parties, contract_obligations, contract_deadlines, contract_defined_terms, contract_type_requirements |
| Clause intelligence | clause_canonicals, clause_deviations, clause_jurisdiction_rules, clause_tag_findings |
| Branding | branding_config |
| Audit | audit_logs (a queryable mirror; the JSONL files are the record) |

The document registry (`uploads/index.json`), the encrypted key store and the
audit log files live on the data volume, not in the database. The court list
used by jurisdiction pickers is a static module (`services/courts.py`).

## AI Pipeline (research chat)

```
User query
  → Intent detection and routing (documents, case law, or both)
  → Embed the query; vector search scoped to the user's documents
  → Keyword (BM25) rescoring of the retrieved candidates
  → Context assembly (document text passed as delimited, untrusted content)
  → LLM generation (OpenAI / Anthropic / Gemini, the user's or the server's key)
  → Citation assembly, case-law guard, claim grounding against selected files
  → Response with sources and an AI-review notice
```

There is no cross-encoder or API reranker: ranking is vector similarity
blended with a BM25 score computed over the candidates the vector search
returned. A term that appears only in chunks outside those candidates is not
found by the keyword step.

When the provider allowlist is enabled, every embedding call and every LLM call
goes through its check (`services/provider_policy.py`): research chat and its
helper passes, contract analysis, drafting, strategy, clause intelligence,
tabular review, deep research, workflows and case-law research.

### Embedding Providers

| Provider | Model | Dimensions | Use case |
|----------|-------|------------|----------|
| OpenAI | text-embedding-3-small | 1536 | Default |
| Voyage AI | voyage-law-2 | 1024 | Legal-tuned |
| Cohere | embed-english-v3.0 | 1024 | Alternative |
| Self-hosted (`EMBEDDING_BASE_URL`) | any OpenAI-compatible embeddings model | model-dependent | Keeps document text on your network |

Changing the embedding model requires re-indexing existing documents.
