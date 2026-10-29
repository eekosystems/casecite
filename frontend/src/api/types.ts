// ==================== Branding Types ====================

/** Credentials of the shared public demo account; present only on a demo instance. */
export interface DemoLoginInfo {
  email: string;
  password: string;
}

export interface BrandingConfig {
  firm_name: string;
  logo_url: string | null;
  primary_color: string;
  secondary_color: string;
  accent_color: string;
  favicon_url: string | null;
  updated_at: string | null;
  demo_login?: DemoLoginInfo | null;
}

// ==================== API Response Types ====================

export interface AuthResponse {
  access_token: string;
  refresh_token?: string;
  user: UserInfo;
  /** Present when login requires a second factor instead of returning tokens. */
  mfa_required?: boolean;
  mfa_token?: string;
}

/** POST /auth/device/approve and /deny — the decision recorded for a device code. */
export interface DeviceDecision {
  status: 'approved' | 'denied';
  user_code: string;
}

export interface MfaStatus {
  enabled: boolean;
  recovery_codes_remaining: number;
}

export interface MfaSetup {
  secret: string;
  provisioning_uri: string;
}

export interface MfaRecoveryCodes {
  recovery_codes: string[];
}

export interface UserInfo {
  id: string;
  email: string;
  roles: string[]; // backend returns a list of role strings, e.g. ["admin"]
  name?: string;
  role?: string; // legacy single-role field (optional)
  /** True for an invited account still on its admin-issued temporary password:
      the API refuses everything except change-password / logout / me. */
  must_change_password?: boolean;
  mfa_enabled?: boolean;
  /** True when the firm requires two-factor authentication and this account
      has not enrolled yet. */
  mfa_enrollment_required?: boolean;
}

/** One active sign-in, as returned by GET /auth/sessions. */
export interface SessionInfo {
  created_at: string;
  last_activity: string;
  /** When this sign-in ends regardless of activity (absolute session lifetime). */
  expires_at?: string;
  ip_address: string;
}

export interface ChatResponse {
  id?: string;
  answer: string;
  content?: string;
  citations: Citation[];
  stats?: ChatStats;
  /** Persisted chat session this exchange belongs to (null if persistence failed). */
  session_id?: string | null;
  [key: string]: unknown;
}

interface Citation {
  text: string;
  source: string;
  page?: number;
  score?: number;
  document_id?: string | null;
  url?: string | null;
  /** CourtListener opinion id (case-law citations): open the source opinion
      with GET /tools/cases/{opinion_id}?kind=opinion. */
  opinion_id?: string | null;
  /** CourtListener cluster (case) id the opinion belongs to. */
  cluster_id?: string | null;
  /** True when the quoted passage was verified verbatim against the source;
      null/undefined when unknown (never false). */
  verified?: boolean | null;
  [key: string]: unknown;
}

interface ChatStats {
  retrieval_time?: number;
  generation_time?: number;
  total_time?: number;
  chunks_retrieved?: number;
  docs_searched?: number;
  processing_time?: string;
  case_law_searched?: number;
  case_law_included?: number;
  [key: string]: unknown;
}

export interface ChatQueryOptions {
  includeCaseLaw?: boolean;
  caseLawLimit?: number;
  includeDocuments?: boolean;
  documentFilter?: string | null;
  /** Persisted chat session to append this exchange to (null = start new). */
  sessionId?: string | null;
  [key: string]: unknown;
}

// ==================== Persistent Chat Session Types ====================

export interface ChatSessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  /** Who started the thread (a matter workspace lists colleagues' threads too). */
  user_id?: string | null;
  matter_id?: string | null;
}

export interface ChatSessionListResponse {
  sessions: ChatSessionSummary[];
}

export interface ChatSessionMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  citations: Record<string, unknown>[];
  strategy: StrategyBriefResponse | null;
  stats: Record<string, unknown> | null;
  created_at: string;
}

export interface ChatSessionDetail {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: ChatSessionMessage[];
}

export interface RAGSettings {
  vectorDb: string;
  indexName: string;
  topK: number;
  similarityThreshold: number;
  chunkSize: number;
  chunkOverlap: number;
  embeddingModel: string;
  dimensions: number;
  llmModel?: string;
  temperature?: number;
  maxTokens?: number;
  enableReranking?: boolean;
  hybridSearch?: boolean;
  citationVerification?: boolean;
  contextCompression?: boolean;
  queryExpansion?: boolean;
  sourceTracking?: boolean;
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
  [key: string]: unknown;
}

export interface DocumentInfo {
  id: string;
  name: string;
  size: number;
  status: string;
  [key: string]: unknown;
}

export interface DocumentTree {
  tree: Record<string, unknown>[];
  total_documents: number;
  [key: string]: unknown;
}

export interface DocumentContent {
  document_id: string;
  text: string;
  filename: string;
}

export interface ConnectorAuthUrl {
  url: string;
  auth_url?: string;
  connected?: boolean;
  [key: string]: unknown;
}

export interface ConnectorStatusResponse {
  connected: boolean;
  docs_indexed?: number;
  [key: string]: unknown;
}

export interface SyncStartResult {
  sync_id: string;
  status: string;
}

/** Why one file of a sync or picker import was not indexed. */
export interface ImportFileError {
  name: string;
  reason: string;
}

/** The completed job result of POST /connectors/{type}/sync. */
export interface SyncResult {
  status: string;
  docs_processed: number;
  docs_skipped: number;
  docs_total: number;
  /** Full tally of failures; `errors` lists at most the first 20. */
  errors_count: number;
  errors: ImportFileError[];
}

export interface SyncStatusResult {
  status: string;
  progress?: number;
  result?: SyncResult | null;
  error?: string;
  [key: string]: unknown;
}

export interface ConnectorDataResponse {
  connectors: Array<{
    id: string;
    connected?: boolean;
    configured?: boolean;
    admin_only?: boolean;
    /** Implemented but not validated against a live tenant; only listed when enabled. */
    experimental?: boolean;
    docs_indexed?: number;
  }>;
  [key: string]: unknown;
}

export interface ImportResponse {
  imported: number;
  failed: number;
  /** One entry per failed file (first 20); `failed` is the full count. */
  errors: ImportFileError[];
  message?: string;
  [key: string]: unknown;
}

export interface PickerConfig {
  provider?: string;
  token?: string;
  clientId?: string;
  appKey?: string;
  google_enabled: boolean;
  /** False when the Google Picker cannot open; `google_missing` names the
      absent settings fields (e.g. ["google_api_key"]) so the UI can explain. */
  google_configured?: boolean;
  google_missing?: string[];
  microsoft_enabled: boolean;
  box_enabled?: boolean;
  dropbox_enabled?: boolean;
  google_client_id?: string;
  /** The Picker developer key (browser-side, referrer-restricted API key);
      absent/null when not configured. */
  google_api_key?: string;
  google_app_id?: string;
  microsoft_client_id?: string;
  microsoft_tenant_id?: string;
  dropbox_app_key?: string;
  [key: string]: unknown;
}

export interface BoxPickerToken {
  access_token: string;
  expires_in: number;
}

export interface MessageResponse {
  message: string;
  [key: string]: unknown;
}

