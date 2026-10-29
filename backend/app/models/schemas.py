import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


# Enums
class ConnectorType(str, Enum):
    GOOGLE_DRIVE = "google_drive"
    ONEDRIVE = "onedrive"
    BOX = "box"
    NETDOCUMENTS = "netdocuments"
    IMANAGE = "imanage"
    FILEVINE = "filevine"
    CLIO = "clio"
    DROPBOX = "dropbox"
    LOCAL = "local"
    # Picker-based imports (no OAuth verification required)
    GOOGLE_PICKER = "google_picker"
    ONEDRIVE_PICKER = "onedrive_picker"
    BOX_PICKER = "box_picker"
    DROPBOX_CHOOSER = "dropbox_chooser"


class DocumentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class CitationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AnalysisMode(str, Enum):
    RESEARCH = "research"
    CASE = "case"
    DOCUMENT = "document"
    COMPLIANCE = "compliance"
    STRATEGY = "strategy"


class QueryIntent(str, Enum):
    """Detected intent of a query - determines response verbosity."""

    FACTUAL = "factual"  # Simple questions: what, when, where, who, how many
    ANALYTICAL = "analytical"  # Complex analysis: strategy, compare, assess, recommend


# Request/Response Models
class DocumentFilter(BaseModel):
    """Filter for selecting which documents to search."""

    document_ids: list[str] | None = None  # Specific document IDs to include
    folder_paths: list[str] | None = None  # Folder paths to include (e.g., "/Cases/Smith v Jones/")
    sources: list[ConnectorType] | None = None  # Filter by source (google_drive, onedrive, etc.)
    doc_types: list[str] | None = None  # Filter by document type (contract, brief, memo, etc.)
    include_subfolders: bool = True  # Whether to include documents in subfolders
    search_all: bool = False  # If True, ignore other filters and search all documents


class CreateFolderRequest(BaseModel):
    """Create a knowledge-base folder."""

    name: str = Field(..., min_length=1, max_length=200)


class MoveDocumentRequest(BaseModel):
    """Move a document into a folder (null/empty = General/root)."""

    folder_path: str | None = None


class ChatRequest(BaseModel):
    """Request model for chat/RAG queries with validation."""

    query: str = Field(..., min_length=1, max_length=100000, description="The query text")
    mode: AnalysisMode = AnalysisMode.RESEARCH
    conversation_id: str | None = Field(None, max_length=100)
    # Persisted chat session to append this exchange to (None = start a new one).
    session_id: str | None = Field(None, max_length=100)
    include_citations: bool = True
    include_documents: bool = True  # Search uploaded documents (RAG)
    # None = decide automatically per message; explicit bool is respected.
    include_case_law: bool | None = None
    case_law_limit: int = Field(default=5, ge=1, le=50, description="Max case law results")
    document_filter: DocumentFilter | None = None  # Filter which documents to search

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        """Validate and sanitize query text."""
        v = v.strip()
        if not v:
            raise ValueError("Query cannot be empty")
        # Remove null bytes and control characters
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
        return v

    @field_validator("conversation_id", "session_id")
    @classmethod
    def validate_conversation_id(cls, v: str | None) -> str | None:
        """Validate conversation/session ID format."""
        if v is not None:
            v = v.strip()
            # Only allow alphanumeric, hyphens, and underscores
            if not re.match(r"^[a-zA-Z0-9_-]+$", v):
                raise ValueError("Invalid conversation ID format")
        return v


class StrategyBriefRequest(BaseModel):
    """Request model for a matter-level strategy brief with per-document coverage."""

    question: str = Field(..., min_length=1, max_length=10000, description="The strategy question")
    folder_path: str | None = Field(None, max_length=1000)  # Scope to a knowledge-base folder
    document_ids: list[str] | None = None  # Scope to specific documents
    include_case_law: bool = False  # Attach supporting case law to the strongest points

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        """Validate and sanitize the question text."""
        v = v.strip()
        if not v:
            raise ValueError("Question cannot be empty")
        # Remove null bytes and control characters
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
        return v


class ReasoningStep(BaseModel):
    type: str
    description: str
    evidence: str | None = None


class CitationLogic(BaseModel):
    query_intent: str
    matching_criteria: str
    application: str


class Citation(BaseModel):
    id: str
    source: str
    type: str
    confidence: float
    status: CitationStatus = CitationStatus.PENDING
    similarity: float
    relevance_rank: int
    chunk_index: int
    token_count: int
    passage: str
    reasoning: list[ReasoningStep]
    # One-sentence LLM explanation of how this source supports the final answer
    # (populated by the post-answer citation-reasoning pass; None if unavailable).
    application: str | None = None
    # Short summary of what the case is about and its holding (case-law
    # citations only; written by the pre-answer relevance judge).
    case_summary: str | None = None
    logic: CitationLogic
    notes: str | None = None
    reviewed_at: datetime | None = None
    document_id: str | None = None
    url: str | None = None  # Link to source (e.g., CourtListener URL)
    # CourtListener identifiers (case-law citations only). ``opinion_id`` is the
    # id of the opinion text that was read/verified; ``cluster_id`` the case it
    # belongs to. Carried so the UI can open the source opinion by the right
    # kind of id (see GET /tools/cases/{id}?kind=opinion).
    opinion_id: str | None = None
    cluster_id: str | None = None
    was_cited_by_ai: bool = False  # Whether AI actually referenced this in response
    # Claim-grounded citations: whether the quote verified verbatim against
    # the source file, and where it sits (char span) so the UI can anchor it
    # inside the opened document.
    verified: bool | None = None
    doc_span_start: int | None = None
    doc_span_end: int | None = None


class ChatStats(BaseModel):
    docs_searched: int
    chunks_retrieved: int
    processing_time: str
    case_law_searched: int = 0  # CourtListener cases found
    case_law_included: int = 0  # Cases included in context
    query_intent: str | None = None  # "factual" or "analytical" - detected intent


class ConversationExportCitation(BaseModel):
    label: str = Field(..., min_length=1, max_length=500)
    quote: str | None = Field(None, max_length=5000)
    url: str | None = Field(None, max_length=1000)


class ConversationExportMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field("", max_length=200_000)
    citations: list[ConversationExportCitation] = Field(default_factory=list, max_length=200)


class ConversationExportRequest(BaseModel):
    """A chat transcript (as displayed) to render as a downloadable file."""

    title: str = Field("Conversation", max_length=300)
    format: Literal["docx", "pdf", "md"] = "docx"
    messages: list[ConversationExportMessage] = Field(..., min_length=1, max_length=500)


class ChatResponse(BaseModel):
    id: str
    content: str
    citations: list[Citation]
    stats: ChatStats
    mode: AnalysisMode
    timestamp: datetime
    # Populated when the message routed to the full-coverage strategy brief.
    strategy: dict | None = None
    # Persisted chat session this exchange was appended to (None if persistence
    # failed — persistence is best-effort and never blocks the response).
    session_id: str | None = None


