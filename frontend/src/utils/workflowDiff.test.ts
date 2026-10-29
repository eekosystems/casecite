import { describe, it, expect } from 'vitest';
import { compareRuns, describeDiff, diffVersions, documentName, fieldLabel, issuesForStep, mergeRefs } from './workflowDiff';
import type { WorkflowRunDetail, WorkflowStep } from '../api/types';

const step = (name: string, kind: WorkflowStep['kind'], params: Record<string, unknown> = {}, extra: Partial<WorkflowStep> = {}): WorkflowStep => ({
  name, kind, params, inputs: { source: 'run', document_ids: [], step: null }, condition: null, ...extra,
});

describe('diffVersions', () => {
  it('reports added, removed, changed and reordered steps by name', () => {
    const before = [step('extract', 'extract_fields', { fields: [] }), step('review', 'analyze_contract', { posture: 'balanced' }), step('cites', 'citation_check')];
    const after = [step('review', 'analyze_contract', { posture: 'strict' }), step('Extract', 'extract_fields', { fields: [] }), step('memo', 'draft_document', { instructions: 'x' })];
    const diff = diffVersions(before, after);
    expect(diff.added).toEqual(['memo']);
    expect(diff.removed).toEqual(['cites']);
    expect(diff.changed).toEqual([{ name: 'review', keys: ['params.posture'] }]);
    expect(diff.reordered).toBe(true);
    expect(describeDiff(diff)).toBe('+1 step, −1 step, 1 changed, reordered');
  });

  it('counts a condition or input change and says when nothing changed', () => {
    const a = [step('extract', 'extract_fields')];
    const b = [step('extract', 'extract_fields', {}, { condition: { step: 'x', path: '', op: 'exists', value: null } })];
    expect(diffVersions(a, b).changed).toEqual([{ name: 'extract', keys: ['condition'] }]);
    expect(describeDiff(diffVersions(a, a))).toBe('No step changes');
    const c = [step('extract', 'extract_fields', {}, { inputs: { source: 'documents', document_ids: ['d1'], step: null } })];
    expect(diffVersions(a, c).changed[0].keys).toEqual(['inputs']);
  });
});

describe('compareRuns', () => {
  const run = (id: string, statuses: Array<[string, string, string]>): WorkflowRunDetail => ({
    id, workflow_id: 'w', workflow_name: 'W', workflow_version: 1, runner_id: 'me', matter_id: null, status: 'completed',
    steps: statuses.length, completed_steps: 0, documents: 1, job_id: null, error: null, resumed_count: 0,
    awaiting_step: null, approver_ids: [], can_approve: false, created_at: '2026-10-01T00:00:00Z', completed_at: null,
    step_definitions: [], input_document_ids: [], document_refs: [],
    step_runs: statuses.map(([name, status, summary], index) => ({
      index, name, kind: 'analyze_contract', status, output: summary ? { summary, data: {}, document_ids: [], produced: {} } : null,
      input_document_ids: [], produced_ids: {}, error: null, started_at: null, finished_at: null,
    })),
  });

  it('matches steps by name and flags status and summary changes', () => {
    const current = run('b', [['extract', 'completed', '3 docs'], ['review', 'completed', '5 issues']]);
    const previous = run('a', [['extract', 'completed', '3 docs'], ['review', 'failed', ''], ['old', 'completed', 'gone']]);
    const rows = compareRuns(current, previous);
    expect(rows.map((r) => [r.name, r.statusChanged, r.summaryChanged])).toEqual([
      ['extract', false, false],
      ['review', true, true],
      ['old', true, false],
    ]);
    expect(rows[2].current).toBeNull();
    expect(compareRuns(current, null).every((r) => !r.statusChanged)).toBe(true);
  });
});

describe('document and validation helpers', () => {
  it('names documents from whichever list knows them and merges without duplicates', () => {
    const refs = mergeRefs([{ id: 'd1', filename: null }], [{ id: 'd1', filename: 'nda.pdf' }, { id: 'd2', filename: 'msa.pdf' }]);
    expect(refs).toEqual([
      { id: 'd1', filename: 'nda.pdf', accessible: true },
      { id: 'd2', filename: 'msa.pdf', accessible: true },
    ]);
    expect(documentName('d2', refs)).toBe('msa.pdf');
    expect(documentName('d9', refs)).toBe('d9');
  });

  it('groups issues by step and labels fields for people', () => {
    const issues = [
      { index: -1, field: 'steps', message: 'too many' },
      { index: 1, field: 'params.fields.0.name', message: 'empty' },
      { index: 1, field: 'condition.step', message: 'later' },
    ];
    expect(issuesForStep(issues, 1)).toHaveLength(2);
    expect(issuesForStep(issues, -1)[0].message).toBe('too many');
    expect(fieldLabel('params.fields.0.name')).toBe('Fields › Name');
    expect(fieldLabel('params.baseline_document_id')).toBe('Baseline document id');
    expect(fieldLabel('')).toBe('Step');
  });
});
