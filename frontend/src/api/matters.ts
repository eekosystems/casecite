import { api } from './client';
import type {
  ChatSessionListResponse,
  DocumentListResponse,
  Matter,
  MatterActivityEvent,
  MatterAnalysisItem,
  MatterDetail,
  MatterRetention,
  MatterWall,
  MatterFacts,
} from './types';

const matterPath = (matterId: string, suffix = '') => `/matters/${encodeURIComponent(matterId)}${suffix}`;

Object.assign(api, {
  async listMatters(): Promise<{ matters: Matter[] }> {
    return api.request('/matters');
  },

  async createMatter(name: string, clientName: string | null = null): Promise<Matter> {
    return api.request('/matters', {
      method: 'POST',
      body: JSON.stringify({ name, client_name: clientName || null }),
    });
  },

  async getMatter(matterId: string): Promise<MatterDetail> {
    return api.request(matterPath(matterId));
  },

  async setMatterFacts(matterId: string, facts: string): Promise<MatterFacts> {
    return api.request(matterPath(matterId, '/facts'), {
      method: 'PUT',
      body: JSON.stringify({ facts }),
    });
  },

  async updateMatter(
    matterId: string,
    changes: { name?: string; client_name?: string | null; retention_days?: number },
  ): Promise<Matter> {
    return api.request(matterPath(matterId), { method: 'PATCH', body: JSON.stringify(changes) });
  },

  async setMatterHold(matterId: string, on: boolean, reason: string | null = null): Promise<Matter> {
    return api.request(matterPath(matterId, '/hold'), {
      method: 'POST',
      body: JSON.stringify({ on, reason }),
    });
  },

  async getMatterRetention(matterId: string): Promise<MatterRetention> {
    return api.request(matterPath(matterId, '/retention'));
  },

  async listMatterWalls(matterId: string): Promise<{ matter_id: string; walls: MatterWall[] }> {
    return api.request(matterPath(matterId, '/walls'));
  },

  async addMatterWall(
    matterId: string, email: string, reason: string | null = null,
  ): Promise<{ matter_id: string; walls: MatterWall[] }> {
    return api.request(matterPath(matterId, '/walls'), {
      method: 'POST',
      body: JSON.stringify({ email, reason }),
    });
  },

  async removeMatterWall(matterId: string, userId: string): Promise<{ matter_id: string; walls: MatterWall[] }> {
    return api.request(matterPath(matterId, `/walls/${encodeURIComponent(userId)}`), { method: 'DELETE' });
  },

  async deleteMatter(matterId: string): Promise<{ status: string; id: string }> {
    return api.request(matterPath(matterId), { method: 'DELETE' });
  },

  async addMatterMember(matterId: string, email: string): Promise<MatterDetail> {
    return api.request(matterPath(matterId, '/members'), {
      method: 'POST',
      body: JSON.stringify({ email }),
    });
  },

  async removeMatterMember(matterId: string, userId: string): Promise<MatterDetail> {
    return api.request(matterPath(matterId, `/members/${encodeURIComponent(userId)}`), {
      method: 'DELETE',
    });
  },

  async transferMatter(matterId: string, newOwnerId: string): Promise<MatterDetail> {
    return api.request(matterPath(matterId, '/transfer'), {
      method: 'POST',
      body: JSON.stringify({ new_owner_id: newOwnerId }),
    });
  },

  async getMatterDocuments(matterId: string): Promise<DocumentListResponse> {
    return api.request(matterPath(matterId, '/documents'));
  },

  async moveDocumentsToMatter(
    matterId: string,
    documentIds: string[],
  ): Promise<{ status: string; matter_id: string; moved: string[] }> {
    return api.request(matterPath(matterId, '/documents/move'), {
      method: 'POST',
      body: JSON.stringify({ document_ids: documentIds }),
    });
  },

  async getMatterChats(matterId: string): Promise<ChatSessionListResponse> {
    return api.request(matterPath(matterId, '/chats'));
  },

  async getMatterAnalyses(matterId: string): Promise<{ analyses: MatterAnalysisItem[] }> {
    return api.request(matterPath(matterId, '/analyses'));
  },

  async getMatterActivity(matterId: string, limit = 50): Promise<{ events: MatterActivityEvent[] }> {
    return api.request(matterPath(matterId, `/activity?limit=${limit}`));
  },
});
