import { useState } from 'react';
import { Icon } from './Icon';
import type { WorkflowRunDetail } from '../../api/types';

interface WorkflowApprovalBannerProps {
  run: WorkflowRunDetail;
  busy: boolean;
  onApprove: (note: string) => void;
  onReject: (note: string) => void;
  styles: Record<string, string>;
}

/** Shown above an awaiting run: what is waiting, and Approve / Reject with a note for approvers. */
export function WorkflowApprovalBanner({ run, busy, onApprove, onReject, styles }: WorkflowApprovalBannerProps) {
  const [note, setNote] = useState('');
  const step = run.step_runs.find((s) => s.status === 'awaiting');
  const data = (step?.output?.data || {}) as Record<string, unknown>;
  const title = typeof data.title === 'string' ? data.title : step?.name || 'Approval';
  return (
    <div className={styles.approvalBanner} role="region" aria-label="Approval">
      <div className={styles.approvalHead}>
        <Icon name="Hourglass" size={15} />
        <div className={styles.approvalText}>
          <strong>Waiting for approval: {title}</strong>
          <span className={styles.muted}>
            {run.can_approve
              ? 'You can decide this step. The run continues after an approval and ends after a rejection.'
              : `Waiting on ${run.approver_ids.length} approver${run.approver_ids.length === 1 ? '' : 's'}. Nothing runs until one of them decides.`}
          </span>
          {typeof data.instructions === 'string' && data.instructions && <span className={styles.approvalInstructions}>{data.instructions}</span>}
        </div>
      </div>
      {run.can_approve && (
        <div className={styles.approvalActions}>
          <input
            className={styles.input}
            placeholder="Note for the record (optional)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            aria-label="Decision note"
            disabled={busy}
          />
          <button type="button" className={styles.primaryBtn} onClick={() => onApprove(note)} disabled={busy}>
            <Icon name="ThumbsUp" size={14} /> Approve
          </button>
          <button type="button" className={styles.dangerBtn} onClick={() => onReject(note)} disabled={busy}>
            <Icon name="ThumbsDown" size={14} /> Reject
          </button>
        </div>
      )}
    </div>
  );
}
