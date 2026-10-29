import { Icon } from './Icon';

interface WorkflowOutputApprovalProps {
  data: Record<string, unknown>;
  status: string;
  styles: Record<string, string>;
}

const str = (v: unknown) => (typeof v === 'string' ? v : '');

/** Who was asked, what they decided, their note and when. */
export function WorkflowOutputApproval({ data, status, styles }: WorkflowOutputApprovalProps) {
  const decision = str(data.decision);
  const approvers = Array.isArray(data.approvers) ? `${(data.approvers as string[]).length} named approver${(data.approvers as string[]).length === 1 ? '' : 's'}`
    : data.approvers === 'any_member' ? 'any member of the matter' : 'the workflow owner';
  return (
    <div className={styles.outputBlock}>
      {str(data.instructions) && <p className={styles.analysisSummary}>{str(data.instructions)}</p>}
      <div className={styles.muted}>Approvers: {approvers}.</div>
      {status === 'awaiting' && (
        <div className={styles.countWarn}><Icon name="Hourglass" size={12} /> Waiting for a decision.</div>
      )}
      {decision && (
        <div className={decision === 'approved' ? styles.countOk : styles.countBad}>
          <Icon name={decision === 'approved' ? 'ThumbsUp' : 'ThumbsDown'} size={12} />{' '}
          {decision === 'approved' ? 'Approved' : 'Rejected'} by {str(data.decided_by_email) || str(data.decided_by)}
          {str(data.decided_at) && ` on ${new Date(str(data.decided_at)).toLocaleString()}`}
        </div>
      )}
      {str(data.note) && <blockquote className={styles.issueQuote}>{str(data.note)}</blockquote>}
    </div>
  );
}