# Persistent Chat Session Models
class CreateChatSessionRequest(BaseModel):
    """Create a new persisted chat session."""

    title: str = Field(..., min_length=1, max_length=255)
    # Optional matter to file the session under (caller must be a member).
    # Omitted -> the caller's personal (owner-only) scope.
    matter_id: str | None = Field(None, max_length=36)


class ChatSessionSummary(BaseModel):
    """Session list item for the History panel."""

    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    # Who started the thread — a matter workspace lists colleagues' sessions
    # beside the caller's own, so the UI can label them.
    user_id: str | None = None
    matter_id: str | None = None


# ---- Matters (collaboration / sharing) ----


class CreateMatterRequest(BaseModel):
    """Create a shared matter."""

    name: str = Field(..., min_length=1, max_length=300)
    client_name: str | None = Field(None, max_length=300)


class AddMatterMemberRequest(BaseModel):
    """Invite a colleague to a matter by email."""

    email: str = Field(..., min_length=3, max_length=255)


class MatterMemberResponse(BaseModel):
    user_id: str
    email: str | None = None
    name: str | None = None
    role: str


class MatterResponse(BaseModel):
    id: str
    name: str
    client_name: str | None = None
    is_personal: bool
    owner_id: str
    role: str  # the caller's role in this matter: "owner" | "member"
    member_count: int
    created_at: datetime


class MatterListResponse(BaseModel):
    matters: list[MatterResponse]


class MatterDetailResponse(MatterResponse):
    members: list[MatterMemberResponse]


class UpdateMatterRequest(BaseModel):
    """Rename a matter or change its client name. Omitted fields keep their value."""

    name: str | None = Field(None, min_length=1, max_length=300)
    client_name: str | None = Field(None, max_length=300)


class TransferMatterRequest(BaseModel):
    """Hand a matter to another member, who becomes its owner."""

    new_owner_id: str = Field(..., min_length=1, max_length=36)


class MoveMatterDocumentsRequest(BaseModel):
    """File documents under the matter in the URL (bulk move between matters)."""

    document_ids: list[str] = Field(..., min_length=1, max_length=500)


class MoveMatterDocumentsResponse(BaseModel):
    status: str = "moved"
    matter_id: str
    moved: list[str]


class MatterAnalysisItem(BaseModel):
    """A contract analysis filed under a matter (the workspace Analyses tab)."""

    analysis_id: str
    document_id: str | None = None
    filename: str | None = None
    contract_type: str | None = None
    created_at: datetime | None = None
    issues: int = 0
    user_id: str | None = None


class MatterAnalysesResponse(BaseModel):
    analyses: list[MatterAnalysisItem]


class MatterActivityEvent(BaseModel):
    """One audit entry about a matter or something filed under it."""

    id: int
    timestamp: datetime
    event_type: str
    action: str | None = None
    user_id: str | None = None
    user_email: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    success: bool = True


class MatterActivityResponse(BaseModel):
    events: list[MatterActivityEvent]


# ---- Tabular review (schema-driven extraction across many documents) ----


class ReviewFieldType(str, Enum):
    TEXT = "text"
    DATE = "date"
    MONEY = "money"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DURATION = "duration"


_FIELD_KEY_RE = re.compile(r"[^a-z0-9]+")


def field_key_from_name(name: str) -> str:
    """Stable slug a field's cells are stored under: "Governing Law" -> "governing_law"."""
    key = _FIELD_KEY_RE.sub("_", name.strip().lower()).strip("_")
    return key[:80] or "field"


class ReviewFieldSpec(BaseModel):
    """One column of a review table. ``key`` is derived from ``name`` when omitted."""

    key: str | None = Field(None, min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=600)
    type: ReviewFieldType = ReviewFieldType.TEXT

    @model_validator(mode="after")
    def _fill_key(self) -> "ReviewFieldSpec":
        self.name = self.name.strip()
        self.description = self.description.strip()
        self.key = field_key_from_name(self.key or self.name)
        return self


def _unique_field_keys(fields: list[ReviewFieldSpec]) -> list[ReviewFieldSpec]:
    seen: set[str] = set()
    for f in fields:
        if f.key in seen:
            raise ValueError(f"Duplicate field: {f.key}")
        seen.add(f.key or "")
    return fields


class ReviewTableCreateRequest(BaseModel):
    """Create a review table. Documents may be given explicitly or, with a
    ``matter_id`` and no ids, default to every document filed under the matter."""

    name: str = Field(..., min_length=1, max_length=300)
    description: str | None = Field(None, max_length=2000)
    matter_id: str | None = Field(None, max_length=36)
    fields: list[ReviewFieldSpec] = Field(..., min_length=1, max_length=40)
    document_ids: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("fields")
    @classmethod
    def _keys_unique(cls, v: list[ReviewFieldSpec]) -> list[ReviewFieldSpec]:
        return _unique_field_keys(v)


class ReviewTableUpdateRequest(BaseModel):
    """Change the name, description, schema or document scope. Omitted fields keep their value."""

    name: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = Field(None, max_length=2000)
    fields: list[ReviewFieldSpec] | None = Field(None, min_length=1, max_length=40)
    document_ids: list[str] | None = Field(None, max_length=1000)

    @field_validator("fields")
    @classmethod
    def _keys_unique(cls, v: list[ReviewFieldSpec] | None) -> list[ReviewFieldSpec] | None:
        return _unique_field_keys(v) if v is not None else None


class ReviewTableRunRequest(BaseModel):
    """Run extraction over the whole table, or only the given documents / fields
    (a per-cell or per-column re-run)."""

    document_ids: list[str] | None = Field(default=None, max_length=1000)
    field_keys: list[str] | None = Field(default=None, max_length=40)


class ReviewCellResponse(BaseModel):
    document_id: str
    field_key: str
    value: str | None = None
    quote: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    verified: bool = False
    status: str
    error: str | None = None
    model_used: str | None = None
    updated_at: datetime | None = None
    # Human review layer. ``effective_value`` is the override when set, else ``value``.
    override_value: str | None = None
    effective_value: str | None = None
    review_status: str = "pending"
    note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)


ReviewStatus = Literal["pending", "approved", "flagged"]


class ReviewCellReviewRequest(BaseModel):
    """Override, approve, flag or annotate one cell. A field left out keeps its
    value; ``override_value: null`` clears the override."""

    override_value: str | None = Field(None, max_length=1000)
    review_status: ReviewStatus | None = None
    note: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _something_given(self) -> "ReviewCellReviewRequest":
        if not self.model_fields_set:
            raise ValueError("Nothing to change.")
        return self


class ReviewCellRef(BaseModel):
    document_id: str = Field(..., max_length=64)
    field_key: str = Field(..., max_length=80)


class ReviewCellBulkRequest(ReviewCellReviewRequest):
    """The same change applied to many cells at once."""

    cells: list[ReviewCellRef] = Field(..., min_length=1, max_length=5000)

    @model_validator(mode="after")
    def _something_given(self) -> "ReviewCellBulkRequest":
        if self.model_fields_set <= {"cells"}:
            raise ValueError("Nothing to change.")
        return self


