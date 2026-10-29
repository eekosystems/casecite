import { useEffect, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { useWorkflowsStore } from '../stores/workflowsStore';
import { useMattersStore } from '../stores/mattersStore';
import { useAuthStore } from '../stores/authStore';
import { appNavigate } from '../utils/router';
import { documentName } from '../utils/workflowDiff';
import {
  Icon,
  ReviewDocumentPicker,
  WorkflowStepList,
  WorkflowRunBar,
  WorkflowRunList,
  WorkflowStepRunCard,
  WorkflowApprovalBanner,
  WorkflowVersionsDrawer,
  WorkflowRunCompare,
} from '../components';
import { conditionSummary } from '../components/shared/WorkflowConditionEditor';
import type { WorkflowStep } from '../api/types';
import s from './WorkflowView.module.css';

/** One workflow: its steps (editable by the owner, validated as typed), versions, the run bar, and the run history with per-step outcomes and controls. */
function WorkflowView() {
  const { id } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const {
    workflow, workflowError, loadWorkflow,
    saveDetails, saveSteps, deleteWorkflow, duplicateWorkflow, archiveWorkflow, unarchiveWorkflow,
    validationIssues, validating, validated, validateDraft,
    versions, versionsOpen, toggleVersions, loadVersionSteps, restoreVersion,
    runs, selectedRunId, selectedRun, selectRun, previousRun, compareOpen, toggleCompare,
    running, progress, runError, run, resume, cancel, retryStep, skipStep, approve, reject,
    exportingDraft, downloadDraft,
    editorOpen, setEditorOpen,
    pickerOpen, pickerDocs, pickerLoading, ensurePickerDocs, openPicker, closePicker, documentRefs,
    approverCandidates, loadApproverCandidates,
  } = useWorkflowsStore();
  const activeMatter = useMattersStore((st) => st.activeMatter());
  const matters = useMattersStore((st) => st.matters);
  const mattersInit = useMattersStore((st) => st.init);
  const userId = useAuthStore((st) => st.user?.id || null);

  const runParam = searchParams.get('run');
  useEffect(() => { mattersInit(); }, [mattersInit]);
  useEffect(() => { if (id) loadWorkflow(id, runParam); }, [id, runParam, loadWorkflow]);
  useEffect(() => { if (editorOpen) { void ensurePickerDocs(); loadApproverCandidates(workflow?.matter_id || null); } }, [editorOpen, ensurePickerDocs, loadApproverCandidates, workflow?.matter_id]);
  // Keep the run in the URL so a run can be shared by link.
  useEffect(() => {
    if (selectedRunId && selectedRunId !== runParam) setSearchParams({ run: selectedRunId }, { replace: true });
  }, [selectedRunId, runParam, setSearchParams]);

  // Local UI state: the step draft while the editor is open, and the rename form.
  const [draftSteps, setDraftSteps] = useState<WorkflowStep[]>([]);
  const [changeNote, setChangeNote] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  useEffect(() => { if (editorOpen && workflow) { setDraftSteps(workflow.steps); setChangeNote(''); } }, [editorOpen, workflow]);

  const isOwner = workflow?.role === 'owner';
  const archived = !!workflow?.archived_at;
  const workflowMatter = workflow?.matter_id ? matters.find((m) => m.id === workflow.matter_id) || null : null;
  const matterName = workflowMatter ? (workflowMatter.is_personal ? 'Personal' : workflowMatter.name) : null;
  const refs = documentRefs();
  const canControlRun = !!selectedRun && !!workflow && (isOwner || selectedRun.runner_id === userId) && !running;
  const runControllable = selectedRun?.status === 'failed' || selectedRun?.status === 'cancelled';

  const startRename = () => {
    if (!workflow) return;
    setName(workflow.name); setDescription(workflow.description || ''); setRenaming(true);
  };
  const submitRename = async (e: React.FormEvent) => {
    e.preventDefault();
    if (await saveDetails(name, description)) setRenaming(false);
  };
  const changeDraft = (steps: WorkflowStep[]) => { setDraftSteps(steps); validateDraft(steps); };
  const submitSteps = async () => {
    if (await saveSteps(draftSteps, changeNote)) setEditorOpen(false);
  };
  const draftClean = validated && validationIssues.length === 0;

  if (workflowError) {
    return (
      <div className={s.container}>
        <div className={s.content}>
          <button className={s.backBtn} onClick={() => appNavigate('/workflows')}>
            <Icon name="ArrowLeft" size={14} /> All workflows
          </button>
          <div className={s.empty}><Icon name="AlertCircle" size={18} /><p>{workflowError}</p></div>
        </div>
      </div>
    );
  }

  if (!workflow) {
    return (
      <div className={s.container}>
        <div className={s.content}>
          <div className={s.empty}><Icon name="Loader2" size={16} className={s.spin} /> Loading workflow…</div>
        </div>
      </div>
    );
  }

  return (
    <div className={s.container}>
      <div className={s.content}>
        <button className={s.backBtn} onClick={() => appNavigate('/workflows')}>
          <Icon name="ArrowLeft" size={14} /> All workflows
        </button>

        <div className={s.headerRow}>
          {renaming ? (
            <form className={s.renameForm} onSubmit={submitRename}>
              <input className={s.input} value={name} onChange={(e) => setName(e.target.value)} aria-label="Workflow name" autoFocus />
              <input className={`${s.input} ${s.inputWide}`} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Description" aria-label="Description" />
              <button type="submit" className={s.primaryBtn} disabled={!name.trim()}><Icon name="Check" size={14} /> Save</button>
              <button type="button" className={s.secondaryBtn} onClick={() => setRenaming(false)}>Cancel</button>
            </form>
          ) : (
            <div className={s.titleBlock}>
              <div className={s.titleRow}>
                <h2 className={s.title}>{workflow.name}</h2>
                <span className={`${s.roleBadge} ${isOwner ? s.roleOwner : ''}`}>{workflow.role}</span>
                <span className={s.roleBadge} title="Every change to the steps records a version">v{workflow.version}</span>
                {workflow.template_key && <span className={s.roleBadge}>from template</span>}
                {archived && <span className={`${s.roleBadge} ${s.roleArchived}`}>archived</span>}
                {isOwner && (
                  <button type="button" className={s.iconBtn} onClick={startRename} title="Rename" aria-label="Rename workflow">
                    <Icon name="Pencil" size={13} />
                  </button>
                )}
              </div>
              {workflow.description && <p className={s.subtitle}>{workflow.description}</p>}
              <div className={s.metaRow}>
                <span>{workflow.step_count} step{workflow.step_count === 1 ? '' : 's'}</span>
                <span>{workflow.run_count} run{workflow.run_count === 1 ? '' : 's'}</span>
                {matterName && <span>Filed under {matterName}</span>}
                {workflow.last_run_at && <span>Last run {new Date(workflow.last_run_at).toLocaleString()}</span>}
              </div>
            </div>
          )}
          <div className={s.headerActions}>
            <button className={`${s.secondaryBtn} ${editorOpen ? s.secondaryBtnActive : ''}`} onClick={() => setEditorOpen(!editorOpen)} disabled={!isOwner || running || archived} title={isOwner ? undefined : 'Only the owner changes the steps'}>
              <Icon name="ListOrdered" size={14} /> Steps
            </button>
            <button className={`${s.secondaryBtn} ${versionsOpen ? s.secondaryBtnActive : ''}`} onClick={toggleVersions} title="Version history">
              <Icon name="History" size={14} /> Versions
            </button>
            <button className={s.secondaryBtn} onClick={() => void duplicateWorkflow()} disabled={running} title="A copy you own, at version 1">
              <Icon name="Copy" size={14} /> Duplicate
            </button>
            {isOwner && (archived ? (
              <button className={s.secondaryBtn} onClick={() => void unarchiveWorkflow()} title="Return it to the list">
                <Icon name="ArchiveRestore" size={14} /> Unarchive
              </button>
            ) : (
              <button className={s.secondaryBtn} onClick={archiveWorkflow} disabled={running} title="Hide it from the list; runs and versions are kept">
                <Icon name="Archive" size={14} /> Archive
              </button>
            ))}
            {isOwner && (
              <button className={s.dangerBtn} onClick={deleteWorkflow} disabled={running} title="Delete this workflow" aria-label="Delete workflow">
                <Icon name="Trash2" size={14} />
              </button>
            )}
          </div>
        </div>

        {archived && (
          <div className={s.noticeBar}>
            <Icon name="Archive" size={14} /> This workflow is archived: it cannot be run or edited until it is unarchived. Its runs and versions are kept.
          </div>
        )}

        {versionsOpen && (
          <WorkflowVersionsDrawer
            versions={versions}
            current={workflow.version}
            currentSteps={workflow.steps}
            canRestore={isOwner && !archived && !running}
            loadSteps={loadVersionSteps}
            onRestore={restoreVersion}
            onClose={toggleVersions}
            styles={s}
          />
        )}

        {editorOpen ? (
          <div className={s.panel}>
            <div className={s.panelHead}>
              <span className={s.panelTitle}>Steps, in order</span>
              <span className={s.panelHint}>
                {validating ? 'Checking…' : draftClean ? 'No problems found.' : validationIssues.length ? `${validationIssues.length} problem${validationIssues.length === 1 ? '' : 's'} to fix.` : 'Saving records a new version; past runs keep the steps they ran with.'}
              </span>
            </div>
            <WorkflowStepList
              steps={draftSteps}
              onChange={changeDraft}
              issues={validationIssues}
              documents={pickerDocs}
              documentsLoading={pickerLoading}
              knownRefs={refs}
              members={approverCandidates}
              styles={s}
            />
            <div className={s.panelActions}>
              <input
                className={`${s.input} ${s.inputWide}`}
                placeholder="What changed and why (kept on the version, optional)"
                value={changeNote}
                onChange={(e) => setChangeNote(e.target.value)}
                aria-label="Change note"
              />
              <button type="button" className={s.primaryBtn} onClick={submitSteps} disabled={validating || validationIssues.length > 0}>
                <Icon name="Check" size={14} /> Save as v{workflow.version + 1}
              </button>
              <button type="button" className={s.secondaryBtn} onClick={() => setEditorOpen(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <ol className={s.stepSummary}>
            {workflow.steps.map((step, i) => (
              <li key={step.name} className={s.stepSummaryItem} title={step.condition ? `Only when ${conditionSummary(step.condition)}` : undefined}>
                <span className={s.stepIndex}>{i + 1}</span>
                <span className={s.stepSummaryName}>{step.name}</span>
                <span className={s.muted}>{step.kind.replace(/_/g, ' ')}</span>
                {step.condition && <Icon name="GitBranch" size={11} className={s.muted} />}
                {step.inputs.source === 'documents' && (
                  <span className={s.muted}>· {step.inputs.document_ids.map((d) => documentName(d, refs)).join(', ')}</span>
                )}
              </li>
            ))}
          </ol>
        )}

        <WorkflowRunBar
          stepCount={workflow.steps.length}
          matterName={matterName}
          running={running}
          progress={progress}
          runError={runError}
          canRun={workflow.steps.length > 0 && !archived}
          onPickDocuments={openPicker}
          onRunOnMatter={() => run([])}
          onCancel={cancel}
          styles={s}
        />

        <div className={s.runWorkspace}>
          <div className={s.runHistory}>
            <div className={s.sectionLabel}>Runs</div>
            <WorkflowRunList
              runs={runs}
              selectedRunId={selectedRunId}
              running={running}
              onSelect={(runId) => void selectRun(runId)}
              onResume={resume}
              styles={s}
            />
          </div>
          <div className={s.runDetail}>
            {selectedRun ? (
              <>
                <div className={s.runDetailHead}>
                  <div className={s.sectionLabel}>
                    Run of {new Date(selectedRun.created_at).toLocaleString()} · {selectedRun.status.replace(/_/g, ' ')} · v{selectedRun.workflow_version}
                    {selectedRun.documents ? ` · ${selectedRun.documents} document${selectedRun.documents === 1 ? '' : 's'}` : ''}
                  </div>
                  <div className={s.runDetailTools}>
                    <button type="button" className={`${s.toolBtn} ${compareOpen ? s.secondaryBtnActive : ''}`} onClick={() => void toggleCompare()} disabled={runs.length < 2} title="Per-step changes against the previous run">
                      <Icon name="Columns2" size={12} /> Compare with previous
                    </button>
                    {running && selectedRun.job_id && (
                      <button type="button" className={s.toolBtn} onClick={cancel}><Icon name="X" size={12} /> Cancel run</button>
                    )}
                  </div>
                </div>
                {selectedRun.input_document_ids.length > 0 && (
                  <div className={s.runDocs}>
                    <Icon name="FileText" size={12} />
                    {selectedRun.input_document_ids.map((d) => documentName(d, refs)).join(', ')}
                  </div>
                )}
                {selectedRun.status === 'awaiting_approval' && (
                  <WorkflowApprovalBanner run={selectedRun} busy={running} onApprove={(note) => void approve(note)} onReject={(note) => void reject(note)} styles={s} />
                )}
                {selectedRun.error && <div className={s.runErrorBox}><Icon name="AlertTriangle" size={14} /> {selectedRun.error}</div>}
                {compareOpen && <WorkflowRunCompare current={selectedRun} previous={previousRun} styles={s} />}
                {selectedRun.step_runs.map((step) => (
                  <WorkflowStepRunCard
                    key={step.index}
                    step={step}
                    definition={selectedRun.step_definitions[step.index]}
                    canControl={canControlRun}
                    controllable={runControllable}
                    exportingDraft={exportingDraft}
                    onRetry={(index) => void retryStep(index)}
                    onSkip={(index) => void skipStep(index)}
                    onDownloadDraft={(title, text) => void downloadDraft(title, text)}
                    styles={s}
                  />
                ))}
              </>
            ) : (
              <div className={s.empty}>
                <Icon name="Workflow" size={18} />
                <p>Run the workflow to see each step&apos;s outcome here.</p>
              </div>
            )}
          </div>
        </div>

        <ReviewDocumentPicker
          isOpen={pickerOpen}
          documents={pickerDocs}
          loading={pickerLoading}
          selectedIds={[]}
          scopeLabel={activeMatter
            ? `Documents filed under "${activeMatter.is_personal ? 'Personal' : activeMatter.name}"`
            : 'Every indexed document you can read. Set an active matter to narrow this list.'}
          onConfirm={async (ids) => { await run(ids); return true; }}
          onClose={closePicker}
          styles={s}
        />
      </div>
    </div>
  );
}

export default WorkflowView;
