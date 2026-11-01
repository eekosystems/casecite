import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, act } from '@testing-library/react';
import { EmbeddingTab } from './EmbeddingTab';
import { api } from '../../../api';
import { useUIStore } from '../../../stores/uiStore';

vi.mock('../../../api', () => ({
  api: {
    getEmbeddingInfo: vi.fn(),
    getEmbeddingStatus: vi.fn(),
    setEmbeddings: vi.fn(),
    clearEmbeddings: vi.fn(),
    reindexEmbeddings: vi.fn(),
  },
}));

const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const styles = new Proxy({} as Record<string, string>, { get: (_, key) => String(key) });

const IDLE = { state: 'idle', total: 0, done: 0, failed: 0, started_at: null, finished_at: null, error: null };

const ENV_STATUS = {
  source: 'env', vector_store: 'chroma', editable: true, provider: 'openai', base_url: null,
  model: 'text-embedding-3-small', dimensions: 1536, api_key_set: false,
  collection: 'legal_documents', reindex: IDLE,
};
const LOCAL_STATUS = {
  ...ENV_STATUS, source: 'instance', provider: 'self_hosted', base_url: 'http://localhost:11434/v1',
  model: 'nomic-embed-text', dimensions: 768, collection: 'legal_documents_abc',
};

async function renderTab(isAdmin: boolean) {
  const view = render(<EmbeddingTab isAdmin={isAdmin} styles={styles} />);
  await act(async () => {});
  return view;
}

describe('EmbeddingTab', () => {
  beforeEach(() => {
    Object.values(mocked).forEach((fn) => fn.mockReset());
    useUIStore.setState(useUIStore.getInitialState(), true);
  });
  afterEach(() => { vi.useRealTimers(); });

  it('shows users the model in use, read-only', async () => {
    mocked.getEmbeddingInfo.mockResolvedValue({
      source: 'instance', provider: 'self_hosted', model: 'nomic-embed-text', dimensions: 768,
      reindex_state: 'idle', reindex_done: 0, reindex_total: 0,
    });
    const { container } = await renderTab(false);
    expect(container.textContent).toContain('nomic-embed-text');
    expect(container.textContent).toContain('on your own server');
    expect(container.textContent).toContain('Your administrator manages the embedding model');
    expect(container.querySelector('input')).toBeNull();
    expect(mocked.getEmbeddingStatus).not.toHaveBeenCalled();
  });

  it('lets an admin save a local server after confirming', async () => {
    mocked.getEmbeddingStatus.mockResolvedValue(ENV_STATUS);
    mocked.setEmbeddings.mockResolvedValue({ ...LOCAL_STATUS, status: 'saved', reindex: { ...IDLE, state: 'running' } });
    const { container, getByText } = await renderTab(true);

    fireEvent.change(container.querySelector('input[placeholder="http://localhost:11434/v1"]')!, { target: { value: 'http://localhost:11434/v1' } });
    fireEvent.change(container.querySelector('input[placeholder="nomic-embed-text"]')!, { target: { value: 'nomic-embed-text' } });
    fireEvent.click(getByText('Save and re-index'));

    // The change re-embeds everything, so it asks first.
    expect(useUIStore.getState().confirmModal.open).toBe(true);
    expect(mocked.setEmbeddings).not.toHaveBeenCalled();
    await act(async () => { useUIStore.getState().handleConfirm(); });

    expect(mocked.setEmbeddings).toHaveBeenCalledWith({
      baseUrl: 'http://localhost:11434/v1', model: 'nomic-embed-text', dimensions: null, apiKey: undefined,
    });
    expect(container.textContent).toContain('Re-indexing documents with the new model');
  });

  it('shows the server check failure to the admin', async () => {
    mocked.getEmbeddingStatus.mockResolvedValue(ENV_STATUS);
    mocked.setEmbeddings.mockRejectedValue(new Error('The embedding server at http://x/v1 did not return an embedding'));
    const { container, getByText } = await renderTab(true);

    fireEvent.change(container.querySelector('input[placeholder="http://localhost:11434/v1"]')!, { target: { value: 'http://x/v1' } });
    fireEvent.change(container.querySelector('input[placeholder="nomic-embed-text"]')!, { target: { value: 'm' } });
    fireEvent.click(getByText('Save and re-index'));
    await act(async () => { useUIStore.getState().handleConfirm(); });

    const toasts = useUIStore.getState().toasts;
    expect(JSON.stringify(toasts)).toContain('did not return an embedding');
  });

  it('fills the form from saved settings and offers switching back', async () => {
    mocked.getEmbeddingStatus.mockResolvedValue({ ...LOCAL_STATUS, api_key_set: true });
    const { container, getByText } = await renderTab(true);
    expect((container.querySelector('input[placeholder="http://localhost:11434/v1"]') as HTMLInputElement).value).toBe('http://localhost:11434/v1');
    expect(container.querySelector('input[type="password"]')!.getAttribute('placeholder')).toContain('saved');
    expect(getByText('Use .env settings')).toBeTruthy();
  });

  it('offers a retry when documents failed', async () => {
    mocked.getEmbeddingStatus.mockResolvedValue({ ...LOCAL_STATUS, reindex: { ...IDLE, state: 'done', total: 5, done: 5, failed: 2 } });
    mocked.reindexEmbeddings.mockResolvedValue({ ...LOCAL_STATUS, status: 'started', reindex: { ...IDLE, state: 'running' } });
    const { container, getByText } = await renderTab(true);
    expect(container.textContent).toContain('2 documents could not be re-indexed');
    await act(async () => { fireEvent.click(getByText('Retry failed documents')); });
    expect(mocked.reindexEmbeddings).toHaveBeenCalled();
  });

  it('polls while re-indexing runs', async () => {
    vi.useFakeTimers();
    mocked.getEmbeddingStatus.mockResolvedValue({ ...LOCAL_STATUS, reindex: { ...IDLE, state: 'running', total: 4, done: 1 } });
    await renderTab(true);
    expect(mocked.getEmbeddingStatus).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(mocked.getEmbeddingStatus).toHaveBeenCalledTimes(2);
  });

  it('explains that Pinecone is configured in .env', async () => {
    mocked.getEmbeddingStatus.mockResolvedValue({ ...ENV_STATUS, vector_store: 'pinecone', editable: false });
    const { container } = await renderTab(true);
    expect(container.textContent).toContain('Pinecone');
    expect(container.querySelector('input')).toBeNull();
  });
});