class ReviewCellBulkResponse(BaseModel):
    updated: int
    cells: list[ReviewCellResponse]


class ReviewSchemaCreateRequest(BaseModel):
    """Save a field schema for reuse; with a matter it is shared with the matter."""

    name: str = Field(..., min_length=1, max_length=300)
    description: str | None = Field(None, max_length=2000)
    matter_id: str | None = Field(None, max_length=36)
    fields: list[ReviewFieldSpec] = Field(..., min_length=1, max_length=40)

    @field_validator("fields")
    @classmethod
    def _keys_unique(cls, v: list[ReviewFieldSpec]) -> list[ReviewFieldSpec]:
        return _unique_field_keys(v)


class ReviewSchemaUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = Field(None, max_length=2000)
    matter_id: str | None = Field(None, max_length=36)
    fields: list[ReviewFieldSpec] | None = Field(None, min_length=1, max_length=40)

    @field_validator("fields")
    @classmethod
    def _keys_unique(cls, v: list[ReviewFieldSpec] | None) -> list[ReviewFieldSpec] | None:
        return _unique_field_keys(v) if v is not None else None


class ReviewSchemaResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    matter_id: str | None = None
    owner_id: str | None = None
    role: str  # "owner" | "member" | "builtin"
    is_builtin: bool = False
    fields: list[ReviewFieldSpec]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReviewSchemaListResponse(BaseModel):
    schemas: list[ReviewSchemaResponse]


class ReviewRunResponse(BaseModel):
    id: str
    table_id: str
    runner_id: str
    scope: dict[str, Any] = Field(default_factory=dict)
    status: str
    job_id: str | None = None
    counts: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ReviewRunListResponse(BaseModel):
    runs: list[ReviewRunResponse]


class ReviewTableAddDocumentsRequest(BaseModel):
    """Add documents to a table's scope; ``run`` extracts just the new rows."""

    document_ids: list[str] = Field(..., min_length=1, max_length=1000)
    run: bool = False


class ReviewDocumentTextResponse(BaseModel):
    """The indexed text of one document in a table, exactly as the cells' offsets see it."""

    document_id: str
    filename: str
    text: str
    length: int


class ReviewTableDocument(BaseModel):
    """A document in the table's scope, as the grid labels it."""

    id: str
    filename: str
    accessible: bool = True


class ReviewTableSummary(BaseModel):
    id: str
    name: str
    description: str | None = None
    matter_id: str | None = None
    owner_id: str
    role: str  # the caller's role: "owner" | "member"
    field_count: int
    document_count: int
    cell_count: int = 0
    approved_count: int = 0
    flagged_count: int = 0
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class ReviewTableListResponse(BaseModel):
    tables: list[ReviewTableSummary]


class ReviewTableDetailResponse(ReviewTableSummary):
    fields: list[ReviewFieldSpec]
    documents: list[ReviewTableDocument]
    cells: list[ReviewCellResponse]


class ReviewTableEstimateResponse(BaseModel):
    """What a run would cost, before it is started."""

    documents: int
    fields: int
    cells: int
    llm_calls: int
    approx_input_tokens: int
    model: str


class ReviewTableRunStarted(BaseModel):
    job_id: str
    poll_url: str
    documents: int
    fields: int
    run_id: str | None = None


class ReviewTableDocumentsAdded(BaseModel):
    added: list[str]
    table: "ReviewTableDetailResponse"
    run: ReviewTableRunStarted | None = None


# ---- Deep research (planned, multi-step, source-verified research) ----


class DeepResearchTool(str, Enum):
    """The fixed tool set a sub-question may use. Nothing else is callable."""

    DOCUMENTS = "documents"  # semantic search over the firm's indexed documents
    CASE_LAW = "case_law"  # CourtListener opinion search (the cited passage is read in full)


class DeepResearchSubQuestion(BaseModel):
    """One step of a research plan, as proposed by the planner and edited by the user."""

    id: str = Field(..., min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")
    question: str = Field(..., min_length=3, max_length=1000)
    tools: list[DeepResearchTool] = Field(..., min_length=1, max_length=2)

    @model_validator(mode="after")
    def _tidy(self) -> "DeepResearchSubQuestion":
        self.question = self.question.strip()
        self.tools = list(dict.fromkeys(self.tools))
        return self


class DeepResearchPlanRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=4000)
    matter_id: str | None = None
    # An explicit document pick (from the Research file workspace); when given,
    # the firm-document tool searches only those documents.
    document_filter: DocumentFilter | None = None


class DeepResearchScope(BaseModel):
    """What a run may search: stated up front so the user sees it before running."""

    matter_id: str | None = None
    # None means every indexed document the caller can read.
    document_ids: list[str] | None = None
    documents_in_scope: int
    case_law_available: bool
    citation_check_available: bool


class DeepResearchPlanResponse(BaseModel):
    question: str
    plan: list[DeepResearchSubQuestion]
    scope: DeepResearchScope
    caps: dict[str, int]
    model: str


class DeepResearchRunRequest(DeepResearchPlanRequest):
    plan: list[DeepResearchSubQuestion] = Field(..., min_length=1, max_length=20)

    @field_validator("plan")
    @classmethod
    def _ids_unique(cls, v: list[DeepResearchSubQuestion]) -> list[DeepResearchSubQuestion]:
        seen: set[str] = set()
        for step in v:
            if step.id in seen:
                raise ValueError(f"Duplicate sub-question id: {step.id}")
            seen.add(step.id)
        return v


class DeepResearchRunStarted(BaseModel):
    run_id: str
    job_id: str
    poll_url: str
    sub_questions: int


class DeepResearchRunSummary(BaseModel):
    id: str
    owner_id: str
    matter_id: str | None
    role: str  # "owner" | "member"
    question: str
    status: str
    sub_questions: int
    sources_cited: int | None = None
    created_at: datetime
    completed_at: datetime | None


class DeepResearchRunDetail(DeepResearchRunSummary):
    plan: list[DeepResearchSubQuestion]
    scope: dict[str, Any]
    job_id: str | None
    result: dict[str, Any] | None
    error: str | None


class DeepResearchListResponse(BaseModel):
    runs: list[DeepResearchRunSummary]


# ---- Workflows (saved, repeatable sequences of the platform's own operations) ----


class WorkflowStepKind(str, Enum):
    """The operations a workflow step may run. Each maps to one existing service call."""

    EXTRACT_FIELDS = "extract_fields"  # tabular review over the step's input documents
    ANALYZE_CONTRACT = "analyze_contract"  # the full contract pipeline, per document
    COMPARE_CONTRACTS = "compare_contracts"  # clause-by-clause against a baseline
    DRAFT_DOCUMENT = "draft_document"  # a draft from instructions and prior outputs
    DEEP_RESEARCH = "deep_research"  # plan + run a deep-research question
    CITATION_CHECK = "citation_check"  # treatment scan of citations found or given
    APPROVAL = "approval"  # pause the run until a named approver decides


