import { Icon } from './Icon';
import { appNavigate } from '../../utils/router';

interface WorkflowOutputExtractProps {
  data: Record<string, unknown>;
  styles: Record<string, string>;
}

interface Cell { value: string | null; status?: string; verified?: boolean }

const PREVIEW_ROWS = 6;
const PREVIEW_COLS = 6;

/** The first rows of the review table an extract step created, with a link to the full grid. */
export function WorkflowOutputExtract({ data, styles }: WorkflowOutputExtractProps) {
  const tableId = typeof data.table_id === 'string' ? data.table_id : null;
  const fields = (Array.isArray(data.fields) ? data.fields : []) as Array<{ key: string; name: string }>;
  const documents = (Array.isArray(data.documents) ? data.documents : []) as Array<{ id: string; filename: string }>;
  const rows = (data.rows || {}) as Record<string, Record<string, Cell>>;
  const counts = (data.counts || {}) as Record<string, number>;
  const shownFields = fields.slice(0, PREVIEW_COLS);
  const shownDocs = documents.slice(0, PREVIEW_ROWS);

  return (
    <div className={styles.outputBlock}>
      <div className={styles.outputHead}>
        <span className={styles.outputCounts}>
          <span className={styles.countOk}>{counts.extracted ?? 0} verified</span>
          <span className={styles.countWarn}>{counts.unverified ?? 0} unverified</span>
          <span className={styles.countMuted}>{counts.not_found ?? 0} not found</span>
          {counts.errors ? <span className={styles.countBad}>{counts.errors} failed</span> : null}
        </span>
        {tableId && (
          <button type="button" className={styles.linkBtn} onClick={() => appNavigate(`/review-tables/${encodeURIComponent(tableId)}`)}>
            <Icon name="Table2" size={13} /> Open review table
          </button>
        )}
      </div>
      {shownDocs.length > 0 && shownFields.length > 0 && (
        <div className={styles.previewWrap}>
          <table className={styles.previewTable}>
            <thead>
              <tr>
                <th>Document</th>
                {shownFields.map((f) => <th key={f.key}>{f.name}</th>)}
              </tr>
            </thead>
            <tbody>
              {shownDocs.map((d) => (
                <tr key={d.id}>
                  <td className={styles.previewDoc} title={d.filename}>{d.filename}</td>
                  {shownFields.map((f) => {
                    const cell = rows[d.id]?.[f.key];
                    const cls = !cell || cell.status === 'not_found' ? styles.previewEmpty
                      : cell.status === 'error' ? styles.previewBad
                      : cell.verified ? '' : styles.previewWarn;
                    return (
                      <td key={f.key} className={cls} title={cell?.status}>
                        {cell?.value ?? '—'}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {(documents.length > PREVIEW_ROWS || fields.length > PREVIEW_COLS) && (
            <div className={styles.muted}>
              Showing {shownDocs.length} of {documents.length} documents and {shownFields.length} of {fields.length} fields.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
