import { api } from './client';
import type {
  WorkflowCreateInput,
  WorkflowDetail,
  WorkflowJob,
  WorkflowRunDetail,
  WorkflowRunStarted,
  WorkflowRunSummary,
  WorkflowStep,
  WorkflowSummary,
  WorkflowTemplate,
  WorkflowUpdateInput,
  WorkflowValidateResponse,
  WorkflowVersionDetail,
  WorkflowVersionList,
} from './types';

const workflowPath = (workflowId: string, suffix = '') => `/workflows/${encodeURIComponent(workflowId)}${suffix}`;
const runPath = (workflowId: string, runId: string, suffix = '') =>
  workflowPath(workflowId, `/runs/${encodeURIComponent(runId)}${suffix}`);

Object.assign(api, {
  async listWorkflows(includeArchived = false): Promise<{ workflows: WorkflowSummary[] }> {
    return api.request(includeArchived ? '/workflows?include_archived=true' : '/workflows');
  },

  async createWorkflow(input: WorkflowCreateInput): Promise<WorkflowDetail> {
    return api.request('/workflows', {
      method: 'POST',
      body: JSON.stringify({
        name: input.name,
        description: input.description || null,
        matter_id: input.matter_id || null,
        steps: input.steps,
      }),
    });
  },

  async getWorkflow(workflowId: string): Promise<WorkflowDetail> {
    return api.request(workflowPath(workflowId));
  },

  async updateWorkflow(workflowId: string, changes: WorkflowUpdateInput): Promise<WorkflowDetail> {
    return api.request(workflowPath(workflowId), { method: 'PATCH', body: JSON.stringify(changes) });
  },

  async deleteWorkflow(workflowId: string): Promise<{ status: string; id: string }> {
    return api.request(workflowPath(workflowId), { method: 'DELETE' });
  },

  async validateWorkflowSteps(steps: WorkflowStep[]): Promise<WorkflowValidateResponse> {
    return api.request('/workflows/validate', { method: 'POST', body: JSON.stringify({ steps }) });
  },

  async listWorkflowTemplates(): Promise<{ templates: WorkflowTemplate[] }> {
    return api.request('/workflows/templates');
  },

  async createWorkflowFromTemplate(key: string, matterId?: string | null): Promise<WorkflowDetail> {
    const query = matterId ? `?matter_id=${encodeURIComponent(matterId)}` : '';
    return api.request(`/workflows/from-template/${encodeURIComponent(key)}${query}`, { method: 'POST' });
  },

  async listRecentWorkflowRuns(matterId?: string | null, limit = 20): Promise<{ runs: WorkflowRunSummary[] }> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (matterId) params.set('matter_id', matterId);
    return api.request(`/workflows/runs?${params.toString()}`);
  },

  async duplicateWorkflow(
    workflowId: string,
    input: { name?: string | null; matter_id?: string | null } = {},
  ): Promise<WorkflowDetail> {
    return api.request(workflowPath(workflowId, '/duplicate'), {
      method: 'POST',
      body: JSON.stringify({ name: input.name || null, matter_id: input.matter_id || null }),
    });
  },

  async archiveWorkflow(workflowId: string): Promise<WorkflowDetail> {
    return api.request(workflowPath(workflowId, '/archive'), { method: 'POST' });
  },

  async unarchiveWorkflow(workflowId: string): Promise<WorkflowDetail> {
    return api.request(workflowPath(workflowId, '/unarchive'), { method: 'POST' });
  },

  async listWorkflowVersions(workflowId: string): Promise<WorkflowVersionList> {
    return api.request(workflowPath(workflowId, '/versions'));
  },

  async getWorkflowVersion(workflowId: string, version: number): Promise<WorkflowVersionDetail> {
    return api.request(workflowPath(workflowId, `/versions/${version}`));
  },

  async restoreWorkflowVersion(workflowId: string, version: number, changeNote?: string | null): Promise<WorkflowDetail> {
    return api.request(workflowPath(workflowId, `/versions/${version}/restore`), {
      method: 'POST',
      body: JSON.stringify({ change_note: changeNote || null }),
    });
  },

  async runWorkflow(workflowId: string, documentIds: string[] = []): Promise<WorkflowRunStarted> {
    return api.request(workflowPath(workflowId, '/run'), {
      method: 'POST',
      body: JSON.stringify({ document_ids: documentIds }),
    });
  },

  async listWorkflowRuns(workflowId: string, limit = 50): Promise<{ runs: WorkflowRunSummary[] }> {
    return api.request(workflowPath(workflowId, `/runs?limit=${limit}`));
  },

  async getWorkflowRun(workflowId: string, runId: string): Promise<WorkflowRunDetail> {
    return api.request(runPath(workflowId, runId));
  },

  async resumeWorkflowRun(workflowId: string, runId: string): Promise<WorkflowRunStarted> {
    return api.request(runPath(workflowId, runId, '/resume'), { method: 'POST' });
  },

  async approveWorkflowRun(workflowId: string, runId: string, note?: string | null): Promise<WorkflowRunStarted> {
    return api.request(runPath(workflowId, runId, '/approve'), {
      method: 'POST',
      body: JSON.stringify({ note: note || null }),
    });
  },

  async rejectWorkflowRun(workflowId: string, runId: string, note?: string | null): Promise<WorkflowRunDetail> {
    return api.request(runPath(workflowId, runId, '/reject'), {
      method: 'POST',
      body: JSON.stringify({ note: note || null }),
    });
  },

  async retryWorkflowStep(workflowId: string, runId: string, index: number): Promise<WorkflowRunStarted> {
    return api.request(runPath(workflowId, runId, `/steps/${index}/retry`), { method: 'POST' });
  },

  async skipWorkflowStep(workflowId: string, runId: string, index: number): Promise<WorkflowRunStarted> {
    return api.request(runPath(workflowId, runId, `/steps/${index}/skip`), { method: 'POST' });
  },

  async getWorkflowJob(jobId: string): Promise<WorkflowJob> {
    return api.request(`/jobs/${jobId}`);
  },

  async cancelWorkflowJob(jobId: string): Promise<{ status: string; job_id: string }> {
    return api.request(`/jobs/${jobId}`, { method: 'DELETE' });
  },
});
