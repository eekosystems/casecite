/**
 * Pure helpers for the workflow views: what changed between two versions,
 * what changed between two runs, the names documents are shown under, and
 * which validation issues belong to which step.
 */
import type {
  WorkflowDocumentRef,
  WorkflowRunDetail,
  WorkflowStep,
  WorkflowStepIssue,
  WorkflowStepRun,
} from '../api/types';

// ---------------------------------------------------------------- versions

export interface VersionDiff {
  added: string[];
  removed: string[];
  /** Steps present in both whose kind, inputs, condition or params differ, with the keys that changed. */
  changed: Array<{ name: string; keys: string[] }>;
  reordered: boolean;
}

const stable = (value: unknown) => JSON.stringify(value ?? null, Object.keys((value as object) || {}).sort());

function changedKeys(a: WorkflowStep, b: WorkflowStep): string[] {
  const keys: string[] = [];
  if (a.kind !== b.kind) keys.push('kind');
  if (stable(a.inputs) !== stable(b.inputs)) keys.push('inputs');
  if (stable(a.condition || null) !== stable(b.condition || null)) keys.push('condition');
  const params = new Set([...Object.keys(a.params || {}), ...Object.keys(b.params || {})]);
  for (const key of params) {
    if (stable((a.params || {})[key]) !== stable((b.params || {})[key])) keys.push(`params.${key}`);
  }
  return keys;
}

/** The difference from `before` to `after`, by step name (case-insensitive). */
export function diffVersions(before: WorkflowStep[], after: WorkflowStep[]): VersionDiff {
  const key = (s: WorkflowStep) => s.name.trim().toLowerCase();
  const beforeBy = new Map(before.map((s) => [key(s), s]));
  const afterBy = new Map(after.map((s) => [key(s), s]));
  const added = after.filter((s) => !beforeBy.has(key(s))).map((s) => s.name);
  const removed = before.filter((s) => !afterBy.has(key(s))).map((s) => s.name);
  const changed: VersionDiff['changed'] = [];
  for (const s of after) {
    const prev = beforeBy.get(key(s));
    if (!prev) continue;
    const keys = changedKeys(prev, s);
    if (keys.length) changed.push({ name: s.name, keys });
  }
  const shared = after.map(key).filter((k) => beforeBy.has(k));
  const sharedBefore = before.map(key).filter((k) => afterBy.has(k));
  const reordered = shared.some((k, i) => k !== sharedBefore[i]);
  return { added, removed, changed, reordered };
}

/** One line for a version card: "+1 step, −1 step, 2 changed". */
export function describeDiff(diff: VersionDiff): string {
  const parts: string[] = [];
  if (diff.added.length) parts.push(`+${diff.added.length} step${diff.added.length === 1 ? '' : 's'}`);
  if (diff.removed.length) parts.push(`−${diff.removed.length} step${diff.removed.length === 1 ? '' : 's'}`);
  if (diff.changed.length) parts.push(`${diff.changed.length} changed`);
  if (diff.reordered) parts.push('reordered');
  return parts.length ? parts.join(', ') : 'No step changes';
}

// -------------------------------------------------------------------- runs

export interface StepComparison {
  index: number;
  name: string;
  kind: string;
  current: WorkflowStepRun | null;
  previous: WorkflowStepRun | null;
  statusChanged: boolean;
  summaryChanged: boolean;
}

/** Per-step status and summary changes from `previous` to `current`, matched by step name. */
export function compareRuns(current: WorkflowRunDetail, previous: WorkflowRunDetail | null): StepComparison[] {
  const prevBy = new Map((previous?.step_runs || []).map((s) => [s.name.toLowerCase(), s]));
  const rows: StepComparison[] = current.step_runs.map((step) => {
    const prev = prevBy.get(step.name.toLowerCase()) || null;
    return {
      index: step.index,
      name: step.name,
      kind: step.kind,
      current: step,
      previous: prev,
      statusChanged: !!prev && prev.status !== step.status,
      summaryChanged: !!prev && (prev.output?.summary || '') !== (step.output?.summary || ''),
    };
  });
  const seen = new Set(current.step_runs.map((s) => s.name.toLowerCase()));
  for (const prev of previous?.step_runs || []) {
    if (!seen.has(prev.name.toLowerCase())) {
      rows.push({
        index: rows.length,
        name: prev.name,
        kind: prev.kind,
        current: null,
        previous: prev,
        statusChanged: true,
        summaryChanged: false,
      });
    }
  }
  return rows;
}

// --------------------------------------------------------------- documents

/** The name to show for a document id, from whichever list knows it. */
export function documentName(id: string, refs: Array<{ id: string; filename?: string | null }>): string {
  const match = refs.find((r) => r.id === id);
  return (match && match.filename) || id;
}

/** Merge document references from several sources without duplicates. */
export function mergeRefs(...lists: Array<Array<{ id: string; filename?: string | null }> | undefined>): WorkflowDocumentRef[] {
  const out = new Map<string, WorkflowDocumentRef>();
  for (const list of lists) {
    for (const ref of list || []) {
      const existing = out.get(ref.id);
      if (!existing || (!existing.filename && ref.filename)) {
        out.set(ref.id, { id: ref.id, filename: ref.filename ?? null, accessible: true });
      }
    }
  }
  return [...out.values()];
}

// -------------------------------------------------------------- validation

/** The issues that belong to step `index` (`-1` for workflow-level ones). */
export function issuesForStep(issues: WorkflowStepIssue[], index: number): WorkflowStepIssue[] {
  return issues.filter((i) => i.index === index);
}

/** A short label for an issue's field, e.g. "params.fields.0.name" -> "Fields › name". */
export function fieldLabel(field: string): string {
  const parts = field.split('.').filter((p) => p && p !== 'params' && !/^\d+$/.test(p));
  if (parts.length === 0) return 'Step';
  return parts.map((p) => p.replace(/_/g, ' ')).map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(' › ');
}
