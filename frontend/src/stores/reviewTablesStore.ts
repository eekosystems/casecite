import { create } from 'zustand';
import { api } from '../api';
import { useUIStore } from './uiStore';
import { useMattersStore } from './mattersStore';
import { registerReset } from './resetRegistry';
import { createJobPoller, toast } from './storeUtils';
import { appNavigate } from '../utils/router';
import logger from '../utils/logger';
import {
  cellMatchesStatus,
  compareValues,
  effectiveValue,
  loadColumnWidths,
  moveFocus,
  saveColumnWidths,
  type GridFocus,
  type StatusFilter,
} from '../utils/reviewGrid';
import type { RagDocument } from '../types';
import type {
  ReviewCell,
  ReviewCellReviewInput,
  ReviewDocumentText,
  ReviewExportOptions,
  ReviewFieldSpec,
  ReviewRun,
  ReviewSchemaTemplate,
  ReviewTableDetail,
  ReviewTableDocument,
  ReviewTableEstimate,
  ReviewTableRunResult,
  ReviewTableSummary,
} from '../api/types';

/** A starting schema for a contract sweep; the reviewer edits it freely. */
export const STANDARD_CONTRACT_FIELDS: ReviewFieldSpec[] = [
  { name: 'Counterparty', description: 'The other party to the agreement', type: 'text' },
  { name: 'Effective date', description: 'When the agreement takes effect', type: 'date' },
  { name: 'Term', description: 'Initial term length', type: 'duration' },
  { name: 'Auto-renewal', description: 'Does the agreement renew automatically?', type: 'boolean' },
  { name: 'Termination notice', description: 'Notice required to terminate for convenience', type: 'duration' },
  { name: 'Governing law', description: 'State or country whose law governs', type: 'text' },
  { name: 'Liability cap', description: 'Cap amount or formula', type: 'money' },
  { name: 'Assignment', description: 'Is assignment permitted without consent?', type: 'boolean' },
];

export interface CellRef {
  documentId: string;
  fieldKey: string;
}

export interface RunProgress {
  message: string;
  fraction: number | null;
}

export interface SortState {
  key: string | null;
  dir: 'asc' | 'desc';
}

export type SourceTextState = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; data: ReviewDocumentText };

const errorMessage = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/** Cells keyed "documentId\u0000fieldKey" for O(1) grid lookups. */
export function indexCells(cells: ReviewCell[]): Map<string, ReviewCell> {
  const map = new Map<string, ReviewCell>();
  for (const c of cells) map.set(cellKey(c.document_id, c.field_key), c);
  return map;
}

export const cellKey = (documentId: string, fieldKey: string) => `${documentId}\u0000${fieldKey}`;
export const parseCellKey = (key: string): CellRef => {
  const [documentId, fieldKey] = key.split('\u0000');
  return { documentId, fieldKey };
};

/** One line summarising a finished run for the toast. */
export function describeRun(result: ReviewTableRunResult | null | undefined): string {
  if (!result) return 'Review finished.';
  const parts = [`${result.cells} cell${result.cells === 1 ? '' : 's'}`];
  if (result.extracted) parts.push(`${result.extracted} verified`);
  if (result.unverified) parts.push(`${result.unverified} unverified`);
  if (result.not_found) parts.push(`${result.not_found} not found`);
  if (result.errors) parts.push(`${result.errors} failed`);
  if (result.changed) parts.push(`${result.changed} changed since last run`);
  return `Review finished: ${parts.join(', ')}.`;
}

/**
 * The rows the grid shows, after the status filter, the per-column text
 * filters and the sort. A row stays when any of its cells passes the status
 * filter; a column filter matches the effective value, case-insensitively.
 */
