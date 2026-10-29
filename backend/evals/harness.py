"""CaseCite accuracy benchmark.

Indexes the seeded corpus (``evals/corpus`` plus the generated look-alike
agreements) into an isolated vector store, then measures:

* Retrieval: for every question with gold evidence, whether the passages that
  prove the answer are among the passages the pipeline would hand the model
  (evidence recall@k, complete-evidence rate, mean reciprocal rank).
* Answers (needs a chat model key): whether the answer states the required
  facts, avoids the forbidden ones, abstains when the documents are silent,
  cites the right documents, and how many of its grounded claims verified.
  ``--judge`` adds a model-graded comparison against the reference answer.

Nothing here touches the application's own data directory: the store lives in
a temporary directory that is removed afterwards.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import re
import shutil
import statistics
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from evals import hash_embeddings
from evals.distractors import generate as generate_distractors

EVALS_DIR = Path(__file__).resolve().parent
CORPUS_DIR = EVALS_DIR / "corpus"
QUESTIONS_FILE = EVALS_DIR / "questions.json"
CACHE_DIR = EVALS_DIR / ".cache"
EVAL_USER = "eval-user"

_TOKEN = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------- #
# Corpus and questions
# --------------------------------------------------------------------------- #
@dataclass
class CorpusDocument:
    document_id: str
    filename: str
    text: str
    gold: bool


def load_corpus(distractors: int = 105) -> list[CorpusDocument]:
    """The gold documents plus ``distractors`` generated look-alikes."""
    docs = [
        CorpusDocument(
            document_id=f"gold-{path.stem}",
            filename=path.name,
            text=path.read_text(encoding="utf-8"),
            gold=True,
        )
        for path in sorted(CORPUS_DIR.glob("*.txt"))
    ]
    for filename, text in generate_distractors(distractors):
        docs.append(
            CorpusDocument(
                document_id=f"gen-{Path(filename).stem}",
                filename=filename,
                text=text,
                gold=False,
            )
        )
    return docs


def load_questions(path: Path = QUESTIONS_FILE) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def evidence_coverage(passage: str, evidence: str) -> float:
    """Share of the evidence's word trigrams that occur in ``passage``.

    Tolerant of whitespace, punctuation and table-markup differences, and of
    a chunk boundary clipping the first or last few words of the evidence.
    """
    ev = _tokens(evidence)
    if not ev:
        return 0.0
    hay = _tokens(passage)
    if len(ev) < 3:
        joined = " ".join(hay)
        return 1.0 if " ".join(ev) in joined else 0.0
    grams = {tuple(ev[i : i + 3]) for i in range(len(ev) - 2)}
    hay_grams = {tuple(hay[i : i + 3]) for i in range(len(hay) - 2)}
    return len(grams & hay_grams) / len(grams)


EVIDENCE_MATCH = 0.6


def evidence_rank(
    results: list[dict[str, Any]], evidence: dict[str, str], filenames: dict[str, str]
) -> int | None:
    """1-based rank of the first result that contains ``evidence``, else None."""
    for i, r in enumerate(results):
        meta = r.get("metadata") or {}
        name = meta.get("filename") or filenames.get(meta.get("document_id", ""), "")
        if name != evidence["doc"]:
            continue
        if evidence_coverage(r.get("text", ""), evidence["text"]) >= EVIDENCE_MATCH:
            return i + 1
    return None


def phrase_present(answer: str, phrase: str) -> bool:
    """Case-insensitive whole-word match; a trailing ``*`` makes it a stem.

    "No" matches "No." but never "Northwind"; "accelerat*" matches
    "accelerated" and "acceleration".
    """
    stem = phrase.endswith("*")
    phrase = phrase.rstrip("*")
    pattern = re.escape(phrase.lower())
    if phrase[:1].isalnum():
        pattern = r"(?<![a-z0-9])" + pattern
    if not stem and phrase[-1:].isalnum():
        pattern += r"(?![a-z0-9])"
    return re.search(pattern, (answer or "").lower()) is not None


def check_facts(answer: str, question: dict[str, Any]) -> dict[str, Any]:
    missing = [
        group
        for group in question.get("must_include", [])
        if not any(phrase_present(answer, p) for p in group)
    ]
    forbidden = [p for p in question.get("must_not_include", []) if phrase_present(answer, p)]
    return {"ok": not missing and not forbidden, "missing": missing, "forbidden": forbidden}


def validate_questions(corpus: list[CorpusDocument], questions: list[dict[str, Any]]) -> list[str]:
    """Problems with the question file: unknown docs, evidence not in its doc."""
    by_name = {d.filename: d for d in corpus}
    problems = []
    seen = set()
    for q in questions:
        if q["id"] in seen:
            problems.append(f"{q['id']}: duplicate id")
        seen.add(q["id"])
        if q.get("expect_abstain") and q.get("evidence"):
            problems.append(f"{q['id']}: expect_abstain questions must have no evidence")
        if not q.get("expect_abstain") and not q.get("evidence"):
            problems.append(f"{q['id']}: no evidence and not expect_abstain")
        for ev in q.get("evidence", []):
            doc = by_name.get(ev["doc"])
            if doc is None:
                problems.append(f"{q['id']}: unknown document {ev['doc']}")
            elif evidence_coverage(doc.text, ev["text"]) < 1.0:
                problems.append(f"{q['id']}: evidence not found verbatim in {ev['doc']}")
    return problems


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
class _CachedProviderEmbeddings:
    """Wrap the real embedding service with an on-disk cache.

    Re-running the benchmark must not re-embed (and re-pay for) an unchanged
    corpus. The cache key includes the model, so switching models never mixes
    vector spaces.
    """

    def __init__(self, service):
        self.service = service
        self.model = service.resolve_route(None).model
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = CACHE_DIR / f"embeddings-{re.sub(r'[^a-zA-Z0-9.-]', '_', self.model)}.json"
        try:
            self.cache: dict[str, list[float]] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.original_texts = service.embed_texts
        self.original_query = service.embed_query

    def _key(self, text: str, kind: str) -> str:
        return hashlib.sha256(f"{self.model}|{kind}|{text}".encode()).hexdigest()

    async def embed_texts(self, texts, user_keys=None):
        missing = [t for t in texts if self._key(t, "doc") not in self.cache]
        if missing:
            vectors = await self.original_texts(missing, user_keys=user_keys)
            for t, v in zip(missing, vectors, strict=True):
                self.cache[self._key(t, "doc")] = v
        return [self.cache[self._key(t, "doc")] for t in texts]

    async def embed_query(self, query, user_keys=None):
        key = self._key(query, "query")
        if key not in self.cache:
            self.cache[key] = await self.original_query(query, user_keys=user_keys)
        return self.cache[key]

    def save(self) -> None:
        with contextlib.suppress(OSError):
            self.path.write_text(json.dumps(self.cache))


@contextlib.contextmanager
def embeddings(mode: str) -> Iterator[str]:
    """Route the app's embedding service through ``mode``: 'hash', 'local' or 'provider'."""
    from app.services.embeddings import embedding_service

    if mode == "hash":

        async def embed_texts(texts, user_keys=None):
            return [hash_embeddings.embed(t) for t in texts]

        async def embed_query(query, user_keys=None):
            return hash_embeddings.embed(query)

        with (
            patch.object(embedding_service, "embed_texts", embed_texts),
            patch.object(embedding_service, "embed_query", embed_query),
        ):
            yield "hash-768"
        return

    if mode == "local":
        # all-MiniLM-L6-v2 via the ONNX runtime chromadb already ships: a real
        # (small, general-purpose) semantic model, offline after a one-time
        # ~80 MB download to ~/.cache/chroma. It reads at most 256 word
        # pieces per text, so long chunks are embedded from their opening.
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        model = ONNXMiniLM_L6_V2()

        def _vectors(texts):
            return [[float(x) for x in v] for v in model(list(texts))]

        async def embed_texts_local(texts, user_keys=None):
            return _vectors(texts)

        async def embed_query_local(query, user_keys=None):
            return _vectors([query])[0]

        with (
            patch.object(embedding_service, "embed_texts", embed_texts_local),
            patch.object(embedding_service, "embed_query", embed_query_local),
        ):
            yield "all-MiniLM-L6-v2 (local)"
        return

    if mode != "provider":
        raise ValueError(f"unknown embeddings mode {mode!r}")
    cached = _CachedProviderEmbeddings(embedding_service)
    try:
        with (
            patch.object(embedding_service, "embed_texts", cached.embed_texts),
            patch.object(embedding_service, "embed_query", cached.embed_query),
        ):
            yield cached.model
    finally:
        cached.save()


