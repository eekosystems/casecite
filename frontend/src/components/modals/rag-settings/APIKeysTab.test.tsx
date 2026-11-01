import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent, act } from '@testing-library/react';
import { APIKeysTab } from './APIKeysTab';
import { api } from '../../../api/client';
import type { RagSettings } from '../../../types';

vi.mock('../../../api/client', () => ({
  api: { request: vi.fn().mockResolvedValue({}) },
}));

/** Answer one endpoint+method with `response`; everything else gets `{}`. */
function answer(endpoint: string, method: string, response: Record<string, unknown>) {
  (api.request as ReturnType<typeof vi.fn>).mockImplementation((ep: string, opts?: { method?: string }) =>
    Promise.resolve(ep === endpoint && (opts?.method || 'GET') === method ? response : {}));
}

function defaultProps(overrides: Record<string, any> = {}) {
  const local: RagSettings = {
    vectorDb: 'chroma',
    indexName: 'default',
    embeddingModel: 'text-embedding-3-small',
    dimensions: 1536,
    chunkSize: 512,
    chunkOverlap: 64,
    similarityThreshold: 0.7,
    topK: 10,
    enableReranking: true,
    hybridSearch: true,
    citationVerification: true,
    contextCompression: false,
    queryExpansion: false,
    sourceTracking: true,
    llmModel: 'gpt-5.5',
    temperature: 0.1,
    maxTokens: 4096,
  };
  return {
    local,
    setLocal: vi.fn(),
    showKeys: {} as Record<string, boolean>,
    setShowKeys: vi.fn(),
    apiKeys: { openai: '', anthropic: '', google: '', voyage: '', cohere: '' } as Record<string, string>,
    setApiKeys: vi.fn(),
    serverKeyStatus: {} as Record<string, boolean>,
    maskedKeys: {} as Record<string, string | null>,
    keyStatusError: false,
    refreshKeyStatus: vi.fn(async () => {}),
    styles: new Proxy({} as Record<string, string>, { get: (_, key) => String(key) }),
    ...overrides,
  };
}

