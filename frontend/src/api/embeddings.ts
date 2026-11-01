import { api } from './client';
import type { EmbeddingConfig, EmbeddingInfo, EmbeddingStatus } from './types';

Object.assign(api, {
  // Read-only view for every signed-in user.
  async getEmbeddingInfo(): Promise<EmbeddingInfo> {
    return api.request('/settings/embeddings');
  },

  // ----- Admin: embedding model and server -----
  async getEmbeddingStatus(): Promise<EmbeddingStatus> {
    return api.request('/admin/integrations/embeddings');
  },

  async setEmbeddings(config: EmbeddingConfig): Promise<EmbeddingStatus & { status: string }> {
    const body: Record<string, unknown> = {
      base_url: config.baseUrl,
      model: config.model,
      dimensions: config.dimensions || null,
    };
    // Omitted: keep the saved key. Only sent when the admin typed one.
    if (config.apiKey) body.api_key = config.apiKey;
    return api.request('/admin/integrations/embeddings', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  async clearEmbeddings(): Promise<EmbeddingStatus & { status: string }> {
    return api.request('/admin/integrations/embeddings', { method: 'DELETE' });
  },

  async reindexEmbeddings(): Promise<EmbeddingStatus & { status: string }> {
    return api.request('/admin/integrations/embeddings/reindex', { method: 'POST' });
  },
});
