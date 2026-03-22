"""
Tests for the NLP Ingestion Pipeline (Phase 2).

Tests:
1. NPCEntitySpec and KGEdgeSpec schema validation
2. StoryletSpec schema validation
3. EffectAnnotationSpec schema validation
4. _build_npc_system_prompt returns non-empty string
5. _build_storylet_prerequisites converts query dicts correctly
6. Deterministic fallback (no LLM) returns empty results
7. ingest_campaign_narrative returns empty list when no LLM
8. annotate_storylet_effects returns empty spec when no LLM
9. Full pipeline summary dict has expected keys
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from pydantic import ValidationError

from ingestion_pipeline import (
    NPCEntitySpec,
    KGEdgeSpec,
    StoryletSpec,
    EffectAnnotationSpec,
    _build_npc_system_prompt,
    _build_storylet_prerequisites,
    ingest_npc_lore,
    ingest_campaign_narrative,
    annotate_storylet_effects,
    run_ingestion_pipeline,
)


class TestNPCEntitySpec:
    def test_valid_spec(self):
        spec = NPCEntitySpec(
            name="Lord Vader",
            node_type="npc",
            aliases=["The Dark Lord"],
            description="A imposing figure in dark armor",
            bio="Once a Jedi, turned to the dark side",
            connections="Allied with the Empire",
            behavioral_dials={"cruelty": 0.9, "cunning": 0.8},
            tags={"imperial", "force_user"},
            is_immutable=True,
        )
        assert spec.name == "Lord Vader"
        assert spec.behavioral_dials["cruelty"] == 0.9

    def test_defaults(self):
        spec = NPCEntitySpec(name="Merchant Bob")
        assert spec.node_type == "npc"
        assert spec.aliases == []
        assert spec.behavioral_dials == {}
        assert spec.is_immutable is False


class TestKGEdgeSpec:
    def test_valid_edge(self):
        spec = KGEdgeSpec(
            subject_name="Lord Vader",
            predicate="hostile_toward",
            object_name="The Jedi",
            weight=1.0,
        )
        assert spec.predicate == "hostile_toward"
        assert spec.weight == 1.0


class TestStoryletSpec:
    def test_valid_storylet(self):
        spec = StoryletSpec(
            name="The Betrayal at Cloud City",
            content="Lord Vader reveals his true plans to Luke.",
            tension_level="cliffhanger",
            priority_override=10,
            tags={"main_quest", "vader"},
        )
        assert spec.tension_level == "cliffhanger"
        assert spec.priority_override == 10

    def test_narrative_beats_alias(self):
        spec = StoryletSpec(
            name="Test Storylet",
            content="Content",
            **{"narrative beats": ["beat1", "beat2"]},
        )
        assert spec.narrative_beats == ["beat1", "beat2"]


class TestEffectAnnotationSpec:
    def test_valid_effects(self):
        spec = EffectAnnotationSpec(
            mutations=[
                {
                    "mutation_type": "add_edge",
                    "node_name": "Lord Vader",
                    "predicate": "allied_with",
                    "target_name": "The Party",
                }
            ],
            summary="Lord Vader allied with the party",
        )
        assert len(spec.mutations) == 1
        assert spec.mutations[0]["mutation_type"] == "add_edge"

    def test_empty_effects(self):
        spec = EffectAnnotationSpec()
        assert spec.mutations == []
        assert spec.summary == ""


class TestBuildNpcSystemPrompt:
    def test_returns_non_empty_string(self):
        prompt = _build_npc_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100
        assert "NPCEntitySpec" in prompt


class TestBuildStoryletPrerequisites:
    def test_converts_node_exists_query(self):
        query_dicts = [
            {
                "query_type": "node_exists",
                "entity_name": "Lord Vader",
            }
        ]
        prereqs = _build_storylet_prerequisites(query_dicts)
        assert prereqs.any_of is not None
        assert len(prereqs.any_of) == 1
        assert prereqs.any_of[0].query_type == "node_exists"

    def test_empty_list_returns_empty_prereqs(self):
        prereqs = _build_storylet_prerequisites([])
        assert prereqs.any_of == []


class TestDeterministicFallback:
    """Without an LLM, pipeline functions return empty/deterministic results."""

    @pytest.mark.asyncio
    async def test_ingest_npc_lore_no_llm_returns_empty(self):
        nodes, edges = await ingest_npc_lore("Some lore text", vault_path="test_vault", llm=None)
        assert nodes == []
        assert edges == []

    @pytest.mark.asyncio
    async def test_ingest_campaign_narrative_no_llm_returns_empty(self):
        storylets = await ingest_campaign_narrative(
            "Some narrative text", vault_path="test_vault", llm=None
        )
        assert storylets == []

    @pytest.mark.asyncio
    async def test_annotate_storylet_effects_no_llm_returns_empty(self):
        result = await annotate_storylet_effects(
            "Resolution text", vault_path="test_vault", llm=None
        )
        assert result.mutations == []
        assert result.summary == ""


class TestRunIngestionPipelineSummary:
    """run_ingestion_pipeline returns a summary dict with expected keys."""

    @pytest.mark.asyncio
    async def test_summary_has_expected_keys(self):
        # LLM is None → all steps are no-ops, but we get a summary dict back
        summary = await run_ingestion_pipeline(
            vault_path="test_vault",
            llm=None,
        )
        assert "nodes_added" in summary
        assert "edges_added" in summary
        assert "storylets_created" in summary
        assert "effects_annotated" in summary
        assert summary["nodes_added"] == 0
        assert summary["storylets_created"] == 0
