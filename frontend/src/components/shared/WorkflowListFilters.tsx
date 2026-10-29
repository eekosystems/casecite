import { Icon } from './Icon';
import { LIST_FILTERS, type WorkflowListFilter } from '../../stores/workflowsStore';

interface WorkflowListFiltersProps {
  search: string;
  filter: WorkflowListFilter;
  counts: Partial<Record<WorkflowListFilter, number>>;
  onSearch: (search: string) => void;
  onFilter: (filter: WorkflowListFilter) => void;
  styles: Record<string, string>;
}

/** Search box and status chips for the workflows list. */
export function WorkflowListFilters({ search, filter, counts, onSearch, onFilter, styles }: WorkflowListFiltersProps) {
  return (
    <div className={styles.filterBar}>
      <label className={styles.searchBox}>
        <Icon name="Search" size={14} />
        <input
          className={styles.searchInput}
          placeholder="Search workflows"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          aria-label="Search workflows"
        />
        {search && (
          <button type="button" className={styles.searchClear} onClick={() => onSearch('')} aria-label="Clear search">
            <Icon name="X" size={12} />
          </button>
        )}
      </label>
      <div className={styles.filterChips} role="tablist" aria-label="Filter workflows">
        {LIST_FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            role="tab"
            aria-selected={filter === f.key}
            className={`${styles.filterChip} ${filter === f.key ? styles.filterChipOn : ''}`}
            onClick={() => onFilter(f.key)}
          >
            {f.label}
            {typeof counts[f.key] === 'number' && <span className={styles.filterCount}>{counts[f.key]}</span>}
          </button>
        ))}
      </div>
    </div>
  );
}
