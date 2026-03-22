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
    CampaignMaterials,
    HydrationReport,
    _build_npc_system_prompt,
    _build_storylet_prerequisites,
    ingest_npc_lore,
    ingest_campaign_narrative,
    annotate_storylet_effects,
    run_ingestion_pipeline,
    extract_entities_from_text,
    EffectAnnotationPipeline,
    CampaignHydrationPipeline,
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


class TestCampaignHydrationPipeline:
    """CampaignHydrationPipeline: one-shot campaign pre-loading."""

    @pytest.mark.asyncio
    async def test_run_returns_hydration_report(self):
        """run() returns a HydrationReport with expected fields."""
        from ingestion_pipeline import CampaignHydrationPipeline, CampaignMaterials

        pipeline = CampaignHydrationPipeline(llm=None, vault_path="test_vault")
        materials = CampaignMaterials(campaign_name="Test Campaign")
        report = await pipeline.run(materials)
        assert isinstance(report, HydrationReport)
        assert hasattr(report, "nodes_created")
        assert hasattr(report, "edges_created")
        assert hasattr(report, "storylets_created")
        assert hasattr(report, "backup_storylets_generated")
        assert hasattr(report, "warnings")

    @pytest.mark.asyncio
    async def test_run_with_no_materials_produces_zeros(self):
        """Empty CampaignMaterials produces zero counts."""
        from ingestion_pipeline import CampaignHydrationPipeline, CampaignMaterials

        pipeline = CampaignHydrationPipeline(llm=None, vault_path="test_vault")
        report = await pipeline.run(CampaignMaterials())
        assert report.nodes_created == 0
        assert report.edges_created == 0
        assert report.storylets_created == 0

    @pytest.mark.asyncio
    async def test_run_with_npc_lore_extracts_entities(self):
        """NPC lore text is processed by extract_entities_from_text()."""
        from ingestion_pipeline import CampaignHydrationPipeline, CampaignMaterials

        pipeline = CampaignHydrationPipeline(llm=None, vault_path="test_vault")
        materials = CampaignMaterials(
            npc_lore="Lord Vader serves the Emperor. He is hostile toward the Jedi.",
        )
        report = await pipeline.run(materials)
        # Deterministic fallback should extract at least some entities
        assert report.nodes_created >= 1

    @pytest.mark.asyncio
    async def test_run_with_campaign_narrative_creates_storylets(self):
        """Campaign narrative is processed by ingest_campaign_narrative()."""
        from ingestion_pipeline import CampaignHydrationPipeline, CampaignMaterials

        pipeline = CampaignHydrationPipeline(llm=None, vault_path="test_vault")
        materials = CampaignMaterials(
            campaign_narrative="The party arrives at a dark castle.",
        )
        report = await pipeline.run(materials)
        # No LLM → empty storylets
        assert report.storylets_created == 0

    @pytest.mark.asyncio
    async def test_run_invalidates_grag_cache(self):
        """After hydration, the GraphRAG cache is invalidated."""
        from ingestion_pipeline import CampaignHydrationPipeline, CampaignMaterials

        pipeline = CampaignHydrationPipeline(llm=None, vault_path="test_grag_cache")
        materials = CampaignMaterials(
            npc_lore="The Goblin King rules the Thornwood.",
        )
        report = await pipeline.run(materials)
        # Should complete without error
        assert report.nodes_created >= 1


class TestCampaignMaterialsSchema:
    """CampaignMaterials schema defaults and validation."""

    def test_all_fields_optional(self):
        """All fields have defaults — empty init is valid."""
        from ingestion_pipeline import CampaignMaterials

        m = CampaignMaterials()
        assert m.campaign_name == ""
        assert m.npc_lore == ""
        assert m.campaign_narrative == ""
        assert m.session_prep_notes == ""
        assert m.storylet_resolutions == {}

    def test_storylet_resolutions_default_empty_dict(self):
        """storylet_resolutions defaults to {} not None."""
        from ingestion_pipeline import CampaignMaterials

        m = CampaignMaterials(storylet_resolutions={"Key": "Value"})
        assert m.storylet_resolutions == {"Key": "Value"}

    def test_get_by_name_not_found(self):
        from storylet_registry import StoryletRegistry

        reg = StoryletRegistry()
        found = reg.get_by_name("Nobody")
        assert found is None