export function visibleDocuments(
  table: ReviewTableDetail | null,
  cells: Map<string, ReviewCell>,
  statusFilter: StatusFilter,
  columnFilters: Record<string, string>,
  sort: SortState,
): ReviewTableDocument[] {
  if (!table) return [];
  const fields = table.fields;
  let docs = table.documents.filter((doc) => {
    if (statusFilter !== 'all' && !fields.some((f) => cellMatchesStatus(cells.get(cellKey(doc.id, f.key || f.name)), statusFilter))) {
      return false;
    }
    for (const [key, needle] of Object.entries(columnFilters)) {
      const q = needle.trim().toLowerCase();
      if (!q) continue;
      const hay = key === '__document' ? doc.filename : effectiveValue(cells.get(cellKey(doc.id, key))) || '';
      if (!hay.toLowerCase().includes(q)) return false;
    }
    return true;
  });
  if (sort.key) {
    const field = fields.find((f) => (f.key || f.name) === sort.key);
    const dir = sort.dir === 'asc' ? 1 : -1;
    docs = [...docs].sort((a, b) => {
      if (sort.key === '__document') return dir * a.filename.localeCompare(b.filename);
      const va = effectiveValue(cells.get(cellKey(a.id, sort.key!)));
      const vb = effectiveValue(cells.get(cellKey(b.id, sort.key!)));
      const cmp = compareValues(va, vb, field?.type || 'text');
      // Blanks stay last in both directions.
      if (va === null || va === '' || vb === null || vb === '') return cmp;
      return dir * cmp;
    });
  }
  return docs;
}

export interface ReviewTablesState {
  _initialized: boolean;
  tables: ReviewTableSummary[];
  tablesLoading: boolean;
  init: () => void;
  loadTables: () => Promise<void>;
  createTable: (input: {
    name: string;
    description: string;
    fields: ReviewFieldSpec[];
    documentIds: string[];
  }) => Promise<ReviewTableDetail | null>;
  openTable: (tableId: string) => void;

  // ── Saved schemas (built-in + the firm's) ──
  schemas: ReviewSchemaTemplate[];
  schemasLoading: boolean;
  loadSchemas: () => Promise<void>;
  saveSchema: (name: string, fields: ReviewFieldSpec[], shareWithMatter: boolean) => Promise<ReviewSchemaTemplate | null>;
  deleteSchema: (schemaId: string) => void;

  // ── One table ──
  tableId: string | null;
  table: ReviewTableDetail | null;
  tableLoading: boolean;
  tableError: string | null;
  loadTable: (tableId: string) => Promise<void>;
  estimate: ReviewTableEstimate | null;
  loadEstimate: () => Promise<void>;
  saveDetails: (name: string, description: string) => Promise<boolean>;
  saveFields: (fields: ReviewFieldSpec[]) => Promise<boolean>;
  saveDocuments: (documentIds: string[]) => Promise<boolean>;
  addDocuments: (documentIds: string[], runNow: boolean) => Promise<boolean>;
  removeDocument: (documentId: string) => void;
  deleteTable: () => void;

  // ── Runs ──
  running: boolean;
  progress: RunProgress | null;
  runError: string | null;
  run: (scope?: { documentIds?: string[]; fieldKeys?: string[] }) => Promise<void>;
  runs: ReviewRun[];
  runsLoading: boolean;
  runsOpen: boolean;
  setRunsOpen: (open: boolean) => void;
  loadRuns: () => Promise<void>;

  // ── Review layer ──
  reviewCell: (ref: CellRef, change: ReviewCellReviewInput) => Promise<boolean>;
  bulkReview: (refs: CellRef[], change: ReviewCellReviewInput) => Promise<boolean>;
  approveColumn: (fieldKey: string) => Promise<boolean>;
  reviewSelected: (change: ReviewCellReviewInput) => Promise<boolean>;
  rerunSelected: () => Promise<void>;

  // ── Export ──
  exporting: boolean;
  exportOpen: boolean;
  setExportOpen: (open: boolean) => void;
  exportOptions: ReviewExportOptions;
  setExportOptions: (patch: Partial<ReviewExportOptions>) => void;
  exportTable: (options?: ReviewExportOptions) => Promise<void>;

