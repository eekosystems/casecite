import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('../api', () => ({
  api: {
    query: vi.fn(),
    listChatSessions: vi.fn(),
    getChatSession: vi.fn(),
    deleteChatSession: vi.fn(),
  },
}));

vi.mock('../utils', () => ({
  generateId: vi.fn(() => 'test-id-' + Math.random().toString(36).slice(2, 8)),
  parseUtcDate: (timestamp: string) => new Date(timestamp + 'Z'),
}));

import { useResearchState } from './useResearchState';
import { api } from '../api';

describe('useResearchState', () => {
  const defaultProps = {
    addToast: vi.fn(),
    activeMode: 'research',
    setActiveMode: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('starts with empty state', () => {
    const { result } = renderHook(() => useResearchState(defaultProps));
    expect(result.current.messages).toEqual([]);
    expect(result.current.inputValue).toBe('');
    expect(result.current.isProcessing).toBe(false);
    expect(result.current.processingStage).toBe('');
    expect(result.current.selectedCitation).toBeNull();
    expect(result.current.allCitations).toEqual([]);
    expect(result.current.currentSessionId).toBeNull();
    expect(result.current.chatSessions).toEqual([]);
    // Intent and case-law routing are server-side now; scope is the only control.
    expect(result.current.mainDocFilter).toBeNull();
  });

  it('does NOT hydrate citations from localStorage — sources are per-search', () => {
    // Persisted citations resurrected phantom sources from dead sessions.
    localStorage.setItem(
      'casecite_citations',
      JSON.stringify([{ id: 'c1', text: 'Stale citation', status: 'pending' }])
    );
    const { result } = renderHook(() => useResearchState(defaultProps));
    expect(result.current.allCitations).toEqual([]);
  });

  describe('handleSend', () => {
    it('does nothing with empty input', async () => {
      const { result } = renderHook(() => useResearchState(defaultProps));
      await act(async () => {
        await result.current.handleSend();
      });
      expect(api.query).not.toHaveBeenCalled();
    });

    it('sends query and adds messages on success', async () => {
      (api.query as any).mockResolvedValue({
        response: 'Here is the answer',
        citations: [
          {
            text: 'Source text',
            source: 'doc.pdf',
            relevance_score: 0.9,
            chunk_index: 0,
          },
        ],
      });

      const { result } = renderHook(() => useResearchState(defaultProps));
      act(() => result.current.setInputValue('What is contract law?'));
      await act(async () => {
        await result.current.handleSend();
      });

      expect(api.query).toHaveBeenCalledWith(
        'What is contract law?',
        'research',
        expect.objectContaining({ sessionId: null }),
        expect.any(AbortSignal),
      );
      // Retrieval depth comes from the user's saved settings server-side.
      expect((api.query as any).mock.calls[0][2]).not.toHaveProperty('topK');
      expect(result.current.messages.length).toBeGreaterThanOrEqual(2); // user + assistant
      expect(result.current.inputValue).toBe('');
      expect(result.current.isProcessing).toBe(false);
    });

    it('adopts the server session id and sends it on the next query', async () => {
      (api.query as any).mockResolvedValue({
        content: 'Answer',
        citations: [],
        session_id: 'sess-123',
      });

      const { result } = renderHook(() => useResearchState(defaultProps));
      act(() => result.current.setInputValue('first question'));
      await act(async () => {
        await result.current.handleSend();
      });

      expect(result.current.currentSessionId).toBe('sess-123');

      act(() => result.current.setInputValue('follow-up question'));
      await act(async () => {
        await result.current.handleSend();
      });

      expect(api.query).toHaveBeenLastCalledWith(
        'follow-up question',
        'research',
        expect.objectContaining({ sessionId: 'sess-123' }),
        expect.any(AbortSignal),
      );
    });

    it('handles API error gracefully', async () => {
      (api.query as any).mockRejectedValue(new Error('Network error'));

      const { result } = renderHook(() => useResearchState(defaultProps));
      act(() => result.current.setInputValue('test'));
      await act(async () => {
        await result.current.handleSend();
      });

      // Should add an error message
      const lastMsg = result.current.messages[result.current.messages.length - 1];
      expect(lastMsg.type).toBe('assistant');
      expect(lastMsg.content).toMatch(/error|failed|sorry/i);
      expect(result.current.isProcessing).toBe(false);
    });
  });

  describe('loadSession', () => {
    const sessionDetail = {
      id: 'sess-42',
      title: 'Sublease questions',
      created_at: '2026-07-01T10:00:00',
      updated_at: '2026-07-01T10:05:00',
      messages: [
        {
          id: 'm1',
          role: 'user',
          content: 'Can we sublet the office?',
          citations: [],
          strategy: null,
          stats: null,
          created_at: '2026-07-01T10:00:00',
        },
        {
          id: 'm2',
          role: 'assistant',
          content: 'Yes, with landlord consent.',
          citations: [
            { id: 'c-old', source: 'lease.pdf', passage: 'Tenant may sublet...', confidence: 90 },
          ],
          strategy: null,
          stats: { docs_searched: 5, chunks_retrieved: 3, processing_time: '1.2s' },
          created_at: '2026-07-01T10:01:00',
        },
        {
          id: 'm3',
          role: 'user',
          content: 'What notice period applies?',
          citations: [],
          strategy: null,
          stats: null,
          created_at: '2026-07-01T10:04:00',
        },
        {
          id: 'm4',
          role: 'assistant',
          content: '30 days written notice.',
          citations: [
            { id: 'c-new', source: 'lease.pdf', passage: '30 days notice...', confidence: 85 },
          ],
          strategy: null,
          stats: { docs_searched: 5, chunks_retrieved: 2, processing_time: '0.9s' },
          created_at: '2026-07-01T10:05:00',
        },
      ],
    };

    it('maps the persisted thread into chat messages', async () => {
      (api.getChatSession as any).mockResolvedValue(sessionDetail);

      const { result } = renderHook(() => useResearchState(defaultProps));
      await act(async () => {
        await result.current.loadSession('sess-42');
      });

      expect(api.getChatSession).toHaveBeenCalledWith('sess-42');
      expect(result.current.messages).toHaveLength(4);
      expect(result.current.messages[0].type).toBe('user');
      expect(result.current.messages[0].content).toBe('Can we sublet the office?');
      expect(result.current.messages[1].type).toBe('assistant');
      expect(result.current.messages[1].citations?.[0].id).toBe('c-old');
      expect(result.current.messages[1].stats?.docsSearched).toBe(5);
      expect(result.current.messages[1].stats?.processingTime).toBe('1.2s');
      expect(result.current.messages[3].timestamp).toBeInstanceOf(Date);

      // Sources panel accumulates EVERY exchange's citations, each tagged with
      // its message and question for per-question grouping + jump-to-answer.
      expect(result.current.allCitations).toHaveLength(2);
      expect(result.current.allCitations[0].id).toBe('c-old');
      expect(result.current.allCitations[0].messageId).toBe('m2');
      expect(result.current.allCitations[0].sourceQuery).toBe('Can we sublet the office?');
      expect(result.current.allCitations[1].id).toBe('c-new');
      expect(result.current.allCitations[1].messageId).toBe('m4');

      expect(result.current.currentSessionId).toBe('sess-42');
      expect(defaultProps.setActiveMode).toHaveBeenCalledWith('research');
    });

    it('surfaces a toast when the session cannot be loaded', async () => {
      (api.getChatSession as any).mockRejectedValue(new Error('Not found'));

      const { result } = renderHook(() => useResearchState(defaultProps));
      await act(async () => {
        await result.current.loadSession('missing');
      });

      expect(defaultProps.addToast).toHaveBeenCalledWith('Not found', 'error');
      expect(result.current.currentSessionId).toBeNull();
    });
  });

  describe('deleteSession', () => {
    it('removes the session from the list and resets the active thread', async () => {
      (api.query as any).mockResolvedValue({
        content: 'Answer',
        citations: [],
        session_id: 'sess-del',
      });
      (api.deleteChatSession as any).mockResolvedValue({ status: 'deleted', id: 'sess-del' });

      const { result } = renderHook(() => useResearchState(defaultProps));
      act(() => result.current.setInputValue('question'));
      await act(async () => {
        await result.current.handleSend();
      });
      expect(result.current.currentSessionId).toBe('sess-del');

      await act(async () => {
        await result.current.deleteSession('sess-del');
      });

      expect(api.deleteChatSession).toHaveBeenCalledWith('sess-del');
      expect(result.current.currentSessionId).toBeNull();
      expect(result.current.messages).toEqual([]);
    });
  });

  describe('handleClearSession', () => {
    it('resets messages, citations and session id', async () => {
      (api.query as any).mockResolvedValue({
        content: 'Answer',
        citations: [],
        session_id: 'sess-clear',
      });

      const { result } = renderHook(() => useResearchState(defaultProps));
      act(() => result.current.setInputValue('test'));
      await act(async () => {
        await result.current.handleSend();
      });
      expect(result.current.currentSessionId).toBe('sess-clear');

      act(() => result.current.handleClearSession());

      expect(result.current.messages).toEqual([]);
      expect(result.current.allCitations).toEqual([]);
      expect(result.current.currentSessionId).toBeNull();
    });
  });

  describe('a failed or cancelled question', () => {
    const answerWithSource = {
      content: 'Answer',
      citations: [{ text: 'C1', source: 'a.pdf', relevance_score: 0.9, chunk_index: 0 }],
    };

    it('keeps the sources of earlier answers when a later query fails', async () => {
      (api.query as any).mockResolvedValueOnce(answerWithSource);
      const { result } = renderHook(() => useResearchState(defaultProps));
      act(() => result.current.setInputValue('first'));
      await act(async () => { await result.current.handleSend(); });
      const before = result.current.allCitations;
      expect(before.length).toBeGreaterThan(0);

      (api.query as any).mockRejectedValueOnce(new Error('LLM timed out'));
      act(() => result.current.setInputValue('second'));
      await act(async () => { await result.current.handleSend(); });

      expect(result.current.allCitations).toEqual(before);
      expect(result.current.messages[result.current.messages.length - 1].isError).toBe(true);
      expect(defaultProps.addToast).toHaveBeenCalledWith('LLM timed out', 'error');
      expect(result.current.isProcessing).toBe(false);
    });

    it('handleCancel aborts the in-flight request without an error toast', async () => {
      let signal: AbortSignal | undefined;
      (api.query as any).mockImplementation((_q: string, _m: string, _o: unknown, sig: AbortSignal) => {
        signal = sig;
        return new Promise((_resolve, reject) => {
          sig.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        });
      });

      const { result } = renderHook(() => useResearchState(defaultProps));
      act(() => result.current.setInputValue('slow question'));
      let pending: Promise<void>;
      act(() => { pending = result.current.handleSend(); });
      expect(result.current.isProcessing).toBe(true);
      expect(result.current.processingStartedAt).not.toBeNull();

      await act(async () => {
        result.current.handleCancel();
        await pending;
      });

      expect(signal?.aborted).toBe(true);
      expect(result.current.isProcessing).toBe(false);
      expect(result.current.processingStartedAt).toBeNull();
      const last = result.current.messages[result.current.messages.length - 1];
      expect(last.isError).toBeUndefined();
      expect(last.content).toMatch(/^Cancelled/);
      expect(defaultProps.addToast).not.toHaveBeenCalled();
    });
  });
});