# --------------------------------------------------------------------------- #
# Isolated index
# --------------------------------------------------------------------------- #
@dataclass
class EvalIndex:
    service: Any
    vector_db: Any
    corpus: list[CorpusDocument]
    filenames: dict[str, str]
    chunks: int
    directory: str


async def build_index(corpus: list[CorpusDocument], directory: str) -> EvalIndex:
    """Index ``corpus`` into a fresh store under ``directory`` through the app's own path."""
    from app.services.rag.service import RAGService
    from app.services.vectordb import ChromaDB

    kwargs: dict[str, Any] = {"path": os.path.join(directory, "chroma")}
    if "lexical_path" in inspect.signature(ChromaDB.__init__).parameters:
        kwargs["lexical_path"] = os.path.join(directory, "lexical.sqlite3")
    vector_db = ChromaDB(**kwargs)
    service = RAGService(vector_db=vector_db)
    chunks = 0
    for doc in corpus:
        chunks += await service.index_document(
            document_id=doc.document_id,
            text=doc.text,
            filename=doc.filename,
            user_id=EVAL_USER,
        )
    return EvalIndex(
        service=service,
        vector_db=vector_db,
        corpus=corpus,
        filenames={d.document_id: d.filename for d in corpus},
        chunks=chunks,
        directory=directory,
    )


