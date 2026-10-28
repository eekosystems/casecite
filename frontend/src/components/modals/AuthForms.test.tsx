import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('../../api', () => ({
  api: {
    token: null,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn().mockResolvedValue(undefined),
    changePassword: vi.fn(),
  },
  API_BASE_URL: 'http://localhost:8000/api/v1',
}));

vi.mock('../../utils/logger', () => ({ default: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } }));

import { api } from '../../api';
import { useAuthStore } from '../../stores/authStore';
import { useBrandingStore } from '../../stores/brandingStore';
import { useUIStore } from '../../stores/uiStore';
import { LoginModal } from './LoginModal';
import { SignupModal } from './SignupModal';
import { ChangePasswordModal } from './ChangePasswordModal';

const user = { id: 'u1', email: 'a@firm.com', roles: ['attorney'] };
const mockCall = (fn: unknown, n: number) => (fn as ReturnType<typeof vi.fn>).mock.calls[n];

beforeEach(() => {
  useAuthStore.setState(useAuthStore.getInitialState(), true);
  useBrandingStore.setState(useBrandingStore.getInitialState(), true);
  useUIStore.setState(useUIStore.getInitialState(), true);
  vi.clearAllMocks();
});

describe('LoginModal on a public demo instance', () => {
  const demo_login = { email: 'demo@firm.com', password: 'Password1!' };
  const withDemo = () => {
    const { branding } = useBrandingStore.getState();
    useBrandingStore.setState({ branding: { ...branding, demo_login } });
  };

  it('prefills the shared demo account and says so', () => {
    withDemo();
    useAuthStore.setState({ showLoginModal: true });
    render(<LoginModal />);

    expect(screen.getByLabelText('Email')).toHaveValue('demo@firm.com');
    expect(screen.getByLabelText('Password')).toHaveValue('Password1!');
    expect(screen.getByRole('note')).toHaveTextContent(/public demo/i);
  });

  it('leaves an email the user already has alone', () => {
    withDemo();
    useAuthStore.setState({ showLoginModal: true, loginEmail: 'a@firm.com' });
    render(<LoginModal />);

    expect(screen.getByLabelText('Email')).toHaveValue('a@firm.com');
    expect(screen.getByLabelText('Password')).toHaveValue('');
  });

  it('does nothing on an ordinary instance', () => {
    useAuthStore.setState({ showLoginModal: true });
    render(<LoginModal />);

    expect(screen.getByLabelText('Email')).toHaveValue('');
    expect(screen.queryByRole('note')).toBeNull();
  });
});

describe('LoginModal', () => {
  it('has a real submit button, so the form can be submitted from the keyboard', async () => {
    (api.login as ReturnType<typeof vi.fn>).mockResolvedValue({ access_token: 't', user });
    useAuthStore.setState({ showLoginModal: true, loginEmail: 'a@firm.com', loginPassword: 'pw' });
    render(<LoginModal />);

    const submit = screen.getByRole('button', { name: 'Sign In' });
    expect(submit).toHaveAttribute('type', 'submit');
    const form = document.querySelector('form') as HTMLFormElement;
    expect(submit).toHaveAttribute('form', form.id);

    // What the browser does when Enter is pressed in a field.
    fireEvent.submit(form);

    await waitFor(() => expect(api.login).toHaveBeenCalledWith('a@firm.com', 'pw'));
  });
});

describe('SignupModal', () => {
  it('submits through the form and reports the password policy', () => {
    useAuthStore.setState({
      showSignupModal: true, signupName: 'A', signupEmail: 'a@firm.com',
      signupPassword: 'short', signupConfirmPassword: 'short',
    });
    render(<SignupModal />);

    expect(screen.getByRole('button', { name: 'Register' })).toHaveAttribute('type', 'submit');
    fireEvent.submit(document.querySelector('form') as HTMLFormElement);

    expect(useAuthStore.getState().signupError).toMatch(/12 characters/);
    expect(api.register).not.toHaveBeenCalled();
  });
});

