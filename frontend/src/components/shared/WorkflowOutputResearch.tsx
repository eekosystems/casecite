import { Icon } from './Icon';
import { MessageMarkdown } from './MessageMarkdown';
import { AiNotice } from './AiNotice';
import { safeHttpUrl } from './safeUrl';

interface WorkflowOutputResearchProps {
  data: Record<string, unknown>;
  styles: Record<string, string>;
}

interface Source { ref: string; title: string; citation?: string | null; kind?: string | null; url?: string | null }

/** The research answer, its verification line, key findings and sources. */
export function WorkflowOutputResearch({ data, styles }: WorkflowOutputResearchProps) {
  const answer = typeof data.answer === 'string' ? data.answer : '';
  const findings = (Array.isArray(data.key_findings) ? data.key_findings : []) as Array<{ text: string; refs?: string[] }>;
  const open = (Array.isArray(data.open_questions) ? data.open_questions : []) as string[];
  const sources = (Array.isArray(data.sources) ? data.sources : []) as Source[];
  const v = (data.verification || {}) as Record<string, number>;
  const parts = [
    `${v.quotes_verified ?? 0} quote${v.quotes_verified === 1 ? '' : 's'} verified`,
    v.quotes_unverified ? `${v.quotes_unverified} unverified` : null,
    v.invalid_refs_stripped ? `${v.invalid_refs_stripped} reference${v.invalid_refs_stripped === 1 ? '' : 's'} removed` : null,
    v.citations_checked ? `${v.citations_checked} authorit${v.citations_checked === 1 ? 'y' : 'ies'} checked` : null,
  ].filter(Boolean);
  return (
    <div className={styles.outputBlock}>
      {typeof data.question === 'string' && <div className={styles.researchQuestion}><Icon name="ScanSearch" size={13} /> {data.question}</div>}
      <div className={styles.researchAnswer}><MessageMarkdown text={answer} /></div>
      <div className={styles.muted}><Icon name="ShieldCheck" size={12} /> {parts.join('; ')}.</div>
      {findings.length > 0 && (
        <ul className={styles.findingList}>
          {findings.map((f, i) => (
            <li key={i}>{f.text}{f.refs?.length ? <span className={styles.muted}> [{f.refs.join(', ')}]</span> : null}</li>
          ))}
        </ul>
      )}
      {open.length > 0 && (
        <div className={styles.muted}><strong>Open questions:</strong> {open.join(' · ')}</div>
      )}
      {sources.length > 0 && (
        <ol className={styles.sourceList}>
          {sources.map((s) => {
            const url = safeHttpUrl(s.url || '');
            return (
              <li key={s.ref}>
                <span className={styles.sourceRef}>{s.ref}</span>{' '}
                {url ? <a href={url} target="_blank" rel="noopener noreferrer">{s.title}</a> : s.title}
                {s.citation && <span className={styles.muted}> · {s.citation}</span>}
                {s.kind && <span className={styles.roleBadge}>{s.kind.replace(/_/g, ' ')}</span>}
              </li>
            );
          })}
        </ol>
      )}
      <AiNotice />
    </div>
  );
}