// ==================== Matter Strategy Types ====================

export interface StrategyBriefPoint {
  point?: string;
  step?: string;
  citation_ids: string[];
  case_refs?: string[];
}

interface StrategyBriefCitation {
  id: string;
  source: string;
  type: 'document' | 'case_law' | string;
  document_id?: string | null;
  passage?: string | null;
  confidence?: number;
  reference?: string | null;
  opinionId?: string | null;
  /** CourtListener cluster (case) id the opinion belongs to. */
  cluster_id?: string | null;
  url?: string | null;
  /** How this authority supports the point it is attached to (case_law only). */
  explanation?: string | null;
  /** True when the passage was verified verbatim against the real opinion text. */
  verified?: boolean;
}

export interface StrategyBriefResponse {
  question: string;
  scope: {
    /** First selected folder (legacy single-folder field). */
    folder_path?: string | null;
    /** Every folder the brief was scoped to (their union). */
    folder_paths?: string[];
    documents_considered: number;
    documents_total: number;
    /** Passages the synthesis actually read (the most relevant per document). */
    passages_read?: number;
    passages_per_document?: number;
    /** True when more documents were in scope than the brief could read. */
    truncated?: boolean;
    /** Server-written sentence stating exactly what was and was not read. */
    coverage_note?: string;
  };
  /** Case references the model offered that could not be verified and were removed. */
  case_law_removed?: string[];
  position: string;
  strengths: StrategyBriefPoint[];
  weaknesses: StrategyBriefPoint[];
  next_steps: StrategyBriefPoint[];
  citations: StrategyBriefCitation[];
  /** Outcome of the case-law support stage — null when case law wasn't requested. */
  case_law?: {
    requested: boolean;
    points_searched: number;
    /** Opinions whose full text was actually retrieved and judged. */
    opinions_read: number;
    /** Opinions too long to judge in full — only their opening windows were read. */
    partially_read?: number;
    attached: number;
    /** Candidates dropped because no usable opinion text could be retrieved. */
    unreadable?: number;
    /** Opinions read in full and judged not to support any proposition. */
    unsupportive?: number;
    /** Opinions the judge endorsed but whose quote never verified verbatim. */
    quote_unverified?: number;
    /** Candidates dropped by an upstream error while judging. */
    errors?: number;
    /** Points whose CourtListener search itself failed. */
    searches_failed?: number;
    /** The concrete failure (e.g. "HTTPStatusError: 429 …") when searches failed. */
    search_error?: string | null;
    /** True when the stage hit its time budget before finishing every point. */
    timed_out?: boolean;
  } | null;
}

/** POST /settings/reindex — per-document failures are listed, not hidden. */
export interface ReindexResult {
  status: string;
  reindexed: number;
  /** One "filename: reason" line per document that could not be re-indexed. */
  errors: string[];
  embedding_model?: string;
  message?: string;
}

// ==================== Document List Response Types ====================

interface RagDocumentItem {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  source: string;
  source_id?: string | null;
  status: 'pending' | 'processing' | 'indexed' | 'failed';
  chunk_count: number;
  created_at: string;
  indexed_at?: string | null;
  metadata?: Record<string, unknown>;
  folder_path?: string | null;
  /** Owner of the document (a matter member may see colleagues' documents). */
  user_id?: string;
  /** Matter the document is filed under; null = the owner's personal scope. */
  matter_id?: string | null;
}

// ==================== Matter (workspace / sharing) Types ====================

export interface MatterMember {
  user_id: string;
  email: string | null;
  name: string | null;
  role: 'owner' | 'member' | string;
}

export interface Matter {
  id: string;
  name: string;
  client_name: string | null;
  is_personal: boolean;
  owner_id: string;
  /** The caller's own role in this matter. */
  role: 'owner' | 'member' | 'none' | string;
  member_count: number;
  created_at: string;
}

export interface MatterDetail extends Matter {
  members: MatterMember[];
}

export interface MatterAnalysisItem {
  analysis_id: string;
  document_id: string | null;
  filename: string | null;
  contract_type: string | null;
  created_at: string | null;
  issues: number;
  user_id: string | null;
}

// ==================== Tabular review Types ====================

export type ReviewFieldType = 'text' | 'date' | 'money' | 'number' | 'boolean' | 'duration';

export interface ReviewFieldSpec {
  /** Stable slug the cells are stored under; the server derives it from the name when omitted. */
  key?: string | null;
  name: string;
  description?: string;
  type: ReviewFieldType;
}

export type ReviewCellStatus = 'extracted' | 'unverified' | 'not_found' | 'error' | string;

export type ReviewStatus = 'pending' | 'approved' | 'flagged';

/** One change to a cell: the model's value, an override, an approval, a flag or a clear. */
export interface ReviewCellHistoryEntry {
  at: string;
  by: string;
  kind: 'model' | 'override' | 'approve' | 'flag' | 'clear' | string;
  value?: string | null;
  status?: string | null;
}

export interface ReviewCell {
  document_id: string;
  field_key: string;
  value: string | null;
  quote: string | null;
  span_start: number | null;
  span_end: number | null;
  verified: boolean;
  status: ReviewCellStatus;
  error: string | null;
  model_used: string | null;
  updated_at: string | null;
  /** Human review layer: the override replaces the model's value when set. */
  override_value: string | null;
  effective_value: string | null;
  review_status: ReviewStatus | string;
  note: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  history: ReviewCellHistoryEntry[];
}

/** A change a reviewer makes to a cell; a field left out keeps its value. */
export interface ReviewCellReviewInput {
  override_value?: string | null;
  review_status?: ReviewStatus;
  note?: string | null;
}

export interface ReviewCellRef {
  document_id: string;
  field_key: string;
}

export interface ReviewSchemaTemplate {
  id: string;
  name: string;
  description: string | null;
  matter_id: string | null;
  owner_id: string | null;
  role: 'owner' | 'member' | 'builtin' | string;
  is_builtin: boolean;
  fields: ReviewFieldSpec[];
  created_at: string | null;
  updated_at: string | null;
}

export interface ReviewSchemaCreateInput {
  name: string;
  description?: string | null;
  matter_id?: string | null;
  fields: ReviewFieldSpec[];
}

