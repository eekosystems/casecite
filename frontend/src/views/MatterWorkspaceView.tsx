import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useMattersStore, type MatterTab } from '../stores/mattersStore';
import { useAuthStore } from '../stores/authStore';
import { useRagDocsStore } from '../stores/ragDocsStore';
import { useContractsStore } from '../stores/contractsStore';
import { useResearch } from '../contexts/ResearchContext';
import { appNavigate } from '../utils/router';
import {
  Icon,
  MatterDocumentsTab,
  MatterChatsTab,
  MatterAnalysesTab,
  MatterMembersTab,
  MatterActivityTab,
} from '../components';
import s from './MatterWorkspaceView.module.css';

const TABS: Array<{ id: MatterTab; label: string; icon: string }> = [
  { id: 'documents', label: 'Documents', icon: 'FileText' },
  { id: 'chats', label: 'Chats', icon: 'MessageSquare' },
  { id: 'analyses', label: 'Analyses', icon: 'FileCheck' },
  { id: 'members', label: 'Members', icon: 'Users' },
  { id: 'activity', label: 'Activity', icon: 'History' },
];

/** One matter: what is filed under it, who can see it, and what happened. */
function MatterWorkspaceView() {
  const { id } = useParams<{ id: string }>();
  const {
    workspace, workspaceLoading, workspaceError, tab, setTab,
    documents, chats, analyses, activity, tabLoading,
    loadWorkspace, renameMatter, addMember, removeMember, transferOwnership, deleteMatter,
    moveDocumentsHere, movableDocuments, activeMatterId, setActiveMatter, init,
  } = useMattersStore();
  const currentUserId = useAuthStore((st) => st.user?.id ?? null);
  const ragDocs = useRagDocsStore((st) => st.ragDocs);
  const ragInit = useRagDocsStore((st) => st.init);
  const openAnalysis = useContractsStore((st) => st.openAnalysis);
  const { loadSession } = useResearch();

  useEffect(() => { init(); ragInit(); }, [init, ragInit]);
  useEffect(() => { if (id) loadWorkspace(id); }, [id, loadWorkspace]);

  // Local UI state: the rename form only.
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState('');
  const [client, setClient] = useState('');

  const startRename = () => {
    if (!workspace) return;
    setName(workspace.name);
    setClient(workspace.client_name || '');
    setRenaming(true);
  };
  const submitRename = async (e: React.FormEvent) => {
    e.preventDefault();
    if (await renameMatter(name, client)) setRenaming(false);
  };

  if (workspaceError) {
    return (
      <div className={s.container}>
        <div className={s.content}>
          <button className={s.backBtn} onClick={() => appNavigate('/matters')}>
            <Icon name="ArrowLeft" size={14} /> All matters
          </button>
          <div className={s.empty}>
            <Icon name="AlertCircle" size={18} />
            <p>{workspaceError}</p>
          </div>
        </div>
      </div>
    );
  }

  if (!workspace) {
    return (
      <div className={s.container}>
        <div className={s.content}>
          <div className={s.empty}><Icon name="Loader2" size={16} className={s.spin} /> Loading matter…</div>
        </div>
      </div>
    );
  }

  const isOwner = workspace.role === 'owner';
  const isActive = activeMatterId === workspace.id;
  const title = workspace.is_personal ? 'Personal' : workspace.name;

  return (
    <div className={s.container}>
      <div className={s.content}>
        <button className={s.backBtn} onClick={() => appNavigate('/matters')}>
          <Icon name="ArrowLeft" size={14} /> All matters
        </button>

        <div className={s.headerRow}>
          <div className={s.headerMain}>
            <div className={s.titleRow}>
              <Icon name={workspace.is_personal ? 'User' : 'Briefcase'} size={20} className={s.titleIcon} />
              <h2 className={s.title}>{title}</h2>
              <span className={`${s.roleBadge} ${isOwner ? s.roleOwner : ''}`}>
                {workspace.is_personal ? 'private' : workspace.role}
              </span>
              {isActive && (
                <span className={s.activeBadge} title="Research, Contracts and Drafting are scoped to this matter">
                  <Icon name="CheckCircle2" size={11} /> Active matter
                </span>
              )}
              {workspaceLoading && <Icon name="Loader2" size={14} className={s.spin} />}
            </div>
            <div className={s.meta}>
              {workspace.client_name && <span>Client: {workspace.client_name}</span>}
              {!workspace.is_personal && (
                <span>{workspace.member_count} member{workspace.member_count === 1 ? '' : 's'}</span>
              )}
              <span>Created {new Date(workspace.created_at).toLocaleDateString()}</span>
            </div>
          </div>
          <div className={s.headerActions}>
            <button
              className={s.secondaryBtn}
              onClick={() => setActiveMatter(isActive ? null : workspace.id)}
              title={isActive ? 'Stop scoping the workspace to this matter' : 'Scope Research, Contracts and Drafting to this matter'}
            >
              <Icon name={isActive ? 'X' : 'Target'} size={14} />
              {isActive ? 'Clear active matter' : 'Set as active'}
            </button>
            {isOwner && !workspace.is_personal && (
              <>
                <button className={s.secondaryBtn} onClick={renaming ? () => setRenaming(false) : startRename}>
                  <Icon name="Pencil" size={14} /> {renaming ? 'Cancel' : 'Rename'}
                </button>
                <button className={s.dangerBtn} onClick={deleteMatter}>
                  <Icon name="Trash2" size={14} /> Delete
                </button>
              </>
            )}
          </div>
        </div>

        {renaming && (
          <form className={s.renameForm} onSubmit={submitRename}>
            <input className={s.input} value={name} onChange={(e) => setName(e.target.value)} aria-label="Matter name" autoFocus />
            <input className={s.input} value={client} onChange={(e) => setClient(e.target.value)} placeholder="Client (optional)" aria-label="Client name" />
            <button type="submit" className={s.primaryBtn} disabled={!name.trim()}>
              <Icon name="Check" size={14} /> Save
            </button>
          </form>
        )}

        <div className={s.tabs} role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              className={`${s.tab} ${tab === t.id ? s.tabActive : ''}`}
              onClick={() => setTab(t.id)}
            >
              <Icon name={t.icon} size={14} />
              {t.label}
              {t.id === 'documents' && documents.length > 0 && <span className={s.tabCountBadge}>{documents.length}</span>}
              {t.id === 'members' && !workspace.is_personal && <span className={s.tabCountBadge}>{workspace.member_count}</span>}
            </button>
          ))}
        </div>

        <div className={s.tabPanel} role="tabpanel">
          {tab === 'documents' && (
            <MatterDocumentsTab
              documents={documents}
              loading={!!tabLoading.documents}
              currentUserId={currentUserId}
              movable={movableDocuments(ragDocs, currentUserId)}
              onMove={moveDocumentsHere}
              onOpenKnowledgeBase={() => appNavigate('/documents')}
              styles={s}
            />
          )}
          {tab === 'chats' && (
            <MatterChatsTab
              chats={chats}
              loading={!!tabLoading.chats}
              currentUserId={currentUserId}
              onOpen={(sessionId) => { loadSession(sessionId); appNavigate('/research'); }}
              styles={s}
            />
          )}
          {tab === 'analyses' && (
            <MatterAnalysesTab
              analyses={analyses}
              loading={!!tabLoading.analyses}
              currentUserId={currentUserId}
              onOpen={(analysisId) => { openAnalysis(analysisId); appNavigate('/contracts'); }}
              styles={s}
            />
          )}
          {tab === 'members' && (
            <MatterMembersTab
              members={workspace.members}
              isOwner={isOwner}
              isPersonal={workspace.is_personal}
              currentUserId={currentUserId}
              onAdd={addMember}
              onRemove={removeMember}
              onTransfer={transferOwnership}
              styles={s}
            />
          )}
          {tab === 'activity' && (
            <MatterActivityTab events={activity} loading={!!tabLoading.activity} styles={s} />
          )}
        </div>
      </div>
    </div>
  );
}

export default MatterWorkspaceView;
