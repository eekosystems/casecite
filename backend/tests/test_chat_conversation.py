"""
Chat as a conversation about a matter: earlier turns, the matter's Facts, and an
answer even when no documents match.

Before this, each message reached the model on its own (a fact typed one message
earlier was forgotten) and chat refused to answer when no documents matched, so
a matter with facts but no uploads could not be discussed at all.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only-32chars!")
os.environ.setdefault("ENCRYPTION_SALT", "test-salt-16chars!")
os.environ.setdefault("DEBUG", "true")

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.schemas import RAGSettings
from app.services.rag.conversation import (
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_CHARS,
    ConversationContext,
    context_notes,
    facts_block,
    standalone_question,
    system_addendum,
    trim_history,
)

FACTS = [{"matter_id": "m1", "name": "Acme v. Beta", "facts": "Landlord kept the deposit."}]


class TestTrimHistory:
    def test_keeps_recent_turns_starting_with_the_user(self):
        messages = [{"role": "assistant", "content": "Welcome"}]
        for i in range(20):
            messages += [
                {"role": "user", "content": f"q{i}"},
                {"role": "assistant", "content": f"a{i}"},
            ]
        turns = trim_history(messages)
        assert len(turns) == MAX_HISTORY_MESSAGES
        assert turns[0] == {"role": "user", "content": "q14"}
        assert turns[-1] == {"role": "assistant", "content": "a19"}

    def test_long_answers_are_cut_and_odd_rows_skipped(self):
        turns = trim_history(
            [
                {"role": "user", "content": "x" * (MAX_MESSAGE_CHARS + 50)},
                {"role": "system", "content": "ignored"},
                {"role": "assistant", "content": "   "},
            ]
        )
        assert len(turns) == 1
        assert turns[0]["content"].endswith("[…]")
        assert len(turns[0]["content"]) < MAX_MESSAGE_CHARS + 10


class TestPromptText:
    def test_nothing_is_added_without_history_or_facts(self):
        assert system_addendum(None) == ""
        assert system_addendum(ConversationContext()) == ""
        assert context_notes(ConversationContext()) == ""

    def test_facts_are_labelled_as_the_case_teams_account(self):
        block = facts_block(FACTS)
        assert "Acme v. Beta" in block and "Landlord kept the deposit." in block
        assert "never cite them" in block
        addendum = system_addendum(ConversationContext(matter_facts=FACTS))
        assert "CONVERSATION AND FACTS RULE" in addendum
        assert "Landlord kept the deposit." in addendum

    def test_context_notes_carry_facts_and_recent_turns(self):
        notes = context_notes(
            ConversationContext(
                history=[{"role": "user", "content": "The client wants the deposit back."}],
                matter_facts=FACTS,
            )
        )
        assert "Landlord kept the deposit." in notes
        assert "User: The client wants the deposit back." in notes


class TestStandaloneQuestion:
    HISTORY = [
        {"role": "user", "content": "Tell me about the Acme lease."},
        {"role": "assistant", "content": "It runs five years."},
    ]

    @pytest.mark.asyncio
    async def test_without_history_the_message_is_used_as_typed(self):
        with patch("app.services.rag.conversation.chosen_provider_text", AsyncMock()) as call:
            assert await standalone_question("When was it signed?", []) == "When was it signed?"
        call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_follow_up_is_rewritten_with_the_conversation(self):
        with patch(
            "app.services.rag.conversation.chosen_provider_text",
            AsyncMock(return_value='"When was the Acme lease signed?"'),
        ) as call:
            rewritten = await standalone_question(
                "When was it signed?", self.HISTORY, model="claude-opus-4-8"
            )
        assert rewritten == "When was the Acme lease signed?"
        assert "It runs five years." in call.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_failure_falls_back_to_the_message(self):
        with patch(
            "app.services.rag.conversation.chosen_provider_text",
            AsyncMock(side_effect=RuntimeError("down")),
        ):
            assert await standalone_question("When was it signed?", self.HISTORY) == (
                "When was it signed?"
            )


class TestHistoryReachesTheModel:
    TURNS = [
        {"role": "user", "content": "The deposit was $2,000."},
        {"role": "assistant", "content": "Noted."},
    ]

    @pytest.mark.asyncio
    async def test_openai_compatible(self):
        from app.services.rag.generation import call_llm

        client = MagicMock()
        client.base_url = "https://api.openai.com/v1"
        client.chat.completions.create = AsyncMock(
            return_value=MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])
        )
        await call_llm(client, None, None, "gpt-5.5", "sys", "How much?", 100, history=self.TURNS)
        sent = client.chat.completions.create.call_args.kwargs["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[1]["content"] == "The deposit was $2,000."

    @pytest.mark.asyncio
    async def test_anthropic_gets_alternating_turns(self):
        from app.services.rag.generation import call_llm

        anthropic = MagicMock()
        anthropic.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="ok")])
        )
        # A stored turn without its answer (a failed save) must not break alternation.
        history = [*self.TURNS, {"role": "user", "content": "Also it was paid in cash."}]
        await call_llm(
            None, anthropic, None, "claude-opus-4-8", "sys", "How much?", 100, history=history
        )
        sent = anthropic.messages.create.call_args.kwargs["messages"]
        roles = [m["role"] for m in sent]
        assert roles == ["user", "assistant", "user"]
        assert sent[-1]["content"].endswith("How much?")
        assert "paid in cash" in sent[-1]["content"]

    @pytest.mark.asyncio
    async def test_gemini_gets_a_transcript(self):
        from app.services.rag.generation import call_llm

        gemini = MagicMock()
        gemini.generate_content = MagicMock(return_value=MagicMock(text="ok"))
        await call_llm(
            None,
            None,
            gemini,
            "gemini-3.1-pro-preview",
            "sys",
            "How much?",
            100,
            history=self.TURNS,
        )
        prompt = gemini.generate_content.call_args.args[0]
        assert "CONVERSATION SO FAR" in prompt and "User: The deposit was $2,000." in prompt


def _service(vector_db=None):
    from app.services.rag.service import RAGService

    service = RAGService.__new__(RAGService)
    service.vector_db = vector_db if vector_db is not None else MagicMock()
    return service


def _llm(content="An answer."):
    client = MagicMock()
    client.base_url = "https://api.openai.com/v1"
    client.chat.completions.create = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=content))])
    )
    return client


class TestAnswersWithoutSources:
    async def _ask(self, service, llm, **kwargs):
        with (
            patch.object(service, "_resolve_llm_clients", return_value=(llm, None, None)),
            patch("app.services.rag.service.search_documents", AsyncMock(return_value=[])),
            patch.object(service, "_explain_threshold_misses", AsyncMock(return_value=None)),
        ):
            return await service.generate_response(
                query="Is the deposit claim strong?",
                user_id="user-1",
                include_case_law=False,
                rag_settings=RAGSettings(query_expansion=False),
                **kwargs,
            )

    @pytest.mark.asyncio
    async def test_an_empty_library_still_gets_an_answer_saying_so(self):
        vector_db = MagicMock()
        vector_db.get_stats = AsyncMock(return_value={"total_chunks": 0})
        llm = _llm("Based on what you told me, yes.")
        result = await self._ask(_service(vector_db), llm)
        assert result["content"].startswith(
            "> **No documents used:** there are no indexed documents"
        )
        assert result["content"].endswith("Based on what you told me, yes.")
        assert result["citations"] == []
        llm.chat.completions.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_match_says_nothing_matched(self):
        vector_db = MagicMock()
        vector_db.get_stats = AsyncMock(return_value={"total_chunks": 40})
        result = await self._ask(_service(vector_db), _llm())
        assert "nothing in your documents or the case law searched matched" in result["content"]

    @pytest.mark.asyncio
    async def test_case_names_the_model_invents_are_still_removed(self):
        vector_db = MagicMock()
        vector_db.get_stats = AsyncMock(return_value={"total_chunks": 0})
        llm = _llm("See Smith v. Jones, 123 F.3d 456 (9th Cir. 1999), which holds so.")
        result = await self._ask(_service(vector_db), llm)
        assert "Smith v. Jones" not in result["content"]

    @pytest.mark.asyncio
    async def test_sources_switched_off_get_no_label(self):
        llm = _llm("Plain answer.")
        result = await self._ask(_service(), llm, include_documents=False)
        assert result["content"] == "Plain answer."


class TestLoadConversation:
    def _request(self, **kwargs):
        from app.models.schemas import ChatRequest

        return ChatRequest(query="And the deposit?", **kwargs)

    @pytest.mark.asyncio
    async def test_history_and_facts_of_the_matters_in_scope(self):
        from app.models.schemas import DocumentFilter
        from app.routers.chat import _load_conversation

        thread = {
            "matter_id": "m-session",
            "messages": [
                {"role": "user", "content": "The lease is with Acme."},
                {"role": "assistant", "content": "Understood."},
            ],
        }
        get_facts = AsyncMock(return_value=FACTS)
        with (
            patch(
                "app.routers.chat.chat_session_service.get_session_with_messages",
                AsyncMock(return_value=thread),
            ),
            patch("app.routers.chat.matters_service.get_matter_facts", get_facts),
        ):
            conversation = await _load_conversation(
                self._request(
                    session_id="s1",
                    document_filter=DocumentFilter(matter_ids=["m-filter", "m-not-mine"]),
                ),
                "user-1",
                {"m-session", "m-filter"},
                None,
            )
        assert [t["content"] for t in conversation.history] == [
            "The lease is with Acme.",
            "Understood.",
        ]
        assert conversation.matter_facts == FACTS
        # Only matters the caller belongs to: the session's and the selected one.
        assert set(get_facts.await_args.args[1]) == {"m-session", "m-filter"}

    @pytest.mark.asyncio
    async def test_search_all_ignores_selected_matters_and_storage_errors_are_quiet(self):
        from app.models.schemas import DocumentFilter
        from app.routers.chat import _load_conversation

        get_facts = AsyncMock()
        with (
            patch(
                "app.routers.chat.chat_session_service.get_session_with_messages",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("app.routers.chat.matters_service.get_matter_facts", get_facts),
        ):
            conversation = await _load_conversation(
                self._request(
                    session_id="s1",
                    document_filter=DocumentFilter(matter_ids=["m1"], search_all=True),
                ),
                "user-1",
                {"m1"},
                None,
            )
        assert conversation.empty
        get_facts.assert_not_awaited()


@pytest.mark.usefixtures("no_rate_limit")
class TestChatEndToEnd:
    def test_a_follow_up_carries_the_conversation_and_the_matters_facts(self, client, auth_headers):
        """Second message of a chat about a matter: the answer gets the first
        exchange and the matter's facts, and searches the standalone question."""
        matter = client.post(
            "/api/v1/matters",
            headers=auth_headers,
            json={"name": f"Deposit {uuid.uuid4().hex[:6]}"},
        ).json()
        client.put(
            f"/api/v1/matters/{matter['id']}/facts",
            headers=auth_headers,
            json={"facts": "Landlord kept the $2,000 deposit."},
        )
        answer = {
            "content": "Answer.",
            "citations": [],
            "stats": {"docs_searched": 0, "chunks_retrieved": 0, "processing_time": "0.1s"},
        }
        generate = AsyncMock(return_value=answer)
        body = {
            "query": "Can we get it back?",
            "document_filter": {"matter_ids": [matter["id"]]},
        }
        with (
            patch("app.routers.chat.rag_service.generate_response", generate),
            patch(
                "app.routers.chat.route_query",
                AsyncMock(return_value=("ask", False, None, False)),
            ),
            patch(
                "app.routers.chat.standalone_question",
                AsyncMock(
                    side_effect=lambda q, h, **_: q if not h else "Can we get the deposit back?"
                ),
            ),
        ):
            first = client.post("/api/v1/chat", headers=auth_headers, json=body).json()
            client.post(
                "/api/v1/chat",
                headers=auth_headers,
                json={**body, "session_id": first["session_id"]},
            )

        first_call, second_call = generate.await_args_list
        assert first_call.kwargs["conversation"].history == []
        assert first_call.kwargs["conversation"].matter_facts[0]["facts"] == (
            "Landlord kept the $2,000 deposit."
        )
        second = second_call.kwargs
        assert [t["role"] for t in second["conversation"].history] == ["user", "assistant"]
        assert second["conversation"].history[0]["content"] == "Can we get it back?"
        assert second["search_query"] == "Can we get the deposit back?"
        assert second["query"] == "Can we get it back?"


class TestStrategyBrief:
    @pytest.mark.asyncio
    async def test_the_case_teams_account_is_background_not_a_source(self):
        from app.services.strategy import strategy_service

        client = MagicMock()
        client.base_url = "https://api.openai.com/v1"
        client.chat.completions.create = AsyncMock(
            return_value=MagicMock(choices=[MagicMock(message=MagicMock(content="{}"))])
        )
        passages = [{"ref": "d0", "filename": "lease.pdf", "snippet": "Deposit clause."}]
        await strategy_service._synthesize(
            client, "Can we get the deposit back?", passages, "Landlord kept the deposit."
        )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "THE CASE TEAM'S OWN ACCOUNT" in prompt
        assert "Landlord kept the deposit." in prompt
        assert "every point must still rest on the passages" in prompt
