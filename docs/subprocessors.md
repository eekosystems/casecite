# Data Flows & Subprocessors

CaseCite is self-hosted: the firm runs the application, database and vector
index on its own infrastructure. There is no CaseCite-operated backend, no
telemetry and no analytics; nothing is ever sent to the CaseCite maintainers.

**But AI features send content to the AI providers you configure.** When a
document is uploaded, its text is sent to the embeddings provider. When a
question is asked or a document is analysed, the question and the relevant
document text are sent to the LLM provider. Those calls are made under the
firm's own API keys and the firm's own agreement with each provider. The only
way to keep document text entirely on your network is to run both the chat
model and the embeddings model yourself (see "Keeping data on-premises").

This document lists every third party the software can contact, so a firm's
InfoSec team can assess and control it.

## Calls made by the server

| Recipient | When | Data sent | How to control |
|-----------|------|-----------|----------------|
| **OpenAI** (`api.openai.com`) | Chat/analysis with an OpenAI model; embeddings by default | Questions, prompts, retrieved document excerpts, full document text for analysis and embedding | Don't configure an OpenAI key; or set `OPENAI_BASE_URL` (chat) **and** `EMBEDDING_BASE_URL` (embeddings) to servers you run |
| **Anthropic** | Chat/analysis with a Claude model | Questions, prompts, document excerpts and text | Don't configure an Anthropic key |
| **Google (Gemini API)** | Chat/analysis with a Gemini model (per-user key only) | Questions, prompts, document excerpts and text | Users don't save a Gemini key; or exclude `google` with the provider allowlist |
| **Voyage AI** | Embeddings, when `VOYAGE_API_KEY` and a `voyage-*` model are set | Document text chunks, search queries | Optional; don't configure |
| **Cohere** | Embeddings, when `COHERE_API_KEY` is set and selected | Document text chunks, search queries | Optional; don't configure |
| **Pinecone** | Only when `VECTOR_DB=pinecone` | Embedding vectors **and document text chunks** (stored as vector metadata), with document and owner identifiers | Optional; the default `VECTOR_DB=chroma` keeps the index on your own disk |
| **CourtListener** (`www.courtlistener.com`, `storage.courtlistener.com`) | Case-law, citation, docket and judge lookups; deep-research sub-questions that use the case-law tool, and the citation check on the authorities a deep-research answer cites ([deep-research.md](deep-research.md)) | Search queries, case names and citations. These can be derived from a user's question (for deep research, the sub-questions and follow-up queries as the planner phrased them) or from citations found in a client document; whole documents are not sent | Optional token; without one the features are rate-limited and citation check is unavailable |
| **Wikipedia** (`en.wikipedia.org`) | Judge Intel biography lookup | The judge's name | Used only by the Judge Intel feature |
| **SendGrid** | Password-reset email | Recipient email address, reset link | Optional; leave `SENDGRID_API_KEY` unset |
| **Microsoft identity platform** (`login.microsoftonline.com`) | Azure AD SSO token validation | Fetches Microsoft's public signing keys; no user data sent | Only when Azure AD SSO is configured |
| **Document-storage providers** — Google Drive, Microsoft OneDrive/SharePoint (Graph), Box, Dropbox, Filevine, Clio; iManage and NetDocuments (experimental) | When a user connects an account, picks files or runs a sync | OAuth tokens; requests for the files being imported. Data flows *in* from the provider | Each connector is off until its client ID/secret is configured. iManage and NetDocuments are untested against a live tenant and are additionally unregistered until `EXPERIMENTAL_CONNECTORS=true` |
| **OpenAI public blob storage** (`openaipublic.blob.core.windows.net`) | First use of the tokenizer after a fresh install outside Docker (the Docker image ships the file in `/opt/tiktoken`) | Nothing about your data — a one-time download of the `tiktoken` encoding file | Behind a strict egress allowlist, allow this host once or pre-seed `TIKTOKEN_CACHE_DIR` |

A custom OpenAI-compatible gateway (Groq, Together, an internal proxy, …) set
through `OPENAI_BASE_URL` receives what the OpenAI row describes. It also
receives the `Authorization` header of every request, which carries the
user's own OpenAI key when they have saved one, so the endpoint an admin
configures (in `.env` or at runtime under Settings → API Keys → Local LLM)
is trusted with those keys. Runtime changes to it are written to the audit
log; review them as you would a change to the provider allowlist.

## Calls made by the user's browser

| Recipient | When | Data sent |
|-----------|------|-----------|
| **Google** (`accounts.google.com`, `apis.google.com`) | Only when a user opens the Google Drive picker | The user's IP address and browser details; Google sign-in |
| **Microsoft** (`login.microsoftonline.com`, OneDrive/SharePoint picker) | Only when a user opens the OneDrive picker or signs in with Azure AD | The user's IP address and browser details; Microsoft sign-in |
| **Box / Dropbox** (`cdn01.boxcdn.net`, `www.dropbox.com`) | Only when a user opens the Box or Dropbox picker | The user's IP address and browser details; provider sign-in |

Nothing else is loaded from a third party. Fonts are bundled with the
application, and no script is fetched from a CDN on page load.

## Keeping data on-premises

- **Self-hosted chat model:** set `OPENAI_BASE_URL` to an OpenAI-compatible
  server on your network (Ollama, vLLM, LM Studio). Admins can also set this at
  runtime (Settings → API Keys → Local LLM).
- **Self-hosted embeddings:** set `EMBEDDING_BASE_URL` (and `EMBEDDING_MODEL`)
  to an OpenAI-compatible embeddings server on your network. This is separate
  from `OPENAI_BASE_URL` on purpose: with only the chat endpoint redirected,
  uploaded document text would still be sent to `api.openai.com` for embedding.
  Changing the embedding model requires re-indexing existing documents.
- With **both** set, `VECTOR_DB=chroma`, no provider keys configured, and the
  connectors and CourtListener features unused, no document text leaves the
  deployment.
- **Provider allowlist:** `HIPAA_ENFORCEMENT_ENABLED=true` with
  `APPROVED_AI_PROVIDERS=["anthropic","self_hosted"]` refuses a call whose
  provider is not listed. The check looks at the endpoint that would receive
  the request, not the model name. **Coverage:** it is enforced on every
  embedding call and every LLM call — research chat (answer generation, query
  routing, per-file answers, claim grounding, citation reasoning), contract
  analysis, drafting, strategy briefs, clause intelligence,
  case-law research and document summaries. It is an application-level check;
  pair it with an egress allowlist if you need a control that does not depend
  on the application. The variable name is historical: this is an allowlist,
  **not** a HIPAA compliance feature, and CaseCite makes no HIPAA claim.
- **Egress control:** run the stack behind an egress allowlist limited to the
  hosts you intentionally use. This is the only control that does not depend
  on the application behaving as documented.

## Where data rests

Persistent data lives in three places on the deployment: the application data
volume (uploaded documents, vector index, encrypted key store, audit logs),
the PostgreSQL database (users, chat history, analyses, extracted clause
text) and Redis (job results, rate-limit, lockout and token-revocation
keys).

Uploaded originals, stored API keys, connector tokens and MFA secrets are
encrypted by the application (AES-256-GCM). The vector index, the database and
Redis hold client text in cleartext at the filesystem level, so **all three
must sit on encrypted disks** (LUKS/dm-crypt or the cloud provider's encrypted
volumes). Backups contain the same data; see "Backups" in
[deployment.md](deployment.md).
