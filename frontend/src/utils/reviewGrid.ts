/**
 * Pure helpers for the tabular-review grid: the effective value of a cell,
 * typed parsing so dates, money and durations sort as quantities, the status
 * filters, change detection against the previous run, keyboard focus moves,
 * per-column statistics and the persisted column widths.
 */
import type { ReviewCell, ReviewFieldSpec, ReviewFieldType } from '../api/types';

export type StatusFilter =
  | 'all'
  | 'unverified'
  | 'not_found'
  | 'error'
  | 'flagged'
  | 'needs_review'
  | 'approved'
  | 'changed';

export const STATUS_FILTERS: Array<{ key: StatusFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'needs_review', label: 'Needs review' },
  { key: 'unverified', label: 'Unverified' },
  { key: 'not_found', label: 'Not found' },
  { key: 'error', label: 'Errors' },
  { key: 'flagged', label: 'Flagged' },
  { key: 'approved', label: 'Approved' },
  { key: 'changed', label: 'Changed' },
];

/** The value the reviewer sees: the override when set, else the model's. */
export function effectiveValue(cell: ReviewCell | null | undefined): string | null {
  if (!cell) return null;
  if (cell.override_value !== null && cell.override_value !== undefined) return cell.override_value;
  return cell.effective_value ?? cell.value ?? null;
}

const MULTIPLIERS: Record<string, number> = { k: 1e3, m: 1e6, mm: 1e6, b: 1e9, bn: 1e9 };

/** "USD 250,000", "$1.2m", "(500)" → a number; null when there is none. */
export function parseMoney(value: string | null | undefined): number | null {
  if (!value) return null;
  const cleaned = value.replace(/[,\s]/g, '').replace(/[A-Za-z]{3}(?=\d|[$€£])/g, '');
  const m = cleaned.match(/(-|\()?[$€£]?(\d+(?:\.\d+)?)\s*(k|mm|m|bn|b)?/i);
  if (!m) return null;
  const negative = Boolean(m[1]);
  const amount = parseFloat(m[2]) * (m[3] ? MULTIPLIERS[m[3].toLowerCase()] || 1 : 1);
  return negative ? -amount : amount;
}

export function parseNumber(value: string | null | undefined): number | null {
  if (!value) return null;
  const m = value.replace(/,/g, '').match(/-?\d+(?:\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
}

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];

/** ISO, "January 5, 2026", "5 Jan 2026" or "1/5/2026" → epoch ms; null otherwise. */
export function parseDate(value: string | null | undefined): number | null {
  if (!value) return null;
  const text = value.trim();
  const iso = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) return Date.UTC(+iso[1], +iso[2] - 1, +iso[3]);
  const monthFirst = text.match(/([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})/);
  const dayFirst = text.match(/(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})/);
  const named = monthFirst
    ? { month: monthFirst[1], day: +monthFirst[2], year: +monthFirst[3] }
    : dayFirst ? { month: dayFirst[2], day: +dayFirst[1], year: +dayFirst[3] } : null;
  if (named) {
    const mi = MONTHS.indexOf(named.month.slice(0, 3).toLowerCase());
    if (mi >= 0) return Date.UTC(named.year, mi, named.day);
  }
  const slash = text.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (slash) return Date.UTC(+slash[3], +slash[1] - 1, +slash[2]);
  const parsed = Date.parse(text);
  return Number.isNaN(parsed) ? null : parsed;
}

const UNIT_DAYS: Record<string, number> = {
  day: 1, days: 1, week: 7, weeks: 7, month: 30, months: 30, year: 365, years: 365, yr: 365, yrs: 365, mo: 30, mos: 30,
};
const WORDS: Record<string, number> = {
  one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, twelve: 12, eighteen: 18, twenty: 20, thirty: 30, sixty: 60, ninety: 90,
};

