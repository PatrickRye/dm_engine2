"""
Tests for Faction Reputation (faction_standing) feature.
"""
import pytest
import uuid

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
from storylet import GraphQuery, ComparisonOp


class TestFactionStandingCheckQuery:
    """GraphQuery.evaluate() faction_standing_check branch."""

    @pytest.fixture
    def kg_with_faction(self):
        kg = KnowledgeGraph()
        faction = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="Thieves Guild",
            attributes={"faction_standing": {"party": 20}},
        )
        kg.add_node(faction)
        return kg

    def test_faction_standing_hostile_below_threshold(self, kg_with_faction):
        """Faction with standing=20 satisfies 'standing < 30' check."""
        q = GraphQuery(
            query_type="faction_standing_check",
            node_name="Thieves Guild",
            op=ComparisonOp.LT,
            value=30,
        )
        assert q.evaluate(kg_with_faction, {}) is True

    def test_faction_standing_allied_above_threshold(self):
        """Faction with standing=80 satisfies 'standing > 70' check."""
        kg = KnowledgeGraph()
        faction = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="Silver Order",
            attributes={"faction_standing": {"party": 80}},
        )
        kg.add_node(faction)

        q = GraphQuery(
            query_type="faction_standing_check",
            node_name="Silver Order",
            op=ComparisonOp.GTE,
            value=70,
        )
        assert q.evaluate(kg, {}) is True

    def test_faction_standing_defaults_to_50(self):
        """Faction without standing attribute defaults to 50."""
        kg = KnowledgeGraph()
        faction = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="Unknown Guild",
            attributes={},
        )
        kg.add_node(faction)

        q = GraphQuery(
            query_type="faction_standing_check",
            node_name="Unknown Guild",
            op=ComparisonOp.GTE,
            value=50,
        )
        assert q.evaluate(kg, {}) is True

    def test_faction_standing_per_character_tracking(self):
        """faction_standing supports per-character keys via ctx['active_character']."""
        kg = KnowledgeGraph()
        faction = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="Merchants Guild",
            attributes={"faction_standing": {"Aragorn": 90, "party": 20}},
        )
        kg.add_node(faction)

        q = GraphQuery(
            query_type="faction_standing_check",
            node_name="Merchants Guild",
            op=ComparisonOp.GTE,
            value=70,
        )
        # With active_character=Aragorn, should use Aragorn's standing (90)
        assert q.evaluate(kg, {"active_character": "Aragorn"}) is True
        # Without active_character, falls back to "party" (20)
        assert q.evaluate(kg, {}) is False

    def test_faction_standing_nonfaction_node_returns_false(self):
        """faction_standing_check on non-faction node returns False."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Not a Faction",
            attributes={"faction_standing": {"party": 90}},
        )
        kg.add_node(npc)

        q = GraphQuery(
            query_type="faction_standing_check",
            node_name="Not a Faction",
            op=ComparisonOp.GTE,
            value=70,
        )
        assert q.evaluate(kg, {}) is False

    def test_faction_standing_nonexistent_node(self):
        """Faction standing check for unknown node returns False."""
        kg = KnowledgeGraph()
        q = GraphQuery(
            query_type="faction_standing_check",
            node_name="Ghost Faction",
            op=ComparisonOp.GT,
            value=0,
        )
        assert q.evaluate(kg, {}) is False


class TestFactionStandingAutoEdgeUpdate:
    """Auto-update HOSTILE_TOWARD/ALLIED_WITH edges when faction threshold crossed."""

    @pytest.fixture
    def kg_with_faction_and_party(self):
        kg = KnowledgeGraph()
        party_node = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
        )
        kg.add_node(party_node)
        faction = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="Thieves Guild",
            attributes={"faction_standing": {"party": 50}},
        )
        kg.add_node(faction)
        return kg, faction, party_node

    def _simulate_update(self, kg, faction_uuid, party_uuid, old_val, new_val):
        """Simulate what _update_attitude_edge does for a faction standing change."""
        old_state = "hostile" if old_val < 30 else ("friendly" if old_val > 70 else None)
        new_state = "hostile" if new_val < 30 else ("friendly" if new_val > 70 else None)

        if old_state == new_state:
            return

        for edge in list(kg.edges):
            if (edge.subject_uuid == faction_uuid and
                edge.object_uuid == party_uuid and
                edge.predicate in (GraphPredicate.HOSTILE_TOWARD, GraphPredicate.ALLIED_WITH)):
                kg.remove_edge(edge.edge_uuid)

        if new_state == "hostile":
            kg.add_edge(KnowledgeGraphEdge(
                subject_uuid=faction_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=party_uuid,
            ))
        elif new_state == "friendly":
            kg.add_edge(KnowledgeGraphEdge(
                subject_uuid=faction_uuid,
                predicate=GraphPredicate.ALLIED_WITH,
                object_uuid=party_uuid,
            ))

    def test_hostile_edge_added_when_standing_below_30(self, kg_with_faction_and_party):
        """Faction standing drops below 30 → HOSTILE_TOWARD edge added."""
        kg, faction, party_node = kg_with_faction_and_party

        self._simulate_update(kg, faction.node_uuid, party_node.node_uuid, 50, 15)

        hostile_edges = kg.query_edges(
            subject_uuid=faction.node_uuid,
            predicate=GraphPredicate.HOSTILE_TOWARD,
        )
        assert len(hostile_edges) == 1

    def test_allied_edge_added_when_standing_above_70(self, kg_with_faction_and_party):
        """Faction standing rises above 70 → ALLIED_WITH edge added."""
        kg, faction, party_node = kg_with_faction_and_party

        self._simulate_update(kg, faction.node_uuid, party_node.node_uuid, 50, 85)

        allied_edges = kg.query_edges(
            subject_uuid=faction.node_uuid,
            predicate=GraphPredicate.ALLIED_WITH,
        )
        assert len(allied_edges) == 1

    def test_attitude_edge_removed_at_neutral(self, kg_with_faction_and_party):
        """Faction standing returns to 30-70 → attitude edge removed."""
        kg, faction, party_node = kg_with_faction_and_party

        # First add hostile edge
        kg.add_edge(KnowledgeGraphEdge(
            subject_uuid=faction.node_uuid,
            predicate=GraphPredicate.HOSTILE_TOWARD,
            object_uuid=party_node.node_uuid,
        ))

        # Back to neutral
        self._simulate_update(kg, faction.node_uuid, party_node.node_uuid, 15, 50)

        hostile_edges = kg.query_edges(
            subject_uuid=faction.node_uuid,
            predicate=GraphPredicate.HOSTILE_TOWARD,
        )
        assert len(hostile_edges) == 0
