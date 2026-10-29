import { Icon } from './Icon';
import type { ReviewRun } from '../../api/types';

interface ReviewRunsDrawerProps {
  open: boolean;
  runs: ReviewRun[];
  loading: boolean;
  fieldNames: Record<string, string>;
  documentNames: Record<string, string>;
  onClose: () => void;
  onShowChanged: () => void;
  styles: Record<string, string>;
}

const STATUS_ICON: Record<string, string> = { completed: 'CheckCircle2', failed: 'XCircle', cancelled: 'Minus', running: 'Loader2' };

function describeScope(run: ReviewRun, fieldNames: Record<string, string>, documentNames: Record<string, string>): string {
  const docs = run.scope.document_ids;
  const keys = run.scope.field_keys;
  const parts: string[] = [];
  if (docs && docs.length) parts.push(docs.length === 1 ? documentNames[docs[0]] || '1 document' : `${docs.length} documents`);
  if (keys && keys.length) parts.push(keys.length === 1 ? fieldNames[keys[0]] || '1 field' : `${keys.length} fields`);
  return parts.length ? parts.join(' × ') : 'Whole table';
}

function duration(run: ReviewRun): string | null {
  if (!run.finished_at) return null;
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
  if (ms < 1000) return '<1s';
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

/** Every extraction run of the table, newest first: scope, outcome counts and what changed. */
export function ReviewRunsDrawer({ open, runs, loading, fieldNames, documentNames, onClose, onShowChanged, styles }: ReviewRunsDrawerProps) {
  if (!open) return null;
  return (
    <div className={styles.runsDrawer} role="region" aria-label="Run history">
      <div className={styles.runsHead}>
        <span className={styles.panelTitle}><Icon name="History" size={14} /> Runs</span>
        <span className={styles.panelHint}>Each run writes the model's value into its cells; approvals stand unless the value changed.</span>
        <span className={styles.spacer} />
        <button type="button" className={styles.linkBtn} onClick={onShowChanged}>Show cells changed by the last run</button>
        <button type="button" className={styles.iconBtn} onClick={onClose} aria-label="Close run history"><Icon name="X" size={14} /></button>
      </div>
      {loading && runs.length === 0 ? (
        <div className={styles.runsEmpty}><Icon name="Loader2" size={13} className={styles.spin} /> Loading runs…</div>
      ) : runs.length === 0 ? (
        <div className={styles.runsEmpty}>No runs yet.</div>
      ) : (
        <ul className={styles.runsList}>
          {runs.map((run) => {
            const c = run.counts;
            return (
              <li key={run.id} className={`${styles.runItem} ${styles[`run_${run.status}`] || ''}`}>
                <Icon name={STATUS_ICON[run.status] || 'Clock'} size={14} className={`${styles.runIcon} ${run.status === 'running' ? styles.spin : ''}`} />
                <div className={styles.runBody}>
                  <div className={styles.runLine}>
                    <span className={styles.runWhen}>{new Date(run.started_at).toLocaleString()}</span>
                    <span className={styles.runScope}>{describeScope(run, fieldNames, documentNames)}</span>
                    <span className={styles.runStatus}>{run.status}{duration(run) ? ` · ${duration(run)}` : ''}</span>
                  </div>
                  {c ? (
                    <div className={styles.runCounts}>
                      <span>{c.cells} cells</span>
                      {c.extracted ? <span className={styles.statVerified}>{c.extracted} verified</span> : null}
                      {c.unverified ? <span className={styles.statUnverified}>{c.unverified} unverified</span> : null}
                      {c.not_found ? <span className={styles.statNotFound}>{c.not_found} not found</span> : null}
                      {c.errors ? <span className={styles.statError}>{c.errors} failed</span> : null}
                      {c.changed ? <span className={styles.statChanged}>{c.changed} changed</span> : null}
                    </div>
                  ) : run.error ? (
                    <div className={styles.runError}>{run.error}</div>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