export interface ReviewRun {
  id: string;
  table_id: string;
  runner_id: string;
  scope: { document_ids?: string[] | null; field_keys?: string[] | null };
  status: 'running' | 'completed' | 'failed' | 'cancelled' | string;
  job_id: string | null;
  counts: ReviewTableRunResult | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export type ReviewExportFormat = 'xlsx' | 'csv' | 'json';

export interface ReviewExportOptions {
  format: ReviewExportFormat;
  /** Field keys to include; empty = all. */
  fields?: string[];
  reviewed_only?: boolean;
  include_quotes?: boolean;
  /** Extraction or review statuses to include; empty = all. */
  statuses?: string[];
}

export interface ReviewDocumentText {
  document_id: string;
  filename: string;
  text: string;
  length: number;
}

export interface ReviewTableDocument {
  id: string;
  filename: string;
  /** False when the caller can no longer read the document (left the matter, deleted). */
  accessible: boolean;
}

export interface ReviewTableSummary {
  id: string;
  name: string;
  description: string | null;
  matter_id: string | null;
  owner_id: string;
  /** The caller's role: owner edits and deletes; a matter member reads, runs and exports. */
  role: 'owner' | 'member' | string;
  field_count: number;
  document_count: number;
  cell_count: number;
  approved_count: number;
  flagged_count: number;
  last_run_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface ReviewTableDetail extends ReviewTableSummary {
  fields: ReviewFieldSpec[];
  documents: ReviewTableDocument[];
  cells: ReviewCell[];
}

export interface ReviewTableEstimate {
  documents: number;
  fields: number;
  cells: number;
  llm_calls: number;
  approx_input_tokens: number;
  model: string;
}

export interface ReviewTableRunStarted {
  job_id: string;
  poll_url: string;
  documents: number;
  fields: number;
  run_id?: string | null;
}

export interface ReviewTableDocumentsAdded {
  added: string[];
  table: ReviewTableDetail;
  run: ReviewTableRunStarted | null;
}

export interface ReviewTableRunResult {
  table_id?: string;
  documents: number;
  fields?: number;
  cells: number;
  extracted?: number;
  unverified?: number;
  not_found?: number;
  errors?: number;
  /** Cells whose model value differed from the previous run. */
  changed?: number;
}

export interface ReviewTableJob {
  job_id: string;
  status: string; // pending | processing | completed | failed | cancelled
  result?: ReviewTableRunResult | null;
  error?: string | null;
  progress?: { message: string; fraction: number | null } | null;
}

export interface ReviewTableCreateInput {
  name: string;
  description?: string | null;
  matter_id?: string | null;
  fields: ReviewFieldSpec[];
  document_ids?: string[];
}

export interface ReviewTableUpdateInput {
  name?: string;
  description?: string | null;
  fields?: ReviewFieldSpec[];
  document_ids?: string[];
}

// ==================== Deep Research Types ====================

export type DeepResearchTool = 'documents' | 'case_law';

export interface DeepResearchSubQuestion {
  id: string;
  question: string;
  tools: DeepResearchTool[];
}

/** The Research file-workspace pick, as the deep-research endpoints accept it. */
export interface DeepResearchDocumentFilter {
  document_ids?: string[] | null;
  folder_paths?: string[] | null;
  sources?: string[] | null;
  doc_types?: string[] | null;
  include_subfolders?: boolean;
  search_all?: boolean;
}

export interface DeepResearchScope {
  matter_id: string | null;
  /** null = every indexed document the caller can read. */
  document_ids: string[] | null;
  documents_in_scope: number;
  case_law_available: boolean;
  citation_check_available: boolean;
}

export interface DeepResearchPlanInput {
  question: string;
  matter_id?: string | null;
  document_filter?: DeepResearchDocumentFilter | null;
}

export interface DeepResearchPlanResponse {
  question: string;
  plan: DeepResearchSubQuestion[];
  scope: DeepResearchScope;
  caps: Record<string, number>;
  model: string;
}

export interface DeepResearchRunInput extends DeepResearchPlanInput {
  plan: DeepResearchSubQuestion[];
}

export interface DeepResearchRunStarted {
  run_id: string;
  job_id: string;
  poll_url: string;
  sub_questions: number;
}

export interface DeepResearchToolCall {
  tool: string;
  query: string;
  passages: number;
  error?: string;
}

export interface DeepResearchClaim {
  text: string;
  refs: string[];
  quote: string | null;
  /** The quote was located, character for character, in a cited passage. */
  verified: boolean;
  quote_ref: string | null;
}

export interface DeepResearchStep {
  id: string;
  question: string;
  tools: string[];
  status: 'pending' | 'completed' | 'failed' | string;
  tool_calls: DeepResearchToolCall[];
  passages_found: number;
  followup_query: string | null;
  answer: string;
  claims: DeepResearchClaim[];
  quotes_verified: number;
  quotes_unverified: number;
  invalid_refs_stripped: number;
  error: string | null;
  duration_ms: number;
}

export interface DeepResearchTreatment {
  status: 'checked' | 'unavailable' | 'error' | string;
  warning_level?: string;
  is_good_law?: boolean | null;
  case_name?: string;
  url?: string | null;
  negative?: number;
  caution?: number;
  detail?: string;
}

export interface DeepResearchSource {
  ref: string;
  type: 'document' | 'case_law' | string;
  title: string;
  snippet: string;
  excerpt_source: 'index' | 'opinion' | 'snippet' | string;
  document_id: string | null;
  chunk_index: number | null;
  opinion_id: string | null;
  cluster_id: string | null;
  citation: string | null;
  url: string | null;
  court: string | null;
  date_filed: string | null;
  score: number | null;
  sub_question_ids: string[];
  quotes_verified: number;
  treatment?: DeepResearchTreatment;
}

export interface DeepResearchResultScope {
  matter_id: string | null;
  document_ids: string[] | null;
  documents_in_scope: number | null;
  case_law_searched: boolean;
  courtlistener_contacted: boolean;
  tool_calls: number;
  tool_call_cap: number;
  passages_collected: number;
  passages_dropped_over_cap: number;
  passages_cited: number;
  steps_failed: number;
  coverage_note: string;
}

export interface DeepResearchVerification {
  quotes_verified: number;
  quotes_unverified: number;
  invalid_refs_stripped: number;
  case_references_removed: string[];
  citations_checked: number;
}

export interface DeepResearchResult {
  run_id?: string;
  question: string;
  answer: string;
  key_findings: { text: string; refs: string[] }[];
  open_questions: string[];
  sub_questions: DeepResearchStep[];
  sources: DeepResearchSource[];
  scope: DeepResearchResultScope;
  verification: DeepResearchVerification;
  model: string;
  notice: string;
}

export interface DeepResearchRunSummary {
  id: string;
  owner_id: string;
  matter_id: string | null;
  role: 'owner' | 'member' | string;
  question: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled' | string;
  sub_questions: number;
  sources_cited: number | null;
  created_at: string;
  completed_at: string | null;
}

export interface DeepResearchRunDetail extends DeepResearchRunSummary {
  plan: DeepResearchSubQuestion[];
  scope: { matter_id: string | null; document_ids: string[] | null; documents_in_scope: number };
  job_id: string | null;
  result: DeepResearchResult | null;
  error: string | null;
}

export interface DeepResearchJob {
  job_id: string;
  status: string; // pending | processing | completed | failed | cancelled
  result?: DeepResearchResult | null;
  error?: string | null;
  progress?: { message: string; fraction: number | null } | null;
}

// ==================== Workflow Types ====================

export type WorkflowStepKind =
  | 'extract_fields'
  | 'analyze_contract'
  | 'compare_contracts'
  | 'draft_document'
  | 'deep_research'
  | 'citation_check'
  | 'approval';

export type WorkflowInputSource = 'run' | 'documents' | 'step';

export interface WorkflowStepInputs {
  source: WorkflowInputSource;
  document_ids: string[];
  step: string | null;
}

export interface ExtractFieldsParams {
  fields: ReviewFieldSpec[];
  table_name?: string | null;
}

export interface AnalyzeContractParams {
  contract_type?: string;
  jurisdiction?: string | null;
  representing?: string | null;
  posture?: 'strict' | 'balanced';
  instructions?: string | null;
}

export interface CompareContractsParams {
  /** Where the baseline comes from: the first input, or the fixed document below. */
  baseline?: 'first_input' | 'document';
  baseline_document_id?: string | null;
  focus?: string | null;
}

export type WorkflowApprovers = 'owner' | 'any_member' | string[];

export interface ApprovalParams {
  title: string;
  instructions?: string | null;
  approvers: WorkflowApprovers;
}

export type WorkflowConditionOp =
  | 'exists'
  | 'not_exists'
  | 'equals'
  | 'not_equals'
  | 'contains'
  | 'gt'
  | 'lt'
  | 'gte'
  | 'lte'
  | 'count_gt'
  | 'count_eq';

/** Run a step only when an earlier step's output satisfies the rule. */
export interface WorkflowCondition {
  step: string;
  /** Dotted path into the earlier output (`data.analyses.0.issues`); empty = the whole output. */
  path: string;
  op: WorkflowConditionOp;
  value: string | number | boolean | null;
}

export interface DraftDocumentParams {
  instructions: string;
  include_outputs_from?: string[];
  reference_document_ids?: string[];
}

export interface DeepResearchParams {
  question: string;
  tools?: DeepResearchTool[] | null;
}

export interface CitationCheckParams {
  citations?: string[];
  from_steps?: string[] | null;
}

export type WorkflowStepParams =
  | ExtractFieldsParams
  | AnalyzeContractParams
  | CompareContractsParams
  | DraftDocumentParams
  | DeepResearchParams
  | CitationCheckParams
  | ApprovalParams;

export interface WorkflowStep {
  name: string;
  kind: WorkflowStepKind;
  params: Record<string, unknown>;
  inputs: WorkflowStepInputs;
  condition?: WorkflowCondition | null;
}

/** A document a workflow or run names, with the name the UI shows for it. */
export interface WorkflowDocumentRef {
  id: string;
  filename: string | null;
  /** False when the caller can no longer read it. */
  accessible: boolean;
}

export interface WorkflowSummary {
  id: string;
  name: string;
  description: string | null;
  matter_id: string | null;
  owner_id: string;
  /** The caller's role: owner edits and deletes; a matter member reads and runs. */
  role: 'owner' | 'member' | string;
  template_key: string | null;
  /** Incremented every time the steps change. */
  version: number;
  archived_at: string | null;
  step_count: number;
  step_kinds: WorkflowStepKind[];
  run_count: number;
  last_run_at: string | null;
  last_run_status: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface WorkflowDetail extends WorkflowSummary {
  steps: WorkflowStep[];
  /** Every document the steps name (fixed inputs, baselines, references). */
  documents: WorkflowDocumentRef[];
}

export interface WorkflowStepIssue {
  /** -1 for a workflow-level problem. */
  index: number;
  field: string;
  message: string;
}

export interface WorkflowValidateResponse {
  valid: boolean;
  issues: WorkflowStepIssue[];
}

export interface WorkflowVersionSummary {
  version: number;
  name: string;
  description: string | null;
  step_count: number;
  step_names: string[];
  created_by: string;
  created_at: string;
  change_note: string | null;
}

export interface WorkflowVersionDetail extends WorkflowVersionSummary {
  steps: WorkflowStep[];
}

export interface WorkflowVersionList {
  current: number;
  versions: WorkflowVersionSummary[];
}

export interface WorkflowTemplate {
  key: string;
  name: string;
  description: string;
  steps: WorkflowStep[];
}

export interface WorkflowCreateInput {
  name: string;
  description?: string | null;
  matter_id?: string | null;
  steps: WorkflowStep[];
}

export interface WorkflowUpdateInput {
  name?: string;
  description?: string | null;
  steps?: WorkflowStep[];
  /** Kept on the version a step change records. */
  change_note?: string | null;
}

export interface WorkflowRunStarted {
  run_id: string;
  job_id: string;
  poll_url: string;
  steps: number;
  documents: number;
}

export type WorkflowStepStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'skipped_condition'
  | 'awaiting'
  | 'rejected'
  | string;

export interface WorkflowStepOutput {
  summary: string;
  data: Record<string, unknown>;
  document_ids: string[];
  produced: Record<string, string[]>;
  /** Set when the stored output was cut to the server's size cap. */
  truncated?: boolean;
  truncated_fields?: string[];
}

export interface WorkflowStepRun {
  index: number;
  name: string;
  kind: WorkflowStepKind | string;
  status: WorkflowStepStatus;
  output: WorkflowStepOutput | null;
  input_document_ids: string[];
  produced_ids: Record<string, string[]>;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export type WorkflowRunStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'awaiting_approval'
  | 'rejected'
  | string;

export interface WorkflowRunSummary {
  id: string;
  workflow_id: string;
  /** The workflow's name at read time, for lists that span workflows. */
  workflow_name: string | null;
  workflow_version: number;
  runner_id: string;
  matter_id: string | null;
  status: WorkflowRunStatus;
  steps: number;
  completed_steps: number;
  documents: number;
  job_id: string | null;
  error: string | null;
  resumed_count: number;
  /** While awaiting approval: the step waiting and who may decide it. */
  awaiting_step: string | null;
  approver_ids: string[];
  can_approve: boolean;
  created_at: string;
  completed_at: string | null;
}

export interface WorkflowRunDetail extends WorkflowRunSummary {
  step_definitions: WorkflowStep[];
  step_runs: WorkflowStepRun[];
  input_document_ids: string[];
  /** Names for every document the run read. */
  document_refs: WorkflowDocumentRef[];
}

export interface WorkflowRunResult {
  run_id: string;
  /** completed, or awaiting_approval when the job ended at an approval step. */
  status: string;
  steps: number;
  completed_steps: number;
  documents: number;
  produced: Record<string, number>;
  awaiting_step?: string;
}

export interface WorkflowJob {
  job_id: string;
  status: string; // pending | processing | completed | failed | cancelled
  result?: WorkflowRunResult | null;
  error?: string | null;
  progress?: { message: string; fraction: number | null } | null;
}

export interface MatterActivityEvent {
  id: number;
  timestamp: string;
  event_type: string;
  action: string | null;
  user_id: string | null;
  user_email: string | null;
  resource_type: string | null;
  resource_id: string | null;
  success: boolean;
}

export interface DocumentListResponse {
  documents: RagDocumentItem[];
  /** Length of this page. */
  count: number;
  /** Documents matching the filters across ALL pages (for paging). */
  total: number;
  limit?: number | null;
  offset?: number;
  [key: string]: unknown;
}

// ==================== Admin Integrations Types ====================

export interface CourtListenerStatus {
  configured: boolean;
  source: 'instance' | 'env' | 'none' | string;
  masked: string | null;
}

/** Per-provider file-picker credentials status (instance DB beats .env). */
export interface ConnectorCredentialStatus {
  provider: string;
  label: string;
  configured: boolean;
  source: 'instance' | 'env' | 'none' | string;
  fields: string[];
  masked: Record<string, string | null>;
}

export interface LocalLlmStatus {
  configured: boolean;
  base_url: string | null;
  chat_model: string | null;
  utility_model: string | null;
}

export interface LocalLlmConfig {
  baseUrl: string;
  chatModel: string;
  utilityModel?: string | null;
}

// ==================== Admin User Management Types ====================

export interface AdminUser {
  id: string;
  email: string;
  name: string;
  roles: string[];
  is_active: boolean;
  created_at?: string | null;
  last_login?: string | null;
  mfa_enabled?: boolean;
  /** Still on the admin-issued temporary password. */
  must_change_password?: boolean;
}

export interface InviteUserResult {
  user: AdminUser;
  temporary_password: string;
}

interface PermissionInfo {
  key: string;
  label: string;
  description: string;
  group: string;
}

/** One tamper-evident audit entry. The server returns the stored record as-is
    (emails and IPs are partially masked in the trail by design). */
export interface AuditLogEntry {
  id: string;
  timestamp: string;
  event_type: string;
  user?: { id?: string | null; email?: string | null } | null;
  resource?: { type?: string | null; id?: string | null } | null;
  action?: Record<string, unknown> | null;
  context?: { ip_address?: string | null; user_agent?: string | null; environment?: string | null } | null;
  outcome?: { success?: boolean; error?: string | null } | null;
  integrity?: { previous_hash?: string | null; entry_hash?: string | null; hash_version?: number | null } | null;
}

export interface AuditLogQuery {
  startDate?: string;
  endDate?: string;
  eventType?: string;
  userId?: string;
  limit?: number;
}

export interface AuditVerifyResult {
  valid: boolean;
  entries_checked: number;
  first_invalid_id?: string | null;
  /** Why verification failed, when it did. */
  reason?: string | null;
  /** Entries written before chain linkage was enforced (signature-checked only). */
  legacy_entries?: number;
  /** True when the range held more entries than one verification scans. */
  truncated?: boolean;
}

export interface RolePermissionsMatrix {
  permissions: PermissionInfo[];
  roles: string[];
  defaults: Record<string, string[]>;
  assigned: Record<string, string[]>;
  customized: string[];
  locked: Record<string, string[]>;
}

// ==================== Universal History (workspace sessions) ====================

export interface WorkspaceSessionSummary {
  id: string;
  surface: string;
  title: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WorkspaceSessionDetail extends WorkspaceSessionSummary {
  payload: Record<string, unknown>;
}

// ==================== API Client Interface ====================

export interface RequestOptions extends RequestInit {
  headers?: Record<string, string>;
  /** Abort after this many ms (request: 30s default, authFetch: 120s). */
  timeout?: number;
  _retried?: boolean;
}

interface DocAnchorReasoningStep {
  type?: string | null;
  description?: string | null;
  evidence?: string | null;
}

interface DocAnchorReasoning {
  steps?: DocAnchorReasoningStep[] | null;
  application?: string | null;
}

/** A claim anchored to a character span of one of the user's documents, with the
    verbatim-verified quote that supports it. Drawn in the opened-document view
    and traceable from the citation modal. */
export interface DocAnchor {
  id?: number;
  proposition: string;
  doc_quote: string | null;
  doc_span_start: number | null;
  doc_span_end: number | null;
  source: 'internal' | 'courtlistener' | string;
  case_name: string | null;
  citation: string | null;
  source_ref: string | null;
  source_url: string | null;
  support_quote: string | null;
  source_span_start: number | null;
  source_span_end: number | null;
  verified: boolean;
  relevance: number;
  note: string | null;
  reasoning?: DocAnchorReasoning | null;
}

/** GET /tools/cases/{id} — a case with its readable opinion text. */
export interface CaseDetail {
  /** The id as requested. */
  id: number;
  /** How the id was read: as a 'cluster' or as an 'opinion'. */
  resolved_as?: 'cluster' | 'opinion' | null;
  cluster_id?: number | null;
  /** The lead opinion's id. */
  opinion_id?: number | null;
  case_name?: string;
  opinion_text?: string;
  syllabus?: string;
  [k: string]: unknown;
}

// ==================== Contract Analysis Types ====================

export interface ContractKeyTerm {
  value: string;
  quote: string | null;
  span_start: number | null;
  span_end: number | null;
  verified: boolean;
}

type ContractIssueGrounding = 'span' | 'absence' | 'unverified';

export interface ContractIssue {
  ref: string;
  title: string;
  status: string;
  severity: string; // critical | major | minor | informational
  why: string;
  suggested_language?: string | null;
  span_start?: number | null;
  span_end?: number | null;
  matched_text?: string | null;
  clause_slug?: string | null;
  grounding: ContractIssueGrounding;
}

interface ContractParty {
  canonical_name: string;
  role: string | null;
  aliases: string[];
  confidence: number;
}

export interface ContractObligation {
  subject_party: string | null;
  modal: string;
  action: string;
  object_text: string | null;
  category: string | null;
  // Present on live analyze results; stored analyses return conditions instead.
  deadlines?: string[];
  conditions?: string[];
  matched_text: string;
  span_start: number | null;
  span_end: number | null;
}

export interface ContractDeadlineItem {
  kind: string; // absolute | relative_offset | period | recurring | conditional
  description: string;
  anchor: string | null;
  offset_days: number | null;
  period_days: number | null;
  resolved_date: string | null;
  matched_text: string | null;
}

interface ContractDefinedTerm {
  term: string;
  definition_text: string | null;
  defined: boolean;
  usage_count: number;
  used_but_undefined: boolean;
  defined_but_unused: boolean;
  circular_reference: boolean;
}

interface ContractAnalysisSummary {
  parties?: number;
  obligations?: number;
  deadlines?: number;
  [k: string]: unknown;
}

export interface ContractAnalysisResult {
  analysis_id: string;
  contract_type: string; // nda | msa | saas | employment | license | sow | other
  representing?: string | null;
  posture?: string | null;
  jurisdiction?: string | null;
  executive_summary?: string | null;
  key_terms: Record<string, ContractKeyTerm>;
  issues: ContractIssue[];
  parties: ContractParty[];
  obligations: ContractObligation[];
  deadlines: ContractDeadlineItem[];
  defined_terms?: ContractDefinedTerm[];
  summary: ContractAnalysisSummary;
}

/** One proposed tracked-changes edit inside a redline result. */
export interface ContractRedlineEdit {
  /** Where this proposal came from — the rule/standard that flagged it. */
  source?: string;
  ref: string;
  kind: 'replace' | 'insert';
  span_start: number;
  span_end: number;
  original_text: string;
  proposed_text: string;
  rationale: string;
  severity: string; // critical | major | minor | informational
  title: string;
}

/** Redline job result — a full analysis plus the proposed edits. */
interface ContractRedlineResult extends ContractAnalysisResult {
  kind: 'redlines';
  redlines: ContractRedlineEdit[];
}

/** Draft job result — a freshly drafted (or revised) document. */
export interface ContractDraft {
  kind: 'draft';
  /** Empty on a scoped revision: the workspace keeps its existing title. */
  title: string;
  text: string;
  /** Long drafts only: each section as drafted, before the reconcile pass. */
  sections?: Array<{ number: number; title: string; text: string }>;
  /** Long drafts only: what the reconcile pass changed (or why it was skipped). */
  reconcile_notes?: string[];
  /** "full" when every section read the whole reference bundle, "excerpts" when it had to be focused per section. */
  references_mode?: 'full' | 'excerpts' | 'none';
  /** Revisions only: which sections were rewritten, or "all". */
  revised_sections?: number[] | 'all';
  /** Case references the server could not verify and removed from the draft. */
  case_law_removed?: string[];
}

/** One planned section of a long draft. The user can edit every field. */
export interface DraftPlanSection {
  number: number;
  title: string;
  brief: string;
  /** What this section must leave to other sections — the overlap guard. */
  exclude: string;
  target_words: number;
}

/** The shared plan every section of a long draft is written against. */
export interface DraftPlan {
  title: string;
  document_type: string;
  sections: DraftPlanSection[];
  definitions: string;
  style_guide: string;
  target_words: number;
  references_mode?: 'full' | 'excerpts' | 'none';
}

/** Plan job result — reviewed and approved before any section is written. */
interface ContractDraftPlan {
  kind: 'draft_plan';
  plan: DraftPlan;
  target_pages: number;
}

/** 202 shape shared by the long-draft endpoints. */
export interface ContractJobStarted {
  type: string;
  job_id: string;
  poll_url?: string;
}

type ContractClauseCompareStatus = 'changed' | 'added' | 'removed' | 'unchanged';

export interface ContractComparisonClause {
  topic: string;
  status: ContractClauseCompareStatus;
  summary: string;
  quote_a: string | null;
  span_a_start: number | null;
  span_a_end: number | null;
  verified_a: boolean | null;
  quote_b: string | null;
  span_b_start: number | null;
  span_b_end: number | null;
  verified_b: boolean | null;
}

/** Compare job result — clause-by-clause diff of two contracts. */
export interface ContractComparison {
  kind: 'comparison';
  overall: string;
  label_a: string;
  label_b: string;
  clauses: ContractComparisonClause[];
  /** Indexed document ids, so the UI can open both texts side by side. */
  document_id_a?: string;
  document_id_b?: string;
  /** The user's own comparison instructions, if any. */
  focus?: string | null;
}

/** Multi-document compare (3-4 docs): baseline vs each other document. */
export interface ContractComparisonSet {
  kind: 'comparison_set';
  comparisons: ContractComparison[];
  focus?: string | null;
}

/**
 * Everything a contract job can resolve to. Plain analyses carry no `kind`
 * discriminant, so narrow with `'kind' in result` / `result.kind`.
 */
export type ContractJobResult =
  | ContractAnalysisResult
  | ContractRedlineResult
  | ContractDraft
  | ContractDraftPlan
  | ContractComparison
  | ContractComparisonSet;

export interface ContractAnalysisJob {
  job_id: string;
  status: string; // pending | processing | completed | failed | cancelled
  result?: ContractJobResult | null;
  error?: string | null;
  /** Reported by long-running pipelines while status is "processing". */
  progress?: { message: string; fraction: number | null } | null;
}

export interface ContractAnalysisListItem {
  analysis_id: string;
  document_id: string | null;
  contract_type: string | null;
  created_at: string | null;
  issues: number;
}

// ==================== Contract Chat Types ====================

export interface ContractChatCitation {
  quote: string;
  span_start: number;
  span_end: number;
}

/** 202 shape — the message asked for a full review; poll the job. */
interface ContractChatAnalysisStarted {
  type: 'analysis_started';
  job_id: string;
  poll_url?: string;
}

/** 202 shape — the message asked for redlines; poll the job. */
interface ContractChatRedlineStarted {
  type: 'redline_started';
  job_id: string;
  poll_url?: string;
}

/** 202 shape — the message asked for a fresh draft; poll the job. */
interface ContractChatDraftStarted {
  type: 'draft_started';
  job_id: string;
  poll_url?: string;
}

/** 202 shape — a two-document comparison kicked off; poll the job. */
export interface ContractCompareStarted {
  type: 'compare_started';
  job_id: string;
  poll_url?: string;
}

/** 200 shape — a direct answer with grounded citations. */
interface ContractChatAnswer {
  type: 'answer';
  answer: string;
  citations: ContractChatCitation[];
  /** Case references the server could not verify and removed from the answer. */
  case_law_removed?: string[];
}

interface ContractChatClarify {
  type: 'clarify';
  question: string;
  options: string[];
}

export type ContractChatResponse =
  | ContractChatAnalysisStarted
  | ContractChatRedlineStarted
  | ContractChatDraftStarted
  | ContractChatAnswer
  | ContractChatClarify;

// ==================== Conversation Export ====================

export type ConversationExportFormat = 'docx' | 'pdf' | 'md';

interface ConversationExportCitation {
  label: string;
  quote?: string | null;
  url?: string | null;
}

export interface ConversationExportMessage {
  role: 'user' | 'assistant';
  text: string;
  citations?: ConversationExportCitation[];
}

export interface ConversationExportPayload {
  title: string;
  format: ConversationExportFormat;
  messages: ConversationExportMessage[];
}

export interface ApiClient {
  token: string | null;