class WorkflowInputSource(str, Enum):
    """Where a step's documents come from."""

    RUN = "run"  # the documents the run was started with
    DOCUMENTS = "documents"  # explicit ids fixed in the workflow
    STEP = "step"  # the documents an earlier step produced or read


class WorkflowStepInputs(BaseModel):
    source: WorkflowInputSource = WorkflowInputSource.RUN
    document_ids: list[str] = Field(default_factory=list, max_length=1000)
    # The earlier step whose documents this step reads (source == "step").
    step: str | None = Field(None, max_length=60)

    @model_validator(mode="after")
    def _consistent(self) -> "WorkflowStepInputs":
        if self.source == WorkflowInputSource.DOCUMENTS and not self.document_ids:
            raise ValueError("inputs.document_ids is required when inputs.source is 'documents'")
        if self.source == WorkflowInputSource.STEP and not self.step:
            raise ValueError("inputs.step is required when inputs.source is 'step'")
        if self.source != WorkflowInputSource.DOCUMENTS:
            self.document_ids = []
        if self.source != WorkflowInputSource.STEP:
            self.step = None
        return self


class ExtractFieldsParams(BaseModel):
    """Tabular review: the fields to extract from every input document."""

    fields: list[ReviewFieldSpec] = Field(..., min_length=1, max_length=40)
    table_name: str | None = Field(None, max_length=300)

    @field_validator("fields")
    @classmethod
    def _keys_unique(cls, v: list[ReviewFieldSpec]) -> list[ReviewFieldSpec]:
        return _unique_field_keys(v)


class AnalyzeContractParams(BaseModel):
    contract_type: str = Field("auto", max_length=80)
    jurisdiction: str | None = Field(None, max_length=120)
    representing: str | None = Field(None, max_length=300)
    posture: Literal["strict", "balanced"] = "balanced"
    instructions: str | None = Field(None, max_length=4000)


class CompareContractsParams(BaseModel):
    """Compare every input document against a baseline.

    ``baseline`` says where the baseline comes from: ``first_input`` (the
    first document the step reads) or ``document`` (the fixed
    ``baseline_document_id``, which is then required).
    """

    baseline: Literal["first_input", "document"] = "first_input"
    baseline_document_id: str | None = Field(None, max_length=64)
    focus: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _baseline_present(self) -> "CompareContractsParams":
        if self.baseline_document_id:
            self.baseline = "document"
        elif self.baseline == "document":
            raise ValueError("baseline_document_id: pick the baseline document to compare against")
        return self


class DraftDocumentParams(BaseModel):
    """Draft from instructions; ``{{step.field}}`` in the instructions is filled from
    earlier outputs, and the named steps' outputs are attached as reference text."""

    instructions: str = Field(..., min_length=3, max_length=8000)
    include_outputs_from: list[str] = Field(default_factory=list, max_length=12)
    reference_document_ids: list[str] = Field(default_factory=list, max_length=4)


class DeepResearchParams(BaseModel):
    """A deep-research question, templated from earlier outputs, over the input documents."""

    question: str = Field(..., min_length=3, max_length=4000)
    # Restrict the planner's tools; None lets it choose.
    tools: list[DeepResearchTool] | None = Field(None, min_length=1, max_length=2)


class CitationCheckParams(BaseModel):
    """Treatment scan of the given citations plus those found in earlier outputs."""

    citations: list[str] = Field(default_factory=list, max_length=50)
    # Earlier steps whose outputs are scanned for reporter citations; None = all.
    from_steps: list[str] | None = Field(None, max_length=12)


APPROVER_GROUPS = ("owner", "any_member")


class ApprovalParams(BaseModel):
    """Pause the run until an approver decides.

    ``approvers`` is ``"owner"`` (the workflow's owner), ``"any_member"`` (any
    member of the workflow's matter, or the owner when it has none) or a list
    of user ids who must be members of that matter.
    """

    title: str = Field("Approval", min_length=1, max_length=200)
    instructions: str | None = Field(None, max_length=4000)
    approvers: str | list[str] = "owner"

    @field_validator("approvers")
    @classmethod
    def _approvers_valid(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, str):
            if v not in APPROVER_GROUPS:
                raise ValueError("approvers must be 'owner', 'any_member' or a list of user ids")
            return v
        ids = [str(x).strip() for x in v if str(x).strip()]
        if not ids:
            raise ValueError("approvers: name at least one approver")
        if len(ids) > 20:
            raise ValueError("approvers: at most 20 named approvers")
        return list(dict.fromkeys(ids))


STEP_PARAM_MODELS: dict[WorkflowStepKind, type[BaseModel]] = {
    WorkflowStepKind.EXTRACT_FIELDS: ExtractFieldsParams,
    WorkflowStepKind.ANALYZE_CONTRACT: AnalyzeContractParams,
    WorkflowStepKind.COMPARE_CONTRACTS: CompareContractsParams,
    WorkflowStepKind.DRAFT_DOCUMENT: DraftDocumentParams,
    WorkflowStepKind.DEEP_RESEARCH: DeepResearchParams,
    WorkflowStepKind.CITATION_CHECK: CitationCheckParams,
    WorkflowStepKind.APPROVAL: ApprovalParams,
}

_STEP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,59}$")
# ``{{step}}``, ``{{step.field}}``, ``{{step.list.0.field}}`` in templated text.
TEMPLATE_REF_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_-]*)((?:\.[A-Za-z0-9_-]+)*)\s*\}\}")

CONDITION_OPS = (
    "exists",
    "not_exists",
    "equals",
    "not_equals",
    "contains",
    "gt",
    "lt",
    "gte",
    "lte",
    "count_gt",
    "count_eq",
)
# Operators that compare against nothing: ``value`` must be absent.
_UNARY_OPS = ("exists", "not_exists")


