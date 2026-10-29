import { Icon } from './Icon';
import { activateOnKey } from './activateOnKey';
import type { ReviewTableSummary } from '../../api/types';

interface ReviewTableCardProps {
  table: ReviewTableSummary;
  onOpen: (tableId: string) => void;
  styles: Record<string, string>;
}

/** One review table in the /review-tables list: name, scope size, review progress, last run. */
export function ReviewTableCard({ table, onOpen, styles }: ReviewTableCardProps) {
  const lastRun = table.last_run_at ? new Date(table.last_run_at).toLocaleString() : null;
  const capacity = table.document_count * table.field_count;
  const pct = capacity ? Math.round((table.approved_count / capacity) * 100) : 0;
  return (
    <div
      className={styles.card}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(table.id)}
      onKeyDown={activateOnKey(() => onOpen(table.id))}
    >
      <div className={styles.cardIcon}>
        <Icon name="Table2" size={18} />
      </div>
      <div className={styles.cardBody}>
        <div className={styles.cardTitleRow}>
          <span className={styles.cardTitle}>{table.name}</span>
          <span className={`${styles.roleBadge} ${table.role === 'owner' ? styles.roleOwner : ''}`}>
            {table.role}
          </span>
        </div>
        {table.description && <div className={styles.cardDesc}>{table.description}</div>}
        <div className={styles.cardMeta}>
          <span>{table.document_count} document{table.document_count === 1 ? '' : 's'}</span>
          <span>{table.field_count} field{table.field_count === 1 ? '' : 's'}</span>
          <span>{lastRun ? `Last run ${lastRun}` : 'Not run yet'}</span>
        </div>
        {capacity > 0 && table.cell_count > 0 && (
          <div className={styles.cardProgress} title={`${table.approved_count} of ${capacity} cells approved`}>
            <div className={styles.cardProgressTrack} aria-hidden="true">
              <div className={styles.cardProgressFill} style={{ width: `${pct}%` }} />
            </div>
            <span className={styles.cardProgressLabel}>
              {pct}% reviewed
              {table.flagged_count > 0 && <span className={styles.cardFlag}> · <Icon name="Flag" size={10} /> {table.flagged_count}</span>}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
