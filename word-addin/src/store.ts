import { create } from 'zustand';
import { ApiClient, SessionExpiredError, type Tokens, webOrigin } from './api/client';
import type {
  ChatResponse,
  ContractAnalysisResult,
  ContractIssue,
  ContractKeyTerm,
  DeviceStart,
  Matter,
  StoredSession,
} from './api/types';
import { DeviceFlowError, runDeviceFlow } from './auth/deviceFlow';
import { AI_REVIEW_NOTICE } from './notice';
import {
  insertAnswerWithFootnotes,
  readDocumentText,
  replaceQuoteTracked,
  selectQuote,
} from './office/document';
import { MATTER_KEY, SERVER_URL_KEY, SESSION_KEY, createStore, readJson } from './storage';

/**
 * All pane state and behaviour. Screens render it; nothing here touches React.
 */

export type Screen = 'analyze' | 'research' | 'settings';

export interface SignInState {
  status: 'idle' | 'starting' | 'waiting' | 'error';
  code: DeviceStart | null;
  error: string;
}

export interface AnalysisState {
  status: 'idle' | 'reading' | 'running' | 'done' | 'error';
  result: ContractAnalysisResult | null;
  error: string;
  representing: string;
  /** Issue ref whose select/apply is in flight. */
  busyRef: string | null;
  /** Last document action outcome, shown under the list. */
  note: string;
}

export interface ResearchState {
  question: string;
  status: 'idle' | 'running' | 'done' | 'error';
  result: ChatResponse | null;
  error: string;
  inserting: boolean;
  note: string;
}

export interface AddinState {
  booted: boolean;
  serverUrl: string;
  session: StoredSession | null;
  client: ApiClient | null;
  screen: Screen;
  matters: Matter[];
  mattersError: string;
  matterId: string | null;
  signIn: SignInState;
  analysis: AnalysisState;
  research: ResearchState;

  init: () => Promise<void>;
  setScreen: (screen: Screen) => void;
  setServerUrl: (url: string) => Promise<void>;
  startSignIn: () => Promise<void>;
  cancelSignIn: () => void;
  signOut: () => Promise<void>;
  loadMatters: () => Promise<void>;
  setMatter: (matterId: string | null) => Promise<void>;

  setRepresenting: (value: string) => void;
  analyze: () => Promise<void>;
  selectIssue: (issue: ContractIssue) => Promise<void>;
  applyRewrite: (issue: ContractIssue) => Promise<void>;
  selectKeyTerm: (term: ContractKeyTerm) => Promise<void>;

  setQuestion: (value: string) => void;
  ask: () => Promise<void>;
  insertAnswer: () => Promise<void>;
}

const store = createStore();
let signInAbort: AbortController | null = null;

const initialSignIn: SignInState = { status: 'idle', code: null, error: '' };
const initialAnalysis: AnalysisState = {
  status: 'idle',
  result: null,
  error: '',
  representing: '',
  busyRef: null,
  note: '',
};
const initialResearch: ResearchState = {
  question: '',
  status: 'idle',
  result: null,
  error: '',
  inserting: false,
  note: '',
};

