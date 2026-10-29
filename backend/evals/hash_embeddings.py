"""Deterministic offline embeddings for running the benchmark without an API key.

Feature-hashed word unigrams and bigrams with log term frequency, L2
normalised. Similarity therefore tracks shared vocabulary, not meaning: this
is a stand-in that lets the retrieval pipeline (chunking, fusion, ranking,
scoping) be regression-tested in CI. It says nothing about how a real
embedding model ranks paraphrases; run the benchmark with ``--embeddings
provider`` for that.
"""

from __future__ import annotations

import hashlib
import math
import re

DIMENSIONS = 768
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
    "what",
    "which",
    "does",
    "do",
    "how",
    "can",
    "under",
    "its",
    "our",
}


def _bucket(feature: str) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "little") % DIMENSIONS
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


def embed(text: str) -> list[float]:
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP]
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    for a, b in zip(words, words[1:], strict=False):
        key = f"{a} {b}"
        counts[key] = counts.get(key, 0) + 1
    vector = [0.0] * DIMENSIONS
    for feature, count in counts.items():
        index, sign = _bucket(feature)
        vector[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    vector = [v / norm for v in vector]
    if not any(vector):
        vector[0] = 1.0  # never the zero vector
    return vector
