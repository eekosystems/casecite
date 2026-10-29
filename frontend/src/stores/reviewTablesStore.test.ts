import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../api', () => ({
  api: {
    listReviewTables: vi.fn(),
    createReviewTable: vi.fn(),
    getReviewTable: vi.fn(),
    updateReviewTable: vi.fn(),
    deleteReviewTable: vi.fn(),
    estimateReviewTable: vi.fn(),
    runReviewTable: vi.fn(),
    getReviewTableJob: vi.fn(),
    exportReviewTable: vi.fn(),
    reviewCell: vi.fn(),
    bulkReviewCells: vi.fn(),
    listReviewSchemas: vi.fn().mockResolvedValue({ schemas: [] }),
    createReviewSchema: vi.fn(),
    deleteReviewSchema: vi.fn(),
    addReviewDocuments: vi.fn(),
    removeReviewDocument: vi.fn(),
    getReviewDocumentText: vi.fn(),
    listReviewRuns: vi.fn().mockResolvedValue({ runs: [] }),
    getMatterDocuments: vi.fn(),
    getAllDocuments: vi.fn(),
    listMatters: vi.fn().mockResolvedValue({ matters: [] }),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

const navigate = vi.fn();
vi.mock('../utils/router', () => ({ appNavigate: (to: string) => navigate(to) }));

import { useReviewTablesStore, indexCells, cellKey, describeRun, visibleDocuments } from './reviewTablesStore';
import { useMattersStore } from './mattersStore';
import { useUIStore } from './uiStore';
import { api } from '../api';

const summary = {
  id: 't1', name: 'NDA sweep', description: null, matter_id: null, owner_id: 'me', role: 'owner',
  field_count: 2, document_count: 1, cell_count: 0, approved_count: 0, flagged_count: 0, last_run_at: null,
  created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
};
const review = {
  override_value: null, effective_value: null as string | null, review_status: 'pending', note: null,
  reviewed_by: null, reviewed_at: null, history: [] as { at: string; by: string; kind: string; value?: string | null }[],
};
const fields = [
  { key: 'governing_law', name: 'Governing law', description: '', type: 'text' as const },
  { key: 'term', name: 'Term', description: '', type: 'duration' as const },
];
const detail = {
  ...summary,
  fields,
  documents: [{ id: 'd1', filename: 'a.pdf', accessible: true }],
  cells: [
    {
      document_id: 'd1', field_key: 'governing_law', value: 'Delaware', quote: 'laws of Delaware',
      span_start: 10, span_end: 26, verified: true, status: 'extracted', error: null,
      model_used: 'gpt', updated_at: '2026-10-02T00:00:00Z', ...review, effective_value: 'Delaware',
    },
  ],
};
const estimate = { documents: 1, fields: 2, cells: 2, llm_calls: 1, approx_input_tokens: 1200, model: 'gpt' };

/** Settle pending promises and zero-delay timers under the fake clock. */
const flush = () => vi.advanceTimersByTimeAsync(0);

describe('reviewTablesStore', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useReviewTablesStore.setState(useReviewTablesStore.getInitialState(), true);
    useMattersStore.setState(useMattersStore.getInitialState(), true);
    useUIStore.setState(useUIStore.getInitialState(), true);
    vi.clearAllMocks();
    (api.listReviewTables as any).mockResolvedValue({ tables: [summary] });
    (api.getReviewTable as any).mockResolvedValue(detail);
    (api.estimateReviewTable as any).mockResolvedValue(estimate);
    (api.listReviewSchemas as any).mockResolvedValue({ schemas: [] });
    (api.listReviewRuns as any).mockResolvedValue({ runs: [] });
    localStorage.clear();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  // ==================== helpers ====================

  it('indexCells keys cells by document and field', () => {
    const map = indexCells(detail.cells);
    expect(map.get(cellKey('d1', 'governing_law'))?.value).toBe('Delaware');
    expect(map.get(cellKey('d1', 'term'))).toBeUndefined();
  });

  it('describeRun summarises a finished run', () => {
    expect(describeRun(null)).toBe('Review finished.');
    expect(describeRun({ documents: 2, cells: 4, extracted: 2, unverified: 1, errors: 1 })).toBe(
      'Review finished: 4 cells, 2 verified, 1 unverified, 1 failed.',
    );
  });

  // ==================== list / create ====================

  it('init loads the tables once', async () => {
    useReviewTablesStore.getState().init();
    useReviewTablesStore.getState().init();
    await flush();
    expect(api.listReviewTables).toHaveBeenCalledTimes(1);
    expect(useReviewTablesStore.getState().tables).toEqual([summary]);
  });

  it('createTable sends the active matter, trims fields, prepends the summary and returns the table', async () => {
    useMattersStore.setState({ activeMatterId: 'm1' });
    (api.createReviewTable as any).mockResolvedValue(detail);
    const created = await useReviewTablesStore.getState().createTable({
      name: '  NDA sweep ',
      description: '',
      fields: [{ name: ' Governing law ', description: ' state ', type: 'text' }, { name: '   ', type: 'text' }],
      documentIds: [],
    });
    expect(created).toEqual(detail);
    expect(api.createReviewTable).toHaveBeenCalledWith({
      name: 'NDA sweep',
      description: null,
      matter_id: 'm1',
      fields: [{ name: 'Governing law', description: 'state', type: 'text' }],
      document_ids: [],
    });
    expect(useReviewTablesStore.getState().tables[0].id).toBe('t1');
  });

  it('createTable refuses an empty name or schema without calling the API', async () => {
    expect(await useReviewTablesStore.getState().createTable({ name: '', description: '', fields, documentIds: [] })).toBeNull();
    expect(await useReviewTablesStore.getState().createTable({ name: 'x', description: '', fields: [], documentIds: [] })).toBeNull();
    expect(api.createReviewTable).not.toHaveBeenCalled();
    expect(useUIStore.getState().toasts.length).toBe(2);
  });

  it('openTable navigates to the table route', () => {
    useReviewTablesStore.getState().openTable('t1');
    expect(navigate).toHaveBeenCalledWith('/review-tables/t1');
  });

  // ==================== one table ====================

  it('loadTable fetches the detail and the estimate, opening the schema editor when there are no fields', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    await flush();
    const st = useReviewTablesStore.getState();
    expect(st.table).toEqual(detail);
    expect(st.estimate).toEqual(estimate);
    expect(st.schemaOpen).toBe(false);

    (api.getReviewTable as any).mockResolvedValue({ ...detail, id: 't2', fields: [], cells: [] });
    await useReviewTablesStore.getState().loadTable('t2');
    expect(useReviewTablesStore.getState().schemaOpen).toBe(true);
  });

  it('loadTable records the error for an unreadable table', async () => {
    (api.getReviewTable as any).mockRejectedValue(new Error('Review table not found'));
    await useReviewTablesStore.getState().loadTable('nope');
    expect(useReviewTablesStore.getState().tableError).toBe('Review table not found');
    expect(useReviewTablesStore.getState().table).toBeNull();
  });

  it('saveDocuments PATCHes the scope, replaces the table and closes the picker', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    useReviewTablesStore.setState({ pickerOpen: true, tables: [summary] });
    const updated = { ...detail, documents: [...detail.documents, { id: 'd2', filename: 'b.pdf', accessible: true }], document_count: 2 };
    (api.updateReviewTable as any).mockResolvedValue(updated);

    expect(await useReviewTablesStore.getState().saveDocuments(['d1', 'd2'])).toBe(true);
    expect(api.updateReviewTable).toHaveBeenCalledWith('t1', { document_ids: ['d1', 'd2'] });
    const st = useReviewTablesStore.getState();
    expect(st.table?.documents).toHaveLength(2);
    expect(st.pickerOpen).toBe(false);
    expect(st.tables.find((t) => t.id === 't1')?.document_count).toBe(2);
  });

  it('saveFields asks before dropping a column that already has values', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.updateReviewTable as any).mockResolvedValue({ ...detail, fields: [fields[1]], cells: [] });

    const pending = useReviewTablesStore.getState().saveFields([fields[1]]);
    await flush();
    const confirm = useUIStore.getState().confirmModal;
    expect(confirm?.open).toBe(true);
    expect(api.updateReviewTable).not.toHaveBeenCalled();
    confirm?.onConfirm?.();
    expect(await pending).toBe(true);
    expect(api.updateReviewTable).toHaveBeenCalledWith('t1', {
      fields: [{ key: 'term', name: 'Term', description: '', type: 'duration' }],
    });
  });

  it('saveFields saves straight away when no extracted column is dropped', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    const added = [...fields, { name: 'Renewal', description: '', type: 'boolean' as const }];
    (api.updateReviewTable as any).mockResolvedValue({ ...detail, fields: added });
    expect(await useReviewTablesStore.getState().saveFields(added)).toBe(true);
    expect(useUIStore.getState().confirmModal?.open).not.toBe(true);
    expect(api.updateReviewTable).toHaveBeenCalledTimes(1);
  });

  it('deleteTable confirms, deletes, drops the table from the list and navigates back', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    useReviewTablesStore.setState({ tables: [summary] });
    (api.deleteReviewTable as any).mockResolvedValue({ status: 'deleted', id: 't1' });

    useReviewTablesStore.getState().deleteTable();
    const confirm = useUIStore.getState().confirmModal;
    expect(confirm?.open).toBe(true);
    await confirm?.onConfirm?.();
    await flush();
    expect(api.deleteReviewTable).toHaveBeenCalledWith('t1');
    expect(useReviewTablesStore.getState().tables).toEqual([]);
    expect(useReviewTablesStore.getState().table).toBeNull();
    expect(navigate).toHaveBeenCalledWith('/review-tables');
  });

  // ==================== runs ====================

  it('run submits the job, tracks progress and reloads the table when it completes', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.runReviewTable as any).mockResolvedValue({ job_id: 'j1', poll_url: '/api/v1/jobs/j1', documents: 1, fields: 2 });
    (api.getReviewTableJob as any)
      .mockResolvedValueOnce({ job_id: 'j1', status: 'processing', progress: { message: 'Extracted 0/1 documents', fraction: 0 } })
      .mockResolvedValueOnce({ job_id: 'j1', status: 'completed', result: { documents: 1, cells: 2, extracted: 2 } });

    await useReviewTablesStore.getState().run({ documentIds: ['d1'] });
    expect(api.runReviewTable).toHaveBeenCalledWith('t1', { document_ids: ['d1'], field_keys: undefined });
    expect(useReviewTablesStore.getState().running).toBe(true);

    await flush(); // first poll
    expect(useReviewTablesStore.getState().progress?.message).toBe('Extracted 0/1 documents');

    await vi.advanceTimersByTimeAsync(2000); // second poll completes the job
    await flush();
    const st = useReviewTablesStore.getState();
    expect(st.running).toBe(false);
    expect(st.progress).toBeNull();
    expect(api.getReviewTable).toHaveBeenCalledTimes(2);
    const toasts = useUIStore.getState().toasts;
    expect(toasts[toasts.length - 1]?.message).toBe('Review finished: 2 cells, 2 verified.');
  });

  it('run surfaces a failed job and reloads whatever finished', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.runReviewTable as any).mockResolvedValue({ job_id: 'j1', poll_url: '', documents: 1, fields: 2 });
    (api.getReviewTableJob as any).mockResolvedValue({ job_id: 'j1', status: 'failed', error: 'provider down' });

    await useReviewTablesStore.getState().run();
    await flush();
    const st = useReviewTablesStore.getState();
    expect(st.running).toBe(false);
    expect(st.runError).toBe('provider down');
    expect(api.getReviewTable).toHaveBeenCalledTimes(2);
  });

  it('run refuses a table with nothing to extract and a start failure', async () => {
    (api.getReviewTable as any).mockResolvedValue({ ...detail, documents: [] });
    await useReviewTablesStore.getState().loadTable('t1');
    await useReviewTablesStore.getState().run();
    expect(api.runReviewTable).not.toHaveBeenCalled();

    (api.getReviewTable as any).mockResolvedValue(detail);
    await useReviewTablesStore.getState().loadTable('t1');
    (api.runReviewTable as any).mockRejectedValue(new Error('No key'));
    await useReviewTablesStore.getState().run();
    expect(useReviewTablesStore.getState().runError).toBe('No key');
    expect(useReviewTablesStore.getState().running).toBe(false);
  });

  // ==================== picker ====================

  it('openPicker lists the active matter documents, else everything', async () => {
    (api.getAllDocuments as any).mockResolvedValue([{ id: 'd9', filename: 'z.pdf', status: 'indexed' }]);
    await useReviewTablesStore.getState().openPicker();
    expect(api.getAllDocuments).toHaveBeenCalled();
    expect(useReviewTablesStore.getState().pickerDocs.map((d) => d.id)).toEqual(['d9']);
    expect(useReviewTablesStore.getState().pickerOpen).toBe(true);

    useMattersStore.setState({ activeMatterId: 'm1' });
    (api.getMatterDocuments as any).mockResolvedValue({ documents: [{ id: 'd1', filename: 'a.pdf', status: 'indexed' }] });
    await useReviewTablesStore.getState().openPicker();
    expect(api.getMatterDocuments).toHaveBeenCalledWith('m1');
    expect(useReviewTablesStore.getState().pickerDocs.map((d) => d.id)).toEqual(['d1']);
  });

  // ==================== review layer ====================

  const cellWith = (over: Record<string, unknown>) => ({ ...detail.cells[0], ...over });

  it('reviewCell merges the returned cell and refreshes the review counts', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    useReviewTablesStore.setState({ tables: [summary] });
    (api.reviewCell as any).mockResolvedValue(cellWith({ override_value: 'New York', effective_value: 'New York', review_status: 'approved' }));
    expect(await useReviewTablesStore.getState().reviewCell({ documentId: 'd1', fieldKey: 'governing_law' }, { override_value: 'New York', review_status: 'approved' })).toBe(true);
    expect(api.reviewCell).toHaveBeenCalledWith('t1', 'd1', 'governing_law', { override_value: 'New York', review_status: 'approved' });
    const st = useReviewTablesStore.getState();
    expect(st.table?.cells[0].override_value).toBe('New York');
    expect(st.table?.approved_count).toBe(1);
    expect(st.tables.find((t) => t.id === 't1')?.approved_count).toBe(1);
  });

  it('approveColumn bulk-approves every readable row and reviewSelected clears the selection', async () => {
    const two = { ...detail, documents: [...detail.documents, { id: 'd2', filename: 'b.pdf', accessible: false }] };
    (api.getReviewTable as any).mockResolvedValue(two);
    await useReviewTablesStore.getState().loadTable('t1');
    (api.bulkReviewCells as any).mockResolvedValue({ updated: 1, cells: [cellWith({ review_status: 'approved' })] });
    expect(await useReviewTablesStore.getState().approveColumn('governing_law')).toBe(true);
    expect(api.bulkReviewCells).toHaveBeenCalledWith('t1', [{ document_id: 'd1', field_key: 'governing_law' }], { review_status: 'approved' });

    useReviewTablesStore.getState().toggleSelected({ documentId: 'd1', fieldKey: 'term' });
    useReviewTablesStore.getState().toggleSelected({ documentId: 'd1', fieldKey: 'governing_law' });
    expect(useReviewTablesStore.getState().selection.size).toBe(2);
    (api.bulkReviewCells as any).mockResolvedValue({ updated: 2, cells: [] });
    await useReviewTablesStore.getState().reviewSelected({ review_status: 'flagged' });
    expect((api.bulkReviewCells as any).mock.calls[1][2]).toEqual({ review_status: 'flagged' });
    expect(useReviewTablesStore.getState().selection.size).toBe(0);
  });

  it('rerunSelected runs the union of the selected rows and columns', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.runReviewTable as any).mockResolvedValue({ job_id: 'j1', poll_url: '', documents: 1, fields: 2 });
    (api.getReviewTableJob as any).mockResolvedValue({ job_id: 'j1', status: 'completed', result: { documents: 1, cells: 2 } });
    useReviewTablesStore.getState().selectRow('d1', true);
    expect(useReviewTablesStore.getState().selection.size).toBe(2);
    await useReviewTablesStore.getState().rerunSelected();
    expect(api.runReviewTable).toHaveBeenCalledWith('t1', { document_ids: ['d1'], field_keys: ['governing_law', 'term'] });
    expect(useReviewTablesStore.getState().selection.size).toBe(0);
  });

  // ==================== filters, sort, focus, widths ====================

  it('visibleDocuments applies the status filter, column filters and a typed sort', () => {
    const docs = [
      { id: 'd1', filename: 'b.pdf', accessible: true },
      { id: 'd2', filename: 'a.pdf', accessible: true },
      { id: 'd3', filename: 'c.pdf', accessible: true },
    ];
    const cells = indexCells([
      cellWith({ document_id: 'd1', field_key: 'term', value: '12 months', effective_value: '12 months', status: 'extracted' }),
      cellWith({ document_id: 'd2', field_key: 'term', value: '30 days', effective_value: '30 days', status: 'unverified', review_status: 'flagged' }),
      cellWith({ document_id: 'd3', field_key: 'term', value: null, effective_value: null, status: 'not_found' }),
    ]);
    const table = { ...detail, documents: docs };
    const ids = (rows: { id: string }[]) => rows.map((r) => r.id);
    expect(ids(visibleDocuments(table, cells, 'all', {}, { key: null, dir: 'asc' }))).toEqual(['d1', 'd2', 'd3']);
    expect(ids(visibleDocuments(table, cells, 'flagged', {}, { key: null, dir: 'asc' }))).toEqual(['d2']);
    expect(ids(visibleDocuments(table, cells, 'not_found', {}, { key: null, dir: 'asc' }))).toEqual(['d3']);
    expect(ids(visibleDocuments(table, cells, 'all', { term: 'month' }, { key: null, dir: 'asc' }))).toEqual(['d1']);
    expect(ids(visibleDocuments(table, cells, 'all', { __document: 'A.PDF' }, { key: null, dir: 'asc' }))).toEqual(['d2']);
    expect(ids(visibleDocuments(table, cells, 'all', {}, { key: 'term', dir: 'asc' }))).toEqual(['d2', 'd1', 'd3']);
    expect(ids(visibleDocuments(table, cells, 'all', {}, { key: 'term', dir: 'desc' }))).toEqual(['d1', 'd2', 'd3']);
    expect(ids(visibleDocuments(table, cells, 'all', {}, { key: '__document', dir: 'asc' }))).toEqual(['d2', 'd1', 'd3']);
    expect(visibleDocuments(null, cells, 'all', {}, { key: null, dir: 'asc' })).toEqual([]);
  });

  it('toggleSort cycles asc, desc, off and filters reset the focus', () => {
    const st = useReviewTablesStore.getState;
    st().toggleSort('term');
    expect(st().sort).toEqual({ key: 'term', dir: 'asc' });
    st().toggleSort('term');
    expect(st().sort).toEqual({ key: 'term', dir: 'desc' });
    st().toggleSort('term');
    expect(st().sort).toEqual({ key: null, dir: 'asc' });
    st().setFocus({ row: 2, col: 1 });
    st().setColumnFilter('term', 'x');
    expect(st().columnFilters).toEqual({ term: 'x' });
    expect(st().focus).toBeNull();
    st().setColumnFilter('term', '');
    expect(st().columnFilters).toEqual({});
    st().setStatusFilter('flagged');
    st().clearFilters();
    expect(st().statusFilter).toBe('all');
  });

  it('moveFocusByKey walks the grid and selection helpers cover rows and columns', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    const st = useReviewTablesStore.getState;
    expect(st().moveFocusByKey('ArrowDown', 3, 2)).toEqual({ row: 1, col: 0 });
    expect(st().moveFocusByKey('ArrowRight', 3, 2)).toEqual({ row: 1, col: 1 });
    expect(st().moveFocusByKey('Enter', 3, 2)).toBeNull();
    expect(st().focus).toEqual({ row: 1, col: 1 });
    st().selectColumn('term', true);
    expect([...st().selection]).toEqual([cellKey('d1', 'term')]);
    st().selectColumn('term', false);
    expect(st().selection.size).toBe(0);
    st().selectRow('d1', true);
    expect(st().selection.size).toBe(2);
    st().clearSelection();
    expect(st().selection.size).toBe(0);
  });

  it('column widths persist per table and reload with it', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    useReviewTablesStore.getState().setColumnWidth('term', 50);
    expect(useReviewTablesStore.getState().columnWidths).toEqual({ term: 80 }); // floor
    useReviewTablesStore.getState().setColumnWidth('term', 240.6);
    expect(JSON.parse(localStorage.getItem('casecite.reviewTable.widths.t1')!)).toEqual({ term: 241 });
    (api.getReviewTable as any).mockResolvedValue({ ...detail, id: 't2' });
    await useReviewTablesStore.getState().loadTable('t2');
    expect(useReviewTablesStore.getState().columnWidths).toEqual({});
    await useReviewTablesStore.getState().loadTable('t1');
    expect(useReviewTablesStore.getState().columnWidths).toEqual({ term: 241 });
  });

  // ==================== source panel, documents, runs, schemas, export ====================

  it('selectCell loads the document text once and records a failure', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.getReviewDocumentText as any).mockResolvedValue({ document_id: 'd1', filename: 'a.pdf', text: 'laws of Delaware', length: 16 });
    useReviewTablesStore.getState().selectCell({ documentId: 'd1', fieldKey: 'governing_law' });
    await flush();
    expect(useReviewTablesStore.getState().sourceTexts.d1).toEqual({ status: 'ready', data: expect.objectContaining({ text: 'laws of Delaware' }) });
    useReviewTablesStore.getState().selectCell({ documentId: 'd1', fieldKey: 'term' });
    await flush();
    expect(api.getReviewDocumentText).toHaveBeenCalledTimes(1);

    (api.getReviewDocumentText as any).mockRejectedValue(new Error('No indexed text'));
    await useReviewTablesStore.getState().loadSourceText('d2');
    expect(useReviewTablesStore.getState().sourceTexts.d2).toEqual({ status: 'error', message: 'No indexed text' });
  });

  it('addDocuments replaces the table and follows the run it started', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    useReviewTablesStore.setState({ pickerOpen: true });
    const bigger = { ...detail, documents: [...detail.documents, { id: 'd2', filename: 'b.pdf', accessible: true }], document_count: 2 };
    (api.addReviewDocuments as any).mockResolvedValue({ added: ['d2'], table: bigger, run: { job_id: 'j2', poll_url: '', documents: 1, fields: 2 } });
    (api.getReviewTableJob as any)
      .mockResolvedValueOnce({ job_id: 'j2', status: 'processing', progress: { message: 'Extracted 0/1 documents', fraction: 0 } })
      .mockResolvedValueOnce({ job_id: 'j2', status: 'completed', result: { documents: 1, cells: 2 } });
    expect(await useReviewTablesStore.getState().addDocuments(['d2'], true)).toBe(true);
    expect(api.addReviewDocuments).toHaveBeenCalledWith('t1', ['d2'], true);
    expect(useReviewTablesStore.getState().table?.documents).toHaveLength(2);
    expect(useReviewTablesStore.getState().pickerOpen).toBe(false);
    await flush();
    expect(useReviewTablesStore.getState().running).toBe(true);
    expect(useReviewTablesStore.getState().progress?.message).toBe('Extracted 0/1 documents');
    await vi.advanceTimersByTimeAsync(2000);
    await flush();
    expect(useReviewTablesStore.getState().running).toBe(false);
  });

  it('removeDocument confirms, then replaces the table', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.removeReviewDocument as any).mockResolvedValue({ ...detail, documents: [], cells: [], document_count: 0 });
    useReviewTablesStore.getState().removeDocument('d1');
    const confirm = useUIStore.getState().confirmModal;
    expect(confirm?.open).toBe(true);
    await confirm?.onConfirm?.();
    await flush();
    expect(api.removeReviewDocument).toHaveBeenCalledWith('t1', 'd1');
    expect(useReviewTablesStore.getState().table?.documents).toEqual([]);
  });

  it('setRunsOpen loads the run history', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.listReviewRuns as any).mockResolvedValue({ runs: [{ id: 'r1', table_id: 't1', runner_id: 'me', scope: {}, status: 'completed', job_id: 'j', counts: { cells: 2, documents: 1 }, error: null, started_at: '2026-10-01T00:00:00Z', finished_at: null }] });
    useReviewTablesStore.getState().setRunsOpen(true);
    await flush();
    expect(api.listReviewRuns).toHaveBeenCalledWith('t1');
    expect(useReviewTablesStore.getState().runs).toHaveLength(1);
  });

  it('saveSchema posts the fields (sharing with the active matter on request) and deleteSchema confirms', async () => {
    useMattersStore.setState({ activeMatterId: 'm1' });
    const saved = { id: 's1', name: 'Mine', description: null, matter_id: 'm1', owner_id: 'me', role: 'owner', is_builtin: false, fields, created_at: null, updated_at: null };
    (api.createReviewSchema as any).mockResolvedValue(saved);
    expect(await useReviewTablesStore.getState().saveSchema(' Mine ', [...fields, { name: ' ', type: 'text' }], true)).toEqual(saved);
    expect(api.createReviewSchema).toHaveBeenCalledWith({
      name: 'Mine', matter_id: 'm1',
      fields: [{ name: 'Governing law', description: '', type: 'text' }, { name: 'Term', description: '', type: 'duration' }],
    });
    expect(useReviewTablesStore.getState().schemas.map((s) => s.id)).toEqual(['s1']);
    expect(await useReviewTablesStore.getState().saveSchema('', fields, false)).toBeNull();

    (api.deleteReviewSchema as any).mockResolvedValue({ status: 'deleted', id: 's1' });
    useReviewTablesStore.getState().deleteSchema('s1');
    await useUIStore.getState().confirmModal?.onConfirm?.();
    await flush();
    expect(api.deleteReviewSchema).toHaveBeenCalledWith('s1');
    expect(useReviewTablesStore.getState().schemas).toEqual([]);
  });

  it('exportTable sends the chosen options and closes the menu', async () => {
    await useReviewTablesStore.getState().loadTable('t1');
    (api.exportReviewTable as any).mockResolvedValue(new Blob(['x']));
    const createObjectURL = vi.fn().mockReturnValue('blob:x');
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    useReviewTablesStore.getState().setExportOpen(true);
    useReviewTablesStore.getState().setExportOptions({ format: 'json', reviewed_only: true, fields: ['term'] });
    await useReviewTablesStore.getState().exportTable();
    expect(api.exportReviewTable).toHaveBeenCalledWith('t1', expect.objectContaining({ format: 'json', reviewed_only: true, fields: ['term'] }));
    expect(useReviewTablesStore.getState().exportOpen).toBe(false);
    await vi.advanceTimersByTimeAsync(1000);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:x');
  });
});
