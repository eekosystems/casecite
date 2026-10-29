"""Structure-aware chunking for legal documents.

Fixed token windows cut clauses in half and carry no idea of where a passage
sits: a late-fee clause chunk says "Tenant shall pay a late charge" but never
which lease, which tenant, or which section. Retrieval then has to match the
clause to the question with half its context missing, and every lease's
late-fee clause looks the same.

This chunker:

* splits at the document's own structure — articles, numbered sections and
  clauses, schedules and exhibits, headings — and packs whole clauses into
  chunks up to the size limit, splitting only clauses that alone exceed it
  (at sentence boundaries, with overlap);
* records each chunk's section path ("ARTICLE 5 TERM AND TERMINATION > 5.2
  Termination for Cause"), its exact character span in the extracted text,
  and the pages it spans when the extractor reported page boundaries;
* builds a one-line document context (title and parties from the preamble)
  that is embedded and keyword-indexed with every chunk, so a clause is
  findable by the agreement it belongs to. The stored chunk text stays the
  verbatim source text — the context is never shown as if it were quoted.

Chunk texts are exact substrings of the source, contiguous or overlapping, so
``merge_overlapping_chunks`` still reassembles the document.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable
from dataclasses import dataclass

CHUNKER_VERSION = "structured-v1"

# Level-1 headings: the document's major divisions.
_ARTICLE = re.compile(
    r"^(?:ARTICLE|Article|PART|Part|CHAPTER|Chapter)\s+([IVXLC]+|\d+)\b[.:]?\s*(.*)$"
)
_SECTION_WORD = re.compile(r"^(?:SECTION|Section|§)\s*(\d+(?:\.\d+)*)\b[.:]?\s*(.*)$")
_ANNEX = re.compile(
    r"^(?:SCHEDULE|Schedule|EXHIBIT|Exhibit|ANNEX|Annex|APPENDIX|Appendix|ATTACHMENT|Attachment)"
    r"\s+([A-Z0-9][A-Za-z0-9.-]{0,8})\b[.:]?\s*(.*)$"
)
_MARKDOWN = re.compile(r"^#{1,6}\s+(.+)$")
# Level-2: a numbered clause at the start of a line ("5.2 Termination ...",
# "1. PURPOSE", "2.1 Base Rent.").
_NUMBERED = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?\s+(\S.*)$")
_DEFINED_TERM = re.compile(
    r'^[“"]([^”"]{1,80})[”"]\s+(?:means|shall mean|has the meaning|includes)'
)
_SENTENCE_END = re.compile(r"[.;:!?)\]”\"]\s*$")
# Sentence / sub-clause boundaries for splitting an oversized clause.
_SPLIT_POINT = re.compile(r"(?<=[.;:])\s+(?=[A-Z(“\"])|\n\s*(?=\([a-z0-9]{1,4}\)\s)")
_PARTIES = re.compile(r"\b(?:by and between|between|among|by)\b", re.IGNORECASE)


@dataclass
class _Block:
    start: int
    end: int
    level: int  # 1 = major heading, 2 = numbered clause, 3 = running text
    heading: str  # level-1 path in force for this block
    label: str  # clause label ("5.2 Termination for Cause"), may be ""


def _is_caps_heading(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3 or len(line) > 80 or len(line.split()) > 12:
        return False
    if line.rstrip().endswith(","):
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.85


def _caption(rest: str) -> str:
    """A clause's caption: its defined term, or the words before the first period."""
    term = _DEFINED_TERM.match(rest)
    if term:
        return f"“{term.group(1)}”"
    head = rest.split(". ", 1)[0].rstrip(".")
    if _is_caps_heading(rest):
        return rest.strip().rstrip(".")
    words = head.split()
    if 0 < len(words) <= 8 and head[:1].isupper():
        return head
    return ""


def _lines(text: str) -> list[tuple[int, int, str]]:
    """(start, end, stripped line) for every line, offsets into ``text``."""
    out = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        end = pos + len(raw)
        out.append((pos, end, raw.strip()))
        pos = end
    if pos < len(text):
        out.append((pos, len(text), text[pos:].strip()))
    return out


