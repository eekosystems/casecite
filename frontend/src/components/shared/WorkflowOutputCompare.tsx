interface WorkflowOutputCompareProps {
  data: Record<string, unknown>;
  styles: Record<string, string>;
}

interface Clause {
  topic: string;
  status: string;
  summary?: string;
  quote_a?: string;
  quote_b?: string;
  verified_a?: boolean;
  verified_b?: boolean;
}

interface Comparison {
  label_a: string;
  label_b: string;
  overall?: string;
  clauses: Clause[];
  differences: number;
}

/** Each comparison against the baseline: the overall delta and the clauses side by side. */
export function WorkflowOutputCompare({ data, styles }: WorkflowOutputCompareProps) {
  const comparisons = (Array.isArray(data.comparisons) ? data.comparisons : []) as Comparison[];
  return (
    <div className={styles.outputBlock}>
      {comparisons.map((c, i) => (
        <div key={i} className={styles.compareCard}>
          <div className={styles.compareHead}>
            <span className={styles.compareLabels}>
              <strong>{c.label_a}</strong> <span className={styles.muted}>vs</span> <strong>{c.label_b}</strong>
            </span>
            <span className={c.differences ? styles.countWarn : styles.countOk}>
              {c.differences} clause difference{c.differences === 1 ? '' : 's'}
            </span>
          </div>
          {c.overall && <p className={styles.analysisSummary}>{c.overall}</p>}
          <div className={styles.compareGrid}>
            {c.clauses.filter((cl) => cl.status !== 'unchanged').map((cl, n) => (
              <div key={n} className={styles.compareRow}>
                <div className={styles.compareTopic}>
                  <span className={`${styles.severityChip} ${styles[`compare_${cl.status}`] || ''}`}>{cl.status.replace(/_/g, ' ')}</span>
                  <strong>{cl.topic}</strong>
                  {cl.summary && <span className={styles.muted}>{cl.summary}</span>}
                </div>
                <div className={styles.compareSides}>
                  <blockquote className={styles.compareQuote}>{cl.quote_a || <em>Absent</em>}</blockquote>
                  <blockquote className={styles.compareQuote}>{cl.quote_b || <em>Absent</em>}</blockquote>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
