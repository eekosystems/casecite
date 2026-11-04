# Reviewing and signing off the benchmark

The benchmark's numbers are only as good as its gold data. Every question,
evidence passage, required fact and reference answer in `questions.json` was
written by the engineering team over synthetic agreements. Before an accuracy
figure from this benchmark is quoted to anyone — a customer, a security
questionnaire, the marketing site — the question set must be reviewed and
signed off by an attorney, and the review recorded. This page is that process.

## Who

- **Reviewing attorney**: a lawyer who reads the kind of documents in the
  corpus for a living. They own the correctness of every question they sign.
- **Engineer**: keeps the file valid (`pytest tests/test_eval_harness.py`),
  runs the benchmark, records the sign-off, and never edits a signed question
  without sending it back for review.

## What to check, per question

1. **The question is one a lawyer would ask**, in the words they would use,
   and has one defensible answer from the documents (or none, for
   `expect_abstain`).
2. **The evidence is the operative text**: the sentence or clause that
   proves the answer, verbatim, and all of it. If the answer depends on two
   passages (a defined term and the clause that uses it, an amendment and
   the clause it replaces), both are listed.
3. **`must_include` captures every fact that changes the outcome**: the
   number, the notice period, the cure right, the exception. Each group lists
   the wordings a correct answer might use (`45 days`, `forty-five (45)
   days`). A missing alternative makes a right answer fail; a missing group
   lets a wrong answer pass.
4. **`must_not_include` holds only wordings a wrong answer would use.** The
   check is literal, so a phrase a correction might repeat (`not 10%`) does
   not belong here; `late charge is 10%` does.
5. **The reference answer is complete and cites its sections.** The
   model-graded pass compares answers to it, so a qualifier it omits is a
   qualifier the grader never looks for.
6. **`expect_abstain` questions really are unanswerable** from the corpus,
   including the generated look-alike agreements (`distractors.py`), which
   exist to tempt a confident wrong answer.
7. **Adversarial questions mislead on purpose.** A `misleading_premise`
   question asserts something the documents contradict; a correct answer
   corrects it. A `conflict` question has two passages that disagree; a
   correct answer says which controls and why.

## Recording the review

Sign-offs live in `questions.json` under `review`:

```json
"review": {
  "status": "reviewed",
  "signoffs": [
    {
      "reviewer": "A. Attorney",
      "role": "attorney",
      "date": "2026-11-20",
      "questions": "all",
      "version": 2,
      "notes": "Rewrote premise-apa-cap-rate; the rest as written."
    }
  ]
}
```

- `questions` is `"all"` or a list of ids. `status` is `unreviewed`,
  `partially_reviewed` (some ids signed) or `reviewed` (every id signed at the
  current `version`).
- Any change to a signed question — wording, evidence, facts, reference —
  bumps `version` and sets `status` back until it is re-signed. A sign-off
  names the version it reviewed, so a stale one is visible.
- Review in batches by `category`; the report groups results the same way,
  so a category whose questions are unreviewed is a category whose numbers
  are provisional.

## Before quoting a number

- The set is reviewed (`status: reviewed`) at the version that produced the
  number.
- The run used the configured provider (`--embeddings provider --mode all
  --judge`), not the offline embeddings and never the mock provider. Offline
  runs are regression floors; the mock-provider run proves the citation
  pipeline works and says nothing about answer quality.
- The report (`results/*.json`) is kept with the embedding model, chat
  model, date and corpus size it records, and the number is quoted with them.
- The corpus is still synthetic. A figure from it describes the pipeline on
  documents built to contain known failure modes, not accuracy on a firm's
  own files. Before claiming real-world accuracy, grow the set to a few
  hundred attorney-written questions over documents like the firm's own, with
  the misleading and conflicting cases included, and review it the same way.
