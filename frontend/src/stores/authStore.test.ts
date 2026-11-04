import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api', () => ({
  api: {
    login: vi.fn(),
    getAzureConfig: vi.fn(),
    loginWithAzure: vi.fn(),
    verifyMfa: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    register: vi.fn(),
    forgotPassword: vi.fn(),
    changePassword: vi.fn(),
    logoutAllSessions: vi.fn(),
    getCurrentUser: vi.fn(),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

import { useAuthStore } from './authStore';
import { useUIStore } from './uiStore';
import { useDraftingStore } from './draftingStore';
import { useContractsStore } from './contractsStore';
import { useMfaStore } from './mfaStore';
import { api } from '../api';

const user = { id: 'u1', email: 'a@b.c', roles: ['user'] };

const msalLoginPopup = vi.fn();
vi.mock('@azure/msal-browser', () => ({
  PublicClientApplication: class {
    initialize = vi.fn().mockResolvedValue(undefined);
    loginPopup = msalLoginPopup;
  },
}));

describe('authStore Azure sign-in', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      azureAvailable: null, loginError: '', loginLoading: false, showLoginModal: true,
      isAuthenticated: false, user: null,
    });
  });

  it('offers Microsoft sign-in only when the server has Azure AD configured', async () => {
    (api.getAzureConfig as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('Azure AD not configured'));
    await useAuthStore.getState().checkAzureAvailable();
    expect(useAuthStore.getState().azureAvailable).toBe(false);

    useAuthStore.setState({ azureAvailable: null });
    (api.getAzureConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      authority: 'https://login.microsoftonline.com/t', client_id: 'cid', redirect_uri: null, scopes: ['openid'],
    });
    await useAuthStore.getState().checkAzureAvailable();
    expect(useAuthStore.getState().azureAvailable).toBe(true);
  });

  it('signs in with the ID token MSAL returns', async () => {
    (api.getAzureConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      authority: 'https://login.microsoftonline.com/t', client_id: 'cid', redirect_uri: null, scopes: ['openid', 'profile', 'email'],
    });
    msalLoginPopup.mockResolvedValue({ idToken: 'id-token-123' });
    (api.loginWithAzure as ReturnType<typeof vi.fn>).mockResolvedValue({
      access_token: 'a', user: { id: 'u-1', email: 'a@firm.com', roles: ['attorney'] },
    });

    await useAuthStore.getState().handleAzureLogin();

    expect(msalLoginPopup).toHaveBeenCalledWith({ scopes: ['openid', 'profile', 'email'], prompt: 'select_account' });
    expect(api.loginWithAzure).toHaveBeenCalledWith('id-token-123');
    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(true);
    expect(state.user?.email).toBe('a@firm.com');
    expect(state.showLoginModal).toBe(false);
    expect(state.loginError).toBe('');
  });

  it('treats a closed popup as a cancel and reports other failures', async () => {
    (api.getAzureConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      authority: 'x', client_id: 'cid', redirect_uri: null, scopes: ['openid'],
    });
    msalLoginPopup.mockRejectedValueOnce(new Error('user_cancelled: User cancelled the flow.'));
    await useAuthStore.getState().handleAzureLogin();
    expect(useAuthStore.getState().loginError).toBe('');
    expect(useAuthStore.getState().isAuthenticated).toBe(false);

    msalLoginPopup.mockResolvedValueOnce({ idToken: 'id' });
    (api.loginWithAzure as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('Your account has not been invited'));
    await useAuthStore.getState().handleAzureLogin();
    expect(useAuthStore.getState().loginError).toContain('Your account has not been invited');
    expect(useAuthStore.getState().loginLoading).toBe(false);
  });
});

