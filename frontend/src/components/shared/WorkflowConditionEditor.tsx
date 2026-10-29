import { Icon } from './Icon';
import type { WorkflowCondition, WorkflowConditionOp } from '../../api/types';

interface WorkflowConditionEditorProps {
  condition: WorkflowCondition | null | undefined;
  /** Names of the steps before this one: the only ones a condition may read. */
  earlierSteps: string[];
  disabled?: boolean;
  onChange: (condition: WorkflowCondition | null) => void;
  styles: Record<string, string>;
}

export const CONDITION_OPS: Array<{ op: WorkflowConditionOp; label: string; unary?: boolean; numeric?: boolean }> = [
  { op: 'exists', label: 'has a value', unary: true },
  { op: 'not_exists', label: 'is empty', unary: true },
  { op: 'equals', label: 'equals' },
  { op: 'not_equals', label: 'does not equal' },
  { op: 'contains', label: 'contains' },
  { op: 'gt', label: 'is greater than', numeric: true },
  { op: 'gte', label: 'is at least', numeric: true },
  { op: 'lt', label: 'is less than', numeric: true },
  { op: 'lte', label: 'is at most', numeric: true },
  { op: 'count_gt', label: 'has more items than', numeric: true },
  { op: 'count_eq', label: 'has exactly this many items', numeric: true },
];

/** Common output paths, offered as suggestions; any dotted path is accepted. */
const PATH_HINTS = ['summary', 'data.issues', 'data.analyses.0.issues', 'data.citations', 'data.counts.extracted', 'data.values'];

export function conditionSummary(c: WorkflowCondition): string {
  const op = CONDITION_OPS.find((o) => o.op === c.op);
  const subject = c.path ? `${c.step} › ${c.path}` : c.step;
  return op?.unary ? `${subject} ${op.label}` : `${subject} ${op?.label || c.op} ${c.value ?? ''}`;
}

/** "Only run when …": one rule over an earlier step's output, or nothing. */
export function WorkflowConditionEditor({ condition, earlierSteps, disabled, onChange, styles }: WorkflowConditionEditorProps) {
  if (!condition) {
    return (
      <button
        type="button"
        className={styles.linkBtn}
        onClick={() => onChange({ step: earlierSteps[0] || '', path: 'data.issues', op: 'gt', value: 0 })}
        disabled={disabled || earlierSteps.length === 0}
        title={earlierSteps.length === 0 ? 'A condition reads an earlier step; add one above first' : 'Run this step only when an earlier output meets a rule'}
      >
        <Icon name="GitBranch" size={12} /> Only run when…
      </button>
    );
  }
  const op = CONDITION_OPS.find((o) => o.op === condition.op);
  const set = (patch: Partial<WorkflowCondition>) => onChange({ ...condition, ...patch });
  return (
    <div className={styles.conditionRow} role="group" aria-label="Run condition">
      <Icon name="GitBranch" size={13} className={styles.conditionIcon} />
      <span className={styles.paramLabel}>Only run when</span>
      <select className={styles.select} value={condition.step} onChange={(e) => set({ step: e.target.value })} disabled={disabled} aria-label="Condition step">
        {!earlierSteps.includes(condition.step) && <option value={condition.step}>{condition.step || '—'}</option>}
        {earlierSteps.map((n) => <option key={n} value={n}>{n}</option>)}
      </select>
      <input
        className={`${styles.input} ${styles.conditionPath}`}
        list="workflow-condition-paths"
        placeholder="path, e.g. data.issues"
        value={condition.path}
        onChange={(e) => set({ path: e.target.value })}
        disabled={disabled}
        aria-label="Condition path"
      />
      <datalist id="workflow-condition-paths">
        {PATH_HINTS.map((p) => <option key={p} value={p} />)}
      </datalist>
      <select className={styles.select} value={condition.op} onChange={(e) => set({ op: e.target.value as WorkflowConditionOp, value: CONDITION_OPS.find((o) => o.op === e.target.value)?.unary ? null : condition.value })} disabled={disabled} aria-label="Condition operator">
        {CONDITION_OPS.map((o) => <option key={o.op} value={o.op}>{o.label}</option>)}
      </select>
      {!op?.unary && (
        <input
          className={`${styles.input} ${styles.conditionValue}`}
          type={op?.numeric ? 'number' : 'text'}
          placeholder={op?.numeric ? '0' : 'value'}
          value={condition.value === null || condition.value === undefined ? '' : String(condition.value)}
          onChange={(e) => set({ value: op?.numeric ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value })}
          disabled={disabled}
          aria-label="Condition value"
        />
      )}
      <button type="button" className={styles.iconBtn} onClick={() => onChange(null)} disabled={disabled} title="Remove the condition" aria-label="Remove condition">
        <Icon name="X" size={13} />
      </button>
    </div>
  );
}
