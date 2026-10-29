import { describe, it, expect, beforeEach } from 'vitest';
import {
  cellMatchesStatus,
  changedSinceLastRun,
  columnStats,
  compareValues,
  effectiveValue,
  filterCounts,
  loadColumnWidths,
  locateQuote,
  moveFocus,
  parseDate,
  parseDuration,
  parseMoney,
  parseNumber,
  reviewProgress,
  saveColumnWidths,
  sortKey,
} from './reviewGrid';
import type { ReviewCell } from '../api/types';

const cell = (over: Partial<ReviewCell> = {}): ReviewCell => ({
  document_id: 'd1', field_key: 'f', value: 'Delaware', quote: 'laws of Delaware', span_start: 10, span_end: 26,
  verified: true, status: 'extracted', error: null, model_used: 'gpt', updated_at: null,
  override_value: null, effective_value: 'Delaware', review_status: 'pending', note: null,
  reviewed_by: null, reviewed_at: null, history: [], ...over,
});

describe('reviewGrid helpers', () => {
  it('effectiveValue prefers the override', () => {
    expect(effectiveValue(cell())).toBe('Delaware');
    expect(effectiveValue(cell({ override_value: 'New York' }))).toBe('New York');
    expect(effectiveValue(cell({ override_value: '', effective_value: '' }))).toBe('');
    expect(effectiveValue(null)).toBeNull();
  });

  it('parses money leniently', () => {
    expect(parseMoney('USD 250,000')).toBe(250000);
    expect(parseMoney('$1.2m')).toBe(1200000);
    expect(parseMoney('£500k')).toBe(500000);
    expect(parseMoney('(500)')).toBe(-500);
    expect(parseMoney('fees paid in the prior year')).toBeNull();
    expect(parseMoney(null)).toBeNull();
  });

  it('parses numbers, dates and durations', () => {
    expect(parseNumber('about 1,250 units')).toBe(1250);
    expect(parseNumber('none')).toBeNull();
    expect(parseDate('2026-01-05')).toBe(Date.UTC(2026, 0, 5));
    expect(parseDate('January 5, 2026')).toBe(Date.UTC(2026, 0, 5));
    expect(parseDate('5 Jan 2026')).toBe(Date.UTC(2026, 0, 5));
    expect(parseDate('1/5/2026')).toBe(Date.UTC(2026, 0, 5));
    expect(parseDate('the Effective Date')).toBeNull();
    expect(parseDuration('12 months')).toBe(360);
    expect(parseDuration('thirty (30) days')).toBe(30);
    expect(parseDuration('2 years')).toBe(730);
    expect(parseDuration('perpetual')).toBeNull();
  });

  it('sorts typed values as quantities and puts blanks last', () => {
    expect(sortKey('yes', 'boolean')).toBe(1);
    expect(sortKey('No', 'boolean')).toBe(0);
    expect(sortKey('Delaware', 'text')).toBe('delaware');
    expect(compareValues('$2m', '$500k', 'money')).toBeGreaterThan(0);
    expect(compareValues('30 days', '12 months', 'duration')).toBeLessThan(0);
    expect(compareValues('2026-01-05', 'March 1, 2025', 'date')).toBeGreaterThan(0);
    expect(compareValues(null, 'x', 'text')).toBeGreaterThan(0);
    expect(compareValues('x', null, 'text')).toBeLessThan(0);
    expect(compareValues('a', 'b', 'text')).toBeLessThan(0);
  });

  it('detects a change between the last two model answers', () => {
    expect(changedSinceLastRun(cell())).toBe(false);
    const same = cell({ history: [{ at: '', by: 'u', kind: 'model', value: 'Delaware', status: 'extracted' }, { at: '', by: 'u', kind: 'approve' }, { at: '', by: 'u', kind: 'model', value: 'Delaware', status: 'extracted' }] });
    expect(changedSinceLastRun(same)).toBe(false);
    const changed = cell({ history: [{ at: '', by: 'u', kind: 'model', value: 'Delaware' }, { at: '', by: 'u', kind: 'model', value: 'Texas' }] });
    expect(changedSinceLastRun(changed)).toBe(true);
  });

  it('status filters match the extraction and review states', () => {
    expect(cellMatchesStatus(cell(), 'all')).toBe(true);
    expect(cellMatchesStatus(cell(), 'needs_review')).toBe(true);
    expect(cellMatchesStatus(cell({ review_status: 'approved' }), 'needs_review')).toBe(false);
    expect(cellMatchesStatus(cell({ status: 'not_found' }), 'needs_review')).toBe(false);
    expect(cellMatchesStatus(cell({ status: 'unverified' }), 'unverified')).toBe(true);
    expect(cellMatchesStatus(cell({ review_status: 'flagged' }), 'flagged')).toBe(true);
    expect(cellMatchesStatus(undefined, 'error')).toBe(false);
    expect(cellMatchesStatus(undefined, 'all')).toBe(true);
  });

  it('moveFocus walks the grid and clamps at the edges', () => {
    expect(moveFocus(null, 'ArrowDown', 3, 2)).toEqual({ row: 1, col: 0 });
    expect(moveFocus({ row: 0, col: 0 }, 'ArrowUp', 3, 2)).toEqual({ row: 0, col: 0 });
    expect(moveFocus({ row: 2, col: 1 }, 'ArrowRight', 3, 2)).toEqual({ row: 2, col: 1 });
    expect(moveFocus({ row: 1, col: 1 }, 'Home', 3, 2)).toEqual({ row: 1, col: 0 });
    expect(moveFocus({ row: 0, col: 0 }, 'End', 3, 5)).toEqual({ row: 0, col: 4 });
    expect(moveFocus({ row: 0, col: 0 }, 'PageDown', 30, 2)).toEqual({ row: 10, col: 0 });
    expect(moveFocus({ row: 0, col: 0 }, 'Enter', 3, 2)).toBeNull();
    expect(moveFocus({ row: 0, col: 0 }, 'ArrowDown', 0, 0)).toBeNull();
  });

  it('counts per column and overall review progress', () => {
    const cells = [
      cell({ field_key: 'f', review_status: 'approved' }),
      cell({ document_id: 'd2', field_key: 'f', status: 'unverified', override_value: 'x', review_status: 'flagged' }),
      cell({ document_id: 'd3', field_key: 'g', status: 'not_found' }),
    ];
    expect(columnStats(cells, 'f')).toEqual({ extracted: 1, unverified: 1, not_found: 0, error: 0, approved: 1, flagged: 1, overridden: 1, total: 2 });
    expect(reviewProgress(cells, 3, 2)).toEqual({ approved: 1, flagged: 1, total: 3, capacity: 6 });
    const map = new Map(cells.map((c) => [`${c.document_id}\u0000${c.field_key}`, c]));
    const counts = filterCounts(map, [{ id: 'd1' }, { id: 'd2' }, { id: 'd3' }], [{ key: 'f', name: 'F', type: 'text' }, { key: 'g', name: 'G', type: 'text' }]);
    expect(counts.all).toBe(6);
    expect(counts.unverified).toBe(1);
    expect(counts.flagged).toBe(1);
    expect(counts.approved).toBe(1);
    expect(counts.needs_review).toBe(1); // d2/f: unverified, flagged; d1/f approved; d3/g not found; rest unextracted
  });

  it('locates a quote by its span, else by searching the text', () => {
    const text = 'This Agreement is governed by the laws of Delaware. Signed.';
    expect(locateQuote(text, cell({ span_start: 34, span_end: 50 }))).toEqual({ start: 34, end: 50, exact: true });
    expect(locateQuote(text, cell({ span_start: null, span_end: null, quote: 'laws of Delaware' }))).toEqual({ start: 34, end: 50, exact: false });
    expect(locateQuote(text, cell({ span_start: null, span_end: null, quote: 'LAWS OF DELAWARE' }))).toEqual({ start: 34, end: 50, exact: false });
    expect(locateQuote(text, cell({ span_start: null, span_end: null, quote: 'nowhere at all in here' }))).toBeNull();
    // A stale span (the document was re-indexed) falls back to searching for the quote.
    expect(locateQuote(text, cell({ span_start: 900, span_end: 950 }))).toEqual({ start: 34, end: 50, exact: false });
  });

  describe('column widths', () => {
    beforeEach(() => localStorage.clear());
    it('round-trip through localStorage and tolerate bad data', () => {
      expect(loadColumnWidths('t1')).toEqual({});
      saveColumnWidths('t1', { parties: 220 });
      expect(loadColumnWidths('t1')).toEqual({ parties: 220 });
      localStorage.setItem('casecite.reviewTable.widths.t2', 'not json');
      expect(loadColumnWidths('t2')).toEqual({});
    });
  });
});