  // Token management (the refresh token is an httpOnly cookie — never in JS)
  setToken(token: string): void;
  getToken(): string | null;
  initCsrf(): Promise<void>;

  // Core request methods
  // Returns any: JSON deserialization boundary — domain modules type the return values
  request(endpoint: string, options?: RequestOptions): Promise<any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  refreshAccessToken(): Promise<boolean>;
  authFetch(url: string, options?: RequestOptions): Promise<Response>;

  // Branding
  getBranding(): Promise<BrandingConfig>;
  updateBranding(config: Partial<BrandingConfig>): Promise<BrandingConfig>;
  uploadBrandingLogo(file: File): Promise<{ status: string; logo_url: string }>;
  resetBranding(): Promise<BrandingConfig>;

  // Auth
  login(email: string, password: string): Promise<AuthResponse>;
  logout(): Promise<void>;
  getCurrentUser(): Promise<UserInfo>;
  register(email: string, name: string, password: string, company: string, bootstrapToken?: string): Promise<AuthResponse>;
  forgotPassword(email: string): Promise<MessageResponse>;
  verifyResetToken(token: string): Promise<{ valid: boolean }>;
  resetPassword(token: string, newPassword: string): Promise<MessageResponse>;
  changePassword(currentPassword: string, newPassword: string): Promise<{ status: string }>;
  getSessions(): Promise<{ sessions: SessionInfo[] }>;
  logoutAllSessions(): Promise<{ status: string }>;

