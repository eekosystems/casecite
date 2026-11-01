import { useState, useEffect, useCallback, useId } from 'react';
import { api } from '../../api';
import { useAuthStore } from '../../stores/authStore';
import { useSettingsStore } from '../../stores/settingsStore';
import { useUIStore } from '../../stores/uiStore';
import { Icon } from '../shared/Icon';
import { ModalShell } from '../shared/ModalShell';
import type { RagSettings } from '../../types';
import s from './RAGSettingsModal.module.css';

import { APIKeysTab } from './rag-settings/APIKeysTab';
import { RetrievalTab } from './rag-settings/RetrievalTab';
import { EmbeddingTab } from './rag-settings/EmbeddingTab';
import { GenerationTab } from './rag-settings/GenerationTab';
import { PromptsTab } from './rag-settings/PromptsTab';
import { AdvancedTab } from './rag-settings/AdvancedTab';
import { BrandingTab } from './rag-settings/BrandingTab';
import { UsersTab } from './rag-settings/UsersTab';
import { SourcesTab } from './rag-settings/SourcesTab';
import { SecurityTab } from './rag-settings/SecurityTab';
import { AuditTab } from './rag-settings/AuditTab';

const TAB_LABELS: Record<string, string> = { api: 'API Keys', audit: 'Audit Log' };

interface RAGSettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  settings: RagSettings;
  /** Resolves false when the save failed — the modal then stays open. */
  onSave: (settings: RagSettings) => void | boolean | Promise<void | boolean>;
}

