import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from './Icon';
import { activateOnKey } from './activateOnKey';
import { useRagDocsStore } from '../../stores/ragDocsStore';
import { useMattersStore, documentInMatter } from '../../stores/mattersStore';
import { formatFileSize } from '../../utils';
import type { DocumentFilter, RagDocument } from '../../types';
import type { Matter } from '../../api/types';
import s from './MatterFileWorkspace.module.css';

/** Documents the caller can see that are filed under none of their matters. */
const OTHER_GROUP = '__other__';

interface Group {
  key: string;
  matter: Matter | null;
  label: string;
  detail: string | null;
  docs: RagDocument[];
}

interface MatterFileWorkspaceProps {
  currentFilter: DocumentFilter | null;
  onScopeChange: (filter: DocumentFilter | null) => void;
  /** Open a file in a viewer tab above the workspace. */
  onOpenFile: (docId: string, filename: string) => void;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

const fileIcon = (name: string) =>
  name.endsWith('.pdf') ? 'FileText' : name.endsWith('.docx') ? 'FileEdit' : 'File';

/**
 * The Matter Strategy workspace: the caller's matters, each with its files.
 *
 * - "All matters" chats with everything the caller can see.
 * - Clicking a matter chats with that matter. It also becomes the header's
 *   active matter, so the app has one notion of "the matter I'm in".
 * - Ticking several matters chats with all of them together; ticking files
 *   adds them to the selection, and clicking a file chats with just it.
 *
 * Matter selections go to the server as matter ids and are resolved there
 * (membership-checked, including colleagues' documents shared through the
 * matter), so the browser never has to hold a matter's full document list.
 */
function MatterFileWorkspace({ currentFilter, onScopeChange, onOpenFile }: MatterFileWorkspaceProps) {
  const matters = useMattersStore((st) => st.matters);
  const mattersLoading = useMattersStore((st) => st.mattersLoading);
  const initMatters = useMattersStore((st) => st.init);
  const loadMatters = useMattersStore((st) => st.loadMatters);
  const activeMatterId = useMattersStore((st) => st.activeMatterId);
  const setActiveMatter = useMattersStore((st) => st.setActiveMatter);

  const ragDocs = useRagDocsStore((st) => st.ragDocs);
  const ragDocsLoading = useRagDocsStore((st) => st.ragDocsLoading);
  const initDocs = useRagDocsStore((st) => st.init);
  const loadDocs = useRagDocsStore((st) => st.loadRagDocuments);

  useEffect(() => {
    initMatters();
    initDocs();
  }, [initMatters, initDocs]);

  const [expanded, setExpanded] = useState(new Set<string>());
  const [search, setSearch] = useState('');

  // ── Groups: one per matter (personal first, as the server lists them),
  //    then anything visible but filed under none of them.
  const groups = useMemo<Group[]>(() => {
    const placed = new Set<string>();
    const out: Group[] = matters.map((m) => {
      const docs = ragDocs.filter((d) => documentInMatter(d, m));
      docs.forEach((d) => placed.add(d.id));
      return {
        key: m.id,
        matter: m,
        label: m.name,
        detail: m.is_personal ? 'Your unfiled documents' : m.client_name,
        docs,
      };
    });
    const rest = ragDocs.filter((d) => !placed.has(d.id));
    if (rest.length) {
      out.push({
        key: OTHER_GROUP,
        matter: null,
        label: matters.length ? 'Other documents' : 'Your documents',
        detail: null,
        docs: rest,
      });
    }
    return out;
  }, [matters, ragDocs]);

  // Open the active matter (or the only group) once, when the list first loads.
  const autoExpanded = useRef(false);
  useEffect(() => {
    if (autoExpanded.current || groups.length === 0) return;
    autoExpanded.current = true;
    const open = groups.length === 1 ? groups[0].key : activeMatterId;
    if (open) setExpanded(new Set([open]));
  }, [groups, activeMatterId]);

  // ── Selection. An explicit pick lives in the filter; with none, the
  //    header's active matter is the scope; with neither, everything.
  const explicit = currentFilter && !currentFilter.search_all ? currentFilter : null;
  const pickedFiles = explicit?.document_ids ?? [];
  const keptFolders = explicit?.folder_paths ?? [];
  const selectedMatters = explicit ? (explicit.matter_ids ?? []) : activeMatterId ? [activeMatterId] : [];
  const allSelected = !explicit && !activeMatterId;
  const selectionSize = selectedMatters.length + pickedFiles.length + keptFolders.length;

  const emit = (matterIds: string[], fileIds: string[]) => {
    if (fileIds.length === 0 && keptFolders.length === 0) {
      if (matterIds.length === 0) {
        setActiveMatter(null);
        onScopeChange(null);
        return;
      }
      if (matterIds.length === 1) {
        // One matter is the active matter: header and workspace agree.
        setActiveMatter(matterIds[0]);
        onScopeChange(null);
        return;
      }
    }
    onScopeChange({
      matter_ids: matterIds.length ? matterIds : null,
      document_ids: fileIds.length ? fileIds : null,
      folder_paths: keptFolders.length ? keptFolders : null,
      include_subfolders: true,
    });
  };

  const toggle = (list: string[], id: string) =>
    list.includes(id) ? list.filter((x) => x !== id) : [...list, id];

  const indexedIds = (docs: RagDocument[]) => docs.filter((d) => d.status === 'indexed').map((d) => d.id);

  const selectAll = () => {
    setActiveMatter(null);
    onScopeChange(null);
  };

  const selectGroupOnly = (g: Group) => {
    if (g.matter) emit([g.matter.id], []);
    else emit([], indexedIds(g.docs));
  };

  const toggleGroupCheck = (g: Group) => {
    if (g.matter) {
      emit(toggle(selectedMatters, g.matter.id), pickedFiles);
      return;
    }
    const ids = indexedIds(g.docs);
    const allIn = ids.length > 0 && ids.every((id) => pickedFiles.includes(id));
    emit(selectedMatters, allIn ? pickedFiles.filter((id) => !ids.includes(id)) : [...new Set([...pickedFiles, ...ids])]);
  };

  const groupChecked = (g: Group) => {
    if (g.matter) return selectedMatters.includes(g.matter.id);
    const ids = indexedIds(g.docs);
    return ids.length > 0 && ids.every((id) => pickedFiles.includes(id));
  };

  const toggleExpanded = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const refresh = () => {
    loadMatters();
    loadDocs();
  };

  // ── Search narrows matters by name/client and files by name.
  const needle = search.trim().toLowerCase();
  const visible = groups
    .map((g) => {
      if (!needle) return { group: g, docs: g.docs, forceOpen: false };
      const groupMatch =
        g.label.toLowerCase().includes(needle) || (g.detail || '').toLowerCase().includes(needle);
      const docs = g.docs.filter((d) => d.filename.toLowerCase().includes(needle));
      if (!groupMatch && docs.length === 0) return null;
      return { group: g, docs: groupMatch ? g.docs : docs, forceOpen: docs.length > 0 };
    })
    .filter((v): v is { group: Group; docs: RagDocument[]; forceOpen: boolean } => v !== null);

  const indexedTotal = ragDocs.filter((d) => d.status === 'indexed').length;
  const loading = (mattersLoading || ragDocsLoading) && groups.length === 0;

  const renderFile = (doc: RagDocument) => {
    const ready = doc.status === 'indexed';
    const isChecked = pickedFiles.includes(doc.id);
    const status = doc.status === 'failed' ? 'failed to index' : !ready ? 'indexing…' : null;
    return (
      <div
        key={doc.id}
        className={`${s.row} ${isChecked ? s.rowSelected : ''}`}
        style={{ '--depth-indent': '18px' } as React.CSSProperties}
        role="button"
        tabIndex={0}
        aria-disabled={!ready}
        onClick={() => ready && emit([], [doc.id])}
        onKeyDown={activateOnKey(() => { if (ready) emit([], [doc.id]); })}
      >
        <span className={s.chevronSpacer} />
        <span className={s.fileIconWrap}>
          <Icon name={fileIcon(doc.filename)} size={15} className={s.fileIcon} />
        </span>
        <span className={s.name}>{doc.filename}</span>
        <span className={s.meta}>{status ?? (doc.size ? formatFileSize(doc.size) : '')}</span>
        <span className={s.rowSpacer} />
        {isChecked && selectionSize === 1 && <span className={s.scopeBadge}>Chatting with this file</span>}
        <button
          className={s.openBtn}
          onClick={(e) => { e.stopPropagation(); onOpenFile(doc.id, doc.filename); }}
          title="Open file"
          aria-label={`Open ${doc.filename}`}
        >
          <Icon name="Eye" size={13} />
        </button>
        {ready && (
          <button
            className={isChecked ? s.checkBtnChecked : s.checkBtn}
            onClick={(e) => { e.stopPropagation(); emit(selectedMatters, toggle(pickedFiles, doc.id)); }}
            aria-label={isChecked ? `Remove ${doc.filename} from selection` : `Add ${doc.filename} to selection`}
          >
            <Icon name="Check" size={11} />
          </button>
        )}
      </div>
    );
  };

  const renderGroup = ({ group: g, docs, forceOpen }: { group: Group; docs: RagDocument[]; forceOpen: boolean }) => {
    const isOpen = forceOpen || expanded.has(g.key);
    const isChecked = groupChecked(g);
    const count = indexedIds(g.docs).length;
    const onlySelection = isChecked && selectionSize === 1 && !!g.matter;
    return (
      <div key={g.key}>
        <div
          className={`${s.row} ${isChecked ? s.rowSelected : ''}`}
          role="button"
          tabIndex={0}
          onClick={() => selectGroupOnly(g)}
          onKeyDown={activateOnKey(() => selectGroupOnly(g))}
        >
          <button
            className={s.chevronBtn}
            onClick={(e) => { e.stopPropagation(); toggleExpanded(g.key); }}
            aria-label={isOpen ? `Hide files in ${g.label}` : `Show files in ${g.label}`}
            aria-expanded={isOpen}
          >
            <Icon name={isOpen ? 'ChevronDown' : 'ChevronRight'} size={14} />
          </button>
          <span className={s.iconChip}>
            <Icon name={g.matter ? (g.matter.is_personal ? 'User' : 'Briefcase') : 'Layers'} size={14} />
          </span>
          <span className={s.folderName}>{g.label}</span>
          {g.detail && <span className={s.meta}>{g.detail}</span>}
          <span className={s.meta}>{plural(count, 'doc')}</span>
          <span className={s.rowSpacer} />
          {onlySelection && <span className={s.scopeBadge}>Chatting with this matter</span>}
          <button
            className={isChecked ? s.checkBtnChecked : s.checkBtn}
            onClick={(e) => { e.stopPropagation(); toggleGroupCheck(g); }}
            aria-label={isChecked ? `Remove ${g.label} from selection` : `Add ${g.label} to selection`}
            aria-pressed={isChecked}
          >
            <Icon name="Check" size={11} />
          </button>
        </div>
        {isOpen && (docs.length ? docs.map(renderFile) : (
          <div className={s.stateBox}>
            <span>No documents filed under this matter yet.</span>
          </div>
        ))}
      </div>
    );
  };

  const summary = [
    selectedMatters.length ? plural(selectedMatters.length, 'matter') : null,
    pickedFiles.length ? plural(pickedFiles.length, 'file') : null,
    keptFolders.length ? plural(keptFolders.length, 'folder') : null,
  ].filter(Boolean).join(' + ');

  return (
    <div className={s.workspace}>
      <div className={s.toolRow}>
        <span className={s.filesLabel}>
          Matters
          <span className={s.filesCount}>{matters.length}</span>
        </span>
        <span className={s.toolSpacer} />
        <div className={s.searchWrap}>
          <Icon name="Search" size={13} className={s.searchIcon} />
          <input
            type="text"
            className={s.searchInput}
            placeholder="Search matters and files…"
            aria-label="Search matters and files"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <button
          className={s.refreshBtn}
          onClick={refresh}
          disabled={mattersLoading || ragDocsLoading}
          aria-label="Refresh matters"
        >
          <Icon name="RefreshCw" size={13} className={mattersLoading || ragDocsLoading ? s.spin : undefined} />
        </button>
      </div>

      <div className={s.treeScroll}>
        <div
          className={`${s.row} ${s.allRow} ${allSelected ? s.rowSelected : ''}`}
          role="button"
          tabIndex={0}
          onClick={selectAll}
          onKeyDown={activateOnKey(selectAll)}
        >
          <span className={s.chevronSpacer} />
          <span className={s.iconChip}>
            <Icon name="Database" size={14} />
          </span>
          <span className={s.folderName}>All matters</span>
          <span className={s.meta}>{plural(indexedTotal, 'doc')}</span>
          <span className={s.rowSpacer} />
          {allSelected && <span className={s.scopeBadge}>Chatting with everything</span>}
        </div>

        {loading ? (
          <div className={s.stateBox}>
            <Icon name="Loader2" size={22} className={s.spin} />
            <span>Loading your matters…</span>
          </div>
        ) : groups.length === 0 ? (
          <div className={s.stateBox}>
            <Icon name="FolderX" size={26} />
            <span>No matters or documents yet — add files in the Knowledge Base and they appear here.</span>
          </div>
        ) : visible.length === 0 ? (
          <div className={s.stateBox}>
            <span>Nothing matches “{search}”.</span>
          </div>
        ) : (
          visible.map(renderGroup)
        )}
      </div>

      {/* Multi-selection summary — one quiet strip instead of a badge per row */}
      {explicit && selectionSize > 1 && (
        <div className={s.scopeBar}>
          <Icon name="MessagesSquare" size={13} />
          <span>Chatting with {summary}</span>
          <button className={s.scopeClear} onClick={selectAll}>
            Clear
          </button>
        </div>
      )}
    </div>
  );
}

export { MatterFileWorkspace };
