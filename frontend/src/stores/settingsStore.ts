import { create } from 'zustand';
import { api } from '../api';
import logger from '../utils/logger';
import { RAG_SETTINGS_KEY, migrateLegacyStorage } from '../utils/storageKeys';
import { useUIStore } from './uiStore';
import { registerReset } from './resetRegistry';
import type { RagSettings, SystemStats } from '../types';

const defaultRagSettings: RagSettings = {
  vectorDb: 'chroma', indexName: 'casecite-legal-docs', embeddingModel: 'text-embedding-3-small',
  // 0.25: text-embedding-3 similarities run low (relevant ≈ 0.30-0.50) — a
  // higher floor silently discards real matches.
  dimensions: 1536, chunkSize: 512, chunkOverlap: 128, similarityThreshold: 0.25,
  topK: 10, enableReranking: true, hybridSearch: true, citationVerification: true,
  contextCompression: false, queryExpansion: true, sourceTracking: true,
  // 4096 matches the server-side default (RAGSettings.max_tokens).
  llmModel: 'gpt-5.5', temperature: 0.1, maxTokens: 4096,
};

export function loadSavedRagSettings(): RagSettings {
  migrateLegacyStorage();
  try {
    const saved = localStorage.getItem(RAG_SETTINGS_KEY);
    if (saved) {
      const parsed = JSON.parse(saved);
      return { ...defaultRagSettings, ...parsed };
    }
  } catch (e) { logger.debug('Could not load RAG settings from localStorage:', e); }
  return defaultRagSettings;
}

/** `value ?? fallback`, but typed for the loosely-typed settings payload. */
function pick<T>(value: unknown, fallback: T): T {
  return (value === undefined || value === null ? fallback : value) as T;
}

/** Map the server's snake_case RAG settings onto the client shape. */
function mapServerSettings(server: Record<string, unknown>): RagSettings {
  const text = (key: string) => pick<string | null>(server[key], null);
  return {
    vectorDb: pick(server.vector_db, defaultRagSettings.vectorDb),
    indexName: pick(server.index_name, defaultRagSettings.indexName),
    embeddingModel: pick(server.embedding_model, defaultRagSettings.embeddingModel),
    dimensions: pick(server.dimensions, defaultRagSettings.dimensions),
    chunkSize: pick(server.chunk_size, defaultRagSettings.chunkSize),
    chunkOverlap: pick(server.chunk_overlap, defaultRagSettings.chunkOverlap),
    // `??` semantics throughout: 0 is a legitimate threshold and temperature.
    similarityThreshold: pick(server.similarity_threshold, defaultRagSettings.similarityThreshold),
    topK: pick(server.top_k, defaultRagSettings.topK),
    enableReranking: pick(server.enable_reranking, true),
    hybridSearch: pick(server.hybrid_search, true),
    citationVerification: pick(server.citation_verification, true),
    contextCompression: pick(server.context_compression, false),
    queryExpansion: pick(server.query_expansion, true),
    sourceTracking: pick(server.source_tracking, true),
    llmModel: pick(server.llm_model, defaultRagSettings.llmModel),
    temperature: pick(server.temperature, defaultRagSettings.temperature),
    maxTokens: pick(server.max_tokens, defaultRagSettings.maxTokens),
    contract_playbook: text('contract_playbook'),
    practice_area: text('practice_area'),
    practice_profile: text('practice_profile'),
    custom_system_prompt: text('custom_system_prompt'),
    custom_grounding_rules: text('custom_grounding_rules'),
    custom_factual_prompt: text('custom_factual_prompt'),
    custom_research_prompt: text('custom_research_prompt'),
    custom_case_prompt: text('custom_case_prompt'),
    custom_document_prompt: text('custom_document_prompt'),
    custom_compliance_prompt: text('custom_compliance_prompt'),
    custom_strategy_prompt: text('custom_strategy_prompt'),
    workflow_email_notifications: pick(server.workflow_email_notifications, true),
  };
}

export interface SettingsState {
  _initialized: boolean;
  /** Hydrate persisted settings from localStorage (lazy — never at import time). */
  init: () => void;
  ragSettings: RagSettings;
  setRagSettings: (settings: RagSettings) => void;
  /**
   * True once this session's settings came from the server. Saving is a full
   * replace (PUT), so saving before this is set would overwrite the stored
   * playbook, practice profile and custom prompts with blanks.
   */
  serverSettingsLoaded: boolean;
  serverSettingsError: boolean;
  /** Load the signed-in user's settings from the server. Resolves true on success. */
  loadServerSettings: () => Promise<boolean>;
  systemStats: SystemStats;
  setSystemStats: (stats: SystemStats | ((prev: SystemStats) => SystemStats)) => void;
  isReindexing: boolean;
  setIsReindexing: (val: boolean) => void;
  /** Resolves true when the server accepted the settings; false after a rollback. */
  handleSaveSettings: (newSettings: RagSettings) => Promise<boolean>;
  handleReindex: () => Promise<void>;
  refreshSystemStats: () => Promise<void>;
}

