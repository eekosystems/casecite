import { create } from 'zustand';
import { api } from '../api';
import { useUIStore } from './uiStore';
import { registerReset } from './resetRegistry';
import { appNavigate } from '../utils/router';
import logger from '../utils/logger';
import type { RagDocument } from '../types';
import type {
  ChatSessionSummary,
  Matter,
  MatterActivityEvent,
  MatterAnalysisItem,
  MatterDetail,
} from '../api/types';

export type MatterTab = 'documents' | 'facts' | 'chats' | 'analyses' | 'members' | 'activity';

/** Longest facts the server accepts (MAX_MATTER_FACTS_CHARS in the backend). */
export const MAX_MATTER_FACTS_CHARS = 20_000;

/** Per-device memory of the matter the header selector is set to. */
const ACTIVE_MATTER_KEY = 'casecite.activeMatterId';

function readStoredActiveMatter(): string | null {
  try {
    return localStorage.getItem(ACTIVE_MATTER_KEY);
  } catch {
    return null;
  }
}

function writeStoredActiveMatter(id: string | null): void {
  try {
    if (id) localStorage.setItem(ACTIVE_MATTER_KEY, id);
    else localStorage.removeItem(ACTIVE_MATTER_KEY);
  } catch {
    /* private mode / blocked storage: the selector still works for the session */
  }
}

const errorMessage = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);

/**
 * True when a document belongs to the matter: filed under it, or (for a
 * personal matter) owned by the matter's owner and not filed anywhere. That
 * second clause is the personal scope documents lived in before matters.
 */
export function documentInMatter(doc: Pick<RagDocument, 'matter_id' | 'user_id'>, matter: Matter): boolean {
  if (doc.matter_id === matter.id) return true;
  return matter.is_personal && !doc.matter_id && doc.user_id === matter.owner_id;
}

export interface MattersState {
  matters: Matter[];
  mattersLoading: boolean;
  _initialized: boolean;
  init: () => void;
  loadMatters: () => Promise<void>;
  createMatter: (name: string, clientName: string | null) => Promise<Matter | null>;
  openMatter: (matterId: string) => void;

  /** Matter the header selector is set to; null = everything the user can see. */
  activeMatterId: string | null;
  setActiveMatter: (matterId: string | null) => void;
  activeMatter: () => Matter | null;
  /** Narrow a document list to the active matter (identity when none is set). */
  scopeDocuments: <T extends Pick<RagDocument, 'matter_id' | 'user_id'>>(docs: T[]) => T[];

  // ── The open workspace (/matters/:id) ──
  workspaceId: string | null;
  workspace: MatterDetail | null;
  workspaceLoading: boolean;
  workspaceError: string | null;
  tab: MatterTab;
  setTab: (tab: MatterTab) => void;
  documents: RagDocument[];
  chats: ChatSessionSummary[];
  analyses: MatterAnalysisItem[];
  activity: MatterActivityEvent[];
  tabLoading: Partial<Record<MatterTab, boolean>>;
  loadWorkspace: (matterId: string) => Promise<void>;
  loadTab: (tab: MatterTab) => Promise<void>;
  renameMatter: (name: string, clientName: string | null) => Promise<boolean>;
  addMember: (email: string) => Promise<boolean>;
  removeMember: (userId: string, label: string) => void;
  transferOwnership: (userId: string, label: string) => void;
  deleteMatter: () => void;
  moveDocumentsHere: (documentIds: string[]) => Promise<boolean>;
  /** Documents the user can file here but that are not here yet (for the move picker). */
  movableDocuments: (allDocs: RagDocument[], currentUserId: string | null) => RagDocument[];

  // ── Facts tab ──
  /** Unsaved edits to the facts; null when there are none. Kept across tab switches. */
  factsDraft: string | null;
  factsSaving: boolean;
  /** The server refused an edit (403): the caller's role can read but not change the facts. */
  factsReadOnly: boolean;
  setFactsDraft: (text: string) => void;
  saveFacts: () => Promise<boolean>;
}

