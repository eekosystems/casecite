import { useState } from 'react';
import { Icon } from './Icon';

interface WorkflowOutputAnalysisProps {
  data: Record<string, unknown>;
  styles: Record<string, string>;
}

interface IssueBrief {
  ref?: string | null;
  title: string;
  severity: string;
  why: string;
  grounding?: string | null;
  matched_text?: string | null;
  suggested_language?: string | null;
}

interface Analysis {
  document_id: string;
  filename: string;
  analysis_id?: string | null;
  contract_type?: string | null;
  issues: number;
  issues_by_severity?: Record<string, number>;
  issue_list?: IssueBrief[];
  executive_summary?: string | null;
  parties?: Array<{ name?: string; role?: string } | string>;
  key_terms?: Record<string, { value?: string } | string>;
}

const SEVERITIES = ['critical', 'major', 'minor', 'informational'];

/** Per document: issue counts by severity, the issues with their quotes, and the key terms. */
export function WorkflowOutputAnalysis({ data, styles }: WorkflowOutputAnalysisProps) {
  const analyses = (Array.isArray(data.analyses) ? data.analyses : []) as Analysis[];
  const failures = (Array.isArray(data.failures) ? data.failures : []) as Array<{ document_id: string; error: string }>;
  const [open, setOpen] = useState<string | null>(analyses.length === 1 ? analyses[0].document_id : null);

  return (
    <div className={styles.outputBlock}>
      {analyses.map((a) => {
        const expanded = open === a.document_id;
        const bySeverity = a.issues_by_severity || {};
        return (
          <div key={a.document_id} className={styles.analysisCard}>
            <button type="button" className={styles.analysisHead} onClick={() => setOpen(expanded ? null : a.document_id)} aria-expanded={expanded}>
              <Icon name={expanded ? 'ChevronDown' : 'ChevronRight'} size={13} />
              <span className={styles.analysisName}>{a.filename}</span>
              {a.contract_type && <span className={styles.roleBadge}>{a.contract_type}</span>}
              <span className={styles.severityRow}>
                {SEVERITIES.filter((s) => bySeverity[s]).map((s) => (
                  <span key={s} className={`${styles.severityChip} ${styles[`severity_${s}`] || ''}`}>{bySeverity[s]} {s}</span>
                ))}
                {a.issues === 0 && <span className={styles.countOk}>No issues</span>}
              </span>
            </button>
            {expanded && (
              <div className={styles.analysisBody}>
                {a.executive_summary && <p className={styles.analysisSummary}>{a.executive_summary}</p>}
                {(a.issue_list || []).map((issue, i) => (
                  <div key={issue.ref || i} className={styles.issueCard}>
                    <div className={styles.issueHead}>
                      <span className={`${styles.severityChip} ${styles[`severity_${issue.severity}`] || ''}`}>{issue.severity}</span>
                      <span className={styles.issueTitle}>{issue.title}</span>
                      {issue.grounding === 'unverified' && <span className={styles.countWarn}>unverified</span>}
                    </div>
                    <p className={styles.issueWhy}>{issue.why}</p>
                    {issue.matched_text && <blockquote className={styles.issueQuote}>{issue.matched_text}</blockquote>}
                    {issue.suggested_language && (
                      <div className={styles.issueSuggestion}><strong>Suggested:</strong> {issue.suggested_language}</div>
                    )}
                  </div>
                ))}
                {a.issues > (a.issue_list || []).length && (
                  <div className={styles.muted}>{a.issues - (a.issue_list || []).length} more on the saved analysis under Contracts.</div>
                )}
                {a.key_terms && Object.keys(a.key_terms).length > 0 && (
                  <dl className={styles.termList}>
                    {Object.entries(a.key_terms).map(([k, v]) => (
                      <div key={k} className={styles.termRow}>
                        <dt>{k.replace(/_/g, ' ')}</dt>
                        <dd>{typeof v === 'string' ? v : v?.value || '—'}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </div>
            )}
          </div>
        );
      })}
      {failures.length > 0 && (
        <div className={styles.countBad}>
          {failures.length} document{failures.length === 1 ? '' : 's'} could not be analysed: {failures.map((f) => f.error).join('; ')}
        </div>
      )}
    </div>
  );
}
