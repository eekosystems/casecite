import { Icon } from './Icon';
import { safeHttpUrl } from './safeUrl';

interface WorkflowOutputCitationsProps {
  data: Record<string, unknown>;
  styles: Record<string, string>;
}

interface Checked {
  citation: string;
  status: string;
  case_name?: string | null;
  warning_level?: string | null;
  is_good_law?: boolean | null;
  url?: string | null;
  negative?: number;
  caution?: number;
  detail?: string;
}

function treatment(c: Checked): { label: string; cls: string } {
  if (c.status === 'unavailable') return { label: 'Not checked (no CourtListener token)', cls: 'countMuted' };
  if (c.status !== 'checked') return { label: 'Check failed', cls: 'countMuted' };
  if (c.negative) return { label: `Negative treatment language in ${c.negative} citing opinion${c.negative === 1 ? '' : 's'}`, cls: 'countBad' };
  if (c.caution) return { label: `Cautionary language in ${c.caution} citing opinion${c.caution === 1 ? '' : 's'}`, cls: 'countWarn' };
  if (c.is_good_law === true) return { label: 'No negative treatment language found', cls: 'countOk' };
  return { label: 'Treatment undetermined', cls: 'countMuted' };
}

/** One row per citation with the treatment scan's outcome — never a verdict the scan cannot give. */
export function WorkflowOutputCitations({ data, styles }: WorkflowOutputCitationsProps) {
  const citations = (Array.isArray(data.citations) ? data.citations : []) as Checked[];
  const skipped = (Array.isArray(data.skipped) ? data.skipped : []) as string[];
  const foundIn = (data.found_in_steps || {}) as Record<string, string[]>;
  if (citations.length === 0) return <div className={styles.muted}>No citations to check.</div>;
  return (
    <div className={styles.outputBlock}>
      <table className={styles.previewTable}>
        <thead>
          <tr><th>Citation</th><th>Case</th><th>Treatment</th></tr>
        </thead>
        <tbody>
          {citations.map((c) => {
            const t = treatment(c);
            const url = safeHttpUrl(c.url || '');
            return (
              <tr key={c.citation}>
                <td className={styles.previewDoc}>{c.citation}</td>
                <td>{url ? <a href={url} target="_blank" rel="noopener noreferrer">{c.case_name || 'Open'}</a> : c.case_name || '—'}</td>
                <td className={styles[t.cls]}><Icon name={t.cls === 'countBad' ? 'AlertTriangle' : t.cls === 'countOk' ? 'CheckCircle' : 'Info'} size={12} /> {t.label}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {Object.keys(foundIn).length > 0 && (
        <div className={styles.muted}>Found in: {Object.entries(foundIn).map(([step, cites]) => `${step} (${cites.length})`).join(', ')}.</div>
      )}
      {skipped.length > 0 && <div className={styles.countWarn}>{skipped.length} citation{skipped.length === 1 ? '' : 's'} not checked (over the per-step cap).</div>}
      <div className={styles.muted}>A scan of citing opinions for treatment language, not a citator. Read before relying.</div>
    </div>
  );
}
