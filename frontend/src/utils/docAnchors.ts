import type { DocAnchor } from '../api/types';
import type { Citation } from '../types/research';

/**
 * Map a document anchor (a claim with a verbatim-verified quote and its
 * character span in one of the user's files) into the app-wide Citation shape
 * so it flows through the Sources panel and the citation verification popup
 * exactly like any other citation.
 *
 * `idPrefix` keeps ids unique when anchors from several files land in one
 * conversation.
 */
export function anchorToCitation(m: DocAnchor, i: number, idPrefix = 'anchor'): Citation {
  const steps = (m.reasoning?.steps ?? [])
    .filter((step) => (step?.description ?? '').trim())
    .map((step) => ({
      type: step.type || 'Analysis',
      description: step.description as string,
      evidence: step.evidence ?? null,
    }));
  return {
    id: `${idPrefix}-${m.id ?? i}-${m.source_ref ?? i}`,
    source: m.case_name || m.citation || 'Authority',
    type: 'case_law',
    confidence: Math.round((m.relevance ?? 0) * 100),
    status: 'pending',
    similarity: m.relevance ?? 0,
    relevanceRank: i + 1,
    chunkIndex: 0,
    tokenCount: 0,
    passage: m.support_quote || '',
    reference: m.citation || undefined,
    reasoning: steps,
    logic: {
      queryIntent: m.proposition,
      matchingCriteria: m.verified
        ? 'Supporting passage verified verbatim in the source'
        : 'Unverified — supporting passage could not be located verbatim in the source',
      application: m.reasoning?.application || '',
    },
    notes: '',
    reviewedAt: null,
    document_id: null,
    url: m.source_url || null,
    was_cited_by_ai: true,
    opinionId: m.source_ref ?? null,
    verified: m.verified,
  };
}