class WorkflowCondition(BaseModel):
    """Run the step only when an earlier step's output satisfies this rule.

    ``path`` walks the earlier step's output like a template reference
    (``data.analyses.0.issues``, ``summary``; empty for the whole output).
    """

    step: str = Field(..., min_length=1, max_length=60)
    path: str = Field("", max_length=300)
    op: Literal[
        "exists",
        "not_exists",
        "equals",
        "not_equals",
        "contains",
        "gt",
        "lt",
        "gte",
        "lte",
        "count_gt",
        "count_eq",
    ]
    value: str | int | float | bool | None = None

    @model_validator(mode="after")
    def _value_matches_op(self) -> "WorkflowCondition":
        self.step = self.step.strip()
        self.path = self.path.strip().strip(".")
        if self.path and not re.match(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$", self.path):
            raise ValueError("path: dotted keys and list indexes only, e.g. data.analyses.0.issues")
        if self.op in _UNARY_OPS:
            self.value = None
        elif self.value is None or (isinstance(self.value, str) and not self.value.strip()):
            raise ValueError(f"value: the '{self.op}' condition needs a value to compare with")
        if self.op in ("gt", "lt", "gte", "lte", "count_gt", "count_eq"):
            try:
                float(str(self.value))
            except ValueError:
                raise ValueError(f"value: the '{self.op}' condition needs a number") from None
        return self


class WorkflowStep(BaseModel):
    """One step: a name other steps refer to, the operation, its parameters and inputs."""

    name: str = Field(..., min_length=1, max_length=60)
    kind: WorkflowStepKind
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: WorkflowStepInputs = Field(
        default_factory=lambda: WorkflowStepInputs(
            source=WorkflowInputSource.RUN, document_ids=[], step=None
        )
    )
    # Run only when an earlier step's output satisfies the rule; else the step
    # is recorded as skipped_condition and the run goes on.
    condition: WorkflowCondition | None = None

    @model_validator(mode="after")
    def _validate_params(self) -> "WorkflowStep":
        self.name = self.name.strip()
        if not _STEP_NAME_RE.match(self.name):
            raise ValueError(
                "Step names start with a letter and use letters, digits, '_' or '-' only"
            )
        model = STEP_PARAM_MODELS[self.kind]
        # Re-serialise so the stored params are exactly the validated, normalised set.
        self.params = model.model_validate(self.params).model_dump(mode="json")
        return self


def _template_refs(text: Any) -> set[str]:
    return {m.group(1) for m in TEMPLATE_REF_RE.finditer(text)} if isinstance(text, str) else set()


def referenced_steps_by_field(step: WorkflowStep) -> dict[str, set[str]]:
    """Every earlier step this step names, keyed by the field that names it."""
    refs: dict[str, set[str]] = {}
    if step.inputs.step:
        refs["inputs.step"] = {step.inputs.step}
    if step.condition is not None:
        refs["condition.step"] = {step.condition.step}
    params = step.params
    for key in ("include_outputs_from", "from_steps"):
        names = {str(n) for n in (params.get(key) or [])}
        if names:
            refs[f"params.{key}"] = names
    for key in ("instructions", "question"):
        names = _template_refs(params.get(key))
        if names:
            refs[f"params.{key}"] = names
    return refs


def _referenced_steps(step: WorkflowStep) -> set[str]:
    """Every earlier step this step names, through its inputs, params, condition or templates."""
    refs: set[str] = set()
    for names in referenced_steps_by_field(step).values():
        refs |= names
    return refs


def validate_workflow_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """Names unique (case-insensitively); every reference names an EARLIER step."""
    seen: dict[str, str] = {}
    for step in steps:
        lowered = step.name.lower()
        if lowered in seen:
            raise ValueError(f"Duplicate step name: {step.name}")
        for ref in sorted(_referenced_steps(step)):
            if ref.lower() == lowered:
                raise ValueError(f"Step '{step.name}' refers to itself")
            if ref.lower() not in seen:
                raise ValueError(
                    f"Step '{step.name}' refers to '{ref}', which is not an earlier step"
                )
        seen[lowered] = step.name
    return steps


class WorkflowCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
    description: str | None = Field(None, max_length=2000)
    matter_id: str | None = Field(None, max_length=36)
    steps: list[WorkflowStep] = Field(..., min_length=1, max_length=50)

    @field_validator("steps")
    @classmethod
    def _steps_consistent(cls, v: list[WorkflowStep]) -> list[WorkflowStep]:
        return validate_workflow_steps(v)


class WorkflowUpdateRequest(BaseModel):
    """Change the name, description or steps. Omitted fields keep their value.

    A change to the steps records a new version; ``change_note`` is kept on it.
    """

    name: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = Field(None, max_length=2000)
    steps: list[WorkflowStep] | None = Field(None, min_length=1, max_length=50)
    change_note: str | None = Field(None, max_length=500)

    @field_validator("steps")
    @classmethod
    def _steps_consistent(cls, v: list[WorkflowStep] | None) -> list[WorkflowStep] | None:
        return validate_workflow_steps(v) if v is not None else None


class WorkflowValidateRequest(BaseModel):
    """Steps as the editor holds them, checked without saving. Raw dicts, so
    every step's problems are reported rather than the first one found."""

    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class WorkflowStepIssue(BaseModel):
    index: int  # -1 for a workflow-level problem
    field: str
    message: str


class WorkflowValidateResponse(BaseModel):
    valid: bool
    issues: list[WorkflowStepIssue]


class WorkflowDocumentRef(BaseModel):
    """A document a workflow or run names, with the name the UI shows for it."""

    id: str
    filename: str | None = None
    # False when the caller can no longer read it (left the matter, deleted).
    accessible: bool = True


class WorkflowSummary(BaseModel):
    id: str
    name: str
    description: str | None = None
    matter_id: str | None = None
    owner_id: str
    role: str  # the caller's role: "owner" | "member"
    template_key: str | None = None
    version: int = 1
    archived_at: datetime | None = None
    step_count: int
    step_kinds: list[WorkflowStepKind] = Field(default_factory=list)
    run_count: int = 0
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowSummary]


class WorkflowDetailResponse(WorkflowSummary):
    steps: list[WorkflowStep]
    # Every document the steps name (fixed inputs, baselines, references).
    documents: list[WorkflowDocumentRef] = Field(default_factory=list)


class WorkflowDuplicateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=300)
    matter_id: str | None = Field(None, max_length=36)


class WorkflowVersionSummary(BaseModel):
    version: int
    name: str
    description: str | None = None
    step_count: int
    step_names: list[str] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    change_note: str | None = None


class WorkflowVersionDetail(WorkflowVersionSummary):
    steps: list[WorkflowStep]


class WorkflowVersionListResponse(BaseModel):
    current: int
    versions: list[WorkflowVersionSummary]


class WorkflowRestoreRequest(BaseModel):
    change_note: str | None = Field(None, max_length=500)


class WorkflowDecisionRequest(BaseModel):
    """Approve or reject an awaiting approval step."""

    note: str | None = Field(None, max_length=2000)


class WorkflowTemplate(BaseModel):
    key: str
    name: str
    description: str
    steps: list[WorkflowStep]


class WorkflowTemplatesResponse(BaseModel):
    templates: list[WorkflowTemplate]


class WorkflowRunRequest(BaseModel):
    """Start a run. With no ids and a workflow filed under a matter, the matter's documents."""

    document_ids: list[str] = Field(default_factory=list, max_length=1000)


class WorkflowRunStarted(BaseModel):
    run_id: str
    job_id: str
    poll_url: str
    steps: int
    documents: int


class WorkflowStepRunResponse(BaseModel):
    index: int
    name: str
    kind: str
    status: str
    output: dict[str, Any] | None = None
    input_document_ids: list[str] = Field(default_factory=list)
    produced_ids: dict[str, list[str]] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowRunSummary(BaseModel):
    id: str
    workflow_id: str
    # The workflow's name at read time, for lists that span workflows.
    workflow_name: str | None = None
    workflow_version: int = 1
    runner_id: str
    matter_id: str | None = None
    status: str
    steps: int
    completed_steps: int
    documents: int
    job_id: str | None = None
    error: str | None = None
    resumed_count: int = 0
    # While awaiting approval: the step waiting and who may decide it.
    awaiting_step: str | None = None
    approver_ids: list[str] = Field(default_factory=list)
    can_approve: bool = False
    created_at: datetime
    completed_at: datetime | None = None


