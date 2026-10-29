import { Icon } from './Icon';
import { compareRuns } from '../../utils/workflowDiff';
import type { WorkflowRunDetail } from '../../api/types';

interface WorkflowRunCompareProps {
  current: WorkflowRunDetail;
  previous: WorkflowRunDetail | null;
  styles: Record<string, string>;
}

/** Per-step status and summary of this run beside the previous run of the same workflow. */
export function WorkflowRunCompare({ current, previous, styles }: WorkflowRunCompareProps) {
  if (!previous) {
    return <div className={styles.muted}>This is the earliest run; there is nothing before it to compare with.</div>;
  }
  const rows = compareRuns(current, previous);
  const changed = rows.filter((r) => r.statusChanged || r.summaryChanged).length;
  return (
    <div className={styles.compareRuns}>
      <div className={styles.compareRunsHead}>
        <span><Icon name="Columns2" size={13} /> Compared with the run of {new Date(previous.created_at).toLocaleString()} (v{previous.workflow_version})</span>
        <span className={changed ? styles.countWarn : styles.countOk}>{changed} step{changed === 1 ? '' : 's'} changed</span>
      </div>
      <table className={styles.previewTable}>
        <thead>
          <tr><th>Step</th><th>Previous</th><th>This run</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.index}-${r.name}`} className={r.statusChanged || r.summaryChanged ? styles.compareChanged : ''}>
              <td className={styles.previewDoc}>{r.name} <span className={styles.muted}>{r.kind.replace(/_/g, ' ')}</span></td>
              <td>
                {r.previous ? (
                  <>
                    <span className={`${styles.runRowStatus} ${styles[`status_${r.previous.status}`] || ''}`}>{r.previous.status.replace(/_/g, ' ')}</span>
                    <div className={styles.compareSummary}>{r.previous.output?.summary || r.previous.error || '—'}</div>
                  </>
                ) : <span className={styles.muted}>not in that run</span>}
              </td>
              <td>
                {r.current ? (
                  <>
                    <span className={`${styles.runRowStatus} ${styles[`status_${r.current.status}`] || ''}`}>{r.current.status.replace(/_/g, ' ')}</span>
                    <div className={styles.compareSummary}>{r.current.output?.summary || r.current.error || '—'}</div>
                  </>
                ) : <span className={styles.muted}>not in this run</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
