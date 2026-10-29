# Workflows

A workflow is a saved, repeatable legal process: an ordered, linear list of
typed steps that run the platform's own operations over a set of documents.
Nothing in a workflow is code. Each step names an operation the platform
already has, the parameters it runs with, where its documents come from, and
optionally a condition over an earlier step's output. A run executes the
steps in order as one background job, keeps every step's output, pauses at an
approval step until an approver decides, and can be resumed, retried from a
step, or have a failed step skipped. Every change to the steps is a version,
and a run records the version it executed.

## Model

```
Workflow            name, description, matter, version, archived_at, steps: [Step, ...]
  Step              name, kind, params (validated per kind), inputs, condition
    inputs.source   run        – the documents the run was started with
                    documents  – ids fixed in the workflow
                    step       – the documents an earlier step produced, else read
    condition       {step, path, op, value} – run only when an earlier output satisfies it
WorkflowVersion     a snapshot of name, description and steps at one version number
WorkflowRun         the steps as they were when the run started, workflow_version, inputs, status
  WorkflowStepRun   per step: status, output {summary, data, document_ids, produced}, error
```

Step names are identifiers (`extract`, `review`, `memo`): other steps refer to
them by name, in `inputs.step`, in `include_outputs_from` / `from_steps`, and in
`{{step.field}}` templates. A reference may only name an **earlier** step;
saving a workflow that refers to a later or nonexistent step, repeats a name,
uses an unknown kind or fails a kind's parameter validation is refused (422).
A workflow holds at most `WORKFLOW_MAX_STEPS` steps (default 12). Settings:
`WORKFLOW_MAX_STEPS`, `WORKFLOW_MAX_DOCUMENTS_PER_STEP`,
`WORKFLOW_MAX_CITATIONS_PER_STEP`, `WORKFLOW_MAX_OUTPUT_BYTES`,
`WORKFLOW_NOTIFICATIONS`.

## Step kinds