class WorkflowRunListResponse(BaseModel):
    runs: list[WorkflowRunSummary]


class WorkflowRunDetailResponse(WorkflowRunSummary):
    step_definitions: list[WorkflowStep]
    step_runs: list[WorkflowStepRunResponse]
    input_document_ids: list[str] = Field(default_factory=list)
    # Names for every document the run read (inputs and documents the steps name).
    document_refs: list[WorkflowDocumentRef] = Field(default_factory=list)


class ChatSessionMessage(BaseModel):
    """A persisted message within a chat session thread."""

    id: str
    role: str  # 'user' | 'assistant'
    content: str
    citations: list[dict] = []
    strategy: dict | None = None
    stats: dict | None = None
    created_at: datetime


class ChatSessionDetail(BaseModel):
    """Full session thread: metadata plus ordered messages."""

    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ChatSessionMessage]


class ChatSessionListResponse(BaseModel):
    """Sessions for the current user, newest first."""

    sessions: list[ChatSessionSummary]


# Document Models


class Document(BaseModel):
    id: str
    user_id: str  # Owner - tenant isolation
    # Matter this document belongs to. None == owner's personal scope (visible
    # only to the owner), which is the historical per-user behavior. When set to
    # a shared matter, every member of that matter can access the document.
    matter_id: str | None = None
    filename: str
    content_type: str
    size: int
    source: ConnectorType
    source_id: str | None = None  # ID in the source system
    status: DocumentStatus
    chunk_count: int = 0
    created_at: datetime
    indexed_at: datetime | None = None
    metadata: dict[str, Any] = {}
    folder_path: str | None = None  # Full folder path (e.g., "/Cases/Smith v Jones/")


class DocumentList(BaseModel):
    documents: list[Document]
    count: int  # length of this page
    # Documents matching the filters across ALL pages, plus the page window
    # that produced this response — what the client needs to page.
    total: int = 0
    limit: int | None = None
    offset: int = 0


# Connector Models
class ConnectorStatus(BaseModel):
    type: ConnectorType
    connected: bool
    configured: bool = True
    account_name: str | None = None
    account_email: str | None = None
    docs_indexed: int = 0
    last_sync: datetime | None = None
    # True for connectors that have not been validated against a live tenant
    # (iManage, NetDocuments); they are registered only with EXPERIMENTAL_CONNECTORS=true.
    experimental: bool = False


class ConnectorAuthUrl(BaseModel):
    auth_url: str
    state: str


class SyncStatus(BaseModel):
    connector: ConnectorType
    status: str  # "idle", "syncing", "completed", "failed"
    progress: float = 0.0
    docs_processed: int = 0
    docs_total: int = 0
    current_file: str | None = None
    error: str | None = None


# RAG Settings Models
class RAGSettings(BaseModel):
    """RAG configuration settings with validation."""

    vector_db: str = Field(default="chroma", pattern=r"^(chroma|pinecone)$")
    index_name: str = Field(default="casecite-legal-docs", min_length=1, max_length=100)
    embedding_model: str = Field(default="text-embedding-3-small", max_length=100)
    dimensions: int = Field(default=1536, ge=256, le=4096)
    chunk_size: int = Field(default=512, ge=100, le=2000)
    chunk_overlap: int = Field(default=128, ge=0, le=500)
    # Same default as settings.similarity_threshold. With text-embedding-3-* a
    # relevant chunk scores ~0.30-0.50, so the old 0.55 default put every real
    # match below the floor for users who had never saved their settings.
    similarity_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    top_k: int = Field(default=10, ge=1, le=100)
    # Cross-encoder reranking. Effective only when the optional
    # sentence-transformers package is installed on the server; otherwise the
    # toggle is ignored and results keep their similarity / keyword order.
    enable_reranking: bool = True
    # Blend a BM25 keyword score over the vector-search candidates into the
    # ranking (rescoring, not a separate keyword index).
    hybrid_search: bool = True
    # Flag which retrieved passages the answer actually draws on. Does not
    # approve citations — review status stays with the reviewer.
    citation_verification: bool = True
    context_compression: bool = False
    query_expansion: bool = True
    source_tracking: bool = True
    llm_model: str = Field(default="gpt-5.5", max_length=100)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=100, le=128000)
    # Firm contract playbook: standing review standards applied to every
    # contract analysis/redline (who we usually represent, what we never
    # accept, preferred fallback positions).
    contract_playbook: str | None = Field(None, max_length=20000)
    # Practice profile: NOTHING about a practice is baked into the product.
    # The user states their practice area, a profile of what matters in that
    # practice is generated on their behalf, they edit it, and it rides along
    # with every review/redline/draft as standing context.
    practice_area: str | None = Field(None, max_length=200)
    practice_profile: str | None = Field(None, max_length=20000)
    # Email when a workflow run the user started (or must approve) completes,
    # fails, waits for approval or is rejected. Only when the server has
    # WORKFLOW_NOTIFICATIONS on and a mail sender configured.
    workflow_email_notifications: bool = True
    # Custom prompts - if empty/None, uses defaults
    custom_system_prompt: str | None = Field(None, max_length=50000)
    custom_grounding_rules: str | None = Field(None, max_length=10000)
    # Factual query prompt - for quick/concise answers
    custom_factual_prompt: str | None = Field(None, max_length=10000)
    # Mode-specific prompts - customize each analysis mode
    custom_research_prompt: str | None = Field(None, max_length=10000)
    custom_case_prompt: str | None = Field(None, max_length=10000)
    custom_document_prompt: str | None = Field(None, max_length=10000)
    custom_compliance_prompt: str | None = Field(None, max_length=10000)
    custom_strategy_prompt: str | None = Field(None, max_length=10000)

    @model_validator(mode="after")
    def validate_overlap_less_than_chunk(self):
        """Ensure overlap is less than chunk size."""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"Chunk overlap ({self.chunk_overlap}) must be less than chunk size ({self.chunk_size})"
            )
        return self


# Document Tree Models (for folder/file selection UI)
class DocumentTreeItem(BaseModel):
    """Represents a file or folder in the document tree."""

    id: str  # Document ID for files, folder path for folders
    name: str
    type: str  # "file" or "folder"
    path: str  # Full path (e.g., "/Cases/Smith v Jones/brief.pdf")
    source: ConnectorType | None = None  # Source connector
    doc_type: str | None = None  # Document type for files
    size: int | None = None  # File size
    children: list["DocumentTreeItem"] = []  # Child items for folders
    document_count: int = 0  # Number of documents in folder (including subfolders)


# Enable forward reference resolution
DocumentTreeItem.model_rebuild()


class DocumentTreeResponse(BaseModel):
    """Response containing the full document tree."""

    tree: list[DocumentTreeItem]
    total_documents: int
    total_folders: int
    sources: list[str]  # Available sources (google_drive, onedrive, local, etc.)


# =============================================================================
# Auth Request Models
# =============================================================================


