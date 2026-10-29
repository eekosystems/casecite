export interface RagSettings {
  vectorDb: string;
  indexName: string;
  embeddingModel: string;
  dimensions: number;
  chunkSize: number;
  chunkOverlap: number;
  similarityThreshold: number;
  topK: number;
  enableReranking: boolean;
  hybridSearch: boolean;
  citationVerification: boolean;
  contextCompression: boolean;
  queryExpansion: boolean;
  sourceTracking: boolean;
  llmModel: string;
  temperature: number;
  maxTokens: number;
  contract_playbook?: string | null;
  practice_area?: string | null;
  practice_profile?: string | null;
  custom_system_prompt?: string | null;
  custom_grounding_rules?: string | null;
  custom_factual_prompt?: string | null;
  custom_research_prompt?: string | null;
  custom_case_prompt?: string | null;
  custom_document_prompt?: string | null;
  custom_compliance_prompt?: string | null;
  custom_strategy_prompt?: string | null;
  /** Email when a workflow run you started or must approve ends or needs you. */
  workflow_email_notifications?: boolean;
  [key: string]: unknown;
}

export interface SystemStats {
  totalDocuments: number;
  totalEmbeddings: number;
  /** null = not checked yet, or the health check itself failed. */
  isHealthy: boolean | null;
}
