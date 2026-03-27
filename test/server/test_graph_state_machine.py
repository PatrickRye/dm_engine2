"""
Integration tests for the LangGraph DM Engine state machine (server/graph.py).

These tests exercise the graph's routing logic, node state transformations,
and the action_logic mutation-capture mechanism WITHOUT requiring real LLM calls.

What IS tested:
- Tool name extraction from AIMessage tool_calls
- defer_mutations frozenset building from tool metadata
- Router functions: planner_tool_router, qa_router, drama_manager_router
- Cache invalidation functions
- Graph assembly (compile succeeds with empty tools list)

What is NOT tested (requires real LLM or exposed action_logic_node):
- action_logic_node mutation-capture (nested inside build_graph, importable after hoisting)
- planner_node, narrator_node, qa_node, drama_manager_node actual LLM invocations
- commit_node LLM-mediated mutation execution
- ingestion_node, compendium_hydration_node actual LLM invocations
"""
import pytest
from typing import Any
from unittest.mock import MagicMock, patch
from langgraph.checkpoint.memory import MemorySaver

from state import DMState
from graph import (
    _get_tool_name_from_message,
    _build_defer_mutations_frozenset,
    planner_tool_router,
    qa_router,
    drama_manager_router,
    _invalidate_grag_cache,
    build_graph,
)


# ------------------------------------------------------------------
# Helper: make a mock AIMessage with tool_calls
# ------------------------------------------------------------------
def _make_ai_msg(tool_calls: list[dict]) -> MagicMock:
    msg = MagicMock()
    msg.tool_calls = tool_calls
    return msg


# ------------------------------------------------------------------
# Helper: minimal valid DMState dict
# ------------------------------------------------------------------
def _dm(messages: list = None, qa_feedback: str = "", revision_count: int = 0,
         active_storylet_id: str = None, pending_mutations: list = None,
         vault_path: str = "default", active_character: str = "Player",
         draft_response: str = "") -> DMState:
    """Construct a minimally-valid DMState dict for testing."""
    return DMState(
        messages=messages or [],
        vault_path=vault_path,
        active_character=active_character,
        draft_response=draft_response,
        qa_feedback=qa_feedback,
        revision_count=revision_count,
        knowledge_graph={},
        active_storylet_id=active_storylet_id,
        pending_mutations=pending_mutations if pending_mutations is not None else [],
        tension_arc={},
        kg_snapshot=None,
        mutation_errors=[],
        pending_ingest={},
    )


# ------------------------------------------------------------------
# Tests: _get_tool_name_from_message
# ------------------------------------------------------------------
class TestGetToolNameFromMessage:
    def test_extractsNameFromDictToolCall(self):
        msg = _make_ai_msg([{"name": "modify_health", "args": {}, "id": "call_1"}])
        assert _get_tool_name_from_message(msg) == "modify_health"

    def test_extractsNameFromObjectToolCall(self):
        tc = MagicMock()
        tc.name = "execute_melee_attack"
        tc.args = {}
        tc.id = "call_2"
        msg = _make_ai_msg([tc])
        assert _get_tool_name_from_message(msg) == "execute_melee_attack"

    def test_returnsNoneWhenNoToolCalls(self):
        msg = MagicMock()
        msg.tool_calls = []
        assert _get_tool_name_from_message(msg) is None

    def test_returnsNoneWhenToolCallsAttrMissing(self):
        msg = MagicMock()
        del msg.tool_calls
        assert _get_tool_name_from_message(msg) is None

    def test_picksFirstNamedToolCall(self):
        msg = _make_ai_msg([
            {"name": "first_tool", "args": {}, "id": "c1"},
            {"name": "second_tool", "args": {}, "id": "c2"},
        ])
        assert _get_tool_name_from_message(msg) == "first_tool"