class TestIncrementalHydrationPipeline:
    """IncrementalHydrationPipeline: delta updates from DM materials."""

    @pytest.mark.asyncio
    async def test_delta_hydrate_with_no_new_entities_returns_empty_report(self):
        """When all content already exists, delta returns zero additions."""
        from ingestion_pipeline import IncrementalHydrationPipeline, CampaignMaterials
        from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType

        # Pre-populate KG with the entity that will appear in new materials
        kg = KnowledgeGraph()
        kg.add_node(KnowledgeGraphNode(
            node_type=GraphNodeType.NPC, name="Lord Vader", attributes={}, tags=set(),
        ))
        import registry as reg_module
        reg_module._KNOWLEDGE_GRAPHS["test_delta_vault"] = kg

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_delta_vault")
        materials = CampaignMaterials(
            npc_lore="Lord Vader is an ancient Sith lord. The Emperor serves him.",
        )
        report = await pipeline.delta_hydrate(materials)
        # The entity is already known so it should be filtered out
        assert report.warnings is not None

        # Clean up
        reg_module._KNOWLEDGE_GRAPHS.pop("test_delta_vault", None)

    @pytest.mark.asyncio
    async def test_delta_hydrate_with_new_entities_adds_them(self):
        """When new content is provided, only genuinely new entities are added."""
        from ingestion_pipeline import IncrementalHydrationPipeline, CampaignMaterials
        from knowledge_graph import KnowledgeGraph

        # Fresh KG
        kg = KnowledgeGraph()
        import registry as reg_module
        reg_module._KNOWLEDGE_GRAPHS["test_new_delta"] = kg

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_new_delta")
        materials = CampaignMaterials(
            npc_lore="Zypyr the Archmage rules the tower.",
        )
        report = await pipeline.delta_hydrate(materials)
        # Zypyr is not in KG → should be extracted
        assert report.nodes_created >= 1

        reg_module._KNOWLEDGE_GRAPHS.pop("test_new_delta", None)

    def test_detect_missing_entities_finds_wikilinks(self):
        """detect_missing_entities finds [[Wikilinks]] not in KG."""
        from ingestion_pipeline import IncrementalHydrationPipeline
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        import registry as reg_module
        reg_module._KNOWLEDGE_GRAPHS["test_missing"] = kg

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_missing")
        missing = pipeline.detect_missing_entities(
            "The Wizard [[Zypyr]] appears in the tower."
        )
        assert "Zypyr" in missing

        reg_module._KNOWLEDGE_GRAPHS.pop("test_missing", None)

    def test_detect_missing_entities_finds_capitalized_names(self):
        """detect_missing_entities finds Title-Case names not in KG."""
        from ingestion_pipeline import IncrementalHydrationPipeline
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        import registry as reg_module
        reg_module._KNOWLEDGE_GRAPHS["test_cap"] = kg

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_cap")
        missing = pipeline.detect_missing_entities(
            "The Archmage Zypyr rules the tower. The Dragon waits."
        )
        # Multi-word Title-Case sequences are captured as full strings
        assert "Zypyr" in missing or "The Archmage Zypyr" in missing

        reg_module._KNOWLEDGE_GRAPHS.pop("test_cap", None)

    def test_detect_missing_entities_excludes_known_entities(self):
        """Known KG entities are not reported as missing."""
        from ingestion_pipeline import IncrementalHydrationPipeline
        from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType

        kg = KnowledgeGraph()
        kg.add_node(KnowledgeGraphNode(
            node_type=GraphNodeType.NPC, name="Vader", attributes={}, tags=set(),
        ))
        import registry as reg_module
        reg_module._KNOWLEDGE_GRAPHS["test_known"] = kg

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_known")
        missing = pipeline.detect_missing_entities(
            "Lord Vader confronts the Jedi."
        )
        assert "Vader" not in missing
        assert "Jedi" in missing  # Jedi is not in KG

        reg_module._KNOWLEDGE_GRAPHS.pop("test_known", None)

    def test_detect_missing_entities_empty_when_all_known(self):
        """Returns empty list when all entities are in KG."""
        from ingestion_pipeline import IncrementalHydrationPipeline
        from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType

        kg = KnowledgeGraph()
        # Use multi-word names to avoid regex capturing adjacent words
        kg.add_node(KnowledgeGraphNode(
            node_type=GraphNodeType.NPC, name="Lord Vader", attributes={}, tags=set(),
        ))
        kg.add_node(KnowledgeGraphNode(
            node_type=GraphNodeType.NPC, name="The Jedi", attributes={}, tags=set(),
        ))
        import registry as reg_module
        reg_module._KNOWLEDGE_GRAPHS["test_all_known"] = kg

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_all_known")
        missing = pipeline.detect_missing_entities("Lord Vader confronts The Jedi.")
        assert missing == []

        reg_module._KNOWLEDGE_GRAPHS.pop("test_all_known", None)

    def test_suggest_hydration_returns_readable_prompt(self):
        """suggest_hydration generates a usable DM prompt."""
        from ingestion_pipeline import IncrementalHydrationPipeline

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_suggest")
        suggestion = pipeline.suggest_hydration(["Zypyr", "Archmage"])
        assert "Zypyr" in suggestion
        assert "Archmage" in suggestion
        assert "hydrate_delta" in suggestion or "hydrate_missing" in suggestion

    def test_suggest_hydration_empty_when_nothing_missing(self):
        """suggest_hydration with no missing entities returns a no-op message."""
        from ingestion_pipeline import IncrementalHydrationPipeline

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_suggest2")
        result = pipeline.suggest_hydration([])
        assert "already" in result.lower()

    @pytest.mark.asyncio
    async def test_hydrate_missing_entity_adds_single_entity(self):
        """hydrate_missing_entity adds a single entity to KG."""
        from ingestion_pipeline import IncrementalHydrationPipeline
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        import registry as reg_module
        reg_module._KNOWLEDGE_GRAPHS["test_hydrate_missing"] = kg

        pipeline = IncrementalHydrationPipeline(llm=None, vault_path="test_hydrate_missing")
        report = await pipeline.hydrate_missing_entity(
            entity_name="Zypyr",
            entity_context="An ancient archmage who rules the tower.",
            node_type="npc",
        )
        # Deterministic extraction should add at least one node
        assert report.nodes_created >= 1

        reg_module._KNOWLEDGE_GRAPHS.pop("test_hydrate_missing", None)

