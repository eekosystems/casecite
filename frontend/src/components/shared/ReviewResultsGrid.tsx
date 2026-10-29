import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { cellKey, type CellRef, type SortState } from '../../stores/reviewTablesStore';
import { changedSinceLastRun, effectiveValue, type GridFocus } from '../../utils/reviewGrid';
import type { ReviewCell, ReviewCellReviewInput, ReviewFieldSpec, ReviewTableDocument } from '../../api/types';

interface ReviewResultsGridProps {
  fields: ReviewFieldSpec[];
  /** The rows to show, already filtered and sorted. */
  documents: ReviewTableDocument[];
  cells: Map<string, ReviewCell>;
  selected: CellRef | null;
  selection: Set<string>;
  focus: GridFocus | null;
  sort: SortState;
  columnFilters: Record<string, string>;
  columnWidths: Record<string, number>;
  running: boolean;
  canEditScope: boolean;
  onSelect: (ref: CellRef | null) => void;
  onEdit: (ref: CellRef) => void;
  onToggleSelected: (ref: CellRef) => void;
  onSelectRow: (documentId: string, on: boolean) => void;
  onSelectColumn: (fieldKey: string, on: boolean) => void;
  onFocus: (focus: GridFocus | null) => void;
  onKeyNav: (key: string, rowCount: number, colCount: number) => GridFocus | null;
  onToggleSort: (fieldKey: string) => void;
  onColumnFilter: (fieldKey: string, value: string) => void;
  onColumnWidth: (fieldKey: string, width: number) => void;
  onReview: (ref: CellRef, change: ReviewCellReviewInput) => void;
  onRerunRow: (documentId: string) => void;
  onRerunColumn: (fieldKey: string) => void;
  onApproveColumn: (fieldKey: string) => void;
  onRemoveRow: (documentId: string) => void;
  styles: Record<string, string>;
}

const ROW_HEIGHT = 36;
const OVERSCAN = 8;

function cellText(cell: ReviewCell | undefined): string {
  if (!cell) return '';
  const value = effectiveValue(cell);
  if (value) return value;
  if (cell.status === 'not_found') return '—';
  if (cell.status === 'error') return 'error';
  return '';
}

function cellClass(cell: ReviewCell | undefined, styles: Record<string, string>): string {
  if (!cell) return styles.cellEmpty;
  const parts: string[] = [];
  if (cell.status === 'extracted') parts.push(styles.cellVerified);
  else if (cell.status === 'unverified') parts.push(styles.cellUnverified);
  else if (cell.status === 'error') parts.push(styles.cellError);
  else parts.push(styles.cellNotFound);
  if (cell.review_status === 'approved') parts.push(styles.cellApproved);
  if (cell.review_status === 'flagged') parts.push(styles.cellFlagged);
  if (cell.override_value !== null && cell.override_value !== undefined) parts.push(styles.cellOverridden);
  return parts.join(' ');
}

/** Rows visible in the scroll window, so a table of hundreds of documents stays light. */
function useRowWindow(total: number) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    setHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);
  if (height === 0 || total * ROW_HEIGHT <= height) return { ref, start: 0, end: total, onScroll: undefined as undefined | (() => void) };
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(total, Math.ceil((scrollTop + height) / ROW_HEIGHT) + OVERSCAN);
  return { ref, start, end, onScroll: () => setScrollTop(ref.current?.scrollTop || 0) };
}

