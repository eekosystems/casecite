import { getCookie } from '../utils';
import logger from '../utils/logger';
import type { ApiClient, RequestOptions } from './types';

// ==================== API CLIENT ====================
// VITE_API_BASE_URL points a dev build at a backend on another port (the
// journey tests run one beside the mock provider); otherwise the dev default.
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL
  || (import.meta.env.DEV
  ? 'http://localhost:8000/api/v1'
  : `${window.location.origin}/api/v1`);

// Endpoints where a 401 means "wrong credentials", not "expired session":
// no refresh attempt, and the server's own message is surfaced.
const CREDENTIAL_ENDPOINTS = new Set([
  '/auth/login',
  '/auth/register',
  '/auth/mfa/verify',
]);

const AUTH_FETCH_TIMEOUT_MS = 120000;

function isCredentialEndpoint(endpoint: string): boolean {
  const path = endpoint.split('?')[0];
  return CREDENTIAL_ENDPOINTS.has(path);
}

/**
 * Turn an error body's `detail` into something a person can read. FastAPI
 * sends a string for HTTPException and an array of `{loc, msg}` objects for
 * request-validation (422) failures — passing that array straight to
 * `new Error()` rendered as "[object Object]".
 */
export function formatErrorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object') {
        const { loc, msg } = item as { loc?: unknown; msg?: unknown };
        const field = Array.isArray(loc)
          ? loc.filter((p) => p !== 'body' && p !== 'query' && p !== 'path').join('.')
          : '';
        const text = typeof msg === 'string' ? msg : '';
        if (field && text) return `${field}: ${text}`;
        return text || field;
      }
      return '';
    }).filter(Boolean);
    if (parts.length) return parts.join('; ');
  }
  if (detail && typeof detail === 'object') {
    const { message, msg } = detail as { message?: unknown; msg?: unknown };
    if (typeof message === 'string' && message) return message;
    if (typeof msg === 'string' && msg) return msg;
  }
  return fallback;
}

/**
 * Abort when either the caller's signal fires or the timeout elapses, and
 * report which of the two it was.
 */
function withTimeout(timeoutMs: number, external?: AbortSignal | null) {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  const onExternalAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener('abort', onExternalAbort, { once: true });
  }
  return {
    signal: controller.signal,
    didTimeOut: () => timedOut,
    cleanup: () => {
      clearTimeout(timer);
      external?.removeEventListener('abort', onExternalAbort);
    },
  };
}

/**
 * The session is dead and unrefreshable (e.g. signing keys rotated by a
 * deploy, or the refresh token was revoked). Never leave the UI in a zombie
 * logged-in state — clear auth and surface the login modal. Dynamic imports
 * avoid a module cycle.
 */
function handleSessionExpired() {
  // A stale tab after a deploy can 404 on a chunk URL; the rejection must be
  // logged rather than left unhandled, or the zombie state this exists to
  // prevent is exactly what the user sees.
  import('../stores/authStore').then(({ useAuthStore }) => {
    const auth = useAuthStore.getState();
    if (auth.isAuthenticated) {
      auth.setIsAuthenticated(false);
      auth.setShowLoginModal(true);
      // Whoever signs in next must not inherit this user's work.
      import('../stores/resetRegistry')
        .then(({ resetAllStores }) => resetAllStores())
        .catch((e) => logger.error('[client] resetAllStores after session expiry failed:', e));
      import('../stores/uiStore')
        .then(({ useUIStore }) => {
          useUIStore.getState().addToast('Your session expired — please sign in again.', 'warning');
        })
        .catch((e) => logger.error('[client] session-expired toast failed:', e));
    }
  }).catch((e) => logger.error('[client] session-expired handler failed to load authStore:', e));
}

/**
 * The server refuses most calls (403 + `code`) while an account still has to
 * replace a temporary password or enrol in required two-factor auth. Reflect
 * that on the signed-in user so the matching screen opens, wherever the
 * refused call came from.
 */
function handleAccountGate(code: unknown) {
  if (code !== 'password_change_required' && code !== 'mfa_enrollment_required') return;
  import('../stores/authStore').then(({ useAuthStore }) => {
    const { user, isAuthenticated, setUser } = useAuthStore.getState();
    if (!isAuthenticated || !user) return;
    if (code === 'password_change_required') {
      // ChangePasswordModal opens (undismissably) off this flag.
      if (!user.must_change_password) setUser({ ...user, must_change_password: true });
      return;
    }
    if (!user.mfa_enrollment_required) setUser({ ...user, mfa_enrollment_required: true });
    import('../stores/uiStore')
      .then(({ useUIStore }) => {
        const ui = useUIStore.getState();
        if (!ui.showSettings || ui.settingsTab !== 'security') ui.openSettings('security');
      })
      .catch((e) => logger.error('[client] account-gate settings open failed:', e));
  });
}

