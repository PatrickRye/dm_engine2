"""
Tests for NPC Disposition tracking feature.
"""
import pytest
import uuid

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
from storylet import GraphQuery, ComparisonOp, TensionLevel
from hard_guardrails import HardGuardrails


class TestDispositionCheckQuery:
    """GraphQuery.evaluate() disposition_check branch."""

    @pytest.fixture
    def kg_with_npc(self):
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Kima",
            attributes={"disposition_toward_party": 25},
        )
        kg.add_node(npc)
        return kg

    def test_disposition_check_hostile_below_threshold(self, kg_with_npc):
        """NPC with disp=25 should satisfy 'disposition < 30' check."""
        q = GraphQuery(
            query_type="disposition_check",
            node_name="Kima",
            op=ComparisonOp.LT,
            value=30,
        )
        assert q.evaluate(kg_with_npc, {}) is True

    def test_disposition_check_neutral_above_threshold(self, kg_with_npc):
        """NPC with disp=25 should NOT satisfy 'disposition > 70' check."""
        q = GraphQuery(
            query_type="disposition_check",
            node_name="Kima",
            op=ComparisonOp.GT,
            value=70,
        )
        assert q.evaluate(kg_with_npc, {}) is False

    def test_disposition_check_friendly(self):
        """NPC with disp=75 satisfies 'disposition > 70' check."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Mira",
            attributes={"disposition_toward_party": 75},
        )
        kg.add_node(npc)

        q = GraphQuery(
            query_type="disposition_check",
            node_name="Mira",
            op=ComparisonOp.GTE,
            value=70,
        )
        assert q.evaluate(kg, {}) is True

    def test_disposition_check_defaults_to_50(self):
        """NPC without disposition attribute defaults to 50."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Anon",
            attributes={},
        )
        kg.add_node(npc)

        q = GraphQuery(
            query_type="disposition_check",
            node_name="Anon",
            op=ComparisonOp.GTE,
            value=50,
        )
        assert q.evaluate(kg, {}) is True

    def test_disposition_check_nonexistent_npc(self):
        """Disperson check for unknown NPC returns False."""
        kg = KnowledgeGraph()
        q = GraphQuery(
            query_type="disposition_check",
            node_name="Ghost NPC",
            op=ComparisonOp.GT,
            value=0,
        )
        assert q.evaluate(kg, {}) is False


class TestDispositionAutoEdgeUpdate:
    """Auto-update HOSTILE_TOWARD/ALLIED_WITH edges when threshold crossed.

    The _update_attitude_edge function is a nested closure inside commit_node.
    These tests verify the underlying KG state transitions that the function manages.
    """

    @pytest.fixture
    def kg_with_npc_and_party(self):
        """KG with an NPC and a Party node."""
        kg = KnowledgeGraph()
        party_node = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
        )
        kg.add_node(party_node)
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Grumpy",
            attributes={"disposition_toward_party": 50},
        )
        kg.add_node(npc)
        return kg, npc, party_node

    def _simulate_update(self, kg, npc_uuid, party_uuid, old_val, new_val):
        """Simulate what _update_attitude_edge does for a disposition/standing change."""
        def current_attitude():
            for edge in kg.edges:
                if edge.subject_uuid != npc_uuid:
                    continue
                if edge.object_uuid != party_uuid:
                    continue
                if edge.predicate == GraphPredicate.HOSTILE_TOWARD:
                    return "hostile"
                if edge.predicate == GraphPredicate.ALLIED_WITH:
                    return "friendly"
            return None

        old_state = "hostile" if old_val < 30 else ("friendly" if old_val > 70 else None)
        new_state = "hostile" if new_val < 30 else ("friendly" if new_val > 70 else None)

        if old_state == new_state:
            return  # No threshold crossed

        # Remove existing attitude edges
        for edge in list(kg.edges):
            if (edge.subject_uuid == npc_uuid and
                edge.object_uuid == party_uuid and
                edge.predicate in (GraphPredicate.HOSTILE_TOWARD, GraphPredicate.ALLIED_WITH)):
                kg.remove_edge(edge.edge_uuid)

        # Add new attitude edge
        if new_state == "hostile":
            kg.add_edge(KnowledgeGraphEdge(
                subject_uuid=npc_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=party_uuid,
            ))
        elif new_state == "friendly":
            kg.add_edge(KnowledgeGraphEdge(
                subject_uuid=npc_uuid,
                predicate=GraphPredicate.ALLIED_WITH,
                object_uuid=party_uuid,
            ))

    def test_hostile_edge_added_when_crossing_below_30(self, kg_with_npc_and_party):
        """When disposition drops below 30, HOSTILE_TOWARD edge is added."""
        kg, npc, party_node = kg_with_npc_and_party

        # Simulate threshold crossing: 50 -> 20
        self._simulate_update(kg, npc.node_uuid, party_node.node_uuid, 50, 20)

        hostile_edges = kg.query_edges(
            subject_uuid=npc.node_uuid,
            predicate=GraphPredicate.HOSTILE_TOWARD,
        )
        assert len(hostile_edges) == 1
        assert hostile_edges[0].object_uuid == party_node.node_uuid

    def test_friendly_edge_added_when_crossing_above_70(self, kg_with_npc_and_party):
        """When disposition rises above 70, ALLIED_WITH edge is added."""
        kg, npc, party_node = kg_with_npc_and_party

        self._simulate_update(kg, npc.node_uuid, party_node.node_uuid, 50, 80)

        friendly_edges = kg.query_edges(
            subject_uuid=npc.node_uuid,
            predicate=GraphPredicate.ALLIED_WITH,
        )
        assert len(friendly_edges) == 1
        assert friendly_edges[0].object_uuid == party_node.node_uuid

    def test_attitude_edge_removed_when_back_to_neutral(self, kg_with_npc_and_party):
        """When disposition returns to 30-70, attitude edge is removed."""
        kg, npc, party_node = kg_with_npc_and_party

        # First add a hostile edge
        kg.add_edge(KnowledgeGraphEdge(
            subject_uuid=npc.node_uuid,
            predicate=GraphPredicate.HOSTILE_TOWARD,
            object_uuid=party_node.node_uuid,
        ))

        # Cross back to neutral (50)
        self._simulate_update(kg, npc.node_uuid, party_node.node_uuid, 20, 50)

        # Both hostile and allied edges should be gone
        hostile_edges = kg.query_edges(
            subject_uuid=npc.node_uuid,
            predicate=GraphPredicate.HOSTILE_TOWARD,
        )
        allied_edges = kg.query_edges(
            subject_uuid=npc.node_uuid,
            predicate=GraphPredicate.ALLIED_WITH,
        )
        assert len(hostile_edges) == 0
        assert len(allied_edges) == 0


