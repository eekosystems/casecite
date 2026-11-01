import { useRef, useCallback, useState, useEffect, useId } from 'react';
import { api } from '../../../api';
import { useUIStore } from '../../../stores/uiStore';
import { Icon } from '../../shared/Icon';
import type { RagSettings } from '../../../types';
import type { CourtListenerStatus, LocalLlmStatus } from '../../../api/types';

interface APIKeysTabProps {
  local: RagSettings;
  setLocal: (s: RagSettings) => void;
  showKeys: Record<string, boolean>;
  setShowKeys: (s: Record<string, boolean>) => void;
  apiKeys: Record<string, string>;
  setApiKeys: (keys: Record<string, string>) => void;
  serverKeyStatus: Record<string, boolean>;
  maskedKeys: Record<string, string | null>;
  keyStatusError: boolean;
  refreshKeyStatus: () => Promise<void>;
  styles: Record<string, string>;
}

type SaveState = 'saving' | 'saved' | 'error';

// Small inline status shown next to each field's label. Module-level so its
// identity is stable across renders (a component defined inside APIKeysTab
// would remount on every keystroke).
const SaveHint = ({ state, styles }: { state: SaveState | undefined; styles: Record<string, string> }) => {
  if (state === 'saving') return <span className={styles.saveHint}>Saving…</span>;
  if (state === 'saved') return <span className={styles.saveHintOk}><Icon name="Check" size={12} /> Saved</span>;
  if (state === 'error') return <span className={styles.saveHintErr}><Icon name="AlertCircle" size={12} /> Failed</span>;
  return null;
};

/** One row of the local model list: a model and the server that runs it. */
interface ModelRow { key: number; name: string; baseUrl: string }

/** The local model form as typed. '' = the role's default: utility and drafting
 * follow the chat model, research follows the utility model. */
interface LlmDraft { rows: ModelRow[]; chat: string; utility: string; research: string; drafting: string }

/** What the form would save: complete rows, unique names, and a valid model per role. */
function resolveLlmDraft(d: LlmDraft) {
  const complete = d.rows.filter((r) => r.name.trim() && r.baseUrl.trim().length >= 4);
  const all = complete.map((r) => r.name.trim());
  const names = Array.from(new Set(all));
  const duplicate = all.find((n, i) => all.indexOf(n) !== i) ?? null;
  const pick = (value: string) => (names.includes(value.trim()) ? value.trim() : '');
  return {
    complete,
    names,
    duplicate,
    // The first model is the chat model until another is chosen.
    chat: pick(d.chat) || names[0] || '',
    utility: pick(d.utility),
    research: pick(d.research),
    drafting: pick(d.drafting),
  };
}

