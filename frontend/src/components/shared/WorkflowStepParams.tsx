import { ReviewSchemaEditor } from './ReviewSchemaEditor';
import { WorkflowDocumentSelect } from './WorkflowDocumentSelect';
import type { DeepResearchTool, MatterMember, ReviewFieldSpec, WorkflowStep } from '../../api/types';
import type { RagDocument } from '../../types';

interface WorkflowStepParamsProps {
  step: WorkflowStep;
  /** Names of the steps before this one: the only ones it may refer to. */
  earlierSteps: string[];
  /** Documents the pickers offer (the active matter's, or everything readable). */
  documents: RagDocument[];
  documentsLoading: boolean;
  /** Names for ids the document list does not know. */
  knownRefs?: Array<{ id: string; filename?: string | null }>;
  /** Members of the workflow's matter, for naming approvers (empty without a matter). */
  members?: MatterMember[];
  disabled?: boolean;
  onChange: (params: Record<string, unknown>) => void;
  styles: Record<string, string>;
}

const TOOLS: Array<{ value: DeepResearchTool; label: string }> = [
  { value: 'documents', label: 'Firm documents' },
  { value: 'case_law', label: 'Case law' },
];

const str = (v: unknown) => (typeof v === 'string' ? v : '');
const list = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

/** The parameter form for one step, by kind. Nothing here is code: every field maps to a validated server parameter. */
export function WorkflowStepParams({
  step, earlierSteps, documents, documentsLoading, knownRefs, members = [], disabled, onChange, styles,
}: WorkflowStepParamsProps) {
  const p = step.params || {};
  const set = (patch: Record<string, unknown>) => onChange({ ...p, ...patch });
  const toggleIn = (key: string, name: string) => {
    const current = list(p[key]);
    set({ [key]: current.includes(name) ? current.filter((n) => n !== name) : [...current, name] });
  };

  const stepPicker = (key: string, label: string, hint: string) => (
    <div className={styles.paramRow}>
      <span className={styles.paramLabel}>{label}</span>
      {earlierSteps.length === 0 ? (
        <span className={styles.paramHint}>{hint}</span>
      ) : (
        <div className={styles.checkRow}>
          {earlierSteps.map((name) => (
            <label key={name} className={styles.checkLabel}>
              <input
                type="checkbox"
                checked={list(p[key]).includes(name)}
                onChange={() => toggleIn(key, name)}
                disabled={disabled}
              />
              {name}
            </label>
          ))}
        </div>
      )}
    </div>
  );

  switch (step.kind) {
    case 'extract_fields':
      return (
        <div className={styles.params}>
          <ReviewSchemaEditor
            fields={(p.fields as ReviewFieldSpec[]) || []}
            onChange={(fields) => set({ fields })}
            disabled={disabled}
            styles={styles}
          />
          <input
            className={styles.input}
            placeholder="Name for the review table it creates (optional)"
            value={str(p.table_name)}
            onChange={(e) => set({ table_name: e.target.value })}
            disabled={disabled}
            aria-label="Review table name"
          />
        </div>
      );
    case 'analyze_contract':
      return (
        <div className={styles.params}>
          <div className={styles.paramRow}>
            <input className={styles.input} placeholder="Contract type (auto)" value={str(p.contract_type)} onChange={(e) => set({ contract_type: e.target.value || 'auto' })} disabled={disabled} aria-label="Contract type" />
            <input className={styles.input} placeholder="Jurisdiction (optional)" value={str(p.jurisdiction)} onChange={(e) => set({ jurisdiction: e.target.value })} disabled={disabled} aria-label="Jurisdiction" />
            <input className={styles.input} placeholder="Representing (optional)" value={str(p.representing)} onChange={(e) => set({ representing: e.target.value })} disabled={disabled} aria-label="Representing" />
            <select className={styles.select} value={str(p.posture) || 'balanced'} onChange={(e) => set({ posture: e.target.value })} disabled={disabled} aria-label="Posture">
              <option value="balanced">Balanced</option>
              <option value="strict">Strict</option>
            </select>
          </div>
          <textarea className={styles.textarea} placeholder="Standing instructions for every review (optional)" value={str(p.instructions)} onChange={(e) => set({ instructions: e.target.value })} disabled={disabled} rows={2} aria-label="Review instructions" />
        </div>
      );
    case 'compare_contracts': {
      const mode = str(p.baseline) === 'document' || p.baseline_document_id ? 'document' : 'first_input';
      return (
        <div className={styles.params}>
          <div className={styles.paramRow}>
            <span className={styles.paramLabel}>Baseline</span>
            <select
              className={styles.select}
              value={mode}
              onChange={(e) => set(e.target.value === 'document' ? { baseline: 'document' } : { baseline: 'first_input', baseline_document_id: null })}
              disabled={disabled}
              aria-label="Baseline source"
            >
              <option value="first_input">The first document the step reads</option>
              <option value="document">A fixed document</option>
            </select>
            {mode === 'document' && (
              <WorkflowDocumentSelect
                documents={documents}
                loading={documentsLoading}
                knownRefs={knownRefs}
                selectedIds={p.baseline_document_id ? [String(p.baseline_document_id)] : []}
                onChange={(ids) => set({ baseline: 'document', baseline_document_id: ids[0] || null })}
                single
                placeholder="Pick the baseline document"
                disabled={disabled}
                ariaLabel="Baseline document"
                styles={styles}
              />
            )}
          </div>
          <input className={`${styles.input} ${styles.inputWide}`} placeholder="Focus, e.g. payment terms and liability (optional)" value={str(p.focus)} onChange={(e) => set({ focus: e.target.value })} disabled={disabled} aria-label="Comparison focus" />
          <span className={styles.paramHint}>Up to three documents are compared against the baseline.</span>
        </div>
      );
    }
    case 'draft_document':
      return (
        <div className={styles.params}>
          <textarea
            className={styles.textarea}
            placeholder="Drafting instructions. Use {{step.summary}} or {{step.data.field}} to pull in earlier outputs."
            value={str(p.instructions)}
            onChange={(e) => set({ instructions: e.target.value })}
            disabled={disabled}
            rows={4}
            aria-label="Drafting instructions"
          />
          {stepPicker('include_outputs_from', 'Attach outputs of', 'No earlier steps yet. Move this step down to attach their outputs.')}
          <div className={styles.paramRow}>
            <span className={styles.paramLabel}>Reference documents</span>
            <WorkflowDocumentSelect
              documents={documents}
              loading={documentsLoading}
              knownRefs={knownRefs}
              selectedIds={list(p.reference_document_ids)}
              onChange={(ids) => set({ reference_document_ids: ids })}
              max={4}
              placeholder="Add up to four"
              disabled={disabled}
              ariaLabel="Reference documents"
              styles={styles}
            />
          </div>
        </div>
      );
    case 'deep_research':
      return (
        <div className={styles.params}>
          <textarea
            className={styles.textarea}
            placeholder="Research question. Use {{step.data.values}} and similar to include earlier outputs."
            value={str(p.question)}
            onChange={(e) => set({ question: e.target.value })}
            disabled={disabled}
            rows={3}
            aria-label="Research question"
          />
          <div className={styles.paramRow}>
            <span className={styles.paramLabel}>Sources</span>
            <div className={styles.checkRow}>
              {TOOLS.map((t) => {
                const tools = list(p.tools);
                const checked = tools.length === 0 || tools.includes(t.value);
                return (
                  <label key={t.value} className={styles.checkLabel}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => {
                        const next = checked ? TOOLS.map((x) => x.value).filter((x) => x !== t.value) : [...tools, t.value];
                        set({ tools: next.length === 0 || next.length === TOOLS.length ? null : next });
                      }}
                      disabled={disabled}
                    />
                    {t.label}
                  </label>
                );
              })}
            </div>
          </div>
        </div>
      );
    case 'citation_check':
      return (
        <div className={styles.params}>
          <textarea
            className={styles.textarea}
            placeholder="Citations to check, one per line (optional)"
            value={list(p.citations).join('\n')}
            onChange={(e) => set({ citations: e.target.value.split('\n').map((c) => c.trim()).filter(Boolean) })}
            disabled={disabled}
            rows={2}
            aria-label="Citations"
          />
          <div className={styles.paramRow}>
            <span className={styles.paramLabel}>Also scan outputs of</span>
            <label className={styles.checkLabel}>
              <input
                type="checkbox"
                checked={p.from_steps === null || p.from_steps === undefined}
                onChange={(e) => set({ from_steps: e.target.checked ? null : [] })}
                disabled={disabled}
              />
              every earlier step
            </label>
            {Array.isArray(p.from_steps) && earlierSteps.map((name) => (
              <label key={name} className={styles.checkLabel}>
                <input type="checkbox" checked={list(p.from_steps).includes(name)} onChange={() => toggleIn('from_steps', name)} disabled={disabled} />
                {name}
              </label>
            ))}
          </div>
        </div>
      );
    case 'approval': {
      const approvers = p.approvers;
      const mode = Array.isArray(approvers) ? 'named' : str(approvers) || 'owner';
      const named = list(approvers);
      return (
        <div className={styles.params}>
          <div className={styles.paramRow}>
            <input className={styles.input} placeholder="What is being approved, e.g. Partner sign-off" value={str(p.title)} onChange={(e) => set({ title: e.target.value })} disabled={disabled} aria-label="Approval title" />
            <select
              className={styles.select}
              value={mode}
              onChange={(e) => set({ approvers: e.target.value === 'named' ? [] : e.target.value })}
              disabled={disabled}
              aria-label="Who approves"
            >
              <option value="owner">The workflow owner approves</option>
              <option value="any_member">Any member of the matter approves</option>
              <option value="named" disabled={members.length === 0}>Named members approve</option>
            </select>
          </div>
          {mode === 'named' && (
            <div className={styles.paramRow}>
              <span className={styles.paramLabel}>Approvers</span>
              <div className={styles.checkRow}>
                {members.map((m) => (
                  <label key={m.user_id} className={styles.checkLabel}>
                    <input
                      type="checkbox"
                      checked={named.includes(m.user_id)}
                      onChange={() => set({ approvers: named.includes(m.user_id) ? named.filter((x) => x !== m.user_id) : [...named, m.user_id] })}
                      disabled={disabled}
                    />
                    {m.name || m.email || m.user_id}
                  </label>
                ))}
              </div>
            </div>
          )}
          <textarea className={styles.textarea} placeholder="What the approver should check (optional)" value={str(p.instructions)} onChange={(e) => set({ instructions: e.target.value })} disabled={disabled} rows={2} aria-label="Approval instructions" />
          <span className={styles.paramHint}>The run pauses here and the approvers are told; it continues when one of them approves, or ends if they reject.</span>
        </div>
      );
    }
    default:
      return null;
  }
}