export const useSettingsStore = create<SettingsState>((set, _get) => ({
  _initialized: false,
  init: () => {
    if (_get()._initialized) return;
    set({ _initialized: true, ragSettings: loadSavedRagSettings() });
  },
  ragSettings: defaultRagSettings,
  setRagSettings: (settings) => set({ ragSettings: settings }),
  serverSettingsLoaded: false,
  serverSettingsError: false,
  loadServerSettings: async () => {
    try {
      const serverSettings = await api.getSettings();
      set({
        ragSettings: mapServerSettings(serverSettings as Record<string, unknown>),
        serverSettingsLoaded: true,
        serverSettingsError: false,
      });
      logger.debug('Loaded RAG settings from backend');
      return true;
    } catch (e) {
      logger.debug('Could not load RAG settings from backend:', e instanceof Error ? e.message : String(e));
      set({ serverSettingsLoaded: false, serverSettingsError: true });
      return false;
    }
  },
  // `null` until /health has answered — the header must not claim "healthy"
  // before (or without) a successful check.
  systemStats: { totalDocuments: 0, totalEmbeddings: 0, isHealthy: null },
  setSystemStats: (stats) => set((state) => ({ systemStats: typeof stats === 'function' ? stats(state.systemStats) : stats })),
  isReindexing: false,
  setIsReindexing: (val) => set({ isReindexing: val }),

  // Optimistic: the new values apply immediately, but a failed save rolls
  // them back and tells the user — a silent failure left the modal's values
  // on screen while the server (and the next reload) still had the old ones.
  handleSaveSettings: async (newSettings) => {
    const { addToast } = useUIStore.getState();
    if (!_get().serverSettingsLoaded) {
      // One more attempt, then refuse: a blind save would wipe stored values.
      const loaded = await _get().loadServerSettings();
      addToast(
        loaded
          ? 'Your saved settings had not finished loading — they are loaded now. Review and save again.'
          : 'Could not load your saved settings from the server, so nothing was saved. Check your connection and try again.',
        loaded ? 'warning' : 'error',
      );
      return false;
    }
    const previous = _get().ragSettings;
    set({ ragSettings: newSettings });
    try {
      await api.updateSettings({
        vector_db: newSettings.vectorDb,
        index_name: newSettings.indexName,
        embedding_model: newSettings.embeddingModel,
        dimensions: newSettings.dimensions,
        chunk_size: newSettings.chunkSize,
        chunk_overlap: newSettings.chunkOverlap,
        similarity_threshold: newSettings.similarityThreshold,
        top_k: newSettings.topK,
        enable_reranking: newSettings.enableReranking,
        hybrid_search: newSettings.hybridSearch,
        citation_verification: newSettings.citationVerification,
        context_compression: newSettings.contextCompression,
        query_expansion: newSettings.queryExpansion,
        source_tracking: newSettings.sourceTracking,
        llm_model: newSettings.llmModel,
        temperature: newSettings.temperature,
        max_tokens: newSettings.maxTokens,
        contract_playbook: newSettings.contract_playbook ?? null,
        practice_area: newSettings.practice_area ?? null,
        practice_profile: newSettings.practice_profile ?? null,
        custom_system_prompt: newSettings.custom_system_prompt ?? null,
        custom_grounding_rules: newSettings.custom_grounding_rules ?? null,
        custom_factual_prompt: newSettings.custom_factual_prompt ?? null,
        custom_research_prompt: newSettings.custom_research_prompt ?? null,
        custom_case_prompt: newSettings.custom_case_prompt ?? null,
        custom_document_prompt: newSettings.custom_document_prompt ?? null,
        custom_compliance_prompt: newSettings.custom_compliance_prompt ?? null,
        custom_strategy_prompt: newSettings.custom_strategy_prompt ?? null,
        workflow_email_notifications: newSettings.workflow_email_notifications ?? true,
      });
      logger.debug('Settings saved to backend');
      return true;
    } catch (error) {
      logger.error('Failed to save settings:', error);
      set({ ragSettings: previous });
      const message = error instanceof Error ? error.message : String(error);
      addToast(`Could not save settings: ${message}`, 'error');
      return false;
    }
  },

  // The header pill's source of truth: the user's own document list. (It used
  // to read a stats field the health endpoint no longer returns, so it was
  // pinned at 0 forever.)
  refreshSystemStats: async () => {
    try {
      const docs = await api.getAllDocuments();
      const indexed = docs.filter((d) => d.status === 'indexed');
      set((state) => ({
        systemStats: {
          ...state.systemStats,
          totalDocuments: indexed.length,
          totalEmbeddings: indexed.reduce(
            (n, d) => n + ((d.chunk_count as number) || 0), 0
          ),
        },
      }));
    } catch (error) {
      logger.debug('Could not refresh document stats:', error);
    }
  },

  handleReindex: async () => {
    const { addToast } = useUIStore.getState();
    set({ isReindexing: true });
    try {
      const result = await api.reindexDocuments();
      const failed = result.errors?.length ?? 0;
      if (failed > 0) {
        // Say which documents failed — they are now marked failed, not indexed.
        const shown = result.errors.slice(0, 3).join('; ');
        const more = failed > 3 ? ` (and ${failed - 3} more)` : '';
        addToast(
          `Reindexed ${result.reindexed ?? 0} document(s); ${failed} could not be re-indexed: ${shown}${more}`,
          result.reindexed ? 'warning' : 'error',
        );
      } else {
        addToast(`Reindex complete — ${result.reindexed ?? 0} document(s)`, 'success');
      }
    } catch (error) {
      logger.error('Reindex error:', error);
      addToast('Reindex failed', 'error');
    }
    set({ isReindexing: false });
    _get().refreshSystemStats();
  },
}));

// Sign-out wipes the in-memory copy (the playbook and practice profile are
// confidential work product) — the server remains the source of truth. The
// system health reading is not user data and survives the reset.
registerReset(() => {
  const { systemStats } = useSettingsStore.getState();
  useSettingsStore.setState({
    ...useSettingsStore.getInitialState(),
    systemStats: { ...useSettingsStore.getInitialState().systemStats, isHealthy: systemStats.isHealthy },
  }, true);
});
