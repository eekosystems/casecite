import { api } from './client';
import type {
  ConnectorAuthUrl, ConnectorDataResponse, ConnectorFoldersResult, ConnectorStatusResponse, MessageResponse,
  SyncStartResult, SyncStatusResult,
} from './types';

Object.assign(api, {
  async getConnectors(): Promise<ConnectorDataResponse> {
    return api.request('/connectors');
  },

  async connectConnector(connectorId: string): Promise<ConnectorAuthUrl> {
    return api.request(`/connectors/${connectorId}/auth`);
  },

  async disconnectConnector(connectorId: string): Promise<MessageResponse> {
    return api.request(`/connectors/${connectorId}/disconnect`, { method: 'POST' });
  },

  async listConnectorFolders(
    connectorId: string, parentId?: string, pageToken?: string,
  ): Promise<ConnectorFoldersResult> {
    const params = new URLSearchParams();
    if (parentId) params.set('parent_id', parentId);
    if (pageToken) params.set('page_token', pageToken);
    const query = params.toString();
    return api.request(`/connectors/${encodeURIComponent(connectorId)}/folders${query ? `?${query}` : ''}`);
  },

  async syncConnector(connectorId: string, scope: { folder_id: string } | { sync_all: true }): Promise<SyncStartResult> {
    return api.request(`/connectors/${connectorId}/sync`, {
      method: 'POST',
      body: JSON.stringify(scope),
    });
  },

  async getConnectorSyncStatus(connectorId: string, syncId: string): Promise<SyncStatusResult> {
    return api.request(`/connectors/${connectorId}/sync/${syncId}`);
  },

  async getConnectorStatus(connectorId: string): Promise<ConnectorStatusResponse> {
    return api.request(`/connectors/${connectorId}/status`);
  },
});