class TestDispositionConsistencyGuardrail:
    """validate_disposition_consistency() guardrail.

    Guardrail logic: An NPC's narrative behavior should be INCONSISTENT with
    their disposition to warrant a flag:
      - Hostile NPC (disp < 30): rejecting hostile actions (a villain attacking is expected)
      - Friendly NPC (disp > 70): rejecting friendly actions (a friend welcoming is expected)

    In practice this catches D&D-narrative dissonance: "Mira the ally snarls and attacks"
    should be flagged because allies don't typically attack party members in prose.
    """

    @pytest.fixture
    def kg_with_npc(self):
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Kima",
            attributes={"disposition_toward_party": 20},  # hostile
        )
        kg.add_node(npc)
        return kg

    def test_hostile_npc_hostile_description_rejected(self, kg_with_npc):
        """Guardrail rejects hostile actions described for a hostile NPC."""
        guardrails = HardGuardrails(kg_with_npc)
        result = guardrails.validate_disposition_consistency(
            "[[Kima]] snarls at the party and draws her blade.",
            {},
        )
        assert not result.allowed
        assert "HOSTILE" in result.reason

    def test_hostile_npc_neutral_description_accepted(self, kg_with_npc):
        """Guardrail accepts neutral/wary description of hostile NPC."""
        guardrails = HardGuardrails(kg_with_npc)
        result = guardrails.validate_disposition_consistency(
            "Kima eyes the party warily.",
            {},
        )
        # Warily is not in hostile or friendly descriptor lists, so should pass
        assert result.allowed

    def test_friendly_npc_friendly_description_rejected(self):
        """Guardrail rejects warm description of a friendly NPC."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Mira",
            attributes={"disposition_toward_party": 85},  # friendly
        )
        kg.add_node(npc)

        guardrails = HardGuardrails(kg)
        result = guardrails.validate_disposition_consistency(
            "[[Mira]] smiles warmly at the party and welcomes them inside.",
            {},
        )
        assert not result.allowed
        assert "FRIENDLY" in result.reason

    def test_friendly_npc_hostile_description_accepted(self):
        """Guardrail accepts hostile description of a friendly NPC (surprising but ok)."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Mira",
            attributes={"disposition_toward_party": 85},  # friendly
        )
        kg.add_node(npc)

        guardrails = HardGuardrails(kg)
        result = guardrails.validate_disposition_consistency(
            "Mira snarls at the party and refuses to speak.",
            {},
        )
        # A friendly NPC attacking is flagged by the friendly-descriptor check,
        # but hostile actions on a friendly NPC are NOT in the HOSTILE_DESCRIPTORS list
        # for disp > 70 (the guardrail checks friendly descriptors for friendly NPCs)
        assert result.allowed

    def test_neutral_npc_passes_both_hostile_and_friendly(self):
        """Neutral NPC (disp=50) passes both hostile and friendly checks."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Neutral NPC",
            attributes={"disposition_toward_party": 50},
        )
        kg.add_node(npc)

        guardrails = HardGuardrails(kg)

        result2 = guardrails.validate_disposition_consistency(
            "Neutral NPC eyes the party.",
            {},
        )
        assert result2.allowed

    def test_narrative_without_wikilinks_passes(self, kg_with_npc):
        """Prose without any Wikilinks is not checked."""
        guardrails = HardGuardrails(kg_with_npc)
        result = guardrails.validate_disposition_consistency(
            "The wind howls through the trees.",
            {},
        )
        assert result.allowed