describe('SignupModal initial setup', () => {
  const filled = {
    showSignupModal: true, signupName: 'Ada Admin', signupEmail: 'ada@firm.com',
    signupPassword: 'First-Admin-Pass-1!', signupConfirmPassword: 'First-Admin-Pass-1!',
  };

  it('reveals the setup-token field when the server asks for it, then sends the token', async () => {
    (api.register as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new Error('A valid bootstrap token is required.'))
      .mockResolvedValueOnce({ access_token: 't', user });
    useAuthStore.setState(filled);
    render(<SignupModal />);
    const disclosure = document.querySelector('details') as HTMLDetailsElement;
    expect(disclosure.open).toBe(false);

    fireEvent.submit(document.querySelector('form') as HTMLFormElement);
    await waitFor(() => expect(useAuthStore.getState().signupNeedsBootstrapToken).toBe(true));
    await waitFor(() => expect(disclosure.open).toBe(true));
    expect(mockCall(api.register, 0)[4]).toBeUndefined();

    fireEvent.change(screen.getByLabelText('Setup token'), { target: { value: ' operator-token ' } });
    fireEvent.submit(document.querySelector('form') as HTMLFormElement);

    await waitFor(() => expect(useAuthStore.getState().isAuthenticated).toBe(true));
    expect(mockCall(api.register, 1)[4]).toBe('operator-token');
    expect(useAuthStore.getState().signupBootstrapToken).toBe('');
  });
});

describe('ChangePasswordModal', () => {
  function fill(current: string, next: string, confirm: string) {
    fireEvent.change(screen.getByLabelText(/current password|temporary password/i), { target: { value: current } });
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: next } });
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: confirm } });
    fireEvent.submit(document.querySelector('form') as HTMLFormElement);
  }

  it('is closed by default', () => {
    useAuthStore.setState({ user, isAuthenticated: true });
    const { container } = render(<ChangePasswordModal />);
    expect(container.innerHTML).toBe('');
  });

  it('opens by itself, with no way to dismiss it, while the account is on a temporary password', () => {
    useAuthStore.setState({ user: { ...user, must_change_password: true }, isAuthenticated: true });
    render(<ChangePasswordModal />);

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Choose a New Password')).toBeInTheDocument();
    expect(screen.queryByLabelText('Close')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('validates locally before calling the server', () => {
    useAuthStore.setState({ user, isAuthenticated: true, showChangePassword: true });
    render(<ChangePasswordModal />);

    fill('Old-Password-1!', 'New-Password-2!', 'Different-3!xx');

    expect(screen.getByRole('alert')).toHaveTextContent('Passwords do not match');
    expect(api.changePassword).not.toHaveBeenCalled();
  });

  it('changes the password, then signs out and asks for a fresh sign-in', async () => {
    (api.changePassword as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 'password_changed' });
    useAuthStore.setState({ user: { ...user, must_change_password: true }, isAuthenticated: true });
    render(<ChangePasswordModal />);

    fill('Temp-Password-1!', 'New-Password-2!', 'New-Password-2!');

    await waitFor(() => expect(api.changePassword).toHaveBeenCalledWith('Temp-Password-1!', 'New-Password-2!'));
    await waitFor(() => expect(useAuthStore.getState().isAuthenticated).toBe(false));
    const st = useAuthStore.getState();
    expect(st.user).toBeNull();
    expect(st.showLoginModal).toBe(true);
    expect(st.loginEmail).toBe('a@firm.com');
    expect(st.loginPassword).toBe('');
  });

  it('shows the server reason when the change is refused', async () => {
    (api.changePassword as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Current password is incorrect.'));
    useAuthStore.setState({ user, isAuthenticated: true, showChangePassword: true });
    render(<ChangePasswordModal />);

    fill('Wrong-Password-1!', 'New-Password-2!', 'New-Password-2!');

    expect(await screen.findByRole('alert')).toHaveTextContent('Current password is incorrect.');
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });
});
