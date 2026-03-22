"""
Tests for Secret Keeper feature — secret edges hidden from narrator but visible to storylets.
"""
import pytest
import uuid

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
from storylet import GraphMutation, GraphQuery, ComparisonOp


class TestSecretEdgeSchema:
    """KnowledgeGraphEdge.secret field and get_context_for_node(hide_secrets)."""

    @pytest.fixture
    def kg_with_secret_edge(self):
        kg = KnowledgeGraph()
        vance = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Lord Vance",
            attributes={},
        )
        cult = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="The Cult",
            attributes={},
        )
        kg.add_node(vance)
        kg.add_node(cult)
        # Secret edge: Lord Vance is secretly a member of The Cult
        secret_edge = KnowledgeGraphEdge(
            subject_uuid=vance.node_uuid,
            predicate=GraphPredicate.MEMBER_OF,
            object_uuid=cult.node_uuid,
            secret=True,
        )
        kg.add_edge(secret_edge)
        return kg, vance, cult

    def test_secret_edge_in_kg(self, kg_with_secret_edge):
        """Secret edge is stored in KG."""
        kg, vance, cult = kg_with_secret_edge
        edges = kg.query_edges(
            subject_uuid=vance.node_uuid,
            predicate=GraphPredicate.MEMBER_OF,
        )
        assert len(edges) == 1
        assert edges[0].secret is True

    def test_secret_edge_hidden_from_narrator(self, kg_with_secret_edge):
        """get_context_for_node(hide_secrets=True) excludes secret edges."""
        kg, vance, cult = kg_with_secret_edge

        # Narrator context (hide_secrets=True)
        ctx_hidden = kg.get_context_for_node(vance.node_uuid, hide_secrets=True)
        neighbors = ctx_hidden.get("neighbors", {})
        # Secret edge should not appear
        member_of_neighbors = neighbors.get("member_of", [])
        assert "The Cult" not in member_of_neighbors

    def test_secret_edge_visible_to_storylets(self, kg_with_secret_edge):
        """get_context_for_node(hide_secrets=False) includes secret edges."""
        kg, vance, cult = kg_with_secret_edge

        # Storylet context (hide_secrets=False)
        ctx_visible = kg.get_context_for_node(vance.node_uuid, hide_secrets=False)
        neighbors = ctx_visible.get("neighbors", {})
        member_of_neighbors = neighbors.get("member_of", [])
        assert "The Cult" in member_of_neighbors

    def test_regular_edge_not_hidden(self):
        """Non-secret edges appear in both hide_secrets=True and False."""
        kg = KnowledgeGraph()
        npc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Guard",
            attributes={},
        )
        loc = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.LOCATION,
            name="Watchtower",
            attributes={},
        )
        kg.add_node(npc)
        kg.add_node(loc)
        kg.add_edge(KnowledgeGraphEdge(
            subject_uuid=npc.node_uuid,
            predicate=GraphPredicate.LOCATED_IN,
            object_uuid=loc.node_uuid,
            secret=False,
        ))

        ctx_hidden = kg.get_context_for_node(npc.node_uuid, hide_secrets=True)
        ctx_visible = kg.get_context_for_node(npc.node_uuid, hide_secrets=False)

        assert "Watchtower" in ctx_hidden.get("neighbors", {}).get("located_in", [])
        assert "Watchtower" in ctx_visible.get("neighbors", {}).get("located_in", [])


class TestSetEdgeAttributeMutation:
    """GraphMutation.execute() set_edge_attribute mutation type."""

    @pytest.fixture
    def kg_with_secret_edge(self):
        kg = KnowledgeGraph()
        vance = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Lord Vance",
            attributes={},
        )
        cult = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="The Cult",
            attributes={},
        )
        kg.add_node(vance)
        kg.add_node(cult)
        secret_edge = KnowledgeGraphEdge(
            subject_uuid=vance.node_uuid,
            predicate=GraphPredicate.MEMBER_OF,
            object_uuid=cult.node_uuid,
            secret=True,
        )
        kg.add_edge(secret_edge)
        return kg, vance, cult

    def test_set_edge_attribute_reveals_secret(self, kg_with_secret_edge):
        """set_edge_attribute(secret=False) reveals the secret edge."""
        kg, vance, cult = kg_with_secret_edge

        mutation = GraphMutation(
            mutation_type="set_edge_attribute",
            node_name="Lord Vance",
            predicate="member_of",
            target_name="The Cult",
            attribute="secret",
            value=False,
        )
        mutation.execute(kg)

        # Edge should no longer be secret
        edges = kg.query_edges(
            subject_uuid=vance.node_uuid,
            predicate=GraphPredicate.MEMBER_OF,
        )
        assert len(edges) == 1
        assert edges[0].secret is False

    def test_set_edge_attribute_on_nonexistent_edge(self):
        """set_edge_attribute on nonexistent edge is a no-op (no error)."""
        kg = KnowledgeGraph()
        mutation = GraphMutation(
            mutation_type="set_edge_attribute",
            node_name="Ghost",
            predicate="member_of",
            target_name="Nowhere",
            attribute="secret",
            value=False,
        )
        # Should not raise
        mutation.execute(kg)

    def test_set_edge_attribute_with_uuid(self, kg_with_secret_edge):
        """set_edge_attribute works with node_uuid instead of node_name."""
        kg, vance, cult = kg_with_secret_edge

        mutation = GraphMutation(
            mutation_type="set_edge_attribute",
            node_uuid=vance.node_uuid,
            predicate="member_of",
            target_uuid=cult.node_uuid,
            attribute="secret",
            value=False,
        )
        mutation.execute(kg)

        edges = kg.query_edges(subject_uuid=vance.node_uuid, predicate=GraphPredicate.MEMBER_OF)
        assert edges[0].secret is False