@contextlib.asynccontextmanager
async def isolated_index(corpus: list[CorpusDocument]):
    directory = tempfile.mkdtemp(prefix="casecite-eval-")
    try:
        yield await build_index(corpus, directory)
    finally:
        # Chroma keeps file handles open on Windows; removal is best-effort.
        shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Retrieval benchmark
# --------------------------------------------------------------------------- #
@dataclass
class RetrievalResult:
    id: str
    category: str
    evidence_ranks: list[int | None]
    retrieved: list[str] = field(default_factory=list)

    def recall_at(self, k: int) -> float:
        if not self.evidence_ranks:
            return 0.0
        return sum(1 for r in self.evidence_ranks if r is not None and r <= k) / len(
            self.evidence_ranks
        )

    def complete_at(self, k: int) -> bool:
        return bool(self.evidence_ranks) and all(
            r is not None and r <= k for r in self.evidence_ranks
        )

    @property
    def reciprocal_rank(self) -> float:
        found = [r for r in self.evidence_ranks if r is not None]
        return 1.0 / min(found) if found else 0.0


def _search_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    from app.services.rag.search import search_documents

    allowed = set(inspect.signature(search_documents).parameters)
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f"search_documents does not accept: {sorted(unknown)}")
    return options


async def run_retrieval(
    index: EvalIndex,
    questions: list[dict[str, Any]],
    top_k: int = 10,
    options: dict[str, Any] | None = None,
) -> list[RetrievalResult]:
    from app.services.rag.search import search_documents

    kwargs = _search_kwargs(dict(options or {}))
    results = []
    for q in questions:
        if not q.get("evidence"):
            continue
        found = await search_documents(
            index.vector_db,
            q["question"],
            top_k=top_k,
            user_id=EVAL_USER,
            **kwargs,
        )
        ranks = [evidence_rank(found, ev, index.filenames) for ev in q["evidence"]]
        results.append(
            RetrievalResult(
                id=q["id"],
                category=q["category"],
                evidence_ranks=ranks,
                retrieved=[
                    f"{(r.get('metadata') or {}).get('filename')}#{(r.get('metadata') or {}).get('chunk_index')}"
                    for r in found
                ],
            )
        )
    return results


