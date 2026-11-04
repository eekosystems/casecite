import { api } from './client';
import type { AuthResponse, AzureConfig, UserInfo, MessageResponse, SessionInfo } from './types';

Object.assign(api, {
  async login(email: string, password: string): Promise<AuthResponse> {
    const result = await api.request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    api.setToken(result.access_token);
    return result;
  },

  async getAzureConfig(): Promise<AzureConfig> {
    return api.request('/auth/azure/config');
  },

  async loginWithAzure(idToken: string): Promise<AuthResponse> {
    const result = await api.request('/auth/azure/login', {
      method: 'POST',
      body: JSON.stringify({ token: idToken }),
    });
    api.setToken(result.access_token);
    return result;
  },

  async logout(): Promise<void> {
    try { await api.request('/auth/logout', { method: 'POST' }); } catch {}
    // The refresh token lives in an httpOnly cookie the backend just cleared.
    api.token = null;
  },

  async getCurrentUser(): Promise<UserInfo> {
    return api.request('/auth/me');
  },

  async register(email: string, name: string, password: string, company: string, bootstrapToken?: string): Promise<AuthResponse> {
    const result = await api.request('/auth/register', {
      method: 'POST',
      // The setup token is only needed (and only sent) when creating the
      // instance's first administrator account.
      body: JSON.stringify({ email, name, password, company, ...(bootstrapToken ? { bootstrap_token: bootstrapToken } : {}) }),
    });
    api.setToken(result.access_token);
    return result;
  },

  async forgotPassword(email: string): Promise<MessageResponse> {
    return api.request('/auth/forgot-password', {
      method: 'POST',
      body: JSON.stringify({ email }),
    });
  },

  async verifyResetToken(token: string): Promise<{ valid: boolean }> {
    return api.request('/auth/verify-reset-token', {
      method: 'POST',
      body: JSON.stringify({ token }),
    });
  },

  async resetPassword(token: string, newPassword: string): Promise<MessageResponse> {
    return api.request('/auth/reset-password', {
      method: 'POST',
      body: JSON.stringify({ token, new_password: newPassword }),
    });
  },

  async changePassword(currentPassword: string, newPassword: string): Promise<{ status: string }> {
    return api.request('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
  },

  async getSessions(): Promise<{ sessions: SessionInfo[] }> {
    return api.request('/auth/sessions');
  },

  async logoutAllSessions(): Promise<{ status: string }> {
    const result = await api.request('/auth/logout/all', { method: 'POST' });
    api.token = null;
    return result;
  },
});
