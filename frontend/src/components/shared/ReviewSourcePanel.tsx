import { useEffect, useState } from 'react';
import { Icon } from './Icon';
import { ReviewSourceText } from './ReviewSourceText';
import { locateQuote } from '../../utils/reviewGrid';
import type { SourceTextState } from '../../stores/reviewTablesStore';
import type { ReviewCell, ReviewCellReviewInput, ReviewFieldSpec, ReviewTableDocument } from '../../api/types';

interface ReviewSourcePanelProps {
  cell: ReviewCell | null;
  field: ReviewFieldSpec | undefined;
  document: ReviewTableDocument | undefined;
  source: SourceTextState | undefined;
  running: boolean;
  /** When true the override editor opens focused (the E shortcut). */
  editing: boolean;
  onReview: (change: ReviewCellReviewInput) => Promise<boolean> | void;
  onRerun: () => void;
  onRetryText: () => void;
  onClose: () => void;
  styles: Record<string, string>;
}

const STATUS_LABEL: Record<string, string> = {
  extracted: 'Verified against the document',
  unverified: 'Unverified: the quote was not found in the document',
  not_found: 'Not found in the document',
  error: 'Extraction failed',
};

const HISTORY_LABEL: Record<string, string> = {
  model: 'Model answered',
  override: 'Overridden',
  approve: 'Approved',
  flag: 'Flagged',
  clear: 'Cleared',
};

/**
 * The evidence and the review controls behind one cell: the document text
 * with the quote highlighted, the model's value against the reviewer's
 * override, approve / flag, a note, and the cell's history.
 */
