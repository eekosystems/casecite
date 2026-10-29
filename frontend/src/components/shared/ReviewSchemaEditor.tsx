import { useState } from 'react';
import { Icon } from './Icon';
import type { ReviewFieldSpec, ReviewFieldType, ReviewSchemaTemplate } from '../../api/types';

interface ReviewSchemaEditorProps {
  fields: ReviewFieldSpec[];
  onChange: (fields: ReviewFieldSpec[]) => void;
  /** Built-in and saved templates to load fields from. */
  templates?: ReviewSchemaTemplate[];
  /** Offered when the list is empty, or as "add" when it is not. */
  onAddStandardFields?: () => void;
  /** Save the current fields as a reusable template. */
  onSaveTemplate?: (name: string, shareWithMatter: boolean) => Promise<unknown>;
  canShareWithMatter?: boolean;
  onDeleteTemplate?: (schemaId: string) => void;
  disabled?: boolean;
  styles: Record<string, string>;
}

const FIELD_TYPES: Array<{ value: ReviewFieldType; label: string }> = [
  { value: 'text', label: 'Text' },
  { value: 'date', label: 'Date' },
  { value: 'money', label: 'Money' },
  { value: 'number', label: 'Number' },
  { value: 'boolean', label: 'Yes / no' },
  { value: 'duration', label: 'Duration' },
];

/** Merge a template's fields into the list, skipping names already present. */
export function mergeTemplateFields(current: ReviewFieldSpec[], template: ReviewFieldSpec[]): ReviewFieldSpec[] {
  const names = new Set(current.map((f) => f.name.trim().toLowerCase()));
  const kept = current.filter((f) => f.name.trim());
  const added = template.filter((f) => !names.has(f.name.trim().toLowerCase())).map((f) => ({ name: f.name, description: f.description || '', type: f.type }));
  return [...kept, ...added];
}

