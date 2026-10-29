"""Full-corpus keyword index (SQLite FTS5, BM25-ranked) beside the vector store.

Vector search ranks passages by meaning. It reliably misses passages whose
match is lexical: an unusual defined term ("Liquidity Event"), a clause
number ("Section 11.3(c)"), a party or product name, an amount. Rescoring the
vector candidates with BM25 cannot help, because a passage outside the vector
candidate pool is never scored at all. This index searches every chunk the
caller may see by keyword, independently, so the two candidate lists can be
fused (see ``app.services.rag.search``).

It mirrors the vector store: the store's write paths (add, delete, re-tag)
update it, and startup reconciliation copies in any indexed document it is
missing, from the chunks already stored (nothing is re-embedded). It is an
accuracy enhancement, not a source of truth: a failure here is logged and
search falls back to vector-only ranking.

Tenant isolation matches the vector store's: every query must carry a
user_id / matter_id scope, and filters it cannot express exactly are refused
rather than dropped (dropping a filter would widen the search).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# Text weight vs heading (file name + section path) weight in BM25.
_BM25_WEIGHTS = (1.0, 0.6)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    rowid INTEGER PRIMARY KEY,
    chunk_id TEXT NOT NULL UNIQUE,
    document_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    matter_id TEXT NOT NULL DEFAULT '',
    heading TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS ix_chunks_user ON chunks(user_id);
CREATE INDEX IF NOT EXISTS ix_chunks_matter ON chunks(matter_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading,
    content='chunks', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.rowid, new.text, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading)
    VALUES ('delete', old.rowid, old.text, old.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text, heading ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading)
    VALUES ('delete', old.rowid, old.text, old.heading);
    INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.rowid, new.text, new.heading);
END;
"""

# Columns a filter may reference directly; other keys are read from the
# stored metadata JSON. Anything else is refused.
_COLUMNS = {"user_id", "matter_id", "document_id"}
_METADATA_KEYS = {"source", "doc_type", "folder_path", "filename", "chunker"}

# Words that carry no keyword signal in a question. Porter-stemmed matching
# makes "terminate"/"termination" one term already.
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "tell",
        "show",
        "give",
        "find",
        "list",
        "explain",
        "describe",
        "say",
        "says",
        "said",
        "happen",
        "happens",
        "happened",
        "shall",
        "may",
        "must",
        "might",
        "whether",
        "ever",
    ]
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# A clause reference with a sub-clause: "11.3(c)", "7.1(b)(ii)".
_CLAUSE_REF = re.compile(r"^(\d+(?:\.\d+)+)\(")


class UnsupportedFilter(ValueError):
    """The filter uses an operator or key this index cannot evaluate exactly."""