class DemoLoginRequest(BaseModel):
    """Request body for the DEBUG-only dev login (POST /auth/demo/login)."""

    email: str = ""  # Defaults to routers.auth.DEV_ACCOUNT_EMAIL at runtime
    password: str = "demo"


class AzureLoginRequest(BaseModel):
    """Request body for Azure AD login."""

    token: str  # Azure AD access token


class EmailLoginRequest(BaseModel):
    """Request body for email/password login."""

    email: str
    password: str


class DeviceUserCodeRequest(BaseModel):
    """POST /auth/device/approve and /deny: the short code shown on the device."""

    user_code: str = Field(..., min_length=8, max_length=12)


class DeviceTokenRequest(BaseModel):
    """POST /auth/device/token: the device's secret code from /auth/device/start."""

    device_code: str = Field(..., min_length=16, max_length=128)


class RegisterRequest(BaseModel):
    """Request body for self-hosted user registration."""

    email: EmailStr
    name: str
    password: str
    company: str | None = None
    # Required only for bootstrapping the first (admin) account when the operator
    # has set REGISTRATION_BOOTSTRAP_TOKEN. Ignored for subsequent registrations.
    bootstrap_token: str | None = None


class ForgotPasswordRequest(BaseModel):
    """Request body for forgot password."""

    email: str


class ResetPasswordRequest(BaseModel):
    """Request body for password reset."""

    token: str
    new_password: str


class VerifyResetTokenRequest(BaseModel):
    """Request body for verifying a reset token."""

    token: str


class ChangePasswordRequest(BaseModel):
    """Request body for an authenticated self-service password change."""

    current_password: str
    new_password: str


# =============================================================================
# Admin User Management
# =============================================================================
class AdminUserItem(BaseModel):
    """A user account as shown in the admin Users tab."""

    id: str
    email: str
    name: str
    roles: list[str]
    is_active: bool
    mfa_enabled: bool = False
    must_change_password: bool = False
    created_at: datetime | None = None
    last_login: datetime | None = None


class AdminUserListResponse(BaseModel):
    """List of all user accounts."""

    users: list[AdminUserItem]


class InviteUserRequest(BaseModel):
    """Request body for inviting (creating) a user with an assigned role."""

    email: EmailStr
    name: str = Field(..., min_length=1, max_length=255)
    role: str = Field(..., min_length=1, max_length=40)


class InviteUserResponse(BaseModel):
    """Response after creating an invited user. Returns a one-time temp password."""

    user: AdminUserItem
    temporary_password: str


class UpdateUserRoleRequest(BaseModel):
    """Request body for changing a user's role."""

    role: str = Field(..., min_length=1, max_length=40)


class UpdateRolePermissionsRequest(BaseModel):
    """Request body for setting the permission list of one role."""

    permissions: list[str] = Field(..., max_length=100)


class WorkspaceSessionCreate(BaseModel):
    """Create a restorable workspace-session snapshot."""

    surface: str = Field(..., min_length=1, max_length=50)
    title: str = Field(..., min_length=1, max_length=300)
    payload: dict = Field(default_factory=dict)


class WorkspaceSessionUpdate(BaseModel):
    """Update a workspace-session snapshot (partial)."""

    title: str | None = Field(None, min_length=1, max_length=300)
    payload: dict | None = None


class SetUserActiveRequest(BaseModel):
    """Request body for activating or deactivating (offboarding) a user."""

    is_active: bool


# =============================================================================
# MFA (TOTP)
# =============================================================================


class MfaSetupResponse(BaseModel):
    """Enrollment data: show the QR (from provisioning_uri) and/or the secret."""

    secret: str
    provisioning_uri: str


class MfaEnableRequest(BaseModel):
    """Confirm enrollment by proving possession of the authenticator."""

    code: str = Field(..., min_length=6, max_length=10)


class MfaEnableResponse(BaseModel):
    """One-time recovery codes — shown once, stored only as hashes."""

    recovery_codes: list[str]


class MfaDisableRequest(BaseModel):
    """Disable MFA by proving a current TOTP (or recovery) code AND the password.

    ``password`` is required for accounts that have a local password, so a
    hijacked session alone cannot strip the second factor. SSO-only accounts
    (no local password) omit it.
    """

    code: str = Field(..., min_length=6, max_length=20)
    password: str | None = Field(default=None, max_length=1024)


class MfaVerifyRequest(BaseModel):
    """Second login step: exchange challenge + code for tokens."""

    mfa_token: str
    code: str = Field(..., min_length=6, max_length=20)


class MfaStatusResponse(BaseModel):
    enabled: bool
    recovery_codes_remaining: int = 0


class AuditLogListResponse(BaseModel):
    """Admin audit-trail query result (raw structured entries)."""

    logs: list[dict]
    count: int


class AuditVerifyResponse(BaseModel):
    """Result of verifying the audit trail's HMAC hash chain."""

    valid: bool
    entries_checked: int
    first_invalid_id: str | None = None
    reason: str | None = None
    # Entries written before chain linkage was enforced (signature checked only)
    legacy_entries: int = 0
    # True when the range held more entries than one verification scans
    truncated: bool = False


# =============================================================================
# Branding Models (moved from routers/branding.py)
# =============================================================================


class DemoLoginInfo(BaseModel):
    """Credentials of the public demo account (DEMO_LOGIN_*), published on purpose."""

    email: str
    password: str


class BrandingResponse(BaseModel):
    """Public branding configuration returned to the frontend."""

    firm_name: str
    logo_url: str | None = None
    primary_color: str
    secondary_color: str
    accent_color: str
    favicon_url: str | None = None
    custom_css: str | None = None
    updated_at: str | None = None
    # Set only on a public demo instance; the sign-in form prefills from it.
    demo_login: DemoLoginInfo | None = None


