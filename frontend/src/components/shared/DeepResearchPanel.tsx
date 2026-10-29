import { useEffect } from 'react';
import { Icon } from './Icon';
import { DeepResearchPlanEditor } from './DeepResearchPlanEditor';
import { DeepResearchResult } from './DeepResearchResult';
import { DeepResearchHistory } from './DeepResearchHistory';
import { useDeepResearchStore } from '../../stores/deepResearchStore';
import { useMattersStore } from '../../stores/mattersStore';
import type { DocumentFilter } from '../../types';
import s from './DeepResearchPanel.module.css';

interface DeepResearchPanelProps {
  /** The explicit pick in the Research file workspace, if any; it wins over the active matter. */
  docFilter: DocumentFilter | null;
}

/**
 * Deep research inside the Research view: ask, review and edit the plan, run
 * it as a background job, read the cited answer with its step log and sources,
 * and reopen past runs. All state lives in deepResearchStore.
 */
export function DeepResearchPanel({ docFilter }: DeepResearchPanelProps) {
  const {
    setOpen, question, setQuestion, planning, planError, plan, planScope, caps, draftPlan,
    updateStep, removeStep, addStep, running, progress, runError, run, cancel,
    result, resultRun, showSteps, setShowSteps, clearResult, runs, runsLoading, runId, openRun, deleteRun,
  } = useDeepResearchStore();
  const activeMatter = useMattersStore((st) => st.activeMatter());

  // Lifecycle only: the history follows the active matter.
  const activeMatterId = useMattersStore((st) => st.activeMatterId);
  useEffect(() => {
    void useDeepResearchStore.getState().loadRuns();
  }, [activeMatterId]);

  const pickedMatters = docFilter?.matter_ids?.length ?? 0;
  const pickedDocs = docFilter?.document_ids?.length ?? 0;
  const scopeLabel = pickedMatters
    ? `${pickedMatters} picked matter${pickedMatters === 1 ? '' : 's'}` +
      (pickedDocs ? ` and ${pickedDocs} document${pickedDocs === 1 ? '' : 's'}` : '')
    : pickedDocs
    ? `${pickedDocs} picked document${pickedDocs === 1 ? '' : 's'}`
    : docFilter?.folder_paths?.length
      ? `${docFilter.folder_paths.length} picked folder${docFilter.folder_paths.length === 1 ? '' : 's'}`
      : activeMatter
        ? `the matter "${activeMatter.name}"`
        : 'every document you can read';

  const canPlan = question.trim().length >= 3 && !planning && !running;
  const canRun = plan.length > 0 && !running && !planning;

  return (
    <section className={s.panel} aria-label="Deep research">
      <div className={s.head}>
        <div className={s.title}>
          <Icon name="ScanSearch" size={15} />
          <span>Deep research</span>
          <span className={s.subtitle}>plan, research step by step, verify every quote</span>
        </div>
        <button type="button" className={s.iconBtn} onClick={() => setOpen(false)} aria-label="Close deep research">
          <Icon name="X" size={15} />
        </button>
      </div>

      <div className={s.askRow}>
        <textarea
          className={s.question}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="What do you need researched? e.g. Is the liability cap in the Acme MSA enforceable under Delaware law?"
          rows={2}
          disabled={running}
          aria-label="Research question"
        />
        <button type="button" className={s.primaryBtn} onClick={() => void draftPlan(docFilter)} disabled={!canPlan}>
          <Icon name={planning ? 'Loader2' : 'Sparkles'} size={14} className={planning ? s.spin : undefined} />
          {planning ? 'Drafting…' : plan.length ? 'Redraft plan' : 'Draft plan'}
        </button>
      </div>
      <div className={s.scopeLine}>
        <Icon name="Info" size={12} />
        <span>
          Documents searched: {scopeLabel}
          {planScope ? ` (${planScope.documents_in_scope} indexed)` : ''}.
          {planScope && !planScope.case_law_available ? ' Case-law search is unavailable on this server.' : ' Case law is searched on CourtListener.'}
        </span>
      </div>
      {planError && <div className={s.error}>{planError}</div>}

      {plan.length > 0 && (
        <DeepResearchPlanEditor
          plan={plan}
          caps={caps}
          disabled={running}
          onChange={updateStep}
          onRemove={removeStep}
          onAdd={addStep}
          styles={s}
        />
      )}

      {(plan.length > 0 || running) && (
        <div className={s.runBar}>
          <div className={s.runInfo}>
            {running && progress ? (
              <>
                <Icon name="Loader2" size={14} className={s.spin} />
                <span>{progress.message}</span>
                {progress.fraction !== null && (
                  <div className={s.progressTrack} aria-hidden="true">
                    <div className={s.progressFill} style={{ width: `${Math.round(progress.fraction * 100)}%` }} />
                  </div>
                )}
              </>
            ) : (
              <span className={s.muted}>
                {plan.length} sub-question{plan.length === 1 ? '' : 's'}; up to {caps.max_tool_calls ?? '—'} searches and{' '}
                {caps.max_passages ?? '—'} passages per run. Only passages the run retrieved can be cited.
              </span>
            )}
          </div>
          {running ? (
            <button type="button" className={s.secondaryBtn} onClick={() => void cancel()}>
              <Icon name="X" size={14} /> Cancel
            </button>
          ) : (
            <button type="button" className={s.primaryBtn} onClick={() => void run(docFilter)} disabled={!canRun}>
              <Icon name="Play" size={14} /> Run research
            </button>
          )}
        </div>
      )}
      {runError && !running && <div className={s.error}>{runError}</div>}

      {result && (
        <DeepResearchResult
          result={result}
          run={resultRun}
          showSteps={showSteps}
          onToggleSteps={() => setShowSteps(!showSteps)}
          onClear={clearResult}
          styles={s}
        />
      )}

      <DeepResearchHistory
        runs={runs}
        loading={runsLoading}
        activeId={runId}
        onOpen={(id) => void openRun(id)}
        onDelete={deleteRun}
        styles={s}
      />
    </section>
  );
}
