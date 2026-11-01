import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, act } from '@testing-library/react';
import { GenerationTab } from './GenerationTab';
import { api } from '../../../api';
import type { RagSettings } from '../../../types';

vi.mock('../../../api', () => ({
  api: { getInstanceModels: vi.fn() },
}));

const getInstanceModels = api.getInstanceModels as ReturnType<typeof vi.fn>;

const OPENAI_INSTANCE = {
  custom_endpoint: false, chat_model: 'gpt-5.5', utility_model: 'gpt-5.4-mini', research_model: 'gpt-5.5',
};
const LOCAL_INSTANCE = {
  custom_endpoint: true, chat_model: 'qwen3:27b', utility_model: 'qwen3:4b', research_model: 'qwen3:27b',
};

async function renderWith(instance: object, overrides: Record<string, any> = {}) {
  getInstanceModels.mockResolvedValue(instance);
  const view = render(<GenerationTab {...defaultProps(overrides)} />);
  await act(async () => {});
  return view;
}

function defaultProps({ llmModel = 'gpt-5.5', ...overrides }: Record<string, any> = {}) {
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
    llmModel,
    temperature: 0.1,
    maxTokens: 4096,
  };
  return {
    local,
    setLocal: vi.fn(),
    styles: new Proxy({} as Record<string, string>, { get: (_, key) => String(key) }),
    ...overrides,
  };
}

describe('GenerationTab', () => {
  beforeEach(() => {
    getInstanceModels.mockReset();
    getInstanceModels.mockResolvedValue(OPENAI_INSTANCE);
  });

  it('renders without crashing', () => {
    const { container } = render(<GenerationTab {...defaultProps()} />);
    expect(container).toBeTruthy();
  });

  it('shows LLM model selector', () => {
    const { container } = render(<GenerationTab {...defaultProps()} />);
    expect(container.textContent).toContain('LLM Model');
  });

  it('shows temperature slider', () => {
    const { container } = render(<GenerationTab {...defaultProps()} />);
    expect(container.textContent).toContain('Temperature');
  });

  it('shows max tokens slider', () => {
    const { container } = render(<GenerationTab {...defaultProps()} />);
    expect(container.textContent).toContain('Max Tokens');
  });

  it('lists model options from multiple providers', () => {
    const { container } = render(<GenerationTab {...defaultProps()} />);
    expect(container.textContent).toContain('GPT-5.5');
    expect(container.textContent).toContain('Claude Opus 4.8');
    expect(container.textContent).toContain('Gemini 3.1 Pro');
  });

  it('offers the instance default, named after the configured chat model', async () => {
    const { container } = await renderWith(OPENAI_INSTANCE, { llmModel: '' });
    const select = container.querySelector('select')!;
    expect(select.value).toBe('');
    expect(select.options[0].textContent).toBe('Instance default (gpt-5.5)');
  });

  it('offers the local models instead of OpenAI ones when a model server is connected', async () => {
    const { container } = await renderWith(LOCAL_INSTANCE, { llmModel: '' });
    const options = Array.from(container.querySelectorAll('option')).map((o) => o.value);
    expect(options).toContain('qwen3:27b');
    expect(options).toContain('qwen3:4b');
    expect(options).not.toContain('gpt-5.5');
    expect(container.textContent).toContain('Instance default (qwen3:27b)');
  });

  it('shows an old GPT default as the instance default on a model server', async () => {
    // Settings saved by earlier builds hold "gpt-5.5"; the server answers those
    // with the local chat model, so the picker says so.
    const { container } = await renderWith(LOCAL_INSTANCE, { llmModel: 'gpt-5.5' });
    expect(container.querySelector('select')!.value).toBe('');
  });

  it('keeps a Claude choice on a model server', async () => {
    const { container } = await renderWith(LOCAL_INSTANCE, { llmModel: 'claude-opus-4-8' });
    expect(container.querySelector('select')!.value).toBe('claude-opus-4-8');
  });

  it('has range inputs for sliders', () => {
    const { container } = render(<GenerationTab {...defaultProps()} />);
    const ranges = container.querySelectorAll('input[type="range"]');
    expect(ranges.length).toBe(2);
  });
});
