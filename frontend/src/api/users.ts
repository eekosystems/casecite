import { api, API_BASE_URL } from './client';
import type {
  AdminUser, AuditLogEntry, AuditLogQuery, AuditVerifyResult, DeleteUserOptions, InviteUserResult,
  RolePermissionsMatrix, UserOwnership,
} from './types';

function auditQueryString(query: AuditLogQuery = {}): string {
  const params = new URLSearchParams();
  if (query.startDate) params.set('start_date', query.startDate);
  if (query.endDate) params.set('end_date', query.endDate);
  if (query.eventType) params.set('event_type', query.eventType);
  if (query.userId) params.set('user_id', query.userId);
  if (query.limit) params.set('limit', String(query.limit));
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

Object.assign(api, {
  async getUsers(): Promise<{ users: AdminUser[] }> {
    return api.request('/admin/users');
  },

  async inviteUser(email: string, name: string, role: string): Promise<InviteUserResult> {
    return api.request('/admin/users/invite', {
      method: 'POST',
      body: JSON.stringify({ email, name, role }),
    });
  },

  async updateUserRole(userId: string, role: string): Promise<AdminUser> {
    return api.request(`/admin/users/${userId}/role`, {
      method: 'PATCH',
      body: JSON.stringify({ role }),
    });
  },

  async getRolePermissions(): Promise<RolePermissionsMatrix> {
    return api.request('/admin/users/roles');
  },

  async updateRolePermissions(role: string, permissions: string[]): Promise<RolePermissionsMatrix> {
    return api.request(`/admin/users/roles/${role}`, {
      method: 'PUT',
      body: JSON.stringify({ permissions }),
    });
  },

  async resetRolePermissions(): Promise<RolePermissionsMatrix> {
    return api.request('/admin/users/roles/reset', { method: 'POST' });
  },

  // ── Offboarding ─────────────────────────────────────────────────
  async setUserActive(userId: string, isActive: boolean): Promise<AdminUser> {
    return api.request(`/admin/users/${encodeURIComponent(userId)}/active`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active: isActive }),
    });
  },

  async getUserOwnership(userId: string): Promise<{ user_id: string; owned: UserOwnership }> {
    return api.request(`/admin/users/${encodeURIComponent(userId)}/ownership`);
  },

  async deleteUser(userId: string, options: DeleteUserOptions = {}): Promise<unknown> {
    // The server refuses (409) to delete a user who owns work product unless
    // told to transfer it (mode=transfer&transfer_to=...) or purge it.
    const params = new URLSearchParams();
    if (options.mode) params.set('mode', options.mode);
    if (options.transferTo) params.set('transfer_to', options.transferTo);
    const query = params.toString();
    return api.request(`/admin/users/${encodeURIComponent(userId)}${query ? `?${query}` : ''}`, {
      method: 'DELETE',
      timeout: 120000,
    });
  },

  async exportUserData(userId: string): Promise<Record<string, unknown>> {
    return api.request(`/admin/users/${encodeURIComponent(userId)}/export`, { timeout: 120000 });
  },

  async forceSignOutUser(userId: string): Promise<unknown> {
    return api.request(`/admin/users/${encodeURIComponent(userId)}/sessions/revoke`, { method: 'POST' });
  },

  async resetUserMfa(userId: string): Promise<unknown> {
    return api.request(`/admin/users/${encodeURIComponent(userId)}/mfa/reset`, { method: 'POST' });
  },

  // ── Audit trail ─────────────────────────────────────────────────
  async getAuditLogs(query: AuditLogQuery = {}): Promise<{ logs: AuditLogEntry[]; count: number }> {
    return api.request(`/admin/audit/logs${auditQueryString(query)}`);
  },

  async verifyAuditChain(query: Pick<AuditLogQuery, 'startDate' | 'endDate'> = {}): Promise<AuditVerifyResult> {
    return api.request(`/admin/audit/verify${auditQueryString(query)}`, { timeout: 120000 });
  },

  async exportAuditLogs(query: AuditLogQuery = {}): Promise<Blob> {
    const resp = await api.authFetch(`${API_BASE_URL}/admin/audit/export${auditQueryString(query)}`);
    if (!resp.ok) throw new Error(`Audit export failed (${resp.status})`);
    return resp.blob();
  },
});
