import { useState, useEffect, useCallback, useId } from 'react';
import { api } from '../../../api';
import { useUIStore } from '../../../stores/uiStore';
import { useAuthStore } from '../../../stores/authStore';
import { Icon } from '../../shared/Icon';
import { downloadBlob } from '../../../utils/downloadBlob';
import type { AdminUser, RolePermissionsMatrix } from '../../../api/types';

interface UsersTabProps {
  styles: Record<string, string>;
}

const ROLES = [
  { value: 'admin', label: 'Admin' },
  { value: 'attorney', label: 'Attorney' },
  { value: 'paralegal', label: 'Paralegal' },
  { value: 'viewer', label: 'Viewer' },
] as const;

export const UsersTab = ({ styles }: UsersTabProps) => {
  const id = useId();
  const addToast = useUIStore((st) => st.addToast);
  const showConfirm = useUIStore((st) => st.showConfirm);
  const currentUserId = useAuthStore((st) => st.user?.id);

  const [users, setUsers] = useState<AdminUser[]>([]);
  const [matrix, setMatrix] = useState<RolePermissionsMatrix | null>(null);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteName, setInviteName] = useState('');
  const [inviteRole, setInviteRole] = useState('attorney');
  const [inviting, setInviting] = useState(false);
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  // Row with an action in flight, and the row whose delete panel is open.
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const [deleteConfirmText, setDeleteConfirmText] = useState('');

  const load = useCallback(async () => {
    try {
      const res = await api.getUsers();
      setUsers(res.users);
    } catch (e) {
      addToast(e instanceof Error ? e.message : 'Failed to load users', 'error');
    }
    try {
      setMatrix(await api.getRolePermissions());
    } catch (e) {
      addToast(e instanceof Error ? e.message : 'Failed to load role permissions', 'error');
    }
  }, [addToast]);

  useEffect(() => { load(); }, [load]);

  const isLocked = (role: string, key: string) =>
    (matrix?.locked[role] || []).includes(key);

  const handleTogglePermission = async (role: string, key: string) => {
    if (!matrix || isLocked(role, key)) return;
    const current = matrix.assigned[role] || [];
    const next = current.includes(key)
      ? current.filter(k => k !== key)
      : [...current, key];
    const prev = matrix;
    // Optimistic flip; the PUT response is server truth (incl. customized flags).
    setMatrix({ ...matrix, assigned: { ...matrix.assigned, [role]: next } });
    try {
      setMatrix(await api.updateRolePermissions(role, next));
    } catch (e) {
      setMatrix(prev);
      addToast(e instanceof Error ? e.message : 'Failed to update permissions', 'error');
    }
  };

  const handleResetRoles = () => {
    showConfirm({
      title: 'Reset role permissions?',
      message: 'Every role returns to the shipped defaults. Custom grants are removed.',
      confirmText: 'Reset',
      onConfirm: async () => {
        try {
          setMatrix(await api.resetRolePermissions());
          addToast('Role permissions reset to defaults', 'success');
        } catch (e) {
          addToast(e instanceof Error ? e.message : 'Failed to reset', 'error');
        }
      },
    });
  };

  const permissionGroups = matrix
    ? Array.from(new Set(matrix.permissions.map(p => p.group)))
    : [];

  const handleInvite = async () => {
    if (!inviteEmail.trim() || !inviteName.trim()) {
      addToast('Email and name are required', 'error');
      return;
    }
    setInviting(true);
    setTempPassword(null);
    try {
      const res = await api.inviteUser(inviteEmail.trim(), inviteName.trim(), inviteRole);
      setTempPassword(res.temporary_password);
      setInviteEmail('');
      setInviteName('');
      setInviteRole('attorney');
      addToast(`Invited ${res.user.email}`, 'success');
      await load();
    } catch (e) {
      addToast(e instanceof Error ? e.message : 'Failed to invite user', 'error');
    }
    setInviting(false);
  };

  const handleRoleChange = async (user: AdminUser, role: string) => {
    const prev = users;
    setUsers(users.map(u => (u.id === user.id ? { ...u, roles: [role] } : u)));
    try {
      await api.updateUserRole(user.id, role);
      addToast(`${user.email} is now ${role}`, 'success');
    } catch (e) {
      setUsers(prev); // revert on failure
      addToast(e instanceof Error ? e.message : 'Failed to update role', 'error');
    }
  };

  /** Run one admin action on a user row, with a busy flag and an error toast. */
  const runUserAction = async (user: AdminUser, action: () => Promise<void>, failure: string) => {
    setBusyUserId(user.id);
    try {
      await action();
    } catch (e) {
      addToast(e instanceof Error ? e.message : failure, 'error');
    } finally {
      setBusyUserId(null);
    }
  };

  const applyActive = (user: AdminUser, isActive: boolean) =>
    runUserAction(user, async () => {
      const updated = await api.setUserActive(user.id, isActive);
      setUsers((prev) => prev.map(u => (u.id === user.id ? { ...u, ...updated } : u)));
      addToast(isActive ? `${user.email} reactivated` : `${user.email} deactivated and signed out`, 'success');
    }, 'Failed to update the account');

  const handleSetActive = (user: AdminUser, isActive: boolean) => {
    if (isActive) { applyActive(user, true); return; }
    showConfirm({
      title: `Deactivate ${user.email}?`,
      message: 'They are signed out everywhere and can no longer sign in. Their documents, conversations and analyses are kept, and the account can be reactivated later.',
      type: 'warning',
      confirmText: 'Deactivate',
      onConfirm: () => { applyActive(user, false); },
    });
  };

  const handleForceSignOut = (user: AdminUser) => {
    showConfirm({
      title: `Sign ${user.email} out everywhere?`,
      message: 'Every session and token for this account is ended on all devices. The account stays active — they can sign in again with their credentials.',
      type: 'warning',
      confirmText: 'Sign out everywhere',
      onConfirm: () => {
        runUserAction(user, async () => {
          await api.forceSignOutUser(user.id);
          addToast(`${user.email} was signed out on every device`, 'success');
        }, 'Failed to sign the user out');
      },
    });
  };

  const handleResetMfa = (user: AdminUser) => {
    showConfirm({
      title: `Reset two-factor authentication for ${user.email}?`,
      message: 'Their authenticator enrollment and recovery codes are removed and they are signed out everywhere. Use this only after confirming their identity — for example when they have lost their device and their recovery codes.',
      type: 'warning',
      confirmText: 'Reset two-factor',
      onConfirm: () => {
        runUserAction(user, async () => {
          await api.resetUserMfa(user.id);
          addToast(`Two-factor authentication reset for ${user.email}`, 'success');
        }, 'Failed to reset two-factor authentication');
      },
    });
  };

  const handleExport = (user: AdminUser) =>
    runUserAction(user, async () => {
      const data = await api.exportUserData(user.id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      downloadBlob(blob, `casecite-user-export-${user.email.replace(/[^a-z0-9.@_-]/gi, '_')}.json`);
      addToast(`Exported data for ${user.email}`, 'success');
    }, 'Failed to export user data');

  const openDelete = (user: AdminUser) => { setDeleteTarget(user); setDeleteConfirmText(''); };
  const closeDelete = () => { setDeleteTarget(null); setDeleteConfirmText(''); };

  const handleDelete = (user: AdminUser) =>
    runUserAction(user, async () => {
      await api.deleteUser(user.id);
      setUsers((prev) => prev.filter(u => u.id !== user.id));
      closeDelete();
      addToast(`${user.email} and their data were permanently deleted`, 'success');
    }, 'Failed to delete the user');

  const copyTempPassword = () => {
    if (tempPassword) {
      navigator.clipboard?.writeText(tempPassword);
      addToast('Temporary password copied', 'success');
    }
  };

  return (
    <div className={styles.usersTabContent}>
      {/* Invite */}
      <div className={styles.inviteCard}>
        <div className={styles.keyStatusTitle}>Invite a User</div>
        <div className={styles.inviteRow}>
          <div className={styles.inviteField}>
            <label className={styles.settingLabel} htmlFor={`${id}-invite-email`}>Email</label>
            <input
              id={`${id}-invite-email`}
              className={styles.modalInput}
              type="email"
              value={inviteEmail}
              onChange={e => setInviteEmail(e.target.value)}
              placeholder="name@firm.com"
            />
          </div>
          <div className={styles.inviteField}>
            <label className={styles.settingLabel} htmlFor={`${id}-invite-name`}>Name</label>
            <input
              id={`${id}-invite-name`}
              className={styles.modalInput}
              value={inviteName}
              onChange={e => setInviteName(e.target.value)}
              placeholder="Full name"
            />
          </div>
          <div className={styles.inviteField}>
            <label className={styles.settingLabel} htmlFor={`${id}-invite-role`}>Role</label>
            <select id={`${id}-invite-role`} className={styles.select} value={inviteRole} onChange={e => setInviteRole(e.target.value)}>
              {ROLES.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </div>
          <button className={styles.btnPrimary} onClick={handleInvite} disabled={inviting}>
            <Icon name="UserPlus" size={14} /> {inviting ? 'Inviting...' : 'Invite'}
          </button>
        </div>

        {tempPassword && (
          <div className={styles.tempPasswordBox}>
            <div className={styles.tempPasswordLabel}>
              Temporary password — share it with the user (shown once). They must choose their own
              password the first time they sign in.
            </div>
            <div className={styles.tempPasswordValue}>
              <span>{tempPassword}</span>
              <button className={styles.btnSecondaryXSmall} onClick={copyTempPassword}>
                <Icon name="Copy" size={12} /> Copy
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Existing users */}
      <div>
        <div className={styles.keyStatusTitle}>Users ({users.length})</div>
        <div className={styles.userList}>
          {users.map(u => {
            const isSelf = u.id === currentUserId;
            const busy = busyUserId === u.id;
            return (
            <div key={u.id} className={styles.userCard}>
              <div className={styles.userRow}>
                <div className={styles.userRowInfo}>
                  <div className={styles.userName}>
                    {u.name || '(no name)'}
                    {isSelf && <span className={styles.youBadge}>You</span>}
                    {!u.is_active && <span className={styles.inactiveBadge}>Deactivated</span>}
                    {u.must_change_password && <span className={styles.inactiveBadge}>Temporary password</span>}
                    {u.mfa_enabled === false && <span className={styles.inactiveBadge}>No two-factor</span>}
                  </div>
                  <div className={styles.userEmail}>{u.email}</div>
                </div>
                <select
                  aria-label={`Role for ${u.email}`}
                  className={styles.userRoleSelect}
                  value={u.roles[0] || 'attorney'}
                  onChange={e => handleRoleChange(u, e.target.value)}
                >
                  {ROLES.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
                </select>
              </div>
              <div className={styles.userActions}>
                <button className={styles.btnSecondaryXSmall} disabled={busy} onClick={() => handleExport(u)}>
                  <Icon name="Download" size={12} /> Export data
                </button>
                {/* Your own account is managed from the Security tab — these
                    actions would lock you out of the page you are on. */}
                {!isSelf && (
                  <>
                    <button className={styles.btnSecondaryXSmall} disabled={busy} onClick={() => handleForceSignOut(u)}>
                      <Icon name="LogOut" size={12} /> Sign out everywhere
                    </button>
                    <button className={styles.btnSecondaryXSmall} disabled={busy} onClick={() => handleResetMfa(u)}>
                      <Icon name="ShieldOff" size={12} /> Reset two-factor
                    </button>
                    <button className={styles.btnSecondaryXSmall} disabled={busy} onClick={() => handleSetActive(u, !u.is_active)}>
                      <Icon name={u.is_active ? 'Lock' : 'Check'} size={12} /> {u.is_active ? 'Deactivate' : 'Reactivate'}
                    </button>
                    <button className={styles.btnDangerXSmall} disabled={busy} onClick={() => openDelete(u)}>
                      <Icon name="Trash2" size={12} /> Delete…
                    </button>
                  </>
                )}
              </div>
              {deleteTarget?.id === u.id && (
                <div className={styles.dangerPanel} role="group" aria-label={`Delete ${u.email}`}>
                  <div className={styles.dangerTitle}>
                    <Icon name="AlertTriangle" size={14} /> Permanently delete this user and everything they own
                  </div>
                  <p className={styles.dangerText}>
                    This destroys {u.email}&apos;s uploaded documents and their search index, their
                    conversations, contract analyses, saved API keys and connector
                    connections, and every matter they own. Nothing is transferred to another user and
                    it cannot be undone. If the firm must keep this work product, <strong>deactivate</strong> the
                    account instead — that blocks sign-in and keeps the data. Export first if you need a record.
                  </p>
                  <label className={styles.settingLabel} htmlFor={`${id}-delete-confirm`}>
                    Type <strong>{u.email}</strong> to confirm
                  </label>
                  <input
                    id={`${id}-delete-confirm`}
                    className={styles.modalInput}
                    value={deleteConfirmText}
                    onChange={e => setDeleteConfirmText(e.target.value)}
                    autoComplete="off"
                  />
                  <div className={styles.boxActions}>
                    <button className={styles.btnSecondaryXSmall} onClick={closeDelete}>Cancel</button>
                    <button
                      className={styles.btnDangerXSmall}
                      disabled={busy || deleteConfirmText.trim().toLowerCase() !== u.email.toLowerCase()}
                      onClick={() => handleDelete(u)}
                    >
                      {busy ? 'Deleting…' : 'Delete user and all their data'}
                    </button>
                  </div>
                </div>
              )}
            </div>
            );
          })}
          {users.length === 0 && <span className={styles.helpText}>No users found.</span>}
        </div>
      </div>

      {/* Role permissions matrix */}
      {matrix && (
        <div className={styles.permSection}>
          <div className={styles.permHeader}>
            <div>
              <div className={styles.keyStatusTitle}>Role Permissions</div>
              <span className={styles.helpText}>
                What each role can do. Defaults are prechecked — check and uncheck to
                customize; changes apply to everyone with that role immediately.
              </span>
            </div>
            {matrix.customized.length > 0 && (
              <button className={styles.btnSecondaryXSmall} onClick={handleResetRoles}>
                <Icon name="RotateCcw" size={12} /> Reset to defaults
              </button>
            )}
          </div>

          <div className={styles.permTableWrap}>
            <table className={styles.permTable}>
              <thead>
                <tr>
                  <th className={styles.permNameCol}>Permission</th>
                  {matrix.roles.map(role => (
                    <th key={role} className={styles.permRoleCol}>
                      {role.charAt(0).toUpperCase() + role.slice(1)}
                      {matrix.customized.includes(role) && (
                        <span className={styles.permCustomBadge} title="Differs from defaults">edited</span>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {permissionGroups.map(group => (
                  [
                    <tr key={group} className={styles.permGroupRow}>
                      <td colSpan={matrix.roles.length + 1}>{group}</td>
                    </tr>,
                    ...matrix.permissions.filter(p => p.group === group).map(p => (
                      <tr key={p.key}>
                        <td className={styles.permNameCol}>
                          <span className={styles.permLabel}>{p.label}</span>
                          <span className={styles.permDesc}>{p.description}</span>
                        </td>
                        {matrix.roles.map(role => (
                          <td key={role} className={styles.permCheckCell}>
                            <input
                              aria-label={`${p.label} for ${role}`}
                              type="checkbox"
                              className={styles.permCheckbox}
                              checked={(matrix.assigned[role] || []).includes(p.key)}
                              disabled={isLocked(role, p.key)}
                              title={isLocked(role, p.key)
                                ? 'Locked — admins always keep user management, so no one can lock everyone out'
                                : undefined}
                              onChange={() => handleTogglePermission(role, p.key)}
                            />
                          </td>
                        ))}
                      </tr>
                    )),
                  ]
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