def build_match_query(query: str, phrases: Iterable[str] = ()) -> str | None:
    """An FTS5 MATCH expression for a natural-language question.

    Every content word becomes an OR'ed term; anything typed as one unit but
    tokenized into several pieces ("11.3(c)", "$250,000", "PX-200", "99.9%")
    becomes a phrase so it matches only as that exact sequence. Quoted phrases
    from the query are kept as phrases. Every piece is double-quoted, so FTS5
    syntax characters in user text are inert. None when nothing is left.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def _add(tokens: list[str]) -> None:
        if not tokens:
            return
        expr = '"' + " ".join(tokens) + '"'
        if expr not in seen:
            seen.add(expr)
            parts.append(expr)

    for phrase in phrases:
        _add([t.lower() for t in _WORD.findall(phrase)])

    for raw in query.split():
        tokens = [t.lower() for t in _WORD.findall(raw)]
        if len(tokens) > 1:
            _add(tokens)
            # "11.3(c)" is written "11.3 Notices. ... (c) ..." in the document:
            # the sub-clause letter is not adjacent to the section number, so
            # the section number is searched on its own as well.
            clause = _CLAUSE_REF.match(raw.lstrip("§("))
            if clause:
                _add(_WORD.findall(clause.group(1)))
            continue
        if not tokens:
            continue
        token = tokens[0]
        if token in _STOPWORDS:
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        _add([token])
    return " OR ".join(parts) if parts else None


def compile_filter(filter: dict | None) -> tuple[str, list[Any]]:
    """Translate a vector-store style filter into SQL over ``chunks c``.

    Supports equality, ``$eq``, ``$in``, ``$and`` and ``$or``. Raises
    UnsupportedFilter for anything else, so a filter is never silently
    ignored.
    """
    if not filter:
        return "1=1", []
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in filter.items():
        if key in ("$and", "$or"):
            if not isinstance(value, list) or not value:
                raise UnsupportedFilter(f"{key} needs a non-empty list")
            subs = []
            for cond in value:
                sql, sub_params = compile_filter(cond)
                subs.append(f"({sql})")
                params.extend(sub_params)
            clauses.append((" AND " if key == "$and" else " OR ").join(subs))
            continue
        if key in _COLUMNS:
            column = f"c.{key}"
        elif key in _METADATA_KEYS:
            column = f"json_extract(c.metadata, '$.{key}')"
        else:
            raise UnsupportedFilter(f"cannot filter on {key!r}")
        if isinstance(value, dict):
            if set(value) == {"$in"} and isinstance(value["$in"], list):
                if not value["$in"]:
                    clauses.append("0=1")
                    continue
                clauses.append(f"{column} IN ({','.join('?' * len(value['$in']))})")
                params.extend(value["$in"])
            elif set(value) == {"$eq"}:
                clauses.append(f"{column} = ?")
                params.append(value["$eq"])
            else:
                raise UnsupportedFilter(f"unsupported operator in {value!r}")
        else:
            clauses.append(f"{column} = ?")
            params.append(value)
    return " AND ".join(f"({c})" for c in clauses) or "1=1", params


def chunk_heading(metadata: dict[str, Any]) -> str:
    """What a chunk is part of: file name, document context, section path.

    Indexed (at a lower BM25 weight) alongside the chunk text, so "the
    Calloway lease late charge" matches the late-charge clause of that lease
    even though the clause itself never names the tenant.
    """
    return " ".join(
        str(metadata.get(k) or "")
        for k in ("filename", "doc_context", "section")
        if metadata.get(k)
    )


class LexicalIndex:
    """One SQLite file holding chunk text and its FTS5 index."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -----------------------------------------------------------
    def add(self, chunks: Iterable[dict[str, Any]]) -> int:
        """Insert or replace chunks: ``{"id", "text", "metadata"}`` dicts.

        ``metadata`` must carry the tenant fields (user_id, optionally
        matter_id) and document_id, as the vector store's metadata does.
        """
        rows = []
        for chunk in chunks:
            meta = dict(chunk.get("metadata") or {})
            if not meta.get("user_id"):
                raise ValueError("user_id is required for lexical index entries")
            rows.append(
                (
                    str(chunk["id"]),
                    str(meta.get("document_id") or ""),
                    str(meta["user_id"]),
                    str(meta.get("matter_id") or ""),
                    chunk_heading(meta),
                    json.dumps(meta, default=str),
                    chunk.get("text") or "",
                )
            )
        if not rows:
            return 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                # REPLACE would delete without firing the FTS delete trigger
                # (recursive triggers are off), leaving stale index entries.
                self._conn.executemany(
                    "DELETE FROM chunks WHERE chunk_id = ?", [(r[0],) for r in rows]
                )
                self._conn.executemany(
                    "INSERT INTO chunks (chunk_id, document_id, user_id, matter_id, heading, "
                    "metadata, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    def delete_ids(self, ids: Iterable[str]) -> None:
        with self._lock:
            self._conn.executemany("DELETE FROM chunks WHERE chunk_id = ?", [(i,) for i in ids])

    def delete_document(self, document_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM chunks")

    def update_matter(self, document_id: str, matter_id: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE chunks SET matter_id = ?, metadata = json_set(metadata, '$.matter_id', ?) "
                "WHERE document_id = ?",
                (matter_id or "", matter_id or "", document_id),
            )

    # -- reads ------------------------------------------------------------
    def has_document(self, document_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM chunks WHERE document_id = ? LIMIT 1", (document_id,)
            ).fetchone()
        return row is not None

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def search(
        self,
        query: str,
        filter: dict | None,
        limit: int = 30,
        phrases: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """BM25-ranked chunks matching ``query`` within ``filter``.

        Results carry ``bm25`` (higher is better) and ``lexical_rank``. They
        have no ``similarity``: a keyword score is not a cosine and must not
        be presented as one.
        """
        from app.services.vectordb import _has_tenant_scope

        if not _has_tenant_scope(filter):
            raise ValueError("a user_id or matter_id filter is required for keyword search")
        match = build_match_query(query, phrases)
        if not match:
            return []
        where, params = compile_filter(filter)
        sql = (
            "SELECT c.chunk_id, c.text, c.metadata, bm25(chunks_fts, ?, ?) AS score "
            "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            f"WHERE chunks_fts MATCH ? AND ({where}) ORDER BY score LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*_BM25_WEIGHTS, match, *params, limit]).fetchall()
        results = []
        for rank, (chunk_id, text, metadata, score) in enumerate(rows, start=1):
            try:
                meta = json.loads(metadata)
            except ValueError:
                meta = {}
            results.append(
                {
                    "id": chunk_id,
                    "text": text,
                    "metadata": meta,
                    # FTS5's bm25() is negative, lower is better.
                    "bm25": -float(score),
                    "lexical_rank": rank,
                }
            )
        return results


def open_lexical_index(path: str | None) -> LexicalIndex | None:
    """Open the index, or None (logged) when it cannot be opened.

    A missing FTS5 build, a read-only volume or a corrupt file degrades search
    to vector-only; it must not stop the vector store from starting.
    """
    if not path:
        return None
    try:
        return LexicalIndex(path)
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Keyword index unavailable at {path} ({e}); search is vector-only.")
        return None
