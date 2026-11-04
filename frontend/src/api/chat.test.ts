import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('./client', () => {
  const api = {
    request: vi.fn(),
    authFetch: vi.fn(),
  };
  return { api, API_BASE_URL: 'http://localhost:8000/api/v1' };
});

import { api } from './client';
import './chat';

const STARTED = { job_id: 'job-1', poll_url: '/api/v1/jobs/job-1' };
const ANSWER = { id: 'r1', content: 'Legal analysis', citations: [], session_id: 's1' };

/** Let the poll loop's sleep elapse and its promise chain settle. */
async function tick() {
  await vi.advanceTimersByTimeAsync(2000);
}

describe('api/chat', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('registers query method on api object', () => {
    expect(typeof (api as any).query).toBe('function');
  });

  it('query submits the question as a background job and returns the polled answer', async () => {
    (api.request as any)
      .mockResolvedValueOnce(STARTED)
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'pending' })
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'processing' })
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'completed', result: ANSWER });

    const pending = (api as any).query('What is tort law?', 'research');
    await tick();
    await tick();
    await tick();
    await expect(pending).resolves.toEqual(ANSWER);

    expect(api.request).toHaveBeenNthCalledWith(1, '/chat?async=true', {
      method: 'POST',
      signal: undefined,
      // Only the submission: the answer is polled, so no proxy or browser
      // timeout bounds how long the server may take.
      timeout: 60000,
      body: JSON.stringify({
        query: 'What is tort law?',
        mode: 'research',
        include_citations: true,
        // null = server decides per message whether authority helps
        include_case_law: null,
        case_law_limit: 5,
        include_documents: true,
        document_filter: null,
        session_id: null,
      }),
    });
    expect(api.request).toHaveBeenNthCalledWith(2, '/jobs/job-1', { signal: undefined });
    expect(api.request).toHaveBeenCalledTimes(4);
  });

  it('query passes custom options correctly', async () => {
    (api.request as any)
      .mockResolvedValueOnce(STARTED)
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'completed', result: ANSWER });
    const pending = (api as any).query('test', 'analysis', {
      includeCaseLaw: false,
      caseLawLimit: 10,
      includeDocuments: false,
      documentFilter: 'contracts',
      sessionId: 'sess-77',
    });
    await tick();
    await expect(pending).resolves.toEqual(ANSWER);
    expect(JSON.parse((api.request as any).mock.calls[0][1].body)).toEqual({
      query: 'test',
      mode: 'analysis',
      include_citations: true,
      include_case_law: false,
      case_law_limit: 10,
      include_documents: false,
      document_filter: 'contracts',
      session_id: 'sess-77',
    });
  });

  it('returns a synchronous answer as it is', async () => {
    (api.request as any).mockResolvedValueOnce(ANSWER);
    await expect((api as any).query('test', 'research')).resolves.toEqual(ANSWER);
    expect(api.request).toHaveBeenCalledTimes(1);
  });

  it("surfaces a failed job's message", async () => {
    (api.request as any)
      .mockResolvedValueOnce(STARTED)
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'failed', error: 'The AI provider took too long to respond. Try again.' });
    const pending = (api as any).query('test', 'research');
    // Attach the rejection handler before the loop settles.
    const outcome = pending.then(() => 'resolved', (e: Error) => e.message);
    await tick();
    expect(await outcome).toBe('The AI provider took too long to respond. Try again.');
  });

  it('rejects an answer the server could not store', async () => {
    (api.request as any)
      .mockResolvedValueOnce(STARTED)
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'completed', result: { _truncated: true }, truncated: true });
    const outcome = (api as any).query('test', 'research').then(() => 'resolved', (e: Error) => e.message);
    await tick();
    expect(await outcome).toMatch(/could not be retrieved/);
  });

  it('keeps polling through throttling and transient status failures', async () => {
    (api.request as any)
      .mockResolvedValueOnce(STARTED)
      .mockRejectedValueOnce(Object.assign(new Error('Too many requests'), { status: 429 }))
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce({ job_id: 'job-1', status: 'completed', result: ANSWER });
    const pending = (api as any).query('test', 'research');
    await vi.advanceTimersByTimeAsync(5000); // rate-limit backoff
    await tick();
    await expect(pending).resolves.toEqual(ANSWER);
  });

  it('gives up after repeated status failures', async () => {
    (api.request as any)
      .mockResolvedValueOnce(STARTED)
      .mockRejectedValue(new Error('Network error'));
    const outcome = (api as any).query('test', 'research').then(() => 'resolved', (e: Error) => e.message);
    for (let i = 0; i < 4; i += 1) await tick();
    expect(await outcome).toBe('Network error');
  });

  it('cancelling stops polling and asks the server to stop the job', async () => {
    const controller = new AbortController();
    (api.request as any).mockImplementation(async (path: string, init: { signal?: AbortSignal; method?: string } = {}) => {
      if (path === '/chat?async=true') return STARTED;
      if (init.method === 'DELETE') return { status: 'cancelled', job_id: 'job-1' };
      if (init.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      return { job_id: 'job-1', status: 'processing' };
    });
    const outcome = (api as any).query('test', 'research', {}, controller.signal)
      .then(() => 'resolved', (e: Error) => e.name);
    await tick();
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect(await outcome).toBe('AbortError');
    expect(api.request).toHaveBeenCalledWith('/jobs/job-1', { method: 'DELETE' });
    const polls = (api.request as any).mock.calls.filter(([p, init]: [string, { method?: string }]) => p === '/jobs/job-1' && !init?.method).length;
    await tick();
    await tick();
    expect((api.request as any).mock.calls.filter(([p, init]: [string, { method?: string }]) => p === '/jobs/job-1' && !init?.method).length).toBe(polls);
  });

  it('stops waiting when the wait budget runs out', async () => {
    (api.request as any)
      .mockResolvedValueOnce(STARTED)
      .mockResolvedValue({ job_id: 'job-1', status: 'processing' });
    const outcome = (api as any).query('test', 'research').then(() => 'resolved', (e: Error) => e.message);
    await vi.advanceTimersByTimeAsync(30 * 60 * 1000 + 4000);
    expect(await outcome).toMatch(/taking too long/);
  });
});
