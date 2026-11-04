import { create } from 'zustand';
import { registerReset } from './resetRegistry';
import { api } from '../api';
import logger from '../utils/logger';
import { isAllowedOAuthUrl } from '../utils/oauthOrigins';
import { useUIStore } from './uiStore';
import { usePickerStore } from './pickerStore';
import { useAuthStore } from './authStore';
import { describeFileErrors } from './storeUtils';
import type { Connector, PickerConfig, PickerFile } from '../types';

const DEFAULT_CONNECTORS = [
  { id: 'google_drive', name: 'Google Drive', icon: 'HardDrive', color: '#4285F4', connected: false, docs: 0 },
  { id: 'onedrive', name: 'OneDrive & SharePoint', icon: 'Cloud', color: '#0078D4', connected: false, docs: 0 },
  { id: 'box', name: 'Box', icon: 'Box', color: '#0061D5', connected: false, docs: 0 },
  { id: 'netdocuments', name: 'NetDocuments', icon: 'FileStack', color: '#1E88E5', connected: false, docs: 0 },
  { id: 'imanage', name: 'iManage', icon: 'Database', color: '#6366F1', connected: false, docs: 0 },
  { id: 'filevine', name: 'Filevine', icon: 'FolderTree', color: '#10B981', connected: false, docs: 0 },
  { id: 'clio', name: 'Clio', icon: 'Scale', color: '#2563EB', connected: false, docs: 0 },
  { id: 'dropbox', name: 'Dropbox', icon: 'Droplet', color: '#0061FF', connected: false, docs: 0 },
];

const SYNC_POLL_INTERVAL_MS = 3000;
const SYNC_POLL_MAX_MS = 15 * 60 * 1000;
const OAUTH_POPUP_POLL_MS = 1000;
const OAUTH_POPUP_MAX_MS = 10 * 60 * 1000;

function isCurrentUserAdmin(): boolean {
  const user = useAuthStore.getState().user;
  return (user?.roles ?? []).includes('admin') || user?.role === 'admin';
}

/** Resolve once the OAuth popup has been closed (or the wait budget runs out). */
function waitForPopupClose(popup: Window): Promise<void> {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const timer = setInterval(() => {
      if (popup.closed || Date.now() - startedAt > OAUTH_POPUP_MAX_MS) {
        clearInterval(timer);
        resolve();
      }
    }, OAUTH_POPUP_POLL_MS);
  });
}

export interface ConnectorsState {
  connectors: Connector[];
  setConnectors: (val: Connector[] | ((prev: Connector[]) => Connector[])) => void;
  pickerConfig: PickerConfig | null;
  setPickerConfig: (val: PickerConfig | null) => void;
  showPickerModal: boolean;
  setShowPickerModal: (val: boolean) => void;
  pickerLoading: boolean;
  setPickerLoading: (val: boolean) => void;
  activePickerProvider: string | null;
  setActivePickerProvider: (val: string | null) => void;

  hasPickerSupport: (connectorId: string) => boolean;
  handlePickerFilesSelected: (files: PickerFile[], provider: string) => Promise<void>;
  handleConnectorClick: (connector: Connector) => Promise<void>;
  /** The connector whose folder tree is being browsed for a scoped sync. */
  folderPicker: { connector: Connector } | null;
  openFolderPicker: (connector: Connector) => void;
  closeFolderPicker: () => void;
  /** Start a sync scoped to one folder (closes the picker). */
  syncFolder: (connector: Connector, folderId: string, folderName: string) => Promise<void>;
  /** Start a whole-account sync, behind an explicit warning (closes the picker). */
  syncWholeAccount: (connector: Connector) => void;
  loadGooglePicker: () => Promise<void>;
  openGooglePicker: () => Promise<void>;
  openOneDrivePicker: () => Promise<void>;
  openDropboxChooser: () => Promise<void>;
  openActivePicker: () => void;
  loadConnectors: () => Promise<void>;
  pollSyncStatus: (connector: Connector, syncId: string) => void;
  _initialized: boolean;
  init: () => Promise<void>;
}

