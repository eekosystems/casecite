# Journey tests (live backend)

The specs in `e2e/` mock every API call and prove the UI. The specs here
prove the product: a browser drives the real backend through sign-in, a real
PDF upload, indexing, a research question, a citation verified against the
file, deletion and the audit trail. Behind the backend sits
`scripts/mock_llm_server.py`, a deterministic stand-in for the AI provider
(chat completions and embeddings), so the whole path runs offline with no
provider key.

CI runs them in the "E2E Journeys (live backend)" job. Locally:

```bash
# 1. The mock provider
python scripts/mock_llm_server.py --port 8089

# 2. A throwaway backend pointed at it (any free port; 8000 is the dev default)
cd backend
DEBUG=true SECRET_KEY=dev-secret-key-at-least-32-characters ENCRYPTION_SALT=dev-salt-16chars \
OPENAI_API_KEY=mock OPENAI_BASE_URL=http://127.0.0.1:8089/v1 \
EMBEDDING_BASE_URL=http://127.0.0.1:8089/v1 EMBEDDING_MODEL=hash-768 EMBEDDING_DIMENSIONS=768 \
UPLOAD_DIR=/tmp/journeys/uploads CHROMA_PERSIST_DIR=/tmp/journeys/chroma \
DATABASE_URL=sqlite+aiosqlite:////tmp/journeys/casecite.db CORS_ORIGINS=http://localhost:3000 \
python -m uvicorn app.main:app --port 8000

# 3. The journeys (Playwright starts the Vite dev server itself)
cd frontend
JOURNEYS=1 npx playwright test --project=journeys
```

When the backend is on another port, set `JOURNEYS_BACKEND_URL` for the
tests and `VITE_API_BASE_URL` (for example `http://localhost:8077/api/v1`)
for the dev server, and add the dev server's origin to `CORS_ORIGINS`.

The first account registered on an empty database is the admin; the audit
check at the end of the research journey needs that, so run against a fresh
data directory.
