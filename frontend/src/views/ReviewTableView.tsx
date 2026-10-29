import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  useReviewTablesStore, indexCells, cellKey, visibleDocuments, STANDARD_CONTRACT_FIELDS, type CellRef,
} from '../stores/reviewTablesStore';
import { useMattersStore } from '../stores/mattersStore';
import { appNavigate } from '../utils/router';
import { filterCounts } from '../utils/reviewGrid';
import {
  Icon,
  ReviewSchemaEditor,
  ReviewDocumentPicker,
  ReviewResultsGrid,
  ReviewSourcePanel,
  ReviewRunBar,
  ReviewSummaryStrip,
  ReviewFilterBar,
  ReviewExportMenu,
  ReviewRunsDrawer,
} from '../components';
import type { ReviewFieldSpec } from '../api/types';
import s from './ReviewTableView.module.css';

/** One review table: its schema and documents, the reviewable results grid, and the evidence behind each cell. */
function ReviewTableView() {
  const { id } = useParams<{ id: string }>();
  const {
    table, tableError, loadTable, estimate,
    saveDetails, saveFields, saveDocuments, addDocuments, removeDocument, deleteTable,
    running, progress, runError, run,
    runs, runsLoading, runsOpen, setRunsOpen,
    reviewCell, approveColumn, reviewSelected, rerunSelected,
    exporting, exportOpen, setExportOpen, exportOptions, setExportOptions, exportTable,
    statusFilter, setStatusFilter, columnFilters, setColumnFilter, clearFilters, sort, toggleSort,
    selection, toggleSelected, selectRow, selectColumn, clearSelection,
    focus, setFocus, moveFocusByKey, columnWidths, setColumnWidth,
    selectedCell, selectCell, sourceTexts, loadSourceText,
    schemaOpen, setSchemaOpen, schemas, loadSchemas, saveSchema, deleteSchema,
    pickerOpen, pickerMode, pickerDocs, pickerLoading, openPicker, closePicker,
  } = useReviewTablesStore();
  const activeMatter = useMattersStore((st) => st.activeMatter());
  const mattersInit = useMattersStore((st) => st.init);

  useEffect(() => { mattersInit(); loadSchemas(); }, [mattersInit, loadSchemas]);
  useEffect(() => { if (id) loadTable(id); }, [id, loadTable]);

  // Local UI state: the schema draft while the editor is open, the rename form,
  // and whether the source panel opened in edit mode (the E shortcut).
  const [draftFields, setDraftFields] = useState<ReviewFieldSpec[]>([]);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (schemaOpen && table) setDraftFields(table.fields); }, [schemaOpen, table]);

  const cells = useMemo(() => indexCells(table?.cells || []), [table]);
  const rows = useMemo(() => visibleDocuments(table, cells, statusFilter, columnFilters, sort), [table, cells, statusFilter, columnFilters, sort]);
  const counts = useMemo(() => (table ? filterCounts(cells, table.documents, table.fields) : null), [table, cells]);
  const isOwner = table?.role === 'owner';
  const selected = selectedCell ? cells.get(cellKey(selectedCell.documentId, selectedCell.fieldKey)) || null : null;
  const selectedField = selectedCell ? table?.fields.find((f) => (f.key || f.name) === selectedCell.fieldKey) : undefined;
  const selectedDoc = selectedCell ? table?.documents.find((d) => d.id === selectedCell.documentId) : undefined;
  const fieldNames = useMemo(() => Object.fromEntries((table?.fields || []).map((f) => [f.key || f.name, f.name])), [table]);
  const documentNames = useMemo(() => Object.fromEntries((table?.documents || []).map((d) => [d.id, d.filename])), [table]);

  const startRename = () => {
    if (!table) return;
    setName(table.name); setDescription(table.description || ''); setRenaming(true);
  };
  const submitRename = async (e: React.FormEvent) => {
    e.preventDefault();
    if (await saveDetails(name, description)) setRenaming(false);
  };
  const submitSchema = async () => {
    if (await saveFields(draftFields)) setSchemaOpen(false);
  };
  const addStandard = () =>
    setDraftFields((prev) => [...prev, ...STANDARD_CONTRACT_FIELDS.filter((f) => !prev.some((p) => p.name.toLowerCase() === f.name.toLowerCase()))]);
  const openCell = (ref: CellRef | null) => { setEditing(false); selectCell(ref); };
  const editCell = (ref: CellRef) => { setEditing(true); selectCell(ref); };

  if (tableError) {
    return (
      <div className={s.container}>
        <div className={s.content}>
          <button className={s.backBtn} onClick={() => appNavigate('/review-tables')}>
            <Icon name="ArrowLeft" size={14} /> All reviews
          </button>
          <div className={s.empty}><Icon name="AlertCircle" size={18} /><p>{tableError}</p></div>
        </div>
      </div>
    );
  }

  if (!table) {
    return (
      <div className={s.container}>
        <div className={s.content}>
          <div className={s.empty}><Icon name="Loader2" size={16} className={s.spin} /> Loading review…</div>
        </div>
      </div>
    );
  }

  const canRun = table.documents.some((d) => d.accessible) && table.fields.length > 0;
  const unavailable = table.documents.filter((d) => !d.accessible).length;
  const presentIds = new Set(table.documents.map((d) => d.id));
  const pickerList = pickerMode === 'add' ? pickerDocs.filter((d) => !presentIds.has(d.id)) : pickerDocs;

  return (
    <div className={s.container}>
      <div className={s.content}>
        <button className={s.backBtn} onClick={() => appNavigate('/review-tables')}>
          <Icon name="ArrowLeft" size={14} /> All reviews
        </button>

        <div className={s.headerRow}>
          {renaming ? (
            <form className={s.renameForm} onSubmit={submitRename}>
              <input className={s.input} value={name} onChange={(e) => setName(e.target.value)} aria-label="Review name" autoFocus />
              <input className={`${s.input} ${s.inputWide}`} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Description" aria-label="Description" />
              <button type="submit" className={s.primaryBtn} disabled={!name.trim()}><Icon name="Check" size={14} /> Save</button>
              <button type="button" className={s.secondaryBtn} onClick={() => setRenaming(false)}>Cancel</button>
            </form>
          ) : (
            <div className={s.titleBlock}>
              <div className={s.titleRow}>
                <h2 className={s.title}>{table.name}</h2>
                <span className={`${s.roleBadge} ${isOwner ? s.roleOwner : ''}`}>{table.role}</span>
                {isOwner && (
                  <button type="button" className={s.iconBtn} onClick={startRename} title="Rename" aria-label="Rename review">
                    <Icon name="Pencil" size={13} />
                  </button>
                )}
              </div>
              {table.description && <p className={s.subtitle}>{table.description}</p>}
              <div className={s.metaRow}>
                <span>{table.documents.length} document{table.documents.length === 1 ? '' : 's'}</span>
                <span>{table.fields.length} field{table.fields.length === 1 ? '' : 's'}</span>
                <span>{table.cell_count} cell{table.cell_count === 1 ? '' : 's'} extracted</span>
                {table.last_run_at && <span>Last run {new Date(table.last_run_at).toLocaleString()}</span>}
                {unavailable > 0 && <span className={s.warn}>{unavailable} document{unavailable === 1 ? '' : 's'} no longer readable</span>}
              </div>
            </div>
          )}
          <div className={s.headerActions}>
            {isOwner && (
              <button className={s.secondaryBtn} onClick={() => openPicker('add')} disabled={running} title="Add documents to this review">
                <Icon name="FolderPlus" size={14} /> Add documents
              </button>
            )}
            <button className={s.secondaryBtn} onClick={() => openPicker('replace')} disabled={!isOwner || running} title={isOwner ? 'Choose which documents are in scope' : 'Only the owner changes the documents'}>
              <Icon name="Files" size={14} /> Documents
            </button>
            <button className={`${s.secondaryBtn} ${schemaOpen ? s.secondaryBtnActive : ''}`} onClick={() => setSchemaOpen(!schemaOpen)} disabled={!isOwner || running} title={isOwner ? undefined : 'Only the owner changes the fields'}>
              <Icon name="Columns2" size={14} /> Fields
            </button>
            <button className={`${s.secondaryBtn} ${runsOpen ? s.secondaryBtnActive : ''}`} onClick={() => setRunsOpen(!runsOpen)} title="Run history">
              <Icon name="History" size={14} /> Runs
            </button>
            <ReviewExportMenu
              open={exportOpen}
              options={exportOptions}
              fields={table.fields}
              exporting={exporting}
              disabled={table.cell_count === 0}
              onToggle={setExportOpen}
              onChange={setExportOptions}
              onExport={() => exportTable()}
              styles={s}
            />
            {isOwner && (
              <button className={s.dangerBtn} onClick={deleteTable} disabled={running} title="Delete this review">
                <Icon name="Trash2" size={14} />
              </button>
            )}
          </div>
        </div>

        {schemaOpen && (
          <div className={s.panel}>
            <div className={s.panelHead}>
              <span className={s.panelTitle}>Fields to extract</span>
              <span className={s.panelHint}>Removing a field deletes its extracted column. Order here is the column order.</span>
            </div>
            <ReviewSchemaEditor
              fields={draftFields}
              onChange={setDraftFields}
              templates={schemas}
              onAddStandardFields={addStandard}
              onSaveTemplate={(templateName, share) => saveSchema(templateName, draftFields, share)}
              canShareWithMatter={Boolean(activeMatter && !activeMatter.is_personal)}
              onDeleteTemplate={deleteSchema}
              styles={s}
            />
            <div className={s.panelActions}>
              <button type="button" className={s.primaryBtn} onClick={submitSchema}><Icon name="Check" size={14} /> Save fields</button>
              <button type="button" className={s.secondaryBtn} onClick={() => setSchemaOpen(false)}>Cancel</button>
            </div>
          </div>
        )}

        <ReviewRunsDrawer
          open={runsOpen}
          runs={runs}
          loading={runsLoading}
          fieldNames={fieldNames}
          documentNames={documentNames}
          onClose={() => setRunsOpen(false)}
          onShowChanged={() => setStatusFilter('changed')}
          styles={s}
        />

        <ReviewRunBar
          estimate={estimate}
          running={running}
          progress={progress}
          runError={runError}
          canRun={canRun}
          onRun={() => run()}
          styles={s}
        />

        {table.documents.length === 0 || table.fields.length === 0 ? (
          <div className={s.empty}>
            <Icon name="Table2" size={18} />
            <p>
              {table.documents.length === 0 && table.fields.length === 0
                ? 'Pick the documents to review and the fields to extract.'
                : table.documents.length === 0
                  ? 'Pick the documents to review.'
                  : 'Add at least one field to extract.'}
            </p>
          </div>
        ) : (
          <>
            {table.cell_count > 0 && (
              <ReviewSummaryStrip
                fields={table.fields}
                cells={table.cells}
                documentCount={table.documents.length}
                running={running}
                progress={progress}
                styles={s}
              />
            )}
            {counts && (
              <ReviewFilterBar
                statusFilter={statusFilter}
                counts={counts}
                hasColumnFilters={Object.values(columnFilters).some((v) => v.trim())}
                visibleRows={rows.length}
                totalRows={table.documents.length}
                selectionSize={selection.size}
                running={running}
                onStatusFilter={setStatusFilter}
                onClearFilters={clearFilters}
                onApproveSelected={() => reviewSelected({ review_status: 'approved' })}
                onFlagSelected={() => reviewSelected({ review_status: 'flagged' })}
                onClearReviewSelected={() => reviewSelected({ review_status: 'pending' })}
                onRerunSelected={rerunSelected}
                onClearSelection={clearSelection}
                styles={s}
              />
            )}
            <div className={`${s.workspace} ${selectedCell ? s.workspaceWithPanel : ''}`}>
              <ReviewResultsGrid
                fields={table.fields}
                documents={rows}
                cells={cells}
                selected={selectedCell}
                selection={selection}
                focus={focus}
                sort={sort}
                columnFilters={columnFilters}
                columnWidths={columnWidths}
                running={running}
                canEditScope={Boolean(isOwner)}
                onSelect={openCell}
                onEdit={editCell}
                onToggleSelected={toggleSelected}
                onSelectRow={selectRow}
                onSelectColumn={selectColumn}
                onFocus={setFocus}
                onKeyNav={moveFocusByKey}
                onToggleSort={toggleSort}
                onColumnFilter={setColumnFilter}
                onColumnWidth={setColumnWidth}
                onReview={(ref, change) => { void reviewCell(ref, change); }}
                onRerunRow={(documentId) => run({ documentIds: [documentId] })}
                onRerunColumn={(fieldKey) => run({ fieldKeys: [fieldKey] })}
                onApproveColumn={(fieldKey) => { void approveColumn(fieldKey); }}
                onRemoveRow={removeDocument}
                styles={s}
              />
              {selectedCell && (
                <ReviewSourcePanel
                  cell={selected}
                  field={selectedField}
                  document={selectedDoc}
                  source={sourceTexts[selectedCell.documentId]}
                  running={running}
                  editing={editing}
                  onReview={(change) => reviewCell(selectedCell, change)}
                  onRerun={() => run({ documentIds: [selectedCell.documentId], fieldKeys: [selectedCell.fieldKey] })}
                  onRetryText={() => loadSourceText(selectedCell.documentId)}
                  onClose={() => openCell(null)}
                  styles={s}
                />
              )}
            </div>
            <div className={s.shortcutHint}>
              Arrow keys move · Enter opens the source · E edits · A approves · F flags · Space selects · Ctrl-click adds to the selection
            </div>
          </>
        )}

        <ReviewDocumentPicker
          isOpen={pickerOpen}
          documents={pickerList}
          loading={pickerLoading}
          selectedIds={pickerMode === 'add' ? [] : table.documents.map((d) => d.id)}
          scopeLabel={pickerMode === 'add'
            ? `Add documents to this review${activeMatter ? ` from "${activeMatter.is_personal ? 'Personal' : activeMatter.name}"` : ''}. ${table.last_run_at ? 'New rows are extracted straight away.' : ''}`
            : activeMatter
              ? `Documents filed under "${activeMatter.is_personal ? 'Personal' : activeMatter.name}"`
              : 'Every indexed document you can read. Set an active matter to narrow this list.'}
          onConfirm={pickerMode === 'add' ? (ids) => addDocuments(ids, Boolean(table.last_run_at)) : saveDocuments}
          onClose={closePicker}
          styles={s}
        />
      </div>
    </div>
  );
}

export default ReviewTableView;