/** "12 months", "thirty (30) days", "2 years" → days; null otherwise. */
export function parseDuration(value: string | null | undefined): number | null {
  if (!value) return null;
  const text = value.toLowerCase();
  const m = text.match(/(\d+(?:\.\d+)?|[a-z]+)\s*(?:\(\d+\)\s*)?(days?|weeks?|months?|years?|yrs?|mos?)\b/);
  if (!m) return null;
  const n = /^\d/.test(m[1]) ? parseFloat(m[1]) : WORDS[m[1]];
  if (n === undefined || Number.isNaN(n)) return null;
  return n * (UNIT_DAYS[m[2]] || 1);
}

/** The key a value sorts by for its field type: a number where the type is a quantity. */
export function sortKey(value: string | null, type: ReviewFieldType): number | string | null {
  if (value === null || value === undefined || value === '') return null;
  switch (type) {
    case 'money': return parseMoney(value);
    case 'number': return parseNumber(value);
    case 'date': return parseDate(value);
    case 'duration': return parseDuration(value);
    case 'boolean': return /^(yes|true|y)\b/i.test(value) ? 1 : /^(no|false|n)\b/i.test(value) ? 0 : null;
    default: return value.toLowerCase();
  }
}

/** Ascending comparison; empty values sort last whatever the direction. */
export function compareValues(a: string | null, b: string | null, type: ReviewFieldType): number {
  const ka = sortKey(a, type);
  const kb = sortKey(b, type);
  if (ka === null && kb === null) return (a || '').localeCompare(b || '');
  if (ka === null) return 1;
  if (kb === null) return -1;
  if (typeof ka === 'number' && typeof kb === 'number') return ka - kb;
  return String(ka).localeCompare(String(kb));
}

/** True when the last two model answers for the cell differ. */
export function changedSinceLastRun(cell: ReviewCell | null | undefined): boolean {
  if (!cell) return false;
  const models = (cell.history || []).filter((h) => h.kind === 'model');
  if (models.length < 2) return false;
  const [prev, last] = models.slice(-2);
  return (prev.value ?? null) !== (last.value ?? null) || (prev.status ?? null) !== (last.status ?? null);
}

export function cellMatchesStatus(cell: ReviewCell | null | undefined, filter: StatusFilter): boolean {
  switch (filter) {
    case 'all': return true;
    case 'unverified': return cell?.status === 'unverified';
    case 'not_found': return cell?.status === 'not_found';
    case 'error': return cell?.status === 'error';
    case 'flagged': return cell?.review_status === 'flagged';
    case 'approved': return cell?.review_status === 'approved';
    case 'needs_review': return Boolean(cell) && cell!.review_status !== 'approved' && cell!.status !== 'not_found';
    case 'changed': return changedSinceLastRun(cell);
    default: return true;
  }
}

export interface GridFocus {
  row: number;
  col: number;
}

/** Where an arrow / Home / End / Page key moves the focused cell; null for other keys. */
export function moveFocus(
  focus: GridFocus | null, key: string, rowCount: number, colCount: number, pageSize = 10,
): GridFocus | null {
  if (rowCount === 0 || colCount === 0) return null;
  const cur = focus ?? { row: 0, col: 0 };
  const clamp = (f: GridFocus): GridFocus => ({
    row: Math.min(Math.max(f.row, 0), rowCount - 1),
    col: Math.min(Math.max(f.col, 0), colCount - 1),
  });
  switch (key) {
    case 'ArrowUp': return clamp({ ...cur, row: cur.row - 1 });
    case 'ArrowDown': return clamp({ ...cur, row: cur.row + 1 });
    case 'ArrowLeft': return clamp({ ...cur, col: cur.col - 1 });
    case 'ArrowRight': return clamp({ ...cur, col: cur.col + 1 });
    case 'Home': return clamp({ ...cur, col: 0 });
    case 'End': return clamp({ ...cur, col: colCount - 1 });
    case 'PageUp': return clamp({ ...cur, row: cur.row - pageSize });
    case 'PageDown': return clamp({ ...cur, row: cur.row + pageSize });
    default: return null;
  }
}

