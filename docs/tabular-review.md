# Tabular review

Tabular review extracts the same set of facts from many documents and lays
them out as a grid: one row per document, one column per field, and behind
every answered cell the verbatim passage that supports it. It is built for the
sweep a firm does across a portfolio of contracts — every NDA's confidentiality
term, every lease's rent escalation — and for the review that has to follow,
because a model proposes and a lawyer disposes.

## Model

```
ReviewTable       name, description, matter, fields (the schema), document_ids (the scope)
  ReviewCell      per (document, field):
                    model layer   value, quote, span_start/span_end, verified, status
                    review layer  override_value, review_status, note, reviewed_by, reviewed_at
                    history       the last 20 changes [{at, by, kind, value, status}]
ReviewRun         per extraction run: runner, scope, status, counts, job
ReviewSchema      a saved field set, personal or shared with a matter
```

A field is a name, a type (`text`, `date`, `money`, `number`, `boolean`,
`duration`) and an optional hint for the model. Its `key` is a slug derived
from the name and is what cells are stored under, so renaming a field keeps
its column.

## Extraction

A run is one background job (`POST /review-tables/{id}/run`, 202 + a job to
poll). Documents are extracted concurrently (`REVIEW_TABLE_CONCURRENCY`,
default 4), one utility-model call per document over labelled excerpts of its
indexed text, and each document's row is written as it finishes. The model's
answer for a field must carry a quote; the quote is located in the document
text character for character (`services/quote_verify.py`):

| Cell status | Meaning |
|-------------|---------|
| `extracted` | The quote was found verbatim; the value is shown as sourced. |
| `unverified` | The model answered but its quote was not found; the value is kept and de-emphasised. |
| `not_found` | The model said the excerpts do not establish the field. |
| `error` | The document could not be read, or the model call failed. |

A run may be narrowed to some documents and/or fields (`document_ids`,
`field_keys`), which is how the grid re-runs a cell, a row, a column or a
selection. Every run is recorded in `review_runs` with its scope and, when it
finishes, its counts (verified, unverified, not found, errors, and **changed**:
cells whose model value differs from the previous run). `GET
/review-tables/{id}/runs` lists them.

## The review layer

Every cell carries a human layer on top of the model's answer:

- **Override.** A reviewer may replace the value. The model's value is kept;
  the *effective value* (what the grid, the exports and downstream steps use)
  is the override when set.
- **Review status.** `pending`, `approved` or `flagged`. Approve what you have
  checked; flag what a colleague should look at.
- **Note.** Free text: why it was approved, flagged or changed.
- **History.** The last twenty changes to the cell — the model's answers
  (`model`), overrides (`override`), approvals (`approve`), flags (`flag`) and
  clears (`clear`) — each with who and when.

`PATCH /review-tables/{id}/cells/{document_id}/{field_key}` changes one cell;
`POST /review-tables/{id}/cells/bulk` applies the same change to many
(approve a column, flag a selection). The owner and every member of the
table's matter are reviewers; anyone else gets 404.

**A re-run never undoes a reviewer's work.** The model's new value goes into
`value`; the override, the note and the reviewer stay. If the model's value
changed and the cell is **not** overridden, an approval drops back to
`pending` so someone looks again; an overridden cell keeps its status
whatever the model now says. The change is appended to the history, and the
grid's **Changed** filter shows the cells whose last two model answers differ.

## The grid

- **Filters.** Status chips with counts (needs review, unverified, not found,
  errors, flagged, approved, changed) and a text filter under every column
  header. A row stays when any of its cells passes the status filter.
- **Sort.** Click a header. Dates, money, numbers and durations sort as
  quantities (`$1.2m` after `USD 250,000`, `12 months` after `30 days`);
  blanks sort last either way.
- **Selection.** Row and column checkboxes, Ctrl-click or Space on a cell;
  the selection bar approves, flags, resets or re-runs the selected cells.
  Each column header also approves or re-runs the whole column.
- **Keyboard.** Arrow keys, Home/End and Page keys move the focused cell;
  Enter opens the source panel; E edits (override); A approves; F flags;
  Escape closes the panel.
- **Source panel.** Opening a cell shows the document's indexed text
  (`GET /review-tables/{id}/documents/{document_id}/text`, the same text the
  offsets refer to) with the quote highlighted and scrolled into view; an
  unverified quote is located by search and marked *approximate*. The panel
  holds the model value, the override editor, approve / flag, the note and
  the history.
- **Columns.** Sticky header and document column; resizable widths kept per
  table in the browser. Rows are windowed, so a table of hundreds of
  documents stays light.
- **Summary strip.** Review progress (approved cells over documents ×
  fields) and per-column outcome bars; the run's progress while it runs.

## Schemas and templates

`GET /review-tables/schemas` lists the built-in schemas (NDA, master services
agreement, commercial lease, employment agreement — defined in code, 8–12
described fields each) and the firm's saved ones. A saved schema (`POST`,
`PATCH`, `DELETE /review-tables/schemas/{id}`) is personal unless filed under
a matter, in which case the matter's members can use it; its owner alone edits
or deletes it. The create form starts from any template or blank; the schema
editor loads a template (replacing or adding to the fields), reorders fields,
and saves the current fields as a new template.

## Documents after creation

`POST /review-tables/{id}/documents` appends documents (owner only; each must
be readable by the caller); with `run: true` only the new rows are extracted.
`DELETE /review-tables/{id}/documents/{document_id}` drops a row and its
cells. The grid offers both: **Add documents** in the header, and a remove
action on each row.

## Exports

`GET /review-tables/{id}/export` takes:

| Parameter | Effect |
|-----------|--------|
| `format` | `xlsx` (default), `csv` or `json` |
| `fields` | Comma-separated field keys to include |
| `reviewed_only=true` | Only cells a reviewer approved |
| `status` | Comma-separated extraction or review statuses: `extracted`, `unverified`, `not_found`, `error`, `approved`, `flagged` |
| `include_quotes=false` | Leave the supporting quotes out |

The workbook has four sheets: **Results** (one row per document, effective
values, every answered cell hyperlinked to its citation; overrides in a
distinct style), **Citations** (quote, offsets, verified, status),
**Review** (model value, override, effective value, status, reviewer, note)
and **Summary** (per-column counts). CSV is one row per (document, field)
with the review columns appended; JSON carries the table, fields, documents,
cells and summary.

## Access and audit

The owner edits the schema and scope, adds and removes documents, and
deletes; owner and matter members read, run, review and export; anyone else
gets 404. Runs, reviews (as counts), document changes, schema changes and
exports are audited with ids and counts only — never a value, quote or note.

## What leaves your infrastructure

A run sends each document's excerpts and the field list to the configured
LLM provider under the runner's key, as the contract key-terms table does.
Reviews, notes, history, schemas and exports stay on your deployment.
