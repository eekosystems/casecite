"""What a chat answer knows besides the documents: the conversation so far and
the matter's facts.

Each chat message used to reach the model on its own, so a fact the user typed
one message earlier was forgotten by the next. Now:

- the recent turns of the saved chat session go with every answer
  (:func:`trim_history` keeps them inside a budget),
- a follow-up ("and when was it signed?") is rewritten into a standalone
  question for searching the documents (:func:`standalone_question`),
- the Facts the case team wrote for the matters in scope go into the prompt
  (:func:`facts_block`).

What the user said, in the chat or in Facts, is their own account. The model is
told to use it and say so, and never to present it as coming from a document
(:data:`CONVERSATION_RULE`); citations still point only at retrieved passages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.services.llm_clients import make_openai, openai_chat, utility_model
from app.services.provider_policy import enforce_openai_client
from app.services.rag.generation import chosen_provider_text

logger = logging.getLogger(__name__)

# The most recent messages sent with an answer, and how much of each.
MAX_HISTORY_MESSAGES = 12
MAX_MESSAGE_CHARS = 4_000
MAX_HISTORY_CHARS = 16_000
# Facts are the case team's own notes; a matter's are capped at 20,000 chars.
MAX_FACTS_CHARS = 24_000

CONVERSATION_RULE = (
    "CONVERSATION AND FACTS RULE: Earlier messages of this conversation are included, "
    "and the case team may have recorded facts for the matter. Treat what the user "
    "has told you, in this conversation or in the recorded facts, as their own "
    "account of the matter: use it, say when your answer relies on it (for example "
    '"based on the facts you gave"), and point out where it conflicts with a '
    "document. Never present it as coming from a document and never cite it as a "
    "source; citations are only for the retrieved passages."
)


NO_SOURCES_RULE = (
    "NO SOURCES: no documents or case law were found for this message. Answer from "
    "the conversation, any facts the case team recorded and general legal knowledge. "
    "Do not invent document contents, quotes, citations or case names. Where the "
    "answer depends on a document or an authority you don't have, say what is needed."
)


@dataclass(frozen=True)
class ConversationContext:
    """The conversation so far and the facts of the matters in scope."""

    # [{"role": "user" | "assistant", "content": str}], oldest first.
    history: list[dict[str, str]] = field(default_factory=list)
    # [{"matter_id", "name", "facts"}]
    matter_facts: list[dict[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.history and not self.matter_facts


def trim_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The most recent user/assistant turns that fit the budget, oldest first.

    Long answers are cut (the end of an answer matters least for follow-ups),
    and the oldest turns are dropped first.
    """
    turns: list[dict[str, str]] = []
    for m in messages:
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + " […]"
        turns.append({"role": role, "content": content})
    turns = turns[-MAX_HISTORY_MESSAGES:]
    while turns and sum(len(t["content"]) for t in turns) > MAX_HISTORY_CHARS:
        turns.pop(0)
    # A conversation sent to a model starts with the user.
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def facts_block(matter_facts: list[dict[str, str]]) -> str:
    """The matters' facts as a prompt section ("" when there are none)."""
    if not matter_facts:
        return ""
    parts = []
    budget = MAX_FACTS_CHARS
    for m in matter_facts:
        text = (m.get("facts") or "").strip()[:budget]
        if not text:
            continue
        budget -= len(text)
        parts.append(
            f"=== FACTS RECORDED FOR THE MATTER “{m.get('name')}” ===\n{text}\n=== END FACTS ==="
        )
        if budget <= 0:
            break
    if not parts:
        return ""
    return (
        "FACTS THE CASE TEAM RECORDED (their own account, written in Matter → Facts; "
        "use them, never cite them as a document):\n\n" + "\n\n".join(parts)
    )


def system_addendum(context: ConversationContext | None) -> str:
    """What to add to a system prompt for this conversation ("" when nothing)."""
    if context is None or context.empty:
        return ""
    sections = [CONVERSATION_RULE]
    facts = facts_block(context.matter_facts)
    if facts:
        sections.append(facts)
    return "\n\n" + "\n\n".join(sections)


def history_transcript(history: list[dict[str, str]], limit: int = 6_000) -> str:
    """Recent turns as plain text, for prompts that take one message."""
    lines = []
    for turn in history:
        who = "User" if turn["role"] == "user" else "Assistant"
        lines.append(f"{who}: {turn['content']}")
    text = "\n\n".join(lines)
    return text[-limit:]


def context_notes(context: ConversationContext | None) -> str:
    """The case team's account for a one-shot prompt (the strategy brief):
    recorded facts and the recent conversation ("" when there is none)."""
    if context is None or context.empty:
        return ""
    parts = []
    facts = facts_block(context.matter_facts)
    if facts:
        parts.append(facts)
    if context.history:
        parts.append("RECENT CONVERSATION:\n" + history_transcript(context.history, 4_000))
    return "\n\n".join(parts)


async def standalone_question(
    query: str,
    history: list[dict[str, str]],
    *,
    user_keys: Any = None,
    model: str | None = None,
) -> str:
    """Rewrite a follow-up into a question that can be searched on its own.

    "When was it signed?" after a message about the Acme lease becomes "When
    was the Acme lease signed?". Without history, or on any failure, the
    message is returned unchanged: this only sharpens retrieval.
    """
    if not history:
        return query
    prompt = (
        "Rewrite the user's latest message as one standalone question that can be "
        "understood and searched without the conversation: resolve pronouns and "
        "references (it, that clause, the second one, they) using the conversation. "
        "Keep every name, date and term the user used. If it already stands alone, "
        "return it unchanged. Return only the question.\n\n"
        f"CONVERSATION:\n{history_transcript(history, 4_000)}\n\n"
        f"LATEST MESSAGE:\n{query[:2_000]}"
    )
    try:
        text = await chosen_provider_text(prompt, user_keys=user_keys, model=model, max_tokens=200)
        if text is None:
            key = user_keys.openai if user_keys and getattr(user_keys, "openai", None) else None
            client = make_openai(key or settings.openai_api_key, async_=True)
            if client is None:
                return query
            enforce_openai_client(client, "follow-up rewriting")
            resp = await openai_chat(
                client,
                model=utility_model(),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=200,
            )
            text = resp.choices[0].message.content
        rewritten = (text or "").strip().strip('"').strip()
        return rewritten[:2_000] if rewritten else query
    except Exception as e:  # rewriting must never break chat
        logger.warning(f"Follow-up rewriting failed, searching with the message as typed: {e}")
        return query