export interface ColumnStats {
  extracted: number;
  unverified: number;
  not_found: number;
  error: number;
  approved: number;
  flagged: number;
  overridden: number;
  total: number;
}

export function columnStats(cells: Iterable<ReviewCell>, fieldKey: string): ColumnStats {
  const stats: ColumnStats = { extracted: 0, unverified: 0, not_found: 0, error: 0, approved: 0, flagged: 0, overridden: 0, total: 0 };
  for (const c of cells) {
    if (c.field_key !== fieldKey) continue;
    stats.total += 1;
    if (c.status === 'extracted') stats.extracted += 1;
    else if (c.status === 'unverified') stats.unverified += 1;
    else if (c.status === 'not_found') stats.not_found += 1;
    else if (c.status === 'error') stats.error += 1;
    if (c.review_status === 'approved') stats.approved += 1;
    if (c.review_status === 'flagged') stats.flagged += 1;
    if (c.override_value !== null && c.override_value !== undefined) stats.overridden += 1;
  }
  return stats;
}

export interface ReviewProgress {
  approved: number;
  flagged: number;
  total: number;
  /** Cells the grid can hold: documents × fields, so unextracted cells count as outstanding. */
  capacity: number;
}

export function reviewProgress(cells: Iterable<ReviewCell>, documents: number, fields: number): ReviewProgress {
  let approved = 0;
  let flagged = 0;
  let total = 0;
  for (const c of cells) {
    total += 1;
    if (c.review_status === 'approved') approved += 1;
    if (c.review_status === 'flagged') flagged += 1;
  }
  return { approved, flagged, total, capacity: documents * fields };
}

/** Counts per status filter, for the chips. */
export function filterCounts(
  cells: Map<string, ReviewCell>, documents: Array<{ id: string }>, fields: ReviewFieldSpec[],
): Record<StatusFilter, number> {
  const counts = Object.fromEntries(STATUS_FILTERS.map((f) => [f.key, 0])) as Record<StatusFilter, number>;
  for (const doc of documents) {
    for (const f of fields) {
      const cell = cells.get(`${doc.id}\u0000${f.key || f.name}`);
      for (const { key } of STATUS_FILTERS) {
        if (cellMatchesStatus(cell, key)) counts[key] += 1;
      }
    }
  }
  return counts;
}

const WIDTHS_KEY = (tableId: string) => `casecite.reviewTable.widths.${tableId}`;

export function loadColumnWidths(tableId: string): Record<string, number> {
  try {
    const raw = localStorage.getItem(WIDTHS_KEY(tableId));
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function saveColumnWidths(tableId: string, widths: Record<string, number>): void {
  try {
    localStorage.setItem(WIDTHS_KEY(tableId), JSON.stringify(widths));
  } catch {
    // Storage may be unavailable (private window, quota); widths are a convenience.
  }
}

/** Where a quote sits in the document text: the stored span, else a lenient search. */
export function locateQuote(text: string, cell: ReviewCell | null | undefined): { start: number; end: number; exact: boolean } | null {
  if (!cell) return null;
  if (cell.span_start !== null && cell.span_end !== null && cell.span_end > cell.span_start && cell.span_end <= text.length) {
    return { start: cell.span_start, end: cell.span_end, exact: true };
  }
  if (!cell.quote) return null;
  const needle = cell.quote.trim();
  let idx = text.indexOf(needle);
  if (idx < 0) idx = text.toLowerCase().indexOf(needle.toLowerCase());
  if (idx < 0) {
    const head = needle.split(/\s+/).slice(0, 6).join(' ');
    if (head.length >= 12) idx = text.toLowerCase().indexOf(head.toLowerCase());
    if (idx < 0) return null;
    return { start: idx, end: Math.min(text.length, idx + needle.length), exact: false };
  }
  return { start: idx, end: idx + needle.length, exact: false };
}
