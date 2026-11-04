import { describe, it, expect, vi } from 'vitest';

vi.mock('./index', () => ({
  generateId: vi.fn().mockReturnValue('mock-id'),
}));

import { parseCitations } from './citationParser';

describe('citationParser', () => {
  it('parses basic citation data', () => {
    const raw = [{
      id: 'c1',
      source: 'Smith v. Jones',
      type: 'case_law',
      confidence: 0.95,
      status: 'verified',
      similarity: 0.9,
      relevance_rank: 1,
      chunk_index: 0,
      token_count: 500,
      passage: 'The court held...',
      reasoning: [{ type: 'analysis', description: 'Relevant precedent', evidence: 'direct' }],
      application: 'Directly applicable',
    }];

    const result = parseCitations(raw, 'Find cases about negligence');
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('c1');
    expect(result[0].source).toBe('Smith v. Jones');
    expect(result[0].type).toBe('case_law');
    expect(result[0].confidence).toBe(95);
    expect(result[0].status).toBe('verified');
    expect(result[0].similarity).toBe(0.9);
    expect(result[0].relevanceRank).toBe(1);
    expect(result[0].chunkIndex).toBe(0);
    expect(result[0].tokenCount).toBe(500);
    expect(result[0].passage).toBe('The court held...');
    expect(result[0].reasoning).toHaveLength(1);
    expect(result[0].reasoning[0].type).toBe('analysis');
    expect(result[0].logic.queryIntent).toBe('Find cases about negligence');
    expect(result[0].logic.matchingCriteria).toBe('Retrieved from CourtListener case law');
    expect(result[0].logic.application).toBe('Directly applicable');
    expect(result[0].notes).toBe('');
    expect(result[0].reviewedAt).toBeNull();
  });

  it('uses honest defaults when fields are missing — no fabricated content', () => {
    const raw = [{}];
    const result = parseCitations(raw);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('mock-id');
    expect(result[0].source).toBe('Source 1');
    expect(result[0].type).toBe('document');
    // No score from the server means no score here — never an invented one.
    expect(result[0].confidence).toBeUndefined();
    expect(result[0].status).toBe('pending');
    expect(result[0].similarity).toBeUndefined();
    expect(result[0].weakMatch).toBeUndefined();
    expect(result[0].relevanceRank).toBe(1);
    expect(result[0].chunkIndex).toBe(0);
    // No invented token counts or placeholder passages.
    expect(result[0].tokenCount).toBe(0);
    expect(result[0].passage).toBe('');
    expect(result[0].reasoning).toEqual([]);
  });

  it('describes matching honestly by source type and never invents application text', () => {
    const raw = [{}];
    const result = parseCitations(raw);
    expect(result[0].logic.queryIntent).toBe('');
    expect(result[0].logic.matchingCriteria).toBe('Semantic match from your documents');
    expect(result[0].logic.application).toBe('');
  });

  it('treats a zero score from the server as no score', () => {
    const result = parseCitations([{ confidence: 0, similarity: 0 }]);
    expect(result[0].confidence).toBeUndefined();
    expect(result[0].similarity).toBeUndefined();
  });

  it('flags passages the server retrieved below the similarity threshold', () => {
    const result = parseCitations([{
      confidence: 18,
      similarity: 0.18,
      reasoning: [{
        type: 'Relevance Ranking',
        description: 'Ranked #4',
        evidence: 'This passage scored 0.180 vector similarity — below your similarity threshold (weak match)',
      }],
    }]);
    expect(result[0].weakMatch).toBe(true);
    expect(result[0].similarity).toBe(0.18);
  });

  it('handles confidence values greater than 1 (percentage)', () => {
    const raw = [{ confidence: 85 }];
    const result = parseCitations(raw);
    expect(result[0].confidence).toBe(85);
  });

  it('caps confidence at 100', () => {
    const raw = [{ confidence: 150 }];
    const result = parseCitations(raw);
    expect(result[0].confidence).toBe(100);
  });

  it('handles multiple citations with correct indexing', () => {
    const raw = [{}, {}, {}];
    const result = parseCitations(raw);
    expect(result).toHaveLength(3);
    expect(result[0].source).toBe('Source 1');
    expect(result[1].source).toBe('Source 2');
    expect(result[2].source).toBe('Source 3');
    expect(result[0].relevanceRank).toBe(1);
    expect(result[1].relevanceRank).toBe(2);
    expect(result[2].relevanceRank).toBe(3);
  });

  it('handles empty reasoning array', () => {
    const raw = [{ reasoning: [] }];
    const result = parseCitations(raw);
    expect(result[0].reasoning).toEqual([]);
  });

  it('drops reasoning steps without a description', () => {
    const raw = [{ reasoning: [{ type: 'semantic' }, { type: 'holding', description: 'Match' }] }];
    const result = parseCitations(raw);
    expect(result[0].reasoning).toHaveLength(1);
    expect(result[0].reasoning[0].type).toBe('holding');
  });

  it('parses reasoning entries with missing evidence', () => {
    const raw = [{ reasoning: [{ type: 'semantic', description: 'Match' }] }];
    const result = parseCitations(raw);
    expect(result[0].reasoning[0].evidence).toBe('');
  });

  it('carries document and opinion anchors through', () => {
    const raw = [{ document_id: 'doc-9', url: 'https://example.com', opinion_id: '123' }];
    const result = parseCitations(raw);
    expect(result[0].document_id).toBe('doc-9');
    expect(result[0].url).toBe('https://example.com');
    expect(result[0].opinionId).toBe('123');
  });

  it('carries the explicit verification state and the support note', () => {
    const result = parseCitations([
      { verification: 'partial', support_note: 'Needs a Material Breach.', verified: true },
      { verification: 'not_checked' },
      { verification: 'made_up' },
      {},
    ]);
    expect(result[0].verification).toBe('partial');
    expect(result[0].supportNote).toBe('Needs a Material Breach.');
    expect(result[1].verification).toBe('not_checked');
    expect(result[1].supportNote).toBeNull();
    expect(result[2].verification).toBeUndefined();
    expect(result[3].verification).toBeUndefined();
  });

  it('carries the cluster id and the verified flag for case-law citations', () => {
    const raw = [
      { type: 'case_law', opinion_id: '123', cluster_id: '77', verified: true },
      { type: 'case_law', opinion_id: '124', cluster_id: null, verified: false },
      { type: 'document', verified: null },
    ];
    const result = parseCitations(raw);
    expect(result[0].clusterId).toBe('77');
    expect(result[0].verified).toBe(true);
    expect(result[1].clusterId).toBeNull();
    expect(result[1].verified).toBe(false);
    // null means "unknown" — never rendered as unverified.
    expect(result[2].verified).toBeUndefined();
  });

  it('handles empty input array', () => {
    expect(parseCitations([])).toEqual([]);
  });
});
