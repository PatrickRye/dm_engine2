"""
Tests for Gap 9: NPC Dial Extraction and GraphRAG Injection

Verifies that:
1. extract_npc_dials() correctly extracts behavioral dials from biography text
2. KnowledgeGraphNode.npc_dials field is correctly set and serialized
3. NPC dials appear in the GraphRAG context injected into narrator's prompt

Architecture under test:
  graph.py::extract_npc_dials() — keyword heuristic dial extraction
  knowledge_graph.py::KnowledgeGraphNode.npc_dials — new field
  graph.py::_get_grag_context() — now includes NPC dials
"""

import pytest
from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType
from graph import extract_npc_dials, _get_grag_context


class TestExtractNpcDials:
    """Test the NPC dial extraction function."""

    def test_greed_high_on_greedy_text(self):
        """Greedy biography → greed dial high."""
        bio = "The merchant is notoriously greedy, always haggling for higher prices."
        dials = extract_npc_dials(bio)
        assert "greed" in dials
        assert dials["greed"] > 0.5

    def test_loyalty_high_on_loyal_text(self):
        """Loyal biography → loyalty dial high."""
        bio = "Sir Cedric is utterly loyal, sworn to protect the crown until death."
        dials = extract_npc_dials(bio)
        assert "loyalty" in dials
        assert dials["loyalty"] > 0.5

    def test_courage_low_on_coward_text(self):
        """Coward biography → courage dial low."""
        bio = "Despite his size, the goblin is a coward who flees at the first sign of danger."
        dials = extract_npc_dials(bio)
        assert "courage" in dials
        assert dials["courage"] < 0.5

    def test_cruelty_high_on_cruel_text(self):
        """Cruel biography → cruelty dial high."""
        bio = "The warlord is known for his cruelty, showing no mercy to prisoners."
        dials = extract_npc_dials(bio)
        assert "cruelty" in dials
        assert dials["cruelty"] > 0.5

    def test_piety_high_on_devout_text(self):
        """Devout biography → piety dial high."""
        bio = "The high priestess is devout, praying daily and living by sacred law."
        dials = extract_npc_dials(bio)
        assert "piety" in dials
        assert dials["piety"] > 0.5

    def test_cunning_high_on_clever_text(self):
        """Cunning biography → cunning dial high."""
        bio = "The rogue is cunning and shrewd, always three steps ahead of enemies."
        dials = extract_npc_dials(bio)
        assert "cunning" in dials
        assert dials["cunning"] > 0.5

    def test_empty_text_returns_empty(self):
        """Empty biography → empty dials."""
        dials = extract_npc_dials("")
        assert dials == {}

    def test_neutral_text_returns_near_empty(self):
        """Neutral biography → near-default dials."""
        bio = "The merchant sells goods at the market."
        dials = extract_npc_dials(bio)
        # No strong indicators → no dials (abs(v-0.5) <= 0.05 is filtered)
        assert dials == {}

    def test_multiple_dials_extracted(self):
        """Biography with multiple traits → multiple dials."""
        bio = (
            "The dark lord is cruel and greedy, always seeking more power. "
            "Yet despite his evil nature, he remains strangely loyal to his lieutenant."
        )
        dials = extract_npc_dials(bio)
        assert "cruelty" in dials
        assert "greed" in dials


class TestKnowledgeGraphNodeNpcDials:
    """Test that KnowledgeGraphNode.npc_dials field works correctly."""

    def test_npc_dials_field_exists(self):
        """KnowledgeGraphNode accepts npc_dials field."""
        node = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Test NPC",
            attributes={},
            tags=set(),
            npc_dials={"greed": 0.8, "loyalty": 0.5},
        )
        assert node.npc_dials["greed"] == 0.8
        assert node.npc_dials["loyalty"] == 0.5

    def test_npc_dials_defaults_to_empty_dict(self):
        """Npc_dials defaults to empty dict."""
        node = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Test NPC",
            attributes={},
            tags=set(),
        )
        assert node.npc_dials == {}

    def test_npc_dials_serializes_correctly(self):
        """NPC dials survive model_dump serialization."""
        node = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Test NPC",
            attributes={},
            tags=set(),
            npc_dials={"courage": 0.3},
        )
        data = node.model_dump()
        assert data["npc_dials"]["courage"] == 0.3


class TestGragContextIncludesNpcDials:
    """Test that GraphRAG context includes NPC behavioral dials."""

    def test_npc_dials_appear_in_grag_context(self):
        """When an NPC has dials set, they appear in the GraphRAG context."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Lord Vader",
            attributes={},
            tags=set(),
            npc_dials={"cruelty": 0.9, "cunning": 0.8},
        )
        kg.add_node(npc)

        ctx = _get_grag_context(kg, active_character="Nobody")
        assert "Lord Vader" in ctx
        assert "cruelty" in ctx.lower()
        assert "cunning" in ctx.lower()

    def test_npc_without_dials_still_works(self):
        """NPC without dials (empty dict) doesn't break context."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Simple Merchant",
            attributes={},
            tags=set(),
            npc_dials={},
        )
        kg.add_node(npc)

        ctx = _get_grag_context(kg, active_character="Nobody")
        assert "Simple Merchant" in ctx
        assert "Behavioral Dials" not in ctx  # Should not appear when empty