export const api = {
  token: null as string | null,

  setToken(token: string) {
    this.token = token;
    // Tokens are stored in httpOnly cookies by the backend (the refresh token
    // never reaches JS). Keep in-memory only for the Authorization header.
  },

  getToken(): string | null {
    return this.token;
  },

  async initCsrf() {
    // Fetch CSRF token on app load so the cookie is set before any POST requests
    try { await this.request('/csrf-token'); } catch { /* non-critical */ }
  },

  async request(endpoint: string, options: RequestOptions = {}) {
    const url = `${API_BASE_URL}${endpoint}`;

    const isFormData = options.body instanceof FormData;
    const method = (options.method || 'GET').toUpperCase();
    const csrfToken = getCookie('_csrf');
    const headers: Record<string, string> = {
      // Don't set Content-Type for FormData - browser sets it with boundary
      ...(!isFormData && { 'Content-Type': 'application/json' }),
      ...(this.getToken() && { 'Authorization': `Bearer ${this.getToken()}` }),
      // CSRF: Include token on state-changing requests
      ...(csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method) && { 'X-CSRF-Token': csrfToken }),
      ...options.headers,
    };

    // Request timeout (30s default); a caller-supplied signal cancels too.
    const { signal: callerSignal, timeout: timeoutOpt, _retried, ...fetchOptions } = options;
    const guard = withTimeout(timeoutOpt || 30000, callerSignal);

    let response: Response;
    try {
      response = await fetch(url, { ...fetchOptions, headers, credentials: 'include', signal: guard.signal });
    } catch (error) {
      if (guard.didTimeOut()) throw new Error('The request timed out. Please try again.');
      throw error;
    } finally {
      guard.cleanup();
    }

    if (response.status === 401) {
      // A 401 from a credential endpoint means the credentials were wrong —
      // there is no session to refresh, and the server's message ("Invalid
      // email or password", "Invalid code") is the one the user needs.
      if (isCredentialEndpoint(endpoint)) {
        const error = await response.json().catch(() => ({}));
        throw new Error(formatErrorDetail(error.detail, 'Invalid credentials'));
      }
      // Attempt one token refresh before giving up
      if (!_retried) {
        const refreshed = await this.refreshAccessToken();
        if (refreshed) {
          return this.request(endpoint, { ...options, _retried: true });
        }
      }
      this.token = null;
      handleSessionExpired();
      throw new Error('Unauthorized - please log in');
    }

    if (response.status === 403) {
      const error = await response.json().catch(() => ({ detail: 'Forbidden' }));
      if (error.detail && typeof error.detail === 'string' && error.detail.includes('CSRF')) {
        throw new Error('Session expired. Please refresh the page.');
      }
      handleAccountGate(error.code);
      // Status attached like every other failure, so a caller can tell
      // "not allowed" apart without parsing the message.
      throw Object.assign(new Error(formatErrorDetail(error.detail, 'Forbidden')), { status: 403 });
    }

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Request failed' }));
      // Carry the HTTP status so callers can tell a 429 (back off and retry)
      // from a real failure without parsing the message.
      throw Object.assign(new Error(formatErrorDetail(error.detail, 'Request failed')), { status: response.status });
    }

    return response.json();
  },

  async refreshAccessToken(): Promise<boolean> {
    try {
      const csrfToken = getCookie('_csrf');
      const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(csrfToken && { 'X-CSRF-Token': csrfToken }),
        },
        credentials: 'include',
        body: JSON.stringify({}),
      });
      if (response.ok) {
        const data = await response.json();
        if (data.access_token) {
          this.token = data.access_token;
          return true;
        }
      }
    } catch {
      // Refresh failed silently
    }
    return false;
  },

  /**
   * Low-level fetch wrapper for callers that need the raw Response (blobs,
   * custom status handling). Takes a full URL, adds Authorization and CSRF
   * headers, and — like `request` — applies a timeout, retries once after a
   * token refresh on 401, and surfaces the login modal when the session is
   * dead. The (possibly 401) Response is still returned so callers keep
   * their own `response.ok` handling.
   */
  async authFetch(url: string, options: RequestOptions = {}): Promise<Response> {
    const { signal: callerSignal, timeout: timeoutOpt, _retried, ...fetchOptions } = options;
    const token = this.getToken();
    const csrfToken = getCookie('_csrf');
    const method = (options.method || 'GET').toUpperCase();
    const isFormData = options.body instanceof FormData;

    // The current token is applied last so a retry after a refresh never
    // re-sends a stale Authorization header captured by the caller.
    const headers: Record<string, string> = {
      ...(!isFormData && { 'Content-Type': 'application/json' }),
      ...options.headers,
      ...(token && { 'Authorization': `Bearer ${token}` }),
      ...(csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method) && { 'X-CSRF-Token': csrfToken }),
    };

    // These calls had no timeout at all; the default is generous because they
    // cover exports, file conversion and CourtListener-backed lookups.
    const guard = withTimeout(timeoutOpt || AUTH_FETCH_TIMEOUT_MS, callerSignal);
    let response: Response;
    try {
      response = await fetch(url, { ...fetchOptions, headers, credentials: 'include', signal: guard.signal });
    } catch (error) {
      if (guard.didTimeOut()) throw new Error('The request timed out. Please try again.');
      throw error;
    } finally {
      guard.cleanup();
    }

    if (response.status === 401) {
      if (!_retried) {
        const refreshed = await this.refreshAccessToken();
        if (refreshed) {
          return this.authFetch(url, { ...options, _retried: true });
        }
      }
      this.token = null;
      handleSessionExpired();
    } else if (response.status === 403) {
      // Peek at a copy so the caller can still read the body.
      response.clone().json().then((body) => handleAccountGate(body?.code)).catch(() => { /* not JSON */ });
    }
    return response;
  },
} as ApiClient;
