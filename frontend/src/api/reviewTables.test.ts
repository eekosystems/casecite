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
import { exportQuery } from './reviewTables';

const a = api as any;

describe('api/reviewTables', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.request as any).mockResolvedValue({});
  });

  it('registers the review-table methods on the api object', () => {
    for (const name of [
      'listReviewTables', 'createReviewTable', 'getReviewTable', 'updateReviewTable', 'deleteReviewTable',
      'estimateReviewTable', 'runReviewTable', 'getReviewTableJob', 'exportReviewTable',
      'reviewCell', 'bulkReviewCells', 'listReviewSchemas', 'createReviewSchema', 'updateReviewSchema', 'deleteReviewSchema',
      'addReviewDocuments', 'removeReviewDocument', 'getReviewDocumentText', 'listReviewRuns',
    ]) {
      expect(typeof a[name]).toBe('function');
    }
  });

  it('createReviewTable normalises optional fields to null / empty', async () => {
    await a.createReviewTable({ name: 'NDA sweep', fields: [{ name: 'Term', type: 'duration' }] });
    expect(api.request).toHaveBeenCalledWith('/review-tables', {
      method: 'POST',
      body: JSON.stringify({
        name: 'NDA sweep',
        description: null,
        matter_id: null,
        fields: [{ name: 'Term', type: 'duration' }],
        document_ids: [],
      }),
    });
  });

  it('updateReviewTable PATCHes only the given changes', async () => {
    await a.updateReviewTable('t1', { document_ids: ['d1', 'd2'] });
    expect(api.request).toHaveBeenCalledWith('/review-tables/t1', {
      method: 'PATCH',
      body: JSON.stringify({ document_ids: ['d1', 'd2'] }),
    });
  });

  it('runReviewTable posts the scope (or an empty object for a full run)', async () => {
    await a.runReviewTable('t1');
    expect(api.request).toHaveBeenCalledWith('/review-tables/t1/run', { method: 'POST', body: '{}' });
    await a.runReviewTable('t1', { document_ids: ['d1'], field_keys: ['term'] });
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/t1/run', {
      method: 'POST',
      body: JSON.stringify({ document_ids: ['d1'], field_keys: ['term'] }),
    });
  });

  it('getReviewTableJob polls the shared jobs endpoint', async () => {
    await a.getReviewTableJob('job-9');
    expect(api.request).toHaveBeenCalledWith('/jobs/job-9');
  });

  it('encodes ids in paths', async () => {
    await a.getReviewTable('a/b');
    expect(api.request).toHaveBeenCalledWith('/review-tables/a%2Fb');
    await a.estimateReviewTable('t1');
    expect(api.request).toHaveBeenCalledWith('/review-tables/t1/estimate');
    await a.deleteReviewTable('t1');
    expect(api.request).toHaveBeenCalledWith('/review-tables/t1', { method: 'DELETE' });
  });

  it('exportReviewTable fetches the file with auth and returns the blob', async () => {
    const blob = new Blob(['x']);
    (api.authFetch as any).mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    await expect(a.exportReviewTable('t1', 'csv')).resolves.toBe(blob);
    expect(api.authFetch).toHaveBeenCalledWith('http://localhost:8000/api/v1/review-tables/t1/export?format=csv');

    (api.authFetch as any).mockResolvedValue({ ok: false, status: 404 });
    await expect(a.exportReviewTable('t1', 'xlsx')).rejects.toThrow('Export failed (404)');
  });

  it('exportQuery omits the defaults and joins the lists', () => {
    expect(exportQuery({ format: 'xlsx' })).toBe('format=xlsx');
    expect(exportQuery({ format: 'csv', fields: ['a', 'b'], reviewed_only: true, include_quotes: false, statuses: ['flagged'] }))
      .toBe('format=csv&fields=a%2Cb&reviewed_only=true&include_quotes=false&status=flagged');
    expect(exportQuery({ format: 'json', fields: [], include_quotes: true, statuses: [] })).toBe('format=json');
  });

  it('exportReviewTable accepts a bare format or full options', async () => {
    (api.authFetch as any).mockResolvedValue({ ok: true, blob: async () => new Blob(['x']) });
    await a.exportReviewTable('t1', 'csv');
    expect(api.authFetch).toHaveBeenLastCalledWith('http://localhost:8000/api/v1/review-tables/t1/export?format=csv');
    await a.exportReviewTable('t1', { format: 'xlsx', reviewed_only: true });
    expect(api.authFetch).toHaveBeenLastCalledWith('http://localhost:8000/api/v1/review-tables/t1/export?format=xlsx&reviewed_only=true');
    (api.authFetch as any).mockResolvedValue({ ok: false, status: 404 });
    await expect(a.exportReviewTable('t1', 'json')).rejects.toThrow('Export failed (404)');
  });

  it('reviewCell PATCHes the cell path and bulkReviewCells posts refs with the change', async () => {
    await a.reviewCell('t1', 'd 1', 'governing_law', { review_status: 'approved' });
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/t1/cells/d%201/governing_law', {
      method: 'PATCH', body: JSON.stringify({ review_status: 'approved' }),
    });
    await a.bulkReviewCells('t1', [{ document_id: 'd1', field_key: 'term' }], { note: 'x' });
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/t1/cells/bulk', {
      method: 'POST', body: JSON.stringify({ cells: [{ document_id: 'd1', field_key: 'term' }], note: 'x' }),
    });
  });

  it('schema, document and run methods hit their routes', async () => {
    await a.listReviewSchemas();
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/schemas');
    await a.createReviewSchema({ name: 'NDA', fields: [{ name: 'Term', type: 'duration' }] });
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/schemas', {
      method: 'POST', body: JSON.stringify({ name: 'NDA', description: null, matter_id: null, fields: [{ name: 'Term', type: 'duration' }] }),
    });
    await a.updateReviewSchema('s1', { name: 'Renamed' });
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/schemas/s1', { method: 'PATCH', body: JSON.stringify({ name: 'Renamed' }) });
    await a.deleteReviewSchema('s1');
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/schemas/s1', { method: 'DELETE' });
    await a.addReviewDocuments('t1', ['d2'], true);
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/t1/documents', { method: 'POST', body: JSON.stringify({ document_ids: ['d2'], run: true }) });
    await a.removeReviewDocument('t1', 'd2');
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/t1/documents/d2', { method: 'DELETE' });
    await a.getReviewDocumentText('t1', 'd2');
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/t1/documents/d2/text');
    await a.listReviewRuns('t1', 10);
    expect(api.request).toHaveBeenLastCalledWith('/review-tables/t1/runs?limit=10');
  });
});