  // ── Grid UI state shared across components ──
  statusFilter: StatusFilter;
  setStatusFilter: (filter: StatusFilter) => void;
  columnFilters: Record<string, string>;
  setColumnFilter: (fieldKey: string, value: string) => void;
  clearFilters: () => void;
  sort: SortState;
  toggleSort: (fieldKey: string) => void;
  selection: Set<string>;
  toggleSelected: (ref: CellRef) => void;
  selectRow: (documentId: string, on: boolean) => void;
  selectColumn: (fieldKey: string, on: boolean) => void;
  clearSelection: () => void;
  focus: GridFocus | null;
  setFocus: (focus: GridFocus | null) => void;
  moveFocusByKey: (key: string, rowCount: number, colCount: number) => GridFocus | null;
  columnWidths: Record<string, number>;
  setColumnWidth: (fieldKey: string, width: number) => void;
  selectedCell: CellRef | null;
  selectCell: (ref: CellRef | null) => void;
  sourceTexts: Record<string, SourceTextState>;
  loadSourceText: (documentId: string) => Promise<void>;
  schemaOpen: boolean;
  setSchemaOpen: (open: boolean) => void;
  pickerOpen: boolean;
  pickerMode: 'replace' | 'add';
  pickerDocs: RagDocument[];
  pickerLoading: boolean;
  openPicker: (mode?: 'replace' | 'add') => Promise<void>;
  closePicker: () => void;
}

const jobPoller = createJobPoller();
const DEFAULT_EXPORT: ReviewExportOptions = { format: 'xlsx', fields: [], reviewed_only: false, include_quotes: true, statuses: [] };

