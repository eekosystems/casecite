import { create } from 'zustand';
import { api } from '../api';
import { useUIStore } from './uiStore';
import { useMattersStore } from './mattersStore';
import { registerReset } from './resetRegistry';
import { createJobPoller, toast } from './storeUtils';
import logger from '../utils/logger';
import type { DocumentFilter } from '../types';
import type {
  DeepResearchDocumentFilter,
  DeepResearchResult,
  DeepResearchRunDetail,
  DeepResearchRunSummary,
  DeepResearchScope,
  DeepResearchSubQuestion,
  DeepResearchTool,
} from '../api/types';

export interface RunProgress {
  message: string;
  fraction: number | null;
}

const errorMessage = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** The explicit file-workspace pick, in the shape the endpoints accept (null = none). */
export function toDocumentFilter(filter: DocumentFilter | null | undefined): DeepResearchDocumentFilter | null {
  if (!filter) return null;
  const { document_ids, folder_paths, sources, doc_types, include_subfolders, search_all, matter_ids } = filter;
  if (
    !document_ids?.length && !folder_paths?.length && !sources?.length && !doc_types?.length &&
    !matter_ids?.length && !search_all
  ) {
    return null;
  }
  return { document_ids, folder_paths, sources, doc_types, include_subfolders, search_all, matter_ids };
}

/** One line summarising a finished run for the toast. */
export function describeResult(result: DeepResearchResult | null | undefined): string {
  if (!result) return 'Deep research finished.';
  const v = result.verification;
  const parts = [`${result.sources.length} source${result.sources.length === 1 ? '' : 's'} cited`];
  if (v.quotes_verified) parts.push(`${v.quotes_verified} quote${v.quotes_verified === 1 ? '' : 's'} verified`);
  if (v.quotes_unverified) parts.push(`${v.quotes_unverified} unverified`);
  if (result.scope.steps_failed) parts.push(`${result.scope.steps_failed} step${result.scope.steps_failed === 1 ? '' : 's'} failed`);
  return `Deep research finished: ${parts.join(', ')}.`;
}

let nextStepSeq = 1;
/** A client-side id for a sub-question the user adds by hand; unique within the plan. */
export function newStepId(plan: DeepResearchSubQuestion[]): string {
  const taken = new Set(plan.map((s) => s.id));
  let id = `u${nextStepSeq}`;
  while (taken.has(id)) {
    nextStepSeq += 1;
    id = `u${nextStepSeq}`;
  }
  nextStepSeq += 1;
  return id;
}

export interface DeepResearchState {
  /** The panel is shown in the Research view. */
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;

  // ── Question and plan ──
  question: string;
  setQuestion: (question: string) => void;
  planning: boolean;
  planError: string | null;
  plan: DeepResearchSubQuestion[];
  planScope: DeepResearchScope | null;
  caps: Record<string, number>;
  draftPlan: (docFilter: DocumentFilter | null) => Promise<void>;
  updateStep: (id: string, patch: { question?: string; tools?: DeepResearchTool[] }) => void;
  removeStep: (id: string) => void;
  addStep: () => void;
  clearPlan: () => void;

  // ── Run ──
  running: boolean;
  progress: RunProgress | null;
  runError: string | null;
  runId: string | null;
  jobId: string | null;
  run: (docFilter: DocumentFilter | null) => Promise<void>;
  cancel: () => Promise<void>;

  // ── Result ──
  result: DeepResearchResult | null;
  resultRun: DeepResearchRunDetail | null;
  showSteps: boolean;
  setShowSteps: (show: boolean) => void;
  clearResult: () => void;

  // ── History ──
  runs: DeepResearchRunSummary[];
  runsLoading: boolean;
  loadRuns: () => Promise<void>;
  openRun: (runId: string) => Promise<void>;
  deleteRun: (runId: string) => void;
}

const jobPoller = createJobPoller();

