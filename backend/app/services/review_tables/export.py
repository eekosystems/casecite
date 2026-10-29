"""Export a review table: an .xlsx workbook, a flat CSV, or JSON.

The workbook has four sheets. **Results** holds one row per document and one
column per field, showing the effective value (the reviewer's override when
there is one, else the model's); every answered cell links to its row on
**Citations**, which carries the quote, its offsets and whether it verified,
so a figure in the spreadsheet can always be traced back to the words in the
document that support it. **Review** records the human layer per cell (model
value, override, status, reviewer, note) and **Summary** counts each column's
outcomes. The options narrow what goes out: a subset of fields, reviewed
cells only, a status filter, and whether quotes are included at all.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

from app.models.review_tables import (
    CELL_ERROR,
    CELL_EXTRACTED,
    CELL_NOT_FOUND,
    CELL_UNVERIFIED,
    REVIEW_APPROVED,
    REVIEW_FLAGGED,
)

RESULTS_SHEET = "Results"
CITATIONS_SHEET = "Citations"
REVIEW_SHEET = "Review"
SUMMARY_SHEET = "Summary"
CITATION_HEADERS = (
    "Document",
    "Field",
    "Value",
    "Quote",
    "Span start",
    "Span end",
    "Verified",
    "Status",
    "Error",
)
REVIEW_HEADERS = (
    "Document",
    "Field",
    "Model value",
    "Override",
    "Effective value",
    "Review status",
    "Reviewed by",
    "Reviewed at",
    "Note",
)
SUMMARY_HEADERS = (
    "Field",
    "Verified",
    "Unverified",
    "Not found",
    "Errors",
    "Approved",
    "Flagged",
    "Overridden",
)

_STATUS_LABEL = {
    CELL_EXTRACTED: "verified",
    CELL_UNVERIFIED: "unverified",
    CELL_NOT_FOUND: "not found",
    CELL_ERROR: "error",
}
# Status names the export filter accepts, including the review-layer ones.
EXPORT_STATUSES = (*(_STATUS_LABEL), REVIEW_APPROVED, REVIEW_FLAGGED)


def effective_value(cell: dict[str, Any]) -> str | None:
    override = cell.get("override_value")
    return override if override is not None else cell.get("value")


def _display_value(cell: dict[str, Any] | None) -> str:
    if cell is None:
        return ""
    value = effective_value(cell)
    if value:
        return str(value)
    status = cell.get("status")
    if status == CELL_NOT_FOUND:
        return "—"
    if status == CELL_ERROR:
        return "error"
    return ""


def _cell_index(cells: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(c["document_id"], c["field_key"]): c for c in cells}


def _status_matches(cell: dict[str, Any], statuses: set[str]) -> bool:
    return cell.get("status") in statuses or cell.get("review_status") in statuses


def select_for_export(
    fields: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    *,
    field_keys: list[str] | None = None,
    reviewed_only: bool = False,
    statuses: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the export options: a field subset, approved cells only, a status filter."""
    if field_keys:
        wanted = set(field_keys)
        fields = [f for f in fields if f["key"] in wanted]
    keys = {f["key"] for f in fields}
    out = [c for c in cells if c["field_key"] in keys]
    if reviewed_only:
        out = [c for c in out if c.get("review_status") == REVIEW_APPROVED]
    if statuses:
        wanted_statuses = set(statuses)
        out = [c for c in out if _status_matches(c, wanted_statuses)]
    return fields, out


def _header_row(ws: Any, titles: tuple[str, ...] | list[str]) -> None:
    font = Font(bold=True)
    fill = PatternFill("solid", fgColor="E8ECF2")
    for col, title in enumerate(titles, start=1):
        c = ws.cell(row=1, column=col, value=title)
        c.font = font
        c.fill = fill


def _citation_row(doc: dict, field: dict, cell: dict, include_quotes: bool) -> list[Any]:
    return [
        doc["filename"],
        field["name"],
        cell.get("value") or "",
        (cell.get("quote") or "") if include_quotes else "",
        cell.get("span_start"),
        cell.get("span_end"),
        "yes" if cell.get("verified") else "no",
        _STATUS_LABEL.get(str(cell.get("status")), str(cell.get("status"))),
        cell.get("error") or "",
    ]


def _review_row(doc: dict, field: dict, cell: dict) -> list[Any]:
    return [
        doc["filename"],
        field["name"],
        cell.get("value") or "",
        cell.get("override_value") or "",
        effective_value(cell) or "",
        cell.get("review_status") or "pending",
        cell.get("reviewed_by") or "",
        str(cell.get("reviewed_at") or ""),
        cell.get("note") or "",
    ]


