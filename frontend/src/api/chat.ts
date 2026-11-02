import { api, API_BASE_URL } from './client';
import type {
  ChatResponse,
  ChatJob,
  ChatJobStarted,
  ChatQueryOptions,
  ConversationExportPayload,
} from './types';

/** How often a chat job is polled for its answer. */
const CHAT_POLL_INTERVAL_MS = 2000;
/** A 429 from /jobs is the poller being throttled, not the job failing. */
const CHAT_POLL_RATE_LIMIT_BACKOFF_MS = 5000;
/** Consecutive status-request failures tolerated before the answer is given up. */
const CHAT_POLL_MAX_TRANSIENT_FAILURES = 3;
/**
 * Longest wait for an answer. A strategy brief reads opinions in full for up
 * to eight minutes (CASE_LAW_TIME_BUDGET_SECONDS) and then synthesises, so
 * this is well past the longest legitimate answer. The server keeps a running
 * job for two hours regardless.
 */
export const CHAT_ANSWER_WAIT_MS = 30 * 60 * 1000;
/**
 * Submitting the question only: the server answers at once with a job to
 * poll, after resolving the document scope and loading the conversation.
 */
const CHAT_SUBMIT_TIMEOUT_MS = 60000;

function isJobStarted(value: unknown): value is ChatJobStarted {
  return !!value && typeof value === 'object'
    && typeof (value as { job_id?: unknown }).job_id === 'string'
    && !('content' in (value as object));
}

function isRateLimited(error: unknown): boolean {
  return !!error && typeof error === 'object' && (error as { status?: unknown }).status === 429;
}

function cancelledError(): Error {
  return typeof DOMException !== 'undefined'
    ? new DOMException('The question was cancelled.', 'AbortError')
    : Object.assign(new Error('The question was cancelled.'), { name: 'AbortError' });
}

/** Resolve after `ms`, or at once when `signal` aborts. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) { resolve(); return; }
    const done = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal?.addEventListener('abort', done, { once: true });
  });
}

/**
 * Poll a chat job until it settles and return its answer. Rejects when the
 * job fails or is cancelled, when `signal` aborts, when status requests keep
 * failing, or when the wait budget runs out.
 */
async function waitForChatAnswer(jobId: string, signal?: AbortSignal): Promise<ChatResponse> {
  const path = `/jobs/${encodeURIComponent(jobId)}`;
  const deadline = Date.now() + CHAT_ANSWER_WAIT_MS;
  let transientFailures = 0;
  for (;;) {
    if (signal?.aborted) throw cancelledError();
    let job: ChatJob;
    try {
      job = await api.request(path, { signal });
      transientFailures = 0;
    } catch (error) {
      if (signal?.aborted) throw error;
      if (isRateLimited(error)) {
        // Throttled: wait it out and keep polling — the job is still running.
        await sleep(CHAT_POLL_RATE_LIMIT_BACKOFF_MS, signal);
        continue;
      }
      transientFailures += 1;
      if (transientFailures > CHAT_POLL_MAX_TRANSIENT_FAILURES) throw error;
      await sleep(CHAT_POLL_INTERVAL_MS, signal);
      continue;
    }
    if (job.status === 'completed') {
      if (job.truncated || !job.result) {
        throw new Error('The answer could not be retrieved. Please try again with a narrower question.');
      }
      return job.result;
    }
    if (job.status === 'failed') throw new Error(job.error || 'The answer failed. Please try again.');
    if (job.status === 'cancelled') throw new Error('The question was cancelled.');
    if (Date.now() >= deadline) {
      throw new Error('The answer is taking too long. Please try again, or ask a narrower question.');
    }
    await sleep(CHAT_POLL_INTERVAL_MS, signal);
  }
}

Object.assign(api, {
  async exportConversation(payload: ConversationExportPayload): Promise<Blob> {
    const resp = await api.authFetch(`${API_BASE_URL}/chat/export`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      throw new Error(resp.status === 501 ? 'PDF export is not available on this server.' : `Export failed (${resp.status})`);
    }
    return resp.blob();
  },

  /**
   * Ask a question and wait for the answer. The question is submitted as a
   * background job and the answer polled from /jobs/{id}: an answer can take
   * minutes (strategy briefs and verified case-law answers read full opinions
   * from CourtListener), longer than one HTTP request survives a browser, the
   * container's nginx or the platform's front proxy. Aborting `signal` stops
   * waiting and asks the server to stop working on the question.
   */
  async query(message: string, mode: string, options: ChatQueryOptions = {}, signal?: AbortSignal): Promise<ChatResponse> {
    const started: ChatResponse | ChatJobStarted = await api.request('/chat?async=true', {
      method: 'POST',
      signal,
      timeout: CHAT_SUBMIT_TIMEOUT_MS,
      body: JSON.stringify({
        query: message,
        mode: mode,
        include_citations: true,
        // null = the server decides per message whether authority helps.
        include_case_law: options.includeCaseLaw ?? null,
        case_law_limit: options.caseLawLimit || 5,
        include_documents: options.includeDocuments !== false,
        document_filter: options.documentFilter || null,
        // Persisted session to append to; null lets the server start a new one.
        session_id: options.sessionId || null,
      }),
    });
    // A server that answered in one go.
    if (!isJobStarted(started)) return started;
    try {
      return await waitForChatAnswer(started.job_id, signal);
    } catch (error) {
      if (signal?.aborted) {
        // Best effort: the server stops the job (nothing is saved to History).
        void api.request(`/jobs/${encodeURIComponent(started.job_id)}`, { method: 'DELETE' }).catch(() => undefined);
      }
      throw error;
    }
  },
});
