import { Icon } from './Icon';
import { STATUS_FILTERS, type StatusFilter } from '../../utils/reviewGrid';

interface ReviewFilterBarProps {
  statusFilter: StatusFilter;
  counts: Record<StatusFilter, number>;
  hasColumnFilters: boolean;
  visibleRows: number;
  totalRows: number;
  selectionSize: number;
  running: boolean;
  onStatusFilter: (filter: StatusFilter) => void;
  onClearFilters: () => void;
  onApproveSelected: () => void;
  onFlagSelected: () => void;
  onClearReviewSelected: () => void;
  onRerunSelected: () => void;
  onClearSelection: () => void;
  styles: Record<string, string>;
}

/** Status chips with counts, the row tally, and the actions on the current selection. */
export function ReviewFilterBar({
  statusFilter, counts, hasColumnFilters, visibleRows, totalRows, selectionSize, running,
  onStatusFilter, onClearFilters, onApproveSelected, onFlagSelected, onClearReviewSelected, onRerunSelected, onClearSelection, styles,
}: ReviewFilterBarProps) {
  const filtered = statusFilter !== 'all' || hasColumnFilters;
  return (
    <div className={styles.filterBar}>
      <div className={styles.chips} role="group" aria-label="Status filter">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            aria-pressed={statusFilter === f.key}
            className={`${styles.chip} ${statusFilter === f.key ? styles.chipActive : ''} ${styles[`chip_${f.key}`] || ''}`}
            onClick={() => onStatusFilter(f.key)}
          >
            {f.label}
            <span className={styles.chipCount}>{counts[f.key]}</span>
          </button>
        ))}
      </div>
      <div className={styles.filterMeta}>
        {filtered ? (
          <>
            <span>{visibleRows} of {totalRows} row{totalRows === 1 ? '' : 's'}</span>
            <button type="button" className={styles.linkBtn} onClick={onClearFilters}>Clear filters</button>
          </>
        ) : (
          <span>{totalRows} row{totalRows === 1 ? '' : 's'}</span>
        )}
      </div>
      {selectionSize > 0 && (
        <div className={styles.selectionBar} role="toolbar" aria-label="Selected cells">
          <span className={styles.selectionCount}><Icon name="Rows3" size={12} /> {selectionSize} selected</span>
          <button type="button" className={styles.secondaryBtnSm} onClick={onApproveSelected} disabled={running}><Icon name="CheckCircle2" size={12} /> Approve</button>
          <button type="button" className={styles.secondaryBtnSm} onClick={onFlagSelected} disabled={running}><Icon name="Flag" size={12} /> Flag</button>
          <button type="button" className={styles.secondaryBtnSm} onClick={onClearReviewSelected} disabled={running} title="Set the selected cells back to pending"><Icon name="Undo2" size={12} /> Reset</button>
          <button type="button" className={styles.secondaryBtnSm} onClick={onRerunSelected} disabled={running}><Icon name="RefreshCw" size={12} /> Re-run</button>
          <button type="button" className={styles.linkBtn} onClick={onClearSelection}>Clear selection</button>
        </div>
      )}
    </div>
  );
}