def summarise(fields: list[dict[str, Any]], cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-field counts of every outcome, in field order."""
    rows: list[dict[str, Any]] = []
    for field in fields:
        mine = [c for c in cells if c["field_key"] == field["key"]]
        rows.append(
            {
                "field": field["name"],
                "key": field["key"],
                "verified": sum(1 for c in mine if c.get("status") == CELL_EXTRACTED),
                "unverified": sum(1 for c in mine if c.get("status") == CELL_UNVERIFIED),
                "not_found": sum(1 for c in mine if c.get("status") == CELL_NOT_FOUND),
                "errors": sum(1 for c in mine if c.get("status") == CELL_ERROR),
                "approved": sum(1 for c in mine if c.get("review_status") == REVIEW_APPROVED),
                "flagged": sum(1 for c in mine if c.get("review_status") == REVIEW_FLAGGED),
                "overridden": sum(1 for c in mine if c.get("override_value") is not None),
            }
        )
    return rows


def _fill_results(
    results: Any,
    citations: Any,
    fields: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    by_key: dict[tuple[str, str], dict[str, Any]],
    include_quotes: bool,
) -> None:
    link_font = Font(color="1F4E99", underline="single")
    unverified_fill = PatternFill("solid", fgColor="FFF4D6")
    error_fill = PatternFill("solid", fgColor="FBE3E3")
    override_font = Font(color="0F766E", bold=True)
    citation_row = 2
    for r, doc in enumerate(documents, start=2):
        results.cell(row=r, column=1, value=doc["filename"])
        for col, field in enumerate(fields, start=2):
            cell = by_key.get((doc["id"], field["key"]))
            out = results.cell(row=r, column=col, value=_display_value(cell))
            if cell is None:
                continue
            if cell.get("override_value") is not None:
                out.font = override_font
            if cell.get("status") == CELL_NOT_FOUND and cell.get("override_value") is None:
                continue
            citations.append(_citation_row(doc, field, cell, include_quotes))
            out.hyperlink = Hyperlink(
                ref=out.coordinate,
                location=f"'{CITATIONS_SHEET}'!A{citation_row}",
                display=out.value,
            )
            if cell.get("override_value") is None:
                out.font = link_font
            if cell.get("status") == CELL_UNVERIFIED:
                out.fill = unverified_fill
            elif cell.get("status") == CELL_ERROR:
                out.fill = error_fill
            citation_row += 1


def render_xlsx(
    name: str,
    fields: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    *,
    include_quotes: bool = True,
) -> bytes:
    """Workbook bytes: Results (hyperlinked to) Citations, plus Review and Summary."""
    wb = Workbook()
    results = wb.active
    assert results is not None
    results.title = RESULTS_SHEET
    citations = wb.create_sheet(CITATIONS_SHEET)
    review = wb.create_sheet(REVIEW_SHEET)
    summary = wb.create_sheet(SUMMARY_SHEET)

    _header_row(results, ["Document", *[f["name"] for f in fields]])
    _header_row(citations, CITATION_HEADERS)
    _header_row(review, REVIEW_HEADERS)
    _header_row(summary, SUMMARY_HEADERS)

    by_key = _cell_index(cells)
    _fill_results(results, citations, fields, documents, by_key, include_quotes)
    for doc in documents:
        for field in fields:
            cell = by_key.get((doc["id"], field["key"]))
            if cell is not None:
                review.append(_review_row(doc, field, cell))
    for row in summarise(fields, cells):
        summary.append(
            [
                row["field"], row["verified"], row["unverified"], row["not_found"],
                row["errors"], row["approved"], row["flagged"], row["overridden"],
            ]
        )  # fmt: skip

    for ws, widths in (
        (results, [36] + [28] * len(fields)),
        (citations, [36, 24, 28, 60, 10, 10, 9, 11, 30]),
        (review, [36, 24, 28, 28, 28, 14, 24, 20, 40]),
        (summary, [28, 10, 11, 10, 8, 10, 9, 11]),
    ):
        for i, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "B2" if ws is results else "A2"
    for row in citations.iter_rows(min_row=2, min_col=4, max_col=4):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    wb.properties.title = name
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_csv(
    fields: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    *,
    include_quotes: bool = True,
) -> str:
    """Flat CSV: one row per (document, field) with value, quote, verification and review."""
    by_key = _cell_index(cells)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([*CITATION_HEADERS, "Override", "Review status", "Reviewed by", "Note"])
    for doc in documents:
        for field in fields:
            cell = by_key.get((doc["id"], field["key"]))
            if cell is None:
                writer.writerow([doc["filename"], field["name"], *([""] * 11)])
                continue
            base = _citation_row(doc, field, cell, include_quotes)
            base[4] = base[4] if base[4] is not None else ""
            base[5] = base[5] if base[5] is not None else ""
            writer.writerow(
                [
                    *base,
                    cell.get("override_value") or "",
                    cell.get("review_status") or "pending",
                    cell.get("reviewed_by") or "",
                    cell.get("note") or "",
                ]
            )
    return buf.getvalue()


def render_json(
    table: dict[str, Any],
    fields: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    *,
    include_quotes: bool = True,
) -> str:
    """A self-describing JSON document: the table, its fields, documents and cells."""
    out_cells = []
    for c in cells:
        item = {
            k: c.get(k)
            for k in (
                "document_id", "field_key", "value", "override_value", "status", "verified",
                "span_start", "span_end", "review_status", "reviewed_by", "reviewed_at",
                "note", "error", "model_used", "updated_at",
            )
        }  # fmt: skip
        item["effective_value"] = effective_value(c)
        if include_quotes:
            item["quote"] = c.get("quote")
        out_cells.append(item)
    return json.dumps(
        {
            "table": table,
            "fields": fields,
            "documents": documents,
            "cells": out_cells,
            "summary": summarise(fields, cells),
        },
        default=str,
        indent=2,
    )
