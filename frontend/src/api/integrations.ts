import { api } from './client';
import type {
  CourtListenerStatus,
  LocalLlmStatus,
  LocalLlmConfig,
  ConnectorCredentialStatus,
} from './types';

Object.assign(api, {
  // ----- Connector (file picker) credentials: Google / Microsoft / Box / Dropbox -----
  async getConnectorCredentials(): Promise<{ providers: ConnectorCredentialStatus[] }> {
    return api.request('/admin/integrations/connectors');
  },

  async setConnectorCredentials(
    provider: string,
    values: Record<string, string>,
  ): Promise<ConnectorCredentialStatus> {
    return api.request(`/admin/integrations/connectors/${encodeURIComponent(provider)}`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    });
  },

  async clearConnectorCredentials(provider: string): Promise<ConnectorCredentialStatus> {
    return api.request(`/admin/integrations/connectors/${encodeURIComponent(provider)}`, {
      method: 'DELETE',
    });
  },

  // ----- CourtListener (Free Law Project) instance token -----
  async getCourtListenerStatus(): Promise<CourtListenerStatus> {
    return api.request('/admin/integrations/courtlistener');
  },

  async setCourtListenerToken(apiToken: string): Promise<CourtListenerStatus & { status: string }> {
    return api.request('/admin/integrations/courtlistener', {
      method: 'POST',
      body: JSON.stringify({ api_token: apiToken }),
    });
  },

  async clearCourtListenerToken(): Promise<CourtListenerStatus & { status: string }> {
    return api.request('/admin/integrations/courtlistener', { method: 'DELETE' });
  },

  // ----- Custom / local OpenAI-compatible model endpoint -----
  async getLocalLlmStatus(): Promise<LocalLlmStatus> {
    return api.request('/admin/integrations/local-llm');
  },

  async setLocalLlm(config: LocalLlmConfig): Promise<LocalLlmStatus & { status: string }> {
    return api.request('/admin/integrations/local-llm', {
      method: 'POST',
      body: JSON.stringify({
        base_url: config.baseUrl,
        chat_model: config.chatModel,
        utility_model: config.utilityModel || null,
        research_model: config.researchModel || null,
      }),
    });
  },

  async clearLocalLlm(): Promise<LocalLlmStatus & { status: string }> {
    return api.request('/admin/integrations/local-llm', { method: 'DELETE' });
  },
});
