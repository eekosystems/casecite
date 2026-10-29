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

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

import pytest
from evals import harness

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
