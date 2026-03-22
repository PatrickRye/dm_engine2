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
    extract_entities_from_text,
    EffectAnnotationPipeline,
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


class TestExtractEntitiesDeterministic:
    """Deterministic extraction from raw DM notes (no LLM)."""

    @pytest.mark.asyncio
    async def test_extract_entities_from_text_returns_nodes_and_edges(self):
        """extract_entities_from_text with no LLM returns deterministic results."""
        from ingestion_pipeline import extract_entities_from_text

        notes = """
        ## NPCs
        Lord Vader, a dark lord serving the Emperor.
        ## Items
        The Shadowblade, owned by Lord Vader.
        """
        nodes, edges = await extract_entities_from_text(notes, vault_path="test_vault", llm=None)
        # Should extract at least the named entities
        assert len(nodes) >= 1
        names = {n.name for n in nodes}
        assert "Lord Vader" in names or "Vader" in names

    @pytest.mark.asyncio
    async def test_extract_entities_infers_relationship(self):
        """Deterministic extractor picks up 'owns' relationship pattern."""
        from ingestion_pipeline import extract_entities_from_text

        notes = "Lord Vader owns the Shadowblade."
        nodes, edges = await extract_entities_from_text(notes, vault_path="test_vault", llm=None)
        assert len(nodes) >= 2
        # Should have at least one edge (possesses/owned_by)
        assert len(edges) >= 1

    @pytest.mark.asyncio
    async def test_extract_entities_empty_text(self):
        """Empty notes produce no nodes or edges."""
        from ingestion_pipeline import extract_entities_from_text

        nodes, edges = await extract_entities_from_text("", vault_path="test_vault", llm=None)
        assert nodes == []
        assert edges == []


class TestEffectAnnotationPipeline:
    """EffectAnnotationPipeline wraps annotate_storylet_effects with KG application."""

    @pytest.mark.asyncio
    async def test_annotate_returns_effect_spec(self):
        """annotate() returns an EffectAnnotationSpec."""
        pipeline = EffectAnnotationPipeline(llm=None)
        result = await pipeline.annotate(
            "Lord Vader hands the map to the party.",
            vault_path="test_vault",
        )
        # No LLM → empty result
        assert isinstance(result, EffectAnnotationSpec)
        assert result.mutations == []

    def test_apply_mutation_returns_bool(self):
        """apply_mutation returns True when mutation executes without exception."""
        from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType
        from storylet import GraphMutation

        kg = KnowledgeGraph()
        node = KnowledgeGraphNode(node_type=GraphNodeType.NPC, name="Vader", attributes={}, tags=set())
        kg.add_node(node)

        pipeline = EffectAnnotationPipeline(llm=None)
        mut = GraphMutation(mutation_type="add_tag", node_name="Vader", value="imperial")
        applied = pipeline.apply_mutation(kg, mut)
        assert applied is True
        assert "imperial" in node.tags

    def test_apply_effects_returns_counts(self):
        """apply_effects returns (applied, failed) counts (both as execute reports them)."""
        from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType
        from storylet import GraphMutation

        kg = KnowledgeGraph()
        node = KnowledgeGraphNode(node_type=GraphNodeType.NPC, name="Vader", attributes={}, tags=set())
        kg.add_node(node)

        pipeline = EffectAnnotationPipeline(llm=None)
        mutations = [
            GraphMutation(mutation_type="add_tag", node_name="Vader", value="dark"),
            GraphMutation(mutation_type="add_tag", node_name="Vader", value="sith"),
        ]
        applied, failed = pipeline.apply_effects(kg, mutations)
        # execute() raises no exception for either; both count as applied
        assert applied == 2
        assert failed == 0
        assert "dark" in node.tags
        assert "sith" in node.tags

    def test_attach_to_storylet_missing_returns_false(self):
        """attach_to_storylet returns False when storylet not found."""
        pipeline = EffectAnnotationPipeline(llm=None)
        result = pipeline.attach_to_storylet(
            "NonExistent Storylet",
            [],
            vault_path="test_vault",
        )
        assert result is False

    def test_attach_to_storylet_success(self):
        """attach_to_storylet attaches mutations to an existing storylet."""
        from storylet_registry import StoryletRegistry
        from storylet import Storylet, GraphMutation, StoryletEffect

        reg = StoryletRegistry()
        s = Storylet(name="Test Attach Storylet", content="Test content.")
        reg.register(s)

        # Patch get_storylet_registry to return our registry
        import ingestion_pipeline
        original = ingestion_pipeline.get_storylet_registry
        ingestion_pipeline.get_storylet_registry = lambda vp: reg
        try:
            pipeline = EffectAnnotationPipeline(llm=None)
            mut = GraphMutation(mutation_type="add_tag", node_name="Vader", value="imperial")
            result = pipeline.attach_to_storylet(
                "Test Attach Storylet",
                [mut],
                vault_path="test_vault",
            )
            assert result is True
            assert len(s.effects) == 1
        finally:
            ingestion_pipeline.get_storylet_registry = original

    @pytest.mark.asyncio
    async def test_run_returns_summary_dict(self):
        """run() returns a summary dict with expected keys."""
        pipeline = EffectAnnotationPipeline(llm=None)
        summary = await pipeline.run(
            {"Some Storylet": "The resolution text."},
            vault_path="test_vault",
        )
        assert "storylets_annotated" in summary
        assert "mutations_applied" in summary
        assert "mutations_failed" in summary
        assert "storylets_updated" in summary
        assert "summaries" in summary


class TestStoryletRegistryGetByName:
    """get_by_name finds storylets by exact name (case-insensitive)."""

    def test_get_by_name_found(self):
        from storylet_registry import StoryletRegistry
        from storylet import Storylet

        reg = StoryletRegistry()
        s = Storylet(name="The Dark Lord", content="Lord Vader reveals himself.")
        reg.register(s)

        found = reg.get_by_name("The Dark Lord")
        assert found is not None
        assert found.name == "The Dark Lord"

    def test_get_by_name_case_insensitive(self):
        from storylet_registry import StoryletRegistry
        from storylet import Storylet

        reg = StoryletRegistry()
        s = Storylet(name="Lord Vader", content="Appears in a flash.")
        reg.register(s)

        found = reg.get_by_name("LORD VADER")
        assert found is not None
        assert found.name == "Lord Vader"

    def test_get_by_name_not_found(self):
        from storylet_registry import StoryletRegistry

        reg = StoryletRegistry()
        found = reg.get_by_name("Nobody")
        assert found is None