  // MFA
  getMfaStatus(): Promise<MfaStatus>;
  setupMfa(): Promise<MfaSetup>;
  enableMfa(code: string): Promise<MfaRecoveryCodes>;
  disableMfa(code: string, password: string): Promise<{ status: string }>;
  verifyMfa(mfaToken: string, code: string): Promise<AuthResponse>;
  regenerateRecoveryCodes(code: string): Promise<MfaRecoveryCodes>;

  // Device-code sign-in (approving the Word add-in's code from the web app)
  approveDeviceCode(userCode: string): Promise<DeviceDecision>;
  denyDeviceCode(userCode: string): Promise<DeviceDecision>;

  // Chat
  query(message: string, mode: string, options?: ChatQueryOptions, signal?: AbortSignal): Promise<ChatResponse>;

  // Chat Sessions (persistent conversations)
  listChatSessions(): Promise<ChatSessionListResponse>;
  getChatSession(sessionId: string): Promise<ChatSessionDetail>;
  deleteChatSession(sessionId: string): Promise<{ status: string; id: string }>;

  // Settings
  getSettings(): Promise<RAGSettings>;
  updateSettings(settings: Record<string, unknown>): Promise<RAGSettings>;
  getKeyStatus(): Promise<Record<string, boolean>>;
  getMaskedKeys(): Promise<Record<string, string | null>>;
  saveApiKey(keyType: string, apiKey: string): Promise<void>;
  deleteApiKey(keyType: string): Promise<void>;
  getPromptDefaults(): Promise<Record<string, unknown>>;
  reindexDocuments(): Promise<ReindexResult>;

