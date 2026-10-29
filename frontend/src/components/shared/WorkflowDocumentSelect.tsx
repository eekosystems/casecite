import { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from './Icon';
import type { RagDocument } from '../../types';
import { documentName } from '../../utils/workflowDiff';

interface WorkflowDocumentSelectProps {
  documents: RagDocument[];
  loading: boolean;
  /** Names for ids the current document list does not know (another matter's, deleted). */
  knownRefs?: Array<{ id: string; filename?: string | null }>;
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  /** One document only (a baseline). */
  single?: boolean;
  /** Most documents that may be picked (references are capped at four). */
  max?: number;
  placeholder: string;
  disabled?: boolean;
  ariaLabel: string;
  styles: Record<string, string>;
}

/**
 * An inline document picker for a step's parameters: the chosen documents as
 * chips with their names, and a searchable list to add more. Replaces typing
 * document ids by hand.
 */
export function WorkflowDocumentSelect({
  documents, loading, knownRefs = [], selectedIds, onChange, single, max, placeholder, disabled, ariaLabel, styles,
}: WorkflowDocumentSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const refs = useMemo(
    () => [...documents.map((d) => ({ id: d.id, filename: d.filename })), ...knownRefs],
    [documents, knownRefs],
  );
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const indexed = documents.filter((d) => d.status === 'indexed');
    return q ? indexed.filter((d) => d.filename.toLowerCase().includes(q) || (d.folder_path || '').toLowerCase().includes(q)) : indexed;
  }, [documents, query]);

  const full = !single && typeof max === 'number' && selectedIds.length >= max;
  const pick = (id: string) => {
    if (single) {
      onChange([id]);
      setOpen(false);
      return;
    }
    if (selectedIds.includes(id)) onChange(selectedIds.filter((x) => x !== id));
    else if (!full) onChange([...selectedIds, id]);
  };

  return (
    <div className={styles.docSelect} ref={wrapRef}>
      <div className={styles.docChips}>
        {selectedIds.map((id) => (
          <span key={id} className={styles.docChip} title={id}>
            <Icon name="FileText" size={12} />
            <span className={styles.docChipName}>{documentName(id, refs)}</span>
            {!disabled && (
              <button
                type="button"
                className={styles.docChipRemove}
                onClick={() => onChange(selectedIds.filter((x) => x !== id))}
                aria-label={`Remove ${documentName(id, refs)}`}
              >
                <Icon name="X" size={11} />
              </button>
            )}
          </span>
        ))}
        <button
          type="button"
          className={styles.docAddBtn}
          onClick={() => setOpen((o) => !o)}
          disabled={disabled || (full && !single)}
          aria-label={ariaLabel}
          aria-expanded={open}
        >
          <Icon name={single && selectedIds.length ? 'Pencil' : 'Plus'} size={12} />
          {selectedIds.length === 0 ? placeholder : single ? 'Change' : full ? `Up to ${max}` : 'Add'}
        </button>
      </div>
      {open && (
        <div className={styles.docMenu} role="listbox" aria-label={ariaLabel}>
          <input
            className={styles.docSearch}
            placeholder="Search documents"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search documents"
            autoFocus
          />
          <div className={styles.docList}>
            {loading ? (
              <div className={styles.docEmpty}><Icon name="Loader2" size={13} className={styles.spin} /> Loading…</div>
            ) : visible.length === 0 ? (
              <div className={styles.docEmpty}>No indexed documents match.</div>
            ) : (
              visible.map((d) => {
                const on = selectedIds.includes(d.id);
                return (
                  <button
                    key={d.id}
                    type="button"
                    role="option"
                    aria-selected={on}
                    className={`${styles.docOption} ${on ? styles.docOptionOn : ''}`}
                    onClick={() => pick(d.id)}
                  >
                    <Icon name={on ? 'CheckCircle2' : 'FileText'} size={13} />
                    <span className={styles.docOptionName}>{d.filename}</span>
                    {d.folder_path && <span className={styles.docOptionFolder}>{d.folder_path}</span>}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
