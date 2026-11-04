"""
The accuracy benchmark's own correctness, and an offline retrieval floor.

The benchmark is only useful if its gold data is right (every evidence passage
exists verbatim in its document) and its matching is honest. The end-to-end
check runs the real indexing and search path over the seeded corpus with
deterministic offline embeddings, so a change that makes retrieval miss
evidence it used to find fails CI. It is a regression floor, not a quality
claim: hosted-embedding numbers come from ``python -m evals.run --embeddings
provider``.
"""

import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

import pytest
from evals import harness

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

# Measured 0.913 recall@10 / 0.913 complete@10 with hash embeddings when the
# hybrid + structured pipeline landed (vector-only rescoring measured 0.685).
RECALL_FLOOR = 0.85


@pytest.fixture(scope="module")
def corpus():
    return harness.load_corpus()


@pytest.fixture(scope="module")
def questions():
    return harness.load_questions()


def test_question_file_is_consistent_with_the_corpus(corpus, questions):
    assert harness.validate_questions(corpus, questions) == []
    assert len(questions) >= 40


def test_distractors_never_mention_gold_parties(corpus):
    gold_names = (
        "Northwind",
        "Brightline",
        "Calloway",
        "Ridgeway",
        "Ostrander",
        "Pelican",
        "Halvorsen",
        "Kestrel",
        "Ridgeline",
        "PX-",
    )
    for doc in corpus:
        if not doc.gold:
            assert not any(name in doc.text for name in gold_names), doc.filename


def test_evidence_coverage_tolerates_markup_and_clipping():
    passage = "| 3 | $76,385.00 | $6,365.42 |\n| 4 | $78,676.00 |"
    assert harness.evidence_coverage(passage, "| 3 | $76,385.00 | $6,365.42 |") == 1.0
    clause = "Customer shall pay each undisputed invoice within forty-five (45) days of the invoice date."
    clipped = clause[12:]  # chunk boundary cut the first words
    assert harness.evidence_coverage(clipped, clause) >= harness.EVIDENCE_MATCH
    assert harness.evidence_coverage("Rent is due monthly.", clause) == 0.0


def test_phrase_matching_is_anchored_at_word_starts():
    assert harness.phrase_present("Payments accelerate on a sale.", "accelerat*")
    assert not harness.phrase_present("Payments accelerate on a sale.", "accelerat")
    assert harness.phrase_present("No. It does not.", "No")
    assert not harness.phrase_present("Northwind may terminate.", "No")
    assert harness.check_facts("It is 45 days.", {"must_include": [["45 days", "forty-five"]]})[
        "ok"
    ]
    assert not harness.check_facts("It is 30 days.", {"must_include": [["45 days"]]})["ok"]


def test_adversarial_questions_are_present(questions):
    """The benchmark must keep asking the questions that mislead: a false
    premise, two passages that conflict, and questions the documents cannot
    answer (where the look-alike agreements tempt a guess)."""
    by_category = {}
    for q in questions:
        by_category.setdefault(q["category"], []).append(q)
    assert len(by_category.get("misleading_premise", [])) >= 6
    assert len(by_category.get("conflict", [])) >= 2
    abstain = [q for q in questions if q.get("expect_abstain")]
    assert len(abstain) >= 6
    # A forbidden phrase is checked literally, so it must be a wording only a
    # wrong answer would use; a required fact must never also be forbidden.
    for q in questions:
        forbidden = [p.lower() for p in q.get("must_not_include", [])]
        for group in q.get("must_include", []):
            for alt in group:
                assert alt.lower() not in forbidden, q["id"]
        if forbidden:
            assert harness.check_facts(q["reference_answer"], q)["forbidden"] == [], q["id"]


@pytest.mark.asyncio
async def test_offline_retrieval_meets_the_floor(corpus, questions):
    with harness.embeddings("hash"):
        async with harness.isolated_index(corpus) as index:
            results = await harness.run_retrieval(
                index, questions, top_k=10, options={"similarity_threshold": 0.001}
            )
    summary = harness.summarize_retrieval(results)["overall"]
    assert summary["recall@10"] >= RECALL_FLOOR, summary
    assert summary["complete@10"] >= RECALL_FLOOR, summary


# --------------------------------------------------------------------------- #
# The answer path, offline
# --------------------------------------------------------------------------- #
ANSWER_PATH_IDS = ("msa-cure-qualifier", "lease-late-charge", "apa-cap")


@pytest.fixture(scope="module")
def mock_provider():
    """scripts/mock_llm_server.py in a thread, with the app's chat client pointed at it."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    import mock_llm_server
    from app.services import llm_clients

    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_llm_server.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    before = llm_clients.get_runtime()["base_url"]
    llm_clients.set_runtime(base_url=f"http://127.0.0.1:{server.server_port}/v1")
    try:
        yield
    finally:
        llm_clients.set_runtime(base_url=before or "")
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_offline_answer_path_verifies_its_citations(corpus, questions, mock_provider):
    """The whole answer path with no provider key: retrieval, the answer, claim
    grounding against the documents' full text, verbatim verification and the
    support labels all run on the real code, with the deterministic stand-in
    answering as the model. The stand-in quotes the documents, so every claim
    must verify and none may be flagged. It is a plumbing check that the
    citation-verification path works and is measured; it says nothing about
    answer quality, which needs a real model (``--mode answers --judge``).
    """
    chosen = [q for q in questions if q["id"] in ANSWER_PATH_IDS]
    assert len(chosen) == len(ANSWER_PATH_IDS)
    with harness.embeddings("hash"):
        async with harness.isolated_index(corpus) as index:
            rows = await harness.run_answers(
                index,
                chosen,
                settings_overrides={"citation_verification": True},
                search_options={"similarity_threshold": 0.001},
            )
    for row in rows:
        assert row["answer"].startswith("Based on your documents:"), row
        assert row["claims"] >= 1, row
        assert row["claims_verified"] == row["claims"], row
        assert row["claims_flagged"] == 0, row
    summary = harness.summarize_answers(rows)["overall"]
    assert summary["claims_verified"] == 1.0, summary
