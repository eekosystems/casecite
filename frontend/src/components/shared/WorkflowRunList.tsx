import { Icon } from './Icon';
import { activateOnKey } from './activateOnKey';
import type { WorkflowRunSummary } from '../../api/types';

interface WorkflowRunListProps {
  runs: WorkflowRunSummary[];
  selectedRunId: string | null;
  running: boolean;
  onSelect: (runId: string) => void;
  onResume: (runId: string) => void;
  styles: Record<string, string>;
}

export const RUN_STATUS_ICON: Record<string, string> = {
  running: 'Loader2',
  completed: 'CheckCircle',
  failed: 'XCircle',
  cancelled: 'Minus',
  awaiting_approval: 'Hourglass',
  rejected: 'ThumbsDown',
};

export const RUN_STATUS_LABEL: Record<string, string> = {
  awaiting_approval: 'awaiting approval',
};

/** Run history: one row per run with its status, version and step count; failed runs offer Resume. */
export function WorkflowRunList({ runs, selectedRunId, running, onSelect, onResume, styles }: WorkflowRunListProps) {
  if (runs.length === 0) {
    return <div className={styles.runEmpty}>No runs yet.</div>;
  }
  return (
    <ul className={styles.runList}>
      {runs.map((run) => {
        const resumable = (run.status === 'failed' || run.status === 'cancelled') && !running;
        return (
          <li
            key={run.id}
            className={`${styles.runRow} ${run.id === selectedRunId ? styles.runRowActive : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => onSelect(run.id)}
            onKeyDown={activateOnKey(() => onSelect(run.id))}
          >
            <Icon
              name={RUN_STATUS_ICON[run.status] || 'Clock'}
              size={14}
              className={`${styles[`status_${run.status}`] || ''} ${run.status === 'running' ? styles.spin : ''}`}
            />
            <div className={styles.runRowBody}>
              <div className={styles.runRowTitle}>
                {new Date(run.created_at).toLocaleString()}
                <span className={`${styles.runRowStatus} ${styles[`status_${run.status}`] || ''}`}>{RUN_STATUS_LABEL[run.status] || run.status}</span>
              </div>
              <div className={styles.runRowMeta}>
                {run.completed_steps}/{run.steps} steps · {run.documents} document{run.documents === 1 ? '' : 's'} · v{run.workflow_version}
                {run.resumed_count > 0 ? ` · resumed ${run.resumed_count}×` : ''}
              </div>
              {run.status === 'awaiting_approval' && (
                <div className={styles.runRowApproval}>
                  <Icon name="UserCheck" size={11} />
                  {run.can_approve ? 'Your approval is needed' : `Waiting on ${run.approver_ids.length} approver${run.approver_ids.length === 1 ? '' : 's'}`}
                  {run.awaiting_step ? ` · ${run.awaiting_step}` : ''}
                </div>
              )}
            </div>
            {resumable && (
              <button
                type="button"
                className={styles.secondaryBtn}
                onClick={(e) => { e.stopPropagation(); onResume(run.id); }}
                title="Re-run from the failed step with earlier outputs kept"
              >
                <Icon name="RotateCcw" size={13} /> Resume
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
