import { create } from 'zustand';
import { api } from '../api';
import { useUIStore } from './uiStore';
import { useMattersStore } from './mattersStore';
import { registerReset } from './resetRegistry';
import { createJobPoller, toast } from './storeUtils';
import { appNavigate } from '../utils/router';
import logger from '../utils/logger';
import type { RagDocument } from '../types';
import type {
  MatterMember,
  WorkflowDetail,
  WorkflowDocumentRef,
  WorkflowRunDetail,
  WorkflowRunResult,
  WorkflowRunSummary,
  WorkflowStep,
  WorkflowStepIssue,
  WorkflowStepKind,
  WorkflowSummary,
  WorkflowTemplate,
  WorkflowVersionSummary,
} from '../api/types';
import { mergeRefs } from '../utils/workflowDiff';

/** What each step kind does, for the editor and the run view. */
export const STEP_KINDS: Array<{ kind: WorkflowStepKind; title: string; desc: string; icon: string }> = [
  { kind: 'extract_fields', title: 'Extract fields', desc: 'A tabular review of the input documents against a field schema', icon: 'Table2' },
  { kind: 'analyze_contract', title: 'Analyse contracts', desc: 'The full contract review on each input document', icon: 'FileSearch' },
  { kind: 'compare_contracts', title: 'Compare contracts', desc: 'Clause by clause against a baseline (the first input by default)', icon: 'GitCompare' },
  { kind: 'draft_document', title: 'Draft a document', desc: 'From instructions, with earlier outputs attached as references', icon: 'PenLine' },
  { kind: 'deep_research', title: 'Deep research', desc: 'A planned, source-verified research question over the inputs', icon: 'ScanSearch' },
  { kind: 'citation_check', title: 'Check citations', desc: 'Treatment scan of citations found in earlier outputs', icon: 'ShieldCheck' },
  { kind: 'approval', title: 'Approval', desc: 'Pause the run until a named approver signs off', icon: 'UserCheck' },
];

export const stepKindInfo = (kind: string) =>
  STEP_KINDS.find((k) => k.kind === kind) || { kind: kind as WorkflowStepKind, title: kind, desc: '', icon: 'Zap' };

const DEFAULT_PARAMS: Record<WorkflowStepKind, Record<string, unknown>> = {
  extract_fields: { fields: [{ name: 'Counterparty', description: '', type: 'text' }] },
  analyze_contract: { contract_type: 'auto', posture: 'balanced' },
  compare_contracts: { baseline: 'first_input', focus: '' },
  draft_document: { instructions: '', include_outputs_from: [] },
  deep_research: { question: '' },
  citation_check: { citations: [], from_steps: null },
  approval: { title: 'Approval', instructions: '', approvers: 'owner' },
};

/** A fresh step of `kind` with a name no other step uses. */
export function newStep(kind: WorkflowStepKind, existing: WorkflowStep[]): WorkflowStep {
  const base = kind.split('_')[0];
  const taken = new Set(existing.map((s) => s.name.toLowerCase()));
  let name = base;
  for (let i = 2; taken.has(name); i += 1) name = `${base}${i}`;
  return { name, kind, params: { ...DEFAULT_PARAMS[kind] }, inputs: { source: 'run', document_ids: [], step: null }, condition: null };
}

export interface RunProgress {
  message: string;
  fraction: number | null;
}

export type WorkflowListFilter = 'all' | 'mine' | 'shared' | 'needs_approval' | 'failed' | 'archived';

export const LIST_FILTERS: Array<{ key: WorkflowListFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'mine', label: 'Mine' },
  { key: 'shared', label: 'Shared with me' },
  { key: 'needs_approval', label: 'Needs approval' },
  { key: 'failed', label: 'Last run failed' },
  { key: 'archived', label: 'Archived' },
];

