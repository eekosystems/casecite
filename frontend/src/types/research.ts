export interface ReasoningStep {
  type: string;
  description: string;
  evidence?: string | null;
}

export interface CitationLogic {
  queryIntent: string;
  matchingCriteria: string;
  application: string;
}

export interface Citation {
  id: string;
  source: string;
  type: string;
  /** Measured score as a percent (0-100); undefined when the server gave none. */
  confidence?: number;
  status: 'pending' | 'approved' | 'rejected';
  /** Measured retrieval similarity (0-1); undefined when the server gave none. */
  similarity?: number;
  /** Retrieved, but scored below the user's similarity threshold. */
  weakMatch?: boolean;
  relevanceRank: number;
  chunkIndex: number;
  tokenCount: number;
  passage: string;
  reference?: string;
  // Short summary of what the case is about and its holding (case-law
  // citations only; written by the pre-answer relevance judge).
  caseSummary?: string | null;
  reasoning: ReasoningStep[];
  logic: CitationLogic;
  notes: string;
  reviewedAt: Date | null;
  document_id?: string | null;
  url?: string | null;
  was_cited_by_ai?: boolean;
  // CourtListener opinion id — when set, the citation modal can fetch the full
  // opinion and anchor/highlight the cited passage for verification.
  opinionId?: string | null;
  // CourtListener cluster (case) id the opinion belongs to.
  clusterId?: string | null;
  verified?: boolean;
  // Which exchange produced this citation: the assistant message id (for
  // jump-to-answer anchoring) and the question it answered (the Sources
  // panel's per-question group label).
  messageId?: string;
  sourceQuery?: string;
  // Claim-grounded citations: exact char span of the quote in its source
  // document, for anchoring inside the opened file.
  docSpanStart?: number | null;
  docSpanEnd?: number | null;
}

export interface ChatStats {
  docsSearched: number;
  chunksRetrieved: number;
  processingTime: string;
  caseLawSearched?: number;
  caseLawIncluded?: number;
  queryIntent?: string | null;
}

export interface ChatMessage {
  id: string;
  type: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  stats?: ChatStats;
  mode?: string;
  timestamp: Date;
  isError?: boolean;
  /** Structured matter-strategy brief; rendered instead of plain content. */
  strategy?: import('../api/types').StrategyBriefResponse;
}