def summarize_retrieval(results: list[RetrievalResult], ks=(1, 3, 5, 10)) -> dict[str, Any]:
    def block(rows: list[RetrievalResult]) -> dict[str, Any]:
        out: dict[str, Any] = {"questions": len(rows)}
        for k in ks:
            out[f"recall@{k}"] = (
                round(statistics.mean(r.recall_at(k) for r in rows), 3) if rows else 0.0
            )
        out[f"complete@{ks[-1]}"] = (
            round(statistics.mean(1.0 if r.complete_at(ks[-1]) else 0.0 for r in rows), 3)
            if rows
            else 0.0
        )
        out["mrr"] = round(statistics.mean(r.reciprocal_rank for r in rows), 3) if rows else 0.0
        out["avg_returned"] = (
            round(statistics.mean(len(r.retrieved) for r in rows), 1) if rows else 0.0
        )
        return out

    by_cat: dict[str, list[RetrievalResult]] = defaultdict(list)
    for r in results:
        by_cat[r.category].append(r)
    return {
        "overall": block(results),
        "by_category": {cat: block(rows) for cat, rows in sorted(by_cat.items())},
        "misses": [
            {"id": r.id, "ranks": r.evidence_ranks, "top": r.retrieved[:5]}
            for r in results
            if not r.complete_at(ks[-1])
        ],
    }


# --------------------------------------------------------------------------- #
# Answer benchmark
# --------------------------------------------------------------------------- #
JUDGE_PROMPT = """You grade answers produced by a legal document assistant.

QUESTION:
{question}

REFERENCE ANSWER (written by a reviewer from the documents):
{reference}

ASSISTANT ANSWER:
{answer}

Grade the assistant answer against the reference only on substance: the
outcome, the numbers, and every condition, exception or qualifier the
reference states. Wording and extra correct detail do not matter. An answer
that states the right outcome but drops a condition that changes it (a notice
period, a cure right, a threshold, an exception) is "partial".

Return STRICT JSON:
{{"grade": "correct" | "partial" | "incorrect", "missing": ["qualifier or fact the answer omitted or got wrong"], "unsupported": ["statement in the answer the reference contradicts"]}}"""