  // Admin Integrations (admin-only)
  getCourtListenerStatus(): Promise<CourtListenerStatus>;
  /** Response carries `status: "saved"` plus the same fields as the status GET. */
  setCourtListenerToken(apiToken: string): Promise<CourtListenerStatus & { status: string }>;
  /** Response carries `status: "cleared"` plus the effective (post-clear) status. */
  clearCourtListenerToken(): Promise<CourtListenerStatus & { status: string }>;
  getConnectorCredentials(): Promise<{ providers: ConnectorCredentialStatus[] }>;
  setConnectorCredentials(provider: string, values: Record<string, string>): Promise<ConnectorCredentialStatus>;
  clearConnectorCredentials(provider: string): Promise<ConnectorCredentialStatus>;
  getLocalLlmStatus(): Promise<LocalLlmStatus>;
  /** Response carries `status: "saved"` plus the same fields as the status GET (`configured: true`). */
  setLocalLlm(config: LocalLlmConfig): Promise<LocalLlmStatus & { status: string }>;
  /** Response carries `status: "cleared"` plus the effective (post-clear) status. */
  clearLocalLlm(): Promise<LocalLlmStatus & { status: string }>;

  // Universal History (workspace sessions)
  listWorkspaceSessions(): Promise<{ sessions: WorkspaceSessionSummary[] }>;
  createWorkspaceSession(surface: string, title: string, payload: Record<string, unknown>): Promise<WorkspaceSessionSummary>;
  updateWorkspaceSession(id: string, updates: { title?: string; payload?: Record<string, unknown> }): Promise<WorkspaceSessionSummary>;
  getWorkspaceSession(id: string): Promise<WorkspaceSessionDetail>;
  deleteWorkspaceSession(id: string): Promise<{ status: string; id: string }>;