/** The columns of a review table: a name, a type and an optional hint for the model; reorderable, loadable from a template. */
export function ReviewSchemaEditor({
  fields, onChange, templates, onAddStandardFields, onSaveTemplate, canShareWithMatter, onDeleteTemplate, disabled, styles,
}: ReviewSchemaEditorProps) {
  const [templateId, setTemplateId] = useState('');
  const [saveOpen, setSaveOpen] = useState(false);
  const [templateName, setTemplateName] = useState('');
  const [share, setShare] = useState(false);
  const [saving, setSaving] = useState(false);

  const update = (index: number, patch: Partial<ReviewFieldSpec>) =>
    onChange(fields.map((f, i) => (i === index ? { ...f, ...patch } : f)));
  const remove = (index: number) => onChange(fields.filter((_f, i) => i !== index));
  const add = () => onChange([...fields, { name: '', description: '', type: 'text' }]);
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= fields.length) return;
    const next = [...fields];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };
  const loadTemplate = (id: string, replace: boolean) => {
    const t = templates?.find((x) => x.id === id);
    if (!t) return;
    onChange(replace ? t.fields.map((f) => ({ name: f.name, description: f.description || '', type: f.type })) : mergeTemplateFields(fields, t.fields));
  };
  const submitSave = async () => {
    if (!onSaveTemplate) return;
    setSaving(true);
    const result = await onSaveTemplate(templateName, share);
    setSaving(false);
    if (result) { setSaveOpen(false); setTemplateName(''); }
  };
  const selectedTemplate = templates?.find((t) => t.id === templateId);

  return (
    <div className={styles.schema}>
      {templates && templates.length > 0 && (
        <div className={styles.templateRow}>
          <Icon name="LayoutTemplate" size={14} className={styles.templateIcon} />
          <select className={styles.select} value={templateId} onChange={(e) => setTemplateId(e.target.value)} disabled={disabled} aria-label="Template">
            <option value="">Start from a template…</option>
            <optgroup label="Built in">
              {templates.filter((t) => t.is_builtin).map((t) => <option key={t.id} value={t.id}>{t.name} ({t.fields.length})</option>)}
            </optgroup>
            {templates.some((t) => !t.is_builtin) && (
              <optgroup label="Saved">
                {templates.filter((t) => !t.is_builtin).map((t) => <option key={t.id} value={t.id}>{t.name} ({t.fields.length}){t.matter_id ? ' · matter' : ''}</option>)}
              </optgroup>
            )}
          </select>
          <button type="button" className={styles.secondaryBtnSm} onClick={() => loadTemplate(templateId, true)} disabled={disabled || !templateId} title="Replace the fields with the template's">Use</button>
          <button type="button" className={styles.secondaryBtnSm} onClick={() => loadTemplate(templateId, false)} disabled={disabled || !templateId} title="Add the template's fields to the list">Add to list</button>
          {selectedTemplate && !selectedTemplate.is_builtin && selectedTemplate.role === 'owner' && onDeleteTemplate && (
            <button type="button" className={styles.iconBtn} onClick={() => { onDeleteTemplate(selectedTemplate.id); setTemplateId(''); }} title="Delete this saved template" aria-label="Delete template">
              <Icon name="Trash2" size={13} />
            </button>
          )}
          {selectedTemplate?.description && <span className={styles.templateHint}>{selectedTemplate.description}</span>}
        </div>
      )}
      {fields.length === 0 && (
        <div className={styles.schemaEmpty}>
          No fields yet. Add the facts you want pulled out of every document — each becomes a column.
        </div>
      )}
      {fields.map((field, index) => (
        <div key={field.key || `new-${index}`} className={styles.schemaRow}>
          <span className={styles.schemaIndex}>{index + 1}</span>
          <input
            className={styles.input}
            placeholder="Field name, e.g. Governing law"
            value={field.name}
            onChange={(e) => update(index, { name: e.target.value })}
            disabled={disabled}
            aria-label={`Field ${index + 1} name`}
          />
          <select
            className={styles.select}
            value={field.type}
            onChange={(e) => update(index, { type: e.target.value as ReviewFieldType })}
            disabled={disabled}
            aria-label={`Field ${index + 1} type`}
          >
            {FIELD_TYPES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
          <input
            className={`${styles.input} ${styles.inputWide}`}
            placeholder="What to look for (optional hint for the model)"
            value={field.description || ''}
            onChange={(e) => update(index, { description: e.target.value })}
            disabled={disabled}
            aria-label={`Field ${index + 1} description`}
          />
          <span className={styles.schemaRowTools}>
            <button type="button" className={styles.iconBtn} onClick={() => move(index, -1)} disabled={disabled || index === 0} title="Move up" aria-label={`Move field ${index + 1} up`}>
              <Icon name="ChevronUp" size={13} />
            </button>
            <button type="button" className={styles.iconBtn} onClick={() => move(index, 1)} disabled={disabled || index === fields.length - 1} title="Move down" aria-label={`Move field ${index + 1} down`}>
              <Icon name="ChevronDown" size={13} />
            </button>
            <button type="button" className={styles.iconBtn} onClick={() => remove(index)} disabled={disabled} title="Remove field" aria-label={`Remove field ${index + 1}`}>
              <Icon name="X" size={14} />
            </button>
          </span>
        </div>
      ))}
      <div className={styles.schemaActions}>
        <button type="button" className={styles.secondaryBtn} onClick={add} disabled={disabled}>
          <Icon name="Plus" size={14} /> Add field
        </button>
        {onAddStandardFields && (
          <button type="button" className={styles.secondaryBtn} onClick={onAddStandardFields} disabled={disabled}>
            <Icon name="Sparkles" size={14} /> Add standard contract fields
          </button>
        )}
        {onSaveTemplate && fields.some((f) => f.name.trim()) && !saveOpen && (
          <button type="button" className={styles.secondaryBtn} onClick={() => setSaveOpen(true)} disabled={disabled}>
            <Icon name="Save" size={14} /> Save as template
          </button>
        )}
      </div>
      {saveOpen && (
        <form className={styles.templateSave} onSubmit={(e) => { e.preventDefault(); void submitSave(); }}>
          <input className={styles.input} placeholder="Template name" value={templateName} onChange={(e) => setTemplateName(e.target.value)} aria-label="Template name" autoFocus />
          {canShareWithMatter && (
            <label className={styles.templateShare}>
              <input type="checkbox" checked={share} onChange={(e) => setShare(e.target.checked)} /> Share with the active matter
            </label>
          )}
          <button type="submit" className={styles.primaryBtnSm} disabled={saving || !templateName.trim()}>
            <Icon name={saving ? 'Loader2' : 'Check'} size={12} className={saving ? styles.spin : undefined} /> Save
          </button>
          <button type="button" className={styles.secondaryBtnSm} onClick={() => setSaveOpen(false)}>Cancel</button>
        </form>
      )}
    </div>
  );
}