class TestEdgeAttributeCheckQuery:
    """GraphQuery.evaluate() edge_attribute_check branch."""

    @pytest.fixture
    def kg_with_secret_edge(self):
        kg = KnowledgeGraph()
        vance = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Lord Vance",
            attributes={},
        )
        cult = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="The Cult",
            attributes={},
        )
        kg.add_node(vance)
        kg.add_node(cult)
        secret_edge = KnowledgeGraphEdge(
            subject_uuid=vance.node_uuid,
            predicate=GraphPredicate.MEMBER_OF,
            object_uuid=cult.node_uuid,
            secret=True,
        )
        kg.add_edge(secret_edge)
        return kg, vance, cult

    def test_edge_attribute_check_secret_still_true(self, kg_with_secret_edge):
        """edge_attribute_check(secret=True) on secret edge returns True."""
        kg, vance, cult = kg_with_secret_edge

        q = GraphQuery(
            query_type="edge_attribute_check",
            node_name="Lord Vance",
            predicate="member_of",
            target_name="The Cult",
            attribute="secret",
            op=ComparisonOp.EQ,
            value=True,
        )
        assert q.evaluate(kg, {}) is True

    def test_edge_attribute_check_secret_is_false_after_reveal(self, kg_with_secret_edge):
        """After secret is revealed, edge_attribute_check(secret=True) returns False."""
        kg, vance, cult = kg_with_secret_edge

        # Reveal the secret
        mutation = GraphMutation(
            mutation_type="set_edge_attribute",
            node_name="Lord Vance",
            predicate="member_of",
            target_name="The Cult",
            attribute="secret",
            value=False,
        )
        mutation.execute(kg)

        q = GraphQuery(
            query_type="edge_attribute_check",
            node_name="Lord Vance",
            predicate="member_of",
            target_name="The Cult",
            attribute="secret",
            op=ComparisonOp.EQ,
            value=True,
        )
        assert q.evaluate(kg, {}) is False

    def test_edge_attribute_check_not_eq(self, kg_with_secret_edge):
        """edge_attribute_check with NE operator works."""
        kg, vance, cult = kg_with_secret_edge

        q = GraphQuery(
            query_type="edge_attribute_check",
            node_name="Lord Vance",
            predicate="member_of",
            target_name="The Cult",
            attribute="secret",
            op=ComparisonOp.NE,
            value=True,
        )
        assert q.evaluate(kg, {}) is False  # secret IS True, so NE is False

    def test_edge_attribute_check_nonexistent_edge(self, kg_with_secret_edge):
        """edge_attribute_check on nonexistent edge returns False."""
        kg, vance, cult = kg_with_secret_edge

        q = GraphQuery(
            query_type="edge_attribute_check",
            node_name="Lord Vance",
            predicate="member_of",
            target_name="NonExistent Faction",
            attribute="secret",
            op=ComparisonOp.EQ,
            value=True,
        )
        assert q.evaluate(kg, {}) is False


class TestAddEdgeWithSecret:
    """GraphMutation.add_edge with secret=True field."""

    @pytest.fixture
    def kg_with_nodes(self):
        kg = KnowledgeGraph()
        spy = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name="Spy",
            attributes={},
        )
        guild = KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.FACTION,
            name="Thieves Guild",
            attributes={},
        )
        kg.add_node(spy)
        kg.add_node(guild)
        return kg, spy, guild

    def test_add_edge_with_secret_true(self, kg_with_nodes):
        """add_edge with secret=True creates a secret edge."""
        kg, spy, guild = kg_with_nodes

        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Spy",
            predicate="member_of",
            target_name="Thieves Guild",
            secret=True,
        )
        mutation.execute(kg)

        edges = kg.query_edges(
            subject_uuid=spy.node_uuid,
            predicate=GraphPredicate.MEMBER_OF,
        )
        assert len(edges) == 1
        assert edges[0].secret is True

    def test_add_edge_secret_false_by_default(self, kg_with_nodes):
        """add_edge without secret field defaults to secret=False."""
        kg, spy, guild = kg_with_nodes

        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Spy",
            predicate="member_of",
            target_name="Thieves Guild",
        )
        mutation.execute(kg)

        edges = kg.query_edges(
            subject_uuid=spy.node_uuid,
            predicate=GraphPredicate.MEMBER_OF,
        )
        assert edges[0].secret is False


class TestSecretPersistence:
    """Secret field on GraphMutation is serialized/deserialized correctly."""

    def test_mutation_secret_serialization_roundtrip(self):
        """GraphMutation with secret=True serializes and deserializes correctly."""
        from storylet_registry import _mutation_to_dict, _dict_to_storylet
        from storylet import Storylet, StoryletPrerequisites, StoryletEffect

        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Spy",
            predicate="member_of",
            target_name="Thieves Guild",
            secret=True,
        )

        d = _mutation_to_dict(mutation)
        assert d["secret"] is True

    def test_restore_mutation_with_secret_field(self):
        """_restore_mutation (inside _dict_to_storylet) correctly restores secret."""
        from storylet_registry import _dict_to_storylet

        data = {
            "id": str(uuid.uuid4()),
            "name": "Test Storylet",
            "prerequisites": {"all_of": [], "any_of": [], "none_of": []},
            "effects": [
                {
                    "id": str(uuid.uuid4()),
                    "graph_mutations": [
                        {
                            "mutation_type": "add_edge",
                            "node_name": "Spy",
                            "predicate": "member_of",
                            "target_name": "Thieves Guild",
                            "secret": True,
                        }
                    ],
                    "flag_changes": {},
                    "attribute_mods": {},
                }
            ],
        }

        storylet = _dict_to_storylet(data)
        mutation = storylet.effects[0].graph_mutations[0]
        assert mutation.secret is True