  // Admin User Management (admin-only)
  getUsers(): Promise<{ users: AdminUser[] }>;
  inviteUser(email: string, name: string, role: string): Promise<InviteUserResult>;
  updateUserRole(userId: string, role: string): Promise<AdminUser>;
  getRolePermissions(): Promise<RolePermissionsMatrix>;
  updateRolePermissions(role: string, permissions: string[]): Promise<RolePermissionsMatrix>;
  resetRolePermissions(): Promise<RolePermissionsMatrix>;
  setUserActive(userId: string, isActive: boolean): Promise<AdminUser>;
  deleteUser(userId: string): Promise<unknown>;
  exportUserData(userId: string): Promise<Record<string, unknown>>;
  forceSignOutUser(userId: string): Promise<unknown>;
  resetUserMfa(userId: string): Promise<unknown>;

  // Audit trail (admin-only)
  getAuditLogs(query?: AuditLogQuery): Promise<{ logs: AuditLogEntry[]; count: number }>;
  verifyAuditChain(query?: Pick<AuditLogQuery, 'startDate' | 'endDate'>): Promise<AuditVerifyResult>;
  exportAuditLogs(query?: AuditLogQuery): Promise<Blob>;

  // Documents
  /** `metadata` is ignored — the server derives filename and type from the upload. */
  uploadDocument(file: File, metadata?: Record<string, string>): Promise<DocumentInfo>;
  getDocuments(limit?: number, offset?: number): Promise<DocumentListResponse>;
  /** Every document across all pages (walks `total` in pages of 200). */
  getAllDocuments(): Promise<DocumentListResponse['documents']>;
  getDocumentTree(): Promise<DocumentTree>;
  getDocumentContent(documentId: string): Promise<DocumentContent>;
  deleteDocument(documentId: string): Promise<MessageResponse>;
  listFolders(): Promise<{ folders: string[] }>;
  createFolder(name: string): Promise<{ path: string; folders: string[] }>;
  deleteFolder(path: string): Promise<{ status: string; folders: string[] }>;
  moveDocument(documentId: string, folderPath: string | null): Promise<{ status: string }>;

  // Matters (workspace / sharing)
  listMatters(): Promise<{ matters: Matter[] }>;
  createMatter(name: string, clientName?: string | null): Promise<Matter>;
  getMatter(matterId: string): Promise<MatterDetail>;
  updateMatter(matterId: string, changes: { name?: string; client_name?: string | null }): Promise<Matter>;
  deleteMatter(matterId: string): Promise<{ status: string; id: string }>;
  addMatterMember(matterId: string, email: string): Promise<MatterDetail>;
  removeMatterMember(matterId: string, userId: string): Promise<MatterDetail>;
  transferMatter(matterId: string, newOwnerId: string): Promise<MatterDetail>;
  getMatterDocuments(matterId: string): Promise<DocumentListResponse>;
  /** File documents under `matterId` (the caller's own, or ones filed under a matter they own). */
  moveDocumentsToMatter(matterId: string, documentIds: string[]): Promise<{ status: string; matter_id: string; moved: string[] }>;
  getMatterChats(matterId: string): Promise<ChatSessionListResponse>;
  getMatterAnalyses(matterId: string): Promise<{ analyses: MatterAnalysisItem[] }>;
  getMatterActivity(matterId: string, limit?: number): Promise<{ events: MatterActivityEvent[] }>;

  // Tabular review
  listReviewTables(): Promise<{ tables: ReviewTableSummary[] }>;
  createReviewTable(input: ReviewTableCreateInput): Promise<ReviewTableDetail>;
  getReviewTable(tableId: string): Promise<ReviewTableDetail>;
  updateReviewTable(tableId: string, changes: ReviewTableUpdateInput): Promise<ReviewTableDetail>;
  deleteReviewTable(tableId: string): Promise<{ status: string; id: string }>;
  estimateReviewTable(tableId: string): Promise<ReviewTableEstimate>;
  runReviewTable(tableId: string, scope?: { document_ids?: string[]; field_keys?: string[] }): Promise<ReviewTableRunStarted>;
  getReviewTableJob(jobId: string): Promise<ReviewTableJob>;
  exportReviewTable(tableId: string, options: ReviewExportOptions | ReviewExportFormat): Promise<Blob>;
  reviewCell(tableId: string, documentId: string, fieldKey: string, change: ReviewCellReviewInput): Promise<ReviewCell>;
  bulkReviewCells(tableId: string, cells: ReviewCellRef[], change: ReviewCellReviewInput): Promise<{ updated: number; cells: ReviewCell[] }>;
  listReviewSchemas(): Promise<{ schemas: ReviewSchemaTemplate[] }>;
  createReviewSchema(input: ReviewSchemaCreateInput): Promise<ReviewSchemaTemplate>;
  updateReviewSchema(schemaId: string, changes: Partial<ReviewSchemaCreateInput>): Promise<ReviewSchemaTemplate>;
  deleteReviewSchema(schemaId: string): Promise<{ status: string; id: string }>;
  addReviewDocuments(tableId: string, documentIds: string[], run?: boolean): Promise<ReviewTableDocumentsAdded>;
  removeReviewDocument(tableId: string, documentId: string): Promise<ReviewTableDetail>;
  getReviewDocumentText(tableId: string, documentId: string): Promise<ReviewDocumentText>;
  listReviewRuns(tableId: string, limit?: number): Promise<{ runs: ReviewRun[] }>;

