import { Icon } from './Icon';
import { AiNotice } from './AiNotice';
import { appNavigate } from '../../utils/router';

interface WorkflowOutputDraftProps {
  data: Record<string, unknown>;
  exporting: boolean;
  onDownload: (title: string, text: string) => void;
  styles: Record<string, string>;
}

const str = (v: unknown) => (typeof v === 'string' ? v : '');

/** The drafted text with Word export, plus what the draft was built from. */
export function WorkflowOutputDraft({ data, exporting, onDownload, styles }: WorkflowOutputDraftProps) {
  const title = str(data.title) || 'Draft';
  const text = str(data.text);
  const references = (Array.isArray(data.references) ? data.references : []) as Array<{ kind: string; name?: string; filename?: string }>;
  const missing = (Array.isArray(data.template_values_missing) ? data.template_values_missing : []) as string[];
  const removed = (Array.isArray(data.case_law_removed) ? data.case_law_removed : []) as string[];
  return (
    <div className={styles.outputBlock}>
      <div className={styles.outputHead}>
        <span className={styles.stepRunTitle}>{title}</span>
        <span className={styles.outputActions}>
          <button type="button" className={styles.linkBtn} onClick={() => onDownload(title, text)} disabled={exporting || !text}>
            <Icon name={exporting ? 'Loader2' : 'FileDown'} size={13} className={exporting ? styles.spin : undefined} /> Download .docx
          </button>
          <button type="button" className={styles.linkBtn} onClick={() => appNavigate('/drafting')} disabled={!text}>
            <Icon name="PenLine" size={13} /> Open Drafting
          </button>
        </span>
      </div>
      {references.length > 0 && (
        <div className={styles.muted}>
          Built from {references.map((r) => (r.kind === 'step' ? `step ${r.name}` : r.filename)).join(', ')}.
        </div>
      )}
      {missing.length > 0 && (
        <div className={styles.countWarn}>Not available when drafting: {missing.map((m) => `{{${m}}}`).join(', ')}.</div>
      )}
      {removed.length > 0 && (
        <div className={styles.countWarn}>{removed.length} unsupported case reference{removed.length === 1 ? '' : 's'} removed by the case-law guard.</div>
      )}
      <pre className={styles.outputText}>{text}</pre>
      <AiNotice />
    </div>
  );
}
