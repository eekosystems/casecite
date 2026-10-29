import { api, API_BASE_URL } from './client';
import type {
  ReviewCell,
  ReviewCellRef,
  ReviewCellReviewInput,
  ReviewDocumentText,
  ReviewExportFormat,
  ReviewExportOptions,
  ReviewRun,
  ReviewSchemaCreateInput,
  ReviewSchemaTemplate,
  ReviewTableCreateInput,
  ReviewTableDetail,
  ReviewTableDocumentsAdded,
  ReviewTableEstimate,
  ReviewTableJob,
  ReviewTableRunStarted,
  ReviewTableSummary,
  ReviewTableUpdateInput,
} from './types';

const tablePath = (tableId: string, suffix = '') => `/review-tables/${encodeURIComponent(tableId)}${suffix}`;
const enc = encodeURIComponent;

/** The query string for an export, from its options (defaults are omitted). */
export function exportQuery(options: ReviewExportOptions): string {
  const q = new URLSearchParams();
  q.set('format', options.format);
  if (options.fields && options.fields.length) q.set('fields', options.fields.join(','));
  if (options.reviewed_only) q.set('reviewed_only', 'true');
  if (options.include_quotes === false) q.set('include_quotes', 'false');
  if (options.statuses && options.statuses.length) q.set('status', options.statuses.join(','));
  return q.toString();
}

Object.assign(api, {
  async listReviewTables(): Promise<{ tables: ReviewTableSummary[] }> {
    return api.request('/review-tables');
  },

  async createReviewTable(input: ReviewTableCreateInput): Promise<ReviewTableDetail> {
    return api.request('/review-tables', {
      method: 'POST',
      body: JSON.stringify({
        name: input.name,
        description: input.description || null,
        matter_id: input.matter_id || null,
        fields: input.fields,
        document_ids: input.document_ids || [],
      }),
    });
  },

  async getReviewTable(tableId: string): Promise<ReviewTableDetail> {
    return api.request(tablePath(tableId));
  },

  async updateReviewTable(tableId: string, changes: ReviewTableUpdateInput): Promise<ReviewTableDetail> {
    return api.request(tablePath(tableId), { method: 'PATCH', body: JSON.stringify(changes) });
  },

  async deleteReviewTable(tableId: string): Promise<{ status: string; id: string }> {
    return api.request(tablePath(tableId), { method: 'DELETE' });
  },

  async estimateReviewTable(tableId: string): Promise<ReviewTableEstimate> {
    return api.request(tablePath(tableId, '/estimate'));
  },

  async runReviewTable(
    tableId: string,
    scope: { document_ids?: string[]; field_keys?: string[] } = {},
  ): Promise<ReviewTableRunStarted> {
    return api.request(tablePath(tableId, '/run'), { method: 'POST', body: JSON.stringify(scope) });
  },

  async getReviewTableJob(jobId: string): Promise<ReviewTableJob> {
    return api.request(`/jobs/${jobId}`);
  },

  /** The cited export as a file: an .xlsx workbook, a flat CSV or JSON, narrowed by the options. */
  async exportReviewTable(tableId: string, options: ReviewExportOptions | ReviewExportFormat): Promise<Blob> {
    const opts = typeof options === 'string' ? { format: options } : options;
    const resp = await api.authFetch(`${API_BASE_URL}${tablePath(tableId, `/export?${exportQuery(opts)}`)}`);
    if (!resp.ok) throw new Error(`Export failed (${resp.status})`);
    return resp.blob();
  },

  // ── Review layer ──

  async reviewCell(
    tableId: string, documentId: string, fieldKey: string, change: ReviewCellReviewInput,
  ): Promise<ReviewCell> {
    return api.request(tablePath(tableId, `/cells/${enc(documentId)}/${enc(fieldKey)}`), {
      method: 'PATCH',
      body: JSON.stringify(change),
    });
  },

  async bulkReviewCells(
    tableId: string, cells: ReviewCellRef[], change: ReviewCellReviewInput,
  ): Promise<{ updated: number; cells: ReviewCell[] }> {
    return api.request(tablePath(tableId, '/cells/bulk'), {
      method: 'POST',
      body: JSON.stringify({ cells, ...change }),
    });
  },

  // ── Saved schemas ──

  async listReviewSchemas(): Promise<{ schemas: ReviewSchemaTemplate[] }> {
    return api.request('/review-tables/schemas');
  },

  async createReviewSchema(input: ReviewSchemaCreateInput): Promise<ReviewSchemaTemplate> {
    return api.request('/review-tables/schemas', {
      method: 'POST',
      body: JSON.stringify({
        name: input.name,
        description: input.description || null,
        matter_id: input.matter_id || null,
        fields: input.fields,
      }),
    });
  },

  async updateReviewSchema(schemaId: string, changes: Partial<ReviewSchemaCreateInput>): Promise<ReviewSchemaTemplate> {
    return api.request(`/review-tables/schemas/${enc(schemaId)}`, { method: 'PATCH', body: JSON.stringify(changes) });
  },

  async deleteReviewSchema(schemaId: string): Promise<{ status: string; id: string }> {
    return api.request(`/review-tables/schemas/${enc(schemaId)}`, { method: 'DELETE' });
  },

  // ── Documents, text, runs ──

  async addReviewDocuments(tableId: string, documentIds: string[], run = false): Promise<ReviewTableDocumentsAdded> {
    return api.request(tablePath(tableId, '/documents'), {
      method: 'POST',
      body: JSON.stringify({ document_ids: documentIds, run }),
    });
  },

  async removeReviewDocument(tableId: string, documentId: string): Promise<ReviewTableDetail> {
    return api.request(tablePath(tableId, `/documents/${enc(documentId)}`), { method: 'DELETE' });
  },

  async getReviewDocumentText(tableId: string, documentId: string): Promise<ReviewDocumentText> {
    return api.request(tablePath(tableId, `/documents/${enc(documentId)}/text`));
  },

  async listReviewRuns(tableId: string, limit = 50): Promise<{ runs: ReviewRun[] }> {
    return api.request(tablePath(tableId, `/runs?limit=${limit}`));
  },
});
