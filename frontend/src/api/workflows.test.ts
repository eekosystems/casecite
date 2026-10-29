import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client', () => {
  const api = {
    request: vi.fn(),
    authFetch: vi.fn(),
    getToken: vi.fn().mockReturnValue('test-token'),
  };
  return { api, API_BASE_URL: 'http://localhost:8000/api/v1' };
});

import { api } from './client';
import './workflows';

const a = api as any;

describe('api/workflows', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.request as any).mockResolvedValue({});
  });

  it('registers the workflow methods on the api object', () => {
    for (const name of [
      'listWorkflows', 'createWorkflow', 'getWorkflow', 'updateWorkflow', 'deleteWorkflow',
      'listWorkflowTemplates', 'createWorkflowFromTemplate', 'runWorkflow', 'listWorkflowRuns',
      'getWorkflowRun', 'resumeWorkflowRun', 'getWorkflowJob', 'cancelWorkflowJob',
    ]) {
      expect(typeof a[name]).toBe('function');
    }
  });

  it('createWorkflow normalises optional fields to null', async () => {
    const steps = [{ name: 'review', kind: 'analyze_contract', params: {}, inputs: { source: 'run', document_ids: [], step: null } }];
    await a.createWorkflow({ name: 'Intake', steps });
    expect(api.request).toHaveBeenCalledWith('/workflows', {
      method: 'POST',
      body: JSON.stringify({ name: 'Intake', description: null, matter_id: null, steps }),
    });
  });

  it('updateWorkflow PATCHes only the given changes', async () => {
    await a.updateWorkflow('w1', { name: 'Renamed' });
    expect(api.request).toHaveBeenCalledWith('/workflows/w1', { method: 'PATCH', body: JSON.stringify({ name: 'Renamed' }) });
  });

  it('createWorkflowFromTemplate passes the matter as a query parameter only when given', async () => {
    await a.createWorkflowFromTemplate('nda_intake');
    expect(api.request).toHaveBeenCalledWith('/workflows/from-template/nda_intake', { method: 'POST' });
    await a.createWorkflowFromTemplate('nda_intake', 'm 1');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/from-template/nda_intake?matter_id=m%201', { method: 'POST' });
  });

  it('runWorkflow posts the document ids (an empty list for the matter default)', async () => {
    await a.runWorkflow('w1');
    expect(api.request).toHaveBeenCalledWith('/workflows/w1/run', { method: 'POST', body: JSON.stringify({ document_ids: [] }) });
    await a.runWorkflow('w1', ['d1', 'd2']);
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/run', {
      method: 'POST',
      body: JSON.stringify({ document_ids: ['d1', 'd2'] }),
    });
  });

  it('run endpoints encode the ids', async () => {
    await a.listWorkflowRuns('w/1', 10);
    expect(api.request).toHaveBeenCalledWith('/workflows/w%2F1/runs?limit=10');
    await a.getWorkflowRun('w1', 'r/1');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/runs/r%2F1');
    await a.resumeWorkflowRun('w1', 'r1');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/runs/r1/resume', { method: 'POST' });
  });

  it('job helpers read and cancel the shared jobs endpoint', async () => {
    await a.getWorkflowJob('j1');
    expect(api.request).toHaveBeenCalledWith('/jobs/j1');
    await a.cancelWorkflowJob('j1');
    expect(api.request).toHaveBeenLastCalledWith('/jobs/j1', { method: 'DELETE' });
  });
  it('validate, recent runs, duplicate, archive and versions hit their routes', async () => {
    const step = { name: 'x', kind: 'approval', params: {}, inputs: { source: 'run', document_ids: [], step: null } };
    await a.validateWorkflowSteps([step]);
    expect(api.request).toHaveBeenLastCalledWith('/workflows/validate', { method: 'POST', body: JSON.stringify({ steps: [step] }) });
    await a.listRecentWorkflowRuns('m1', 5);
    expect(api.request).toHaveBeenLastCalledWith('/workflows/runs?limit=5&matter_id=m1');
    await a.listRecentWorkflowRuns();
    expect(api.request).toHaveBeenLastCalledWith('/workflows/runs?limit=20');
    await a.listWorkflows(true);
    expect(api.request).toHaveBeenLastCalledWith('/workflows?include_archived=true');
    await a.duplicateWorkflow('w1', { name: 'Copy' });
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/duplicate', { method: 'POST', body: JSON.stringify({ name: 'Copy', matter_id: null }) });
    await a.archiveWorkflow('w1');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/archive', { method: 'POST' });
    await a.unarchiveWorkflow('w1');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/unarchive', { method: 'POST' });
    await a.listWorkflowVersions('w1');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/versions');
    await a.getWorkflowVersion('w1', 2);
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/versions/2');
    await a.restoreWorkflowVersion('w1', 2, 'back');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/versions/2/restore', { method: 'POST', body: JSON.stringify({ change_note: 'back' }) });
  });

  it('approve, reject, retry and skip post to the run', async () => {
    await a.approveWorkflowRun('w1', 'r1', 'ok');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/runs/r1/approve', { method: 'POST', body: JSON.stringify({ note: 'ok' }) });
    await a.rejectWorkflowRun('w1', 'r1');
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/runs/r1/reject', { method: 'POST', body: JSON.stringify({ note: null }) });
    await a.retryWorkflowStep('w1', 'r1', 2);
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/runs/r1/steps/2/retry', { method: 'POST' });
    await a.skipWorkflowStep('w1', 'r1', 2);
    expect(api.request).toHaveBeenLastCalledWith('/workflows/w1/runs/r1/steps/2/skip', { method: 'POST' });
  });
});