export function ReviewSourcePanel({
  cell, field, document, source, running, editing, onReview, onRerun, onRetryText, onClose, styles,
}: ReviewSourcePanelProps) {
  const [draft, setDraft] = useState('');
  const [note, setNote] = useState('');
  const [editOpen, setEditOpen] = useState(false);
  const [seq, setSeq] = useState(0);
  const cellId = `${cell?.document_id}:${cell?.field_key}`;

  useEffect(() => {
    setDraft(cell?.override_value ?? '');
    setNote(cell?.note ?? '');
    setEditOpen(editing);
  }, [cellId, editing, cell?.override_value, cell?.note]);

  if (!field || !document) return null;
  const status = cell?.status || 'pending';
  const text = source?.status === 'ready' ? source.data.text : null;
  const highlight = text ? locateQuote(text, cell) : null;
  const approved = cell?.review_status === 'approved';
  const flagged = cell?.review_status === 'flagged';

  const saveOverride = async () => {
    const value = draft.trim();
    if (await onReview({ override_value: value || null })) setEditOpen(false);
  };
  const saveNote = () => onReview({ note: note.trim() || null });

  return (
    <aside className={styles.sourcePanel} aria-label="Cell source and review">
      <div className={styles.sourceHead}>
        <div className={styles.sourceTitles}>
          <div className={styles.sourceTitle}>{field.name}</div>
          <div className={styles.sourceSub} title={document.filename}>{document.filename}</div>
        </div>
        <div className={styles.sourceHeadActions}>
          {highlight && (
            <button type="button" className={styles.iconBtn} onClick={() => setSeq((n) => n + 1)} title="Scroll to the quote" aria-label="Scroll to the quote">
              <Icon name="Crosshair" size={14} />
            </button>
          )}
          <button type="button" className={styles.iconBtn} onClick={onClose} aria-label="Close source panel">
            <Icon name="PanelRightClose" size={14} />
          </button>
        </div>
      </div>

      <div className={styles.sourceReview}>
        <div className={`${styles.statusLine} ${styles[`status_${status}`] || ''}`}>
          <Icon name={status === 'extracted' ? 'CheckCircle' : status === 'error' ? 'XCircle' : status === 'pending' ? 'Clock' : 'AlertTriangle'} size={14} />
          {cell ? STATUS_LABEL[status] || status : 'Not extracted yet'}
          {highlight && !highlight.exact && <span className={styles.approxTag}>approximate match</span>}
        </div>

        <div className={styles.valueRow}>
          <div className={styles.valueBlock}>
            <div className={styles.sourceLabel}>Model value</div>
            <div className={styles.sourceValue}>{cell?.value || <span className={styles.muted}>none</span>}</div>
          </div>
          <div className={styles.valueBlock}>
            <div className={styles.sourceLabel}>
              Reviewer override
              {cell?.override_value !== null && cell?.override_value !== undefined && !editOpen && (
                <button type="button" className={styles.linkBtn} onClick={() => onReview({ override_value: null })} disabled={running}>clear</button>
              )}
            </div>
            {editOpen ? (
              <form className={styles.overrideForm} onSubmit={(e) => { e.preventDefault(); void saveOverride(); }}>
                <input
                  className={styles.input}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder="Type the correct value"
                  aria-label="Override value"
                  autoFocus
                  onKeyDown={(e) => { if (e.key === 'Escape') setEditOpen(false); }}
                />
                <button type="submit" className={styles.primaryBtnSm}><Icon name="Check" size={12} /> Save</button>
                <button type="button" className={styles.secondaryBtnSm} onClick={() => setEditOpen(false)}>Cancel</button>
              </form>
            ) : (
              <button type="button" className={styles.overrideValue} onClick={() => setEditOpen(true)} title="Edit the value (E)">
                {cell?.override_value ?? <span className={styles.muted}>Click to override · E</span>}
                <Icon name="Pencil" size={11} />
              </button>
            )}
          </div>
        </div>

        <div className={styles.reviewActions}>
          <button type="button" className={`${styles.reviewBtn} ${approved ? styles.reviewBtnApproved : ''}`} onClick={() => onReview({ review_status: approved ? 'pending' : 'approved' })} disabled={running} title="Approve (A)">
            <Icon name="CheckCircle2" size={13} /> {approved ? 'Approved' : 'Approve'}
          </button>
          <button type="button" className={`${styles.reviewBtn} ${flagged ? styles.reviewBtnFlagged : ''}`} onClick={() => onReview({ review_status: flagged ? 'pending' : 'flagged' })} disabled={running} title="Flag for a colleague (F)">
            <Icon name={flagged ? 'FlagOff' : 'Flag'} size={13} /> {flagged ? 'Flagged' : 'Flag'}
          </button>
          <button type="button" className={styles.secondaryBtnSm} onClick={onRerun} disabled={running || !document.accessible} title="Ask the model again for this cell">
            <Icon name="RefreshCw" size={12} /> Re-run
          </button>
        </div>

        <div className={styles.noteBlock}>
          <div className={styles.sourceLabel}>Note</div>
          <textarea
            className={styles.noteInput}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            onBlur={() => { if ((note.trim() || null) !== (cell?.note ?? null)) void saveNote(); }}
            placeholder="Why you approved, flagged or changed this"
            rows={2}
            aria-label="Review note"
          />
        </div>

        {cell?.reviewed_by && (
          <div className={styles.sourceMeta}>
            Reviewed by {cell.reviewed_by}{cell.reviewed_at ? ` · ${new Date(cell.reviewed_at).toLocaleString()}` : ''}
          </div>
        )}
        {cell?.error && <div className={styles.sourceError}>{cell.error}</div>}
      </div>

      <div className={styles.sourceDoc}>
        <div className={styles.sourceLabelRow}>
          <span className={styles.sourceLabel}>
            Source{cell?.span_start !== null && cell?.span_start !== undefined && cell.span_end !== null ? ` · chars ${cell.span_start}–${cell.span_end}` : ''}
          </span>
          {cell?.quote && !text && <span className={styles.muted}>quote only</span>}
        </div>
        {source?.status === 'loading' && <div className={styles.sourceEmpty}><Icon name="Loader2" size={13} className={styles.spin} /> Loading document text…</div>}
        {source?.status === 'error' && (
          <div className={styles.sourceEmpty}>
            {source.message}
            <button type="button" className={styles.linkBtn} onClick={onRetryText}>retry</button>
          </div>
        )}
        {text ? (
          <div className={styles.sourceScroll}>
            <ReviewSourceText text={text} highlight={highlight} seq={seq} />
          </div>
        ) : cell?.quote && source?.status !== 'loading' ? (
          <blockquote className={styles.quote}>{cell.quote}</blockquote>
        ) : null}
        {text && cell?.quote && !highlight && <div className={styles.muted}>The quote could not be located in the current text.</div>}
      </div>

      {cell && cell.history.length > 0 && (
        <details className={styles.history}>
          <summary className={styles.historySummary}><Icon name="History" size={12} /> History ({cell.history.length})</summary>
          <ul className={styles.historyList}>
            {[...cell.history].reverse().map((h, i) => (
              <li key={i} className={styles.historyItem}>
                <span className={styles.historyKind}>{HISTORY_LABEL[h.kind] || h.kind}</span>
                {h.value !== undefined && h.value !== null && <span className={styles.historyValue}>“{h.value}”</span>}
                {h.status && h.kind === 'model' && <span className={styles.historyStatus}>{h.status}</span>}
                <span className={styles.historyWho}>{h.by}{h.at ? ` · ${new Date(h.at).toLocaleString()}` : ''}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className={styles.sourceMeta}>
        {cell?.model_used && <span>{cell.model_used}</span>}
        {cell?.updated_at && <span>{new Date(cell.updated_at).toLocaleString()}</span>}
      </div>
    </aside>
  );
}