  // Deep research
  planDeepResearch(input: DeepResearchPlanInput): Promise<DeepResearchPlanResponse>;
  runDeepResearch(input: DeepResearchRunInput): Promise<DeepResearchRunStarted>;
  listDeepResearch(matterId?: string | null, limit?: number): Promise<{ runs: DeepResearchRunSummary[] }>;
  getDeepResearch(runId: string): Promise<DeepResearchRunDetail>;
  deleteDeepResearch(runId: string): Promise<{ status: string; id: string }>;
  getDeepResearchJob(jobId: string): Promise<DeepResearchJob>;
  cancelDeepResearchJob(jobId: string): Promise<{ status: string; job_id: string }>;

  // Workflows
  listWorkflows(includeArchived?: boolean): Promise<{ workflows: WorkflowSummary[] }>;
  createWorkflow(input: WorkflowCreateInput): Promise<WorkflowDetail>;
  getWorkflow(workflowId: string): Promise<WorkflowDetail>;
  updateWorkflow(workflowId: string, changes: WorkflowUpdateInput): Promise<WorkflowDetail>;
  deleteWorkflow(workflowId: string): Promise<{ status: string; id: string }>;
  listWorkflowTemplates(): Promise<{ templates: WorkflowTemplate[] }>;
  createWorkflowFromTemplate(key: string, matterId?: string | null): Promise<WorkflowDetail>;
  /** Start a run (202 + a job to poll). With no ids and a matter-filed workflow, the matter's documents. */
  runWorkflow(workflowId: string, documentIds?: string[]): Promise<WorkflowRunStarted>;
  listWorkflowRuns(workflowId: string, limit?: number): Promise<{ runs: WorkflowRunSummary[] }>;
  getWorkflowRun(workflowId: string, runId: string): Promise<WorkflowRunDetail>;
  resumeWorkflowRun(workflowId: string, runId: string): Promise<WorkflowRunStarted>;
  getWorkflowJob(jobId: string): Promise<WorkflowJob>;
  cancelWorkflowJob(jobId: string): Promise<{ status: string; job_id: string }>;
  /** Check a step list without saving: every problem with its step and field. */
  validateWorkflowSteps(steps: WorkflowStep[]): Promise<WorkflowValidateResponse>;
  /** The newest runs across every workflow the caller can read, optionally one matter's. */
  listRecentWorkflowRuns(matterId?: string | null, limit?: number): Promise<{ runs: WorkflowRunSummary[] }>;
  duplicateWorkflow(workflowId: string, input?: { name?: string | null; matter_id?: string | null }): Promise<WorkflowDetail>;
  archiveWorkflow(workflowId: string): Promise<WorkflowDetail>;
  unarchiveWorkflow(workflowId: string): Promise<WorkflowDetail>;
  listWorkflowVersions(workflowId: string): Promise<WorkflowVersionList>;
  getWorkflowVersion(workflowId: string, version: number): Promise<WorkflowVersionDetail>;
  restoreWorkflowVersion(workflowId: string, version: number, changeNote?: string | null): Promise<WorkflowDetail>;
  approveWorkflowRun(workflowId: string, runId: string, note?: string | null): Promise<WorkflowRunStarted>;
  rejectWorkflowRun(workflowId: string, runId: string, note?: string | null): Promise<WorkflowRunDetail>;
  retryWorkflowStep(workflowId: string, runId: string, index: number): Promise<WorkflowRunStarted>;
  skipWorkflowStep(workflowId: string, runId: string, index: number): Promise<WorkflowRunStarted>;

  // Connectors
  getConnectors(): Promise<ConnectorDataResponse>;
  connectConnector(connectorId: string): Promise<ConnectorAuthUrl>;
  disconnectConnector(connectorId: string): Promise<MessageResponse>;
  /** The server refuses a sync with no scope: pass a folder, or ask for the whole account explicitly. */
  syncConnector(connectorId: string, scope: { folder_id: string } | { sync_all: true }): Promise<SyncStartResult>;
  getConnectorSyncStatus(connectorId: string, syncId: string): Promise<SyncStatusResult>;
  getConnectorStatus(connectorId: string): Promise<ConnectorStatusResponse>;

  // Pickers
  getPickerConfig(): Promise<PickerConfig>;
  getBoxPickerToken(): Promise<BoxPickerToken>;
  importFromGooglePicker(files: unknown[]): Promise<ImportResponse>;
  importFromOneDrivePicker(files: unknown[]): Promise<ImportResponse>;
  importFromBoxPicker(files: unknown[]): Promise<ImportResponse>;
  importFromDropboxChooser(files: Array<{ name: string; url?: string; size?: number }>): Promise<ImportResponse>;

  // Case law
  /** GET /tools/cases/{id}?kind= — pass 'opinion' for a citation's opinion_id,
      'cluster' for a search result id; omitted = cluster first, then opinion. */
  getCaseOpinion(opinionId: string, kind?: 'opinion' | 'cluster'): Promise<CaseDetail>;

  // Contract Analysis
  getContractJob(jobId: string): Promise<ContractAnalysisJob>;
  listContractAnalyses(): Promise<{ analyses: ContractAnalysisListItem[] }>;
  getContractAnalysis(id: string): Promise<ContractAnalysisResult>;
  /** Delete a stored analysis and everything derived from it (owner only). */
  deleteContractAnalysis(id: string): Promise<{ status: string; id: string }>;
  exportContractAnalysis(id: string, format: 'docx' | 'md'): Promise<Blob>;
  contractChat(documentId: string | null, message: string, mode?: string, draftText?: string, referenceIds?: string[]): Promise<ContractChatResponse>;
  generatePracticeProfile(practiceArea: string): Promise<{ practice_area: string; profile: string }>;
  compareContracts(documentIds: string[], focus?: string): Promise<ContractCompareStarted>;
  exportRedlines(analysisId: string, excludeRefs: string[], overrides?: Record<string, string>): Promise<Blob>;
  exportDraft(title: string, text: string): Promise<Blob>;
  planDraft(message: string, referenceIds: string[], targetPages: number): Promise<ContractJobStarted>;
  generateDraft(plan: DraftPlan, message: string, referenceIds: string[]): Promise<ContractJobStarted>;
  /** Render a chat transcript as Word / PDF / Markdown (POST /chat/export). */
  exportConversation(payload: ConversationExportPayload): Promise<Blob>;

}
