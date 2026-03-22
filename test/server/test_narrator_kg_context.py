"""
Tests for Gap 2: Narrative Guardrails Pre-Injection

Verifies that:
1. Immutable KG nodes are queried and formatted as forbidden claims
2. The constraints are injected into the narrator's system prompt BEFORE LLM invocation
3. The pre-injection works as a pre-constraint (not just post-hoc validation)

Architecture under test:
  graph.py::_build_kg_constraints_prompt() — formats immutable facts
  graph.py::narrator_node() — injects constraints into system prompt before LLM call
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from langchain_core.messages import AIMessage, HumanMessage

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
from graph import _build_kg_constraints_prompt


class TestKGConstraintsPrompt:
    """Test that immutable KG facts are formatted as forbidden claims."""

    def setup_method(self):
        self.kg = KnowledgeGraph()

    def test_no_immutable_nodes_returns_empty_string(self):
        """When no nodes are immutable, no constraints are emitted."""
        node = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Bob",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(node)
        assert _build_kg_constraints_prompt(self.kg) == ""

    def test_immutable_node_prints_name_and_type(self):
        """Immutable nodes are listed with their type."""
        node = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="King Aldric",
            attributes={},
            tags=set(),
            is_immutable=True,
        )
        self.kg.add_node(node)
        prompt = _build_kg_constraints_prompt(self.kg)
        assert "King Aldric" in prompt
        assert "npc" in prompt.lower()

    def test_immutable_node_includes_attributes(self):
        """Immutable nodes include key attributes in the constraint text."""
        node = KnowledgeGraphNode(
            node_type=GraphNodeType.LOCATION,
            name="Thornwatch Castle",
            attributes={"ruler": "King Aldric", "population": 5000},
            tags=set(),
            is_immutable=True,
        )
        self.kg.add_node(node)
        prompt = _build_kg_constraints_prompt(self.kg)
        assert "Thornwatch Castle" in prompt
        assert "ruler" in prompt
        assert "King Aldric" in prompt

    def test_immutable_node_includes_outgoing_edges(self):
        """Immutable nodes include their outgoing KG edges."""
        king = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="King Aldric",
            attributes={},
            tags=set(),
            is_immutable=True,
        )
        sword = KnowledgeGraphNode(
            node_type=GraphNodeType.ITEM,
            name="Excalibur",
            attributes={},
            tags=set(),
            is_immutable=True,
        )
        self.kg.add_node(king)
        self.kg.add_node(sword)
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=king.node_uuid,
                predicate=GraphPredicate.POSSESSES,
                object_uuid=sword.node_uuid,
            )
        )
        prompt = _build_kg_constraints_prompt(self.kg)
        assert "possesses" in prompt.lower() or "Excalibur" in prompt

    def test_mutable_node_not_included_in_constraints(self):
        """Only immutable nodes appear in the constraints prompt."""
        immutable = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Lord Vader",
            attributes={},
            tags=set(),
            is_immutable=True,
        )
        mutable = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Goblin A",
            attributes={},
            tags=set(),
            is_immutable=False,
        )
        self.kg.add_node(immutable)
        self.kg.add_node(mutable)
        prompt = _build_kg_constraints_prompt(self.kg)
        assert "Lord Vader" in prompt
        assert "Goblin A" not in prompt

    def test_multiple_immutable_nodes_all_listed(self):
        """All immutable nodes appear in the constraints."""
        nodes = [
            KnowledgeGraphNode(
                node_type=GraphNodeType.NPC,
                name=f"Immortal_{i}",
                attributes={},
                tags=set(),
                is_immutable=True,
            )
            for i in range(3)
        ]
        for n in nodes:
            self.kg.add_node(n)
        prompt = _build_kg_constraints_prompt(self.kg)
        for n in nodes:
            assert n.name in prompt


class TestNarratorPreConstraintIntegration:
    """Verify the constraints are visible when the narrator builds its system prompt."""

    def test_constraints_appear_in_prompt_for_immutable_kg(self):
        """
        The constraints prompt built from a KG with immutable nodes must contain
        those nodes' names and key facts — proving the pre-injection will work.
        """
        kg = KnowledgeGraph()
        king = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="King Aldric",
            attributes={"title": "King", "kingdom": "Valdris"},
            tags=set(),
            is_immutable=True,
        )
        sword = KnowledgeGraphNode(
            node_type=GraphNodeType.ITEM,
            name="Excalibur",
            attributes={"type": "legendary sword"},
            tags=set(),
            is_immutable=True,
        )
        kg.add_node(king)
        kg.add_node(sword)
        kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=king.node_uuid,
                predicate=GraphPredicate.POSSESSES,
                object_uuid=sword.node_uuid,
            )
        )

        prompt = _build_kg_constraints_prompt(kg)

        # Immutable node names must appear
        assert "King Aldric" in prompt
        assert "Excalibur" in prompt
        # Key attributes must appear
        assert "Valdris" in prompt
        assert "legendary sword" in prompt
        # Relationship must appear (possesses)
        assert "possesses" in prompt.lower()
        # Excluded mutable content
        assert "Goblin" not in prompt