describe('APIKeysTab', () => {
  it('renders without crashing', () => {
    const { container } = render(<APIKeysTab {...defaultProps()} />);
    expect(container).toBeTruthy();
  });

  it('displays the Bring Your Own Keys banner', () => {
    const { container } = render(<APIKeysTab {...defaultProps()} />);
    expect(container.textContent).toContain('Bring Your Own Keys');
  });

  it('shows all provider key fields', () => {
    const { container } = render(<APIKeysTab {...defaultProps()} />);
    expect(container.textContent).toContain('OpenAI API Key');
    expect(container.textContent).toContain('Anthropic API Key');
    expect(container.textContent).toContain('Google Gemini API Key');
    expect(container.textContent).toContain('Voyage AI Key');
    expect(container.textContent).toContain('Cohere API Key');
  });

  it('shows the key status section', () => {
    const { container } = render(<APIKeysTab {...defaultProps()} />);
    expect(container.textContent).toContain('Key Status');
  });

  describe('integration saves', () => {
    afterEach(() => {
      vi.useRealTimers();
      (api.request as ReturnType<typeof vi.fn>).mockReset();
      (api.request as ReturnType<typeof vi.fn>).mockResolvedValue({});
    });

    it('marks CourtListener configured from the save response', async () => {
      vi.useFakeTimers();
      answer('/admin/integrations/courtlistener', 'POST', { status: 'saved', configured: true, source: 'instance', masked: 'abcd...wxyz' });
      const { container } = render(<APIKeysTab {...defaultProps()} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(container.textContent).not.toContain('Configured (source: instance)');

      const input = container.querySelector('input[placeholder="Free Law Project API token"]') as HTMLInputElement;
      fireEvent.change(input, { target: { value: 'token-1234567890' } });
      await act(async () => { await vi.advanceTimersByTimeAsync(800); });

      expect(container.textContent).toContain('Configured (source: instance)');
      const badge = Array.from(container.querySelectorAll('div')).find((el) => el.className.includes('keyBadge') && el.textContent === 'CourtListener');
      expect(badge?.className).toContain('keyBadgeActive');
    });

    it('marks the local model configured from the save response', async () => {
      vi.useFakeTimers();
      answer('/admin/integrations/local-llm', 'POST', {
        status: 'saved', configured: true, base_url: 'http://localhost:11434/v1', chat_model: 'llama3.1:70b', utility_model: null,
      });
      const { container } = render(<APIKeysTab {...defaultProps()} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      fireEvent.change(container.querySelector('input[placeholder="http://localhost:11434/v1"]')!, { target: { value: 'http://localhost:11434/v1' } });
      fireEvent.change(container.querySelector('input[placeholder="llama3.1:70b"]')!, { target: { value: 'llama3.1:70b' } });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });

      const badge = Array.from(container.querySelectorAll('div')).find((el) => el.className.includes('keyBadge') && el.textContent === 'Local Model');
      expect(badge?.className).toContain('keyBadgeActive');
    });
  });

  describe('local models', () => {
    const OLLAMA = 'http://ollama:11434/v1';
    const VLLM = 'http://vllm.internal:8000/v1';

    afterEach(() => { vi.useRealTimers(); });

    const urlInputs = (c: HTMLElement) =>
      Array.from(c.querySelectorAll<HTMLInputElement>('input[aria-label^="Server URL for model"]'));
    const nameInputs = (c: HTMLElement) =>
      Array.from(c.querySelectorAll<HTMLInputElement>('input[aria-label^="Name of model"]'));
    const savedBodies = () => (api.request as ReturnType<typeof vi.fn>).mock.calls
      .filter(([ep, opts]) => ep === '/admin/integrations/local-llm' && opts?.method === 'POST')
      .map(([, opts]) => JSON.parse(opts.body));

    it('saves several models on different servers, with a model per role', async () => {
      vi.useFakeTimers();
      const view = render(<APIKeysTab {...defaultProps()} />);
      const { container } = view;
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      fireEvent.change(urlInputs(container)[0], { target: { value: OLLAMA } });
      fireEvent.change(nameInputs(container)[0], { target: { value: 'qwen3:27b' } });
      fireEvent.click(view.getByText('Add model'));
      // A new row starts on the previous row's server.
      expect(urlInputs(container)[1].value).toBe(OLLAMA);
      fireEvent.change(nameInputs(container)[1], { target: { value: 'qwen3:4b' } });
      fireEvent.click(view.getByText('Add model'));
      fireEvent.change(urlInputs(container)[2], { target: { value: VLLM } });
      fireEvent.change(nameInputs(container)[2], { target: { value: 'llama-3.3-70b' } });

      fireEvent.change(view.getByLabelText('Chat Model'), { target: { value: 'llama-3.3-70b' } });
      fireEvent.change(view.getByLabelText('Utility Model'), { target: { value: 'qwen3:4b' } });
      fireEvent.change(view.getByLabelText('Research Model'), { target: { value: 'qwen3:27b' } });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });

      const bodies = savedBodies();
      expect(bodies).toHaveLength(1); // debounced: one save for the whole edit
      expect(bodies[0]).toEqual({
        models: [
          { name: 'qwen3:27b', base_url: OLLAMA },
          { name: 'qwen3:4b', base_url: OLLAMA },
          { name: 'llama-3.3-70b', base_url: VLLM },
        ],
        chat_model: 'llama-3.3-70b',
        utility_model: 'qwen3:4b',
        research_model: 'qwen3:27b',
      });
    });

    it('loads the saved models and roles', async () => {
      answer('/admin/integrations/local-llm', 'GET', {
        configured: true, base_url: VLLM, chat_model: 'llama-3.3-70b', utility_model: 'qwen3:4b',
        research_model: null, embeddings_provider: 'self_hosted',
        models: [{ name: 'qwen3:4b', base_url: OLLAMA }, { name: 'llama-3.3-70b', base_url: VLLM }],
      });
      const view = render(<APIKeysTab {...defaultProps()} />);
      await act(async () => {});

      expect(nameInputs(view.container).map((i) => i.value)).toEqual(['qwen3:4b', 'llama-3.3-70b']);
      expect(urlInputs(view.container).map((i) => i.value)).toEqual([OLLAMA, VLLM]);
      expect((view.getByLabelText('Chat Model') as HTMLSelectElement).value).toBe('llama-3.3-70b');
      expect((view.getByLabelText('Utility Model') as HTMLSelectElement).value).toBe('qwen3:4b');
      expect((view.getByLabelText('Research Model') as HTMLSelectElement).value).toBe('');
    });

    it('removing a model also clears the role it filled', async () => {
      answer('/admin/integrations/local-llm', 'GET', {
        configured: true, base_url: OLLAMA, chat_model: 'qwen3:27b', utility_model: 'qwen3:4b',
        research_model: null, embeddings_provider: 'self_hosted',
        models: [{ name: 'qwen3:27b', base_url: OLLAMA }, { name: 'qwen3:4b', base_url: OLLAMA }],
      });
      const view = render(<APIKeysTab {...defaultProps()} />);
      await act(async () => {});
      vi.useFakeTimers();

      fireEvent.click(view.getByLabelText('Remove model 2'));
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });

      expect(nameInputs(view.container)).toHaveLength(1);
      const bodies = savedBodies();
      expect(bodies[bodies.length - 1]).toEqual({
        models: [{ name: 'qwen3:27b', base_url: OLLAMA }],
        chat_model: 'qwen3:27b',
        utility_model: null,
        research_model: null,
      });
    });

    it('does not save while a model name is listed twice', async () => {
      vi.useFakeTimers();
      const view = render(<APIKeysTab {...defaultProps()} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      fireEvent.change(urlInputs(view.container)[0], { target: { value: OLLAMA } });
      fireEvent.change(nameInputs(view.container)[0], { target: { value: 'qwen3' } });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      const before = savedBodies().length;

      fireEvent.click(view.getByText('Add model'));
      fireEvent.change(nameInputs(view.container)[1], { target: { value: 'qwen3' } });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });

      expect(savedBodies()).toHaveLength(before);
      expect(view.container.textContent).toContain('is listed more than once');
    });

    it('warns when chat is local but embeddings go to a hosted provider', async () => {
      answer('/admin/integrations/local-llm', 'GET', {
        configured: true, base_url: 'http://localhost:11434/v1', chat_model: 'qwen3:27b',
        utility_model: 'qwen3:27b', research_model: null, embeddings_provider: 'openai',
      });
      const { container } = render(<APIKeysTab {...defaultProps()} />);
      await act(async () => {});
      expect(container.textContent).toContain('they go to openai');
    });

    it('says nothing about embeddings when they are self-hosted too', async () => {
      answer('/admin/integrations/local-llm', 'GET', {
        configured: true, base_url: 'http://localhost:11434/v1', chat_model: 'qwen3:27b',
        utility_model: 'qwen3:27b', research_model: null, embeddings_provider: 'self_hosted',
      });
      const { container } = render(<APIKeysTab {...defaultProps()} />);
      await act(async () => {});
      expect(container.textContent).not.toContain("Embeddings don't use these model servers");
    });
  });

  it('has password inputs for api keys', () => {
    const { container } = render(<APIKeysTab {...defaultProps()} />);
    const passwordInputs = container.querySelectorAll('input[type="password"]');
    expect(passwordInputs.length).toBeGreaterThanOrEqual(5);
  });
});
