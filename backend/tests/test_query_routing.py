"""
Unit tests for per-message chat routing (ask vs strategy, case-law flag).

The heuristic is the always-available fallback behind the utility-LLM
classifier, so the shapes users actually type must route correctly here.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from app.services.rag.routing import (
    distill_search_query,
    heuristic_route,
    parse_route_payload,
    parse_search_query,
)


class TestHeuristicRoute:
    def test_factual_question_is_ask_without_case_law(self):
        assert heuristic_route("Summarize the indemnification obligations") == ("ask", False)

    def test_strategy_wording_routes_to_strategy(self):
        intent, _ = heuristic_route("What are our strongest arguments in this matter?")
        assert intent == "strategy"

    def test_all_case_law_for_a_file_is_strategy(self):
        # "ALL the case law" anchored to the user's own file(s) is a strategy
        # ask: the brief reads every scoped document and attaches verified
        # authority to each point it derives.
        assert heuristic_route(
            "Look in the Johnson file and find me all relevant case law to support my case"
        ) == ("strategy", True)

    def test_all_case_law_for_selected_files_is_strategy(self):
        assert heuristic_route(
            "Read all six of these files and give me all of the case law relevant to them"
        ) == ("strategy", True)

    def test_all_case_law_on_a_topic_is_not_strategy(self):
        # No document anchor — a pure research lookup, never a document audit.
        intent, _ = heuristic_route("Give me all the case law on DWI stops")
        assert intent == "research"

    def test_pull_case_law_supporting_our_position_is_strategy_with_case_law(self):
        assert heuristic_route("Pull case law supporting our position") == ("strategy", True)

    def test_case_law_that_supports_us_is_strategy(self):
        intent, use_case_law = heuristic_route("What case law supports our claim here?")
        assert intent == "strategy"
        assert use_case_law is True

    def test_plain_case_law_lookup_is_ask_with_case_law(self):
        # Mentions authority but asks nothing strategic — grounded Q&A with case law.
        assert heuristic_route("Which court issued the ruling cited in exhibit B?") == (
            "ask",
            True,
        )

    def test_topical_case_law_lookup_is_research(self):
        # No tie to the user's matter — a pure lookup that must NOT read the
        # documents (routing it to the strategy brief drags every doc in).
        assert heuristic_route("find me case law for a dwi") == ("research", True)

    def test_case_law_about_doctrine_is_research(self):
        assert heuristic_route("Find case law on termination for convenience") == (
            "research",
            True,
        )

    def test_find_case_law_tied_to_this_matter_is_strategy(self):
        # "this ... contract" ties the lookup to the matter, even with words
        # in between — the position must be derived from the documents first.
        intent, use_case_law = heuristic_route(
            "find me case law i could use to get out of this faith group contract"
        )
        assert intent == "strategy"
        assert use_case_law is True


class TestParseRoutePayload:
    def test_valid_payload_passes_through(self):
        assert parse_route_payload('{"intent": "strategy", "use_case_law": true}', "whatever") == (
            "strategy",
            True,
        )

    def test_research_intent_is_accepted(self):
        assert parse_route_payload('{"intent": "research", "use_case_law": true}', "whatever") == (
            "research",
            True,
        )

    def test_garbage_falls_back_to_heuristic(self):
        assert parse_route_payload("not json", "find case law to support my case") == (
            "strategy",
            True,
        )

    def test_unknown_intent_falls_back_to_heuristic(self):
        assert parse_route_payload('{"intent": "poem"}', "summarize the lease") == (
            "ask",
            False,
        )


class TestSearchQueryDistillation:
    def test_strips_request_filler_to_topic(self):
        # The raw message keyword-matches contract cases on "case"/"law"/"find";
        # the search term must be the topic alone.
        assert distill_search_query("find me case law for a dwi") == "dwi"

    def test_keeps_doctrine_terms(self):
        assert (
            distill_search_query("pull precedents about qualified immunity excessive force")
            == "qualified immunity excessive force"
        )

    def test_returns_none_when_nothing_substantive_remains(self):
        assert distill_search_query("find me some case law") is None

    def test_parse_search_query_uses_classifier_value(self):
        assert (
            parse_search_query(
                '{"search_query": "DWI warrantless blood draw"}', "find me case law for a dwi"
            )
            == "DWI warrantless blood draw"
        )

    def test_parse_search_query_falls_back_to_distillation(self):
        assert parse_search_query("not json", "find me case law for a dwi") == "dwi"
        assert parse_search_query('{"search_query": ""}', "find me case law for a dwi") == "dwi"
