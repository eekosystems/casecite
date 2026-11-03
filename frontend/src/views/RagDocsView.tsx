import React, { useEffect, useState } from 'react';
import { useRagDocsStore } from '../stores/ragDocsStore';
import { useSettingsStore } from '../stores/settingsStore';
import { useAuthStore } from '../stores/authStore';
import { useConnectorsStore } from '../stores/connectorsStore';
import { Icon } from '../components';
import type { RagDocument, Connector } from '../types';
import s from './RagDocsView.module.css';

// Picker-based connectors that can be used directly in this build.
const PICKER_SOURCE_IDS = ['google_drive', 'onedrive', 'box', 'dropbox'];

// Enterprise integrations. Cards go live automatically when the server has
// credentials configured for the connector (a Private Install); otherwise
// they render as informational cards.
const PRIVATE_INSTALL_GROUPS = [
  {
    label: 'Document Management',
    items: [
      { id: 'netdocuments', name: 'NetDocuments', icon: 'FileStack', color: '#1E88E5', desc: 'Full document sync and search' },
      { id: 'imanage', name: 'iManage', icon: 'Database', color: '#6366F1', desc: 'Work product management integration' },
    ],
  },
  {
    label: 'Practice Management',
    items: [
      { id: 'clio', name: 'Clio', icon: 'Scale', color: '#2563EB', desc: 'Matter document sync' },
      { id: 'filevine', name: 'Filevine', icon: 'FolderTree', color: '#10B981', desc: 'Case management integration' },
    ],
  },
  {
    label: 'Full Background Sync',
    items: [
      { id: null, name: 'Google Drive', icon: 'HardDrive', color: '#4285F4', desc: 'Automatic background sync of entire folders' },
      { id: null, name: 'OneDrive & SharePoint', icon: 'Cloud', color: '#0078D4', desc: 'Continuous sync via Microsoft Graph' },
      { id: null, name: 'Box', icon: 'Box', color: '#0061D5', desc: 'Enterprise content management sync' },
      { id: null, name: 'Dropbox', icon: 'Droplet', color: '#0061FF', desc: 'Automatic folder monitoring and sync' },
    ],
  },
] as const;

