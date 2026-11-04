# Roadmap

What ships today, what is planned, and — just as important for a firm
evaluating the product — what does **not** exist yet.

## Current
- Research chat over the firm's documents with citations back to the source and a case-law guard
- Deep research: a question planned into sub-questions the user edits, each researched over the firm's documents and CourtListener as one background job, every quote verified against its passage and every citation joined back to a real source in code, with a step log and bounded cost
- Contract analysis, redlining as native Word tracked changes, and long-document drafting
- Tabular review (`/review-tables`): one user-defined field schema (built-in and saved templates) across many documents, each value quote-verified against the source with a source panel, a human review layer (override, approve, flag, note, history) that survives re-runs, filters, typed sorting, bulk actions, run history with change tracking, and exports to Excel (Results, Citations, Review, Summary), CSV or JSON
- Workflows (`/workflows`): a saved, ordered list of the platform's own operations (extract fields, analyse, compare, draft, deep research, citation check) run over a document set as one job, with every step's output persisted, resume from a failed step, and three starter templates
- Word add-in (`word-addin/`): analyze the open document and select each flagged passage, insert suggested rewrites as tracked changes, research the firm's documents with the answer inserted as footnoted paragraphs; signed in with a device code approved in the web app, listed as its own session; built into the image, served at `/addin/`, with the deployment's manifest rendered at `/api/v1/addin/manifest.xml` for Centralized Deployment
- Strategy briefs with quote-verified authorities (API only)
- Judge intelligence, citation check, and case-law tools backed by CourtListener
- Matters: a workspace per client engagement (`/matters`) with the documents, chats, analyses, members and activity filed under it; an active-matter selector that scopes Research, Contracts and Drafting; bulk filing of documents between matters; ownership transfer for offboarding
- Offboarding that cannot destroy client work by accident: deleting a user who owns matters, documents, conversations, analyses, review tables or workflows requires an explicit decision — transfer all of it to a colleague first (documents, index entries and files included) or purge it knowingly; the Users tab shows what they own and offers both
- Legal hold and retention per matter: a hold (owner or admin, with a reason) blocks every delete of the matter and everything filed under it — by members, by a purge of a departing owner, by re-upload replacement, by retention — until lifted; a single document can be held on its own; a retention policy removes documents and conversations older than N days through the normal delete path, daily and audited
- Ethical walls: an admin can wall a user off a matter (with a reason, audited); they lose membership, cannot be added back while the wall stands (the inviter gets the same generic refusal as for an unknown address), their work filed under the matter passes to its owner, and the matter is hidden from them everywhere — including the admin oversight list
- AI usage metering: every provider call is recorded per user with its token counts, model and purpose (`GET /usage/me`, admins `GET /admin/usage`); `SERVER_KEY_DAILY_TOKEN_QUOTA` caps what a user without their own key may spend per day on the instance's keys
- Multi-worker / multi-replica deployments: the document registry lives in the database, sessions in Redis, the permission matrix, legal holds and connector credentials reload when another process changes them, and the audit chain is serialised through a lock file; `UVICORN_WORKERS` above 1 is accepted with PostgreSQL, Redis and a non-embedded vector store (`VECTOR_DB=pinecone`) and refused, naming what is missing, without them
- Connector folder picker: a connected Google Drive, OneDrive, Box or Dropbox account is browsed folder by folder and a sync is scoped to the chosen folder; importing the whole account stays behind an explicit warning (Clio can only sync the whole account)
- Microsoft (Azure AD) sign-in in the web app: a "Sign in with Microsoft" button appears when the server has Azure AD configured, MSAL obtains the ID token in a popup, and the backend exchange mints the local session (roles come from the local account, as before)
- HMAC-chained audit log with startup and on-demand verification, viewable and exportable by admins
- BYOK provider keys and uploaded originals encrypted at rest (AES-256-GCM)
- TOTP MFA (optionally mandatory), password reset and change, session lifetime and idle timeout
- User administration: invite, roles and permissions, deactivate, delete, MFA reset
- OpenAI, Anthropic and Gemini for chat; OpenAI, Voyage, Cohere or a self-hosted server for embeddings

## Next
- In-place `SECRET_KEY` rotation with re-encryption of stored documents
- Benchmark rerankers on the accuracy benchmark (Cohere Rerank and a local cross-encoder are wired in; neither is on by default, and no legal-domain reranker has been tried)
- Grow the accuracy benchmark (`backend/evals/`) from the synthetic starter set to attorney-written questions over real-world documents
- Preserve run-level formatting from the source .docx in redline exports

## Future
- SAML / generic OIDC single sign-on and SCIM provisioning (today: Azure AD only)
- External anchoring of the audit-chain head (object-lock bucket or SIEM forwarding)
- Application-level encryption of the vector index and database text (today these rely on disk encryption)
- Integration with additional court e-filing systems
- Multi-language document support
