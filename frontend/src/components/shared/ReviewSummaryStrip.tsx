import { Icon } from './Icon';
import { columnStats, reviewProgress } from '../../utils/reviewGrid';
import type { RunProgress } from '../../stores/reviewTablesStore';
import type { ReviewCell, ReviewFieldSpec } from '../../api/types';

interface ReviewSummaryStripProps {
  fields: ReviewFieldSpec[];
  cells: ReviewCell[];
  documentCount: number;
  running: boolean;
  progress: RunProgress | null;
  styles: Record<string, string>;
}

/** Review progress and per-column outcomes at a glance; the run's progress bar while it runs. */
export function ReviewSummaryStrip({ fields, cells, documentCount, running, progress, styles }: ReviewSummaryStripProps) {
  const overall = reviewProgress(cells, documentCount, fields.length);
  const pct = overall.capacity ? Math.round((overall.approved / overall.capacity) * 100) : 0;
  return (
    <div className={styles.summaryStrip}>
      <div className={styles.summaryProgress}>
        <div className={styles.summaryProgressHead}>
          <span className={styles.summaryLabel}>Review progress</span>
          <span className={styles.summaryCount}>
            {overall.approved} / {overall.capacity} approved
            {overall.flagged > 0 && <span className={styles.summaryFlag}> · {overall.flagged} flagged</span>}
          </span>
        </div>
        <div className={styles.progressTrack} aria-hidden="true">
          <div className={`${styles.progressFill} ${styles.progressFillGreen}`} style={{ width: `${pct}%` }} />
        </div>
        {running && progress && (
          <div className={styles.summaryRun}>
            <Icon name="Loader2" size={12} className={styles.spin} />
            <span>{progress.message}</span>
            {progress.fraction !== null && (
              <div className={styles.progressTrack} aria-hidden="true">
                <div className={styles.progressFill} style={{ width: `${Math.round(progress.fraction * 100)}%` }} />
              </div>
            )}
          </div>
        )}
      </div>
      <div className={styles.summaryColumns}>
        {fields.map((f) => {
          const key = f.key || f.name;
          const st = columnStats(cells, key);
          return (
            <div key={key} className={styles.summaryCol} title={`${f.name}: ${st.extracted} verified, ${st.unverified} unverified, ${st.not_found} not found, ${st.error} failed, ${st.approved} approved, ${st.flagged} flagged`}>
              <div className={styles.summaryColName}>{f.name}</div>
              <div className={styles.summaryBar} aria-hidden="true">
                {st.total > 0 && (
                  <>
                    <span className={styles.segVerified} style={{ flexGrow: st.extracted }} />
                    <span className={styles.segUnverified} style={{ flexGrow: st.unverified }} />
                    <span className={styles.segNotFound} style={{ flexGrow: st.not_found }} />
                    <span className={styles.segError} style={{ flexGrow: st.error }} />
                  </>
                )}
              </div>
              <div className={styles.summaryColStats}>
                <span className={styles.statVerified}>{st.extracted}</span>
                <span className={styles.statUnverified}>{st.unverified}</span>
                <span className={styles.statNotFound}>{st.not_found}</span>
                {st.error > 0 && <span className={styles.statError}>{st.error}</span>}
                {st.flagged > 0 && <span className={styles.statFlagged}><Icon name="Flag" size={9} />{st.flagged}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