export const APIKeysTab = ({ local: _local, setLocal: _setLocal, showKeys, setShowKeys, apiKeys, setApiKeys, serverKeyStatus, maskedKeys, keyStatusError, refreshKeyStatus, styles }: APIKeysTabProps) => {
  const id = useId();
  const saveTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  // Values typed but not yet persisted (the debounce window). Flushed
  // immediately on blur and on unmount so closing the modal can never race
  // a pending save.
  const pendingSaves = useRef<Record<string, string>>({});
  const addToast = useUIStore((st) => st.addToast);
  const showConfirm = useUIStore((st) => st.showConfirm);

  // CourtListener instance token
  const [clStatus, setClStatus] = useState<CourtListenerStatus | null>(null);
  const [clToken, setClToken] = useState('');

  // Local / custom OpenAI-compatible models: a list of (server, model) rows and
  // the model chosen for each role. '' = the role's default (see LlmDraft).
  const [llmStatus, setLlmStatus] = useState<LocalLlmStatus | null>(null);
  const rowKey = useRef(0);
  const [modelRows, setModelRows] = useState<ModelRow[]>([{ key: 0, name: '', baseUrl: '' }]);
  const [chatModel, setChatModel] = useState('');
  const [utilityModel, setUtilityModel] = useState('');
  const [researchModel, setResearchModel] = useState('');
  const [draftingModel, setDraftingModel] = useState('');

  const newRow = useCallback((baseUrl = ''): ModelRow => ({ key: ++rowKey.current, name: '', baseUrl }), []);

  /** Load the form from a status response (GET, or after a revert). */
  const applyLlmStatus = useCallback((llm: LocalLlmStatus) => {
    setLlmStatus(llm);
    const models = llm.configured
      ? (llm.models?.length ? llm.models : llm.base_url && llm.chat_model ? [{ name: llm.chat_model, base_url: llm.base_url }] : [])
      : [];
    setModelRows(models.length
      ? models.map((m) => ({ key: ++rowKey.current, name: m.name, baseUrl: m.base_url }))
      : [newRow()]);
    setChatModel(llm.configured ? llm.chat_model || '' : '');
    setUtilityModel(llm.configured && llm.utility_model && llm.utility_model !== llm.chat_model ? llm.utility_model : '');
    setResearchModel(llm.configured ? llm.research_model || '' : '');
    setDraftingModel(llm.configured ? llm.drafting_model || '' : '');
  }, [newRow]);

  const loadIntegrations = useCallback(async () => {
    try {
      setClStatus(await api.getCourtListenerStatus());
    } catch { /* non-critical */ }
    try {
      applyLlmStatus(await api.getLocalLlmStatus());
    } catch { /* non-critical */ }
  }, [applyLlmStatus]);

  useEffect(() => { loadIntegrations(); }, [loadIntegrations]);

  // Per-provider save state for the inline "Saving.../Saved" hint.
  const [savedState, setSavedState] = useState<Record<string, SaveState>>({});
  const markSaved = (key: string, state: SaveState) =>
    setSavedState((prev) => ({ ...prev, [key]: state }));

  const saveKeyNow = useCallback(async (provider: string, value: string) => {
    delete pendingSaves.current[provider];
    markSaved(provider, 'saving');
    try {
      await api.saveApiKey(provider, value);
      markSaved(provider, 'saved');
      // Reflect the SERVER's truth immediately — the check mark and masked
      // hint must never depend on reopening the modal.
      await refreshKeyStatus();
    } catch (e) {
      markSaved(provider, 'error');
      addToast(e instanceof Error ? e.message : `Failed to save ${provider} key`, 'error');
    }
  }, [addToast, refreshKeyStatus]);

  const persistKeyToServer = useCallback((provider: string, value: string) => {
    if (saveTimers.current[provider]) clearTimeout(saveTimers.current[provider]);
    delete pendingSaves.current[provider];
    if (!value || value.length < 10) return;
    pendingSaves.current[provider] = value;
    markSaved(provider, 'saving');
    saveTimers.current[provider] = setTimeout(() => saveKeyNow(provider, value), 800);
  }, [saveKeyNow]);

  // Flush a provider's pending save immediately (field blur).
  const flushKeySave = useCallback((provider: string) => {
    const value = pendingSaves.current[provider];
    if (!value) return;
    if (saveTimers.current[provider]) clearTimeout(saveTimers.current[provider]);
    saveKeyNow(provider, value);
  }, [saveKeyNow]);

  // Closing the modal unmounts this tab — fire anything still pending so a
  // fast type-then-close never loses the key. The field said "Saving…" when
  // the modal closed, so a failure must still reach the admin: the toast
  // store outlives this tab.
  useEffect(() => {
    const timers = saveTimers.current;
    const pending = pendingSaves.current;
    return () => {
      for (const provider of Object.keys(pending)) {
        if (timers[provider]) clearTimeout(timers[provider]);
        const value = pending[provider];
        if (value) {
          api.saveApiKey(provider, value).catch((e: unknown) => {
            useUIStore.getState().addToast(
              e instanceof Error && e.message
                ? `The ${provider} key was not saved: ${e.message}`
                : `The ${provider} key was not saved. Open Settings and enter it again.`,
              'error',
            );
          });
        }
      }
    };
  }, []);

  const handleApiKeyChange = (provider: string, value: string) => {
    const updated = { ...apiKeys, [provider]: value };
    setApiKeys(updated);
    persistKeyToServer(provider, value);
  };

  // Helper: get the placeholder for a key field.
  // If a key is saved server-side, show masked hint; otherwise show format hint.
  const getPlaceholder = (provider: string, defaultHint: string) => {
    if (maskedKeys[provider]) return maskedKeys[provider]!;
    return defaultHint;
  };

  // Helper: check if a provider is configured (either locally entered or on server)
  const isConfigured = (provider: string) => apiKeys[provider] || serverKeyStatus[provider];

  // Unified status for every credential/endpoint on this page.
  const statusItems = [
    { key: 'openai', label: 'OpenAI', required: true, configured: !!isConfigured('openai') },
    { key: 'anthropic', label: 'Anthropic', required: false, configured: !!isConfigured('anthropic') },
    { key: 'google', label: 'Gemini', required: false, configured: !!isConfigured('google') },
    { key: 'voyage', label: 'Voyage', required: false, configured: !!isConfigured('voyage') },
    { key: 'cohere', label: 'Cohere', required: false, configured: !!isConfigured('cohere') },
    { key: 'courtlistener', label: 'CourtListener', required: false, configured: !!clStatus?.configured },
    { key: 'local', label: 'Local Model', required: false, configured: !!llmStatus?.configured },
  ];

  // ----- CourtListener handlers (auto-save, debounced) -----
  const clTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handleClChange = (value: string) => {
    setClToken(value);
    if (clTimer.current) clearTimeout(clTimer.current);
    if (value.trim().length < 10) return;
    markSaved('courtlistener', 'saving');
    clTimer.current = setTimeout(async () => {
      try {
        // The save response is the status: a successful save means configured,
        // whatever the previous (or a stale) status said.
        const saved = await api.setCourtListenerToken(value.trim());
        setClStatus((prev) => ({ ...prev, ...saved, configured: saved.configured ?? true }));
        setClToken('');
        markSaved('courtlistener', 'saved');
      } catch (e) {
        markSaved('courtlistener', 'error');
        addToast(e instanceof Error ? e.message : 'Failed to save token', 'error');
      }
    }, 800);
  };

  const handleClearCl = () => {
    showConfirm({
      title: 'Clear CourtListener token',
      message: 'Clear the CourtListener token? Case law search reverts to the .env fallback (or anonymous, rate-limited).',
      confirmText: 'Clear',
      onConfirm: async () => {
        try {
          const cleared = await api.clearCourtListenerToken();
          setClToken('');
          // The response carries the effective (post-clear) status — e.g. an
          // .env fallback still counts as configured.
          if (typeof cleared?.configured === 'boolean') setClStatus((prev) => ({ ...prev, ...cleared }));
          else await loadIntegrations();
          addToast('CourtListener token cleared', 'success');
        } catch (e) {
          addToast(e instanceof Error ? e.message : 'Failed to clear token', 'error');
        }
      },
    });
  };

  // ----- Remove a stored provider key -----
  const handleDeleteKey = (provider: string, label: string) => {
    showConfirm({
      title: `Remove your ${label} key?`,
      message: `The stored ${label} key is deleted from the server. Features that rely on it stop working until you add a key again (or the instance provides one).`,
      confirmText: 'Remove key',
      onConfirm: async () => {
        // A half-typed replacement must not be re-saved after the delete.
        if (saveTimers.current[provider]) clearTimeout(saveTimers.current[provider]);
        delete pendingSaves.current[provider];
        try {
          await api.deleteApiKey(provider);
          setApiKeys({ ...apiKeys, [provider]: '' });
          await refreshKeyStatus();
          addToast(`${label} key removed`, 'success');
        } catch (e) {
          addToast(e instanceof Error ? e.message : `Failed to remove the ${label} key`, 'error');
        }
      },
    });
  };

  // ----- Local models (auto-save, debounced) -----
  const llmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoSaveLlm = (next: LlmDraft) => {
    if (llmTimer.current) clearTimeout(llmTimer.current);
    // Saved once at least one row is complete and every name is unique.
    const draft = resolveLlmDraft(next);
    if (!draft.chat || draft.duplicate) return;
    markSaved('local', 'saving');
    llmTimer.current = setTimeout(async () => {
      try {
        const models = draft.complete.map((r) => ({ name: r.name.trim(), baseUrl: r.baseUrl.trim() }));
        const saved = await api.setLocalLlm({
          models,
          chatModel: draft.chat,
          utilityModel: draft.utility || null,
          researchModel: draft.research || null,
          draftingModel: draft.drafting || null,
        });
        setLlmStatus((prev) => ({
          ...prev,
          ...saved,
          configured: saved.configured ?? true,
          base_url: saved.base_url ?? models.find((m) => m.name === draft.chat)?.baseUrl ?? null,
          chat_model: saved.chat_model ?? draft.chat,
          utility_model: saved.utility_model ?? (draft.utility || draft.chat),
          research_model: saved.research_model ?? (draft.research || null),
          drafting_model: saved.drafting_model ?? (draft.drafting || null),
          models: saved.models ?? models.map((m) => ({ name: m.name, base_url: m.baseUrl })),
        }));
        markSaved('local', 'saved');
      } catch (e) {
        markSaved('local', 'error');
        addToast(e instanceof Error ? e.message : 'Failed to save models', 'error');
      }
    }, 1000);
  };

  /** Change the form and schedule a save with the new values. */
  const updateLlm = (change: Partial<LlmDraft>) => {
    const next: LlmDraft = { rows: modelRows, chat: chatModel, utility: utilityModel, research: researchModel, drafting: draftingModel, ...change };
    if (change.rows) setModelRows(change.rows);
    if (change.chat !== undefined) setChatModel(change.chat);
    if (change.utility !== undefined) setUtilityModel(change.utility);
    if (change.research !== undefined) setResearchModel(change.research);
    if (change.drafting !== undefined) setDraftingModel(change.drafting);
    autoSaveLlm(next);
  };
  const updateRow = (key: number, field: 'name' | 'baseUrl', value: string) =>
    updateLlm({ rows: modelRows.map((r) => (r.key === key ? { ...r, [field]: value } : r)) });
  // A new row starts on the previous row's server: several models on one Ollama is the common case.
  const addModelRow = () => setModelRows([...modelRows, newRow(modelRows[modelRows.length - 1]?.baseUrl || '')]);
  const removeModelRow = (key: number) => {
    const rows = modelRows.filter((r) => r.key !== key);
    updateLlm({ rows: rows.length ? rows : [newRow()] });
  };
  const llmDraft = resolveLlmDraft({ rows: modelRows, chat: chatModel, utility: utilityModel, research: researchModel, drafting: draftingModel });

  const handleClearLlm = () => {
    showConfirm({
      title: 'Revert to OpenAI',
      message: 'Remove the local endpoint and revert to the default OpenAI models?',
      confirmText: 'Revert',
      onConfirm: async () => {
        try {
          const cleared = await api.clearLocalLlm();
          if (typeof cleared?.configured === 'boolean') {
            applyLlmStatus({ ...llmStatus, ...cleared });
          } else {
            await loadIntegrations();
          }
          addToast('Reverted to default models', 'success');
        } catch (e) {
          addToast(e instanceof Error ? e.message : 'Failed to revert', 'error');
        }
      },
    });
  };

  // Provider keys this user has stored server-side (each can be removed).
  const storedKeys = statusItems.filter((item) => serverKeyStatus[item.key]);

  return (
    <div className={styles.settingsGrid}>
      {/* Key Status — kept at the top so configuration state is visible at a glance */}
      <div className={styles.keyStatusSection}>
        <div className={styles.keyStatusTitle}>Key Status</div>
        <div className={styles.keyStatusList}>
          {statusItems.map((item) => (
            <div key={item.key} className={`${styles.keyBadge} ${item.configured ? styles.keyBadgeActive : (item.required ? styles.keyBadgeRequired : styles.keyBadgeOptional)}`}>
              <Icon name={item.configured ? 'Check' : (item.required ? 'AlertCircle' : 'Minus')} size={12} />
              {item.label}
            </div>
          ))}
        </div>
      </div>

      {keyStatusError && (
        <div className={styles.byokBanner}>
          <div className={styles.bannerHeader}>
            <Icon name="AlertCircle" size={16} className={styles.bannerIconAmber} />
            <span className={styles.bannerTitleBlue}>Could not load saved-key status</span>
          </div>
          <p className={styles.bannerTextBlue}>
            The fields below may show as unconfigured even though your keys are still saved on the
            server. Reopen Settings to retry — do not re-enter keys unless a provider genuinely
            stops working.
          </p>
        </div>
      )}

      <div className={styles.byokBanner}>
        <div className={styles.bannerHeader}>
          <Icon name="Key" size={16} className={styles.bannerIconBlue} />
          <span className={styles.bannerTitleBlue}>Bring Your Own Keys</span>
        </div>
        <p className={styles.bannerTextBlue}>
          Your API keys are encrypted and stored securely on our servers. Keys are never exposed in API responses.
          {Object.values(serverKeyStatus).some(Boolean) && ' Saved keys are shown masked below — leave a field empty to keep your existing key.'}
        </p>
      </div>

      {storedKeys.length > 0 && (
        <div className={styles.keyStatusSection}>
          <div className={styles.keyStatusTitle}>Your stored keys</div>
          {storedKeys.map((item) => (
            <div key={item.key} className={styles.storedKeyRow}>
              <span>
                {item.label}
                {maskedKeys[item.key] && <span className={styles.helpText}> · {maskedKeys[item.key]}</span>}
              </span>
              <button
                type="button"
                className={styles.btnDangerXSmall}
                onClick={() => handleDeleteKey(item.key, item.label)}
              >
                <Icon name="Trash2" size={12} /> Remove
              </button>
            </div>
          ))}
        </div>
      )}

      {/* ===== Case Law ===== */}
      <div className={styles.sectionTitle}>
        <Icon name="Scale" size={13} /> Case Law
      </div>

      <div className={styles.settingGroupFullWidth}>
        <label className={styles.settingLabel} htmlFor={`${id}-key-courtlistener`}>
          <span className={styles.labelFlex}>
            CourtListener API Token
            {clStatus?.configured && <Icon name="CheckCircle" size={14} className={styles.checkIcon} />}
            <SaveHint state={savedState["courtlistener"]} styles={styles} />
          </span>
        </label>
        <div className={styles.inputRow}>
          <input
            id={`${id}-key-courtlistener`}
            type={showKeys.courtlistener ? 'text' : 'password'}
            className={styles.modalInputMono}
            value={clToken}
            onChange={e => handleClChange(e.target.value)}
            placeholder={clStatus?.masked || 'Free Law Project API token'}
          />
          <button
            className={styles.btnSecondarySmall}
            onClick={() => setShowKeys({ ...showKeys, courtlistener: !showKeys.courtlistener })}
          >
            <Icon name={showKeys.courtlistener ? 'EyeOff' : 'Eye'} size={16} />
          </button>
        </div>
        <span className={styles.helpText}>
          {clStatus?.configured
            ? `Configured (source: ${clStatus.source}). Powers every Legal Tool. Type a new token to replace it — saves automatically.`
            : 'Powers every Legal Tool (case search, judges, dockets, precedents). Paste your token — it saves automatically. Get one free at courtlistener.com.'}
        </span>
        {clStatus?.configured && clStatus.source === 'instance' && (
          <div className={styles.boxActions}>
            <button className={styles.brandingResetBtn} onClick={handleClearCl}>Clear</button>
          </div>
        )}
      </div>

      {/* ===== Language Models ===== */}
      <div className={styles.sectionTitle}>
        <Icon name="Sparkles" size={13} /> Language Models (LLMs)
      </div>

      {/* OpenAI */}
      <div className={styles.settingGroupFullWidth}>
        <label className={styles.settingLabel} htmlFor={`${id}-key-openai`}>
          <span className={styles.labelFlex}>
            <span className={`${styles.providerIcon} ${styles.providerIconOpenAI}`}>
              <span className={styles.providerIconText}>AI</span>
            </span>
            OpenAI API Key
            {isConfigured('openai') && <Icon name="CheckCircle" size={14} className={styles.checkIcon} />}
            <SaveHint state={savedState["openai"]} styles={styles} />
          </span>
        </label>
        <div className={styles.inputRow}>
          <input
            id={`${id}-key-openai`}
            type={showKeys.openai ? 'text' : 'password'}
            className={styles.modalInputMono}
            value={apiKeys.openai}
            onChange={e => handleApiKeyChange('openai', e.target.value)}
            onBlur={() => flushKeySave('openai')}
            placeholder={getPlaceholder('openai', 'sk-...')}
          />
          <button
            className={styles.btnSecondarySmall}
            onClick={() => setShowKeys({...showKeys, openai: !showKeys.openai})}
          >
            <Icon name={showKeys.openai ? 'EyeOff' : 'Eye'} size={16} />
          </button>
        </div>
        <span className={styles.helpText}>Used for GPT-5 and default embeddings. Get key at platform.openai.com</span>
      </div>

      {/* Anthropic */}
      <div className={styles.settingGroupFullWidth}>
        <label className={styles.settingLabel} htmlFor={`${id}-key-anthropic`}>
          <span className={styles.labelFlex}>
            <span className={`${styles.providerIcon} ${styles.providerIconAnthropic}`}>
              <span className={styles.providerIconTextSmall}>A</span>
            </span>
            Anthropic API Key
            {isConfigured('anthropic') && <Icon name="CheckCircle" size={14} className={styles.checkIcon} />}
            <SaveHint state={savedState["anthropic"]} styles={styles} />
          </span>
        </label>
        <div className={styles.inputRow}>
          <input
            id={`${id}-key-anthropic`}
            type={showKeys.anthropic ? 'text' : 'password'}
            className={styles.modalInputMono}
            value={apiKeys.anthropic}
            onChange={e => handleApiKeyChange('anthropic', e.target.value)}
            onBlur={() => flushKeySave('anthropic')}
            placeholder={getPlaceholder('anthropic', 'sk-ant-...')}
          />
          <button
            className={styles.btnSecondarySmall}
            onClick={() => setShowKeys({...showKeys, anthropic: !showKeys.anthropic})}
          >
            <Icon name={showKeys.anthropic ? 'EyeOff' : 'Eye'} size={16} />
          </button>
        </div>
        <span className={styles.helpText}>Used for Claude models. Get key at console.anthropic.com</span>
      </div>

      {/* Google Gemini */}
      <div className={styles.settingGroupFullWidth}>
        <label className={styles.settingLabel} htmlFor={`${id}-key-google`}>
          <span className={styles.labelFlex}>
            <span className={`${styles.providerIcon} ${styles.providerIconGoogle}`}>
              <span className={styles.providerIconTextSmall}>G</span>
            </span>
            Google Gemini API Key
            {isConfigured('google') && <Icon name="CheckCircle" size={14} className={styles.checkIcon} />}
            <SaveHint state={savedState["google"]} styles={styles} />
          </span>
        </label>
        <div className={styles.inputRow}>
          <input
            id={`${id}-key-google`}
            type={showKeys.google ? 'text' : 'password'}
            className={styles.modalInputMono}
            value={apiKeys.google}
            onChange={e => handleApiKeyChange('google', e.target.value)}
            onBlur={() => flushKeySave('google')}
            placeholder={getPlaceholder('google', 'AIza...')}
          />
          <button
            className={styles.btnSecondarySmall}
            onClick={() => setShowKeys({...showKeys, google: !showKeys.google})}
          >
            <Icon name={showKeys.google ? 'EyeOff' : 'Eye'} size={16} />
          </button>
        </div>
        <span className={styles.helpText}>Used for Gemini models. Get key at aistudio.google.com</span>
      </div>

      {/* ===== Embeddings ===== */}
      <div className={styles.sectionTitle}>
        <Icon name="Layers" size={13} /> Embedding Models
      </div>
      <div className={styles.sectionSubtext}>
        Dedicated embedding providers for vector search. OpenAI (above) supplies the default embeddings.
      </div>

      {/* Voyage */}
      <div className={styles.settingGroup}>
        <label className={styles.settingLabel} htmlFor={`${id}-key-voyage`}>
          <span className={styles.labelFlex}>
            Voyage AI Key
            {isConfigured('voyage') && <Icon name="CheckCircle" size={14} className={styles.checkIcon} />}
            <SaveHint state={savedState["voyage"]} styles={styles} />
          </span>
        </label>
        <div className={styles.inputRow}>
          <input
            id={`${id}-key-voyage`}
            type={showKeys.voyage ? 'text' : 'password'}
            className={styles.modalInputMono}
            value={apiKeys.voyage}
            onChange={e => handleApiKeyChange('voyage', e.target.value)}
            onBlur={() => flushKeySave('voyage')}
            placeholder={getPlaceholder('voyage', 'pa-...')}
          />
          <button
            className={styles.btnSecondarySmall}
            onClick={() => setShowKeys({...showKeys, voyage: !showKeys.voyage})}
          >
            <Icon name={showKeys.voyage ? 'EyeOff' : 'Eye'} size={16} />
          </button>
        </div>
        <span className={styles.helpText}>Legal-optimized embeddings (voyage-law-2)</span>
      </div>

      {/* Cohere */}
      <div className={styles.settingGroup}>
        <label className={styles.settingLabel} htmlFor={`${id}-key-cohere`}>
          <span className={styles.labelFlex}>
            Cohere API Key
            {isConfigured('cohere') && <Icon name="CheckCircle" size={14} className={styles.checkIcon} />}
            <SaveHint state={savedState["cohere"]} styles={styles} />
          </span>
        </label>
        <div className={styles.inputRow}>
          <input
            id={`${id}-key-cohere`}
            type={showKeys.cohere ? 'text' : 'password'}
            className={styles.modalInputMono}
            value={apiKeys.cohere}
            onChange={e => handleApiKeyChange('cohere', e.target.value)}
            onBlur={() => flushKeySave('cohere')}
            placeholder={getPlaceholder('cohere', '...')}
          />
          <button
            className={styles.btnSecondarySmall}
            onClick={() => setShowKeys({...showKeys, cohere: !showKeys.cohere})}
          >
            <Icon name={showKeys.cohere ? 'EyeOff' : 'Eye'} size={16} />
          </button>
        </div>
        <span className={styles.helpText}>Alternative embeddings provider</span>
      </div>

      {/* ===== Local / Self-Hosted (highlighted box) ===== */}
      <div className={styles.localModelBox}>
        <div className={styles.localModelHeader}>
          <Icon name="Server" size={16} className={styles.bannerIconAmber} />
          <span className={styles.localModelTitle}>Local / Self-Hosted Models</span>
          {llmStatus?.configured && <Icon name="CheckCircle" size={14} className={styles.checkIcon} />}
          <SaveHint state={savedState["local"]} styles={styles} />
        </div>
        <p className={styles.localModelDesc}>
          Add the models your OpenAI-compatible servers run — Ollama, LM Studio, vLLM, or hosted gateways
          (Together, Groq, OpenRouter). Models can be on different servers. Leave empty to use the default
          OpenAI models above.
        </p>

        <div className={styles.localModelList}>
          <span className={styles.settingLabel}>Models (server URL and model name)</span>
          {modelRows.map((row, i) => (
            <div key={row.key} className={styles.localModelRow}>
              <input
                aria-label={`Server URL for model ${i + 1}`}
                className={styles.modalInputMono}
                value={row.baseUrl}
                onChange={e => updateRow(row.key, 'baseUrl', e.target.value)}
                placeholder="http://localhost:11434/v1"
              />
              <input
                aria-label={`Name of model ${i + 1}`}
                className={styles.modalInputMono}
                value={row.name}
                onChange={e => updateRow(row.key, 'name', e.target.value)}
                placeholder="llama3.1:70b"
              />
              <button
                type="button"
                className={styles.btnSecondarySmall}
                onClick={() => removeModelRow(row.key)}
                aria-label={`Remove model ${i + 1}`}
              >
                <Icon name="X" size={16} />
              </button>
            </div>
          ))}
          {llmDraft.duplicate && (
            <span className={styles.saveHintErr}>
              &ldquo;{llmDraft.duplicate}&rdquo; is listed more than once. Each model name must be unique.
            </span>
          )}
          <div>
            <button type="button" className={styles.btnSecondarySmall} onClick={addModelRow}>
              <Icon name="Plus" size={14} /> Add model
            </button>
          </div>
        </div>

        <div>
          <label className={styles.settingLabel} htmlFor={`${id}-llm-chat-model`}>Chat Model</label>
          <select
            id={`${id}-llm-chat-model`}
            className={styles.select}
            value={llmDraft.chat}
            onChange={e => updateLlm({ chat: e.target.value })}
            disabled={!llmDraft.names.length}
          >
            {!llmDraft.names.length && <option value="">Add a model above</option>}
            {llmDraft.names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className={styles.helpText}>Answers in chat.</span>
        </div>

        <div>
          <label className={styles.settingLabel} htmlFor={`${id}-llm-drafting-model`}>Drafting Model</label>
          <select
            id={`${id}-llm-drafting-model`}
            className={styles.select}
            value={llmDraft.drafting}
            onChange={e => updateLlm({ drafting: e.target.value })}
          >
            <option value="">Same as chat model</option>
            {llmDraft.names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className={styles.helpText}>Drafting documents: the drafting workspace, draft mode in contract chat, and workflow draft steps.</span>
        </div>

        <div>
          <label className={styles.settingLabel} htmlFor={`${id}-llm-utility-model`}>Utility Model</label>
          <select
            id={`${id}-llm-utility-model`}
            className={styles.select}
            value={llmDraft.utility}
            onChange={e => updateLlm({ utility: e.target.value })}
          >
            <option value="">Same as chat model</option>
            {llmDraft.names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className={styles.helpText}>
            Many small background jobs: routing questions, checking claims, pulling contract terms. A small, fast model works well here.
          </span>
        </div>

        <div>
          <label className={styles.settingLabel} htmlFor={`${id}-llm-research-model`}>Research Model</label>
          <select
            id={`${id}-llm-research-model`}
            className={styles.select}
            value={llmDraft.research}
            onChange={e => updateLlm({ research: e.target.value })}
          >
            <option value="">Same as utility model</option>
            {llmDraft.names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className={styles.helpText}>Deep research: the plan, each research step and the final write-up.</span>
        </div>

        {llmStatus?.configured && llmStatus.embeddings_provider && llmStatus.embeddings_provider !== 'self_hosted' && (
          <p className={styles.bannerTextAmber}>
            Embeddings don&apos;t use these model servers: they go to {llmStatus.embeddings_provider}, and uploaded
            document text is sent there when it is indexed. To keep it on your network, set up a local embeddings
            server in the Embedding tab.
          </p>
        )}

        {llmStatus?.configured && (
          <div className={styles.boxActions}>
            <button className={styles.brandingResetBtn} onClick={handleClearLlm}>Revert to OpenAI</button>
          </div>
        )}
      </div>

    </div>
  );
};
