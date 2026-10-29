# Roadmap

What ships today, what is planned, and — just as important for a firm
evaluating the product — what does **not** exist yet.

## Current
- Research chat over the firm's documents with citations back to the source and a case-law guard
- Deep research: a question planned into sub-questions the user edits, each researched over the firm's documents and CourtListener as one background job, every quote verified against its passage and every citation joined back to a real source in code, with a step log and bounded cost
- Contract analysis, redlining as native Word tracked changes, and long-document drafting
- Tabular review (`/review-tables`): one user-defined field schema (built-in and saved templates) across many documents, each value quote-verified against the source with a source panel, a human review layer (override, approve, flag, note, history) that survives re-runs, filters, typed sorting, bulk actions, run history with change tracking, and exports to Excel (Results, Citations, Review, Summary), CSV or JSON
- Workflows (`/workflows`): a saved, ordered list of the platform's own operations (extract fields, analyse, compare, draft, deep research, citation check) run over a document set as one job, with every step's output persisted, resume from a failed step, and three starter templates
- Word add-in (`word-addin/`): analyze the open document and select each flagged passage, insert suggested rewrites as tracked changes, research the firm's documents with the answer inserted as footnoted paragraphs; signed in with a device code approved in the web app, listed as its own session
- Strategy briefs with quote-verified authorities (API only)
- Judge intelligence, citation check, and case-law tools backed by CourtListener
- Matters: a workspace per client engagement (`/matters`) with the documents, chats, analyses, members and activity filed under it; an active-matter selector that scopes Research, Contracts and Drafting; bulk filing of documents between matters; ownership transfer for offboarding
- HMAC-chained audit log with startup and on-demand verification, viewable and exportable by admins
- BYOK provider keys and uploaded originals encrypted at rest (AES-256-GCM)
- TOTP MFA (optionally mandatory), password reset and change, session lifetime and idle timeout
- User administration: invite, roles and permissions, deactivate, delete, MFA reset
- OpenAI, Anthropic and Gemini for chat; OpenAI, Voyage, Cohere or a self-hosted server for embeddings

## Next
- Connector folder picker (the sync API takes a folder; the UI can only import a whole connected account)
- Azure AD SSO sign-in flow in the frontend (the backend token exchange is complete)
- Ownership transfer of documents when a user is offboarded (matters can be handed over today; deleting a user still deletes their documents, so transfer their matters and deactivate instead)
- Ethical walls: admin-managed matter access and deny-lists (today, access is by matter membership only)
- Retention policies and legal hold for documents and chats (today, only audit logs have retention)
- In-place `SECRET_KEY` rotation with re-encryption of stored documents
- Per-user usage metering and quotas for server-level provider keys
- Benchmark rerankers on the accuracy benchmark (Cohere Rerank and a local cross-encoder are wired in; neither is on by default, and no legal-domain reranker has been tried)
- Grow the accuracy benchmark (`backend/evals/`) from the synthetic starter set to attorney-written questions over real-world documents
- Preserve run-level formatting from the source .docx in redline exports

## Future
- SAML / generic OIDC single sign-on and SCIM provisioning (today: Azure AD only)
- External anchoring of the audit-chain head (object-lock bucket or SIEM forwarding)
- Application-level encryption of the vector index and database text (today these rely on disk encryption)
- Multi-worker / multi-replica deployments (sessions and document registry in Redis/Postgres)
- Integration with additional court e-filing systems
- Multi-language document support
