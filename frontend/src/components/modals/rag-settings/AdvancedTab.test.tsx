import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import { AdvancedTab } from './AdvancedTab';
import type { RagSettings } from '../../../types';

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
    styles: new Proxy({} as Record<string, string>, { get: (_, key) => String(key) }),
    ...overrides,
  };
}

describe('AdvancedTab', () => {
  it('renders without crashing', () => {
    const { container } = render(<AdvancedTab {...defaultProps()} />);
    expect(container).toBeTruthy();
  });

  it('shows all toggle options', () => {
    const { container } = render(<AdvancedTab {...defaultProps()} />);
    expect(container.textContent).toContain('Cross-Encoder Reranking');
    expect(container.textContent).toContain('Keyword Rescoring (BM25)');
    expect(container.textContent).toContain('Citation Verification');
    expect(container.textContent).toContain('Context Compression');
    expect(container.textContent).toContain('Query Expansion');
    expect(container.textContent).toContain('Source Tracking');
  });

  it('renders toggle buttons plus the rebuild-index button', () => {
    const { container } = render(<AdvancedTab {...defaultProps()} />);
    const buttons = container.querySelectorAll('button');
    expect(buttons.length).toBe(8); // 7 toggles + Rebuild Search Index
    expect(container.textContent).toContain('Rebuild Search Index');
  });

  it('shows descriptions for each toggle', () => {
    const { container } = render(<AdvancedTab {...defaultProps()} />);
    // The copy states the real conditions rather than promising a reranker
    // or a separate keyword search.
    expect(container.textContent).toContain('only when the server has the optional sentence-transformers package');
    expect(container.textContent).toContain('Blend keyword-match scores');
  });
});
