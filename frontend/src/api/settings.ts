import { api } from './client';
import type { InstanceModels, RAGSettings, ReindexResult } from './types';

Object.assign(api, {
  async getSettings(): Promise<RAGSettings> {
    return api.request('/settings/rag');
  },

  async updateSettings(settings: Record<string, unknown>): Promise<RAGSettings> {
    return api.request('/settings/rag', {
      method: 'PUT',
      body: JSON.stringify(settings),
    });
  },

  async getKeyStatus(): Promise<Record<string, boolean>> {
    return api.request('/user/keys/status');
  },

  async getMaskedKeys(): Promise<Record<string, string | null>> {
    return api.request('/user/keys/masked');
  },

  async saveApiKey(keyType: string, apiKey: string): Promise<void> {
    return api.request('/user/keys/save', {
      method: 'POST',
      body: JSON.stringify({ key_type: keyType, api_key: apiKey }),
    });
  },

  async deleteApiKey(keyType: string): Promise<void> {
    return api.request(`/user/keys/${encodeURIComponent(keyType)}`, { method: 'DELETE' });
  },

  async getPromptDefaults(): Promise<Record<string, unknown>> {
    return api.request('/settings/prompts/defaults');
  },

  async getInstanceModels(): Promise<InstanceModels> {
    return api.request('/settings/models');
  },

  async reindexDocuments(): Promise<ReindexResult> {
    return api.request('/settings/reindex', { method: 'POST' });
  },
});
