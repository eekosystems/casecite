# CaseCite for Word

A task-pane add-in for Microsoft Word that talks to your firm's own CaseCite
server. It lets a lawyer stay in the document they are working on and:

- **Analyze this document** — runs CaseCite's contract review on the open
  document and lists the issues and key terms in the pane. Clicking an issue
  selects the quoted passage in Word.
- **Insert a suggested rewrite as a tracked change** — the issue's suggested
  language replaces the quoted passage with Track Changes on, whatever the
  document's own tracking setting is, which is then restored.
- **Research** — ask a question over the firm's indexed documents (optionally
  one matter's), read the cited answer, and insert it at the cursor with one
  footnote per source.
- **Settings** — the server address, the matter to work in, and sign-out.

Every AI-generated insertion ends with the same verify-before-relying notice
the web app puts on its exports. The add-in is a client only: documents and
answers go to your CaseCite server and from there to the AI provider your firm
configured, exactly as they do from the browser.

## How sign-in works

The pane cannot share the browser's CaseCite cookies, so it signs in the way a
TV app does (RFC 8628 device code):

1. The pane asks the server for a code and shows it (`XXXX-XXXX`).
2. It opens `https://<your CaseCite>/device` in your browser, where you are
   already signed in; you type or confirm the code and press **Approve**.
3. The pane's next poll receives a session. That session is listed in the web
   app under Settings → Security → Sessions as **Word add-in** and can be ended
   there like any other.

Accounts that `REQUIRE_MFA` obliges to enrol must do so before they can
approve a code. The dev-only `dev@localhost` account cannot be used from the
add-in because it has no user row; sign in with a real account.

## Prerequisites

- A running CaseCite server reachable over HTTPS from the machine running Word
  (`DEBUG=true` on `http://localhost:8000` works for local development).
- On the server, `WORD_ADDIN_ORIGIN` set to the origin the pane is served from
  (see below). `FRONTEND_URL` should point at the web app so the approval link
  the pane opens is absolute; without it the pane prefixes its own server URL.
- Node.js 20.19 or newer.
- Word for Windows, Word for Mac, or Word on the web, on an account that can
  sideload add-ins (Microsoft 365, or Office 2021 and later for the desktop
  apps).

## Run it locally

```bash
cd word-addin
npm install
npm run dev          # https://localhost:3200
```

Office loads task panes over HTTPS only, localhost included. The dev server
uses a self-signed certificate by default; Word will refuse it until the OS
trusts it. The simplest route is Microsoft's own tool, which creates and
trusts a localhost certificate once:

```bash
npx office-addin-dev-certs install
# then point Vite at it (paths printed by the command above; on Windows they
# are under %USERPROFILE%\.office-addin-dev-certs)
ADDIN_SSL_KEY=~/.office-addin-dev-certs/localhost.key \
ADDIN_SSL_CERT=~/.office-addin-dev-certs/localhost.crt \
npm run dev
```

Set `WORD_ADDIN_ORIGIN=https://localhost:3200` in the server's `.env` so the
pane's requests pass CORS.

### Sideload the manifest

`manifest.xml` already points at `https://localhost:3200`.

- **Word on the web:** open a document, *Home → Add-ins → More Add-ins →
  My Add-ins → Upload My Add-in*, choose `manifest.xml`.
- **Word for Windows:** share a folder (any local folder shared with yourself
  works), add it under *File → Options → Trust Center → Trust Center Settings →
  Trusted Add-in Catalogs* with "Show in Menu" ticked, restart Word, then
  *Home → Add-ins → More Add-ins → Shared Folder*.
- **Word for Mac:** copy `manifest.xml` to
  `~/Library/Containers/com.microsoft.Word/Data/Documents/wef/` (create the
  folder if needed), restart Word, then *Insert → Add-ins → My Add-ins*.

The **CaseCite** button appears on the Home tab and opens the pane. Microsoft's
sideloading guide covers edge cases: <https://learn.microsoft.com/office/dev/add-ins/testing/test-debug-office-add-ins>.

## Deploy for the firm

1. Build the pane and host `dist/` on an HTTPS origin of your choice (any static
   host; it is plain files). The pane and the API may share an origin, but the
   pane must not be served from the CaseCite API path.

   ```bash
   npm run build
   npm run manifest -- --origin https://addin.yourfirm.com
   ```

   `dist/manifest.xml` now points every URL at that origin. Keep the `<Id>`
   stable across versions of one deployment; pass `--id <new GUID>` for a
   second, independent deployment.

2. On the CaseCite server set `WORD_ADDIN_ORIGIN=https://addin.yourfirm.com`
   and `FRONTEND_URL=https://casecite.yourfirm.com`, then restart.

3. Publish the manifest to users through **Centralized Deployment**: Microsoft
   365 admin center → *Settings → Integrated apps → Upload custom apps* →
   upload `dist/manifest.xml`, choose the users or groups. Word picks it up
   within a day; sideloading is not needed.
   <https://learn.microsoft.com/microsoft-365/admin/manage/centralized-deployment-of-add-ins>

Each user then signs in once from the pane as described above.

## Development

```bash
npm run lint          # ESLint, same rules as frontend/
npx tsc --noEmit
npx vitest run        # API client, device-flow state machine, Word search fallback, storage
npm run build
```

Tests never load office.js; the `Office`, `Word` and `OfficeRuntime` globals
are stubbed per test. Nothing here needs a Word host, which also means the
Word-side behaviour (selection, tracked changes, footnotes) is verified by
hand in Word, not by this suite.

Layout: `src/api` (HTTP client and response types), `src/auth/deviceFlow.ts`
(the polling state machine), `src/office` (everything behind `Word.run`),
`src/store.ts` (one Zustand store with all pane state), `src/screens` and
`src/components` (render only), `scripts/make-manifest.mjs`.

## What is not supported yet

- Only Word; no Outlook or Excel surface.
- Analysis runs on the body text Word reports; headers, footers, footnotes,
  comments and text boxes are not sent.
- Suggested rewrites replace the passage Word finds by text search. A passage
  Word cannot locate as written (heavy formatting, fields, tracked deletions)
  is reported as not found and nothing is changed.
- Footnotes need WordApi 1.5 (Word 2021 / Microsoft 365). Older hosts get a
  "Sources" list under the answer instead.
- Research answers are inserted as plain paragraphs; the web app's rich
  citation chips are not available in the pane.
- The pane uses the firm's server-level key or the key saved in the user's
  web Settings; keys cannot be entered in the pane.
- No Azure AD single sign-on inside the pane; sign-in always goes through the
  device code and the web app's own login (which may itself be Azure AD).
