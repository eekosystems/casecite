import { useEffect, useState } from 'react';
import { Icon } from './Icon';
import { diffVersions, describeDiff } from '../../utils/workflowDiff';
import type { WorkflowStep, WorkflowVersionSummary } from '../../api/types';

interface WorkflowVersionsDrawerProps {
  versions: WorkflowVersionSummary[];
  current: number;
  currentSteps: WorkflowStep[];
  canRestore: boolean;
  loadSteps: (version: number) => Promise<WorkflowStep[] | null>;
  onRestore: (version: number) => void;
  onClose: () => void;
  styles: Record<string, string>;
}

/** Every version of the steps, newest first, with what each changed against the current steps. */
export function WorkflowVersionsDrawer({
  versions, current, currentSteps, canRestore, loadSteps, onRestore, onClose, styles,
}: WorkflowVersionsDrawerProps) {
  const [diffs, setDiffs] = useState<Record<number, string>>({});
  const [expanded, setExpanded] = useState<number | null>(null);
  const [steps, setSteps] = useState<Record<number, WorkflowStep[]>>({});

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const next: Record<number, string> = {};
      const loaded: Record<number, WorkflowStep[]> = {};
      for (const v of versions) {
        if (v.version === current) continue;
        const s = await loadSteps(v.version);
        if (cancelled) return;
        if (s) {
          loaded[v.version] = s;
          next[v.version] = describeDiff(diffVersions(s, currentSteps));
        }
      }
      if (!cancelled) { setDiffs(next); setSteps((prev) => ({ ...prev, ...loaded })); }
    })();
    return () => { cancelled = true; };
  }, [versions, current, currentSteps, loadSteps]);

  return (
    <div className={styles.drawer} role="dialog" aria-label="Version history">
      <div className={styles.drawerHead}>
        <span className={styles.drawerTitle}><Icon name="History" size={14} /> Versions</span>
        <button type="button" className={styles.iconBtn} onClick={onClose} aria-label="Close versions">
          <Icon name="X" size={14} />
        </button>
      </div>
      {versions.length === 0 ? (
        <div className={styles.muted}>Loading…</div>
      ) : (
        <ol className={styles.versionList}>
          {versions.map((v) => {
            const isCurrent = v.version === current;
            const open = expanded === v.version;
            const detail = steps[v.version];
            const diff = detail ? diffVersions(detail, currentSteps) : null;
            return (
              <li key={v.version} className={`${styles.versionRow} ${isCurrent ? styles.versionRowCurrent : ''}`}>
                <div className={styles.versionHead}>
                  <span className={styles.versionNumber}>v{v.version}</span>
                  <span className={styles.versionMeta}>
                    {new Date(v.created_at).toLocaleString()} · {v.step_count} step{v.step_count === 1 ? '' : 's'}
                    {isCurrent ? ' · current' : diffs[v.version] ? ` · ${diffs[v.version]} vs current` : ''}
                  </span>
                  {!isCurrent && (
                    <button type="button" className={styles.iconBtn} onClick={() => setExpanded(open ? null : v.version)} aria-label={open ? 'Hide changes' : 'Show changes'}>
                      <Icon name={open ? 'ChevronUp' : 'ChevronDown'} size={13} />
                    </button>
                  )}
                  {!isCurrent && canRestore && (
                    <button type="button" className={styles.toolBtn} onClick={() => onRestore(v.version)} title={`Make version ${v.version}'s steps current as a new version`}>
                      <Icon name="RotateCcw" size={12} /> Restore
                    </button>
                  )}
                </div>
                {v.change_note && <div className={styles.versionNote}>{v.change_note}</div>}
                <div className={styles.versionSteps}>{v.step_names.join(' → ')}</div>
                {open && diff && (
                  <ul className={styles.diffList}>
                    {diff.added.map((n) => <li key={`a-${n}`} className={styles.countWarn}>Current adds step “{n}”</li>)}
                    {diff.removed.map((n) => <li key={`r-${n}`} className={styles.countBad}>Current drops step “{n}”</li>)}
                    {diff.changed.map((c) => <li key={`c-${c.name}`}>“{c.name}” differs in {c.keys.join(', ')}</li>)}
                    {diff.reordered && <li>Steps are in a different order</li>}
                    {!diff.added.length && !diff.removed.length && !diff.changed.length && !diff.reordered && <li className={styles.muted}>Same steps as the current version.</li>}
                  </ul>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
