"""Run the CaseCite accuracy benchmark.

Run from ``backend/``::

    # Offline: pipeline regression check, no API key needed
    python -m evals.run

    # Real retrieval quality with the configured embedding provider
    python -m evals.run --embeddings provider --out evals/results/provider.json

    # Full answers (chat model key needed), graded against reference answers
    python -m evals.run --embeddings provider --mode all --judge

    # Compare a configuration against a saved run
    python -m evals.run --search use_lexical=false --compare evals/results/provider.json

See evals/README.md for the metrics and how to add questions.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Keep the benchmark out of the application's data directory: the app config
# is imported with throwaway secrets and a temporary data dir, exactly as the
# test suite does. Provider keys still come from the environment / .env, so a
# real key is used when one is configured.
_DATA_DIR = tempfile.mkdtemp(prefix="casecite-eval-data-")
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SECRET_KEY", "eval-only-secret-key-not-for-production-use")
os.environ.setdefault("ENCRYPTION_SALT", "eval-only-salt16")
os.environ.setdefault("UPLOAD_DIR", os.path.join(_DATA_DIR, "uploads"))
os.environ.setdefault("CHROMA_PERSIST_DIR", os.path.join(_DATA_DIR, "chroma"))
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+aiosqlite:///" + os.path.join(_DATA_DIR, "casecite.db").replace(os.sep, "/"),
)

from evals import harness  # noqa: E402


def _value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null"):
        return None
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw


def _pairs(items: list[str]) -> dict[str, Any]:
    out = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep:
            raise SystemExit(f"expected key=value, got {item!r}")
        out[key.strip()] = _value(raw.strip())
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--embeddings", choices=("hash", "local", "provider"), default="hash")
    p.add_argument("--mode", choices=("retrieval", "answers", "all"), default="retrieval")
    p.add_argument(
        "--top-k", type=int, default=10, help="passages handed to the model (default 10)"
    )
    p.add_argument("--distractors", type=int, default=105, help="generated look-alike agreements")
    p.add_argument(
        "--search", action="append", default=[], metavar="KEY=VALUE", help="search_documents option"
    )
    p.add_argument(
        "--setting", action="append", default=[], metavar="KEY=VALUE", help="RAGSettings override"
    )
    p.add_argument("--judge", action="store_true", help="grade answers against reference answers")
    p.add_argument("--only", default="", help="comma-separated question ids")
    p.add_argument("--out", type=Path, default=harness.EVALS_DIR / "results" / "latest.json")
    p.add_argument("--compare", type=Path, help="earlier report to show deltas against")
    p.add_argument("--label", default="", help="free-text label stored in the report")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    corpus = harness.load_corpus(args.distractors)
    questions = harness.load_questions()
    problems = harness.validate_questions(corpus, questions)
    if problems:
        print("Question file problems:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        questions = [q for q in questions if q["id"] in wanted]

    search_options = _pairs(args.search)
    if args.embeddings in ("hash", "local"):
        # The app's similarity floor is calibrated for the hosted embedding
        # models; the offline models' cosines sit on a different scale, so the
        # floor would cut results by calibration rather than by ranking.
        # Provider runs keep the app's real floor.
        search_options.setdefault("similarity_threshold", 0.001)
    settings_overrides = {"top_k": args.top_k, **_pairs(args.setting)}
    compare = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None

    report: dict[str, Any] = {
        "label": args.label,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "embeddings": args.embeddings,
        "top_k": args.top_k,
        "search_options": search_options,
        "settings": settings_overrides,
        "corpus": {
            "documents": len(corpus),
            "gold": sum(1 for d in corpus if d.gold),
        },
    }
    started = time.time()
    with harness.embeddings(args.embeddings) as model:
        report["embedding_model"] = model
        async with harness.isolated_index(corpus) as index:
            report["corpus"]["chunks"] = index.chunks
            if args.mode in ("retrieval", "all"):
                retrieval = await harness.run_retrieval(
                    index, questions, top_k=args.top_k, options=search_options
                )
                report["retrieval"] = harness.summarize_retrieval(retrieval)
                report["retrieval_detail"] = retrieval
            if args.mode in ("answers", "all"):
                rows = await harness.run_answers(
                    index, questions, settings_overrides=settings_overrides, judge=args.judge
                )
                report["answers"] = harness.summarize_answers(rows)
                report["answers_detail"] = rows
    report["seconds"] = round(time.time() - started, 1)

    harness.write_report(args.out, report)
    print(
        f"\nCorpus: {report['corpus']['documents']} documents, {report['corpus']['chunks']} chunks; "
        f"embeddings: {report['embedding_model']}; top_k={args.top_k}; {report['seconds']}s"
    )
    if "retrieval" in report:
        print("\nRetrieval\n")
        print(harness.markdown_table(report["retrieval"], (compare or {}).get("retrieval")))
        misses = report["retrieval"]["misses"]
        if misses:
            print(f"\nIncomplete evidence at k={args.top_k}: " + ", ".join(m["id"] for m in misses))
    if "answers" in report:
        print("\nAnswers\n")
        print(harness.markdown_table(report["answers"], (compare or {}).get("answers")))
        failures = report["answers"]["failures"]
        if failures:
            print("\nFailed: " + ", ".join(f["id"] for f in failures))
    print(f"\nReport: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
