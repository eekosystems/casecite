"""Turn a research question into a plan the user can read and edit.

One utility-model call proposes 3-8 sub-questions, each tagged with the tools
it may use. The plan is normalised here (ids assigned server-side, unknown
tools dropped, the count capped) and returned synchronously so the user sees
and edits it before anything is searched.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.llm_clients import openai_chat, research_model
from app.services.rag.prompt_safety import UNTRUSTED_CONTENT_RULE, untrusted_block

logger = logging.getLogger(__name__)

TOOL_DOCUMENTS = "documents"
TOOL_CASE_LAW = "case_law"
ALL_TOOLS = (TOOL_DOCUMENTS, TOOL_CASE_LAW)
MIN_SUBQUESTIONS = 3


class PlanningError(RuntimeError):
    """The planner call failed or returned nothing usable."""


def _tool_lines(available: dict[str, bool]) -> str:
    lines = []
    if available.get(TOOL_DOCUMENTS, True):
        lines.append(
            f'- "{TOOL_DOCUMENTS}": semantic search over the firm\'s own indexed documents '
            "(contracts, filings, memos, correspondence)"
        )
    if available.get(TOOL_CASE_LAW, True):
        lines.append(
            f'- "{TOOL_CASE_LAW}": search of published court opinions (CourtListener), '
            "with the relevant passage of each opinion read"
        )
    return "\n".join(lines)


def build_plan_prompt(question: str, available: dict[str, bool], max_subquestions: int) -> str:
    return (
        "A lawyer wants a research question answered thoroughly. Break it into "
        f"{MIN_SUBQUESTIONS}-{max_subquestions} distinct sub-questions that, answered "
        "together, answer the whole. Each sub-question must be self-contained (it "
        "will be researched on its own) and name the tools it should use.\n\n"
        f"TOOLS:\n{_tool_lines(available)}\n\n"
        'Use "documents" for anything about the client\'s own facts, agreements or '
        'filings; use "case_law" for rules of law, standards and how courts have '
        "decided; use both when a sub-question needs the facts and the law.\n\n"
        f"{UNTRUSTED_CONTENT_RULE}\n\n"
        f"QUESTION:\n{untrusted_block('Research question', question)}\n\n"
        'Return STRICT JSON: {"sub_questions": [{"question": "...", '
        '"tools": ["documents", "case_law"]}, ...]}'
    )


def normalise_plan(
    raw: Any, available: dict[str, bool], max_subquestions: int
) -> list[dict[str, Any]]:
    """Validate the planner's output into [{id, question, tools}], capped."""
    items = raw.get("sub_questions") if isinstance(raw, dict) else None
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if len(question) < 3 or question.lower() in seen:
            continue
        raw_tools = item.get("tools")
        tools = [str(t).strip().lower() for t in (raw_tools if isinstance(raw_tools, list) else [])]
        tools = [t for t in dict.fromkeys(tools) if t in ALL_TOOLS and available.get(t, True)]
        if not tools:
            tools = [t for t in ALL_TOOLS if available.get(t, True)]
        if not tools:
            continue
        seen.add(question.lower())
        plan.append({"id": f"q{len(plan) + 1}", "question": question[:1000], "tools": tools})
        if len(plan) >= max_subquestions:
            break
    return plan


async def plan_research(
    client: Any,
    question: str,
    *,
    available: dict[str, bool],
    max_subquestions: int,
) -> list[dict[str, Any]]:
    """Propose the plan. Raises PlanningError when nothing usable comes back."""
    prompt = build_plan_prompt(question, available, max_subquestions)
    try:
        resp = await openai_chat(
            client,
            model=research_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        logger.warning("Deep research planning failed: %s", e)
        raise PlanningError("The research plan could not be drafted. Please try again.") from e
    plan = normalise_plan(data, available, max_subquestions)
    if not plan:
        raise PlanningError("The planner returned no usable sub-questions. Please try again.")
    return plan