async def _judge(question: dict[str, Any], answer: str) -> dict[str, Any] | None:
    from app.config import settings
    from app.services.llm_clients import make_openai, openai_chat, utility_model

    client = make_openai(settings.openai_api_key, async_=True)
    if client is None:
        return None
    prompt = JUDGE_PROMPT.format(
        question=question["question"], reference=question["reference_answer"], answer=answer
    )
    resp = await openai_chat(
        client,
        model=os.environ.get("EVAL_JUDGE_MODEL") or utility_model(),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(resp.choices[0].message.content or "{}")
    except ValueError:
        return None


def _citation_dict(c: Any) -> dict[str, Any]:
    return c.model_dump() if hasattr(c, "model_dump") else dict(c)


async def run_answers(
    index: EvalIndex,
    questions: list[dict[str, Any]],
    settings_overrides: dict[str, Any] | None = None,
    judge: bool = False,
) -> list[dict[str, Any]]:
    from app.models.schemas import RAGSettings

    rag_settings = RAGSettings(**(settings_overrides or {}))
    rows = []
    for q in questions:
        started = time.time()
        result = await index.service.generate_response(
            query=q["question"],
            include_case_law=False,
            user_id=EVAL_USER,
            rag_settings=rag_settings,
        )
        answer = result.get("content") or ""
        citations = [_citation_dict(c) for c in result.get("citations") or []]
        claims = [c for c in citations if str(c.get("id", "")).startswith("claim-")]
        cited_docs = {c.get("source") for c in citations if c.get("was_cited_by_ai")}
        evidence_docs = {ev["doc"] for ev in q.get("evidence", [])}
        support = [c.get("support") for c in claims if c.get("support")]
        row = {
            "id": q["id"],
            "category": q["category"],
            "expect_abstain": bool(q.get("expect_abstain")),
            "facts": check_facts(answer, q),
            "evidence_docs_cited": (
                len(evidence_docs & cited_docs) / len(evidence_docs) if evidence_docs else None
            ),
            "claims": len(claims),
            "claims_verified": sum(1 for c in claims if c.get("verified")),
            "claims_supported": sum(1 for s in support if s == "supported"),
            "claims_flagged": sum(1 for s in support if s in ("contradicted", "unsupported")),
            "seconds": round(time.time() - started, 1),
            "answer": answer,
        }
        if judge and q.get("reference_answer"):
            row["judge"] = await _judge(q, answer)
        rows.append(row)
    return rows


def summarize_answers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {"questions": 0}
        out: dict[str, Any] = {
            "questions": len(items),
            "facts_ok": round(statistics.mean(1.0 if r["facts"]["ok"] else 0.0 for r in items), 3),
        }
        cited = [r["evidence_docs_cited"] for r in items if r["evidence_docs_cited"] is not None]
        if cited:
            out["evidence_docs_cited"] = round(statistics.mean(cited), 3)
        claims = sum(r["claims"] for r in items)
        if claims:
            out["claims_verified"] = round(sum(r["claims_verified"] for r in items) / claims, 3)
            judged = sum(r["claims_supported"] + r["claims_flagged"] for r in items)
            if judged:
                out["claims_flagged"] = round(sum(r["claims_flagged"] for r in items) / claims, 3)
        grades = [
            r["judge"]["grade"]
            for r in items
            if isinstance(r.get("judge"), dict) and r["judge"].get("grade")
        ]
        if grades:
            out["judge_correct"] = round(grades.count("correct") / len(grades), 3)
            out["judge_partial"] = round(grades.count("partial") / len(grades), 3)
            out["judge_incorrect"] = round(grades.count("incorrect") / len(grades), 3)
        return out

    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    return {
        "overall": block(rows),
        "by_category": {cat: block(items) for cat, items in sorted(by_cat.items())},
        "failures": [
            {
                "id": r["id"],
                "missing": r["facts"]["missing"],
                "forbidden": r["facts"]["forbidden"],
                "judge": r.get("judge"),
            }
            for r in rows
            if not r["facts"]["ok"]
            or (isinstance(r.get("judge"), dict) and r["judge"].get("grade") != "correct")
        ],
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def markdown_table(summary: dict[str, Any], compare: dict[str, Any] | None = None) -> str:
    """Render one summary block (overall + by_category) as a Markdown table."""
    rows = [("overall", summary["overall"])] + list(summary["by_category"].items())
    columns = [c for c in summary["overall"] if c != "questions"]
    header = "| slice | n | " + " | ".join(columns) + " |"
    lines = [header, "|" + "---|" * (len(columns) + 2)]
    for name, block in rows:
        cells = []
        for col in columns:
            value = block.get(col)
            cell = "" if value is None else f"{value:.3f}"
            if compare is not None and value is not None:
                old_block = (
                    compare["overall"]
                    if name == "overall"
                    else compare.get("by_category", {}).get(name, {})
                )
                old = old_block.get(col)
                if isinstance(old, (int, float)):
                    cell += f" ({value - old:+.3f})"
            cells.append(cell)
        lines.append(f"| {name} | {block.get('questions', 0)} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=_json_default), encoding="utf-8")


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return str(value)