export const useDeepResearchStore = create<DeepResearchState>((set, get) => ({
  open: false,
  setOpen: (open) => {
    set({ open });
    if (open) void get().loadRuns();
  },
  toggle: () => get().setOpen(!get().open),

  // ── Question and plan ──
  question: '',
  setQuestion: (question) => set({ question }),
  planning: false,
  planError: null,
  plan: [],
  planScope: null,
  caps: {},

  draftPlan: async (docFilter) => {
    const question = get().question.trim();
    if (question.length < 3) {
      toast('Ask a research question first.', 'error');
      return;
    }
    set({ planning: true, planError: null, result: null, resultRun: null, runError: null });
    try {
      const response = await api.planDeepResearch({
        question,
        matter_id: useMattersStore.getState().activeMatterId,
        document_filter: toDocumentFilter(docFilter),
      });
      set({ plan: response.plan, planScope: response.scope, caps: response.caps });
    } catch (e) {
      logger.error('planDeepResearch failed', e);
      set({ planError: errorMessage(e, 'Could not draft a research plan.') });
    } finally {
      set({ planning: false });
    }
  },

  updateStep: (id, patch) => {
    set((st) => ({
      plan: st.plan.map((s) => (s.id === id ? { ...s, ...patch } : s)),
    }));
  },

  removeStep: (id) => set((st) => ({ plan: st.plan.filter((s) => s.id !== id) })),

  addStep: () => {
    const { plan, caps } = get();
    const max = caps.max_subquestions ?? 8;
    if (plan.length >= max) {
      toast(`A plan may have at most ${max} sub-questions.`, 'error');
      return;
    }
    set({ plan: [...plan, { id: newStepId(plan), question: '', tools: ['documents', 'case_law'] }] });
  },

  clearPlan: () => set({ plan: [], planScope: null, planError: null }),

  // ── Run ──
  running: false,
  progress: null,
  runError: null,
  runId: null,
  jobId: null,

  run: async (docFilter) => {
    const { question, plan, running } = get();
    if (running) return;
    const steps = plan
      .map((s) => ({ ...s, question: s.question.trim() }))
      .filter((s) => s.question.length >= 3 && s.tools.length > 0);
    if (steps.length === 0) {
      toast('Add at least one sub-question with a tool before running.', 'error');
      return;
    }
    jobPoller.stop();
    set({
      running: true, runError: null, result: null, resultRun: null,
      progress: { message: 'Starting…', fraction: 0 },
    });
    try {
      const started = await api.runDeepResearch({
        question: question.trim(),
        matter_id: useMattersStore.getState().activeMatterId,
        document_filter: toDocumentFilter(docFilter),
        plan: steps,
      });
      set({ runId: started.run_id, jobId: started.job_id, plan: steps });
      jobPoller.start({
        fetchJob: () => api.getDeepResearchJob(started.job_id),
        maxAttempts: 900,
        intervalMs: 2000,
        failedMessage: 'The research run failed.',
        timeoutMessage: 'Timed out waiting for the research to finish.',
        onPending: (job) => {
          if (get().jobId !== started.job_id) return;
          if (job.progress) set({ progress: { message: job.progress.message, fraction: job.progress.fraction } });
        },
        onCompleted: async (job) => {
          if (get().jobId !== started.job_id) return;
          set({ running: false, progress: null, result: job.result || null, showSteps: false });
          toast(describeResult(job.result), job.result?.scope.steps_failed ? 'warning' : 'success');
          void get().loadRuns();
          try {
            set({ resultRun: await api.getDeepResearch(started.run_id) });
          } catch (e) {
            logger.warn('getDeepResearch after completion failed', e);
          }
        },
        onFailed: (message) => {
          if (get().jobId !== started.job_id) return;
          set({ running: false, progress: null, runError: message });
          toast(message, 'error');
          void get().loadRuns();
        },
      });
    } catch (e) {
      logger.error('runDeepResearch failed', e);
      const message = errorMessage(e, 'Could not start the research run.');
      set({ running: false, progress: null, runError: message });
      toast(message, 'error');
    }
  },

  cancel: async () => {
    const { jobId, running } = get();
    if (!jobId || !running) return;
    try {
      await api.cancelDeepResearchJob(jobId);
      jobPoller.stop();
      set({ running: false, progress: null, runError: 'Cancelled.' });
      toast('Research run cancelled.', 'info');
      void get().loadRuns();
    } catch (e) {
      logger.error('cancelDeepResearchJob failed', e);
      toast(errorMessage(e, 'Could not cancel the run.'), 'error');
    }
  },

  // ── Result ──
  result: null,
  resultRun: null,
  showSteps: false,
  setShowSteps: (show) => set({ showSteps: show }),
  clearResult: () => set({ result: null, resultRun: null, runError: null }),

  // ── History ──
  runs: [],
  runsLoading: false,

  loadRuns: async () => {
    set({ runsLoading: true });
    try {
      const { runs } = await api.listDeepResearch(useMattersStore.getState().activeMatterId);
      set({ runs });
    } catch (e) {
      logger.error('listDeepResearch failed', e);
    } finally {
      set({ runsLoading: false });
    }
  },

  openRun: async (runId) => {
    try {
      const run = await api.getDeepResearch(runId);
      set({
        resultRun: run,
        result: run.result,
        runId: run.id,
        question: run.question,
        plan: run.plan,
        runError: run.status === 'completed' ? null : run.error || `This run ${run.status}.`,
        showSteps: false,
      });
    } catch (e) {
      logger.error('getDeepResearch failed', e);
      toast(errorMessage(e, 'Could not open that research run.'), 'error');
    }
  },

  deleteRun: (runId) => {
    useUIStore.getState().showConfirm({
      title: 'Delete research run',
      message: 'Delete this run and its answer? This cannot be undone.',
      confirmText: 'Delete',
      onConfirm: async () => {
        try {
          await api.deleteDeepResearch(runId);
          set((st) => ({
            runs: st.runs.filter((r) => r.id !== runId),
            ...(st.runId === runId ? { result: null, resultRun: null, runId: null } : {}),
          }));
          toast('Research run deleted.', 'success');
        } catch (e) {
          logger.error('deleteDeepResearch failed', e);
          toast(errorMessage(e, 'Could not delete the run.'), 'error');
        }
      },
    });
  },
}));

registerReset(() => {
  jobPoller.stop();
  useDeepResearchStore.setState(useDeepResearchStore.getInitialState(), true);
});
