import { generateId } from './index';
import type { Citation, ReasoningStep, VerificationState } from '../types';

const VERIFICATION_STATES: ReadonlySet<string> = new Set([
  'supported', 'partial', 'contradicted', 'unsupported', 'quote_only', 'quote_unverified', 'not_checked',
]);

/** Phrase the backend writes into a citation's ranking step when the passage
 * scored below the user's similarity threshold (services/rag/citations.py). */
const WEAK_MATCH_MARKER = 'below your similarity threshold';

/** A score the server actually measured, or undefined — never a default.
 * The API sends 0 for "no score", so 0 is treated as absent. */
function measuredScore(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined;
}

/**
 * Parse raw citation data from the RAG API response into typed Citation objects.
 *
 * Reasoning/logic fields are populated only from what the backend actually
 * produced — never with invented placeholder text. `query` (when provided)
 * becomes the citation's query intent so the reasoning modal can show what
 * the source was matched against.
 */
export function parseCitations(rawCitations: unknown[], query?: string): Citation[] {
  return (rawCitations as Record<string, unknown>[]).map((cite, idx) => {
    const type = (cite.type as string) || 'document';
    const rawConfidence = measuredScore(cite.confidence);
    const rawReasoning = (cite.reasoning as Record<string, unknown>[]) || [];
    const weakMatch = cite.below_threshold === true
      || rawReasoning.some((r) => typeof r.evidence === 'string' && r.evidence.includes(WEAK_MATCH_MARKER));
    return {
      id: (cite.id as string) || generateId(),
      source: (cite.source as string) || `Source ${idx + 1}`,
      type,
      // Percent (0-100) when the server measured one; undefined otherwise.
      confidence: rawConfidence === undefined
        ? undefined
        : Math.min(100, Math.round(rawConfidence > 1 ? rawConfidence : rawConfidence * 100)),
      status: (cite.status as Citation['status']) || 'pending',
      similarity: measuredScore(cite.similarity),
      weakMatch: weakMatch || undefined,
      relevanceRank: (cite.relevance_rank as number) || idx + 1,
      chunkIndex: (cite.chunk_index as number) || idx,
      tokenCount: (cite.token_count as number) || 0,
      passage: (cite.passage as string) || '',
      caseSummary: (cite.case_summary as string) || null,
      reasoning: rawReasoning.map((r): ReasoningStep => ({
        type: (r.type as string) || 'Analysis',
        description: r.description as string,
        evidence: (r.evidence as string) || '',
      })).filter((r) => (r.description ?? '').trim()),
      logic: {
        // Prefer what the backend actually determined (claim grounding writes
        // real verification language here); fall back to generic provenance.
        queryIntent: ((cite.logic as Record<string, unknown>)?.query_intent as string) || query || '',
        matchingCriteria: ((cite.logic as Record<string, unknown>)?.matching_criteria as string)
          || (type === 'case_law'
            ? 'Retrieved from CourtListener case law'
            : 'Semantic match from your documents'),
        application: ((cite.logic as Record<string, unknown>)?.application as string)
          || (cite.application as string) || '',
      },
      notes: '',
      reviewedAt: null,
      document_id: (cite.document_id as string) || null,
      url: (cite.url as string) || null,
      opinionId: (cite.opinion_id as string) || (cite.opinionId as string) || null,
      clusterId: (cite.cluster_id as string) || (cite.clusterId as string) || null,
      // null from the server means "unknown" — never shown as unverified.
      verified: typeof cite.verified === 'boolean' ? (cite.verified as boolean) : undefined,
      // The explicit state; an unknown value is dropped rather than guessed at.
      verification: typeof cite.verification === 'string' && VERIFICATION_STATES.has(cite.verification)
        ? (cite.verification as VerificationState)
        : undefined,
      supportNote: typeof cite.support_note === 'string' && cite.support_note ? cite.support_note : null,
      docSpanStart: (cite.doc_span_start as number) ?? null,
      docSpanEnd: (cite.doc_span_end as number) ?? null,
    };
  });
}
