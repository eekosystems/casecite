import { useState } from 'react';
import { Icon } from './Icon';
import { WorkflowStepParams } from './WorkflowStepParams';
import { WorkflowConditionEditor } from './WorkflowConditionEditor';
import { WorkflowDocumentSelect } from './WorkflowDocumentSelect';
import { STEP_KINDS, newStep, stepKindInfo } from '../../stores/workflowsStore';
import { fieldLabel, issuesForStep } from '../../utils/workflowDiff';
import type { MatterMember, WorkflowInputSource, WorkflowStep, WorkflowStepIssue, WorkflowStepKind } from '../../api/types';
import type { RagDocument } from '../../types';

interface WorkflowStepListProps {
  steps: WorkflowStep[];
  onChange: (steps: WorkflowStep[]) => void;
  /** Problems the server found with the draft, shown under the step they concern. */
  issues?: WorkflowStepIssue[];
  documents: RagDocument[];
  documentsLoading: boolean;
  knownRefs?: Array<{ id: string; filename?: string | null }>;
  members?: MatterMember[];
  disabled?: boolean;
  styles: Record<string, string>;
}

/** The ordered step editor: each step's name, inputs, condition and parameter form; add, reorder, remove. */
export function WorkflowStepList({
  steps, onChange, issues = [], documents, documentsLoading, knownRefs, members, disabled, styles,
}: WorkflowStepListProps) {
  const [adding, setAdding] = useState(false);

  const update = (index: number, patch: Partial<WorkflowStep>) =>
    onChange(steps.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  const remove = (index: number) => onChange(steps.filter((_s, i) => i !== index));
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= steps.length) return;
    const next = [...steps];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };
  const add = (kind: WorkflowStepKind) => {
    onChange([...steps, newStep(kind, steps)]);
    setAdding(false);
  };
  const general = issuesForStep(issues, -1);

  return (
    <div className={styles.stepList}>
      {steps.length === 0 && (
        <div className={styles.schemaEmpty}>No steps yet. Add the first operation the workflow should run.</div>
      )}
      {general.length > 0 && (
        <ul className={styles.issueList} aria-label="Workflow problems">
          {general.map((i, n) => <li key={n}><Icon name="AlertCircle" size={12} /> {i.message}</li>)}
        </ul>
      )}
      {steps.map((step, index) => {
        const info = stepKindInfo(step.kind);
        const earlier = steps.slice(0, index).map((s) => s.name).filter(Boolean);
        const stepIssues = issuesForStep(issues, index);
        return (
          <div key={`${index}-${step.kind}`} className={`${styles.stepCard} ${stepIssues.length ? styles.stepCardInvalid : ''}`}>
            <div className={styles.stepHead}>
              <span className={styles.stepIndex}>{index + 1}</span>
              <span className={styles.stepKind} title={info.desc}>
                <Icon name={info.icon} size={14} /> {info.title}
              </span>
              <input
                className={styles.stepName}
                value={step.name}
                onChange={(e) => update(index, { name: e.target.value })}
                placeholder="step name"
                disabled={disabled}
                aria-label={`Step ${index + 1} name`}
                title="Other steps refer to this step by name"
              />
              {step.kind !== 'approval' && (
                <select
                  className={styles.select}
                  value={step.inputs.source}
                  onChange={(e) => {
                    const source = e.target.value as WorkflowInputSource;
                    update(index, { inputs: { source, document_ids: [], step: source === 'step' ? earlier[0] || null : null } });
                  }}
                  disabled={disabled}
                  aria-label={`Step ${index + 1} inputs`}
                >
                  <option value="run">Reads the run&apos;s documents</option>
                  <option value="step" disabled={earlier.length === 0}>Reads an earlier step&apos;s documents</option>
                  <option value="documents">Reads fixed documents</option>
                </select>
              )}
              {step.inputs.source === 'step' && (
                <select
                  className={styles.select}
                  value={step.inputs.step || ''}
                  onChange={(e) => update(index, { inputs: { ...step.inputs, step: e.target.value || null } })}
                  disabled={disabled}
                  aria-label={`Step ${index + 1} source step`}
                >
                  {earlier.map((name) => <option key={name} value={name}>{name}</option>)}
                </select>
              )}
              <div className={styles.stepTools}>
                <button type="button" className={styles.iconBtn} onClick={() => move(index, -1)} disabled={disabled || index === 0} title="Move up" aria-label={`Move step ${index + 1} up`}>
                  <Icon name="ChevronUp" size={14} />
                </button>
                <button type="button" className={styles.iconBtn} onClick={() => move(index, 1)} disabled={disabled || index === steps.length - 1} title="Move down" aria-label={`Move step ${index + 1} down`}>
                  <Icon name="ChevronDown" size={14} />
                </button>
                <button type="button" className={styles.iconBtn} onClick={() => remove(index)} disabled={disabled} title="Remove step" aria-label={`Remove step ${index + 1}`}>
                  <Icon name="X" size={14} />
                </button>
              </div>
            </div>
            {step.inputs.source === 'documents' && (
              <div className={styles.paramRow}>
                <span className={styles.paramLabel}>Fixed documents</span>
                <WorkflowDocumentSelect
                  documents={documents}
                  loading={documentsLoading}
                  knownRefs={knownRefs}
                  selectedIds={step.inputs.document_ids}
                  onChange={(ids) => update(index, { inputs: { ...step.inputs, document_ids: ids } })}
                  placeholder="Pick the documents this step always reads"
                  disabled={disabled}
                  ariaLabel={`Step ${index + 1} documents`}
                  styles={styles}
                />
              </div>
            )}
            <WorkflowStepParams
              step={step}
              earlierSteps={earlier}
              documents={documents}
              documentsLoading={documentsLoading}
              knownRefs={knownRefs}
              members={members}
              disabled={disabled}
              onChange={(params) => update(index, { params })}
              styles={styles}
            />
            <WorkflowConditionEditor
              condition={step.condition}
              earlierSteps={earlier}
              disabled={disabled}
              onChange={(condition) => update(index, { condition })}
              styles={styles}
            />
            {stepIssues.length > 0 && (
              <ul className={styles.issueList} aria-label={`Step ${index + 1} problems`}>
                {stepIssues.map((i, n) => (
                  <li key={n}><Icon name="AlertCircle" size={12} /> <strong>{fieldLabel(i.field)}:</strong> {i.message}</li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
      <div className={styles.schemaActions}>
        {adding ? (
          <div className={styles.kindMenu} role="menu">
            {STEP_KINDS.map((k) => (
              <button key={k.kind} type="button" className={styles.kindOption} onClick={() => add(k.kind)} role="menuitem">
                <Icon name={k.icon} size={14} />
                <span>
                  <strong>{k.title}</strong>
                  <small>{k.desc}</small>
                </span>
              </button>
            ))}
            <button type="button" className={styles.secondaryBtn} onClick={() => setAdding(false)}>Cancel</button>
          </div>
        ) : (
          <button type="button" className={styles.secondaryBtn} onClick={() => setAdding(true)} disabled={disabled}>
            <Icon name="Plus" size={14} /> Add step
          </button>
        )}
      </div>
    </div>
  );
}
