import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api', () => ({
  api: {
    getConnectors: vi.fn(),
    getPickerConfig: vi.fn(),
    connectConnector: vi.fn(),
    disconnectConnector: vi.fn(),
    syncConnector: vi.fn(),
    listConnectorFolders: vi.fn(),
    getConnectorStatus: vi.fn(),
    importFromGooglePicker: vi.fn(),
    importFromOneDrivePicker: vi.fn(),
    importFromBoxPicker: vi.fn(),
    importFromDropboxChooser: vi.fn(),
    getDocuments: vi.fn(),
    getAllDocuments: vi.fn().mockResolvedValue([]),
    getConnectorSyncStatus: vi.fn(),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

import { useConnectorsStore } from './connectorsStore';
import { usePickerStore } from './pickerStore';
import { useUIStore } from './uiStore';
import { useDocumentsStore } from './documentsStore';
import { useAuthStore } from './authStore';
import { api } from '../api';

describe('connectorsStore', () => {
  beforeEach(() => {
    useConnectorsStore.setState(useConnectorsStore.getInitialState(), true);
    usePickerStore.setState(usePickerStore.getInitialState(), true);
    useUIStore.setState(useUIStore.getInitialState(), true);
    useDocumentsStore.setState(useDocumentsStore.getInitialState(), true);
    vi.clearAllMocks();
  });

  // ==================== Initial State ====================

  it('has correct initial state', () => {
    const state = useConnectorsStore.getState();
    expect(state.connectors).toHaveLength(8);
    expect(state.connectors[0].id).toBe('google_drive');
    expect(state.connectors[0].connected).toBe(false);
    expect(state.pickerConfig).toBeNull();
    expect(state.showPickerModal).toBe(false);
    expect(state.pickerLoading).toBe(false);
    expect(state.activePickerProvider).toBeNull();
  });

  // ==================== Setters ====================

  it('setConnectors replaces connectors with array', () => {
    const newConnectors = [{ id: 'test', name: 'Test', icon: 'X', color: '#000', connected: true, docs: 5 }];
    useConnectorsStore.getState().setConnectors(newConnectors as any);
    expect(useConnectorsStore.getState().connectors).toEqual(newConnectors);
  });

  it('setConnectors supports functional update', () => {
    useConnectorsStore.getState().setConnectors((prev) => prev.map(c => c.id === 'google_drive' ? { ...c, connected: true } : c));
    const google = useConnectorsStore.getState().connectors.find(c => c.id === 'google_drive');
    expect(google?.connected).toBe(true);
  });

  it('setPickerConfig updates pickerConfig', () => {
    const config = { google_enabled: true, microsoft_enabled: false } as any;
    useConnectorsStore.getState().setPickerConfig(config);
    expect(useConnectorsStore.getState().pickerConfig).toEqual(config);
  });

  it('setShowPickerModal updates showPickerModal', () => {
    useConnectorsStore.getState().setShowPickerModal(true);
    expect(useConnectorsStore.getState().showPickerModal).toBe(true);
  });

  it('setPickerLoading updates pickerLoading', () => {
    useConnectorsStore.getState().setPickerLoading(true);
    expect(useConnectorsStore.getState().pickerLoading).toBe(true);
  });

  it('setActivePickerProvider updates activePickerProvider', () => {
    useConnectorsStore.getState().setActivePickerProvider('google_drive');
    expect(useConnectorsStore.getState().activePickerProvider).toBe('google_drive');
  });

  // ==================== hasPickerSupport ====================

  it('hasPickerSupport returns false when pickerConfig is null', () => {
    expect(useConnectorsStore.getState().hasPickerSupport('google_drive')).toBe(false);
  });

  it('hasPickerSupport returns true for enabled provider', () => {
    useConnectorsStore.setState({
      pickerConfig: { google_enabled: true, microsoft_enabled: false } as any,
    });
    expect(useConnectorsStore.getState().hasPickerSupport('google_drive')).toBe(true);
    expect(useConnectorsStore.getState().hasPickerSupport('onedrive')).toBe(false);
  });

  it('hasPickerSupport returns false for unknown provider', () => {
    useConnectorsStore.setState({
      pickerConfig: { google_enabled: true, microsoft_enabled: true } as any,
    });
    expect(useConnectorsStore.getState().hasPickerSupport('imanage')).toBe(false);
  });

  // ==================== handlePickerFilesSelected ====================

  it('handlePickerFilesSelected does nothing for empty files', async () => {
    await useConnectorsStore.getState().handlePickerFilesSelected([], 'google');
    expect(api.importFromGooglePicker).not.toHaveBeenCalled();
  });

  it('handlePickerFilesSelected imports files from google and shows toast', async () => {
    (api.importFromGooglePicker as ReturnType<typeof vi.fn>).mockResolvedValue({ imported: 2, failed: 0 });
    (api.getAllDocuments as ReturnType<typeof vi.fn>).mockResolvedValue([{ id: '1', filename: 'a.pdf' }]);

    const files = [{ id: '1', name: 'test.pdf' }] as any;
    await useConnectorsStore.getState().handlePickerFilesSelected(files, 'google');

    expect(api.importFromGooglePicker).toHaveBeenCalled();
    expect(useConnectorsStore.getState().pickerLoading).toBe(false);
  });

  it('handlePickerFilesSelected shows error toast for failed imports', async () => {
    (api.importFromOneDrivePicker as ReturnType<typeof vi.fn>).mockResolvedValue({ imported: 1, failed: 2, errors: [] });
    (api.getAllDocuments as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    const files = [{ id: '1', name: 'test.pdf' }] as any;
    await useConnectorsStore.getState().handlePickerFilesSelected(files, 'onedrive');

    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.message.includes('Failed to import'))).toBe(true);
  });

  it('handlePickerFilesSelected handles unknown provider', async () => {
    const files = [{ id: '1', name: 'test.pdf' }] as any;
    await useConnectorsStore.getState().handlePickerFilesSelected(files, 'unknown_provider');

    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.message.includes('Failed to import files'))).toBe(true);
    expect(useConnectorsStore.getState().pickerLoading).toBe(false);
  });

  it('shows imported files as sidebar chips in the shape the panel renders, then lets them go', async () => {
    vi.useFakeTimers();
    try {
      (api.importFromBoxPicker as ReturnType<typeof vi.fn>).mockResolvedValue({ imported: 1, failed: 0, errors: [] });
      (api.getAllDocuments as ReturnType<typeof vi.fn>).mockResolvedValue([
        { id: 'old', filename: 'old.pdf', size: 10, content_type: 'application/pdf', status: 'indexed', chunk_count: 2, created_at: '2026-01-01T00:00:00Z' },
        { id: 'new', filename: 'new.pdf', size: 20, content_type: 'application/pdf', status: 'indexed', chunk_count: 7, created_at: '2026-03-01T00:00:00Z' },
      ]);
      useDocumentsStore.setState({ documents: [{ id: 'pending', name: 'typing.pdf', status: 'error' }] });

      await usePickerStore.getState().handlePickerFilesSelected([{ id: 'f1', name: 'new.pdf' }] as any, 'box');

      const docs = useDocumentsStore.getState().documents;
      expect(docs).toHaveLength(2);
      // Only the newly imported file joins the existing rows — not the whole Knowledge Base.
      expect(docs[1]).toMatchObject({ id: 'new', name: 'new.pdf', status: 'indexed', chunks: 7, size: 20 });
      expect(docs[1]).not.toHaveProperty('filename');

      // Like an upload, the confirmation chip vanishes on its own.
      await vi.advanceTimersByTimeAsync(2500);
      expect(useDocumentsStore.getState().documents.map((d) => d.id)).toEqual(['pending']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('names the files that failed to import, and how many more there were', async () => {
    (api.importFromGooglePicker as ReturnType<typeof vi.fn>).mockResolvedValue({
      imported: 0,
      failed: 5,
      errors: [
        { name: 'a.pdf', reason: 'too large' },
        { name: 'b.docx', reason: 'unsupported type' },
        { name: 'c.pdf', reason: 'download failed' },
        { name: 'd.pdf', reason: 'download failed' },
      ],
    });

    await useConnectorsStore.getState().handlePickerFilesSelected([{ id: '1', name: 'a.pdf' }] as any, 'google');

    const toast = useUIStore.getState().toasts.find((t) => t.message.startsWith('Failed to import 5 file(s)'));
    expect(toast?.message).toBe(
      'Failed to import 5 file(s): a.pdf: too large; b.docx: unsupported type; c.pdf: download failed; and 2 more',
    );
    expect(api.getAllDocuments).not.toHaveBeenCalled();
  });

  it('openGooglePicker names the missing settings instead of opening a broken picker', async () => {
    useConnectorsStore.getState().setPickerConfig({
      google_enabled: true,
      google_configured: false,
      google_missing: ['google_api_key', 'google_app_id'],
      microsoft_enabled: false,
    });

    await usePickerStore.getState().openGooglePicker();

    const toast = useUIStore.getState().toasts.find((t) => t.message.includes('Google Picker'));
    expect(toast?.message).toContain('Missing: google_api_key, google_app_id');
    expect(toast?.type).toBe('error');
    expect(usePickerStore.getState().pickerLoading).toBe(false);
    expect(document.querySelector('script[src*="apis.google.com"]')).toBeNull();
  });

  it('pollSyncStatus reports the per-file failures of a finished sync', async () => {
    vi.useFakeTimers();
    try {
      (api.getConnectorSyncStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
        status: 'completed',
        result: {
          status: 'completed', docs_processed: 3, docs_skipped: 0, docs_total: 5, errors_count: 2,
          errors: [{ name: 'x.pdf', reason: 'too large' }, { name: 'y.pdf', reason: 'empty file' }],
        },
      });
      (api.getConnectors as ReturnType<typeof vi.fn>).mockResolvedValue({ connectors: [] });
      (api.getPickerConfig as ReturnType<typeof vi.fn>).mockResolvedValue({});

      useConnectorsStore.getState().pollSyncStatus({ id: 'box', name: 'Box' } as any, 'sync-1');
      await vi.advanceTimersByTimeAsync(3000);

      const messages = useUIStore.getState().toasts.map((t) => t.message);
      expect(api.getConnectorSyncStatus).toHaveBeenCalledWith('box', 'sync-1');
      expect(messages).toContain('Box sync complete — 3 documents indexed.');
      expect(messages).toContain('2 files could not be indexed: x.pdf: too large; y.pdf: empty file');
    } finally {
      vi.useRealTimers();
    }
  });

  // ==================== handleConnectorClick ====================

  it('handleConnectorClick shows error for unconfigured connector', async () => {
    const connector = { id: 'imanage', name: 'iManage', configured: false, connected: false } as any;
    await useConnectorsStore.getState().handleConnectorClick(connector);

    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.message.includes('not configured'))).toBe(true);
  });

  it('handleConnectorClick opens picker modal for supported picker provider', async () => {
    useConnectorsStore.setState({
      pickerConfig: { google_enabled: true, microsoft_enabled: false } as any,
    });

    const connector = { id: 'google_drive', name: 'Google Drive', configured: true, connected: false } as any;
    await useConnectorsStore.getState().handleConnectorClick(connector);

    expect(useConnectorsStore.getState().activePickerProvider).toBe('google_drive');
    expect(useConnectorsStore.getState().showPickerModal).toBe(true);
  });

  it('offers the folder picker for a connector that can sync one folder', async () => {
    const connector = { id: 'box', name: 'Box', configured: true, connected: true, supportsFolderSync: true } as any;
    await useConnectorsStore.getState().handleConnectorClick(connector);
    const manage = useUIStore.getState().confirmModal;
    expect(manage.title).toBe('Manage Box');
    expect(manage.confirmText).toBe('Choose folder…');

    useUIStore.getState().handleConfirm();
    expect(useConnectorsStore.getState().folderPicker?.connector.id).toBe('box');
    expect(api.syncConnector).not.toHaveBeenCalled();
  });

  it('syncFolder starts a sync scoped to that folder and closes the picker', async () => {
    (api.syncConnector as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 'started', sync_id: 's-1' });
    const connector = { id: 'box', name: 'Box', configured: true, connected: true, supportsFolderSync: true } as any;
    useConnectorsStore.setState({ folderPicker: { connector }, pollSyncStatus: vi.fn() } as any);

    await useConnectorsStore.getState().syncFolder(connector, 'f-9', 'Clients');
    expect(api.syncConnector).toHaveBeenCalledWith('box', { folder_id: 'f-9' });
    expect(useConnectorsStore.getState().folderPicker).toBeNull();
    expect(useUIStore.getState().toasts.some(t => t.message.includes('Importing "Clients"'))).toBe(true);
  });

  it('never starts a whole-account sync without a second, explicit confirmation', async () => {
    (api.syncConnector as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 'started' });
    const connector = { id: 'clio', name: 'Clio', configured: true, connected: true } as any;

    await useConnectorsStore.getState().handleConnectorClick(connector);
    expect(useUIStore.getState().confirmModal.title).toBe('Manage Clio');

    // "Sync Now" only opens the scope warning — nothing is sent yet.
    useUIStore.getState().handleConfirm();
    expect(api.syncConnector).not.toHaveBeenCalled();
    const warning = useUIStore.getState().confirmModal;
    expect(warning.open).toBe(true);
    expect(warning.title).toBe('Import the entire Clio account?');
    expect(warning.message).toMatch(/Every document/);
    expect(warning.message).toMatch(/embedding provider/);
    expect(warning.confirmText).toBe('Import entire account');

    useUIStore.getState().handleConfirm();
    await vi.waitFor(() => expect(api.syncConnector).toHaveBeenCalledWith('clio', { sync_all: true }));
  });

  it('cancelling the whole-account warning sends nothing', async () => {
    const connector = { id: 'box', name: 'Box', configured: true, connected: true } as any;
    await useConnectorsStore.getState().handleConnectorClick(connector);
    useUIStore.getState().handleConfirm();
    useUIStore.getState().closeConfirm();
    expect(api.syncConnector).not.toHaveBeenCalled();
  });

  it('an admin-only connector is refused for a non-admin, and usable by an admin', async () => {
    const connector = { id: 'filevine', name: 'Filevine', configured: true, connected: true, adminOnly: true } as any;

    useAuthStore.setState({ user: { id: 'u1', email: 'a@firm.com', roles: ['attorney'] }, isAuthenticated: true });
    await useConnectorsStore.getState().handleConnectorClick(connector);
    expect(useUIStore.getState().confirmModal.open).toBe(false);
    expect(useUIStore.getState().toasts.some(t => t.message.includes('only an administrator'))).toBe(true);

    useAuthStore.setState({ user: { id: 'u2', email: 'admin@firm.com', roles: ['admin'] }, isAuthenticated: true });
    await useConnectorsStore.getState().handleConnectorClick(connector);
    expect(useUIStore.getState().confirmModal.title).toBe('Manage Filevine');
  });

  it('loadConnectors carries the admin_only flag', async () => {
    (api.getConnectors as ReturnType<typeof vi.fn>).mockResolvedValue({
      connectors: [{ id: 'filevine', connected: false, configured: true, admin_only: true }],
    });
    (api.getPickerConfig as ReturnType<typeof vi.fn>).mockResolvedValue({});
    await useConnectorsStore.getState().loadConnectors();
    const filevine = useConnectorsStore.getState().connectors.find(c => c.id === 'filevine');
    expect(filevine?.adminOnly).toBe(true);
  });

  it('loadConnectors marks connectors the server did not list as unavailable', async () => {
    // EXPERIMENTAL_CONNECTORS=false: iManage and NetDocuments are absent from
    // the response. They must not keep their default (unset) `configured`,
    // which the Sources tab would read as "Configured".
    (api.getConnectors as ReturnType<typeof vi.fn>).mockResolvedValue({
      connectors: [{ id: 'clio', connected: false, configured: true }],
    });
    (api.getPickerConfig as ReturnType<typeof vi.fn>).mockResolvedValue({});
    await useConnectorsStore.getState().loadConnectors();
    const byId = (id: string) => useConnectorsStore.getState().connectors.find(c => c.id === id);
    expect(byId('clio')).toMatchObject({ available: true, configured: true, experimental: false });
    expect(byId('imanage')).toMatchObject({ available: false, configured: false, connected: false });
    expect(byId('netdocuments')).toMatchObject({ available: false, configured: false });
  });

  it('loadConnectors carries the experimental flag', async () => {
    (api.getConnectors as ReturnType<typeof vi.fn>).mockResolvedValue({
      connectors: [{ id: 'imanage', connected: false, configured: true, experimental: true }],
    });
    (api.getPickerConfig as ReturnType<typeof vi.fn>).mockResolvedValue({});
    await useConnectorsStore.getState().loadConnectors();
    const imanage = useConnectorsStore.getState().connectors.find(c => c.id === 'imanage');
    expect(imanage).toMatchObject({ available: true, experimental: true, configured: true });
  });

  it('refuses to open an OAuth address that only looks like a provider', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    (api.connectConnector as ReturnType<typeof vi.fn>).mockResolvedValue({
      auth_url: 'https://app.clio.com.attacker.example/oauth',
    });
    const connector = { id: 'clio', name: 'Clio', configured: true, connected: false } as any;

    await useConnectorsStore.getState().handleConnectorClick(connector);

    expect(open).not.toHaveBeenCalled();
    expect(useUIStore.getState().toasts.some(t => t.message.includes('unexpected sign-in address'))).toBe(true);
    open.mockRestore();
  });

  it('checks the connection only after the OAuth popup has closed', async () => {
    vi.useFakeTimers();
    const popup = { closed: false } as Window;
    const open = vi.spyOn(window, 'open').mockReturnValue(popup);
    (api.connectConnector as ReturnType<typeof vi.fn>).mockResolvedValue({ auth_url: 'https://app.clio.com/oauth/authorize' });
    (api.getConnectorStatus as ReturnType<typeof vi.fn>).mockResolvedValue({ connected: true, docs_indexed: 3 });
    const connector = { id: 'clio', name: 'Clio', configured: true, connected: false } as any;

    const pending = useConnectorsStore.getState().handleConnectorClick(connector);
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.getConnectorStatus).not.toHaveBeenCalled();

    (popup as { closed: boolean }).closed = true;
    await vi.advanceTimersByTimeAsync(1000);
    await pending;

    expect(api.getConnectorStatus).toHaveBeenCalledWith('clio');
    expect(useConnectorsStore.getState().connectors.find(c => c.id === 'clio')?.connected).toBe(true);
    open.mockRestore();
    vi.useRealTimers();
  });

  // ==================== openActivePicker ====================

  it('openActivePicker routes box to the Box picker (unconfigured shows config toast)', () => {
    useConnectorsStore.setState({ activePickerProvider: 'box' });
    useConnectorsStore.getState().openActivePicker();

    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.message.includes('Box picker not configured'))).toBe(true);
    expect(toasts.some(t => t.message.includes('coming soon'))).toBe(false);
    expect(useConnectorsStore.getState().showPickerModal).toBe(false);
  });

  it('openActivePicker shows error for unknown provider', () => {
    useConnectorsStore.setState({ activePickerProvider: 'unknown' });
    useConnectorsStore.getState().openActivePicker();

    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.message.includes('Picker not available'))).toBe(true);
  });

  // ==================== loadConnectors ====================

  it('loadConnectors fetches connectors and picker config', async () => {
    (api.getConnectors as ReturnType<typeof vi.fn>).mockResolvedValue({
      connectors: [{ id: 'google_drive', connected: true, configured: true, docs_indexed: 10 }],
    });
    (api.getPickerConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ google_enabled: true, microsoft_enabled: false });

    await useConnectorsStore.getState().loadConnectors();

    const google = useConnectorsStore.getState().connectors.find(c => c.id === 'google_drive');
    expect(google?.connected).toBe(true);
    expect(google?.docs).toBe(10);
    expect(useConnectorsStore.getState().pickerConfig).toEqual({ google_enabled: true, microsoft_enabled: false });
  });

  it('loadConnectors handles connector API error gracefully', async () => {
    (api.getConnectors as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Network error'));
    (api.getPickerConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ google_enabled: false, microsoft_enabled: false });

    await useConnectorsStore.getState().loadConnectors();

    // Should not throw; connectors remain at defaults
    expect(useConnectorsStore.getState().connectors).toHaveLength(8);
  });

  it('loadConnectors handles picker config API error gracefully', async () => {
    (api.getConnectors as ReturnType<typeof vi.fn>).mockResolvedValue({ connectors: [] });
    (api.getPickerConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Picker error'));

    await useConnectorsStore.getState().loadConnectors();

    // pickerConfig should remain null from initial
    expect(useConnectorsStore.getState().pickerConfig).toBeNull();
  });
});