# ------------------------------------------------------------------
# Tests: _build_defer_mutations_frozenset
# ------------------------------------------------------------------
class TestBuildDeferMutationsFrozenset:
    def _tool(self, name: str, defer: bool | None = True):
        t = MagicMock()
        t.name = name
        t.metadata = {"defer_mutations": defer} if defer is not None else {}
        return t

    def test_includesDeferMutationsTrue(self):
        result = _build_defer_mutations_frozenset([self._tool("request_graph_mutations", True)])
        assert "request_graph_mutations" in result

    def test_excludesDeferMutationsFalse(self):
        result = _build_defer_mutations_frozenset([self._tool("some_tool", False)])
        assert "some_tool" not in result

    def test_excludesNoMetadata(self):
        result = _build_defer_mutations_frozenset([self._tool("some_tool", None)])
        assert "some_tool" not in result

    def test_emptyList(self):
        result = _build_defer_mutations_frozenset([])
        assert result == frozenset()

    def test_multipleDeferMutationsTools(self):
        tools = [
            self._tool("request_graph_mutations", True),
            self._tool("create_storylet", True),
            self._tool("mark_entity_immutable", True),
            self._tool("modify_health", False),
        ]
        result = _build_defer_mutations_frozenset(tools)
        assert result == frozenset({
            "request_graph_mutations", "create_storylet", "mark_entity_immutable"
        })

    def test_createStoryletInDeferSet(self):
        """create_storylet with defer_mutations=True is in defer_mutations_set."""
        result = _build_defer_mutations_frozenset([self._tool("create_storylet", True)])
        assert "create_storylet" in result

    def test_revealSecretInDeferSet(self):
        """reveal_secret with defer_mutations=True is in defer_mutations_set."""
        result = _build_defer_mutations_frozenset([self._tool("reveal_secret", True)])
        assert "reveal_secret" in result

    def test_regularCombatToolsNotInDeferSet(self):
        """Regular combat tools (execute_melee_attack) are NOT in defer_mutations_set."""
        tools = [
            self._tool("execute_melee_attack", None),
            self._tool("modify_health", None),
            self._tool("use_ability_or_spell", None),
        ]
        for t in tools:
            result = _build_defer_mutations_frozenset([t])
            assert t.name not in result


# ------------------------------------------------------------------
# Tests: planner_tool_router
# ------------------------------------------------------------------
class TestPlannerToolRouter:
    def test_routesIngestionToolToIngestionNode(self):
        msg = _make_ai_msg([{"name": "run_ingestion_pipeline_tool", "args": {}, "id": "c1"}])
        assert planner_tool_router(_dm(messages=[msg])) == "ingestion"

    def test_routesHydrateCampaignToIngestionNode(self):
        msg = _make_ai_msg([{"name": "hydrate_campaign", "args": {}, "id": "c1"}])
        assert planner_tool_router(_dm(messages=[msg])) == "ingestion"

    def test_routesHydrateCompendiumToCompendiumHydrationNode(self):
        msg = _make_ai_msg([{"name": "hydrate_compendium", "args": {}, "id": "c1"}])
        assert planner_tool_router(_dm(messages=[msg])) == "compendium_hydration"

    def test_routesOtherToolsToClearMutations(self):
        msg = _make_ai_msg([{"name": "modify_health", "args": {}, "id": "c1"}])
        assert planner_tool_router(_dm(messages=[msg])) == "clear_mutations"

    def test_routesNoToolCallsToClearMutations(self):
        msg = _make_ai_msg([])
        assert planner_tool_router(_dm(messages=[msg])) == "clear_mutations"

    def test_routesEmptyMessagesToClearMutations(self):
        assert planner_tool_router(_dm(messages=[])) == "clear_mutations"


# ------------------------------------------------------------------
# Tests: qa_router
# ------------------------------------------------------------------
class TestQARouter:
    def test_routesCommitToCommitNode(self):
        assert qa_router(_dm(qa_feedback="COMMIT")) == "commit"

    def test_routesRejectionToNarrator(self):
        assert qa_router(_dm(qa_feedback="[HARD GUARDRAIL REJECTED]: ...")) == "narrator"

    def test_routesBelowMaxRevisionsToNarrator(self):
        assert qa_router(_dm(revision_count=1, qa_feedback="Try again")) == "narrator"