export const useConnectorsStore = create<ConnectorsState>((set, get) => ({
  // ── Core connector state ──────────────────────────────────────────
  connectors: DEFAULT_CONNECTORS,
  setConnectors: (val) => set((state) => ({
    connectors: typeof val === 'function' ? val(state.connectors) : val,
  })),
  pickerConfig: null,
  setPickerConfig: (val) => set({ pickerConfig: val }),

  // ── Picker state (delegates to pickerStore) ───────────────────────
  showPickerModal: false,
  setShowPickerModal: (val) => {
    set({ showPickerModal: val });
    usePickerStore.getState().setShowPickerModal(val);
  },
  pickerLoading: false,
  setPickerLoading: (val) => {
    set({ pickerLoading: val });
    usePickerStore.getState().setPickerLoading(val);
  },
  activePickerProvider: null,
  setActivePickerProvider: (val) => {
    set({ activePickerProvider: val });
    usePickerStore.getState().setActivePickerProvider(val);
  },

  // ── Folder-scoped sync ───────────────────────────────────────────
  folderPicker: null,
  openFolderPicker: (connector) => set({ folderPicker: { connector } }),
  closeFolderPicker: () => set({ folderPicker: null }),

  syncFolder: async (connector, folderId, folderName) => {
    const { addToast } = useUIStore.getState();
    set({ folderPicker: null });
    try {
      const result = await api.syncConnector(connector.id, { folder_id: folderId });
      addToast(`Importing "${folderName}" from ${connector.name}...`, 'info');
      if (result.sync_id) {
        get().pollSyncStatus(connector, result.sync_id);
      }
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      addToast(`Sync failed: ${message}`, 'error');
    }
  },

  syncWholeAccount: (connector) => {
    const { addToast, showConfirm } = useUIStore.getState();
    set({ folderPicker: null });
    showConfirm({
      title: `Import the entire ${connector.name} account?`,
      message: `Every document this ${connector.name} account can reach will be downloaded and indexed — not one folder or matter. The text of each document is sent to your configured embedding provider. To bring in specific files instead, cancel and use the file picker where one is offered.`,
      type: 'warning',
      confirmText: 'Import entire account',
      cancelText: 'Cancel',
      onConfirm: async () => {
        try {
          const result = await api.syncConnector(connector.id, { sync_all: true });
          addToast(`Syncing ${connector.name}...`, 'info');
          if (result.sync_id) {
            get().pollSyncStatus(connector, result.sync_id);
          }
        } catch (error: unknown) {
          const message = error instanceof Error ? error.message : String(error);
          addToast(`Sync failed: ${message}`, 'error');
        }
      },
    });
  },

  // ── Core methods ──────────────────────────────────────────────────
  hasPickerSupport: (connectorId) => {
    const { pickerConfig } = get();
    if (!pickerConfig) return false;
    const map: Record<string, boolean> = {
      'google_drive': pickerConfig.google_enabled ?? false,
      'onedrive': pickerConfig.microsoft_enabled ?? false,
      'box': pickerConfig.box_enabled ?? false,
      'dropbox': pickerConfig.dropbox_enabled ?? false,
    };
    return map[connectorId] || false;
  },

  handleConnectorClick: async (connector) => {
    const { addToast, showConfirm } = useUIStore.getState();
    const { hasPickerSupport } = get();

    if (connector.adminOnly && !isCurrentUserAdmin()) {
      addToast(`${connector.name} uses a firm-wide credential, so only an administrator can connect or sync it.`, 'info');
      return;
    }

    if (connector.configured === false) {
      addToast(`${connector.name} is not configured yet. An administrator needs to set up OAuth credentials for this connector in the server environment.`, 'error');
      return;
    }

    if (connector.connected) {
      // Only a connector the server said can scope a sync gets the picker;
      // anything else keeps the explicit whole-account warning.
      const canPickFolder = connector.supportsFolderSync === true;
      showConfirm({
        title: `Manage ${connector.name}`,
        message: canPickFolder
          ? `Choose a folder of ${connector.name} to import, or click Cancel to disconnect.`
          : `Would you like to sync documents from ${connector.name}? Or click Cancel to disconnect.`,
        type: 'info',
        confirmText: canPickFolder ? 'Choose folder…' : 'Sync Now',
        cancelText: 'Disconnect',
        onConfirm: () => {
          if (canPickFolder) {
            get().openFolderPicker(connector);
            return;
          }
          // No folder chooser for this connector: a sync can only cover the
          // whole account, which is never started without saying so first.
          get().syncWholeAccount(connector);
        },
        onCancel: () => {
          showConfirm({
            title: `Disconnect ${connector.name}`,
            message: `Are you sure you want to disconnect from ${connector.name}?`,
            type: 'warning',
            onConfirm: async () => {
              try {
                await api.disconnectConnector(connector.id);
                set((state) => ({
                  connectors: state.connectors.map((c) =>
                    c.id === connector.id ? { ...c, connected: false, docs: 0 } : c
                  ),
                }));
              } catch (error: unknown) {
                const message = error instanceof Error ? error.message : String(error);
                addToast(`Disconnect failed: ${message}`, 'error');
              }
            },
          });
        },
      });
    } else {
      if (hasPickerSupport(connector.id)) {
        set({ activePickerProvider: connector.id, showPickerModal: true });
        usePickerStore.getState().setActivePickerProvider(connector.id);
        usePickerStore.getState().setShowPickerModal(true);
      } else {
        try {
          const result = await api.connectConnector(connector.id);
          if (isAllowedOAuthUrl(result.auth_url)) {
            const popup = window.open(result.auth_url, '_blank', 'width=600,height=700');
            if (!popup) {
              addToast('Popup blocked — please allow popups for this site and try again.', 'error');
              return;
            }
            addToast('Complete authentication in the popup window.', 'info');
            // The connection only exists once the provider has redirected back
            // to the server — check after the popup closes, not before the
            // user has had a chance to sign in.
            await waitForPopupClose(popup);
            const status = await api.getConnectorStatus(connector.id);
            if (status.connected) {
              set((state) => ({
                connectors: state.connectors.map((c) =>
                  c.id === connector.id ? { ...c, connected: true, docs: status.docs_indexed || 0 } : c
                ),
              }));
              addToast(`${connector.name} connected.`, 'success');
            } else {
              addToast(`${connector.name} was not connected. Try again when you are ready.`, 'info');
            }
          } else if (result.auth_url) {
            addToast(`${connector.name} returned an unexpected sign-in address, so it was not opened.`, 'error');
          } else if (result.connected) {
            set((state) => ({
              connectors: state.connectors.map((c) =>
                c.id === connector.id ? { ...c, connected: true } : c
              ),
            }));
          }
        } catch (error: unknown) {
          const message = error instanceof Error ? error.message : String(error);
          addToast(`Connection failed: ${message}`, 'error');
        }
      }
    }
  },

  pollSyncStatus: (connector, syncId) => {
    const { addToast } = useUIStore.getState();
    const startedAt = Date.now();

    const poll = async () => {
      if (Date.now() - startedAt > SYNC_POLL_MAX_MS) {
        addToast(`${connector.name} sync is taking a while — it will finish in the background.`, 'info');
        return;
      }
      try {
        const status = await api.getConnectorSyncStatus(connector.id, syncId);
        const state = String(status.status || '').toLowerCase();
        if (['succeeded', 'completed', 'complete', 'done'].includes(state)) {
          const result = status.result;
          const processed = result?.docs_processed;
          addToast(
            `${connector.name} sync complete${typeof processed === 'number' ? ` — ${processed} document${processed === 1 ? '' : 's'} indexed` : ''}.`,
            'success',
          );
          // Per-file failures used to vanish behind the success toast.
          const failedCount = result?.errors_count ?? result?.errors?.length ?? 0;
          if (failedCount > 0) {
            addToast(describeFileErrors(`${failedCount} file${failedCount === 1 ? '' : 's'} could not be indexed`, result?.errors, failedCount), 'error');
          }
          await get().loadConnectors();
          return;
        }
        if (['failed', 'error', 'cancelled'].includes(state)) {
          addToast(`${connector.name} sync failed${status.error ? `: ${status.error}` : '.'}`, 'error');
          return;
        }
        setTimeout(poll, SYNC_POLL_INTERVAL_MS);
      } catch (error: unknown) {
        // A transient status-fetch failure shouldn't kill the watcher.
        logger.debug('Sync status poll failed:', error);
        setTimeout(poll, SYNC_POLL_INTERVAL_MS * 2);
      }
    };

    setTimeout(poll, SYNC_POLL_INTERVAL_MS);
  },

  _initialized: false,
  init: async () => {
    if (get()._initialized) return;
    set({ _initialized: true });
    await get().loadConnectors();
  },

  loadConnectors: async () => {
    try {
      const connectorData = await api.getConnectors();
      if (connectorData && connectorData.connectors) {
        set((state) => ({
          connectors: state.connectors.map((c) => {
            const sc = connectorData.connectors.find((sc) => sc.id === c.id);
            if (sc) {
              return {
                ...c,
                connected: sc.connected || false,
                configured: sc.configured !== false,
                adminOnly: sc.admin_only === true,
                available: true,
                experimental: sc.experimental === true,
                supportsFolderSync: sc.supports_folder_sync !== false,
                docs: sc.docs_indexed || 0,
              };
            }
            // The server answered but does not offer this connector (an
            // experimental one left off): it is neither connected nor configured.
            return { ...c, connected: false, configured: false, available: false, docs: 0 };
          }),
        }));
      }
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e);
      logger.debug('Could not load connectors:', message);
    }
    try {
      const pickerCfg = await api.getPickerConfig();
      set({ pickerConfig: pickerCfg });
      logger.debug('Picker config loaded:', pickerCfg);
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e);
      logger.debug('Could not load picker config:', message);
    }
  },

  // ── Picker methods (delegate to pickerStore) ──────────────────────
  handlePickerFilesSelected: (files, provider) => {
    return usePickerStore.getState().handlePickerFilesSelected(files, provider);
  },

  loadGooglePicker: () => {
    return usePickerStore.getState().loadGooglePicker();
  },

  openGooglePicker: () => {
    return usePickerStore.getState().openGooglePicker();
  },

  openOneDrivePicker: () => {
    return usePickerStore.getState().openOneDrivePicker();
  },

  openDropboxChooser: () => {
    return usePickerStore.getState().openDropboxChooser();
  },

  openActivePicker: () => {
    // Sync current activePickerProvider to pickerStore before delegating,
    // in case it was set via setState() rather than setActivePickerProvider().
    const { activePickerProvider } = get();
    usePickerStore.getState().setActivePickerProvider(activePickerProvider);
    usePickerStore.getState().openActivePicker();
  },
}));

// Sync picker state changes back to the connectors store so consumers
// who read showPickerModal / pickerLoading / activePickerProvider from
// useConnectorsStore see updates triggered internally by pickerStore methods.
usePickerStore.subscribe((pickerState) => {
  const connState = useConnectorsStore.getState();
  const patch: Partial<ConnectorsState> = {};
  if (connState.showPickerModal !== pickerState.showPickerModal) {
    patch.showPickerModal = pickerState.showPickerModal;
  }
  if (connState.pickerLoading !== pickerState.pickerLoading) {
    patch.pickerLoading = pickerState.pickerLoading;
  }
  if (connState.activePickerProvider !== pickerState.activePickerProvider) {
    patch.activePickerProvider = pickerState.activePickerProvider;
  }
  if (Object.keys(patch).length > 0) {
    useConnectorsStore.setState(patch);
  }
});

registerReset(() => useConnectorsStore.setState(useConnectorsStore.getInitialState(), true));