function message(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

/** Open the approval page in the system browser (Office) or a new tab (dev). */
export function openApprovalPage(url: string): void {
  try {
    const office = (globalThis as { Office?: typeof Office }).Office;
    if (office?.context?.ui?.openBrowserWindow) {
      office.context.ui.openBrowserWindow(url);
      return;
    }
  } catch {}
  try {
    window.open(url, '_blank', 'noopener');
  } catch {}
}

/** The approval URL for a code: the server's answer, made absolute if it was a bare path. */
/**
 * The CaseCite server to use when none is saved: when the pane is served by
 * the CaseCite image itself (at /addin/ on the app's origin), that origin is
 * the server, so a first sign-in needs no address typed in. Empty elsewhere.
 */
export function bundledServerUrl(loc: { origin: string; pathname: string } | null = typeof window === 'undefined' ? null : window.location): string {
  if (!loc) return '';
  return /^\/addin(\/|$)/.test(loc.pathname) ? loc.origin : '';
}

export function approvalUrl(serverUrl: string, code: DeviceStart): string {
  const uri = code.verification_uri_complete || code.verification_uri;
  if (/^https?:\/\//i.test(uri)) return uri;
  return `${webOrigin(serverUrl)}${uri.startsWith('/') ? '' : '/'}${uri}`;
}

export const useAddinStore = create<AddinState>((set, get) => {
  function makeClient(serverUrl: string, tokens: Tokens | null): ApiClient {
    return new ApiClient({
      serverUrl,
      tokens,
      onTokens: (next) => {
        const { session } = get();
        if (!next) {
          if (session) {
            set({ session: null });
            void store.remove(SESSION_KEY);
          }
          return;
        }
        if (session) {
          const updated = { ...session, accessToken: next.accessToken, refreshToken: next.refreshToken };
          set({ session: updated });
          void store.set(SESSION_KEY, JSON.stringify(updated));
        }
      },
    });
  }

  /** A failed call that means "signed out" clears the session; everything else is reported. */
  function handleFailure(e: unknown, fallback: string): string {
    if (e instanceof SessionExpiredError) {
      set({ session: null, client: makeClient(get().serverUrl, null) });
      void store.remove(SESSION_KEY);
    }
    return message(e, fallback);
  }

  return {
    booted: false,
    serverUrl: '',
    session: null,
    client: null,
    screen: 'analyze',
    matters: [],
    mattersError: '',
    matterId: null,
    signIn: initialSignIn,
    analysis: initialAnalysis,
    research: initialResearch,

    init: async () => {
      if (get().booted) return;
      const session = await readJson<StoredSession>(store, SESSION_KEY);
      const savedUrl = (await store.get(SERVER_URL_KEY)) || session?.serverUrl || bundledServerUrl();
      const matterId = (await store.get(MATTER_KEY)) || null;
      const tokens = session ? { accessToken: session.accessToken, refreshToken: session.refreshToken } : null;
      set({
        booted: true,
        serverUrl: savedUrl,
        session,
        matterId,
        client: savedUrl ? makeClient(savedUrl, tokens) : null,
        screen: session ? 'analyze' : 'settings',
      });
      if (session) await get().loadMatters();
    },

    setScreen: (screen) => set({ screen }),

    setServerUrl: async (url) => {
      const serverUrl = url.trim().replace(/\/+$/, '');
      const changed = serverUrl !== get().serverUrl;
      set({ serverUrl, client: serverUrl ? makeClient(serverUrl, null) : null });
      await store.set(SERVER_URL_KEY, serverUrl);
      if (changed && get().session) {
        // A session belongs to one server.
        set({ session: null, matters: [], matterId: null });
        await store.remove(SESSION_KEY);
      }
    },

    startSignIn: async () => {
      const { client, serverUrl } = get();
      if (!client || !serverUrl) {
        set({ signIn: { ...initialSignIn, status: 'error', error: 'Enter your CaseCite server address first.' } });
        return;
      }
      signInAbort?.abort();
      const abort = new AbortController();
      signInAbort = abort;
      set({ signIn: { status: 'starting', code: null, error: '' } });
      try {
        const { tokens, user } = await runDeviceFlow(client, {
          signal: abort.signal,
          onCode: (code) => {
            set({ signIn: { status: 'waiting', code, error: '' } });
            openApprovalPage(approvalUrl(serverUrl, code));
          },
        });
        const session: StoredSession = {
          serverUrl,
          accessToken: tokens.accessToken,
          refreshToken: tokens.refreshToken,
          user,
        };
        set({ session, signIn: initialSignIn, screen: 'analyze' });
        await store.set(SESSION_KEY, JSON.stringify(session));
        await get().loadMatters();
      } catch (e: unknown) {
        if (e instanceof DeviceFlowError && e.reason === 'cancelled') {
          set({ signIn: initialSignIn });
          return;
        }
        set({ signIn: { status: 'error', code: null, error: message(e, 'Sign-in failed.') } });
      } finally {
        if (signInAbort === abort) signInAbort = null;
      }
    },

    cancelSignIn: () => {
      signInAbort?.abort();
      signInAbort = null;
      set({ signIn: initialSignIn });
    },

    signOut: async () => {
      const { client } = get();
      if (client) await client.logout();
      set({ session: null, matters: [], screen: 'settings', analysis: initialAnalysis, research: initialResearch });
      await store.remove(SESSION_KEY);
    },

    loadMatters: async () => {
      const { client, session } = get();
      if (!client || !session) return;
      try {
        const matters = await client.listMatters();
        const { matterId } = get();
        set({
          matters,
          mattersError: '',
          matterId: matterId && matters.some((m) => m.id === matterId) ? matterId : null,
        });
      } catch (e: unknown) {
        set({ matters: [], mattersError: handleFailure(e, 'Could not load matters.') });
      }
    },

    setMatter: async (matterId) => {
      set({ matterId });
      if (matterId) await store.set(MATTER_KEY, matterId);
      else await store.remove(MATTER_KEY);
    },

    setRepresenting: (value) => set({ analysis: { ...get().analysis, representing: value } }),

    analyze: async () => {
      const { client, session, analysis } = get();
      if (!client || !session) return;
      set({ analysis: { ...analysis, status: 'reading', error: '', note: '' } });
      let text = '';
      try {
        text = await readDocumentText();
      } catch (e: unknown) {
        set({ analysis: { ...get().analysis, status: 'error', error: message(e, 'Could not read the document.') } });
        return;
      }
      if (!text.trim()) {
        set({ analysis: { ...get().analysis, status: 'error', error: 'The document is empty.' } });
        return;
      }
      set({ analysis: { ...get().analysis, status: 'running' } });
      try {
        const result = await client.analyzeText(text, get().analysis.representing || undefined);
        set({ analysis: { ...get().analysis, status: 'done', result } });
      } catch (e: unknown) {
        set({ analysis: { ...get().analysis, status: 'error', error: handleFailure(e, 'Analysis failed.') } });
      }
    },

    selectIssue: async (issue) => {
      set({ analysis: { ...get().analysis, busyRef: issue.ref, note: '' } });
      try {
        const hit = await selectQuote(issue.matched_text);
        set({
          analysis: {
            ...get().analysis,
            busyRef: null,
            note: hit.found ? '' : 'That passage was not found in the document as written.',
          },
        });
      } catch (e: unknown) {
        set({ analysis: { ...get().analysis, busyRef: null, note: message(e, 'Could not select the passage.') } });
      }
    },

    applyRewrite: async (issue) => {
      if (!issue.suggested_language) return;
      set({ analysis: { ...get().analysis, busyRef: issue.ref, note: '' } });
      try {
        const hit = await replaceQuoteTracked(issue.matched_text, issue.suggested_language);
        set({
          analysis: {
            ...get().analysis,
            busyRef: null,
            note: hit.found
              ? `Inserted as a tracked change. ${AI_REVIEW_NOTICE}`
              : 'That passage was not found in the document, so nothing was changed.',
          },
        });
      } catch (e: unknown) {
        set({ analysis: { ...get().analysis, busyRef: null, note: message(e, 'Could not insert the rewrite.') } });
      }
    },

    selectKeyTerm: async (term) => {
      try {
        const hit = await selectQuote(term.quote);
        set({
          analysis: {
            ...get().analysis,
            note: hit.found ? '' : 'That passage was not found in the document as written.',
          },
        });
      } catch (e: unknown) {
        set({ analysis: { ...get().analysis, note: message(e, 'Could not select the passage.') } });
      }
    },

    setQuestion: (value) => set({ research: { ...get().research, question: value } }),

    ask: async () => {
      const { client, session, research, matterId } = get();
      if (!client || !session) return;
      const question = research.question.trim();
      if (!question) return;
      set({ research: { ...research, status: 'running', error: '', note: '' } });
      try {
        let documentIds: string[] | undefined;
        if (matterId) {
          const docs = await client.listMatterDocuments(matterId);
          documentIds = docs.map((d) => d.id);
          if (documentIds.length === 0) {
            set({
              research: {
                ...get().research,
                status: 'error',
                error: 'The selected matter has no documents yet. Choose another matter or "All my documents".',
              },
            });
            return;
          }
        }
        const result = await client.research(question, documentIds);
        set({ research: { ...get().research, status: 'done', result } });
      } catch (e: unknown) {
        set({ research: { ...get().research, status: 'error', error: handleFailure(e, 'Research failed.') } });
      }
    },

    insertAnswer: async () => {
      const { research } = get();
      if (!research.result) return;
      set({ research: { ...research, inserting: true, note: '' } });
      try {
        await insertAnswerWithFootnotes(research.result.content, research.result.citations, AI_REVIEW_NOTICE);
        set({ research: { ...get().research, inserting: false, note: 'Inserted at the cursor with the sources as footnotes.' } });
      } catch (e: unknown) {
        set({ research: { ...get().research, inserting: false, note: message(e, 'Could not insert the answer.') } });
      }
    },
  };
});