def _blocks(text: str) -> list[_Block]:
    """Split ``text`` into structural blocks with their heading paths."""
    lines = _lines(text)
    blocks: list[_Block] = []
    heading = ""
    prev = ""  # previous non-empty line
    pending_title_for: int | None = None  # index of a heading block awaiting its title line

    def _open(start: int, level: int, label: str) -> None:
        if blocks:
            blocks[-1].end = start
        blocks.append(_Block(start=start, end=len(text), level=level, heading=heading, label=label))

    for start, _end, line in lines:
        if not line:
            continue
        clean = line.lstrip("#").strip() if line.startswith("#") else line
        after_break = not prev or bool(_SENTENCE_END.search(prev)) or _is_caps_heading(prev)

        # A heading on its own line followed by its title ("ARTICLE 1" / "DEFINITIONS").
        if pending_title_for is not None and _is_caps_heading(line) and not _NUMBERED.match(line):
            heading = f"{heading} {line.strip()}".strip()
            blocks[pending_title_for].heading = heading
            pending_title_for = None
            prev = line
            continue
        pending_title_for = None

        level1 = None
        for pattern in (_ARTICLE, _ANNEX, _SECTION_WORD):
            m = pattern.match(clean)
            if m and after_break and len(clean) <= 120:
                level1 = clean if len(clean) <= 80 else clean[:80]
                if not m.group(2).strip():
                    pending_title_for = len(blocks)
                break
        if level1 is None:
            md = _MARKDOWN.match(line)
            if md:
                level1 = md.group(1).strip()
            elif after_break and _is_caps_heading(line) and not _NUMBERED.match(line):
                level1 = line

        if level1 is not None:
            # "SECTION 2. RENT 2.1 ..." style keeps the word heading; a numbered
            # top-level caps heading ("1. PURPOSE") is also level 1 below.
            heading = level1.strip().rstrip(".:")
            _open(start, 1, "")
            prev = line
            continue

        numbered = _NUMBERED.match(line)
        if numbered and after_break and numbered.group(2)[:1] not in ",;)":
            number, rest = numbered.group(1), numbered.group(2)
            if "." not in number and _is_caps_heading(rest):
                heading = f"{number}. {rest.strip().rstrip('.')}"
                _open(start, 1, "")
            else:
                caption = _caption(rest)
                _open(start, 2, f"{number} {caption}".strip())
            prev = line
            continue

        if not blocks:
            _open(start, 3, "")
        prev = line

    # Trailing whitespace belongs to no one.
    return [b for b in blocks if text[b.start : b.end].strip()]


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _section_label(blocks: list[_Block]) -> str:
    """Where a chunk sits: its heading(s), then its first and last clause."""
    headings: list[str] = []
    for b in blocks:
        if b.heading and b.heading not in headings:
            headings.append(b.heading)
    heading = "; ".join(headings)
    if len(heading) > 160:
        heading = heading[:157].rsplit(" ", 1)[0] + "..."
    labels = [b.label for b in blocks if b.label]
    if not labels:
        return heading
    span = labels[0] if len(labels) == 1 else f"{labels[0]} – {labels[-1]}"
    return f"{heading} > {span}" if heading else span


def _pages(
    page_starts: list[tuple[int, int]] | None, start: int, end: int
) -> tuple[int | None, int | None]:
    if not page_starts:
        return None, None
    offsets = [o for o, _ in page_starts]
    first = bisect.bisect_right(offsets, start) - 1
    last = bisect.bisect_right(offsets, max(start, end - 1)) - 1
    if first < 0:
        first = 0
    if last < 0:
        last = 0
    return page_starts[first][1], page_starts[last][1]


def _split_oversized(
    text: str,
    start: int,
    end: int,
    chunk_size: int,
    overlap: int,
    count_tokens: Callable[[str], int],
) -> list[tuple[int, int]]:
    """Spans covering ``text[start:end]`` at sentence boundaries, with overlap."""
    cuts = [start]
    for m in _SPLIT_POINT.finditer(text, start, end):
        cuts.append(m.end())
    cuts.append(end)
    pieces = [(a, b) for a, b in zip(cuts, cuts[1:], strict=False) if text[a:b].strip()]

    spans: list[tuple[int, int]] = []
    n = len(pieces)
    i = 0
    while i < n:
        span_start = pieces[i][0]
        j = i
        while j + 1 < n and count_tokens(text[span_start : pieces[j + 1][1]]) <= chunk_size:
            j += 1
        span_end = pieces[j][1]
        if count_tokens(text[span_start:span_end]) > chunk_size:
            # One sentence longer than a chunk: cut it by characters, in
            # proportion to its token count, with the same overlap.
            spans.extend(
                _char_windows(text, span_start, span_end, chunk_size, overlap, count_tokens)
            )
            i = j + 1
            continue
        spans.append((span_start, span_end))
        if j + 1 >= n:
            break
        # The next window repeats the trailing sentences that fit the overlap
        # budget — but only as many as still leave room for the next new
        # sentence, so every window advances.
        k = j + 1
        while (
            k - 1 > i
            and count_tokens(text[pieces[k - 1][0] : span_end]) <= overlap
            and count_tokens(text[pieces[k - 1][0] : pieces[j + 1][1]]) <= chunk_size
        ):
            k -= 1
        i = k
    return spans


