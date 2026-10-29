import { useState } from 'react';
import { Icon } from './Icon';
import { stepKindInfo } from '../../stores/workflowsStore';
import { conditionSummary } from './WorkflowConditionEditor';
import { WorkflowOutputExtract } from './WorkflowOutputExtract';
import { WorkflowOutputAnalysis } from './WorkflowOutputAnalysis';
import { WorkflowOutputCompare } from './WorkflowOutputCompare';
import { WorkflowOutputDraft } from './WorkflowOutputDraft';
import { WorkflowOutputResearch } from './WorkflowOutputResearch';
import { WorkflowOutputCitations } from './WorkflowOutputCitations';
import { WorkflowOutputApproval } from './WorkflowOutputApproval';
import type { WorkflowCondition, WorkflowStepRun } from '../../api/types';

interface WorkflowStepRunCardProps {
  step: WorkflowStepRun;
  /** The step's definition in the run, for the condition it carried. */
  definition?: { condition?: WorkflowCondition | null } | null;
  /** Shown when the run can be continued from this step (owner or runner, run not in progress). */
  canControl?: boolean;
  /** True when this run can skip/retry (failed or cancelled). */
  controllable?: boolean;
  exportingDraft?: boolean;
  onRetry?: (index: number) => void;
  onSkip?: (index: number) => void;
  onDownloadDraft?: (title: string, text: string) => void;
  styles: Record<string, string>;
}

const STATUS_ICON: Record<string, string> = {
  pending: 'Clock',
  running: 'Loader2',
  completed: 'CheckCircle',
  failed: 'XCircle',
  skipped: 'Minus',
  skipped_condition: 'GitBranch',
  awaiting: 'Hourglass',
  rejected: 'ThumbsDown',
};

const STATUS_LABEL: Record<string, string> = {
  skipped_condition: 'skipped · condition not met',
  awaiting: 'awaiting approval',
};

/** One step of a run: status, summary, error, the kind's own output viewer and per-step controls. */
export function WorkflowStepRunCard({
  step, definition, canControl, controllable, exportingDraft = false, onRetry, onSkip, onDownloadDraft, styles,
}: WorkflowStepRunCardProps) {
  const detailed = step.status === 'completed' || step.status === 'awaiting' || step.status === 'rejected' || step.status === 'skipped_condition';
  const [open, setOpen] = useState(step.status === 'awaiting' || step.status === 'rejected');
  const info = stepKindInfo(step.kind);
  const data = (step.output?.data || {}) as Record<string, unknown>;
  const hasDetail = detailed && Object.keys(data).length > 0;
  const condition = (data.condition || null) as Record<string, unknown> | null;
  const duration = step.finished_at && step.started_at
    ? Math.max(1, Math.round((new Date(step.finished_at).getTime() - new Date(step.started_at).getTime()) / 1000))
    : null;
  const canSkip = controllable && canControl && (step.status === 'failed' || step.status === 'pending' || step.status === 'skipped');
  const canRetry = controllable && canControl && step.status !== 'awaiting';

  const output = () => {
    switch (step.kind) {
      case 'extract_fields': return <WorkflowOutputExtract data={data} styles={styles} />;
      case 'analyze_contract': return <WorkflowOutputAnalysis data={data} styles={styles} />;
      case 'compare_contracts': return <WorkflowOutputCompare data={data} styles={styles} />;
      case 'draft_document': return <WorkflowOutputDraft data={data} exporting={exportingDraft} onDownload={onDownloadDraft || (() => undefined)} styles={styles} />;
      case 'deep_research': return <WorkflowOutputResearch data={data} styles={styles} />;
      case 'citation_check': return <WorkflowOutputCitations data={data} styles={styles} />;
      case 'approval': return <WorkflowOutputApproval data={data} status={step.status} styles={styles} />;
      default: return <pre className={styles.outputJson}>{JSON.stringify(data, null, 2)}</pre>;
    }
  };

  return (
    <div className={`${styles.stepRun} ${styles[`stepRun_${step.status}`] || ''}`}>
      <div className={styles.stepRunHead}>
        <Icon
          name={STATUS_ICON[step.status] || 'Clock'}
          size={15}
          className={`${styles[`status_${step.status}`] || ''} ${step.status === 'running' ? styles.spin : ''}`}
        />
        <span className={styles.stepIndex}>{step.index + 1}</span>
        <span className={styles.stepRunName}>{step.name}</span>
        <span className={styles.stepKind}><Icon name={info.icon} size={12} /> {info.title}</span>
        <span className={styles.stepRunStatus}>{STATUS_LABEL[step.status] || step.status}</span>
        {duration !== null && <span className={styles.muted}>{duration}s</span>}
        {step.output?.truncated === true && <span className={styles.countWarn} title={`Cut: ${((step.output.truncated_fields as string[]) || []).join(', ')}`}>output truncated</span>}
        <span className={styles.spacer} />
        {canSkip && onSkip && (
          <button type="button" className={styles.toolBtn} onClick={() => onSkip(step.index)} title="Mark this step skipped and continue from the next one">
            <Icon name="SkipForward" size={12} /> Skip and continue
          </button>
        )}
        {canRetry && onRetry && (
          <button type="button" className={styles.toolBtn} onClick={() => onRetry(step.index)} title="Re-run this step with the same inputs and continue">
            <Icon name="RotateCw" size={12} /> Retry this step
          </button>
        )}
        {hasDetail && (
          <button type="button" className={styles.iconBtn} onClick={() => setOpen((o) => !o)} aria-label={open ? 'Hide output' : 'Show output'} title={open ? 'Hide output' : 'Show output'}>
            <Icon name={open ? 'ChevronUp' : 'ChevronDown'} size={14} />
          </button>
        )}
      </div>
      {definition?.condition && step.status !== 'skipped_condition' && (
        <div className={styles.muted}><Icon name="GitBranch" size={11} /> Ran because {conditionSummary(definition.condition)}.</div>
      )}
      {step.output?.summary && <div className={styles.stepRunSummary}>{step.output.summary}</div>}
      {step.status === 'skipped_condition' && condition && (
        <div className={styles.muted}>
          Rule: {String(condition.step)}{condition.path ? ` › ${String(condition.path)}` : ''} {String(condition.op)}{condition.value !== null && condition.value !== undefined ? ` ${String(condition.value)}` : ''}
          {condition.found ? ` · found ${String(condition.actual)}` : ' · nothing at that path'}
        </div>
      )}
      {step.error && <div className={styles.stepRunError}>{step.error}</div>}
      {open && hasDetail && step.status !== 'skipped_condition' && (
        <div className={styles.stepRunDetail}>{output()}</div>
      )}
    </div>
  );
}
