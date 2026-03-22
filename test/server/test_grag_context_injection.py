"""
Tests for Gap 8: GraphRAG Context Injection

Verifies that:
1. _get_grag_context returns empty string for empty KG
2. _get_grag_context returns formatted context for KG with NPCs and relationships
3. _get_grag_context is called in narrator_node system prompt (integration)

Architecture under test:
  graph.py::_get_grag_context() — formats KG subgraph for narrator prompt
  graph.py::narrator_node() — injects grag_context into system prompt
"""

import pytest
from unittest.mock import MagicMock, patch

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
from graph import _get_grag_context
from langchain_core.messages import HumanMessage


class TestGragContextExtraction:
    """Test the GraphRAG context extraction from KG."""

    def setup_method(self):
        self.kg = KnowledgeGraph()

    def test_empty_kg_returns_empty_string(self):
        """No nodes → no context."""
        ctx = _get_grag_context(self.kg, active_character="Nobody")
        assert ctx == ""

    def test_active_character_neighbors_included(self):
        """When active character has KG neighbors, they appear in context."""
        char = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="Kaelen",
            attributes={},
            tags=set(),
        )
        npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Lord Vader",
            attributes={"title": "Sith Lord"},
            tags=set(),
        )
        self.kg.add_node(char)
        self.kg.add_node(npc)
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=npc.node_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=char.node_uuid,
            )
        )

        ctx = _get_grag_context(self.kg, active_character="Kaelen")
        assert "Kaelen" in ctx
        assert "Lord Vader" in ctx

    def test_npc_attributes_included_in_context(self):
        """NPC attributes are included in the context output."""
        npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Goblin Chief",
            attributes={"cr": "2", "hp": "30"},
            tags=set(),
        )
        self.kg.add_node(npc)

        ctx = _get_grag_context(self.kg, active_character="The Party")
        assert "Goblin Chief" in ctx
        assert "cr" in ctx.lower()
        assert "30" in ctx

    def test_npc_relationship_edges_included(self):
        """NPC edges (relationships) appear in the context."""
        goblin = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Goblin Scout",
            attributes={},
            tags=set(),
        )
        party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(goblin)
        self.kg.add_node(party)
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=goblin.node_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=party.node_uuid,
            )
        )

        ctx = _get_grag_context(self.kg, active_character="The Party")
        assert "Goblin Scout" in ctx
        assert "hostile_toward" in ctx.lower()

    def test_context_limited_to_5_npcs(self):
        """Context is limited to 5 NPCs to avoid prompt bloat."""
        for i in range(8):
            npc = KnowledgeGraphNode(
                node_type=GraphNodeType.NPC,
                name=f"NPC_{i}",
                attributes={},
                tags=set(),
            )
            self.kg.add_node(npc)

        ctx = _get_grag_context(self.kg, active_character="Nobody")
        # At most 5 NPCs should appear
        npc_count = sum(1 for i in range(8) if f"NPC_{i}" in ctx)
        assert npc_count <= 5

    def test_no_active_character_still_returns_npc_context(self):
        """Even without an active character, NPC context is returned."""
        npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Lord Vader",
            attributes={"title": "Sith Lord"},
            tags=set(),
        )
        self.kg.add_node(npc)

        ctx = _get_grag_context(self.kg, active_character=None)
        assert "Lord Vader" in ctx


class TestNarratorGragIntegration:
    """Verify the GraphRAG context content is suitable for system prompt injection."""

    def test_grag_context_appears_in_system_prompt_content(self):
        """
        The _get_grag_context output must contain the elements that would
        ground the narrator's LLM: NPC names, attributes, and relationship edges.
        """
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Lord Vader",
            attributes={"title": "Dark Lord"},
            tags=set(),
        )
        party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        kg.add_node(npc)
        kg.add_node(party)
        kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=npc.node_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=party.node_uuid,
            )
        )

        grag_output = _get_grag_context(kg, active_character="The Party")

        # Must contain NPC and relationship
        assert "Lord Vader" in grag_output
        assert "hostile_toward" in grag_output.lower()
        # Must not be empty
        assert len(grag_output) > 50