export const useMattersStore = create<MattersState>((set, get) => ({
  matters: [],
  mattersLoading: false,
  _initialized: false,

  init: () => {
    if (get()._initialized) return;
    set({ _initialized: true, activeMatterId: readStoredActiveMatter() });
    get().loadMatters();
  },

  loadMatters: async () => {
    set({ mattersLoading: true });
    try {
      const resp = await api.listMatters();
      const matters = resp.matters || [];
      // A remembered active matter the user no longer belongs to is dropped,
      // otherwise every view would quietly scope to nothing.
      const active = get().activeMatterId;
      const stillThere = active ? matters.some((m) => m.id === active) : false;
      if (active && !stillThere) writeStoredActiveMatter(null);
      set({ matters, activeMatterId: stillThere ? active : null });
    } catch (e: unknown) {
      logger.error('Failed to load matters:', e);
      useUIStore.getState().addToast(`Could not load matters: ${errorMessage(e, 'unknown error')}`, 'error');
    } finally {
      set({ mattersLoading: false });
    }
  },

  createMatter: async (name, clientName) => {
    const trimmed = name.trim();
    if (!trimmed) {
      useUIStore.getState().addToast('Give the matter a name.', 'error');
      return null;
    }
    try {
      const matter = await api.createMatter(trimmed, clientName?.trim() || null);
      set((state) => ({ matters: [...state.matters, matter] }));
      useUIStore.getState().addToast(`Matter "${matter.name}" created`, 'success');
      return matter;
    } catch (e: unknown) {
      useUIStore.getState().addToast(`Could not create matter: ${errorMessage(e, 'unknown error')}`, 'error');
      return null;
    }
  },

  openMatter: (matterId) => {
    appNavigate(`/matters/${encodeURIComponent(matterId)}`);
  },

  activeMatterId: null,
  setActiveMatter: (matterId) => {
    writeStoredActiveMatter(matterId);
    set({ activeMatterId: matterId });
  },
  activeMatter: () => {
    const { activeMatterId, matters } = get();
    return activeMatterId ? matters.find((m) => m.id === activeMatterId) || null : null;
  },
  scopeDocuments: (docs) => {
    const matter = get().activeMatter();
    return matter ? docs.filter((d) => documentInMatter(d, matter)) : docs;
  },

  // ── Workspace ──
  workspaceId: null,
  workspace: null,
  workspaceLoading: false,
  workspaceError: null,
  tab: 'documents',
  setTab: (tab) => {
    set({ tab });
    get().loadTab(tab);
  },
  documents: [],
  chats: [],
  analyses: [],
  activity: [],
  tabLoading: {},

  loadWorkspace: async (matterId) => {
    const changed = get().workspaceId !== matterId;
    set({
      workspaceId: matterId,
      workspaceLoading: true,
      workspaceError: null,
      ...(changed
        ? {
          workspace: null, documents: [], chats: [], analyses: [], activity: [],
          factsDraft: null, factsReadOnly: false,
        }
        : {}),
    });
    try {
      const detail = await api.getMatter(matterId);
      if (get().workspaceId !== matterId) return; // navigated away meanwhile
      set({ workspace: detail });
      await get().loadTab(get().tab);
    } catch (e: unknown) {
      if (get().workspaceId !== matterId) return;
      const status = (e as { status?: number }).status;
      set({
        workspace: null,
        workspaceError: status === 404
          ? 'This matter does not exist, or you are not a member of it.'
          : errorMessage(e, 'The matter could not be loaded.'),
      });
    } finally {
      if (get().workspaceId === matterId) set({ workspaceLoading: false });
    }
  },

  loadTab: async (tab) => {
    const matterId = get().workspaceId;
    if (!matterId || !get().workspace) return;
    set((state) => ({ tabLoading: { ...state.tabLoading, [tab]: true } }));
    try {
      switch (tab) {
        case 'documents': {
          const resp = await api.getMatterDocuments(matterId);
          if (get().workspaceId === matterId) set({ documents: resp.documents || [] });
          break;
        }
        case 'chats': {
          const resp = await api.getMatterChats(matterId);
          if (get().workspaceId === matterId) set({ chats: resp.sessions || [] });
          break;
        }
        case 'analyses': {
          const resp = await api.getMatterAnalyses(matterId);
          if (get().workspaceId === matterId) set({ analyses: resp.analyses || [] });
          break;
        }
        case 'activity': {
          const resp = await api.getMatterActivity(matterId);
          if (get().workspaceId === matterId) set({ activity: resp.events || [] });
          break;
        }
        case 'members':
        case 'facts': {
          // Members and facts arrive with the matter detail; refresh it so they are
          // current. Unsaved facts edits live in factsDraft and are left alone.
          const detail = await api.getMatter(matterId);
          if (get().workspaceId === matterId) set({ workspace: detail });
          break;
        }
      }
    } catch (e: unknown) {
      logger.error(`Failed to load matter ${tab}:`, e);
      useUIStore.getState().addToast(`Could not load ${tab}: ${errorMessage(e, 'unknown error')}`, 'error');
    } finally {
      set((state) => ({ tabLoading: { ...state.tabLoading, [tab]: false } }));
    }
  },

  renameMatter: async (name, clientName) => {
    const { workspaceId } = get();
    if (!workspaceId) return false;
    const trimmed = name.trim();
    if (!trimmed) {
      useUIStore.getState().addToast('A matter needs a name.', 'error');
      return false;
    }
    try {
      const updated = await api.updateMatter(workspaceId, { name: trimmed, client_name: clientName?.trim() ?? '' });
      set((state) => ({
        workspace: state.workspace ? { ...state.workspace, name: updated.name, client_name: updated.client_name } : null,
        matters: state.matters.map((m) => (m.id === updated.id ? { ...m, name: updated.name, client_name: updated.client_name } : m)),
      }));
      useUIStore.getState().addToast('Matter updated', 'success');
      return true;
    } catch (e: unknown) {
      useUIStore.getState().addToast(`Could not update matter: ${errorMessage(e, 'unknown error')}`, 'error');
      return false;
    }
  },

  addMember: async (email) => {
    const { workspaceId } = get();
    const address = email.trim();
    if (!workspaceId || !address) return false;
    try {
      const detail = await api.addMatterMember(workspaceId, address);
      set((state) => ({
        workspace: detail,
        matters: state.matters.map((m) => (m.id === detail.id ? { ...m, member_count: detail.member_count } : m)),
      }));
      useUIStore.getState().addToast(`${address} added to the matter`, 'success');
      return true;
    } catch (e: unknown) {
      useUIStore.getState().addToast(errorMessage(e, 'That person could not be added.'), 'error');
      return false;
    }
  },

  removeMember: (userId, label) => {
    const { workspaceId } = get();
    if (!workspaceId) return;
    useUIStore.getState().showConfirm({
      title: 'Remove member',
      message: `Remove ${label} from this matter? They lose access to every document, chat and analysis filed under it.`,
      type: 'warning',
      confirmText: 'Remove',
      onConfirm: async () => {
        try {
          const detail = await api.removeMatterMember(workspaceId, userId);
          set((state) => ({
            workspace: detail,
            matters: state.matters.map((m) => (m.id === detail.id ? { ...m, member_count: detail.member_count } : m)),
          }));
          useUIStore.getState().addToast(`${label} removed`, 'success');
        } catch (e: unknown) {
          useUIStore.getState().addToast(`Could not remove member: ${errorMessage(e, 'unknown error')}`, 'error');
        }
      },
    });
  },

  transferOwnership: (userId, label) => {
    const { workspaceId } = get();
    if (!workspaceId) return;
    useUIStore.getState().showConfirm({
      title: 'Transfer ownership',
      message: `Make ${label} the owner of this matter? You stay a member, but only the owner can share, rename, transfer or delete the matter and delete what is filed under it.`,
      type: 'warning',
      confirmText: 'Transfer',
      onConfirm: async () => {
        try {
          const detail = await api.transferMatter(workspaceId, userId);
          set((state) => ({
            workspace: detail,
            matters: state.matters.map((m) => (m.id === detail.id ? { ...m, owner_id: detail.owner_id, role: detail.role } : m)),
          }));
          useUIStore.getState().addToast(`${label} now owns this matter`, 'success');
        } catch (e: unknown) {
          useUIStore.getState().addToast(`Could not transfer the matter: ${errorMessage(e, 'unknown error')}`, 'error');
        }
      },
    });
  },

  deleteMatter: () => {
    const { workspaceId, workspace } = get();
    if (!workspaceId || !workspace) return;
    useUIStore.getState().showConfirm({
      title: 'Delete matter',
      message: `Delete "${workspace.name}"? Members lose access. Documents, chats and analyses filed under it are kept and become visible only to the person who created each one.`,
      type: 'danger',
      confirmText: 'Delete',
      onConfirm: async () => {
        try {
          await api.deleteMatter(workspaceId);
          set((state) => ({
            matters: state.matters.filter((m) => m.id !== workspaceId),
            workspaceId: null,
            workspace: null,
            activeMatterId: state.activeMatterId === workspaceId ? null : state.activeMatterId,
          }));
          if (readStoredActiveMatter() === workspaceId) writeStoredActiveMatter(null);
          useUIStore.getState().addToast('Matter deleted', 'success');
          appNavigate('/matters');
        } catch (e: unknown) {
          useUIStore.getState().addToast(`Could not delete the matter: ${errorMessage(e, 'unknown error')}`, 'error');
        }
      },
    });
  },

  moveDocumentsHere: async (documentIds) => {
    const { workspaceId } = get();
    if (!workspaceId || documentIds.length === 0) return false;
    try {
      const resp = await api.moveDocumentsToMatter(workspaceId, documentIds);
      useUIStore.getState().addToast(
        `${resp.moved.length} document${resp.moved.length === 1 ? '' : 's'} filed under this matter`,
        'success',
      );
      await get().loadTab('documents');
      // The Knowledge Base list carries matter_id too; refresh it lazily.
      const { useRagDocsStore } = await import('./ragDocsStore');
      useRagDocsStore.getState().loadRagDocuments();
      return true;
    } catch (e: unknown) {
      useUIStore.getState().addToast(`Could not move documents: ${errorMessage(e, 'unknown error')}`, 'error');
      return false;
    }
  },

  factsDraft: null,
  factsSaving: false,
  factsReadOnly: false,

  setFactsDraft: (text) => {
    const saved = get().workspace?.facts ?? '';
    set({ factsDraft: text === saved ? null : text });
  },

  saveFacts: async () => {
    const { workspaceId, factsDraft } = get();
    if (!workspaceId || factsDraft === null) return false;
    if (factsDraft.length > MAX_MATTER_FACTS_CHARS) {
      useUIStore.getState().addToast(
        `The facts are too long: ${MAX_MATTER_FACTS_CHARS.toLocaleString()} characters at most.`,
        'error',
      );
      return false;
    }
    set({ factsSaving: true });
    try {
      const saved = await api.setMatterFacts(workspaceId, factsDraft);
      if (get().workspaceId !== workspaceId) return false; // navigated away meanwhile
      set((state) => ({
        workspace: state.workspace ? { ...state.workspace, ...saved } : null,
        factsDraft: null,
      }));
      useUIStore.getState().addToast(
        saved.facts ? 'Facts saved. Chats in this matter now use them.' : 'Facts cleared',
        'success',
      );
      return true;
    } catch (e: unknown) {
      if ((e as { status?: number }).status === 403) {
        set({ factsReadOnly: true });
        useUIStore.getState().addToast('Your role can read these facts but not change them.', 'error');
      } else {
        useUIStore.getState().addToast(`Could not save the facts: ${errorMessage(e, 'unknown error')}`, 'error');
      }
      return false;
    } finally {
      set({ factsSaving: false });
    }
  },

  movableDocuments: (allDocs, currentUserId) => {
    const { workspace, documents, matters } = get();
    if (!workspace) return [];
    const here = new Set(documents.map((d) => d.id));
    const owned = new Set(matters.filter((m) => m.role === 'owner').map((m) => m.id));
    return allDocs.filter(
      (d) =>
        !here.has(d.id) &&
        (d.user_id === currentUserId || (!!d.matter_id && owned.has(d.matter_id))),
    );
  },
}));

registerReset(() => {
  writeStoredActiveMatter(null);
  useMattersStore.setState(useMattersStore.getInitialState(), true);
});
