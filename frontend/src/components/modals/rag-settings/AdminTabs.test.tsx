import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

vi.mock('../../../api', () => ({
  api: {
    getUsers: vi.fn(),
    getRolePermissions: vi.fn(),
    setUserActive: vi.fn(),
    deleteUser: vi.fn(),
    exportUserData: vi.fn(),
    forceSignOutUser: vi.fn(),
    resetUserMfa: vi.fn(),
    getAuditLogs: vi.fn(),
    verifyAuditChain: vi.fn(),
    exportAuditLogs: vi.fn(),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../../../utils/downloadBlob', () => ({ downloadBlob: vi.fn() }));
vi.mock('../../../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

import { api } from '../../../api';
import { downloadBlob } from '../../../utils/downloadBlob';
import { useAuthStore } from '../../../stores/authStore';
import { useUIStore } from '../../../stores/uiStore';
import { UsersTab } from './UsersTab';
import { AuditTab } from './AuditTab';

// Identity proxy: every class name resolves to itself.
const s = new Proxy({}, { get: (_t, key) => String(key) }) as Record<string, string>;
const mock = (fn: unknown) => fn as ReturnType<typeof vi.fn>;

const admin = { id: 'admin-1', email: 'admin@firm.com', name: 'Ada Admin', roles: ['admin'], is_active: true };
const departed = { id: 'u-2', email: 'departed@firm.com', name: 'Dee Parted', roles: ['attorney'], is_active: true };

beforeEach(() => {
  vi.clearAllMocks();
  useUIStore.setState(useUIStore.getInitialState(), true);
  useAuthStore.setState({ user: { id: 'admin-1', email: 'admin@firm.com', roles: ['admin'] }, isAuthenticated: true });
  mock(api.getUsers).mockResolvedValue({ users: [admin, departed] });
  mock(api.getRolePermissions).mockRejectedValue(new Error('not needed here'));
});

const matrixBase = {
  permissions: [{ key: 'chat.use', label: 'Chat', description: '', group: 'Workspace' }],
  roles: ['admin', 'attorney', 'paralegal', 'viewer'],
  defaults: { admin: ['chat.use'], attorney: ['chat.use'], paralegal: ['chat.use'], viewer: ['chat.use'] },
  assigned: { admin: ['chat.use'], attorney: ['chat.use'], paralegal: ['chat.use'], viewer: ['chat.use'] },
  customized: [],
  locked: { admin: ['admin.users'] },
};

describe('UsersTab permission policy status', () => {
  it('shows no alert while the stored matrix is being served', async () => {
    mock(api.getRolePermissions).mockResolvedValue({
      ...matrixBase,
      policy: { healthy: true, source: 'stored', error: null, snapshot_present: true },
    });
    render(<UsersTab styles={s} />);
    await screen.findByText('Role Permissions');
    expect(screen.queryByTestId('permission-policy-alert')).toBeNull();
  });

  it('warns when the last-known-good snapshot is in force', async () => {
    mock(api.getRolePermissions).mockResolvedValue({
      ...matrixBase,
      policy: { healthy: false, source: 'snapshot', error: 'SecretUnreadableError: x', snapshot_present: true },
    });
    render(<UsersTab styles={s} />);
    const alert = await screen.findByTestId('permission-policy-alert');
    expect(alert).toHaveTextContent('the last readable version is in force');
    expect(alert).toHaveTextContent('SecretUnreadableError: x');
  });

  it('warns when every role is reduced to its locked permissions', async () => {
    mock(api.getRolePermissions).mockResolvedValue({
      ...matrixBase,
      policy: { healthy: false, source: 'locked_only', error: 'OperationalError: db', snapshot_present: false },
    });
    render(<UsersTab styles={s} />);
    const alert = await screen.findByTestId('permission-policy-alert');
    expect(alert).toHaveTextContent('limited to its locked permissions');
  });
});

describe('UsersTab offboarding', () => {
  async function renderTab() {
    render(<UsersTab styles={s} />);
    const email = await screen.findByText('departed@firm.com');
    return email.closest('.userCard') as HTMLElement;
  }

  it('offers no destructive actions on your own account', async () => {
    await renderTab();
    const own = (screen.getByText('admin@firm.com').closest('.userCard')) as HTMLElement;
    expect(within(own).getByText('Export data')).toBeInTheDocument();
    expect(within(own).queryByText('Deactivate')).toBeNull();
    expect(within(own).queryByText('Delete…')).toBeNull();
  });

  it('deactivates a user after confirmation and shows the new state', async () => {
    mock(api.setUserActive).mockResolvedValue({ ...departed, is_active: false });
    const card = await renderTab();

    fireEvent.click(within(card).getByText('Deactivate'));
    expect(api.setUserActive).not.toHaveBeenCalled();
    useUIStore.getState().handleConfirm();

    await waitFor(() => expect(api.setUserActive).toHaveBeenCalledWith('u-2', false));
    expect(await within(card).findByText('Deactivated')).toBeInTheDocument();
    expect(within(card).getByText('Reactivate')).toBeInTheDocument();
  });

  it('only deletes once the email has been typed, and says what is destroyed', async () => {
    mock(api.deleteUser).mockResolvedValue({ status: 'deleted' });
    const card = await renderTab();

    fireEvent.click(within(card).getByText('Delete…'));
    expect(within(card).getByText(/destroys departed@firm.com's uploaded documents/)).toBeInTheDocument();
    const confirm = within(card).getByRole('button', { name: 'Delete user and all their data' });
    expect(confirm).toBeDisabled();

    fireEvent.change(within(card).getByRole('textbox'), { target: { value: 'someone-else@firm.com' } });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(card).getByRole('textbox'), { target: { value: 'departed@firm.com' } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(api.deleteUser).toHaveBeenCalledWith('u-2'));
    await waitFor(() => expect(screen.queryByText('departed@firm.com')).toBeNull());
  });

  it('exports a user\'s data as a JSON download', async () => {
    mock(api.exportUserData).mockResolvedValue({ profile: { id: 'u-2' }, documents: [] });
    const card = await renderTab();

    fireEvent.click(within(card).getByText('Export data'));

    await waitFor(() => expect(downloadBlob).toHaveBeenCalled());
    expect(mock(downloadBlob).mock.calls[0][1]).toBe('casecite-user-export-departed@firm.com.json');
  });

  it('force sign-out and MFA reset each need a confirmation', async () => {
    mock(api.forceSignOutUser).mockResolvedValue({ status: 'signed_out' });
    mock(api.resetUserMfa).mockResolvedValue({ status: 'mfa_reset' });
    const card = await renderTab();

    fireEvent.click(within(card).getByText('Sign out everywhere'));
    expect(api.forceSignOutUser).not.toHaveBeenCalled();
    useUIStore.getState().handleConfirm();
    await waitFor(() => expect(api.forceSignOutUser).toHaveBeenCalledWith('u-2'));

    fireEvent.click(within(card).getByText('Reset two-factor'));
    expect(api.resetUserMfa).not.toHaveBeenCalled();
    useUIStore.getState().handleConfirm();
    await waitFor(() => expect(api.resetUserMfa).toHaveBeenCalledWith('u-2'));
  });

  it('surfaces a server refusal', async () => {
    mock(api.setUserActive).mockRejectedValue(new Error('Cannot deactivate the last admin.'));
    const card = await renderTab();
    fireEvent.click(within(card).getByText('Deactivate'));
    useUIStore.getState().handleConfirm();
    await waitFor(() =>
      expect(useUIStore.getState().toasts.some(t => t.message === 'Cannot deactivate the last admin.')).toBe(true));
  });
});

describe('AuditTab', () => {
  const entry = {
    id: 'e-1',
    timestamp: '2026-10-01T12:00:00+00:00Z',
    event_type: 'auth.login.failure',
    user: { id: 'u-2', email: 'd***@firm.com' },
    resource: { type: null, id: null },
    action: { reason: 'bad_password' },
    context: { ip_address: '10.0.0.x' },
    outcome: { success: false, error: null },
  };

  beforeEach(() => {
    mock(api.getAuditLogs).mockResolvedValue({ logs: [entry], count: 1 });
  });

  it('loads the newest entries on open and renders them', async () => {
    render(<AuditTab styles={s} />);
    expect(await screen.findByText('auth.login.failure (failed)')).toBeInTheDocument();
    expect(screen.getByText('d***@firm.com')).toBeInTheDocument();
    expect(screen.getByText('reason: bad_password')).toBeInTheDocument();
    expect(mock(api.getAuditLogs).mock.calls[0][0]).toMatchObject({ limit: 200 });
  });

  it('sends the filters as UTC day bounds', async () => {
    render(<AuditTab styles={s} />);
    await screen.findByText('auth.login.failure (failed)');

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-01' } });
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-09-30' } });
    fireEvent.change(screen.getByLabelText('Event type'), { target: { value: 'document.download' } });
    fireEvent.change(screen.getByLabelText('User'), { target: { value: 'u-2' } });
    fireEvent.click(screen.getByRole('button', { name: /Search/ }));

    await waitFor(() => expect(api.getAuditLogs).toHaveBeenLastCalledWith({
      startDate: '2026-09-01T00:00:00Z',
      endDate: '2026-09-30T23:59:59Z',
      eventType: 'document.download',
      userId: 'u-2',
      limit: 200,
    }));
  });

  it('reports a clean verification, including legacy and truncated notes', async () => {
    mock(api.verifyAuditChain).mockResolvedValue({ valid: true, entries_checked: 1234, legacy_entries: 10, truncated: true });
    render(<AuditTab styles={s} />);
    await screen.findByText('auth.login.failure (failed)');

    fireEvent.click(screen.getByRole('button', { name: /Verify integrity/ }));

    const result = await screen.findByRole('status');
    expect(result).toHaveTextContent('Integrity verified.');
    expect(result).toHaveTextContent('1,234 entries checked');
    expect(result).toHaveTextContent('10 older entries predate chain linking');
    expect(result).toHaveTextContent('only the newest were checked');
  });

  it('reports a failed verification as an alert with the reason and entry', async () => {
    mock(api.verifyAuditChain).mockResolvedValue({
      valid: false, entries_checked: 50, first_invalid_id: 'e-17', reason: 'Entry signature does not match.',
    });
    render(<AuditTab styles={s} />);
    await screen.findByText('auth.login.failure (failed)');

    fireEvent.click(screen.getByRole('button', { name: /Verify integrity/ }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Integrity check failed.');
    expect(alert).toHaveTextContent('Entry signature does not match.');
    expect(alert).toHaveTextContent('e-17');
  });

  it('exports the filtered range as a JSONL download', async () => {
    const blob = new Blob(['{}\n']);
    mock(api.exportAuditLogs).mockResolvedValue(blob);
    render(<AuditTab styles={s} />);
    await screen.findByText('auth.login.failure (failed)');

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-01' } });
    fireEvent.click(screen.getByRole('button', { name: /Export/ }));

    await waitFor(() => expect(downloadBlob).toHaveBeenCalledWith(blob, 'casecite-audit-2026-09-01_to_now.jsonl'));
    expect(mock(api.exportAuditLogs).mock.calls[0][0]).toMatchObject({ startDate: '2026-09-01T00:00:00Z' });
  });
});
