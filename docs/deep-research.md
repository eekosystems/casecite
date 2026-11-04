# Deep Research

Deep research is the Research view's multi-step mode. Instead of answering a
question from one retrieval, it plans the question into sub-questions, researches
each against a fixed set of tools, verifies every quote the model produces
against the text it was retrieved from, and writes an answer that can cite only
passages the run actually retrieved. The user sees and edits the plan before
anything is searched, and the finished run carries a step log that shows exactly
what was searched and what came back.

## Pipeline

```
Question
  → Plan: 3-8 sub-questions, each tagged with the tools it may use   (one model call, synchronous)
  → User edits the plan (rewrite, remove, add, re-target sub-questions)
  → Run (one background job):
      for each sub-question, in parallel under a semaphore:
        tools → passages with server-assigned refs P1, P2, …
        one "answer with refs" call → claims, each citing refs and copying a quote
        optional one follow-up query (depth 1) → tools again → answer re-written
      verify: quotes located in their cited passage; unknown refs stripped and counted
      synthesise: one call over the sub-answers and cited passages → final answer
      join refs back in code; redact model prose that names a case in no passage
      citation check on the case-law authorities the answer cites (treatment scan)
  → Result: answer, key findings, open questions, step log, sources, scope, verification counts
```

The grounding rule is the one `strategy.py` established: **the model never names a
source**. Every passage a tool returns is given a reference by the server, the
model is told to cite only those, and after every call the references in its
output are joined back to real passages in code. A reference that names no
passage is removed and counted in the result; it is never shown. A quote the
model attributes to a passage is checked against that passage's text (the same
whitespace-tolerant match the key-terms table and the strategy brief use). A
quote that cannot be located is **kept and flagged unverified**, never dropped
silently, so the user sees both the claim and the fact that it is ungrounded.

## Tools

The tool set is fixed. The planner picks from it; the run calls only what the
approved plan allows.

| Tool | What it does | Source of the passage text |
|------|--------------|----------------------------|
| `documents` | Semantic search over the firm's indexed documents, with the same tenant scope and document filter research chat uses (the explicit file pick, else the active matter's documents, else everything the caller can read) | The stored chunk text |
| `case_law` | CourtListener opinion search; for each candidate the opinion is fetched and the passage most relevant to the sub-question is cut from its real text | The opinion text (a verbatim slice); the search snippet only when the opinion text cannot be fetched, and the source is then marked `snippet only` |
| citation check | `validate_citation` on the reporter citations of the case-law authorities the final answer cites — the treatment-language scan, **not a citator** | n/a |

There is no connector-backed fetch. The connector services import files into the
index; there is no read-a-document-on-demand path to wrap, and inventing one would
bypass the index's tenant scoping. Documents reach deep research the way they
reach every other feature: by being indexed.

## Model

Every model call in a run (the plan, each sub-question, the synthesis) uses the
**research model**: `OPENAI_RESEARCH_MODEL` (or `OPENAI_DEEP_RESEARCH_MODEL`), or
*Research Model* in Settings → API Keys → Local / Self-Hosted Models. Unset, it
is the utility model, as before the setting existed. It is kept
apart from the utility model on purpose: the utility model serves many small
background calls, so it can stay small and fast while research runs on a
stronger model. Calls go through the instance's OpenAI-compatible client, so a
configured local endpoint serves them.

## Caps

Every run is bounded so its cost is predictable. All of these are settings
(`.env`):

| Setting | Default | Meaning |
|---------|---------|---------|
| `DEEP_RESEARCH_MAX_SUBQUESTIONS` | 8 | Sub-questions a plan may hold (the planner proposes 3-8; a run with more is refused) |
| `DEEP_RESEARCH_CONCURRENCY` | 3 | Sub-questions researched at the same time within one run |
| `DEEP_RESEARCH_MAX_TOOL_CALLS` | 24 | Searches per run across all sub-questions and follow-ups; a call past the cap is recorded as refused on the step |
| `DEEP_RESEARCH_MAX_PASSAGES` | 60 | Passages the run may hold; further passages are dropped and counted |
| `DEEP_RESEARCH_FOLLOWUP_DEPTH` | 1 | Follow-up queries a sub-question may propose (0 disables follow-ups) |

Fixed in code: 6 passages per document search, 3 candidates per case-law search,
an opinion passage of at most 4,000 characters, 8 claims per sub-question, and
at most 8 cited authorities checked for treatment. The job's result is capped at
1 MB like every other job, so passages are carried as 400-character snippets;
the full text is used for verification but not stored.

## What leaves your infrastructure

- **The LLM provider** (the user's own key, else the server's) receives: the
  question and the planner prompt; for each sub-question the retrieved passages
  (document chunks and opinion excerpts) inside the untrusted-content
  delimiters; and the sub-answers plus cited passages for the synthesis.
- **CourtListener** receives the case-law search queries (the sub-question or
  its follow-up query, as the planner phrased them), the ids of the opinions
  fetched, and the reporter citations checked for treatment. No document text
  is sent to CourtListener.
- Nothing else is contacted. See [subprocessors.md](subprocessors.md) for the
  full table and how to keep both model calls on your own network.

## Access and audit

- `POST /deep-research/plan` and `POST /deep-research/run` need the `chat.use`
  permission. A `matter_id` the caller does not belong to answers 404.
- A run is readable by its owner and by members of its matter; the owner alone
  deletes it; anyone else is told it does not exist (404, never 403).
- Runs are persisted (`deep_research_runs`, migration 016) with their plan, scope,
  status (`running`, `completed`, `failed`, `cancelled`), result and error, so a
  past run reopens from the panel's history.
- Audit entries: `deep_research_started` (matter id, sub-question count, tools,
  documents in scope), `deep_research_completed` (sources cited, tool calls,
  passages, quotes verified and unverified, invalid refs stripped, citations
  checked, model) and `deep_research_deleted`. Ids and counts only: the
  question, answers and quotes never enter the audit log.
- Cancelling the job (`DELETE /jobs/{job_id}`) stops the run and marks it
  `cancelled`. A run is never retried by the job queue: a failure is recorded on
  the run and surfaced, because re-running the whole pipeline on a failed call
  would silently multiply its cost.

## Reading a result

- **Answer** — markdown with inline `[P<n>]` references. Every reference names a
  passage in the Sources list.
- **Key findings / Not settled by this research** — the synthesis's own summary
  and the points it says the passages did not resolve.
- **Verification line** — counts from the run: quotes verified, quotes marked
  unverified, references to no real source removed, case names redacted,
  authorities checked.
- **Sources** — one entry per cited passage: title, citation, the snippet, how
  many quotes verified against it, and for case law the treatment check's
  outcome in plain words (never a verdict the scan cannot give).
- **Step log** — per sub-question: each search, its query and passage count (or
  the error), the follow-up query, the sub-answer, every claim with its quote
  and whether the quote was located, and the step's counters.
- **Scope note** — what was searched and what was not: documents were searched,
  not read in full; opinions were read in the passage cited, not in full.

Every result ends with the AI-review notice. Deep research is a research aid;
verify every statement, citation and quote against the source before relying on
it.