describe('authStore', () => {
  beforeEach(() => {
    useAuthStore.setState(useAuthStore.getInitialState(), true);
    useUIStore.setState(useUIStore.getInitialState(), true);
    vi.clearAllMocks();
  });

  it('clears the typed credentials once login succeeds', async () => {
    (api.login as ReturnType<typeof vi.fn>).mockResolvedValue({ access_token: 't', user });
    useAuthStore.setState({ loginEmail: 'a@b.c', loginPassword: 'hunter2hunter2!A' });

    await useAuthStore.getState().handleLoginSubmit();

    const st = useAuthStore.getState();
    expect(st.isAuthenticated).toBe(true);
    expect(st.loginEmail).toBe('');
    expect(st.loginPassword).toBe('');
  });

  it('keeps the email but drops the password while an MFA challenge is pending', async () => {
    (api.login as ReturnType<typeof vi.fn>).mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-1', user });
    useAuthStore.setState({ loginEmail: 'a@b.c', loginPassword: 'hunter2hunter2!A' });

    await useAuthStore.getState().handleLoginSubmit();

    const st = useAuthStore.getState();
    expect(st.showMfaModal).toBe(true);
    expect(st.loginPassword).toBe('');
  });

  it('clears the credentials after MFA verification succeeds', async () => {
    (api.verifyMfa as ReturnType<typeof vi.fn>).mockResolvedValue({ access_token: 't', user });
    useAuthStore.setState({ loginEmail: 'a@b.c', loginPassword: 'x', mfaToken: 'mfa-1', mfaCode: '123456', showMfaModal: true });

    await useAuthStore.getState().handleMfaVerifySubmit();

    const st = useAuthStore.getState();
    expect(st.isAuthenticated).toBe(true);
    expect(st.loginEmail).toBe('');
    expect(st.loginPassword).toBe('');
    expect(st.mfaToken).toBeNull();
  });

  it('cancelMfaChallenge clears the challenge and the credentials', () => {
    useAuthStore.setState({ loginEmail: 'a@b.c', loginPassword: 'x', mfaToken: 'mfa-1', showMfaModal: true });

    useAuthStore.getState().cancelMfaChallenge();

    const st = useAuthStore.getState();
    expect(st.showMfaModal).toBe(false);
    expect(st.mfaToken).toBeNull();
    expect(st.loginEmail).toBe('');
    expect(st.loginPassword).toBe('');
  });

  it('surfaces the server message when login fails', async () => {
    (api.login as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Invalid email or password'));
    useAuthStore.setState({ loginEmail: 'a@b.c', loginPassword: 'wrong' });

    await useAuthStore.getState().handleLoginSubmit();

    expect(useAuthStore.getState().loginError).toBe('Invalid email or password');
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('logout resets the other stores so the next user starts clean', async () => {
    useAuthStore.setState({ user, isAuthenticated: true, loginEmail: 'a@b.c' });
    useDraftingStore.setState({
      messages: [{ id: 'm1', role: 'user', text: 'Draft me an NDA' }],
      draftWorkspace: { title: 'NDA', text: 'Confidential…', dirty: true },
    });
    useContractsStore.setState({
      selectedDocumentId: 'doc-1',
      messages: [{ id: 'c1', role: 'user', kind: 'text', text: 'Review this' }],
    });
    useMfaStore.setState({ freshRecoveryCodes: ['aaaa-bbbb'], setup: { secret: 's', provisioning_uri: 'otpauth://x' } });

    await useAuthStore.getState().handleLogout();

    expect(api.logout).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().loginEmail).toBe('');

    expect(useDraftingStore.getState().messages).toEqual([]);
    expect(useDraftingStore.getState().draftWorkspace).toBeNull();
    expect(useContractsStore.getState().selectedDocumentId).toBe('');
    expect(useContractsStore.getState().messages).toEqual([]);
    expect(useMfaStore.getState().freshRecoveryCodes).toBeNull();
    expect(useMfaStore.getState().setup).toBeNull();

    // The stores still work after a full-state replace.
    useDraftingStore.getState().setTargetPages(5);
    expect(useDraftingStore.getState().targetPages).toBe(5);
    expect(useUIStore.getState().toasts.some(t => t.message === 'Signed out')).toBe(true);
  });

  it('logout removes the locally cached settings under both the current and the old key', async () => {
    localStorage.setItem('casecite_rag_settings', JSON.stringify({ contract_playbook: 'secret' }));
    localStorage.setItem('wl_rag_settings', JSON.stringify({ contract_playbook: 'older secret' }));
    useAuthStore.setState({ user, isAuthenticated: true });

    await useAuthStore.getState().handleLogout();

    expect(localStorage.getItem('casecite_rag_settings')).toBeNull();
    expect(localStorage.getItem('wl_rag_settings')).toBeNull();
  });

  it('keeps the must-change-password flag from the login response on the user', async () => {
    (api.login as ReturnType<typeof vi.fn>).mockResolvedValue({
      access_token: 't', user: { ...user, must_change_password: true },
    });
    useAuthStore.setState({ loginEmail: 'a@b.c', loginPassword: 'Temp-Password-1!' });

    await useAuthStore.getState().handleLoginSubmit();

    expect(useAuthStore.getState().user?.must_change_password).toBe(true);
  });

  it('handleChangePassword rejects a reused password without calling the server', async () => {
    useAuthStore.setState({ user, isAuthenticated: true });
    const ok = await useAuthStore.getState().handleChangePassword('Same-Password-1!', 'Same-Password-1!', 'Same-Password-1!');
    expect(ok).toBe(false);
    expect(api.changePassword).not.toHaveBeenCalled();
    expect(useAuthStore.getState().changePasswordError).toMatch(/different/);
  });

  it('handleLogoutEverywhere revokes all sessions, then clears this device', async () => {
    (api.logoutAllSessions as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 'all_sessions_terminated' });
    useAuthStore.setState({ user, isAuthenticated: true });
    useDraftingStore.setState({ messages: [{ id: 'm1', role: 'user', text: 'Draft me an NDA' }] });

    await useAuthStore.getState().handleLogoutEverywhere();

    expect(api.logoutAllSessions).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useDraftingStore.getState().messages).toEqual([]);
  });

  it('handleLogoutEverywhere stays signed in when the server call fails', async () => {
    (api.logoutAllSessions as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Server unavailable'));
    useAuthStore.setState({ user, isAuthenticated: true });

    await useAuthStore.getState().handleLogoutEverywhere();

    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useUIStore.getState().toasts.some(t => t.message === 'Server unavailable')).toBe(true);
  });

  it('refreshUser picks up cleared flags (e.g. after enrolling in MFA)', async () => {
    (api.getCurrentUser as ReturnType<typeof vi.fn>).mockResolvedValue({ ...user, mfa_enrollment_required: false });
    useAuthStore.setState({ user: { ...user, mfa_enrollment_required: true }, isAuthenticated: true });

    await useAuthStore.getState().refreshUser();

    expect(useAuthStore.getState().user?.mfa_enrollment_required).toBe(false);
  });
});