def _char_windows(
    text: str, start: int, end: int, chunk_size: int, overlap: int, count_tokens
) -> list[tuple[int, int]]:
    tokens = max(1, count_tokens(text[start:end]))
    chars_per_token = (end - start) / tokens
    width = max(1, int(chunk_size * chars_per_token))
    step = max(1, int((chunk_size - min(overlap, chunk_size - 1)) * chars_per_token))
    spans = []
    pos = start
    while pos < end:
        stop = min(end, pos + width)
        if stop < end:
            # End on a word boundary when one is near, and never over budget.
            space = text.rfind(" ", pos + width // 2, stop)
            if space > pos:
                stop = space
        while stop - pos > 1 and count_tokens(text[pos:stop]) > chunk_size:
            stop = pos + max(1, int((stop - pos) * 0.95))
        spans.append((pos, stop))
        if stop >= end:
            break
        pos = max(pos + 1, min(stop, pos + step))
    return spans


def _merge_tiny(
    groups: list[tuple[int, int, list[_Block]]],
    text: str,
    chunk_size: int,
    count_tokens: Callable[[str], int],
) -> list[tuple[int, int, list[_Block]]]:
    """Fold fragments (signature blocks, a lone heading) into a neighbour.

    A chunk of a few dozen tokens is embedded almost entirely from its
    document header, so it would match every question about that document
    while carrying no answer.
    """
    tiny = max(1, chunk_size // 8)
    out: list[tuple[int, int, list[_Block]]] = []
    for start, end, blocks in groups:
        size = count_tokens(text[start:end])
        if out and size < tiny:
            prev_start, prev_end, prev_blocks = out[-1]
            if prev_end <= start and count_tokens(text[prev_start:end]) <= chunk_size:
                out[-1] = (prev_start, end, prev_blocks + blocks)
                continue
        if out:
            prev_start, prev_end, prev_blocks = out[-1]
            if count_tokens(text[prev_start:prev_end]) < tiny and prev_end <= start:
                if count_tokens(text[prev_start:end]) <= chunk_size:
                    out[-1] = (prev_start, end, prev_blocks + blocks)
                    continue
        out.append((start, end, blocks))
    return out


def chunk_document(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    count_tokens: Callable[[str], int],
    page_starts: list[tuple[int, int]] | None = None,
) -> list[dict]:
    """Structure-aware chunks of ``text``.

    ``page_starts`` is ``[(char_offset, page_number), ...]`` in ascending
    offset order, as the PDF extractor reports it. Each chunk is a dict with
    ``text`` (verbatim ``text[char_start:char_end]``), ``chunk_index``,
    ``token_count``, ``char_start``, ``char_end``, ``section``, and
    ``page_start`` / ``page_end`` (None without page information).
    """
    blocks = _blocks(text)
    if not blocks:
        return []
    min_fill = chunk_size // 4

    groups: list[tuple[int, int, list[_Block]]] = []
    current: list[_Block] = []
    current_tokens = 0

    def _flush() -> None:
        nonlocal current, current_tokens
        if current:
            groups.append((current[0].start, current[-1].end, current))
        current, current_tokens = [], 0

    for block in blocks:
        tokens = count_tokens(text[block.start : block.end])
        if current and (
            current_tokens + tokens > chunk_size
            or (block.level == 1 and current_tokens >= min_fill)
        ):
            _flush()
        if tokens > chunk_size:
            _flush()
            for a, b in _split_oversized(
                text, block.start, block.end, chunk_size, chunk_overlap, count_tokens
            ):
                groups.append((a, b, [block]))
            continue
        current.append(block)
        current_tokens += tokens
    _flush()

    groups = _merge_tiny(groups, text, chunk_size, count_tokens)

    chunks: list[dict] = []
    for start, end, group in groups:
        start, end = _trim(text, start, end)
        if start >= end:
            continue
        body = text[start:end]
        page_start, page_end = _pages(page_starts, start, end)
        chunks.append(
            {
                "text": body,
                "chunk_index": len(chunks),
                "token_count": count_tokens(body),
                "char_start": start,
                "char_end": end,
                "section": _section_label(group),
                "page_start": page_start,
                "page_end": page_end,
            }
        )
    return chunks


def document_context(text: str, filename: str = "") -> str:
    """One line saying what document this is: its title and its parties.

    Taken from the opening of the document — a heading line for the title,
    and the preamble sentence naming the parties ("... between X and Y").
    Falls back to the file name.
    """
    head = text[:3000]
    title = ""
    prose: list[str] = []
    for _start, _end, line in _lines(head):
        if not line:
            continue
        candidate = line.lstrip("#").strip()
        if _is_caps_heading(candidate) or line.startswith("#"):
            if not title and not prose:
                title = candidate.rstrip(".")
            continue
        prose.append(line)
    parties = ""
    # A sentence ends before a capital letter: "Acme Corp. (“Provider”)" and
    # "Northwind Analytics, Inc., a Delaware corporation" stay whole.
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", " ".join(prose)):
        if _PARTIES.search(sentence) and len(sentence) > 40:
            parties = " ".join(sentence.split())
            break
    if len(parties) > 320:
        parties = parties[:317].rsplit(" ", 1)[0] + "..."
    parts = [p for p in (title, parties) if p]
    if not parts and filename:
        parts = [filename.rsplit(".", 1)[0].replace("_", " ")]
    return " — ".join(parts)


def embedding_text(chunk: dict, filename: str, context: str) -> str:
    """What gets embedded for a chunk: where it sits, then its verbatim text."""
    header = [f"Document: {filename}" if filename else "", context, chunk.get("section") or ""]
    lines = [h for h in header if h]
    text = str(chunk["text"])
    return ("\n".join(lines) + "\n\n" + text) if lines else text