| Kind | What it runs | Parameters | Output `data` |
|------|--------------|------------|---------------|
| `extract_fields` | A tabular review of the step's documents (`services/review_tables`). Creates a review table the runner owns, named `<workflow> — <step>` (or `table_name`), described as created by the run, then runs it. | `fields` (1–40, as a review table's schema: name, type, optional description), `table_name` | `table_id`, `fields`, `documents`, `rows` (per document, per field: value, status, verified, quote), `values` (per document, per field: value), `counts` |
| `analyze_contract` | The full contract pipeline (`contract_analysis.service.analyze`) on each document, up to `WORKFLOW_MAX_DOCUMENTS_PER_STEP` (25). A document that cannot be read or analysed is recorded in `failures`; the step fails only when every document fails. | `contract_type` (default `auto`), `jurisdiction`, `representing`, `posture` (`balanced` / `strict`), `instructions` | `analyses` (document, analysis id, contract type, issue counts by severity, `issue_list` with up to 25 issues' title, severity, reason, quote and suggested language, executive summary, parties, key terms), `failures`, `skipped_document_ids` |
| `compare_contracts` | Clause-by-clause comparison (`contract_analysis.compare.compare_contracts`) of up to three documents against a baseline. | `baseline` (`first_input`, the default, or `document`), `baseline_document_id` (required when `baseline` is `document`), `focus` | `baseline_document_id`, `comparisons` (labels, overall, clauses with status and verified quotes, difference count), `failures` |
| `draft_document` | A draft (`contract_analysis.drafting.draft_document`) from the instructions after `{{…}}` substitution, with the user's practice profile and playbook prepended as the drafting routes do, the named earlier outputs and up to four reference documents as reference text, and the case-law guard applied. | `instructions` (templated), `include_outputs_from` (earlier step names), `reference_document_ids` (≤ 4) | `title`, `text`, `case_law_removed`, `references`, `template_values_used`, `template_values_missing` |
| `deep_research` | A deep-research run (`services/deep_research`): the templated question is planned, the plan optionally narrowed to `tools`, and run over the step's documents (none → everything the runner may read). The run is persisted like any other and appears in the Research view's history. | `question` (templated), `tools` (`documents`, `case_law`; omit for the planner's choice) | `run_id`, `question`, `answer`, `key_findings`, `open_questions`, `sources`, `verification`, template usage |
| `citation_check` | The treatment-language scan (`validate_citation`) of reporter and Westlaw citations given explicitly plus those found in earlier outputs, up to `WORKFLOW_MAX_CITATIONS_PER_STEP` (10). Not a citator. | `citations`, `from_steps` (earlier step names; omit for all earlier steps) | `citations` (per citation: status, warning level, negative and caution counts), `found_in_steps`, `skipped` |
| `approval` | Nothing. The run is parked as `awaiting_approval`, the step as `awaiting`, the job ends, and the approvers are told. `POST /workflows/{id}/runs/{run_id}/approve` continues from the next step; `/reject` ends the run as `rejected`. | `title`, `instructions`, `approvers` (`owner`, `any_member`, or a list of user ids who must be members of the workflow's matter) | `title`, `instructions`, `approvers`, `approver_ids` (resolved when the run reached the step), then `decision`, `decided_by`, `decided_by_email`, `decided_at`, `note` |

Every step's output also carries a one-line `summary`, the `document_ids` it
read, and `produced` ids by kind (`review_tables`, `analyses`, `research_runs`).
Work product a step creates belongs to the runner and stays when the workflow
or run is deleted. A stored output larger than `WORKFLOW_MAX_OUTPUT_BYTES`
(default 524,288) is cut: the summary, document ids and produced ids always
survive, the largest `data` fields are shortened or dropped first, and the
output carries `truncated: true` with `truncated_fields` naming what was cut.

## Conditions

Any step may carry `condition: {step, path, op, value}`. Before the step runs,
`path` is walked over the named earlier step's output exactly as a
`{{step.path}}` template reference is (`data.analyses.0.issues`, `summary`;
empty for the whole output) and compared with `value`:

| `op` | True when |
|------|-----------|
| `exists` / `not_exists` | the path holds a non-empty value / does not |
| `equals` / `not_equals` | the value equals `value` (numbers as numbers, text case-insensitively) |
| `contains` | a list has the item, a dictionary has the key, or text contains it |
| `gt`, `gte`, `lt`, `lte` | the value, or the leading number in it ("36 months", "USD 250,000"), compares so with `value` |
| `count_gt`, `count_eq` | a list, dictionary or text has more than / exactly `value` items |

A false rule records the step as `skipped_condition` with what was found
(`output.data.condition`: the rule, whether the path existed, and the value
seen), and the run continues; later steps see the skipped step's placeholder
output. A rule over a step that has not run, or a comparison that makes no
sense, is false; nothing is evaluated. The condition's step must be an earlier
one, like every other reference.

## Approval gates

An `approval` step parks the run: the step becomes `awaiting`, the run
`awaiting_approval`, the job ends (nothing holds the event loop), and the
approvers are resolved at that moment (`owner`: the workflow's owner;
`any_member`: every member of the workflow's matter, or the owner when it has
none; a list: those users, who must be matter members when the workflow is
saved). Run summaries carry `awaiting_step`, `approver_ids` and `can_approve`.
`POST /workflows/{id}/runs/{run_id}/approve` (body `{note}`) by an approver
records the decision on the step and re-submits the run from the next step
with every earlier output reloaded; `/reject` records it, skips the later
steps and ends the run as `rejected`. Anyone else who can read the run gets
403; strangers 404. A run waiting for approval cannot be resumed or retried.

## Validation while editing

`POST /workflows/validate` takes `{steps}` as the editor holds them and
answers `{valid, issues: [{index, field, message}]}` without saving: every
problem, not the first. Unknown kinds, each kind's parameter errors under
`params.<field>`, duplicate names, a reference to itself or to a later or
missing step (in `inputs.step`, `condition.step`, `from_steps`,
`include_outputs_from` and `{{templates}}`), a compare step whose baseline is
a fixed document with none picked, empty drafting instructions, and the
step-count cap (`index: -1`). Saving applies the same rules and refuses with
422.

## Versions, duplicates and archiving

Every create writes version 1; every PATCH whose steps differ from the stored
ones writes a new `workflow_versions` row (name, description, steps, who, when,
`change_note`) and bumps `version`; a PATCH that only renames does not. A run
records `workflow_version`. `GET /workflows/{id}/versions` lists them newest
first, `GET …/versions/{n}` returns one with its steps, and
`POST …/versions/{n}/restore` (owner) makes that version's steps current as a
NEW version; history is never rewritten. `POST /workflows/{id}/duplicate`
copies a workflow the caller can read into one they own (named
"<name> (copy)" unless given; optional `matter_id`), at version 1.
`POST …/archive` and `/unarchive` (owner) set `archived_at`: an archived
workflow is left out of `GET /workflows` unless `?include_archived=true`,
cannot be run, resumed or edited, and keeps its runs and versions.

## Run controls

- `POST /workflows/{id}/runs/{run_id}/steps/{index}/retry` resets step
  `index` and everything after it and re-submits from there, whatever the
  step's state (a completed step re-runs with the same inputs). Owner or the
  person who ran it; not while the run is in progress or awaiting approval.
- `POST …/steps/{index}/skip` marks a failed (or not-yet-run) step `skipped`
  with `skipped_by` and continues from the next one; later steps do not see
  the skipped step's output.
- `GET /workflows/runs?matter_id=&limit=` lists the newest runs across every
  workflow the caller can read, each with `workflow_name`.
- The run view opens a run by link (`/workflows/{id}?run={run_id}`) and can
  show each step beside the previous run of the same workflow.

## Notifications

With `WORKFLOW_NOTIFICATIONS=true` and `SENDGRID_API_KEY` set, the runner is
emailed when a run completes, fails or is rejected, and the runner and the
resolved approvers when a run waits for approval. The mail names the
workflow, when the run started, its status, the failed step and its error or
the approval title, and links to the run (`FRONTEND_URL` +
`/workflows/{id}?run={run_id}`); it never carries document text or step
outputs. Each user may opt out with the `workflow_email_notifications` setting
(Settings, Advanced, Workflow Emails). Delivery runs off the event loop; a
failure is logged at warning level and never affects the run.

## Templates: `{{step.field}}`

The `instructions` of a draft step and the `question` of a research step may
embed values from earlier outputs:

```
{{extract}}                       the step's one-line summary
{{extract.data.values}}           its data, as JSON
{{research.data.answer}}          a nested field
{{review.data.analyses.0.contract_type}}   a list index
```

The lookup is a plain path walk over the earlier step's output (dictionary
keys and list indexes). Nothing is evaluated. A path that does not exist
renders as an empty string and is listed in the step's
`template_values_missing`, so the user sees what was not available. A
substituted value is capped at 12,000 characters.

## Runs

`POST /workflows/{id}/run` takes `document_ids`; with none, and a workflow filed
under a matter, every document filed under that matter is the input. The
route checks once, up front, that the runner can read every input, every
fixed document, baseline and reference the steps name, and that a model is
available; the steps then call the service layer directly as the runner. The
run is one job (202 + `poll_url`), never retried by the queue:

- Each step is marked `running`, then `completed` with its output, or `failed`
  with its error. A failure stops the run: later steps are `skipped` and the run
  is `failed` with the step named in its error.
- Cancelling the job (`DELETE /jobs/{job_id}`) marks the current step failed
  (`Cancelled`) and the run `cancelled`.
- `POST /workflows/{id}/runs/{run_id}/resume` re-submits a failed or cancelled
  run from its first unfinished step. Earlier steps keep their outputs and are
  not re-run; the steps executed are the ones the run started with, even if the
  workflow has been edited since. A run in progress or already completed cannot
  be resumed (400).
- Progress is reported per step (`Step 2/3 (review): Analysing contract 4/12`).

## Access and audit

- Routes need the `contracts.use` permission. The owner, or a member of the
  workflow's matter, reads and runs it; the owner alone edits the steps or
  deletes it; anyone else gets 404, never 403. A member runs with their own key
  and their own document access.
- Audit entries: `workflow_created` (matter, step count, kinds, template),
  `workflow_updated`, `workflow_deleted`, `workflow_run_started` and
  `workflow_run_resumed` (run id, steps, start index, documents, kinds),
  `workflow_run_completed` (counts of steps and produced work product) and
  `workflow_run_failed` (the step error, truncated). Parameters, document text
  and outputs never enter the audit log.
- Also audited: `workflow_run_awaiting_approval`, `workflow_run_approved` and
  `workflow_run_rejected` (run id, step index, note length), `workflow_step_retried`
  and `workflow_step_skipped` (run id, start index), `workflow_duplicated`,
  `workflow_archived`, `workflow_unarchived` and `workflow_version_restored`.
- Tables: `workflows`, `workflow_runs`, `workflow_step_runs` (migration 017);
  `workflow_versions`, `workflows.version`, `workflows.archived_at` and
  `workflow_runs.workflow_version` (migration 020).

## Built-in templates

`GET /workflows/templates` lists them; `POST /workflows/from-template/{key}`
makes a workflow of the caller's own from one, which they edit freely.

| Key | Steps |
|-----|-------|
| `nda_intake` | extract NDA terms → full review of each NDA (`contract_type: nda`) → citation check over the review's outputs |
| `lease_review` | extract lease terms → compare every lease against the first → draft a comparison memo attaching the comparison and the extracted terms |
| `due_diligence` | extract deal-relevant terms → deep research on change-of-control and assignment exposure, seeded with the extracted terms → draft a findings memo attaching both |

## What leaves your infrastructure

A workflow calls nothing a step does not already call on its own. The LLM
provider receives what the extract, analyse, compare, draft and research
steps send (see the features' own notes and
[subprocessors.md](subprocessors.md)); CourtListener receives the research
step's case-law queries and the citation-check step's citations. No new
third party is contacted.

Every output a workflow produces is model output. Verify it against the
source documents and current law before relying on it.
