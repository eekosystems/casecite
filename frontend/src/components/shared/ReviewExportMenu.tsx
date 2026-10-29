import { useEffect, useRef } from 'react';
import { Icon } from './Icon';
import type { ReviewExportFormat, ReviewExportOptions, ReviewFieldSpec } from '../../api/types';

interface ReviewExportMenuProps {
  open: boolean;
  options: ReviewExportOptions;
  fields: ReviewFieldSpec[];
  exporting: boolean;
  disabled: boolean;
  onToggle: (open: boolean) => void;
  onChange: (patch: Partial<ReviewExportOptions>) => void;
  onExport: () => void;
  styles: Record<string, string>;
}

const FORMATS: Array<{ value: ReviewExportFormat; label: string; icon: string; hint: string }> = [
  { value: 'xlsx', label: 'Excel', icon: 'Table2', hint: 'Results, Citations, Review and Summary sheets' },
  { value: 'csv', label: 'CSV', icon: 'FileText', hint: 'One row per document and field' },
  { value: 'json', label: 'JSON', icon: 'FileJson', hint: 'Every cell with its quote and review state' },
];
const STATUSES: Array<{ value: string; label: string }> = [
  { value: 'extracted', label: 'Verified' },
  { value: 'unverified', label: 'Unverified' },
  { value: 'not_found', label: 'Not found' },
  { value: 'error', label: 'Errors' },
  { value: 'approved', label: 'Approved' },
  { value: 'flagged', label: 'Flagged' },
];

/** The export button and its options popover: format, columns, which cells, quotes. */
export function ReviewExportMenu({ open, options, fields, exporting, disabled, onToggle, onChange, onExport, styles }: ReviewExportMenuProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onToggle(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onToggle(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey); };
  }, [open, onToggle]);

  const chosen = new Set(options.fields || []);
  const statuses = new Set(options.statuses || []);
  const toggleIn = (set: Set<string>, value: string) => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value); else next.add(value);
    return [...next];
  };

  return (
    <div className={styles.exportWrap} ref={ref}>
      <button type="button" className={`${styles.secondaryBtn} ${open ? styles.secondaryBtnActive : ''}`} onClick={() => onToggle(!open)} disabled={disabled || exporting} aria-haspopup="dialog" aria-expanded={open}>
        <Icon name={exporting ? 'Loader2' : 'Download'} size={14} className={exporting ? styles.spin : undefined} /> Export
      </button>
      {open && (
        <div className={styles.exportMenu} role="dialog" aria-label="Export options">
          <div className={styles.exportSection}>
            <div className={styles.exportLabel}>Format</div>
            <div className={styles.exportFormats}>
              {FORMATS.map((f) => (
                <button key={f.value} type="button" className={`${styles.exportFormat} ${options.format === f.value ? styles.exportFormatActive : ''}`} onClick={() => onChange({ format: f.value })} title={f.hint}>
                  <Icon name={f.icon} size={14} /> {f.label}
                </button>
              ))}
            </div>
          </div>
          <div className={styles.exportSection}>
            <div className={styles.exportLabel}>
              Columns
              <button type="button" className={styles.linkBtn} onClick={() => onChange({ fields: [] })}>all</button>
            </div>
            <div className={styles.exportChecks}>
              {fields.map((f) => {
                const key = f.key || f.name;
                const on = chosen.size === 0 || chosen.has(key);
                return (
                  <label key={key} className={styles.exportCheck}>
                    <input type="checkbox" checked={on} onChange={() => onChange({ fields: chosen.size === 0 ? fields.map((x) => x.key || x.name).filter((k) => k !== key) : toggleIn(chosen, key) })} />
                    {f.name}
                  </label>
                );
              })}
            </div>
          </div>
          <div className={styles.exportSection}>
            <div className={styles.exportLabel}>
              Cells
              <button type="button" className={styles.linkBtn} onClick={() => onChange({ statuses: [], reviewed_only: false })}>all</button>
            </div>
            <div className={styles.exportChecks}>
              <label className={styles.exportCheck}>
                <input type="checkbox" checked={Boolean(options.reviewed_only)} onChange={(e) => onChange({ reviewed_only: e.target.checked })} />
                Approved cells only
              </label>
              {STATUSES.map((st) => (
                <label key={st.value} className={styles.exportCheck}>
                  <input type="checkbox" checked={statuses.has(st.value)} onChange={() => onChange({ statuses: toggleIn(statuses, st.value) })} />
                  {st.label}
                </label>
              ))}
            </div>
          </div>
          <div className={styles.exportSection}>
            <label className={styles.exportCheck}>
              <input type="checkbox" checked={options.include_quotes !== false} onChange={(e) => onChange({ include_quotes: e.target.checked })} />
              Include the supporting quotes
            </label>
          </div>
          <div className={styles.exportActions}>
            <button type="button" className={styles.primaryBtn} onClick={onExport} disabled={exporting}>
              <Icon name={exporting ? 'Loader2' : 'Download'} size={14} className={exporting ? styles.spin : undefined} /> Download {options.format.toUpperCase()}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