const errorMessage = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** One line summarising a finished run for the toast. */
export function describeRun(result: WorkflowRunResult | null | undefined): string {
  if (!result) return 'Workflow finished.';
  if (result.status === 'awaiting_approval') {
    return `Workflow paused: step "${(result as { awaiting_step?: string }).awaiting_step || 'approval'}" is waiting for approval.`;
  }
  const produced = Object.entries(result.produced || {})
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${k.replace(/_/g, ' ')}`);
  const steps = `${result.completed_steps} step${result.completed_steps === 1 ? '' : 's'}`;
  return produced.length ? `Workflow finished: ${steps}, produced ${produced.join(', ')}.` : `Workflow finished: ${steps}.`;
}

/** Apply the list's search and filter to the workflows the server returned. */
export function filterWorkflows(
  workflows: WorkflowSummary[],
  search: string,
  filter: WorkflowListFilter,
  runs: WorkflowRunSummary[],
): WorkflowSummary[] {
  const q = search.trim().toLowerCase();
  const awaiting = new Set(runs.filter((r) => r.status === 'awaiting_approval').map((r) => r.workflow_id));
  return workflows.filter((w) => {
    if (q && !`${w.name} ${w.description || ''}`.toLowerCase().includes(q)) return false;
    switch (filter) {
      case 'mine': return w.role === 'owner' && !w.archived_at;
      case 'shared': return w.role !== 'owner' && !w.archived_at;
      case 'needs_approval': return awaiting.has(w.id);
      case 'failed': return w.last_run_status === 'failed';
      case 'archived': return !!w.archived_at;
      default: return !w.archived_at;
    }
  });
}

export interface WorkflowsState {
  _initialized: boolean;
  workflows: WorkflowSummary[];
  workflowsLoading: boolean;
  templates: WorkflowTemplate[];
  search: string;
  listFilter: WorkflowListFilter;
  recentRuns: WorkflowRunSummary[];
  init: () => void;
  loadWorkflows: () => Promise<void>;
  loadTemplates: () => Promise<void>;
  loadRecentRuns: () => Promise<void>;
  setSearch: (search: string) => void;
  setListFilter: (filter: WorkflowListFilter) => void;
  createWorkflow: (input: { name: string; description: string; steps: WorkflowStep[] }) => Promise<WorkflowDetail | null>;
  createFromTemplate: (key: string) => Promise<WorkflowDetail | null>;
  openWorkflow: (workflowId: string) => void;
  openRun: (workflowId: string, runId: string) => void;

  // ── One workflow ──
  workflowId: string | null;
  workflow: WorkflowDetail | null;
  workflowLoading: boolean;
  workflowError: string | null;
  loadWorkflow: (workflowId: string, runId?: string | null) => Promise<void>;
  saveDetails: (name: string, description: string) => Promise<boolean>;
  saveSteps: (steps: WorkflowStep[], changeNote?: string) => Promise<boolean>;
  deleteWorkflow: () => void;
  duplicateWorkflow: () => Promise<void>;
  archiveWorkflow: () => void;
  unarchiveWorkflow: () => Promise<void>;

  // ── Validation while editing ──
  validationIssues: WorkflowStepIssue[];
  validating: boolean;
  validated: boolean;
  validateDraft: (steps: WorkflowStep[]) => void;
  clearValidation: () => void;

  // ── Versions ──
  versions: WorkflowVersionSummary[];
  versionsOpen: boolean;
  versionSteps: Record<number, WorkflowStep[]>;
  toggleVersions: () => void;
  loadVersions: () => Promise<void>;
  loadVersionSteps: (version: number) => Promise<WorkflowStep[] | null>;
  restoreVersion: (version: number) => void;

  // ── Runs ──
  runs: WorkflowRunSummary[];
  runsLoading: boolean;
  loadRuns: () => Promise<void>;
  selectedRunId: string | null;
  selectedRun: WorkflowRunDetail | null;
  selectRun: (runId: string | null) => Promise<void>;
  previousRun: WorkflowRunDetail | null;
  compareOpen: boolean;
  toggleCompare: () => Promise<void>;
  running: boolean;
  progress: RunProgress | null;
  runError: string | null;
  run: (documentIds: string[]) => Promise<void>;
  resume: (runId: string) => Promise<void>;
  cancel: () => Promise<void>;
  retryStep: (index: number) => Promise<void>;
  skipStep: (index: number) => Promise<void>;
  approve: (note: string) => Promise<void>;
  reject: (note: string) => Promise<void>;
  exportingDraft: boolean;
  downloadDraft: (title: string, text: string) => Promise<void>;

  // ── Shared UI state ──
  editorOpen: boolean;
  setEditorOpen: (open: boolean) => void;
  /** Members of the matter an approval step may name, keyed by the matter loaded. */
  approverCandidates: MatterMember[];
  approverMatterId: string | null;
  loadApproverCandidates: (matterId: string | null) => Promise<void>;
  pickerOpen: boolean;
  pickerDocs: RagDocument[];
  pickerLoading: boolean;
  pickerLoaded: boolean;
  pickerMatterId: string | null;
  ensurePickerDocs: () => Promise<void>;
  openPicker: () => Promise<void>;
  closePicker: () => void;
  /** Names for every document id the open workflow or run mentions. */
  documentRefs: () => WorkflowDocumentRef[];
}

const jobPoller = createJobPoller();
let validateTimer: ReturnType<typeof setTimeout> | null = null;
let validateSeq = 0;

export const useWorkflowsStore = create<WorkflowsState>((set, get) => ({
  _initialized: false,
  workflows: [],
  workflowsLoading: false,
  templates: [],
  search: '',
  listFilter: 'all',
  recentRuns: [],

  init: () => {
    if (get()._initialized) return;
    set({ _initialized: true });
    void get().loadWorkflows();
    void get().loadTemplates();
    void get().loadRecentRuns();
  },

  loadWorkflows: async () => {
    set({ workflowsLoading: true });
    try {
      const { workflows } = await api.listWorkflows(true);
      set({ workflows });
    } catch (e) {
      logger.error('loadWorkflows failed', e);
      toast(errorMessage(e, 'Could not load workflows.'), 'error');
    } finally {
      set({ workflowsLoading: false });
    }
  },

  loadTemplates: async () => {
    try {
      const { templates } = await api.listWorkflowTemplates();
      set({ templates });
    } catch (e) {
      logger.debug('loadWorkflowTemplates failed', e);
    }
  },

  loadRecentRuns: async () => {
    try {
      const matterId = useMattersStore.getState().activeMatterId;
      const { runs } = await api.listRecentWorkflowRuns(matterId, 12);
      set({ recentRuns: runs });
    } catch (e) {
      logger.debug('loadRecentWorkflowRuns failed', e);
    }
  },

  setSearch: (search) => set({ search }),
  setListFilter: (listFilter) => set({ listFilter }),

  createWorkflow: async ({ name, description, steps }) => {
    const trimmed = name.trim();
    if (!trimmed) {
      toast('Give the workflow a name.', 'error');
      return null;
    }
    if (steps.length === 0) {
      toast('Add at least one step.', 'error');
      return null;
    }
    try {
      const matterId = useMattersStore.getState().activeMatterId;
      const workflow = await api.createWorkflow({
        name: trimmed,
        description: description.trim() || null,
        matter_id: matterId,
        steps: cleanSteps(steps),
      });
      set((st) => ({ workflows: [workflowSummary(workflow), ...st.workflows] }));
      toast(`Created "${workflow.name}".`, 'success');
      return workflow;
    } catch (e) {
      logger.error('createWorkflow failed', e);
      toast(errorMessage(e, 'Could not create the workflow.'), 'error');
      return null;
    }
  },

  createFromTemplate: async (key) => {
    try {
      const matterId = useMattersStore.getState().activeMatterId;
      const workflow = await api.createWorkflowFromTemplate(key, matterId);
      set((st) => ({ workflows: [workflowSummary(workflow), ...st.workflows] }));
      toast(`Created "${workflow.name}" from the template. Edit its steps freely.`, 'success');
      return workflow;
    } catch (e) {
      logger.error('createWorkflowFromTemplate failed', e);
      toast(errorMessage(e, 'Could not create the workflow.'), 'error');
      return null;
    }
  },

  openWorkflow: (workflowId) => {
    appNavigate(`/workflows/${encodeURIComponent(workflowId)}`);
  },

  openRun: (workflowId, runId) => {
    appNavigate(`/workflows/${encodeURIComponent(workflowId)}?run=${encodeURIComponent(runId)}`);
  },

  // ── One workflow ──
  workflowId: null,
  workflow: null,
  workflowLoading: false,
  workflowError: null,

  loadWorkflow: async (workflowId, runId = null) => {
    if (get().workflowId !== workflowId) {
      jobPoller.stop();
      set({
        workflowId, workflow: null, workflowError: null, runs: [], selectedRunId: runId || null, selectedRun: null,
        previousRun: null, compareOpen: false, running: false, progress: null, runError: null, editorOpen: false,
        pickerOpen: false, versions: [], versionsOpen: false, versionSteps: {}, validationIssues: [], validated: false,
      });
    } else if (runId && runId !== get().selectedRunId) {
      set({ selectedRunId: runId, selectedRun: null, previousRun: null });
    }
    set({ workflowLoading: true });
    try {
      const workflow = await api.getWorkflow(workflowId);
      if (get().workflowId !== workflowId) return;
      set({ workflow, workflowError: null, editorOpen: workflow.steps.length === 0 });
      void get().loadRuns();
    } catch (e) {
      logger.error('loadWorkflow failed', e);
      if (get().workflowId === workflowId) set({ workflowError: errorMessage(e, 'Could not load this workflow.') });
    } finally {
      if (get().workflowId === workflowId) set({ workflowLoading: false });
    }
  },

  saveDetails: async (name, description) => {
    const id = get().workflowId;
    if (!id || !name.trim()) return false;
    return applyUpdate(id, { name: name.trim(), description: description.trim() || null }, 'Saved.');
  },

  saveSteps: async (steps, changeNote) => {
    const id = get().workflowId;
    if (!id) return false;
    if (steps.length === 0) {
      toast('Keep at least one step.', 'error');
      return false;
    }
    if (steps.some((s) => !s.name.trim())) {
      toast('Every step needs a name.', 'error');
      return false;
    }
    if (get().validationIssues.length > 0) {
      toast('Fix the problems shown under the steps first.', 'error');
      return false;
    }
    const ok = await applyUpdate(
      id,
      { steps: cleanSteps(steps), change_note: changeNote?.trim() || null },
      'Steps saved as a new version. Past runs keep the steps they ran with.',
    );
    if (ok) {
      set({ validationIssues: [], validated: false });
      if (get().versionsOpen) void get().loadVersions();
    }
    return ok;
  },

  deleteWorkflow: () => {
    const workflow = get().workflow;
    if (!workflow) return;
    useUIStore.getState().showConfirm({
      title: `Delete "${workflow.name}"?`,
      message: 'The workflow, its versions and its run history are permanently deleted. Review tables, analyses and research runs its steps created are kept.',
      type: 'danger',
      confirmText: 'Delete workflow',
      onConfirm: async () => {
        try {
          await api.deleteWorkflow(workflow.id);
          jobPoller.stop();
          set((st) => ({
            workflows: st.workflows.filter((w) => w.id !== workflow.id),
            workflowId: null, workflow: null, runs: [], selectedRun: null, selectedRunId: null,
            running: false, progress: null,
          }));
          toast('Workflow deleted.', 'success');
          appNavigate('/workflows');
        } catch (e) {
          logger.error('deleteWorkflow failed', e);
          toast(errorMessage(e, 'Could not delete the workflow.'), 'error');
        }
      },
    });
  },

  duplicateWorkflow: async () => {
    const workflow = get().workflow;
    if (!workflow) return;
    try {
      const matterId = useMattersStore.getState().activeMatterId;
      const copy = await api.duplicateWorkflow(workflow.id, { matter_id: matterId });
      set((st) => ({ workflows: [workflowSummary(copy), ...st.workflows] }));
      toast(`Created "${copy.name}". You own the copy.`, 'success');
      appNavigate(`/workflows/${encodeURIComponent(copy.id)}`);
    } catch (e) {
      logger.error('duplicateWorkflow failed', e);
      toast(errorMessage(e, 'Could not duplicate the workflow.'), 'error');
    }
  },

  archiveWorkflow: () => {
    const workflow = get().workflow;
    if (!workflow) return;
    useUIStore.getState().showConfirm({
      title: `Archive "${workflow.name}"?`,
      message: 'An archived workflow leaves the list and cannot be run. Its runs and versions are kept, and you can unarchive it at any time.',
      type: 'warning',
      confirmText: 'Archive',
      onConfirm: async () => {
        try {
          const updated = await api.archiveWorkflow(workflow.id);
          replaceWorkflow(updated);
          toast('Workflow archived.', 'success');
        } catch (e) {
          logger.error('archiveWorkflow failed', e);
          toast(errorMessage(e, 'Could not archive the workflow.'), 'error');
        }
      },
    });
  },

  unarchiveWorkflow: async () => {
    const workflow = get().workflow;
    if (!workflow) return;
    try {
      replaceWorkflow(await api.unarchiveWorkflow(workflow.id));
      toast('Workflow restored to the list.', 'success');
    } catch (e) {
      logger.error('unarchiveWorkflow failed', e);
      toast(errorMessage(e, 'Could not unarchive the workflow.'), 'error');
    }
  },

  // ── Validation while editing ──
  validationIssues: [],
  validating: false,
  validated: false,

  validateDraft: (steps) => {
    if (validateTimer) clearTimeout(validateTimer);
    const seq = ++validateSeq;
    set({ validating: true });
    validateTimer = setTimeout(async () => {
      try {
        const result = await api.validateWorkflowSteps(cleanSteps(steps));
        if (seq !== validateSeq) return;
        set({ validationIssues: result.issues, validated: true, validating: false });
      } catch (e) {
        logger.debug('validateWorkflowSteps failed', e);
        if (seq === validateSeq) set({ validating: false });
      }
    }, 350);
  },

  clearValidation: () => {
    if (validateTimer) clearTimeout(validateTimer);
    validateTimer = null;
    validateSeq += 1;
    set({ validationIssues: [], validating: false, validated: false });
  },

  // ── Versions ──
  versions: [],
  versionsOpen: false,
  versionSteps: {},

  toggleVersions: () => {
    const open = !get().versionsOpen;
    set({ versionsOpen: open });
    if (open) void get().loadVersions();
  },

  loadVersions: async () => {
    const id = get().workflowId;
    if (!id) return;
    try {
      const { versions } = await api.listWorkflowVersions(id);
      if (get().workflowId === id) set({ versions });
    } catch (e) {
      logger.error('listWorkflowVersions failed', e);
      toast(errorMessage(e, 'Could not load the version history.'), 'error');
    }
  },

  loadVersionSteps: async (version) => {
    const id = get().workflowId;
    if (!id) return null;
    const cached = get().versionSteps[version];
    if (cached) return cached;
    try {
      const detail = await api.getWorkflowVersion(id, version);
      if (get().workflowId !== id) return null;
      set((st) => ({ versionSteps: { ...st.versionSteps, [version]: detail.steps } }));
      return detail.steps;
    } catch (e) {
      logger.error('getWorkflowVersion failed', e);
      return null;
    }
  },

  restoreVersion: (version) => {
    const workflow = get().workflow;
    if (!workflow) return;
    useUIStore.getState().showConfirm({
      title: `Restore version ${version}?`,
      message: `The steps of version ${version} become the current steps as a new version ${workflow.version + 1}. Nothing in the history is rewritten.`,
      type: 'warning',
      confirmText: 'Restore',
      onConfirm: async () => {
        try {
          replaceWorkflow(await api.restoreWorkflowVersion(workflow.id, version));
          toast(`Restored version ${version}.`, 'success');
          void get().loadVersions();
        } catch (e) {
          logger.error('restoreWorkflowVersion failed', e);
          toast(errorMessage(e, 'Could not restore that version.'), 'error');
        }
      },
    });
  },

  // ── Runs ──
  runs: [],
  runsLoading: false,
  loadRuns: async () => {
    const id = get().workflowId;
    if (!id) return;
    set({ runsLoading: true });
    try {
      const { runs } = await api.listWorkflowRuns(id);
      if (get().workflowId !== id) return;
      set({ runs });
      // Keep the open run fresh, or open the newest one when nothing is selected.
      const selected = get().selectedRunId || runs[0]?.id || null;
      if (selected) await get().selectRun(selected);
    } catch (e) {
      logger.error('loadWorkflowRuns failed', e);
    } finally {
      if (get().workflowId === id) set({ runsLoading: false });
    }
  },

  selectedRunId: null,
  selectedRun: null,
  selectRun: async (runId) => {
    const id = get().workflowId;
    if (!id) return;
    if (!runId) {
      set({ selectedRunId: null, selectedRun: null, previousRun: null });
      return;
    }
    if (get().selectedRunId !== runId) set({ selectedRunId: runId, previousRun: null });
    try {
      const run = await api.getWorkflowRun(id, runId);
      if (get().workflowId === id && get().selectedRunId === runId) {
        set({ selectedRun: run });
        if (get().compareOpen) await loadPrevious(id, run);
      }
    } catch (e) {
      logger.error('getWorkflowRun failed', e);
      toast(errorMessage(e, 'Could not load that run.'), 'error');
    }
  },

  previousRun: null,
  compareOpen: false,
  toggleCompare: async () => {
    const open = !get().compareOpen;
    set({ compareOpen: open });
    const id = get().workflowId;
    const run = get().selectedRun;
    if (open && id && run) await loadPrevious(id, run);
  },

  running: false,
  progress: null,
  runError: null,

  run: async (documentIds) => {
    const id = get().workflowId;
    const workflow = get().workflow;
    if (!id || !workflow || get().running) return;
    if (workflow.steps.length === 0) {
      toast('Add at least one step before running.', 'error');
      return;
    }
    if (documentIds.length === 0 && !workflow.matter_id) {
      toast('Pick the documents to run the workflow on.', 'error');
      return;
    }
    try {
      const started = await api.runWorkflow(id, documentIds);
      set({ pickerOpen: false });
      await trackJob(id, started.run_id, started.job_id);
    } catch (e) {
      logger.error('runWorkflow failed', e);
      const message = errorMessage(e, 'Could not start the workflow.');
      set({ running: false, progress: null, runError: message });
      toast(message, 'error');
    }
  },

  resume: async (runId) => {
    await continueRun(() => api.resumeWorkflowRun(get().workflowId || '', runId), 'Could not resume the run.');
  },

  cancel: async () => {
    const run = get().selectedRun;
    const jobId = run?.job_id;
    if (!jobId || !get().running) return;
    try {
      await api.cancelWorkflowJob(jobId);
      toast('Cancelling the run…', 'info');
    } catch (e) {
      logger.error('cancelWorkflowJob failed', e);
      toast(errorMessage(e, 'Could not cancel the run.'), 'error');
    }
  },

  retryStep: async (index) => {
    const runId = get().selectedRunId;
    if (!runId) return;
    await continueRun(() => api.retryWorkflowStep(get().workflowId || '', runId, index), 'Could not retry the step.');
  },

  skipStep: async (index) => {
    const runId = get().selectedRunId;
    if (!runId) return;
    await continueRun(() => api.skipWorkflowStep(get().workflowId || '', runId, index), 'Could not skip the step.');
  },

  approve: async (note) => {
    const runId = get().selectedRunId;
    if (!runId) return;
    await continueRun(() => api.approveWorkflowRun(get().workflowId || '', runId, note.trim() || null), 'Could not approve the run.');
  },

  reject: async (note) => {
    const id = get().workflowId;
    const runId = get().selectedRunId;
    if (!id || !runId) return;
    try {
      const run = await api.rejectWorkflowRun(id, runId, note.trim() || null);
      set({ selectedRun: run });
      toast('Run rejected. The remaining steps were skipped.', 'info');
      await get().loadRuns();
      void get().loadWorkflows();
    } catch (e) {
      logger.error('rejectWorkflowRun failed', e);
      toast(errorMessage(e, 'Could not reject the run.'), 'error');
    }
  },

  exportingDraft: false,
  downloadDraft: async (title, text) => {
    set({ exportingDraft: true });
    try {
      const blob = await api.exportDraft(title, text);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${(title || 'draft').replace(/[^\w\s-]/g, '').trim().slice(0, 60) || 'draft'}.docx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      logger.error('exportDraft failed', e);
      toast(errorMessage(e, 'Could not export the draft.'), 'error');
    } finally {
      set({ exportingDraft: false });
    }
  },

  // ── Shared UI state ──
  editorOpen: false,
  setEditorOpen: (open) => {
    set({ editorOpen: open });
    if (!open) get().clearValidation();
  },
  approverCandidates: [],
  approverMatterId: null,
  loadApproverCandidates: async (matterId) => {
    if (!matterId) {
      set({ approverCandidates: [], approverMatterId: null });
      return;
    }
    if (get().approverMatterId === matterId) return;
    try {
      const matter = await api.getMatter(matterId);
      set({ approverCandidates: matter.members, approverMatterId: matterId });
    } catch (e) {
      logger.debug('loadApproverCandidates failed', e);
      set({ approverCandidates: [], approverMatterId: matterId });
    }
  },
  pickerOpen: false,
  pickerDocs: [],
  pickerLoading: false,
  pickerLoaded: false,
  pickerMatterId: null,

  ensurePickerDocs: async () => {
    // Within an active matter the picker offers that matter's documents
    // (including colleagues' filed ones); otherwise everything the user can read.
    const matterId = useMattersStore.getState().activeMatterId;
    if ((get().pickerLoaded && get().pickerMatterId === matterId) || get().pickerLoading) return;
    set({ pickerLoading: true });
    try {
      const docs = matterId
        ? (await api.getMatterDocuments(matterId)).documents
        : await api.getAllDocuments();
      set({ pickerDocs: docs as RagDocument[], pickerLoaded: true, pickerMatterId: matterId });
    } catch (e) {
      logger.error('workflow picker load failed', e);
      toast(errorMessage(e, 'Could not load your documents.'), 'error');
      set({ pickerDocs: [] });
    } finally {
      set({ pickerLoading: false });
    }
  },

  openPicker: async () => {
    set({ pickerOpen: true });
    await get().ensurePickerDocs();
  },
  closePicker: () => set({ pickerOpen: false }),

  documentRefs: () => {
    const st = get();
    return mergeRefs(
      st.workflow?.documents,
      st.selectedRun?.document_refs,
      st.pickerDocs.map((d) => ({ id: d.id, filename: d.filename })),
    );
  },
}));

/** Submit a continuation (resume, retry, skip, approve) and track its job. */
async function continueRun(start: () => Promise<{ run_id: string; job_id: string }>, failure: string): Promise<void> {
  const st = useWorkflowsStore.getState();
  const id = st.workflowId;
  if (!id || st.running) return;
  try {
    const started = await start();
    await trackJob(id, started.run_id, started.job_id);
  } catch (e) {
    logger.error('workflow continuation failed', e);
    const message = errorMessage(e, failure);
    useWorkflowsStore.setState({ running: false, progress: null, runError: message });
    toast(message, 'error');
  }
}

/** The run before `run` in this workflow's history, for the comparison view. */
async function loadPrevious(workflowId: string, run: WorkflowRunDetail): Promise<void> {
  const runs = useWorkflowsStore.getState().runs;
  const index = runs.findIndex((r) => r.id === run.id);
  const previous = index >= 0 ? runs[index + 1] : null;
  if (!previous) {
    useWorkflowsStore.setState({ previousRun: null });
    return;
  }
  try {
    const detail = await api.getWorkflowRun(workflowId, previous.id);
    const st = useWorkflowsStore.getState();
    if (st.workflowId === workflowId && st.selectedRunId === run.id) useWorkflowsStore.setState({ previousRun: detail });
  } catch (e) {
    logger.debug('previous run load failed', e);
  }
}

/** Poll the job behind a run, refreshing the run detail as steps complete. */
async function trackJob(workflowId: string, runId: string, jobId: string): Promise<void> {
  const st = useWorkflowsStore.getState();
  jobPoller.stop();
  useWorkflowsStore.setState({ running: true, runError: null, progress: { message: 'Starting…', fraction: 0 }, selectedRunId: runId });
  await st.loadRuns();
  let lastRefresh = 0;
  jobPoller.start({
    fetchJob: () => api.getWorkflowJob(jobId),
    maxAttempts: 1800,
    intervalMs: 2000,
    failedMessage: 'The workflow run failed.',
    timeoutMessage: 'Timed out waiting for the workflow to finish.',
    onPending: (job, attempt) => {
      const current = useWorkflowsStore.getState();
      if (current.workflowId !== workflowId) return;
      if (job.progress) useWorkflowsStore.setState({ progress: { message: job.progress.message, fraction: job.progress.fraction } });
      // Refresh the step list every few polls so completed steps appear as they finish.
      if (attempt - lastRefresh >= 3) {
        lastRefresh = attempt;
        void current.selectRun(runId);
      }
    },
    onCompleted: async (job) => {
      const current = useWorkflowsStore.getState();
      if (current.workflowId !== workflowId) return;
      useWorkflowsStore.setState({ running: false, progress: null });
      toast(describeRun(job.result), job.result?.status === 'awaiting_approval' ? 'info' : 'success');
      await current.loadRuns();
      void current.loadWorkflows();
      void current.loadRecentRuns();
    },
    onFailed: async (message) => {
      const current = useWorkflowsStore.getState();
      if (current.workflowId !== workflowId) return;
      useWorkflowsStore.setState({ running: false, progress: null, runError: message });
      toast(message, 'error');
      await current.loadRuns(); // whatever finished before the failure is already stored
      void current.loadWorkflows();
      void current.loadRecentRuns();
    },
  });
}

function workflowSummary(workflow: WorkflowDetail): WorkflowSummary {
  const { steps: _steps, documents: _documents, ...summary } = workflow;
  return summary;
}

function replaceWorkflow(workflow: WorkflowDetail): void {
  useWorkflowsStore.setState((prev) => ({
    workflow: prev.workflowId === workflow.id ? workflow : prev.workflow,
    workflows: prev.workflows.map((w) => (w.id === workflow.id ? workflowSummary(workflow) : w)),
  }));
}

/** Trim names and drop empty optional strings so the server stores exactly what was meant. */
export function cleanSteps(steps: WorkflowStep[]): WorkflowStep[] {
  return steps.map((s) => {
    const params: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(s.params || {})) {
      if (typeof value === 'string') {
        const trimmed = value.trim();
        if (trimmed || key === 'instructions' || key === 'question' || key === 'title') params[key] = trimmed;
      } else if (value !== undefined) {
        params[key] = value;
      }
    }
    const condition = s.condition && s.condition.step
      ? { step: s.condition.step, path: s.condition.path || '', op: s.condition.op, value: s.condition.value ?? null }
      : null;
    return {
      name: s.name.trim(),
      kind: s.kind,
      params,
      inputs: {
        source: s.inputs.source,
        document_ids: s.inputs.source === 'documents' ? s.inputs.document_ids : [],
        step: s.inputs.source === 'step' ? s.inputs.step : null,
      },
      condition,
    };
  });
}

/** PATCH the open workflow and replace it in the store. */
async function applyUpdate(
  id: string,
  changes: Parameters<typeof api.updateWorkflow>[1],
  successMessage: string,
): Promise<boolean> {
  try {
    const workflow = await api.updateWorkflow(id, changes);
    const st = useWorkflowsStore.getState();
    if (st.workflowId !== id) return true;
    replaceWorkflow(workflow);
    toast(successMessage, 'success');
    return true;
  } catch (e) {
    logger.error('updateWorkflow failed', e);
    toast(errorMessage(e, 'Could not save the change.'), 'error');
    return false;
  }
}

registerReset(() => {
  jobPoller.stop();
  if (validateTimer) clearTimeout(validateTimer);
  validateTimer = null;
  useWorkflowsStore.setState(useWorkflowsStore.getInitialState(), true);
});
