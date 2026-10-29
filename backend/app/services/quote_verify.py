"""
Verbatim quote verification.

The gate every AI-cited passage passes before it is shown as fact: a quote the
model claims to have copied from a source must be located in that source's
text, tolerant only of whitespace differences. A quote that cannot be found is
treated as unverified. This is the mechanism that catches hallucinated
citations instead of shipping them, shared by contract analysis, tabular
review, deep research, the strategy brief and the per-file RAG answer.
"""

from __future__ import annotations

import re


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def find_quote_offset(haystack: str, needle: str) -> tuple[int, int] | None:
    """Locate ``needle`` in ``haystack``, tolerant of whitespace differences.

    Returns (start, end) offsets into the ORIGINAL haystack, or None if the
    quote cannot be located. This is the verification gate: a quote that cannot
    be found (modulo whitespace) in the source is treated as unverified — the
    mechanism that catches hallucinated citations instead of shipping them.
    """
    if not haystack or not needle:
        return None
    idx = haystack.find(needle)
    if idx >= 0:
        return (idx, idx + len(needle))
    norm = _normalize_ws(needle)
    if len(norm) < 12:  # too short to verify confidently
        return None
    pattern = r"\s+".join(re.escape(tok) for tok in norm.split(" "))
    try:
        m = re.search(pattern, haystack)
    except re.error:
        return None
    return (m.start(), m.end()) if m else None