function RagDocsView() {
  const {
    ragDocs, ragDocsLoading, ragDocsDeleting, ragDocsSearchQuery,
    setRagDocsSearchQuery, loadRagDocuments, handleRagDocDelete,
    ragDocsFiltered, folders, createFolder, deleteFolder, moveDocToFolder,
    init,
  } = useRagDocsStore();
  const { ragSettings } = useSettingsStore();
  const { connectors, handleConnectorClick, init: initConnectors } = useConnectorsStore();
  const isAdmin = useAuthStore((st) => (st.user?.roles ?? []).includes('admin') || st.user?.role === 'admin');
  useEffect(() => { init(); initConnectors(); }, [init, initConnectors]);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [openFolders, setOpenFolders] = useState<Record<string, boolean>>({});
  const [dragOverKey, setDragOverKey] = useState<string | null>(null);
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');

  const generalDocs = ragDocsFiltered.filter((d: RagDocument) => !d.folder_path);
  const docsInFolder = (path: string) =>
    ragDocsFiltered.filter((d: RagDocument) => d.folder_path === path);

  const submitNewFolder = () => {
    const name = newFolderName.trim();
    if (name) createFolder(name);
    setNewFolderName('');
    setNewFolderOpen(false);
  };

  const isErrored = (doc: RagDocument) => doc.status === 'failed' || doc.status === 'delete_failed';
  const deletionFailureText = (doc: RagDocument) => {
    const info = (doc.metadata?.deletion ?? {}) as { failed_step?: string; error?: string };
    const step = info.failed_step === 'vectors' ? 'search index'
      : info.failed_step === 'derived' ? 'derived analyses'
      : info.failed_step === 'file' ? 'stored file' : 'a step';
    return `Deletion failed at the ${step} — delete again to retry${info.error ? ` (${info.error})` : ''}`;
  };

  const renderDocCard = (doc: RagDocument) => (
    <div
      key={doc.id}
      className={s.docCard}
      draggable
      onDragStart={(e) => e.dataTransfer.setData('text/doc-id', doc.id)}
      title="Drag into a folder"
    >
      <div className={`${s.docIcon} ${isErrored(doc) ? s.docIconError : ''}`}>
        <Icon name={isErrored(doc) ? 'AlertCircle' : 'FileText'} size={20}
          className={isErrored(doc) ? s.docIconColorError : s.docIconColorDefault} />
      </div>
      <div className={s.docDetails}>
        <div className={s.docFilename}>{doc.filename}</div>
        <div className={`${s.docMetaText} ${isErrored(doc) ? s.docMetaError : ''}`}>
          {doc.status === 'delete_failed'
            ? deletionFailureText(doc)
            : doc.status === 'failed'
              ? String(doc.metadata?.error || 'Failed to index')
              : `${doc.chunk_count || 0} chunks`}
          {doc.created_at && ` · ${new Date(doc.created_at).toLocaleDateString()}`}
        </div>
        {!isErrored(doc) && typeof doc.metadata?.extraction_warning === 'string' && doc.metadata.extraction_warning && (
          <div className={`${s.docMetaText} ${s.docMetaWarning}`}>
            Partially indexed: {doc.metadata.extraction_warning}
          </div>
        )}
      </div>
      <button
        onClick={() => handleRagDocDelete(doc)}
        disabled={ragDocsDeleting[doc.id]}
        title="Delete document"
        className={`${s.deleteBtn} ${ragDocsDeleting[doc.id] ? s.deleteBtnDisabled : ''}`}
      >
        <Icon name={ragDocsDeleting[doc.id] ? 'Loader2' : 'Trash2'} size={16}
          className={ragDocsDeleting[doc.id] ? s.spinnerIcon : undefined} />
      </button>
    </div>
  );

  const dropProps = (key: string, targetPath: string | null) => ({
    onDragOver: (e: React.DragEvent) => { e.preventDefault(); setDragOverKey(key); },
    onDragLeave: () => setDragOverKey((k) => (k === key ? null : k)),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      const id = e.dataTransfer.getData('text/doc-id');
      if (id) moveDocToFolder(id, targetPath);
      setDragOverKey(null);
    },
  });

  return (
    <div className={s.container}>
      <div className={s.content}>
        <div className={s.headerRow}>
          <div>
            <h2 className={s.title}>
              Knowledge Base
            </h2>
            <p className={s.subtitle}>
              Your document brain — {ragDocs.length} documents indexed in {ragSettings.vectorDb}
            </p>
          </div>
          <button
            onClick={loadRagDocuments}
            disabled={ragDocsLoading}
            className={`${s.refreshBtn} ${ragDocsLoading ? s.refreshBtnDisabled : ''}`}
          >
            <Icon name={ragDocsLoading ? 'Loader2' : 'RefreshCw'} size={14}
              className={ragDocsLoading ? s.spinnerIcon : undefined} />
            Refresh
          </button>
        </div>

        {/* Sources — everything that can feed the knowledge base */}
        <div className={s.sourcesSection}>
          <button
            className={s.sourcesToggle}
            onClick={() => setSourcesOpen(o => !o)}
            aria-expanded={sourcesOpen}
          >
            <span className={s.sourcesLabel}>Sources</span>
            <Icon name={sourcesOpen ? 'ChevronUp' : 'ChevronDown'} size={16} className={s.sourcesChevron} />
          </button>

          {sourcesOpen && (<>
          {/* Connect now (available in this build) */}
          <div className={s.sourceGroupHeading}>Connect a source</div>
          <div className={s.sourcesGrid}>
            {connectors
              .filter((c: Connector) => PICKER_SOURCE_IDS.includes(c.id))
              .map((connector: Connector) => (
                <button
                  key={connector.id}
                  onClick={() => handleConnectorClick(connector)}
                  className={s.sourceCard}
                  title={connector.connected ? `Manage ${connector.name}` : `Connect ${connector.name}`}
                >
                  <div
                    className={s.sourceIcon}
                    style={{ background: connector.connected ? connector.color : 'rgba(255,255,255,0.08)' }}
                  >
                    <Icon name={connector.icon} size={18}
                      style={{ color: connector.connected ? 'white' : 'rgba(255,255,255,0.5)' }} />
                  </div>
                  <div className={s.sourceInfo}>
                    <span className={s.sourceName}>{connector.name}</span>
                    <span className={s.sourceStatus}>
                      {connector.adminOnly && !isAdmin
                        ? 'Admin only'
                        : connector.configured === false
                        ? 'Not configured'
                        : connector.connected
                          ? `${connector.docs > 1000 ? (connector.docs / 1000).toFixed(1) + 'k' : connector.docs} docs`
                          : 'Connect'}
                    </span>
                  </div>
                  <Icon
                    name={connector.connected ? 'Check' : 'Plus'}
                    size={14}
                    className={connector.connected ? s.sourceConnected : s.sourceAdd}
                  />
                </button>
              ))}
          </div>

          {/* Enterprise integrations — live when the server has credentials, informational otherwise */}
          {PRIVATE_INSTALL_GROUPS.map((group) => (
            <div key={group.label}>
              <div className={s.sourceGroupHeading}>{group.label}</div>
              <div className={s.sourcesGrid}>
                {group.items.map((p) => {
                  const live = p.id
                    ? connectors.find((c: Connector) => c.id === p.id && c.configured === true)
                    : undefined;
                  if (live) {
                    return (
                      <button
                        key={p.name}
                        onClick={() => handleConnectorClick(live)}
                        className={s.sourceCard}
                        title={live.connected ? `Manage ${live.name}` : `Connect ${live.name}`}
                      >
                        <div className={s.sourceIcon} style={{ background: p.color }}>
                          <Icon name={p.icon} size={18} style={{ color: 'white' }} />
                        </div>
                        <div className={s.sourceInfo}>
                          <span className={s.sourceName}>{p.name}</span>
                          <span className={s.sourceStatus}>
                            {live.adminOnly && !isAdmin
                              ? 'Admin only'
                              : live.connected
                              ? `${live.docs > 1000 ? (live.docs / 1000).toFixed(1) + 'k' : live.docs} docs`
                              : 'Connect'}
                          </span>
                        </div>
                        <Icon
                          name={live.connected ? 'Check' : 'Plus'}
                          size={14}
                          className={live.connected ? s.sourceConnected : s.sourceAdd}
                        />
                      </button>
                    );
                  }
                  return (
                    <div
                      key={p.name}
                      className={`${s.sourceCard} ${s.sourceCardStatic}`}
                      title="Available with a Private Install"
                    >
                      <div className={s.sourceIcon} style={{ background: p.color }}>
                        <Icon name={p.icon} size={18} style={{ color: 'white' }} />
                      </div>
                      <div className={s.sourceInfo}>
                        <span className={s.sourceName}>{p.name}</span>
                        <span className={s.sourceDesc}>{p.desc}</span>
                      </div>
                      <span className={s.privateBadge}>Private Install</span>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}

          <p className={s.sourcesNote}>
            Enterprise DMS and full background sync require a Private Install, where your IT team
            configures secure credentials inside your own infrastructure — full sync, automatic
            document monitoring, and DMS connectivity while keeping complete control over your data.
          </p>
          </>)}
        </div>

        {/* Search */}
        <div className={s.searchWrapper}>
          <input
            aria-label="Search documents"
            type="text"
            placeholder="Search documents..."
            value={ragDocsSearchQuery}
            onChange={e => setRagDocsSearchQuery(e.target.value)}
            className={s.searchInput}
          />
        </div>

        {/* Library header + New Folder */}
        <div className={s.libraryHeader}>
          <span className={s.sourcesLabel}>Library</span>
          {newFolderOpen ? (
            <div className={s.newFolderRow}>
              <input
                aria-label="New folder name"
                autoFocus
                className={s.newFolderInput}
                placeholder="Folder name"
                value={newFolderName}
                onChange={e => setNewFolderName(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') submitNewFolder();
                  if (e.key === 'Escape') { setNewFolderOpen(false); setNewFolderName(''); }
                }}
              />
              <button className={s.newFolderConfirm} onClick={submitNewFolder}>Create</button>
              <button className={s.newFolderCancel} onClick={() => { setNewFolderOpen(false); setNewFolderName(''); }}>Cancel</button>
            </div>
          ) : (
            <button className={s.newFolderBtn} onClick={() => setNewFolderOpen(true)}>
              <Icon name="FolderPlus" size={14} /> New Folder
            </button>
          )}
        </div>

        {ragDocsLoading && ragDocs.length === 0 ? (
          <div className={s.loadingState}>
            <Icon name="Loader2" size={32} className={s.loadingSpinner} />
            <div>Loading documents...</div>
          </div>
        ) : (
          <div className={s.library}>
            {/* Folders */}
            {folders.map((path: string) => {
              const docs = docsInFolder(path);
              const open = openFolders[path];
              return (
                <div
                  key={path}
                  className={`${s.folder} ${dragOverKey === path ? s.folderDragOver : ''}`}
                  {...dropProps(path, path)}
                >
                  <div className={s.folderRow}>
                    <button
                      className={s.folderToggle}
                      onClick={() => setOpenFolders(o => ({ ...o, [path]: !o[path] }))}
                    >
                      <Icon name={open ? 'ChevronDown' : 'ChevronRight'} size={14} className={s.folderChevron} />
                      <Icon name="Folder" size={16} className={s.folderIcon} />
                      <span className={s.folderName}>{path.replace(/^\//, '')}</span>
                      <span className={s.folderCount}>{docs.length}</span>
                    </button>
                    <button className={s.folderDelete} title="Delete folder" onClick={() => deleteFolder(path)}>
                      <Icon name="Trash2" size={14} />
                    </button>
                  </div>
                  {open && (
                    <div className={s.folderDocs}>
                      {docs.length === 0
                        ? <div className={s.folderEmpty}>Drag documents here</div>
                        : docs.map(renderDocCard)}
                    </div>
                  )}
                </div>
              );
            })}

            {/* General (root-level documents) */}
            <div
              className={`${s.generalSection} ${dragOverKey === '__general__' ? s.folderDragOver : ''}`}
              {...dropProps('__general__', null)}
            >
              <div className={s.generalHeader}>
                <Icon name="Files" size={16} className={s.folderIcon} />
                <span className={s.folderName}>General</span>
                <span className={s.folderCount}>{generalDocs.length}</span>
              </div>
              {generalDocs.length === 0 ? (
                <div className={s.emptyState}>
                  <Icon name="FileSearch" size={40} className={s.emptyIcon} />
                  <div className={s.emptyTitle}>
                    {ragDocsSearchQuery ? 'No matching documents' : 'No documents yet'}
                  </div>
                  <div className={s.emptyText}>
                    {ragDocsSearchQuery ? 'Try a different search term' : 'Upload from the sidebar, then drag files into folders'}
                  </div>
                </div>
              ) : (
                <div className={s.docsList}>{generalDocs.map(renderDocCard)}</div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default RagDocsView;
