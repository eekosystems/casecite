import { Icon } from './Icon';
import { activateOnKey } from './activateOnKey';
import { RUN_STATUS_ICON, RUN_STATUS_LABEL } from './WorkflowRunList';
import type { WorkflowRunSummary } from '../../api/types';

interface WorkflowRecentRunsProps {
  runs: WorkflowRunSummary[];
  onOpen: (workflowId: string, runId: string) => void;
  styles: Record<string, string>;
}

/** The newest runs across every workflow the user can see, with the ones needing them first. */
export function WorkflowRecentRuns({ runs, onOpen, styles }: WorkflowRecentRunsProps) {
  if (runs.length === 0) return null;
  const ordered = [...runs].sort((a, b) => Number(b.can_approve) - Number(a.can_approve));
  return (
    <div className={styles.recentStrip} aria-label="Recent runs">
      {ordered.map((run) => (
        <div
          key={run.id}
          className={`${styles.recentCard} ${run.can_approve ? styles.recentCardAction : ''}`}
          role="button"
          tabIndex={0}
          onClick={() => onOpen(run.workflow_id, run.id)}
          onKeyDown={activateOnKey(() => onOpen(run.workflow_id, run.id))}
        >
          <Icon
            name={RUN_STATUS_ICON[run.status] || 'Clock'}
            size={14}
            className={`${styles[`status_${run.status}`] || ''} ${run.status === 'running' ? styles.spin : ''}`}
          />
          <div className={styles.recentBody}>
            <div className={styles.recentTitle}>{run.workflow_name || 'Workflow'}</div>
            <div className={styles.recentMeta}>
              {run.can_approve ? 'Needs your approval' : RUN_STATUS_LABEL[run.status] || run.status} · {new Date(run.created_at).toLocaleString()}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
