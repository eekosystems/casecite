import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api', () => ({
  api: {
    updateSettings: vi.fn(),
    getSettings: vi.fn(),
    reindexDocuments: vi.fn(),
    getDocuments: vi.fn().mockResolvedValue({ documents: [] }),
    getAllDocuments: vi.fn().mockResolvedValue([]),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

import { useSettingsStore, loadSavedRagSettings } from './settingsStore';
import { useUIStore } from './uiStore';
import { api } from '../api';

const defaultRagSettings = {
  vectorDb: 'chroma', indexName: 'casecite-legal-docs', embeddingModel: 'text-embedding-3-small',
  dimensions: 1536, chunkSize: 512, chunkOverlap: 128, similarityThreshold: 0.25,
  topK: 10, enableReranking: true, hybridSearch: true, citationVerification: true,
  contextCompression: false, queryExpansion: true, sourceTracking: true,
  llmModel: '', temperature: 0.1, maxTokens: 4096,
};

describe('settingsStore', () => {
  beforeEach(() => {
    useSettingsStore.setState(useSettingsStore.getInitialState(), true);
    useUIStore.setState(useUIStore.getInitialState(), true);
    vi.clearAllMocks();
    localStorage.clear();
  });

  // ==================== loadSavedRagSettings ====================

  it('loadSavedRagSettings returns defaults when no saved settings', () => {
    const settings = loadSavedRagSettings();
    expect(settings).toEqual(defaultRagSettings);
  });

  it('loadSavedRagSettings merges saved settings with defaults', () => {
    localStorage.setItem('casecite_rag_settings', JSON.stringify({ topK: 20, llmModel: 'gpt-5.4-mini' }));
    const settings = loadSavedRagSettings();
    expect(settings.topK).toBe(20);
    expect(settings.llmModel).toBe('gpt-5.4-mini');
    expect(settings.vectorDb).toBe('chroma');
  });

  it('migrates settings saved under the old key prefix, once', () => {
    localStorage.setItem('wl_rag_settings', JSON.stringify({ topK: 33 }));
    localStorage.setItem('wl_citations', '[]');
    localStorage.setItem('wl_search_history', '[]');

    expect(loadSavedRagSettings().topK).toBe(33);

    expect(localStorage.getItem('wl_rag_settings')).toBeNull();
    expect(localStorage.getItem('wl_citations')).toBeNull();
    expect(localStorage.getItem('wl_search_history')).toBeNull();
    expect(JSON.parse(localStorage.getItem('casecite_rag_settings') as string).topK).toBe(33);
  });

  it('does not let a stale old-prefix value overwrite current settings', () => {
    localStorage.setItem('casecite_rag_settings', JSON.stringify({ topK: 12 }));
    localStorage.setItem('wl_rag_settings', JSON.stringify({ topK: 33 }));
    expect(loadSavedRagSettings().topK).toBe(12);
    expect(localStorage.getItem('wl_rag_settings')).toBeNull();
  });

  it('loadSavedRagSettings returns defaults on invalid JSON', () => {
    localStorage.setItem('casecite_rag_settings', 'not valid json');
    const settings = loadSavedRagSettings();
    expect(settings).toEqual(defaultRagSettings);
  });

  // ==================== init (lazy hydration) ====================

  it('init hydrates ragSettings from localStorage', () => {
    localStorage.setItem('casecite_rag_settings', JSON.stringify({ topK: 42 }));
    useSettingsStore.getState().init();
    expect(useSettingsStore.getState().ragSettings.topK).toBe(42);
    expect(useSettingsStore.getState()._initialized).toBe(true);
  });

  it('init is a no-op after the first call', () => {
    localStorage.setItem('casecite_rag_settings', JSON.stringify({ topK: 42 }));
    useSettingsStore.getState().init();
    localStorage.setItem('casecite_rag_settings', JSON.stringify({ topK: 7 }));
    useSettingsStore.getState().init();
    expect(useSettingsStore.getState().ragSettings.topK).toBe(42);
  });

  it('does not read localStorage before init', () => {
    localStorage.setItem('casecite_rag_settings', JSON.stringify({ topK: 99 }));
    // State was reset in beforeEach; without init() the defaults must hold.
    expect(useSettingsStore.getState().ragSettings).toEqual(defaultRagSettings);
  });

  // ==================== Initial State ====================

  it('has correct initial state', () => {
    const state = useSettingsStore.getState();
    expect(state.ragSettings).toEqual(defaultRagSettings);
    expect(state.systemStats).toEqual({ totalDocuments: 0, totalEmbeddings: 0, isHealthy: null });
    expect(state.isReindexing).toBe(false);
    expect(state.serverSettingsLoaded).toBe(false);
  });

  // ==================== Setters ====================

  it('setRagSettings updates ragSettings', () => {
    const newSettings = { ...defaultRagSettings, topK: 25 };
    useSettingsStore.getState().setRagSettings(newSettings);
    expect(useSettingsStore.getState().ragSettings.topK).toBe(25);
  });

  it('setSystemStats updates systemStats with object', () => {
    useSettingsStore.getState().setSystemStats({ totalDocuments: 10, totalEmbeddings: 100, isHealthy: true });
    expect(useSettingsStore.getState().systemStats.totalDocuments).toBe(10);
  });

  it('setSystemStats supports functional update', () => {
    useSettingsStore.getState().setSystemStats((prev) => ({ ...prev, totalDocuments: prev.totalDocuments + 5 }));
    expect(useSettingsStore.getState().systemStats.totalDocuments).toBe(5);
  });

  it('setIsReindexing updates isReindexing', () => {
    useSettingsStore.getState().setIsReindexing(true);
    expect(useSettingsStore.getState().isReindexing).toBe(true);
  });

  // ==================== loadServerSettings ====================

  it('loadServerSettings maps the server payload and marks settings as loaded', async () => {
    (api.getSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
      vector_db: 'chroma', top_k: 7, temperature: 0, similarity_threshold: 0,
      citation_verification: false, max_tokens: 2048,
      contract_playbook: 'Never accept unlimited liability.',
      practice_profile: 'Commercial leasing',
      custom_system_prompt: 'Be terse.',
    });

    await expect(useSettingsStore.getState().loadServerSettings()).resolves.toBe(true);

    const st = useSettingsStore.getState();
    expect(st.serverSettingsLoaded).toBe(true);
    expect(st.ragSettings.topK).toBe(7);
    // 0 is a real value, not "unset".
    expect(st.ragSettings.temperature).toBe(0);
    expect(st.ragSettings.similarityThreshold).toBe(0);
    expect(st.ragSettings.citationVerification).toBe(false);
    expect(st.ragSettings.maxTokens).toBe(2048);
    expect(st.ragSettings.contract_playbook).toBe('Never accept unlimited liability.');
    expect(st.ragSettings.custom_research_prompt).toBeNull();
  });

  it('loadServerSettings records a failure without marking settings as loaded', async () => {
    (api.getSettings as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'));
    await expect(useSettingsStore.getState().loadServerSettings()).resolves.toBe(false);
    expect(useSettingsStore.getState().serverSettingsLoaded).toBe(false);
    expect(useSettingsStore.getState().serverSettingsError).toBe(true);
  });

  // ==================== handleSaveSettings ====================

  it('refuses to save before the server settings have loaded (a blind save would wipe them)', async () => {
    (api.getSettings as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'));

    const ok = await useSettingsStore.getState().handleSaveSettings({ ...defaultRagSettings, topK: 20 });

    expect(ok).toBe(false);
    expect(api.updateSettings).not.toHaveBeenCalled();
    expect(useSettingsStore.getState().ragSettings.topK).toBe(defaultRagSettings.topK);
    expect(useUIStore.getState().toasts.some(t => t.type === 'error')).toBe(true);
  });

  it('a save after a normal sign-in carries the stored playbook, profile and prompts', async () => {
    (api.getSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
      contract_playbook: 'Playbook', practice_area: 'Leasing', practice_profile: 'Profile',
      custom_system_prompt: 'System', custom_strategy_prompt: 'Strategy',
    });
    (api.updateSettings as ReturnType<typeof vi.fn>).mockResolvedValue({});
    await useSettingsStore.getState().loadServerSettings();

    // The user only moves the Top K slider.
    const edited = { ...useSettingsStore.getState().ragSettings, topK: 15 };
    await expect(useSettingsStore.getState().handleSaveSettings(edited)).resolves.toBe(true);

    expect(api.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      top_k: 15,
      contract_playbook: 'Playbook',
      practice_area: 'Leasing',
      practice_profile: 'Profile',
      custom_system_prompt: 'System',
      custom_strategy_prompt: 'Strategy',
    }));
  });

  it('sends the advanced toggles the backend supports', async () => {
    (api.updateSettings as ReturnType<typeof vi.fn>).mockResolvedValue({});
    useSettingsStore.setState({ serverSettingsLoaded: true });

    await useSettingsStore.getState().handleSaveSettings({
      ...defaultRagSettings,
      citationVerification: false, contextCompression: true, queryExpansion: false, sourceTracking: false,
    });

    expect(api.updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      citation_verification: false,
      context_compression: true,
      query_expansion: false,
      source_tracking: false,
    }));
  });

  it('handleSaveSettings saves settings locally and to backend', async () => {
    (api.updateSettings as ReturnType<typeof vi.fn>).mockResolvedValue({});
    useSettingsStore.setState({ serverSettingsLoaded: true });

    const newSettings = { ...defaultRagSettings, topK: 20 };
    await useSettingsStore.getState().handleSaveSettings(newSettings);

    expect(useSettingsStore.getState().ragSettings.topK).toBe(20);
    expect(api.updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({ top_k: 20 })
    );
  });

  it('handleSaveSettings resolves true on success', async () => {
    (api.updateSettings as ReturnType<typeof vi.fn>).mockResolvedValue({});
    useSettingsStore.setState({ serverSettingsLoaded: true });
    await expect(
      useSettingsStore.getState().handleSaveSettings({ ...defaultRagSettings, topK: 20 })
    ).resolves.toBe(true);
    expect(useUIStore.getState().toasts).toHaveLength(0);
  });

  it('handleSaveSettings rolls back, toasts, and resolves false when the backend fails', async () => {
    (api.updateSettings as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Backend error'));
    useSettingsStore.setState({ serverSettingsLoaded: true, ragSettings: { ...defaultRagSettings, topK: 5 } });

    const newSettings = { ...defaultRagSettings, topK: 30 };
    const ok = await useSettingsStore.getState().handleSaveSettings(newSettings);

    expect(ok).toBe(false);
    expect(useSettingsStore.getState().ragSettings.topK).toBe(5);
    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.type === 'error' && t.message.includes('Backend error'))).toBe(true);
  });

  // ==================== handleReindex ====================

  it('handleReindex reindexes and shows success toast', async () => {
    (api.reindexDocuments as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 'completed', reindexed: 4, errors: [] });

    await useSettingsStore.getState().handleReindex();

    expect(useSettingsStore.getState().isReindexing).toBe(false);
    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.type === 'success' && t.message.startsWith('Reindex complete'))).toBe(true);
  });

  it('handleReindex names the documents that could not be re-indexed', async () => {
    (api.reindexDocuments as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: 'completed', reindexed: 2, errors: ['Lease.pdf: file not found'],
    });

    await useSettingsStore.getState().handleReindex();

    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.type === 'success')).toBe(false);
    expect(toasts.some(t => t.message.includes('1 could not be re-indexed') && t.message.includes('Lease.pdf: file not found'))).toBe(true);
  });

  it('handleReindex shows error toast on failure', async () => {
    (api.reindexDocuments as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Reindex error'));

    await useSettingsStore.getState().handleReindex();

    expect(useSettingsStore.getState().isReindexing).toBe(false);
    const toasts = useUIStore.getState().toasts;
    expect(toasts.some(t => t.message === 'Reindex failed')).toBe(true);
  });

});
