"""
Tests for EmergentWorldBuilder and emergent worldbuilding tools.
"""
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate
from storylet_registry import StoryletRegistry
from storylet import TensionLevel


class TestEmergentWorldBuilder:
    """Tests for the EmergentWorldBuilder class."""

    @pytest.fixture(autouse=True)
    def fresh_registry(self):
        """Each test gets a fresh in-memory KG and registry."""
        import ingestion_pipeline

        # Create fresh KG + registry for this test
        fresh_kg = KnowledgeGraph()
        fresh_reg = StoryletRegistry()
        vault = "test_ewb_vault"

        # Patch both getter functions to return our fresh instances
        with patch.object(ingestion_pipeline, "get_knowledge_graph", return_value=fresh_kg), \
             patch.object(ingestion_pipeline, "get_storylet_registry", return_value=fresh_reg):
            yield {"kg": fresh_kg, "reg": fresh_reg, "vault": vault}

    @pytest.mark.asyncio
    async def test_on_entity_created_mary_barkeep(self, fresh_registry):
        """Entity 'Mary' added to KG gets storylets and edges."""
        from ingestion_pipeline import EmergentWorldBuilder

        kg = fresh_registry["kg"]
        reg = fresh_registry["reg"]
        vault = fresh_registry["vault"]

        # Pre-load the KG with a tavern location
        tavern_node = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.LOCATION,
            name="The Prancing Pony",
            attributes={"description": "A cozy tavern."},
        )
        kg.add_node(tavern_node)

        # Add Mary to the KG
        mary_node = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Mary",
            attributes={"description": "A secretive barkeep."},
        )
        kg.add_node(mary_node)

        builder = EmergentWorldBuilder(llm=None, vault_path=vault)
        report = await builder.on_entity_created(
            entity_name="Mary",
            entity_type="npc",
            context="Mary works as a barkeep at The Prancing Pony. She serves the Thieves Guild.",
        )

        assert report.entity_name == "Mary"
        assert report.storylets_created == 3  # 3 quest hooks generated

        # Verify storylets registered
        registered_names = [s.name for s in reg.get_all()]
        assert any("Mary" in name and "personal secret" in name for name in registered_names), \
            f"Expected storylet not found. Registered: {registered_names}"
        assert any("Mary" in name and "missing" in name for name in registered_names), \
            f"Expected storylet not found. Registered: {registered_names}"
        assert any("Mary" in name and "debt" in name for name in registered_names), \
            f"Expected storylet not found. Registered: {registered_names}"

        # Verify Mary has at least one edge (located_in or faction)
        mary_uuid = kg.get_node_by_name("Mary").node_uuid
        mary_neighbors = kg.get_neighbors(mary_uuid)
        assert len(mary_neighbors) >= 1

    @pytest.mark.asyncio
    async def test_on_entity_created_missing_entity_returns_warning(self, fresh_registry):
        """Report warns when entity not in KG."""
        from ingestion_pipeline import EmergentWorldBuilder

        vault = fresh_registry["vault"]
        builder = EmergentWorldBuilder(llm=None, vault_path=vault)
        report = await builder.on_entity_created(entity_name="Ghost NPC")

        assert "Ghost NPC" in report.warnings[0] and "not in KG" in report.warnings[0]

    @pytest.mark.asyncio
    async def test_generate_side_quest_storylets_no_duplicates(self, fresh_registry):
        """Calling on same entity twice doesn't create duplicate storylets."""
        from ingestion_pipeline import EmergentWorldBuilder

        kg = fresh_registry["kg"]
        vault = fresh_registry["vault"]

        node = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Bob",
            attributes={},
        )
        kg.add_node(node)

        builder = EmergentWorldBuilder(llm=None, vault_path=vault)
        report1 = await builder.on_entity_created(entity_name="Bob")
        report2 = await builder.on_entity_created(entity_name="Bob")

        assert report1.storylets_created == 3
        assert report2.storylets_created == 0  # Already exist

    def test_infer_world_edges_creates_location_node(self, fresh_registry):
        """Location keyword in context triggers location node creation."""
        from ingestion_pipeline import EmergentWorldBuilder

        kg = fresh_registry["kg"]
        reg = fresh_registry["reg"]
        vault = fresh_registry["vault"]

        node = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Innkeeper",
            attributes={},
        )
        kg.add_node(node)

        builder = EmergentWorldBuilder(llm=None, vault_path=vault)
        edges = builder._infer_world_edges(
            node,
            "The innkeeper works at a tavern in the city.",
            kg,
            reg,
        )

        assert edges >= 1
        # Should have created a location node (city from "in the city")
        assert kg.get_node_by_name("city") is not None

    def test_infer_world_edges_faction_connection(self, fresh_registry):
        """Faction keyword in context creates faction edge."""
        from ingestion_pipeline import EmergentWorldBuilder

        kg = fresh_registry["kg"]
        reg = fresh_registry["reg"]
        vault = fresh_registry["vault"]

        node = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Spy",
            attributes={},
        )
        kg.add_node(node)

        builder = EmergentWorldBuilder(llm=None, vault_path=vault)
        edges = builder._infer_world_edges(
            node,
            "The spy serves the Thieves Guild as an informant.",
            kg,
            reg,
        )

        assert edges >= 1
        guild = kg.get_node_by_name("Thieves Guild")
        assert guild is not None


class TestEmergentEntityReport:
    """Tests for EmergentEntityReport schema."""

    def test_report_fields(self):
        from ingestion_pipeline import EmergentEntityReport

        report = EmergentEntityReport(
            entity_name="Test",
            storylets_created=2,
            edges_created=3,
        )
        assert report.entity_name == "Test"
        assert report.storylets_created == 2
        assert report.edges_created == 3
        assert report.warnings == []

    def test_report_with_warnings(self):
        from ingestion_pipeline import EmergentEntityReport

        report = EmergentEntityReport(
            entity_name="Ghost",
            warnings=["Entity 'Ghost' not in KG."],
        )
        assert len(report.warnings) == 1
