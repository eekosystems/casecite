import { api, API_BASE_URL } from './client';
import type { ChatResponse, ChatQueryOptions, ConversationExportPayload } from './types';

Object.assign(api, {
  async exportConversation(payload: ConversationExportPayload): Promise<Blob> {
    const resp = await api.authFetch(`${API_BASE_URL}/chat/export`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      throw new Error(resp.status === 501 ? 'PDF export is not available on this server.' : `Export failed (${resp.status})`);
    }
    return resp.blob();
  },

  async query(message: string, mode: string, options: ChatQueryOptions = {}, signal?: AbortSignal): Promise<ChatResponse> {
    return api.request('/chat', {
      method: 'POST',
      // Lets the user cancel a long-running answer.
      signal,
      // Strategy briefs and verified case-law answers read full opinions from
      // CourtListener — routinely past the 30s default abort. Give chat a
      // real budget; the backend bounds its own case-law stage.
      timeout: 180000,
      body: JSON.stringify({
        query: message,
        mode: mode,
        include_citations: true,
        // null = the server decides per message whether authority helps.
        include_case_law: options.includeCaseLaw ?? null,
        case_law_limit: options.caseLawLimit || 5,
        include_documents: options.includeDocuments !== false,
        document_filter: options.documentFilter || null,
        // Persisted session to append to; null lets the server start a new one.
        session_id: options.sessionId || null,
      }),
    });
  },
});