class BrandingUpdate(BaseModel):
    """Request body for updating branding configuration."""

    firm_name: str | None = Field(None, max_length=255, description="Firm / product display name")
    logo_url: str | None = Field(None, max_length=500, description="URL of the logo image")
    primary_color: str | None = Field(None, max_length=20, description="Primary brand color (hex)")
    secondary_color: str | None = Field(
        None, max_length=20, description="Secondary brand color (hex)"
    )
    accent_color: str | None = Field(None, max_length=20, description="Accent color (hex)")
    favicon_url: str | None = Field(None, max_length=500, description="URL of the favicon")
    custom_css: str | None = Field(
        None, max_length=50_000, description="Additional CSS injected into the frontend"
    )

    @field_validator("logo_url", "favicon_url")
    @classmethod
    def _safe_image_url(cls, v: str | None) -> str | None:
        """Same-origin path or https URL only ("" clears the value).

        These land in <img src> / <link href> on the public login page, so
        javascript:, data: and plain-http URLs are refused.
        """
        if not v:
            return v
        v = v.strip()
        same_origin = v.startswith("/") and not v.startswith("//") and "\\" not in v
        if not (same_origin or v.lower().startswith("https://")):
            raise ValueError("must be an https:// URL or a path starting with /")
        if any(ch.isspace() or ch in "\"'<>" for ch in v):
            raise ValueError("contains characters that are not allowed in a URL")
        return v

    @field_validator("primary_color", "secondary_color", "accent_color")
    @classmethod
    def _hex_color(cls, v: str | None) -> str | None:
        """Hex colours only — the value is interpolated into CSS."""
        if v is None:
            return v
        v = v.strip()
        if not re.fullmatch(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", v):
            raise ValueError("must be a hex colour such as #1A2B3C")
        return v


# =============================================================================
# Contract Analysis Request Models (moved from routers/contract_analysis.py)
# =============================================================================


class FullAnalyzeRequest(BaseModel):
    document_text: str | None = Field(
        None, description="Full contract text; omit when document_id is given", max_length=2_000_000
    )
    contract_type: str | None = Field(
        "auto", description='Contract type slug, or "auto"/empty to auto-detect'
    )
    jurisdiction: str | None = None
    effective_date: str | None = Field(
        None, description="ISO date — used to resolve relative deadlines"
    )
    document_id: str | None = None
    representing: str | None = Field(
        None,
        description='Free-text description of the client\'s side (e.g. "Customer", '
        '"Vendor"). Omit for a neutral analysis.',
    )
    posture: str = Field(
        "balanced", description='"strict" (flag everything, aggressive fallbacks) or "balanced"'
    )


class ContractChatRequest(BaseModel):
    document_id: str | None = Field(
        None,
        description="Indexed document to chat about. Optional for mode='draft' — "
        "drafting from scratch needs no reference document.",
    )
    draft_text: str | None = Field(
        None,
        max_length=500_000,
        description="The drafting workspace's current draft. With mode='draft' the "
        "message revises it; other modes answer questions about it.",
    )
    reference_document_ids: list[str] | None = Field(
        None,
        max_length=4,
        description="Indexed documents attached as drafting source material — "
        '"write a demand letter based on this service agreement".',
    )
    message: str = Field(..., min_length=1, max_length=8000)
    mode: str | None = Field(
        None,
        description='Explicit mode ("ask"|"analyze"|"redline"|"draft"). The '
        "user selected it and their message IS the instructions — no intent "
        "routing, no guessing. Omit for automatic routing.",
    )


class DraftPlanRequest(BaseModel):
    """Phase 1 of a long draft: plan the sections from the full reference bundle."""

    message: str = Field(..., min_length=1, max_length=8000)
    reference_document_ids: list[str] | None = Field(None, max_length=4)
    target_pages: int = Field(
        ..., ge=1, le=200, description="Approximate length of the finished document in pages"
    )


class DraftGenerateRequest(BaseModel):
    """Phases 2-3 of a long draft: draft every planned section, then reconcile."""

    message: str = Field(..., min_length=1, max_length=8000)
    reference_document_ids: list[str] | None = Field(None, max_length=4)
    plan: dict = Field(..., description="The plan from /draft/plan, as edited by the user")

    @field_validator("plan")
    @classmethod
    def _bounded_plan(cls, v: dict) -> dict:
        """A plan is a section outline, not a document — cap what gets queued."""
        import json

        if len(json.dumps(v, default=str)) > 200_000:
            raise ValueError("plan is too large")
        return v


class PracticeProfileRequest(BaseModel):
    practice_area: str = Field(..., min_length=2, max_length=200)


class CompareRequest(BaseModel):
    # New multi-document form: 2-4 indexed documents, first is the baseline.
    document_ids: list[str] | None = Field(
        None, min_length=2, max_length=4, description="2-4 indexed documents; first is baseline"
    )
    # Legacy pairwise form (kept for compatibility).
    document_id_a: str | None = Field(None, description="Baseline contract (indexed document)")
    document_id_b: str | None = Field(None, description="Contract to compare against the baseline")
    focus: str | None = Field(
        None, max_length=2000, description="The user's own comparison instructions"
    )


class DraftExportRequest(BaseModel):
    title: str = Field("Draft", max_length=300)
    text: str = Field(..., min_length=1, max_length=500_000)


class RedlineExportRequest(BaseModel):
    """POST body for redline export: rejected refs plus the user's own edits
    to proposed text (ref -> replacement wording)."""

    exclude: list[str] = Field(default_factory=list, max_length=2000)
    overrides: dict[str, str] = Field(default_factory=dict, max_length=2000)

    @field_validator("overrides")
    @classmethod
    def _bounded_overrides(cls, v: dict[str, str]) -> dict[str, str]:
        if any(len(k) > 200 or len(text) > 100_000 for k, text in v.items()):
            raise ValueError("an override is too large")
        return v


# =============================================================================
# File Picker Models (moved from routers/pickers.py)
# =============================================================================


class PickerConfig(BaseModel):
    """Configuration for client-side file pickers."""

    google_enabled: bool = False
    # False when the Google Picker cannot open; ``google_missing`` names the
    # settings fields that are absent (e.g. ["google_api_key"]) so the UI can
    # explain instead of showing a picker that fails to load.
    google_configured: bool = False
    google_missing: list[str] = []
    # The Picker developer key (a browser-side, referrer-restricted API key).
    google_api_key: str | None = None
    google_client_id: str | None = None
    google_app_id: str | None = None

    microsoft_enabled: bool = False
    microsoft_client_id: str | None = None
    microsoft_tenant_id: str | None = None

    box_enabled: bool = False
    box_client_id: str | None = None

    dropbox_enabled: bool = False
    dropbox_app_key: str | None = None


class GooglePickerFile(BaseModel):
    """File selected from Google Picker."""

    id: str
    name: str
    mimeType: str
    url: str | None = None
    sizeBytes: int | None = None
    # OAuth token from picker (scoped to selected files only)
    oauthToken: str


class OneDrivePickerFile(BaseModel):
    """File selected from OneDrive/SharePoint Picker (v8)."""

    id: str
    name: str
    size: int | None = None
    webUrl: str | None = None
    # Direct download URL provided by picker
    downloadUrl: str | None = None
    # Access token from picker (scoped to selected files)
    accessToken: str
    # v8 picker fields for cross-drive (SharePoint) file access
    driveId: str | None = None
    sharepointEndpoint: str | None = None


class BoxPickerFile(BaseModel):
    """File selected from Box Content Picker."""

    id: str
    name: str
    size: int | None = None
    # Access token from Box UI Elements
    accessToken: str


class DropboxChooserFile(BaseModel):
    """File selected from Dropbox Chooser."""

    name: str
    link: str  # Direct download link (temporary, valid for 4 hours)
    bytes: int | None = None
    icon: str | None = None


class BoxPickerTokenResponse(BaseModel):
    """Downscoped Box token for the browser-side Content Picker."""

    access_token: str
    expires_in: int


class ImportFileError(BaseModel):
    """Why one file of an import or sync was not indexed."""

    name: str
    reason: str


class PickerImportResponse(BaseModel):
    """Response from file import."""

    imported: int
    failed: int
    # One entry per failed file (bounded to the first 20); ``failed`` is the
    # full count.
    errors: list[ImportFileError]