# ------------------------------------------------------------------
# Tests: drama_manager_router
# ------------------------------------------------------------------
class TestDramaManagerRouter:
    def test_routesToNarratorWhenStoryletActive(self):
        assert drama_manager_router(_dm(active_storylet_id="storylet-123")) == "narrator"

    def test_routesToPlannerWhenNoActiveStorylet(self):
        assert drama_manager_router(_dm(active_storylet_id=None)) == "planner"

    def test_routesToPlannerWhenActiveStoryletIdEmpty(self):
        assert drama_manager_router(_dm(active_storylet_id="")) == "planner"


# ------------------------------------------------------------------
# Tests: GraphRAG cache invalidation
# ------------------------------------------------------------------
class TestGraphRAGCacheInvalidation:
    def setup_method(self):
        from graph import _grag_cache, _kg_constraints_cache
        _grag_cache.clear()
        _kg_constraints_cache.clear()

    def test_invalidateOneVault(self):
        from graph import _grag_cache
        _grag_cache[("vault1", "char", 1)] = (0.0, "cached_result")
        _grag_cache[("vault2", "char", 1)] = (0.0, "cached_result2")

        _invalidate_grag_cache("vault1")

        assert ("vault1", "char", 1) not in _grag_cache
        assert ("vault2", "char", 1) in _grag_cache

    def test_invalidateAllVaults(self):
        from graph import _grag_cache
        _grag_cache[("vault1", "char", 1)] = (0.0, "r1")
        _grag_cache[("vault2", "char", 1)] = (0.0, "r2")

        _invalidate_grag_cache(None)

        assert len(_grag_cache) == 0


# ------------------------------------------------------------------
# Tests: build_graph — compilation with empty tools list
# (ToolNode requires real tool objects, so only test empty list compilation)
# ------------------------------------------------------------------
class TestBuildGraphCompilation:
    def test_compilesWithEmptyToolsList(self):
        """build_graph compiles successfully with an empty tools list."""
        mock_draft_llm = MagicMock()
        mock_qa_llm = MagicMock()

        graph = build_graph(
            draft_llm=mock_draft_llm,
            qa_llm=mock_qa_llm,
            master_tools_list=[],
            checkpointer=MemorySaver(),
        )
        assert graph is not None


# ------------------------------------------------------------------
# Tests: DMState transitions — known routing paths
# ------------------------------------------------------------------
class TestDMStateTransitions:
    def test_qaFeedbackCycle_CommitPath(self):
        """COMMIT routes to commit_node."""
        assert qa_router(_dm(qa_feedback="COMMIT")) == "commit"

    def test_qaFeedbackCycle_RejectionPath(self):
        """QA rejection routes back to narrator."""
        state = _dm(qa_feedback="[HARD GUARDRAIL REJECTED]: HP was wrong", revision_count=0)
        assert qa_router(state) == "narrator"

    def test_storyletActivationRoutesToNarrator(self):
        """Storylet active → narrator router."""
        assert drama_manager_router(_dm(active_storylet_id="storylet-abc")) == "narrator"

    def test_noStoryletRoutesToPlanner(self):
        """No active storylet → planner router."""
        assert drama_manager_router(_dm(active_storylet_id=None)) == "planner"

    def test_plannerRoutesSeparation_ingestion(self):
        """Planner tool router: ingestion → ingestion_node."""
        ingestion_msg = _make_ai_msg([{"name": "run_ingestion_pipeline_tool", "args": {}, "id": "c1"}])
        assert planner_tool_router(_dm(messages=[ingestion_msg])) == "ingestion"

    def test_plannerRoutesSeparation_normal(self):
        """Planner tool router: normal tools → clear_mutations."""
        normal_msg = _make_ai_msg([{"name": "modify_health", "args": {}, "id": "c2"}])
        assert planner_tool_router(_dm(messages=[normal_msg])) == "clear_mutations"

    def test_plannerRoutesSeparation_compendium(self):
        """Planner tool router: hydrate_compendium → compendium_hydration."""
        compendium_msg = _make_ai_msg([{"name": "hydrate_compendium", "args": {}, "id": "c3"}])
        assert planner_tool_router(_dm(messages=[compendium_msg])) == "compendium_hydration"