export const RAGSettingsModal = ({ isOpen, onClose, settings, onSave }: RAGSettingsModalProps) => {
  const isAuthenticated = useAuthStore((st) => st.isAuthenticated);
  const isAdmin = useAuthStore((st) => (st.user?.roles ?? []).includes('admin') || st.user?.role === 'admin');
  const serverSettingsLoaded = useSettingsStore((st) => st.serverSettingsLoaded);
  const serverSettingsError = useSettingsStore((st) => st.serverSettingsError);
  const loadServerSettings = useSettingsStore((st) => st.loadServerSettings);
  const requestedTab = useUIStore((st) => st.settingsTab);
  const [local, setLocal] = useState(settings);
  const [activeTab, setActiveTab] = useState('api');
  const [saving, setSaving] = useState(false);

  // Opened straight onto a tab (e.g. Security when MFA enrollment is required).
  useEffect(() => {
    if (isOpen && requestedTab) setActiveTab(requestedTab);
  }, [isOpen, requestedTab]);

  // Saving is a full replace on the server. If this session's settings never
  // arrived, retry when the modal opens rather than let "Save" overwrite the
  // stored playbook, practice profile and prompts with blanks.
  useEffect(() => {
    if (isOpen && isAuthenticated && !serverSettingsLoaded) loadServerSettings();
  }, [isOpen, isAuthenticated, serverSettingsLoaded, loadServerSettings]);
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});

  const [apiKeys, setApiKeys] = useState<Record<string, string>>({
    openai: '',
    anthropic: '',
    google: '',
    voyage: '',
    cohere: '',
  });

  const [serverKeyStatus, setServerKeyStatus] = useState<Record<string, boolean>>({});
  const [maskedKeys, setMaskedKeys] = useState<Record<string, string | null>>({});
  // Distinguish "no keys saved" from "couldn't ASK the server" — showing empty
  // fields when the status fetch failed reads as "your keys are gone".
  const [keyStatusError, setKeyStatusError] = useState(false);

  useEffect(() => { setLocal(settings); }, [settings]);

  // Server-truth key state. Loaded on open and re-loaded after every save so
  // the check marks reflect what is actually stored, not what was typed.
  const refreshKeyStatus = useCallback(async () => {
    let failed = false;
    try {
      const status = await api.getKeyStatus();
      setServerKeyStatus({
        openai: status.openai_configured,
        anthropic: status.anthropic_configured,
        google: status.google_configured,
        voyage: status.voyage_configured,
        cohere: status.cohere_configured,
      });
    } catch {
      failed = true;
    }
    try {
      const masked = await api.getMaskedKeys();
      setMaskedKeys(masked);
    } catch {
      failed = true;
    }
    setKeyStatusError(failed);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    // Reset input fields on each open so stale values don't linger
    setApiKeys({ openai: '', anthropic: '', google: '', voyage: '', cohere: '' });
    refreshKeyStatus();
  }, [isOpen, refreshKeyStatus]);

  const titleId = useId();

  // Settings is gated behind login as a whole (consistent: no per-tab login gates).
  if (!isOpen || !isAuthenticated) return null;

  return (
    <ModalShell onClose={onClose} overlayClassName={s.modalOverlay} className={s.modalContainer} labelledBy={titleId}>
        <div className={s.modalHeader}>
          <div className={s.modalHeaderLeft}>
            <div className={s.modalIcon}>
              <Icon name="Settings" size={20} className={s.modalIconWhite} />
            </div>
            <div>
              <h2 id={titleId} className={s.modalTitle}>Settings</h2>
              <p className={s.modalSubtitle}>Configure API keys and RAG settings</p>
            </div>
          </div>
          <button className={s.modalClose} onClick={onClose} aria-label="Close"><Icon name="X" size={20} /></button>
        </div>

        <div className={s.tabBar}>
          {(['api', 'security', 'retrieval', 'embedding', 'generation', 'prompts', 'advanced', ...(isAdmin ? ['sources', 'users', 'audit', 'branding'] : [])] as const).map(tab => (
            <button key={tab} className={`tab-btn ${activeTab === tab ? 'active' : ''}`} onClick={() => setActiveTab(tab)}>
              {TAB_LABELS[tab] ?? tab.charAt(0).toUpperCase() + tab.slice(1)}
            </button>
          ))}
        </div>

        <div className={s.modalBody}>
          {activeTab === 'api' && (
            <APIKeysTab local={local} setLocal={setLocal} showKeys={showKeys} setShowKeys={setShowKeys} apiKeys={apiKeys} setApiKeys={setApiKeys} serverKeyStatus={serverKeyStatus} maskedKeys={maskedKeys} keyStatusError={keyStatusError} refreshKeyStatus={refreshKeyStatus} styles={s} />
          )}
          {activeTab === 'security' && (
            <SecurityTab styles={s} />
          )}
          {activeTab === 'retrieval' && (
            <RetrievalTab local={local} setLocal={setLocal} styles={s} />
          )}
          {activeTab === 'embedding' && (
            <EmbeddingTab isAdmin={isAdmin} styles={s} />
          )}
          {activeTab === 'generation' && (
            <GenerationTab local={local} setLocal={setLocal} styles={s} />
          )}
          {activeTab === 'prompts' && (
            <PromptsTab local={local} setLocal={setLocal} styles={s} />
          )}
          {activeTab === 'sources' && isAdmin && (
            <SourcesTab styles={s} />
          )}
          {activeTab === 'users' && isAdmin && (
            <UsersTab styles={s} />
          )}
          {activeTab === 'audit' && isAdmin && (
            <AuditTab styles={s} />
          )}
          {activeTab === 'advanced' && (
            <AdvancedTab local={local} setLocal={setLocal} styles={s} />
          )}
          {activeTab === 'branding' && isAdmin && (
            <BrandingTab styles={s} />
          )}
        </div>

        <div className={s.modalFooter}>
          {/* Open source: point users at the code and license from inside the
              product. Forks should point this at their own repository. */}
          <a
            className={s.helpText}
            href="https://github.com/thomasunise/casecite"
            target="_blank"
            rel="noopener noreferrer"
          >
            CaseCite source code (Apache-2.0)
          </a>
          {!serverSettingsLoaded && (
            <span className={s.helpText} role="status">
              {serverSettingsError
                ? 'Your saved settings could not be loaded, so saving is turned off. '
                : 'Loading your saved settings… '}
              {serverSettingsError && (
                <button type="button" className={s.btnSecondaryXSmall} onClick={() => loadServerSettings()}>Retry</button>
              )}
            </span>
          )}
          <button className={s.btnSecondary} onClick={onClose}>Cancel</button>
          <button
            className={s.btnPrimary}
            disabled={!serverSettingsLoaded || saving}
            onClick={async () => {
              setSaving(true);
              try {
                if ((await onSave(local)) !== false) onClose();
              } finally {
                setSaving(false);
              }
            }}
          >
            <Icon name="Check" size={16} /> {saving ? 'Saving…' : 'Save Changes'}
          </button>
        </div>
    </ModalShell>
  );
};
