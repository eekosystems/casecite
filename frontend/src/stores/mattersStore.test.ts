import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api', () => ({
  api: {
    listMatters: vi.fn(),
    createMatter: vi.fn(),
    getMatter: vi.fn(),
    updateMatter: vi.fn(),
    deleteMatter: vi.fn(),
    addMatterMember: vi.fn(),
    removeMatterMember: vi.fn(),
    transferMatter: vi.fn(),
    getMatterDocuments: vi.fn(),
    moveDocumentsToMatter: vi.fn(),
    getMatterChats: vi.fn(),
    getMatterAnalyses: vi.fn(),
    getMatterActivity: vi.fn(),
    setMatterFacts: vi.fn(),
    getAllDocuments: vi.fn().mockResolvedValue([]),
    listFolders: vi.fn().mockResolvedValue({ folders: [] }),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

const navigate = vi.fn();
vi.mock('../utils/router', () => ({ appNavigate: (to: string) => navigate(to) }));

import { useMattersStore, documentInMatter } from './mattersStore';
import { useUIStore } from './uiStore';
import { api } from '../api';

const personal = {
  id: 'p1', name: 'Personal', client_name: null, is_personal: true,
  owner_id: 'me', role: 'owner', member_count: 1, created_at: '2026-01-01T00:00:00Z',
};
const shared = {
  id: 'm1', name: 'Acme v. Beta', client_name: 'Acme', is_personal: false,
  owner_id: 'me', role: 'owner', member_count: 2, created_at: '2026-02-01T00:00:00Z',
};
const detail = {
  ...shared,
  members: [
    { user_id: 'me', email: 'me@firm.com', name: 'Me', role: 'owner' },
    { user_id: 'col', email: 'col@firm.com', name: 'Colleague', role: 'member' },
  ],
};

describe('mattersStore', () => {
  beforeEach(() => {
    useMattersStore.setState(useMattersStore.getInitialState(), true);
    useUIStore.setState(useUIStore.getInitialState(), true);
    vi.clearAllMocks();
    localStorage.clear();
    (api.listMatters as any).mockResolvedValue({ matters: [personal, shared] });
  });

  // ==================== documentInMatter ====================

  it('documentInMatter files a tagged document under its matter only', () => {
    expect(documentInMatter({ matter_id: 'm1', user_id: 'me' }, shared as any)).toBe(true);
    expect(documentInMatter({ matter_id: 'm1', user_id: 'me' }, personal as any)).toBe(false);
  });

  it('documentInMatter puts the owner\'s untagged documents in their personal matter', () => {
    expect(documentInMatter({ matter_id: null, user_id: 'me' }, personal as any)).toBe(true);
    expect(documentInMatter({ matter_id: null, user_id: 'col' }, personal as any)).toBe(false);
    expect(documentInMatter({ matter_id: null, user_id: 'me' }, shared as any)).toBe(false);
  });

  // ==================== init / loadMatters ====================

  it('init loads matters once and restores the remembered active matter', async () => {
    localStorage.setItem('casecite.activeMatterId', 'm1');
    useMattersStore.getState().init();
    useMattersStore.getState().init();
    await vi.waitFor(() => expect(useMattersStore.getState().matters).toHaveLength(2));
    expect(api.listMatters).toHaveBeenCalledTimes(1);
    expect(useMattersStore.getState().activeMatterId).toBe('m1');
    expect(useMattersStore.getState().activeMatter()?.name).toBe('Acme v. Beta');
  });

  it('loadMatters drops a remembered active matter the user no longer belongs to', async () => {
    localStorage.setItem('casecite.activeMatterId', 'gone');
    useMattersStore.getState().init();
    await vi.waitFor(() => expect(useMattersStore.getState().matters).toHaveLength(2));
    expect(useMattersStore.getState().activeMatterId).toBeNull();
    expect(localStorage.getItem('casecite.activeMatterId')).toBeNull();
  });

  it('loadMatters toasts on failure', async () => {
    (api.listMatters as any).mockRejectedValue(new Error('boom'));
    await useMattersStore.getState().loadMatters();
    expect(useUIStore.getState().toasts.some((t) => t.message.includes('boom'))).toBe(true);
  });

  // ==================== active matter ====================

  it('setActiveMatter persists the choice and scopeDocuments narrows to it', () => {
    useMattersStore.setState({ matters: [personal, shared] });
    useMattersStore.getState().setActiveMatter('m1');
    expect(localStorage.getItem('casecite.activeMatterId')).toBe('m1');
    const docs = [
      { id: 'a', matter_id: 'm1', user_id: 'col' },
      { id: 'b', matter_id: null, user_id: 'me' },
    ];
    expect(useMattersStore.getState().scopeDocuments(docs).map((d) => d.id)).toEqual(['a']);
    useMattersStore.getState().setActiveMatter(null);
    expect(useMattersStore.getState().scopeDocuments(docs)).toHaveLength(2);
    expect(localStorage.getItem('casecite.activeMatterId')).toBeNull();
  });

  // ==================== createMatter / openMatter ====================

  it('createMatter adds the matter and refuses a blank name', async () => {
    (api.createMatter as any).mockResolvedValue({ ...shared, id: 'm2', name: 'New' });
    expect(await useMattersStore.getState().createMatter('   ', null)).toBeNull();
    expect(api.createMatter).not.toHaveBeenCalled();
    const created = await useMattersStore.getState().createMatter(' New ', ' Client ');
    expect(api.createMatter).toHaveBeenCalledWith('New', 'Client');
    expect(created?.id).toBe('m2');
    expect(useMattersStore.getState().matters.map((m) => m.id)).toContain('m2');
  });

  it('openMatter navigates to the workspace route', () => {
    useMattersStore.getState().openMatter('m1');
    expect(navigate).toHaveBeenCalledWith('/matters/m1');
  });

  // ==================== workspace ====================

  it('loadWorkspace fetches the detail then the current tab', async () => {
    (api.getMatter as any).mockResolvedValue(detail);
    (api.getMatterDocuments as any).mockResolvedValue({ documents: [{ id: 'd1', filename: 'a.pdf' }] });
    await useMattersStore.getState().loadWorkspace('m1');
    const st = useMattersStore.getState();
    expect(st.workspace?.members).toHaveLength(2);
    expect(st.documents.map((d) => d.id)).toEqual(['d1']);
    expect(st.workspaceLoading).toBe(false);
    expect(st.workspaceError).toBeNull();
  });

  it('loadWorkspace reports a 404 as not-a-member', async () => {
    (api.getMatter as any).mockRejectedValue(Object.assign(new Error('Not found'), { status: 404 }));
    await useMattersStore.getState().loadWorkspace('nope');
    expect(useMattersStore.getState().workspace).toBeNull();
    expect(useMattersStore.getState().workspaceError).toMatch(/not a member/);
  });

  it('setTab loads that tab\'s data', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any });
    (api.getMatterChats as any).mockResolvedValue({ sessions: [{ id: 's1', title: 'T', message_count: 2, user_id: 'col' }] });
    (api.getMatterActivity as any).mockResolvedValue({ events: [{ id: 1, action: 'matter_created' }] });
    useMattersStore.getState().setTab('chats');
    await vi.waitFor(() => expect(useMattersStore.getState().chats).toHaveLength(1));
    useMattersStore.getState().setTab('activity');
    await vi.waitFor(() => expect(useMattersStore.getState().activity).toHaveLength(1));
    expect(useMattersStore.getState().tab).toBe('activity');
  });

  // ==================== facts ====================

  it('setFactsDraft tracks edits and drops them when the text matches what is saved', () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: { ...detail, facts: 'Saved facts' } as any });
    useMattersStore.getState().setFactsDraft('Saved facts, edited');
    expect(useMattersStore.getState().factsDraft).toBe('Saved facts, edited');
    useMattersStore.getState().setFactsDraft('Saved facts');
    expect(useMattersStore.getState().factsDraft).toBeNull();
  });

  it('saveFacts sends the draft, updates the workspace and clears the draft', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: { ...detail, facts: null } as any, factsDraft: 'Lease signed 1 March.' });
    (api.setMatterFacts as any).mockResolvedValue({
      facts: 'Lease signed 1 March.', facts_updated_at: '2026-11-01T02:00:00', facts_updated_by: 'me',
    });
    expect(await useMattersStore.getState().saveFacts()).toBe(true);
    expect(api.setMatterFacts).toHaveBeenCalledWith('m1', 'Lease signed 1 March.');
    const st = useMattersStore.getState();
    expect(st.workspace?.facts).toBe('Lease signed 1 March.');
    expect(st.workspace?.facts_updated_by).toBe('me');
    expect(st.factsDraft).toBeNull();
    expect(st.factsSaving).toBe(false);
    expect(useUIStore.getState().toasts.slice(-1)[0]?.type).toBe('success');
  });

  it('saveFacts turns read-only when the server refuses the edit (403)', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, factsDraft: 'New facts' });
    (api.setMatterFacts as any).mockRejectedValue(Object.assign(new Error('Permission denied'), { status: 403 }));
    expect(await useMattersStore.getState().saveFacts()).toBe(false);
    const st = useMattersStore.getState();
    expect(st.factsReadOnly).toBe(true);
    expect(st.factsDraft).toBe('New facts'); // the text is not thrown away
    expect(useUIStore.getState().toasts.slice(-1)[0]?.message).toMatch(/can read these facts but not change them/);
  });

  it('saveFacts shows the server message on other failures and stays editable', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, factsDraft: 'New facts' });
    (api.setMatterFacts as any).mockRejectedValue(new Error('Database is locked'));
    expect(await useMattersStore.getState().saveFacts()).toBe(false);
    expect(useMattersStore.getState().factsReadOnly).toBe(false);
    expect(useUIStore.getState().toasts.slice(-1)[0]?.message).toMatch(/Database is locked/);
  });

  it('saveFacts does nothing without edits', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, factsDraft: null });
    expect(await useMattersStore.getState().saveFacts()).toBe(false);
    expect(api.setMatterFacts).not.toHaveBeenCalled();
  });

  it('opening another matter drops the previous draft and read-only state', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, factsDraft: 'x', factsReadOnly: true });
    (api.getMatter as any).mockResolvedValue({ ...detail, id: 'm2' });
    (api.getMatterDocuments as any).mockResolvedValue({ documents: [] });
    await useMattersStore.getState().loadWorkspace('m2');
    expect(useMattersStore.getState().factsDraft).toBeNull();
    expect(useMattersStore.getState().factsReadOnly).toBe(false);
  });

  it('the facts tab refreshes the matter so the facts are current, keeping a draft', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, factsDraft: 'mine' });
    (api.getMatter as any).mockResolvedValue({ ...detail, facts: 'Edited by a colleague' });
    useMattersStore.getState().setTab('facts');
    await vi.waitFor(() => expect(useMattersStore.getState().workspace?.facts).toBe('Edited by a colleague'));
    expect(useMattersStore.getState().factsDraft).toBe('mine');
  });

  it('renameMatter updates the workspace and the list', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, matters: [personal, shared] });
    (api.updateMatter as any).mockResolvedValue({ ...shared, name: 'Acme v. Gamma', client_name: null });
    expect(await useMattersStore.getState().renameMatter(' Acme v. Gamma ', '')).toBe(true);
    expect(api.updateMatter).toHaveBeenCalledWith('m1', { name: 'Acme v. Gamma', client_name: '' });
    expect(useMattersStore.getState().workspace?.name).toBe('Acme v. Gamma');
    expect(useMattersStore.getState().matters.find((m) => m.id === 'm1')?.client_name).toBeNull();
  });

  it('addMember refreshes the members from the response', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: { ...detail, members: [detail.members[0]] } as any, matters: [shared] });
    (api.addMatterMember as any).mockResolvedValue(detail);
    expect(await useMattersStore.getState().addMember(' col@firm.com ')).toBe(true);
    expect(api.addMatterMember).toHaveBeenCalledWith('m1', 'col@firm.com');
    expect(useMattersStore.getState().workspace?.members).toHaveLength(2);
    expect(useMattersStore.getState().matters[0].member_count).toBe(2);
  });

  it('removeMember and transferOwnership go through the confirm dialog', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, matters: [shared] });
    (api.removeMatterMember as any).mockResolvedValue({ ...detail, members: [detail.members[0]], member_count: 1 });
    useMattersStore.getState().removeMember('col', 'Colleague');
    expect(api.removeMatterMember).not.toHaveBeenCalled();
    expect(useUIStore.getState().confirmModal.open).toBe(true);
    useUIStore.getState().handleConfirm();
    await vi.waitFor(() => expect(api.removeMatterMember).toHaveBeenCalledWith('m1', 'col'));
    await vi.waitFor(() => expect(useMattersStore.getState().workspace?.members).toHaveLength(1));

    (api.transferMatter as any).mockResolvedValue({ ...detail, owner_id: 'col', role: 'member' });
    useMattersStore.getState().transferOwnership('col', 'Colleague');
    useUIStore.getState().handleConfirm();
    await vi.waitFor(() => expect(api.transferMatter).toHaveBeenCalledWith('m1', 'col'));
    await vi.waitFor(() => expect(useMattersStore.getState().workspace?.owner_id).toBe('col'));
    expect(useMattersStore.getState().matters[0].role).toBe('member');
  });

  it('deleteMatter confirms, removes the matter, clears the active scope and navigates', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any, matters: [personal, shared], activeMatterId: 'm1' });
    (api.deleteMatter as any).mockResolvedValue({ status: 'deleted', id: 'm1' });
    useMattersStore.getState().deleteMatter();
    useUIStore.getState().handleConfirm();
    await vi.waitFor(() => expect(api.deleteMatter).toHaveBeenCalledWith('m1'));
    await vi.waitFor(() => expect(useMattersStore.getState().matters.map((m) => m.id)).toEqual(['p1']));
    expect(useMattersStore.getState().activeMatterId).toBeNull();
    expect(navigate).toHaveBeenCalledWith('/matters');
  });

  it('moveDocumentsHere moves, toasts and reloads the documents tab', async () => {
    useMattersStore.setState({ workspaceId: 'm1', workspace: detail as any });
    (api.moveDocumentsToMatter as any).mockResolvedValue({ status: 'moved', matter_id: 'm1', moved: ['d1', 'd2'] });
    (api.getMatterDocuments as any).mockResolvedValue({ documents: [{ id: 'd1' }, { id: 'd2' }] });
    expect(await useMattersStore.getState().moveDocumentsHere(['d1', 'd2'])).toBe(true);
    expect(api.moveDocumentsToMatter).toHaveBeenCalledWith('m1', ['d1', 'd2']);
    expect(useMattersStore.getState().documents).toHaveLength(2);
    expect(useUIStore.getState().toasts.some((t) => t.message.includes('2 documents'))).toBe(true);
  });

  it('movableDocuments offers own documents and ones in matters the user owns, not ones already here', () => {
    useMattersStore.setState({
      workspace: detail as any,
      documents: [{ id: 'here' }] as any,
      matters: [{ ...shared, id: 'owned', role: 'owner' }, { ...shared, id: 'joined', role: 'member' }],
    });
    const all = [
      { id: 'here', user_id: 'me', matter_id: 'm1' },
      { id: 'mine', user_id: 'me', matter_id: null },
      { id: 'in-owned', user_id: 'col', matter_id: 'owned' },
      { id: 'in-joined', user_id: 'col', matter_id: 'joined' },
      { id: 'theirs', user_id: 'col', matter_id: null },
    ] as any;
    expect(useMattersStore.getState().movableDocuments(all, 'me').map((d: any) => d.id)).toEqual(['mine', 'in-owned']);
  });
});