/** Documents down, fields across: a reviewable grid with sort, filter, selection and keyboard navigation. */
export function ReviewResultsGrid({
  fields, documents, cells, selected, selection, focus, sort, columnFilters, columnWidths, running, canEditScope,
  onSelect, onEdit, onToggleSelected, onSelectRow, onSelectColumn, onFocus, onKeyNav, onToggleSort, onColumnFilter,
  onColumnWidth, onReview, onRerunRow, onRerunColumn, onApproveColumn, onRemoveRow, styles,
}: ReviewResultsGridProps) {
  const { ref: wrapRef, start, end, onScroll } = useRowWindow(documents.length);
  const keyOf = (f: ReviewFieldSpec) => f.key || f.name;

  // Keep the focused cell in view when the keyboard moves it.
  useEffect(() => {
    if (!focus) return;
    const el = wrapRef.current?.querySelector('[data-focused="true"]');
    el?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [focus, wrapRef]);

  const refAt = (f: GridFocus): CellRef | null => {
    const doc = documents[f.row];
    const field = fields[f.col];
    return doc && field ? { documentId: doc.id, fieldKey: keyOf(field) } : null;
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).tagName === 'INPUT') return;
    const moved = onKeyNav(e.key, documents.length, fields.length);
    if (moved) { e.preventDefault(); return; }
    const ref = focus ? refAt(focus) : null;
    if (e.key === 'Escape') { onSelect(null); return; }
    if (!ref) return;
    const k = e.key.toLowerCase();
    if (e.key === 'Enter') { e.preventDefault(); onSelect(ref); }
    else if (e.key === ' ') { e.preventDefault(); onToggleSelected(ref); }
    else if (k === 'e') { e.preventDefault(); onEdit(ref); }
    else if (k === 'a') { e.preventDefault(); onReview(ref, { review_status: 'approved' }); }
    else if (k === 'f') { e.preventDefault(); onReview(ref, { review_status: 'flagged' }); }
  };

  const startResize = (fieldKey: string, e: React.MouseEvent<HTMLSpanElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const th = (e.currentTarget as HTMLElement).closest('th') as HTMLElement | null;
    const startX = e.clientX;
    const startWidth = th?.getBoundingClientRect().width || columnWidths[fieldKey] || 160;
    const move = (ev: MouseEvent) => onColumnWidth(fieldKey, startWidth + ev.clientX - startX);
    const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  };

  const columnAllSelected = (fieldKey: string) =>
    documents.length > 0 && documents.every((d) => selection.has(cellKey(d.id, fieldKey)));
  const rowAllSelected = (docId: string) =>
    fields.length > 0 && fields.every((f) => selection.has(cellKey(docId, keyOf(f))));
  const widthStyle = (key: string, fallback?: number) =>
    columnWidths[key] ? { width: columnWidths[key], minWidth: columnWidths[key] } : fallback ? { minWidth: fallback } : undefined;

  return (
    <div
      ref={wrapRef}
      className={styles.gridWrap}
      onScroll={onScroll}
      tabIndex={0}
      role="grid"
      aria-label="Review results"
      aria-rowcount={documents.length}
      onKeyDown={onKeyDown}
    >
      <table className={styles.grid}>
        <thead>
          <tr>
            <th className={`${styles.th} ${styles.thDoc}`} style={widthStyle('__document')}>
              <div className={styles.thInner}>
                <button type="button" className={styles.sortBtn} onClick={() => onToggleSort('__document')} title="Sort by document">
                  <span className={styles.thName}>Document</span>
                  <Icon name={sort.key === '__document' ? (sort.dir === 'asc' ? 'ArrowUp' : 'ArrowDown') : 'ArrowUpDown'} size={11} className={sort.key === '__document' ? styles.sortActive : styles.sortIdle} />
                </button>
              </div>
              <span className={styles.resizeHandle} onMouseDown={(e) => startResize('__document', e)} aria-hidden="true" />
            </th>
            {fields.map((f) => {
              const key = keyOf(f);
              const sorted = sort.key === key;
              return (
                <th key={key} className={styles.th} style={widthStyle(key)} aria-sort={sorted ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                  <div className={styles.thInner}>
                    <input
                      type="checkbox"
                      className={styles.checkbox}
                      checked={columnAllSelected(key)}
                      onChange={(e) => onSelectColumn(key, e.target.checked)}
                      aria-label={`Select column ${f.name}`}
                    />
                    <button type="button" className={styles.sortBtn} onClick={() => onToggleSort(key)} title={f.description || `Sort by ${f.name}`}>
                      <span className={styles.thName}>{f.name}</span>
                      <Icon name={sorted ? (sort.dir === 'asc' ? 'ArrowUp' : 'ArrowDown') : 'ArrowUpDown'} size={11} className={sorted ? styles.sortActive : styles.sortIdle} />
                    </button>
                    <span className={styles.thTools}>
                      <button type="button" className={styles.thBtn} onClick={() => onApproveColumn(key)} disabled={running || documents.length === 0} title={`Approve every "${f.name}" value`} aria-label={`Approve column ${f.name}`}>
                        <Icon name="ClipboardCheck" size={12} />
                      </button>
                      <button type="button" className={styles.thBtn} onClick={() => onRerunColumn(key)} disabled={running} title={`Re-run "${f.name}" for every document`} aria-label={`Re-run column ${f.name}`}>
                        <Icon name="RefreshCw" size={12} />
                      </button>
                    </span>
                  </div>
                  <span className={styles.resizeHandle} onMouseDown={(e) => startResize(key, e)} aria-hidden="true" />
                </th>
              );
            })}
          </tr>
          <tr className={styles.filterRow}>
            <th className={`${styles.thFilter} ${styles.thDoc}`}>
              <input
                className={styles.filterInput}
                placeholder="Filter documents"
                value={columnFilters.__document || ''}
                onChange={(e) => onColumnFilter('__document', e.target.value)}
                aria-label="Filter by document name"
              />
            </th>
            {fields.map((f) => {
              const key = keyOf(f);
              return (
                <th key={key} className={styles.thFilter}>
                  <input
                    className={styles.filterInput}
                    placeholder="Filter"
                    value={columnFilters[key] || ''}
                    onChange={(e) => onColumnFilter(key, e.target.value)}
                    aria-label={`Filter ${f.name}`}
                  />
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {start > 0 && <tr aria-hidden="true"><td colSpan={fields.length + 1} style={{ height: start * ROW_HEIGHT, padding: 0, border: 0 }} /></tr>}
          {documents.slice(start, end).map((doc, i) => {
            const row = start + i;
            return (
              <tr key={doc.id} className={doc.accessible ? undefined : styles.rowUnavailable} aria-rowindex={row + 1}>
                <td className={`${styles.td} ${styles.tdDoc}`}>
                  <div className={styles.tdDocInner}>
                    <input
                      type="checkbox"
                      className={styles.checkbox}
                      checked={rowAllSelected(doc.id)}
                      onChange={(e) => onSelectRow(doc.id, e.target.checked)}
                      aria-label={`Select row ${doc.filename}`}
                    />
                    <Icon name={doc.accessible ? 'FileText' : 'FileX'} size={13} />
                    <span className={styles.docName} title={doc.filename}>{doc.filename}</span>
                    <span className={styles.rowTools}>
                      <button type="button" className={styles.rerunBtn} onClick={() => onRerunRow(doc.id)} disabled={running || !doc.accessible} title="Re-run every field for this document" aria-label={`Re-run row ${doc.filename}`}>
                        <Icon name="RefreshCw" size={11} />
                      </button>
                      {canEditScope && (
                        <button type="button" className={styles.rerunBtn} onClick={() => onRemoveRow(doc.id)} disabled={running} title="Remove this document from the review" aria-label={`Remove row ${doc.filename}`}>
                          <Icon name="X" size={11} />
                        </button>
                      )}
                    </span>
                  </div>
                </td>
                {fields.map((f, col) => {
                  const key = keyOf(f);
                  const cell = cells.get(cellKey(doc.id, key));
                  const ref = { documentId: doc.id, fieldKey: key };
                  const isSelected = selected?.documentId === doc.id && selected?.fieldKey === key;
                  const isFocused = focus?.row === row && focus?.col === col;
                  const inSelection = selection.has(cellKey(doc.id, key));
                  const changed = changedSinceLastRun(cell);
                  return (
                    <td
                      key={key}
                      data-focused={isFocused ? 'true' : undefined}
                      className={`${styles.td} ${cellClass(cell, styles)} ${isSelected ? styles.cellSelected : ''} ${isFocused ? styles.cellFocused : ''} ${inSelection ? styles.cellInSelection : ''}`}
                      onClick={(e) => {
                        onFocus({ row, col });
                        if (e.ctrlKey || e.metaKey) onToggleSelected(ref);
                        else onSelect(isSelected ? null : ref);
                      }}
                      role="gridcell"
                      aria-selected={isSelected}
                      title={cell?.quote ? `“${cell.quote.slice(0, 240)}${cell.quote.length > 240 ? '…' : ''}”` : undefined}
                    >
                      <span className={styles.cellValue}>{cellText(cell)}</span>
                      <span className={styles.cellBadges}>
                        {changed && <span className={styles.changedDot} title="Changed since the previous run" />}
                        {cell?.override_value !== null && cell?.override_value !== undefined && <Icon name="Pencil" size={10} className={styles.badgeOverride} />}
                        {cell?.review_status === 'approved' && <Icon name="CheckCircle2" size={11} className={styles.badgeApproved} />}
                        {cell?.review_status === 'flagged' && <Icon name="Flag" size={11} className={styles.badgeFlagged} />}
                        {cell?.status === 'extracted' && cell.review_status === 'pending' && <Icon name="CheckCircle" size={11} className={styles.badgeVerified} />}
                        {cell?.status === 'unverified' && <Icon name="AlertTriangle" size={11} className={styles.badgeUnverified} />}
                      </span>
                    </td>
                  );
                })}
              </tr>
            );
          })}
          {end < documents.length && <tr aria-hidden="true"><td colSpan={fields.length + 1} style={{ height: (documents.length - end) * ROW_HEIGHT, padding: 0, border: 0 }} /></tr>}
          {documents.length === 0 && (
            <tr><td className={styles.tdEmpty} colSpan={fields.length + 1}>No rows match the current filters.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
