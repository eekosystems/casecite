"""Tabular review: persist tables, resolve their documents, run extraction,
and keep the human review layer on top of what the model extracted.

A run is one background job. Documents are extracted concurrently under a
bounded semaphore (``settings.review_table_concurrency``); each document's
cells are written as soon as it finishes, so a partially complete run is
already visible in the grid, and the job reports progress per document.
Every run is recorded in ``review_runs`` with its scope and counts.

Writing a cell never discards the reviewer's work: an override, an approval,
a flag and the note survive a re-run. The model's new value goes into
``value``; when it differs from the previous one and the cell is not
overridden, the review status drops back to pending so someone looks again.
Every change is appended to the cell's bounded history.

Access: the owner, or a member of the table's matter, may read, run and
review it; only the owner changes its schema or scope or deletes it. A
document in the table's scope that the caller cannot read (left the matter,
deleted, never theirs) is reported as an error cell rather than silently
dropped.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.clause_intel import ClauseTagFinding, ContractAnalysisRun
from app.models.review_tables import (
    CELL_ERROR,
    CELL_NOT_FOUND,
    HISTORY_LIMIT,
    REVIEW_APPROVED,
    REVIEW_FLAGGED,
    REVIEW_PENDING,
    REVIEW_STATUSES,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    ReviewCell,
    ReviewRun,
    ReviewTable,
)
from app.models.schemas import Document
from app.services.contract_analysis.service import merge_overlapping_chunks
from app.services.documents import document_service
from app.services.job_queue import job_manager
from app.services.llm_clients import make_openai, utility_model
from app.services.rag.search import get_document_chunks_directly
from app.services.review_tables.extraction import (
    CellResult,
    ExtractionError,
    estimate_input_tokens,
    extract_fields,
)
from app.services.vectordb import get_vector_db

logger = logging.getLogger(__name__)

# Characters of indexed text a chunk holds, roughly, for the cost estimate.
_CHARS_PER_CHUNK = 4 * max(int(getattr(settings, "chunk_size", 512) or 512), 64)
_MAX_CHUNKS_PER_DOCUMENT = 4000
_OVERRIDE_MAX_CHARS = 1000
_NOTE_MAX_CHARS = 2000

# "Leave this field alone" marker for the review operations.
UNSET: Any = object()


def _history_entry(kind: str, by: str, value: str | None, status: str | None = None) -> dict:
    entry: dict[str, Any] = {"at": datetime.utcnow().isoformat(), "by": by, "kind": kind}
    if value is not None:
        entry["value"] = value[:_OVERRIDE_MAX_CHARS]
    if status is not None:
        entry["status"] = status
    return entry


def _push_history(cell: ReviewCell, entry: dict[str, Any]) -> None:
    history = list(cell.history or [])
    history.append(entry)
    cell.history = history[-HISTORY_LIMIT:]


class ReviewTableService:
    # ------------------------------------------------------------------ access

    @staticmethod
    def can_read(table: ReviewTable, user_id: str, accessible_matter_ids: set[str] | None) -> bool:
        if table.owner_id == user_id:
            return True
        return bool(
            accessible_matter_ids and table.matter_id and table.matter_id in accessible_matter_ids
        )

    @staticmethod
    def role_for(table: ReviewTable, user_id: str) -> str:
        return "owner" if table.owner_id == user_id else "member"

    # --------------------------------------------------------------- persistence

    async def create(
        self,
        session: Any,
        *,
        owner_id: str,
        matter_id: str | None,
        name: str,
        description: str | None,
        fields: list[dict[str, Any]],
        document_ids: list[str],
    ) -> ReviewTable:
        table = ReviewTable(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            matter_id=matter_id,
            name=name.strip(),
            description=(description or "").strip() or None,
            fields=fields,
            document_ids=list(dict.fromkeys(document_ids)),
        )
        session.add(table)
        await session.commit()
        await session.refresh(table)
        return table

    async def list_for_user(
        self, session: Any, user_id: str, accessible_matter_ids: set[str] | None
    ) -> list[ReviewTable]:
        clause = ReviewTable.owner_id == user_id
        if accessible_matter_ids:
            clause = clause | ReviewTable.matter_id.in_(sorted(accessible_matter_ids))
        rows = await session.execute(
            select(ReviewTable).where(clause).order_by(ReviewTable.updated_at.desc())
        )
        return list(rows.scalars().all())

    async def get_accessible(
        self,
        session: Any,
        table_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
    ) -> ReviewTable | None:
        table: ReviewTable | None = await session.get(ReviewTable, table_id)
        if table is None or not self.can_read(table, user_id, accessible_matter_ids):
            return None
        return table

    async def update(
        self,
        session: Any,
        table: ReviewTable,
        *,
        name: str | None = None,
        description: str | None = None,
        fields: list[dict[str, Any]] | None = None,
        document_ids: list[str] | None = None,
    ) -> ReviewTable:
        """Apply the given changes; cells for dropped fields or documents go too."""
        if name is not None:
            table.name = name.strip()
        if description is not None:
            table.description = description.strip() or None
        if fields is not None:
            kept_keys = {f["key"] for f in fields}
            await session.execute(
                delete(ReviewCell).where(
                    ReviewCell.table_id == table.id, ReviewCell.field_key.not_in(kept_keys)
                )
            )
            table.fields = fields
        if document_ids is not None:
            kept_docs = list(dict.fromkeys(document_ids))
            await session.execute(
                delete(ReviewCell).where(
                    ReviewCell.table_id == table.id, ReviewCell.document_id.not_in(kept_docs)
                )
            )
            table.document_ids = kept_docs
        table.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(table)
        return table

    async def add_documents(self, session: Any, table: ReviewTable, ids: list[str]) -> list[str]:
        """Append documents to the scope; returns the ids that were new."""
        present = set(table.document_ids or [])
        added = [d for d in dict.fromkeys(ids) if d not in present]
        if added:
            table.document_ids = [*(table.document_ids or []), *added]
            table.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(table)
        return added

    async def remove_document(self, session: Any, table: ReviewTable, doc_id: str) -> bool:
        """Drop one document and its cells; False when it was not in scope."""
        if doc_id not in (table.document_ids or []):
            return False
        await session.execute(
            delete(ReviewCell).where(
                ReviewCell.table_id == table.id, ReviewCell.document_id == doc_id
            )
        )
        table.document_ids = [d for d in table.document_ids if d != doc_id]
        table.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(table)
        return True

    async def delete(self, session: Any, table: ReviewTable) -> None:
        # Explicit so SQLite without foreign-key enforcement also drops the children.
        await session.execute(delete(ReviewCell).where(ReviewCell.table_id == table.id))
        await session.execute(delete(ReviewRun).where(ReviewRun.table_id == table.id))
        await session.delete(table)
        await session.commit()

    async def cells(self, session: Any, table_id: str) -> list[ReviewCell]:
        rows = await session.execute(
            select(ReviewCell)
            .where(ReviewCell.table_id == table_id)
            .order_by(ReviewCell.document_id, ReviewCell.field_key)
        )
        return list(rows.scalars().all())

    async def cell_counts(self, session: Any, table_ids: list[str]) -> dict[str, dict[str, int]]:
        """Per table: cells, approved and flagged counts."""
        if not table_ids:
            return {}
        rows = await session.execute(
            select(ReviewCell.table_id, ReviewCell.review_status, func.count(ReviewCell.id))
            .where(ReviewCell.table_id.in_(table_ids))
            .group_by(ReviewCell.table_id, ReviewCell.review_status)
        )
        out: dict[str, dict[str, int]] = {}
        for table_id, status, count in rows.all():
            bucket = out.setdefault(str(table_id), {"cells": 0, "approved": 0, "flagged": 0})
            bucket["cells"] += int(count)
            if status == REVIEW_APPROVED:
                bucket["approved"] += int(count)
            elif status == REVIEW_FLAGGED:
                bucket["flagged"] += int(count)
        return out

    # ------------------------------------------------------------------ review

    @staticmethod
    def _in_scope(table: ReviewTable, document_id: str, field_key: str) -> bool:
        return document_id in (table.document_ids or []) and any(
            f.get("key") == field_key for f in (table.fields or [])
        )

    async def _cell_for_review(
        self, session: Any, table: ReviewTable, document_id: str, field_key: str
    ) -> ReviewCell:
        if not self._in_scope(table, document_id, field_key):
            raise LookupError("That document or field is not in this table")
        cell: ReviewCell | None = (
            await session.execute(
                select(ReviewCell).where(
                    ReviewCell.table_id == table.id,
                    ReviewCell.document_id == document_id,
                    ReviewCell.field_key == field_key,
                )
            )
        ).scalar_one_or_none()
        if cell is None:
            # A reviewer may fill a cell the model has not reached yet.
            cell = ReviewCell(
                table_id=table.id,
                document_id=document_id,
                field_key=field_key,
                status=CELL_NOT_FOUND,
                verified=False,
                review_status=REVIEW_PENDING,
                history=[],
            )
            session.add(cell)
        return cell

    @staticmethod
    def _apply_review(
        cell: ReviewCell,
        *,
        reviewer: str,
        override_value: Any,
        review_status: str | None,
        note: Any,
    ) -> None:
        now = datetime.utcnow()
        if override_value is not UNSET:
            cleaned = (override_value or "").strip()[:_OVERRIDE_MAX_CHARS] or None
            if cleaned != cell.override_value:
                cell.override_value = cleaned
                _push_history(
                    cell, _history_entry("override" if cleaned else "clear", reviewer, cleaned)
                )
        if review_status is not None:
            if review_status not in REVIEW_STATUSES:
                raise ValueError(f"Unknown review status: {review_status}")
            if review_status != cell.review_status:
                cell.review_status = review_status
                kind = {REVIEW_APPROVED: "approve", REVIEW_FLAGGED: "flag"}.get(
                    review_status, "clear"
                )
                _push_history(cell, _history_entry(kind, reviewer, None, review_status))
        if note is not UNSET:
            cell.note = (note or "").strip()[:_NOTE_MAX_CHARS] or None
        cell.reviewed_by = reviewer
        cell.reviewed_at = now
        cell.updated_at = now

    async def review_cell(
        self,
        session: Any,
        table: ReviewTable,
        document_id: str,
        field_key: str,
        *,
        reviewer: str,
        override_value: Any = UNSET,
        review_status: str | None = None,
        note: Any = UNSET,
    ) -> ReviewCell:
        """Override, approve, flag or annotate one cell. Raises LookupError for
        a (document, field) outside the table and ValueError for a bad status."""
        cell = await self._cell_for_review(session, table, document_id, field_key)
        self._apply_review(
            cell,
            reviewer=reviewer,
            override_value=override_value,
            review_status=review_status,
            note=note,
        )
        await session.commit()
        await session.refresh(cell)
        return cell

    async def bulk_review(
        self,
        session: Any,
        table: ReviewTable,
        refs: list[tuple[str, str]],
        *,
        reviewer: str,
        override_value: Any = UNSET,
        review_status: str | None = None,
        note: Any = UNSET,
    ) -> list[ReviewCell]:
        """The same change on many cells in one transaction; refs outside the
        table are skipped, not fatal."""
        cells: list[ReviewCell] = []
        for document_id, field_key in dict.fromkeys(refs):
            if not self._in_scope(table, document_id, field_key):
                continue
            cell = await self._cell_for_review(session, table, document_id, field_key)
            self._apply_review(
                cell,
                reviewer=reviewer,
                override_value=override_value,
                review_status=review_status,
                note=note,
            )
            cells.append(cell)
        await session.commit()
        for cell in cells:
            await session.refresh(cell)
        return cells

    # -------------------------------------------------------------------- runs

    async def create_run(
        self,
        session: Any,
        table: ReviewTable,
        *,
        runner_id: str,
        scope: dict[str, Any],
    ) -> ReviewRun:
        run = ReviewRun(
            id=str(uuid.uuid4()),
            table_id=table.id,
            runner_id=runner_id,
            scope=scope,
            status=RUN_RUNNING,
            started_at=datetime.utcnow(),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run

    async def set_run_job(self, session: Any, run_id: str, job_id: str) -> None:
        await session.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(job_id=job_id))
        await session.commit()

    async def list_runs(self, session: Any, table_id: str, limit: int = 50) -> list[ReviewRun]:
        rows = await session.execute(
            select(ReviewRun)
            .where(ReviewRun.table_id == table_id)
            .order_by(ReviewRun.started_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def _finish_run(
        self, run_id: str | None, status: str, counts: dict[str, Any] | None, error: str | None
    ) -> None:
        if run_id is None:
            return
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(ReviewRun)
                .where(ReviewRun.id == run_id)
                .values(
                    status=status,
                    counts=counts,
                    error=(error or None) and error[:1000],
                    finished_at=datetime.utcnow(),
                )
            )
            await session.commit()

    # ---------------------------------------------------------------- documents

    async def resolve_documents(
        self, table: ReviewTable, user_id: str, accessible_matter_ids: set[str] | None
    ) -> list[tuple[str, Document | None]]:
        """The table's documents in scope order; None for one the caller cannot read."""
        out: list[tuple[str, Document | None]] = []
        for doc_id in table.document_ids or []:
            doc = await document_service.get_document(doc_id, user_id, accessible_matter_ids)
            out.append((doc_id, doc))
        return out

    async def document_text(self, doc: Document) -> str:
        """The indexed text of a document, rebuilt from its chunks in order.

        Scoped by the document's owner and matter, which the caller has already
        been checked against; a member reading a colleague's filed document
        finds the owner's chunks. Cell offsets refer to exactly this text.
        """
        vector_db = get_vector_db()
        if vector_db is None:
            raise RuntimeError("Document storage is temporarily unavailable")
        chunks = await get_document_chunks_directly(
            vector_db,
            [doc.id],
            limit=_MAX_CHUNKS_PER_DOCUMENT,
            user_id=doc.user_id,
            matter_ids=[doc.matter_id] if doc.matter_id else None,
        )
        chunks.sort(key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))
        text = merge_overlapping_chunks([c.get("text") or "" for c in chunks]).strip()
        if not text:
            raise LookupError("No indexed text is available for this document")
        return text

    async def document_tags(self, session: Any, document_id: str) -> list[dict[str, Any]]:
        """Clause spans from the document's latest completed contract analysis, if any."""
        run_id = (
            await session.execute(
                select(ContractAnalysisRun.id)
                .where(
                    ContractAnalysisRun.document_id == document_id,
                    ContractAnalysisRun.is_complete.is_(True),
                )
                .order_by(ContractAnalysisRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run_id is None:
            return []
        rows = await session.execute(
            select(ClauseTagFinding)
            .where(ClauseTagFinding.analysis_id == run_id)
            .order_by(ClauseTagFinding.span_start)
        )
        return [
            {
                "canonical_slug": t.canonical_slug,
                "span_start": t.span_start,
                "span_end": t.span_end,
            }
            for t in rows.scalars().all()
        ]

    # ----------------------------------------------------------------- estimate

    def estimate(self, documents: list[Document | None], field_count: int) -> dict[str, Any]:
        readable = [d for d in documents if d is not None]
        tokens = sum(
            estimate_input_tokens((d.chunk_count or 1) * _CHARS_PER_CHUNK, field_count)
            for d in readable
        )
        return {
            "documents": len(readable),
            "fields": field_count,
            "cells": len(readable) * field_count,
            "llm_calls": len(readable),
            "approx_input_tokens": tokens,
            "model": utility_model(),
        }

    # ---------------------------------------------------------------------- run

    @staticmethod
    def _llm_client(user_keys: Any) -> Any:
        """The runner's OpenAI-compatible client (BYOK key, else the instance key)."""
        key = user_keys.openai if user_keys and getattr(user_keys, "openai", None) else None
        client = make_openai(key or settings.openai_api_key, async_=True)
        if client is None:
            raise ValueError(
                "An OpenAI API key or a custom LLM endpoint is required. Configure one in Settings."
            )
        return client

    async def _extract_document(
        self,
        doc_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
        fields: list[dict[str, Any]],
        client: Any,
        model: str,
    ) -> tuple[dict[str, CellResult], int | None]:
        doc = await document_service.get_document(doc_id, user_id, accessible_matter_ids)
        if doc is None:
            failed = CellResult.failed("Document is not accessible or no longer exists")
            return {f["key"]: failed for f in fields}, None
        try:
            text = await self.document_text(doc)
        except (LookupError, RuntimeError, ValueError) as e:
            failed = CellResult.failed(str(e))
            return {f["key"]: failed for f in fields}, None
        except Exception as e:  # the vector store raises its own classes
            logger.warning("Review table: reading %s failed: %s", doc_id, e)
            failed = CellResult.failed("The document index could not be read")
            return {f["key"]: failed for f in fields}, None

        async with AsyncSessionLocal() as session:
            tags = await self.document_tags(session, doc_id)
        try:
            results = await extract_fields(client, text, fields, model=model, tags=tags)
        except ExtractionError as e:
            logger.warning("Review table: extraction for %s failed: %s", doc_id, e)
            failed = CellResult.failed(str(e))
            return {f["key"]: failed for f in fields}, len(text)
        return results, len(text)

    @staticmethod
    def _apply_model_result(
        cell: ReviewCell, result: CellResult, model: str, text_len: int | None, runner: str
    ) -> bool:
        """Write the model's answer into an existing cell, keeping the
        reviewer's work. Returns True when the model's value changed."""
        changed = cell.value != result.value or cell.status != result.status
        cell.value = result.value
        cell.quote = result.quote
        cell.span_start = result.span_start
        cell.span_end = result.span_end
        cell.verified = result.verified
        cell.status = result.status
        cell.error = result.error
        cell.model_used = model if result.status != CELL_ERROR else None
        cell.text_len = text_len
        cell.updated_at = datetime.utcnow()
        if changed:
            if cell.override_value is None and cell.review_status != REVIEW_PENDING:
                cell.review_status = REVIEW_PENDING
            _push_history(cell, _history_entry("model", runner, result.value, result.status))
        return changed

    async def _write_cells(
        self,
        table_id: str,
        doc_id: str,
        results: dict[str, CellResult],
        model: str,
        text_len: int | None,
        runner: str,
    ) -> int:
        """Upsert one document's cells; returns how many model values changed."""
        changed = 0
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                select(ReviewCell).where(
                    ReviewCell.table_id == table_id,
                    ReviewCell.document_id == doc_id,
                    ReviewCell.field_key.in_(list(results)),
                )
            )
            existing = {c.field_key: c for c in rows.scalars().all()}
            for key, result in results.items():
                cell = existing.get(key)
                if cell is None:
                    cell = ReviewCell(
                        table_id=table_id,
                        document_id=doc_id,
                        field_key=key,
                        review_status=REVIEW_PENDING,
                        history=[],
                    )
                    session.add(cell)
                    self._apply_model_result(cell, result, model, text_len, runner)
                elif self._apply_model_result(cell, result, model, text_len, runner):
                    changed += 1
            await session.commit()
        return changed

    async def _load_scope(
        self,
        table_id: str,
        document_ids: list[str] | None,
        field_keys: list[str] | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        async with AsyncSessionLocal() as session:
            table = await session.get(ReviewTable, table_id)
            if table is None:
                raise LookupError("Review table not found")
            fields = [
                f for f in (table.fields or []) if not field_keys or f["key"] in set(field_keys)
            ]
            doc_ids = [
                d for d in (table.document_ids or []) if not document_ids or d in set(document_ids)
            ]
        return fields, doc_ids

    async def run(
        self,
        *,
        table_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
        user_keys: Any,
        document_ids: list[str] | None = None,
        field_keys: list[str] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Extract every (document, field) in scope; the job body.

        ``document_ids`` / ``field_keys`` narrow the run to a re-run of some
        rows or columns. Cells are written per document as each completes.
        ``run_id`` names the ``review_runs`` row to finish.
        """
        try:
            result = await self._run(
                table_id, user_id, accessible_matter_ids, user_keys, document_ids, field_keys
            )
        except asyncio.CancelledError:
            await self._finish_run(run_id, RUN_CANCELLED, None, "Cancelled")
            raise
        except Exception as e:
            await self._finish_run(run_id, RUN_FAILED, None, f"{type(e).__name__}: {e}")
            raise
        await self._finish_run(run_id, RUN_COMPLETED, result, None)
        return result

    async def _run(
        self,
        table_id: str,
        user_id: str,
        accessible_matter_ids: set[str] | None,
        user_keys: Any,
        document_ids: list[str] | None,
        field_keys: list[str] | None,
    ) -> dict[str, Any]:
        fields, doc_ids = await self._load_scope(table_id, document_ids, field_keys)
        if not fields or not doc_ids:
            return {
                "documents": 0, "fields": 0, "cells": 0, "extracted": 0,
                "unverified": 0, "not_found": 0, "errors": 0, "changed": 0,
            }  # fmt: skip

        client = self._llm_client(user_keys)
        model = utility_model()
        semaphore = asyncio.Semaphore(max(1, int(settings.review_table_concurrency)))
        total = len(doc_ids)
        done = 0
        tally = {"extracted": 0, "unverified": 0, "not_found": 0, "errors": 0, "changed": 0}
        job_manager.report_progress(f"Extracting 0/{total} documents", 0.0)

        async def one(doc_id: str) -> None:
            nonlocal done
            async with semaphore:
                results, text_len = await self._extract_document(
                    doc_id, user_id, accessible_matter_ids, fields, client, model
                )
                tally["changed"] += await self._write_cells(
                    table_id, doc_id, results, model, text_len, user_id
                )
            for cell in results.values():
                if cell.status == CELL_ERROR:
                    tally["errors"] += 1
                elif cell.status == CELL_NOT_FOUND:
                    tally["not_found"] += 1
                elif cell.verified:
                    tally["extracted"] += 1
                else:
                    tally["unverified"] += 1
            done += 1
            job_manager.report_progress(f"Extracted {done}/{total} documents", done / total)

        await asyncio.gather(*(one(d) for d in doc_ids))

        async with AsyncSessionLocal() as session:
            await session.execute(
                update(ReviewTable)
                .where(ReviewTable.id == table_id)
                .values(last_run_at=datetime.utcnow(), updated_at=datetime.utcnow())
            )
            await session.commit()

        return {
            "table_id": table_id,
            "documents": total,
            "fields": len(fields),
            "cells": total * len(fields),
            **tally,
        }


review_table_service = ReviewTableService()