export const useReviewTablesStore = create<ReviewTablesState>((set, get) => ({
  _initialized: false,
  tables: [],
  tablesLoading: false,

  init: () => {
    if (get()._initialized) return;
    set({ _initialized: true });
    void get().loadTables();
    void get().loadSchemas();
  },

  loadTables: async () => {
    set({ tablesLoading: true });
    try {
      const { tables } = await api.listReviewTables();
      set({ tables });
    } catch (e) {
      logger.error('loadReviewTables failed', e);
      toast(errorMessage(e, 'Could not load review tables.'), 'error');
    } finally {
      set({ tablesLoading: false });
    }
  },

  createTable: async ({ name, description, fields, documentIds }) => {
    const trimmed = name.trim();
    if (!trimmed) {
      toast('Give the review a name.', 'error');
      return null;
    }
    const cleanFields = fields.filter((f) => f.name.trim());
    if (cleanFields.length === 0) {
      toast('Add at least one field to extract.', 'error');
      return null;
    }
    try {
      // With an active matter and no explicit documents, the server scopes
      // the table to every document filed under that matter.
      const matterId = useMattersStore.getState().activeMatterId;
      const table = await api.createReviewTable({
        name: trimmed,
        description: description.trim() || null,
        matter_id: matterId,
        fields: cleanFields.map((f) => ({ name: f.name.trim(), description: (f.description || '').trim(), type: f.type })),
        document_ids: documentIds,
      });
      set((st) => ({ tables: [tableSummary(table), ...st.tables] }));
      toast(`Created "${table.name}".`, 'success');
      return table;
    } catch (e) {
      logger.error('createReviewTable failed', e);
      toast(errorMessage(e, 'Could not create the review table.'), 'error');
      return null;
    }
  },

  openTable: (tableId) => {
    appNavigate(`/review-tables/${encodeURIComponent(tableId)}`);
  },

  // ── Saved schemas ──
  schemas: [],
  schemasLoading: false,
  loadSchemas: async () => {
    set({ schemasLoading: true });
    try {
      const { schemas } = await api.listReviewSchemas();
      set({ schemas });
    } catch (e) {
      logger.debug('listReviewSchemas failed', e);
    } finally {
      set({ schemasLoading: false });
    }
  },

  saveSchema: async (name, fields, shareWithMatter) => {
    const trimmed = name.trim();
    const clean = fields.filter((f) => f.name.trim());
    if (!trimmed || clean.length === 0) {
      toast('A template needs a name and at least one field.', 'error');
      return null;
    }
    try {
      const matterId = shareWithMatter ? useMattersStore.getState().activeMatterId : null;
      const schema = await api.createReviewSchema({
        name: trimmed,
        matter_id: matterId,
        fields: clean.map((f) => ({ name: f.name.trim(), description: (f.description || '').trim(), type: f.type })),
      });
      set((st) => ({ schemas: [...st.schemas, schema] }));
      toast(`Saved template "${schema.name}".`, 'success');
      return schema;
    } catch (e) {
      logger.error('createReviewSchema failed', e);
      toast(errorMessage(e, 'Could not save the template.'), 'error');
      return null;
    }
  },

  deleteSchema: (schemaId) => {
    const schema = get().schemas.find((s) => s.id === schemaId);
    if (!schema || schema.is_builtin) return;
    useUIStore.getState().showConfirm({
      title: `Delete template "${schema.name}"?`,
      message: 'Tables created from it keep their fields. Only the saved template is removed.',
      type: 'danger',
      confirmText: 'Delete template',
      onConfirm: async () => {
        try {
          await api.deleteReviewSchema(schemaId);
          set((st) => ({ schemas: st.schemas.filter((s) => s.id !== schemaId) }));
          toast('Template deleted.', 'success');
        } catch (e) {
          logger.error('deleteReviewSchema failed', e);
          toast(errorMessage(e, 'Could not delete the template.'), 'error');
        }
      },
    });
  },

  // ── One table ──
  tableId: null,
  table: null,
  tableLoading: false,
  tableError: null,

  loadTable: async (tableId) => {
    if (get().tableId !== tableId) {
      jobPoller.stop();
      set({
        tableId, table: null, tableError: null, estimate: null, selectedCell: null,
        running: false, progress: null, runError: null, schemaOpen: false, pickerOpen: false,
        runs: [], runsOpen: false, statusFilter: 'all', columnFilters: {}, sort: { key: null, dir: 'asc' },
        selection: new Set(), focus: null, sourceTexts: {}, exportOpen: false,
        columnWidths: loadColumnWidths(tableId),
      });
    }
    set({ tableLoading: true });
    try {
      const table = await api.getReviewTable(tableId);
      if (get().tableId !== tableId) return;
      set({ table, tableError: null, schemaOpen: table.fields.length === 0 });
      void get().loadEstimate();
    } catch (e) {
      logger.error('loadReviewTable failed', e);
      if (get().tableId === tableId) set({ tableError: errorMessage(e, 'Could not load this review table.') });
    } finally {
      if (get().tableId === tableId) set({ tableLoading: false });
    }
  },

  estimate: null,
  loadEstimate: async () => {
    const id = get().tableId;
    if (!id) return;
    try {
      const estimate = await api.estimateReviewTable(id);
      if (get().tableId === id) set({ estimate });
    } catch (e) {
      logger.debug('estimateReviewTable failed', e);
    }
  },

  saveDetails: async (name, description) => {
    const id = get().tableId;
    if (!id || !name.trim()) return false;
    return applyUpdate(id, { name: name.trim(), description: description.trim() || null }, 'Saved.');
  },

  saveFields: async (fields) => {
    const id = get().tableId;
    if (!id) return false;
    const clean = fields
      .filter((f) => f.name.trim())
      .map((f) => ({ key: f.key || undefined, name: f.name.trim(), description: (f.description || '').trim(), type: f.type }));
    if (clean.length === 0) {
      toast('Keep at least one field.', 'error');
      return false;
    }
    const current = get().table;
    const dropped = current ? current.fields.filter((f) => f.key && !clean.some((c) => c.key === f.key)) : [];
    const hasCells = dropped.some((f) => current!.cells.some((c) => c.field_key === f.key));
    if (hasCells) {
      return new Promise<boolean>((resolve) => {
        useUIStore.getState().showConfirm({
          title: 'Remove extracted columns?',
          message: `Removing ${dropped.map((f) => `"${f.name}"`).join(', ')} also deletes the values already extracted for ${dropped.length === 1 ? 'it' : 'them'}.`,
          type: 'danger',
          confirmText: 'Remove',
          onConfirm: async () => resolve(await applyUpdate(id, { fields: clean }, 'Schema saved.')),
          onCancel: () => resolve(false),
        });
      });
    }
    return applyUpdate(id, { fields: clean }, 'Schema saved.');
  },

  saveDocuments: async (documentIds) => {
    const id = get().tableId;
    if (!id) return false;
    const ok = await applyUpdate(id, { document_ids: documentIds }, `${documentIds.length} document${documentIds.length === 1 ? '' : 's'} in scope.`);
    if (ok) set({ pickerOpen: false });
    return ok;
  },

  addDocuments: async (documentIds, runNow) => {
    const id = get().tableId;
    if (!id || documentIds.length === 0) return false;
    try {
      const result = await api.addReviewDocuments(id, documentIds, runNow);
      if (get().tableId !== id) return true;
      replaceTable(result.table);
      set({ pickerOpen: false });
      toast(`${result.added.length} document${result.added.length === 1 ? '' : 's'} added.`, 'success');
      if (result.run) watchJob(id, result.run.job_id);
      return true;
    } catch (e) {
      logger.error('addReviewDocuments failed', e);
      toast(errorMessage(e, 'Could not add the documents.'), 'error');
      return false;
    }
  },

  removeDocument: (documentId) => {
    const table = get().table;
    const doc = table?.documents.find((d) => d.id === documentId);
    if (!table || !doc) return;
    useUIStore.getState().showConfirm({
      title: `Remove "${doc.filename}" from this review?`,
      message: 'Its row and every value extracted for it are deleted. The document itself is not affected.',
      type: 'danger',
      confirmText: 'Remove row',
      onConfirm: async () => {
        try {
          const updated = await api.removeReviewDocument(table.id, documentId);
          if (get().tableId !== table.id) return;
          replaceTable(updated);
          set({ selectedCell: null });
          toast('Row removed.', 'success');
        } catch (e) {
          logger.error('removeReviewDocument failed', e);
          toast(errorMessage(e, 'Could not remove the row.'), 'error');
        }
      },
    });
  },

  deleteTable: () => {
    const table = get().table;
    if (!table) return;
    useUIStore.getState().showConfirm({
      title: `Delete "${table.name}"?`,
      message: 'The schema, every extracted value and quote, and the review history are permanently deleted. The documents themselves are not affected.',
      type: 'danger',
      confirmText: 'Delete review',
      onConfirm: async () => {
        try {
          await api.deleteReviewTable(table.id);
          jobPoller.stop();
          set((st) => ({
            tables: st.tables.filter((t) => t.id !== table.id),
            tableId: null, table: null, estimate: null, running: false, progress: null,
          }));
          toast('Review table deleted.', 'success');
          appNavigate('/review-tables');
        } catch (e) {
          logger.error('deleteReviewTable failed', e);
          toast(errorMessage(e, 'Could not delete the review table.'), 'error');
        }
      },
    });
  },

  // ── Runs ──
  running: false,
  progress: null,
  runError: null,

  run: async (scope = {}) => {
    const id = get().tableId;
    const table = get().table;
    if (!id || !table || get().running) return;
    if (table.documents.length === 0 || table.fields.length === 0) {
      toast('Add at least one document and one field before running.', 'error');
      return;
    }
    set({ runError: null });
    try {
      const started = await api.runReviewTable(id, {
        document_ids: scope.documentIds,
        field_keys: scope.fieldKeys,
      });
      watchJob(id, started.job_id);
    } catch (e) {
      logger.error('runReviewTable failed', e);
      const message = errorMessage(e, 'Could not start the review.');
      set({ running: false, progress: null, runError: message });
      toast(message, 'error');
    }
  },

  runs: [],
  runsLoading: false,
  runsOpen: false,
  setRunsOpen: (open) => {
    set({ runsOpen: open });
    if (open) void get().loadRuns();
  },
  loadRuns: async () => {
    const id = get().tableId;
    if (!id) return;
    set({ runsLoading: true });
    try {
      const { runs } = await api.listReviewRuns(id);
      if (get().tableId === id) set({ runs });
    } catch (e) {
      logger.debug('listReviewRuns failed', e);
    } finally {
      if (get().tableId === id) set({ runsLoading: false });
    }
  },

  // ── Review layer ──
  reviewCell: async (ref, change) => {
    const id = get().tableId;
    if (!id) return false;
    try {
      const cell = await api.reviewCell(id, ref.documentId, ref.fieldKey, change);
      if (get().tableId !== id) return true;
      mergeCells([cell]);
      return true;
    } catch (e) {
      logger.error('reviewCell failed', e);
      toast(errorMessage(e, 'Could not save the review.'), 'error');
      return false;
    }
  },

  bulkReview: async (refs, change) => {
    const id = get().tableId;
    if (!id || refs.length === 0) return false;
    try {
      const result = await api.bulkReviewCells(
        id,
        refs.map((r) => ({ document_id: r.documentId, field_key: r.fieldKey })),
        change,
      );
      if (get().tableId !== id) return true;
      mergeCells(result.cells);
      const what = change.review_status === 'approved' ? 'approved' : change.review_status === 'flagged' ? 'flagged' : 'updated';
      toast(`${result.updated} cell${result.updated === 1 ? '' : 's'} ${what}.`, 'success');
      return true;
    } catch (e) {
      logger.error('bulkReviewCells failed', e);
      toast(errorMessage(e, 'Could not update the cells.'), 'error');
      return false;
    }
  },

  approveColumn: async (fieldKey) => {
    const table = get().table;
    if (!table) return false;
    const refs = table.documents.filter((d) => d.accessible).map((d) => ({ documentId: d.id, fieldKey }));
    return get().bulkReview(refs, { review_status: 'approved' });
  },

  reviewSelected: async (change) => {
    const refs = [...get().selection].map(parseCellKey);
    const ok = await get().bulkReview(refs, change);
    if (ok) set({ selection: new Set() });
    return ok;
  },

  rerunSelected: async () => {
    const refs = [...get().selection].map(parseCellKey);
    if (refs.length === 0) return;
    const documentIds = [...new Set(refs.map((r) => r.documentId))];
    const fieldKeys = [...new Set(refs.map((r) => r.fieldKey))];
    set({ selection: new Set() });
    await get().run({ documentIds, fieldKeys });
  },

  // ── Export ──
  exporting: false,
  exportOpen: false,
  setExportOpen: (open) => set({ exportOpen: open }),
  exportOptions: DEFAULT_EXPORT,
  setExportOptions: (patch) => set((st) => ({ exportOptions: { ...st.exportOptions, ...patch } })),
  exportTable: async (options) => {
    const table = get().table;
    if (!table || get().exporting) return;
    const opts = options || get().exportOptions;
    set({ exporting: true });
    try {
      const blob = await api.exportReviewTable(table.id, opts);
      const url = URL.createObjectURL(blob);
      try {
        const a = document.createElement('a');
        a.href = url;
        a.download = `${table.name.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'review'}.${opts.format}`;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        // Give the browser a tick to start the download before the URL goes away.
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      set({ exportOpen: false });
      toast(`Exported ${opts.format.toUpperCase()}.`, 'success');
    } catch (e) {
      logger.error('exportReviewTable failed', e);
      toast(errorMessage(e, 'Export failed.'), 'error');
    } finally {
      set({ exporting: false });
    }
  },

  // ── Grid UI state ──
  statusFilter: 'all',
  setStatusFilter: (statusFilter) => set({ statusFilter, focus: null }),
  columnFilters: {},
  setColumnFilter: (fieldKey, value) =>
    set((st) => {
      const next = { ...st.columnFilters };
      if (value) next[fieldKey] = value; else delete next[fieldKey];
      return { columnFilters: next, focus: null };
    }),
  clearFilters: () => set({ statusFilter: 'all', columnFilters: {}, sort: { key: null, dir: 'asc' } }),
  sort: { key: null, dir: 'asc' },
  toggleSort: (fieldKey) =>
    set((st) => {
      if (st.sort.key !== fieldKey) return { sort: { key: fieldKey, dir: 'asc' } };
      if (st.sort.dir === 'asc') return { sort: { key: fieldKey, dir: 'desc' } };
      return { sort: { key: null, dir: 'asc' } };
    }),
  selection: new Set(),
  toggleSelected: (ref) =>
    set((st) => {
      const next = new Set(st.selection);
      const key = cellKey(ref.documentId, ref.fieldKey);
      if (next.has(key)) next.delete(key); else next.add(key);
      return { selection: next };
    }),
  selectRow: (documentId, on) =>
    set((st) => {
      const next = new Set(st.selection);
      for (const f of st.table?.fields || []) {
        const key = cellKey(documentId, f.key || f.name);
        if (on) next.add(key); else next.delete(key);
      }
      return { selection: next };
    }),
  selectColumn: (fieldKey, on) =>
    set((st) => {
      const next = new Set(st.selection);
      for (const d of st.table?.documents || []) {
        const key = cellKey(d.id, fieldKey);
        if (on) next.add(key); else next.delete(key);
      }
      return { selection: next };
    }),
  clearSelection: () => set({ selection: new Set() }),
  focus: null,
  setFocus: (focus) => set({ focus }),
  moveFocusByKey: (key, rowCount, colCount) => {
    const next = moveFocus(get().focus, key, rowCount, colCount);
    if (next) set({ focus: next });
    return next;
  },
  columnWidths: {},
  setColumnWidth: (fieldKey, width) => {
    const id = get().tableId;
    const widths = { ...get().columnWidths, [fieldKey]: Math.max(80, Math.round(width)) };
    set({ columnWidths: widths });
    if (id) saveColumnWidths(id, widths);
  },
  selectedCell: null,
  selectCell: (ref) => {
    set({ selectedCell: ref });
    if (ref) void get().loadSourceText(ref.documentId);
  },
  sourceTexts: {},
  loadSourceText: async (documentId) => {
    const id = get().tableId;
    const current = get().sourceTexts[documentId];
    if (!id || (current && current.status !== 'error')) return;
    set((st) => ({ sourceTexts: { ...st.sourceTexts, [documentId]: { status: 'loading' } } }));
    try {
      const data = await api.getReviewDocumentText(id, documentId);
      if (get().tableId !== id) return;
      set((st) => ({ sourceTexts: { ...st.sourceTexts, [documentId]: { status: 'ready', data } } }));
    } catch (e) {
      logger.debug('getReviewDocumentText failed', e);
      if (get().tableId !== id) return;
      const message = errorMessage(e, 'The document text could not be loaded.');
      set((st) => ({ sourceTexts: { ...st.sourceTexts, [documentId]: { status: 'error', message } } }));
    }
  },
  schemaOpen: false,
  setSchemaOpen: (open) => set({ schemaOpen: open }),
  pickerOpen: false,
  pickerMode: 'replace',
  pickerDocs: [],
  pickerLoading: false,

  openPicker: async (mode = 'replace') => {
    set({ pickerOpen: true, pickerMode: mode, pickerLoading: true });
    try {
      // Within an active matter the picker offers that matter's documents
      // (including colleagues' filed ones); otherwise everything the user can read.
      const matterId = useMattersStore.getState().activeMatterId;
      const docs = matterId
        ? (await api.getMatterDocuments(matterId)).documents
        : await api.getAllDocuments();
      set({ pickerDocs: docs as RagDocument[] });
    } catch (e) {
      logger.error('review picker load failed', e);
      toast(errorMessage(e, 'Could not load your documents.'), 'error');
      set({ pickerDocs: [] });
    } finally {
      set({ pickerLoading: false });
    }
  },
  closePicker: () => set({ pickerOpen: false }),
}));

function tableSummary(table: ReviewTableDetail): ReviewTableSummary {
  const { fields: _fields, documents: _documents, cells: _cells, ...summary } = table;
  return summary;
}

/** Put a fresh copy of the open table in the store and the list. */
function replaceTable(table: ReviewTableDetail) {
  useReviewTablesStore.setState((prev) => ({
    table,
    tables: prev.tables.map((t) => (t.id === table.id ? tableSummary(table) : t)),
  }));
  void useReviewTablesStore.getState().loadEstimate();
}

/** Merge reviewed cells into the open table and refresh its review counts. */
function mergeCells(updated: ReviewCell[]) {
  useReviewTablesStore.setState((prev) => {
    if (!prev.table) return {};
    const byKey = new Map(updated.map((c) => [cellKey(c.document_id, c.field_key), c]));
    const kept = prev.table.cells.filter((c) => !byKey.has(cellKey(c.document_id, c.field_key)));
    const cells = [...kept, ...updated];
    const approved = cells.filter((c) => c.review_status === 'approved').length;
    const flagged = cells.filter((c) => c.review_status === 'flagged').length;
    const table = { ...prev.table, cells, cell_count: cells.length, approved_count: approved, flagged_count: flagged };
    return { table, tables: prev.tables.map((t) => (t.id === table.id ? tableSummary(table) : t)) };
  });
}

/** Follow a run's job until it settles, then reload the table. */
function watchJob(id: string, jobId: string) {
  const { get, set } = { get: useReviewTablesStore.getState, set: useReviewTablesStore.setState };
  jobPoller.stop();
  set({ running: true, runError: null, progress: { message: 'Starting…', fraction: 0 } });
  jobPoller.start({
    fetchJob: () => api.getReviewTableJob(jobId),
    maxAttempts: 600,
    intervalMs: 2000,
    failedMessage: 'The review run failed.',
    timeoutMessage: 'Timed out waiting for the review to finish.',
    onPending: (job) => {
      if (get().tableId !== id) return;
      if (job.progress) set({ progress: { message: job.progress.message, fraction: job.progress.fraction } });
    },
    onCompleted: async (job) => {
      if (get().tableId !== id) return;
      set({ running: false, progress: null, sourceTexts: {} });
      toast(describeRun(job.result), job.result?.errors ? 'warning' : 'success');
      await get().loadTable(id);
      void get().loadTables();
      if (get().runsOpen) void get().loadRuns();
    },
    onFailed: (message) => {
      if (get().tableId !== id) return;
      set({ running: false, progress: null, runError: message });
      toast(message, 'error');
      void get().loadTable(id); // whatever finished before the failure is already stored
      if (get().runsOpen) void get().loadRuns();
    },
  });
}

/** PATCH the open table, replace it in the store, refresh the estimate. */
async function applyUpdate(
  id: string,
  changes: Parameters<typeof api.updateReviewTable>[1],
  successMessage: string,
): Promise<boolean> {
  try {
    const table = await api.updateReviewTable(id, changes);
    const st = useReviewTablesStore.getState();
    if (st.tableId !== id) return true;
    replaceTable(table);
    useReviewTablesStore.setState({ selectedCell: null });
    toast(successMessage, 'success');
    return true;
  } catch (e) {
    logger.error('updateReviewTable failed', e);
    toast(errorMessage(e, 'Could not save the change.'), 'error');
    return false;
  }
}

registerReset(() => {
  jobPoller.stop();
  useReviewTablesStore.setState(useReviewTablesStore.getInitialState(), true);
});
