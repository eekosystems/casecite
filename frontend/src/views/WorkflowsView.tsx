import { useEffect, useMemo, useState } from 'react';
import { useWorkflowsStore, newStep, filterWorkflows, LIST_FILTERS, type WorkflowListFilter } from '../stores/workflowsStore';
import { useMattersStore } from '../stores/mattersStore';
import {
  Icon, WorkflowCard, WorkflowTemplateGallery, WorkflowStepList, WorkflowListFilters, WorkflowRecentRuns,
} from '../components';
import type { WorkflowStep } from '../api/types';
import s from './WorkflowsView.module.css';

/** The firm's workflows: recent runs, search and filters, templates, and a validated step editor for new ones. */
function WorkflowsView() {
  const {
    workflows, workflowsLoading, templates, recentRuns, search, listFilter, setSearch, setListFilter,
    init, loadWorkflows, loadRecentRuns, createWorkflow, createFromTemplate, openWorkflow, openRun,
    validationIssues, validating, validateDraft, clearValidation,
    pickerDocs, pickerLoading, ensurePickerDocs, approverCandidates, loadApproverCandidates,
  } = useWorkflowsStore();
  const activeMatter = useMattersStore((st) => st.activeMatter());
  const activeMatterId = useMattersStore((st) => st.activeMatterId);
  const mattersInit = useMattersStore((st) => st.init);
  useEffect(() => { init(); mattersInit(); }, [init, mattersInit]);
  useEffect(() => { void loadRecentRuns(); }, [activeMatterId, loadRecentRuns]);

  // Local UI state only: the create form and which template is being instantiated.
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [steps, setSteps] = useState<WorkflowStep[]>(() => [newStep('extract_fields', [])]);
  const [creating, setCreating] = useState(false);
  const [templateBusy, setTemplateBusy] = useState<string | null>(null);
  useEffect(() => {
    if (formOpen) { void ensurePickerDocs(); loadApproverCandidates(activeMatterId); } else clearValidation();
  }, [formOpen, ensurePickerDocs, loadApproverCandidates, activeMatterId, clearValidation]);

  const changeSteps = (next: WorkflowStep[]) => { setSteps(next); validateDraft(next); };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    const workflow = await createWorkflow({ name, description, steps });
    setCreating(false);
    if (workflow) {
      setName(''); setDescription(''); setSteps([newStep('extract_fields', [])]); setFormOpen(false);
      openWorkflow(workflow.id);
    }
  };

  const useTemplate = async (key: string) => {
    setTemplateBusy(key);
    const workflow = await createFromTemplate(key);
    setTemplateBusy(null);
    if (workflow) openWorkflow(workflow.id);
  };

  const visible = useMemo(() => filterWorkflows(workflows, search, listFilter, recentRuns), [workflows, search, listFilter, recentRuns]);
  const counts = useMemo(() => {
    const out: Partial<Record<WorkflowListFilter, number>> = {};
    for (const f of LIST_FILTERS) out[f.key] = filterWorkflows(workflows, '', f.key, recentRuns).length;
    return out;
  }, [workflows, recentRuns]);
  const scopeLabel = activeMatter ? (activeMatter.is_personal ? 'Personal' : activeMatter.name) : null;

  return (
    <div className={s.container}>
      <div className={s.content}>
        <div className={s.headerRow}>
          <div>
            <h2 className={s.title}>Workflows</h2>
            <p className={s.subtitle}>
              Save a review, research or drafting process as an ordered list of steps, then run it over any set of
              documents. Every step&apos;s output is kept, a run can pause for an approval or skip a step on a condition,
              and every change to the steps is versioned.
            </p>
          </div>
          <div className={s.headerActions}>
            <button className={s.secondaryBtn} onClick={() => { void loadWorkflows(); void loadRecentRuns(); }} disabled={workflowsLoading}>
              <Icon name={workflowsLoading ? 'Loader2' : 'RefreshCw'} size={14} className={workflowsLoading ? s.spin : undefined} />
              Refresh
            </button>
            <button className={s.primaryBtn} onClick={() => setFormOpen((o) => !o)}>
              <Icon name="Plus" size={14} /> New workflow
            </button>
          </div>
        </div>

        {formOpen && (
          <form className={s.createForm} onSubmit={submit}>
            <div className={s.formRow}>
              <input
                className={s.input}
                placeholder="Workflow name, e.g. Vendor contract intake"
                value={name}
                onChange={(e) => setName(e.target.value)}
                aria-label="Workflow name"
                autoFocus
              />
              <input
                className={`${s.input} ${s.inputWide}`}
                placeholder="What this workflow is for (optional)"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                aria-label="Description"
              />
            </div>
            <div className={s.formLabel}>
              Steps, in order
              <span className={s.formLabelHint}>{validating ? ' · checking…' : validationIssues.length ? ` · ${validationIssues.length} problem${validationIssues.length === 1 ? '' : 's'}` : ''}</span>
            </div>
            <WorkflowStepList
              steps={steps}
              onChange={changeSteps}
              issues={validationIssues}
              documents={pickerDocs}
              documentsLoading={pickerLoading}
              members={approverCandidates}
              styles={s}
            />
            <div className={s.formNote}>
              <Icon name={scopeLabel ? 'Target' : 'Info'} size={13} />
              {scopeLabel
                ? `Filed under "${scopeLabel}": members of that matter can see and run it, and "Run on matter" uses its documents.`
                : 'Private to you. Set an active matter in the header to share a workflow with the matter’s members.'}
            </div>
            <div className={s.formActions}>
              <button type="submit" className={s.primaryBtn} disabled={creating || validating || validationIssues.length > 0 || !name.trim() || steps.length === 0}>
                <Icon name={creating ? 'Loader2' : 'Check'} size={14} className={creating ? s.spin : undefined} />
                Create workflow
              </button>
              <button type="button" className={s.secondaryBtn} onClick={() => setFormOpen(false)}>Cancel</button>
            </div>
          </form>
        )}

        {recentRuns.length > 0 && (
          <>
            <div className={s.sectionLabel}>Recent runs{scopeLabel ? ` · ${scopeLabel}` : ''}</div>
            <WorkflowRecentRuns runs={recentRuns} onOpen={openRun} styles={s} />
          </>
        )}

        {templates.length > 0 && (
          <>
            <div className={s.sectionLabel}>Start from a template</div>
            <WorkflowTemplateGallery templates={templates} busyKey={templateBusy} onUse={useTemplate} styles={s} />
          </>
        )}

        <div className={s.sectionLabel}>Your workflows</div>
        <WorkflowListFilters search={search} filter={listFilter} counts={counts} onSearch={setSearch} onFilter={setListFilter} styles={s} />
        {workflowsLoading && workflows.length === 0 ? (
          <div className={s.empty}><Icon name="Loader2" size={16} className={s.spin} /> Loading workflows…</div>
        ) : visible.length === 0 ? (
          <div className={s.empty}>
            <Icon name="Workflow" size={18} />
            <p>{workflows.length === 0 ? 'No workflows yet. Start from a template above, or build one step by step.' : 'Nothing matches this search and filter.'}</p>
          </div>
        ) : (
          <div className={s.grid}>
            {visible.map((w) => (
              <WorkflowCard key={w.id} workflow={w} onOpen={openWorkflow} styles={s} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default WorkflowsView;
