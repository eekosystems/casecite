import { Icon } from './Icon';
import { activateOnKey } from './activateOnKey';
import { stepKindInfo } from '../../stores/workflowsStore';
import type { WorkflowSummary } from '../../api/types';

interface WorkflowCardProps {
  workflow: WorkflowSummary;
  onOpen: (workflowId: string) => void;
  styles: Record<string, string>;
}

/** One workflow in the /workflows list: name, its steps as a chain of icons, last run. */
export function WorkflowCard({ workflow, onOpen, styles }: WorkflowCardProps) {
  const lastRun = workflow.last_run_at ? new Date(workflow.last_run_at).toLocaleString() : null;
  return (
    <div
      className={styles.card}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(workflow.id)}
      onKeyDown={activateOnKey(() => onOpen(workflow.id))}
    >
      <div className={styles.cardIcon}>
        <Icon name="Workflow" size={18} />
      </div>
      <div className={styles.cardBody}>
        <div className={styles.cardTitleRow}>
          <span className={styles.cardTitle}>{workflow.name}</span>
          <span className={`${styles.roleBadge} ${workflow.role === 'owner' ? styles.roleOwner : ''}`}>
            {workflow.role}
          </span>
          <span className={styles.roleBadge} title="Current version of the steps">v{workflow.version}</span>
          {workflow.archived_at && <span className={styles.roleBadge}>archived</span>}
        </div>
        {workflow.description && <div className={styles.cardDesc}>{workflow.description}</div>}
        <div className={styles.stepChain} aria-label={`${workflow.step_count} steps`}>
          {workflow.step_kinds.map((kind, i) => {
            const info = stepKindInfo(kind);
            return (
              <span key={`${kind}-${i}`} className={styles.stepChip} title={info.title}>
                <Icon name={info.icon} size={12} /> {info.title}
              </span>
            );
          })}
        </div>
        <div className={styles.cardMeta}>
          <span>{workflow.run_count} run{workflow.run_count === 1 ? '' : 's'}</span>
          <span>
            {lastRun ? `Last run ${lastRun}` : 'Not run yet'}
            {workflow.last_run_status ? ` · ${workflow.last_run_status.replace(/_/g, ' ')}` : ''}
          </span>
        </div>
      </div>
    </div>
  );
}
