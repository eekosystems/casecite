import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../api', () => ({
  api: {
    listWorkflows: vi.fn(),
    createWorkflow: vi.fn(),
    getWorkflow: vi.fn(),
    updateWorkflow: vi.fn(),
    deleteWorkflow: vi.fn(),
    listWorkflowTemplates: vi.fn(),
    createWorkflowFromTemplate: vi.fn(),
    runWorkflow: vi.fn(),
    listWorkflowRuns: vi.fn(),
    getWorkflowRun: vi.fn(),
    resumeWorkflowRun: vi.fn(),
    getWorkflowJob: vi.fn(),
    cancelWorkflowJob: vi.fn(),
    getMatterDocuments: vi.fn(),
    getAllDocuments: vi.fn(),
    listMatters: vi.fn().mockResolvedValue({ matters: [] }),
    getMatter: vi.fn(),
    validateWorkflowSteps: vi.fn(),
    listRecentWorkflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
    duplicateWorkflow: vi.fn(),
    archiveWorkflow: vi.fn(),
    unarchiveWorkflow: vi.fn(),
    listWorkflowVersions: vi.fn(),
    getWorkflowVersion: vi.fn(),
    restoreWorkflowVersion: vi.fn(),
    approveWorkflowRun: vi.fn(),
    rejectWorkflowRun: vi.fn(),
    retryWorkflowStep: vi.fn(),
    skipWorkflowStep: vi.fn(),
    exportDraft: vi.fn(),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

const navigate = vi.fn();
vi.mock('../utils/router', () => ({ appNavigate: (to: string) => navigate(to) }));

import { useWorkflowsStore, newStep, describeRun, stepKindInfo, cleanSteps, filterWorkflows } from './workflowsStore';
import { useMattersStore } from './mattersStore';
import { useUIStore } from './uiStore';
import { api } from '../api';
import type { WorkflowStep, WorkflowSummary } from '../api/types';

const steps: WorkflowStep[] = [
  { name: 'extract', kind: 'extract_fields', params: { fields: [{ key: 'term', name: 'Term', description: '', type: 'duration' }] }, inputs: { source: 'run', document_ids: [], step: null } },
  { name: 'review', kind: 'analyze_contract', params: { contract_type: 'auto', posture: 'balanced' }, inputs: { source: 'run', document_ids: [], step: null } },
];
const summary: WorkflowSummary = {
  id: 'w1', name: 'Intake', description: null, matter_id: null, owner_id: 'me', role: 'owner', template_key: null,
  version: 1, archived_at: null,
  step_count: 2, step_kinds: ['extract_fields', 'analyze_contract'], run_count: 0, last_run_at: null, last_run_status: null,
  created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
};
const detail = { ...summary, steps, documents: [] };
const runSummary = {
  id: 'r1', workflow_id: 'w1', workflow_name: 'Intake', workflow_version: 1, runner_id: 'me', matter_id: null,
  status: 'failed', steps: 2, completed_steps: 1,
  documents: 1, job_id: 'j1', error: "Step 2/2 'review' failed: boom", resumed_count: 0,
  awaiting_step: null, approver_ids: [], can_approve: false,
  created_at: '2026-10-02T00:00:00Z', completed_at: '2026-10-02T00:01:00Z',
};
const runDetail = {
  ...runSummary,
  step_definitions: steps,
  input_document_ids: ['d1'],
  document_refs: [{ id: 'd1', filename: 'nda.pdf', accessible: true }],
  step_runs: [
    { index: 0, name: 'extract', kind: 'extract_fields', status: 'completed', output: { summary: 'ok', data: {}, document_ids: ['d1'], produced: {} }, input_document_ids: ['d1'], produced_ids: {}, error: null, started_at: null, finished_at: null },
    { index: 1, name: 'review', kind: 'analyze_contract', status: 'failed', output: null, input_document_ids: [], produced_ids: {}, error: 'boom', started_at: null, finished_at: null },
  ],
};

/** Settle pending promises and zero-delay timers under the fake clock. */
const flush = () => vi.advanceTimersByTimeAsync(0);

describe('workflowsStore', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useWorkflowsStore.setState(useWorkflowsStore.getInitialState(), true);
    useMattersStore.setState(useMattersStore.getInitialState(), true);
    useUIStore.setState(useUIStore.getInitialState(), true);
    vi.clearAllMocks();
    (api.listWorkflows as any).mockResolvedValue({ workflows: [summary] });
    (api.listWorkflowTemplates as any).mockResolvedValue({ templates: [{ key: 'nda_intake', name: 'NDA intake', description: 'd', steps }] });
    (api.getWorkflow as any).mockResolvedValue(detail);
    (api.listWorkflowRuns as any).mockResolvedValue({ runs: [runSummary] });
    (api.getWorkflowRun as any).mockResolvedValue(runDetail);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  // ==================== helpers ====================

  it('newStep picks a unique name and the kind defaults', () => {
    const first = newStep('extract_fields', []);
    expect(first.name).toBe('extract');
    expect(first.inputs).toEqual({ source: 'run', document_ids: [], step: null });
    expect(newStep('extract_fields', [first]).name).toBe('extract2');
    expect(newStep('deep_research', []).params).toEqual({ question: '' });
  });

  it('describeRun and stepKindInfo summarise for the UI', () => {
    expect(describeRun(null)).toBe('Workflow finished.');
    expect(describeRun({ run_id: 'r', status: 'completed', steps: 2, completed_steps: 2, documents: 3, produced: { review_tables: 1, analyses: 0 } }))
      .toBe('Workflow finished: 2 steps, produced 1 review tables.');
    expect(stepKindInfo('citation_check').title).toBe('Check citations');
    expect(stepKindInfo('mystery').title).toBe('mystery');
  });

  // ==================== list / create ====================

  it('init loads the workflows and templates once', async () => {
    useWorkflowsStore.getState().init();
    useWorkflowsStore.getState().init();
    await flush();
    expect(api.listWorkflows).toHaveBeenCalledTimes(1);
    expect(api.listWorkflowTemplates).toHaveBeenCalledTimes(1);
    expect(useWorkflowsStore.getState().workflows).toEqual([summary]);
    expect(useWorkflowsStore.getState().templates).toHaveLength(1);
  });

  it('createWorkflow sends the active matter and cleaned steps, then prepends the summary', async () => {
    useMattersStore.setState({ activeMatterId: 'm1' });
    (api.createWorkflow as any).mockResolvedValue(detail);
    const created = await useWorkflowsStore.getState().createWorkflow({
      name: '  Intake ',
      description: '',
      steps: [
        { name: ' extract ', kind: 'extract_fields', params: { fields: steps[0].params.fields, table_name: '  ' }, inputs: { source: 'documents', document_ids: ['d1'], step: 'x' } },
        { name: 'memo', kind: 'draft_document', params: { instructions: ' Write it ', include_outputs_from: ['extract'] }, inputs: { source: 'step', document_ids: ['zzz'], step: 'extract' } },
      ],
    });
    expect(created).toEqual(detail);
    expect(api.createWorkflow).toHaveBeenCalledWith({
      name: 'Intake',
      description: null,
      matter_id: 'm1',
      steps: [
        { name: 'extract', kind: 'extract_fields', params: { fields: steps[0].params.fields }, inputs: { source: 'documents', document_ids: ['d1'], step: null }, condition: null },
        { name: 'memo', kind: 'draft_document', params: { instructions: 'Write it', include_outputs_from: ['extract'] }, inputs: { source: 'step', document_ids: [], step: 'extract' }, condition: null },
      ],
    });
    expect(useWorkflowsStore.getState().workflows[0].id).toBe('w1');
  });

  it('createWorkflow refuses an empty name or step list without calling the API', async () => {
    expect(await useWorkflowsStore.getState().createWorkflow({ name: '', description: '', steps })).toBeNull();
    expect(await useWorkflowsStore.getState().createWorkflow({ name: 'x', description: '', steps: [] })).toBeNull();
    expect(api.createWorkflow).not.toHaveBeenCalled();
    expect(useUIStore.getState().toasts.length).toBe(2);
  });

  it('createFromTemplate passes the active matter and returns the new workflow', async () => {
    useMattersStore.setState({ activeMatterId: 'm1' });
    (api.createWorkflowFromTemplate as any).mockResolvedValue({ ...detail, template_key: 'nda_intake' });
    const created = await useWorkflowsStore.getState().createFromTemplate('nda_intake');
    expect(api.createWorkflowFromTemplate).toHaveBeenCalledWith('nda_intake', 'm1');
    expect(created?.template_key).toBe('nda_intake');
    expect(useWorkflowsStore.getState().workflows[0].id).toBe('w1');
  });

  it('openWorkflow navigates to the workflow route', () => {
    useWorkflowsStore.getState().openWorkflow('w 1');
    expect(navigate).toHaveBeenCalledWith('/workflows/w%201');
  });

  // ==================== one workflow ====================

  it('loadWorkflow loads the workflow, its runs and the newest run detail', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    const st = useWorkflowsStore.getState();
    expect(st.workflow).toEqual(detail);
    expect(st.runs).toEqual([runSummary]);
    expect(st.selectedRunId).toBe('r1');
    expect(st.selectedRun?.step_runs[1].status).toBe('failed');
    expect(st.editorOpen).toBe(false);
  });

  it('loadWorkflow records an error for an unreadable workflow', async () => {
    (api.getWorkflow as any).mockRejectedValue(new Error('Workflow not found'));
    await useWorkflowsStore.getState().loadWorkflow('nope');
    expect(useWorkflowsStore.getState().workflowError).toBe('Workflow not found');
    expect(useWorkflowsStore.getState().workflow).toBeNull();
  });

  it('saveSteps PATCHes the cleaned steps and replaces the workflow', async () => {
    useWorkflowsStore.setState({ workflows: [summary] });
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    (api.updateWorkflow as any).mockResolvedValue({ ...detail, step_count: 1, steps: [steps[1]] });
    const ok = await useWorkflowsStore.getState().saveSteps([{ ...steps[1], name: ' review ' }]);
    expect(ok).toBe(true);
    expect(api.updateWorkflow).toHaveBeenCalledWith('w1', { steps: [{ ...steps[1], name: 'review', condition: null }], change_note: null });
    expect(useWorkflowsStore.getState().workflow?.step_count).toBe(1);
    expect(useWorkflowsStore.getState().workflows[0].step_count).toBe(1);
  });

  it('saveSteps refuses an empty list or a nameless step without calling the API', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    expect(await useWorkflowsStore.getState().saveSteps([])).toBe(false);
    expect(await useWorkflowsStore.getState().saveSteps([{ ...steps[0], name: '  ' }])).toBe(false);
    expect(api.updateWorkflow).not.toHaveBeenCalled();
  });

  it('deleteWorkflow asks for confirmation, then deletes and navigates back', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    (api.deleteWorkflow as any).mockResolvedValue({ status: 'deleted', id: 'w1' });
    useWorkflowsStore.getState().deleteWorkflow();
    const confirm = useUIStore.getState().confirmModal;
    expect(confirm.open).toBe(true);
    expect(api.deleteWorkflow).not.toHaveBeenCalled();
    await useUIStore.getState().handleConfirm();
    await flush();
    expect(api.deleteWorkflow).toHaveBeenCalledWith('w1');
    expect(useWorkflowsStore.getState().workflow).toBeNull();
    expect(useWorkflowsStore.getState().workflows).toEqual([]);
    expect(navigate).toHaveBeenCalledWith('/workflows');
  });

  // ==================== runs ====================

  it('run starts the job, polls it, and refreshes the runs on completion', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    (api.runWorkflow as any).mockResolvedValue({ run_id: 'r2', job_id: 'j2', poll_url: '/jobs/j2', steps: 2, documents: 1 });
    (api.getWorkflowJob as any)
      .mockResolvedValueOnce({ job_id: 'j2', status: 'processing', progress: { message: 'Step 1/2 (extract): starting', fraction: 0 } })
      .mockResolvedValueOnce({ job_id: 'j2', status: 'completed', result: { run_id: 'r2', status: 'completed', steps: 2, completed_steps: 2, documents: 1, produced: { analyses: 1 } } });

    const runPromise = useWorkflowsStore.getState().run(['d1']);
    await flush();
    await runPromise;
    expect(api.runWorkflow).toHaveBeenCalledWith('w1', ['d1']);
    expect(useWorkflowsStore.getState().running).toBe(true);
    expect(useWorkflowsStore.getState().selectedRunId).toBe('r2');

    await flush(); // first poll: processing
    expect(useWorkflowsStore.getState().progress?.message).toBe('Step 1/2 (extract): starting');
    await vi.advanceTimersByTimeAsync(2000); // second poll: completed
    await flush();
    expect(useWorkflowsStore.getState().running).toBe(false);
    expect(useUIStore.getState().toasts.some((t) => t.message.includes('produced 1 analyses'))).toBe(true);
    expect(api.listWorkflowRuns).toHaveBeenCalledTimes(3); // load, start, completion
  });

  it('run refuses to start without documents for a private workflow', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await useWorkflowsStore.getState().run([]);
    expect(api.runWorkflow).not.toHaveBeenCalled();
    expect(useUIStore.getState().toasts[0].message).toBe('Pick the documents to run the workflow on.');
  });

  it('run records a failed job and keeps whatever finished', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    (api.runWorkflow as any).mockResolvedValue({ run_id: 'r2', job_id: 'j2', poll_url: '/jobs/j2', steps: 2, documents: 1 });
    (api.getWorkflowJob as any).mockResolvedValue({ job_id: 'j2', status: 'failed', error: "Step 'review' failed: boom" });
    await useWorkflowsStore.getState().run(['d1']);
    await flush();
    const st = useWorkflowsStore.getState();
    expect(st.running).toBe(false);
    expect(st.runError).toBe("Step 'review' failed: boom");
  });

  it('resume re-submits the run and cancel deletes the job', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    (api.resumeWorkflowRun as any).mockResolvedValue({ run_id: 'r1', job_id: 'j3', poll_url: '/jobs/j3', steps: 2, documents: 1 });
    (api.getWorkflowJob as any).mockResolvedValue({ job_id: 'j3', status: 'processing', progress: null });
    (api.getWorkflowRun as any).mockResolvedValue({ ...runDetail, status: 'running', job_id: 'j3' });
    await useWorkflowsStore.getState().resume('r1');
    await flush();
    expect(api.resumeWorkflowRun).toHaveBeenCalledWith('w1', 'r1');
    expect(useWorkflowsStore.getState().running).toBe(true);

    (api.cancelWorkflowJob as any).mockResolvedValue({ status: 'cancelled', job_id: 'j3' });
    await useWorkflowsStore.getState().cancel();
    expect(api.cancelWorkflowJob).toHaveBeenCalledWith('j3');
  });

  // ==================== picker ====================

  it('openPicker lists the active matter documents, else everything readable', async () => {
    (api.getAllDocuments as any).mockResolvedValue([{ id: 'd1', filename: 'a.pdf', status: 'indexed' }]);
    await useWorkflowsStore.getState().openPicker();
    expect(api.getAllDocuments).toHaveBeenCalled();
    expect(useWorkflowsStore.getState().pickerDocs).toHaveLength(1);

    useMattersStore.setState({ activeMatterId: 'm1' });
    (api.getMatterDocuments as any).mockResolvedValue({ documents: [{ id: 'd2', filename: 'b.pdf', status: 'indexed' }] });
    await useWorkflowsStore.getState().openPicker();
    expect(api.getMatterDocuments).toHaveBeenCalledWith('m1');
    expect(useWorkflowsStore.getState().pickerDocs[0].id).toBe('d2');
    useWorkflowsStore.getState().closePicker();
    expect(useWorkflowsStore.getState().pickerOpen).toBe(false);
  });
  // ==================== validation, conditions, versions, controls ====================

  it('cleanSteps keeps a condition only when it names a step and normalises it', () => {
    const withCondition = { ...steps[1], condition: { step: 'extract', path: 'data.issues', op: 'gt' as const, value: 2 } };
    const blank = { ...steps[1], condition: { step: '', path: '', op: 'exists' as const, value: null } };
    expect(cleanSteps([withCondition])[0].condition).toEqual({ step: 'extract', path: 'data.issues', op: 'gt', value: 2 });
    expect(cleanSteps([blank])[0].condition).toBeNull();
    expect(cleanSteps([{ ...steps[1], condition: undefined }])[0].condition).toBeNull();
  });

  it('validateDraft debounces and keeps only the newest answer', async () => {
    (api.validateWorkflowSteps as any)
      .mockResolvedValueOnce({ valid: false, issues: [{ index: 0, field: 'name', message: 'old' }] })
      .mockResolvedValueOnce({ valid: false, issues: [{ index: 1, field: 'params.instructions', message: 'new' }] });
    useWorkflowsStore.getState().validateDraft(steps);
    useWorkflowsStore.getState().validateDraft([steps[0]]);
    expect(useWorkflowsStore.getState().validating).toBe(true);
    await vi.advanceTimersByTimeAsync(400);
    await flush();
    expect(api.validateWorkflowSteps).toHaveBeenCalledTimes(1);
    expect(useWorkflowsStore.getState().validationIssues).toEqual([{ index: 0, field: 'name', message: 'old' }]);
    expect(useWorkflowsStore.getState().validated).toBe(true);
    useWorkflowsStore.getState().clearValidation();
    expect(useWorkflowsStore.getState().validationIssues).toEqual([]);
  });

  it('saveSteps refuses while the draft has validation problems', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    useWorkflowsStore.setState({ validationIssues: [{ index: 0, field: 'name', message: 'bad' }] });
    expect(await useWorkflowsStore.getState().saveSteps(steps)).toBe(false);
    expect(api.updateWorkflow).not.toHaveBeenCalled();
    expect(useUIStore.getState().toasts[0].message).toBe('Fix the problems shown under the steps first.');
  });

  it('filterWorkflows applies search and each filter', () => {
    const mine = summary;
    const shared = { ...summary, id: 'w2', name: 'Lease review', role: 'member', last_run_status: 'failed' };
    const archived = { ...summary, id: 'w3', name: 'Old', archived_at: '2026-10-01T00:00:00Z' };
    const all = [mine, shared, archived];
    const waiting = [{ ...runSummary, workflow_id: 'w2', status: 'awaiting_approval' }];
    expect(filterWorkflows(all, '', 'all', []).map((w) => w.id)).toEqual(['w1', 'w2']);
    expect(filterWorkflows(all, 'lease', 'all', []).map((w) => w.id)).toEqual(['w2']);
    expect(filterWorkflows(all, '', 'mine', []).map((w) => w.id)).toEqual(['w1']);
    expect(filterWorkflows(all, '', 'shared', []).map((w) => w.id)).toEqual(['w2']);
    expect(filterWorkflows(all, '', 'failed', []).map((w) => w.id)).toEqual(['w2']);
    expect(filterWorkflows(all, '', 'archived', []).map((w) => w.id)).toEqual(['w3']);
    expect(filterWorkflows(all, '', 'needs_approval', waiting).map((w) => w.id)).toEqual(['w2']);
  });

  it('describeRun says when a run paused for approval', () => {
    expect(describeRun({ run_id: 'r', status: 'awaiting_approval', steps: 3, completed_steps: 1, documents: 1, produced: {}, awaiting_step: 'gate' }))
      .toBe('Workflow paused: step "gate" is waiting for approval.');
  });

  it('toggleVersions loads the history and restoreVersion asks first', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    (api.listWorkflowVersions as any).mockResolvedValue({ current: 1, versions: [{ version: 1, name: 'Intake', description: null, step_count: 2, step_names: ['extract', 'review'], created_by: 'me', created_at: '2026-10-01T00:00:00Z', change_note: 'Created' }] });
    useWorkflowsStore.getState().toggleVersions();
    await flush();
    expect(useWorkflowsStore.getState().versionsOpen).toBe(true);
    expect(useWorkflowsStore.getState().versions).toHaveLength(1);
    (api.getWorkflowVersion as any).mockResolvedValue({ version: 1, steps });
    expect(await useWorkflowsStore.getState().loadVersionSteps(1)).toEqual(steps);
    expect(await useWorkflowsStore.getState().loadVersionSteps(1)).toEqual(steps);
    expect(api.getWorkflowVersion).toHaveBeenCalledTimes(1);

    (api.restoreWorkflowVersion as any).mockResolvedValue({ ...detail, version: 2 });
    useWorkflowsStore.getState().restoreVersion(1);
    expect(api.restoreWorkflowVersion).not.toHaveBeenCalled();
    await useUIStore.getState().handleConfirm();
    await flush();
    expect(api.restoreWorkflowVersion).toHaveBeenCalledWith('w1', 1);
    expect(useWorkflowsStore.getState().workflow?.version).toBe(2);
  });

  it('duplicate navigates to the copy; archive asks, then unarchive restores', async () => {
    useWorkflowsStore.setState({ workflows: [summary] });
    await useWorkflowsStore.getState().loadWorkflow('w1');
    (api.duplicateWorkflow as any).mockResolvedValue({ ...detail, id: 'w9', name: 'Intake (copy)' });
    await useWorkflowsStore.getState().duplicateWorkflow();
    expect(api.duplicateWorkflow).toHaveBeenCalledWith('w1', { matter_id: null });
    expect(navigate).toHaveBeenCalledWith('/workflows/w9');
    expect(useWorkflowsStore.getState().workflows[0].id).toBe('w9');

    (api.archiveWorkflow as any).mockResolvedValue({ ...detail, archived_at: '2026-10-02T00:00:00Z' });
    useWorkflowsStore.getState().archiveWorkflow();
    await useUIStore.getState().handleConfirm();
    await flush();
    expect(useWorkflowsStore.getState().workflow?.archived_at).toBe('2026-10-02T00:00:00Z');
    (api.unarchiveWorkflow as any).mockResolvedValue(detail);
    await useWorkflowsStore.getState().unarchiveWorkflow();
    expect(useWorkflowsStore.getState().workflow?.archived_at).toBeNull();
  });

  it('approve, reject, retry and skip act on the selected run', async () => {
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    const started = { run_id: 'r1', job_id: 'j5', poll_url: '/jobs/j5', steps: 2, documents: 1 };
    (api.getWorkflowJob as any).mockResolvedValue({ job_id: 'j5', status: 'processing', progress: null });
    (api.retryWorkflowStep as any).mockResolvedValue(started);
    await useWorkflowsStore.getState().retryStep(1);
    expect(api.retryWorkflowStep).toHaveBeenCalledWith('w1', 'r1', 1);
    expect(useWorkflowsStore.getState().running).toBe(true);
    // Nothing else starts while a run is in progress.
    await useWorkflowsStore.getState().skipStep(1);
    expect(api.skipWorkflowStep).not.toHaveBeenCalled();
    useWorkflowsStore.setState({ running: false });
    (api.skipWorkflowStep as any).mockResolvedValue(started);
    await useWorkflowsStore.getState().skipStep(1);
    expect(api.skipWorkflowStep).toHaveBeenCalledWith('w1', 'r1', 1);
    useWorkflowsStore.setState({ running: false });
    (api.approveWorkflowRun as any).mockResolvedValue(started);
    await useWorkflowsStore.getState().approve('  fine ');
    expect(api.approveWorkflowRun).toHaveBeenCalledWith('w1', 'r1', 'fine');
    useWorkflowsStore.setState({ running: false });
    (api.rejectWorkflowRun as any).mockResolvedValue({ ...runDetail, status: 'rejected' });
    (api.getWorkflowRun as any).mockResolvedValue({ ...runDetail, status: 'rejected' });
    await useWorkflowsStore.getState().reject('');
    expect(api.rejectWorkflowRun).toHaveBeenCalledWith('w1', 'r1', null);
    expect(useWorkflowsStore.getState().selectedRun?.status).toBe('rejected');
  });

  it('toggleCompare loads the run before the selected one', async () => {
    (api.listWorkflowRuns as any).mockResolvedValue({ runs: [runSummary, { ...runSummary, id: 'r0', created_at: '2026-10-01T00:00:00Z' }] });
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    (api.getWorkflowRun as any).mockResolvedValue({ ...runDetail, id: 'r0' });
    await useWorkflowsStore.getState().toggleCompare();
    expect(useWorkflowsStore.getState().compareOpen).toBe(true);
    expect(api.getWorkflowRun).toHaveBeenLastCalledWith('w1', 'r0');
    expect(useWorkflowsStore.getState().previousRun?.id).toBe('r0');
  });

  it('documentRefs merges names from the workflow, the run and the picker', async () => {
    (api.getWorkflow as any).mockResolvedValue({ ...detail, documents: [{ id: 'd1', filename: null, accessible: false }] });
    await useWorkflowsStore.getState().loadWorkflow('w1');
    await flush();
    useWorkflowsStore.setState({ pickerDocs: [{ id: 'd3', filename: 'c.pdf' } as any] });
    expect(useWorkflowsStore.getState().documentRefs().map((r) => [r.id, r.filename])).toEqual([['d1', 'nda.pdf'], ['d3', 'c.pdf']]);
  });

  it('loadApproverCandidates fetches a matter once', async () => {
    (api.getMatter as any).mockResolvedValue({ id: 'm1', members: [{ user_id: 'u2', email: 'a@b.c', name: 'A', role: 'member' }] });
    await useWorkflowsStore.getState().loadApproverCandidates('m1');
    await useWorkflowsStore.getState().loadApproverCandidates('m1');
    expect(api.getMatter).toHaveBeenCalledTimes(1);
    expect(useWorkflowsStore.getState().approverCandidates).toHaveLength(1);
    await useWorkflowsStore.getState().loadApproverCandidates(null);
    expect(useWorkflowsStore.getState().approverCandidates).toEqual([]);
  });
});
