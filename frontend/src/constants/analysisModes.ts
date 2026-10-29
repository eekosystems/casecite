// Self-hosted build: focused on RAG over private documents and
// CourtListener-backed case/judge lookups. The knowledge base (documents +
// import sources) is not an analysis mode — it lives in its own Documents
// section in the sidebar and routes to /documents.
// "Give me all the case law for this file/folder" is a Matter Strategy ask:
// the brief reads every scoped document and attaches verified authority.
export const ANALYSIS_MODES = [
  { id: 'research', title: 'Matter Strategy', icon: 'Lightbulb', desc: 'Your files, chat, strategy & verified case law' },
  { id: 'contracts', title: 'Contracts', icon: 'FileText', desc: 'Review, redline & compare contracts' },
  { id: 'drafting', title: 'Drafting', icon: 'PenLine', desc: 'Draft contracts, letters & documents' },
  { id: 'review-tables', title: 'Tabular Review', icon: 'Table2', desc: 'Extract cited fields across many contracts' },
  { id: 'workflows', title: 'Workflows', icon: 'Workflow', desc: 'Save and re-run multi-step review processes' },
] as const;
