# CaseCite accuracy benchmark

A measured answer to "how often does CaseCite find, and correctly state, what
the documents say?" It indexes a seeded corpus into an isolated store through
the application's own indexing and search code, asks each question, and
scores the result against gold evidence and gold facts.

Nothing touches the application's data directory: the index lives in a
temporary directory, and the app config is loaded with throwaway secrets.

## Run it

From `backend/`:

```bash
# Offline: no API key. A pipeline regression check (CI runs a floor of this).
python -m evals.run

# Offline with a small real semantic model (all-MiniLM-L6-v2, ~80 MB one-time
# download to ~/.cache/chroma). Better than hash embeddings, still not the
# production model.
python -m evals.run --embeddings local

# The numbers that matter: the configured embedding provider (OPENAI_API_KEY,
# VOYAGE_API_KEY or COHERE_API_KEY in the environment or .env). Corpus
# embeddings are cached in evals/.cache/, so re-runs only pay for new text.
python -m evals.run --embeddings provider --out evals/results/provider.json

# Full answers (needs a chat model key), graded against the reference answers
python -m evals.run --embeddings provider --mode all --judge

# Compare a configuration against a saved run
python -m evals.run --embeddings provider --search use_lexical=false \
    --compare evals/results/provider.json
```

Options: `--search key=value` passes an argument to the search function
(`use_lexical`, `use_hybrid`, `use_reranking`, `similarity_threshold`),
`--setting key=value` overrides a per-user RAG setting for answers
(`query_expansion`, `citation_verification`, `llm_model`, ...),
`--top-k` sets how many passages the model is given (default 10),
`--only id,id` runs a subset, and environment variables override server
settings for a run (`HYBRID_RRF_K=60`, `CONTEXTUAL_CHUNK_HEADERS=false`,
`CHUNKING_STRATEGY=fixed`, `RERANKER_PROVIDER=cohere`).

The offline modes lower the similarity floor to 0.001, because the app's
default floor is calibrated for the hosted embedding models. Provider runs
keep the app's real floor.

## What is measured

**Retrieval** (every question with gold evidence):

| Metric | Meaning |
|---|---|
| recall@k | Share of the evidence passages found among the top k passages |
| complete@k | Share of questions where *every* evidence passage is in the top k |
| mrr | Mean reciprocal rank of the first evidence passage |

A retrieved passage "contains" an evidence snippet when it holds at least 60%
of the snippet's word trigrams, which tolerates whitespace, table markup and a
chunk boundary clipping a few words.

**Answers** (`--mode answers` or `all`):

| Metric | Meaning |
|---|---|
| facts_ok | The answer states every required fact and none of the forbidden ones. For questions the documents do not answer, this is the abstention check |
| evidence_docs_cited | Share of the evidence documents among the answer's citations |
| claims_verified | Share of grounded claims whose quote verified verbatim |
| claims_flagged | Share of grounded claims the support check labelled partial, contradicted or unsupported |
| judge_correct / partial / incorrect | `--judge`: a model compares the answer with the reference answer, counting dropped qualifiers as partial |

## The corpus

`corpus/` holds eight synthetic agreements written for this benchmark (an
MSA and its amendment, a commercial lease, a loan, a supply agreement, an
executive employment agreement, a mutual NDA, an asset purchase agreement).
They are built to contain the failure modes that matter in legal work:
defined terms used far from their definitions, provisos and cure rights, an
amendment that replaces a term, tables, clause-number lookups, and clauses
that are deliberately absent (no governing-law clause in the NDA, no insurance
clause in the MSA, no purchase right in the lease).

`distractors.py` generates 105 look-alike agreements from a fixed seed:
other leases with other late fees, other loans that accelerate on default,
other NDAs with other survival periods. Without them every passage would be in
every candidate pool and recall would be meaningless.

## Adding questions

Questions live in `questions.json`:

```json
{
  "id": "lease-late-charge",
  "category": "fact",
  "question": "What is the late charge under the Calloway Bakery lease?",
  "evidence": [{"doc": "harbor_point_calloway_lease.txt", "text": "verbatim text that proves the answer"}],
  "must_include": [["5%", "five percent"], ["10 days", "ten (10) days"]],
  "must_not_include": [],
  "expect_abstain": false,
  "reference_answer": "5% of the overdue amount if rent is not received within 10 days after its due date."
}
```

- `evidence` text must appear verbatim in the named document (the run refuses
  a question file where it does not). Keep it short: the operative sentence.
- `must_include` is a list of groups; each group is a list of acceptable
  alternatives, one of which must appear. Matching is case-insensitive and on
  whole words; end an alternative with `*` to make it a stem (`accelerat*`).
- Questions the documents do not answer have `"evidence": []`,
  `"expect_abstain": true`, and abstention phrases in `must_include`.
- `category` groups the report: `fact`, `qualifier`, `defined_term`,
  `exact_clause`, `table`, `amendment`, `cross_reference`, `multi_doc`,
  `missing_evidence`.

To benchmark on your own documents, add `.txt` files to `corpus/` (or point a
copy of the harness at another folder) and write questions against them. The
starter set is 49 questions over synthetic documents; it shows the shape of a
benchmark, not a measure of real-world accuracy. Before claiming an accuracy
figure, grow it to a few hundred questions written and checked by attorneys
over documents like the firm's own, including questions designed to mislead.

## Results

`results/` is git-ignored except files named `baseline-*.json`. Commit a
baseline when you want later runs compared against it.
