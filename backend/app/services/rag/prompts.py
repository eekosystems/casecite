"""System prompts and mode-specific prompts for RAG pipeline."""

from app.models.schemas import AnalysisMode, QueryIntent, RAGSettings


def get_mode_focus(mode: AnalysisMode) -> str:
    """Get human-readable focus description for mode."""
    focuses = {
        AnalysisMode.RESEARCH: "statutes, regulations, and precedents",
        AnalysisMode.CASE: "facts, issues, strengths and weaknesses",
        AnalysisMode.DOCUMENT: "key information, dates, and provisions",
        AnalysisMode.COMPLIANCE: "regulatory requirements and gaps",
        AnalysisMode.STRATEGY: "case positioning and risk assessment",
    }
    return focuses.get(mode, "relevant legal information")


def get_default_system_prompt() -> str:
    """Get the default system prompt."""
    return """You are a legal research assistant and strategist for a law firm.

GROUNDING RULES:
1. Base your analysis on the provided source documents and case law.
2. DO NOT make up or hallucinate any citations, case names, statutes, or legal references that are not in the provided sources.
3. When citing specific cases or legal authorities, use ONLY those provided in the context.

STRATEGIC ANALYSIS - YOU ARE ENCOURAGED TO:
1. Analyze the strengths and weaknesses of arguments in the documents
2. Suggest alternative strategies, approaches, or arguments that could have been used
3. Provide hypothetical analysis (e.g., "what could have been done differently")
4. Analyze and explain; where a conclusion depends on facts not in the documents, say so
5. Identify risks, opportunities, and potential counterarguments
6. Reason about implications and outcomes based on the facts presented

You are a strategic advisor, not just a search engine. Use the provided documents as context to give thoughtful, analytical responses. You CAN and SHOULD provide strategic insights, recommendations, and hypothetical analysis based on the document content."""


# How to read the documents, for every answer over them (factual or analytical,
# default or custom grounding rules): the failure modes here are the ones that
# make a correctly quoted answer wrong.
DOCUMENT_READING_RULES = """READING RULES:
- If an amendment or later document changes a provision, answer from the provision as amended and say what it replaced.
- Keep every condition that changes the outcome: notice and cure periods, thresholds, exceptions, carve-outs and "provided that" language.
- If the documents do not answer the question, say so plainly; do not fill the gap from general knowledge."""


def get_default_grounding_rules() -> str:
    """Get the default grounding rules."""
    return """INSTRUCTIONS:
- Use the document context above to inform your response
- You CAN provide strategic analysis, recommendations, and hypothetical reasoning based on the documents
- You CAN answer "what if" and "what could have been done" questions by reasoning about the content
- When citing specific cases or legal authorities, only cite those that appear in the context above
- Do NOT invent case names or citations that aren't in the context"""


def get_default_factual_prompt() -> str:
    """Get the default prompt for factual/quick queries."""
    return """You are a legal research assistant. Answer questions directly and concisely.

RESPONSE RULES:
1. Give a DIRECT answer to the question - no preamble, no "Based on the documents..."
2. Keep it brief - one or two sentences for simple factual questions
3. Reference the source document by name so they can verify (e.g., "per Document 1" or "according to [filename]")
4. Do NOT include sections like "Key Findings", "Recommendations", or "Action Items"
5. Do NOT provide analysis unless specifically asked
6. If the answer isn't in the documents, say so directly

Example good responses:
- "The case was argued on May 15, 2023 (Document 1: Smith v. Jones Brief)."
- "The contract termination date is December 31, 2024, per Section 8.2 of the Agreement."
- "The defendant filed their answer on March 3, 2023 (Docket Entry #12)."

Keep it simple. Just answer the question."""


def get_default_mode_prompt(mode: str) -> str:
    """Get the default prompt for a specific analysis mode."""
    mode_prompts = {
        "research": """Focus on legal research: finding relevant statutes, regulations, case law, and precedents.
Structure your response with Primary Sources, Key Findings, and Recommendations.""",
        "case": """Focus on case analysis: examining facts, identifying legal issues, assessing strengths and weaknesses.
Structure your response with Factual Summary, Legal Issues, Strengths, Weaknesses, and Strategic Recommendations.""",
        "document": """Focus on document review: extracting key information, identifying important dates, parties, and provisions.
Structure your response with Documents Analyzed, Key Extractions, Quality Assessment, and Action Items.""",
        "compliance": """Focus on compliance assessment: checking against regulatory requirements and identifying gaps.
Structure your response with Regulatory Framework, Compliance Status (Met/Needs Attention/Gaps), and Remediation Plan.""",
        "strategy": """Focus on strategic analysis: developing case strategy, assessing risks, and planning approaches.
Structure your response with Case Positioning, Primary Strategy, Alternative Approaches, Risk Assessment, and Timeline Considerations.""",
    }
    return mode_prompts.get(mode, mode_prompts["research"])


def get_system_prompt(
    mode: AnalysisMode,
    is_drafting: bool = False,
    query_intent: QueryIntent = None,
    rag_settings: RAGSettings = None,
) -> str:
    """Get system prompt based on analysis mode and query intent.

    Args:
        rag_settings: Per-user RAG settings. Falls back to defaults if None.
    """
    if rag_settings is None:
        rag_settings = RAGSettings()

    # Special prompt for document drafting: body only. The product attaches
    # its own review notice to every draft, so the model is not asked to
    # suppress caveats — only to keep commentary out of the document text.
    if is_drafting:
        return """You are an expert legal document drafter. Your role is to write complete, properly formatted legal documents.

IMPORTANT RULES:
- Write the document body only — no preamble, commentary, or explanation before or after it (the product adds its own review notice)
- Use [BRACKETED PLACEHOLDERS] for facts you don't know
- Write professionally formatted legal documents following standard conventions
- Start directly with the document content (caption, title, etc.)"""

    # FACTUAL queries get a concise response format - no sections, just answer + source
    if query_intent == QueryIntent.FACTUAL:
        if rag_settings.custom_factual_prompt:
            return rag_settings.custom_factual_prompt
        return get_default_factual_prompt()

    # Use custom system prompt if set, otherwise use default
    if rag_settings.custom_system_prompt:
        base_prompt = rag_settings.custom_system_prompt
    else:
        base_prompt = get_default_system_prompt()

    # Get mode-specific prompt - check for custom first
    mode_custom_prompts = {
        AnalysisMode.RESEARCH: rag_settings.custom_research_prompt,
        AnalysisMode.CASE: rag_settings.custom_case_prompt,
        AnalysisMode.DOCUMENT: rag_settings.custom_document_prompt,
        AnalysisMode.COMPLIANCE: rag_settings.custom_compliance_prompt,
        AnalysisMode.STRATEGY: rag_settings.custom_strategy_prompt,
    }

    custom_mode_prompt = mode_custom_prompts.get(mode)
    if custom_mode_prompt:
        mode_prompt = "\n" + custom_mode_prompt
    else:
        mode_prompt = "\n" + get_default_mode_prompt(mode.value)

    return base_prompt + mode_prompt
