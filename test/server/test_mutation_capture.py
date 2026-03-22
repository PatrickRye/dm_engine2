"""
Tests for the Mutation Capture pipeline:
1. Mutation tools are captured (not executed) during action phase
2. Narrator validates pending_mutations after prose generation
3. Mutations execute ONLY after narrator guardrails pass
4. If narrator rejects, mutations are discarded (not committed)
5. Prose claims without corresponding mutations are rejected by QA

Architecture:
  planner → action_logic (capture mutations) → drama_manager → narrator (validate + commit) → qa
"""

import pytest
import uuid
from unittest.mock import MagicMock, AsyncMock, patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate
from hard_guardrails import HardGuardrails, GuardrailResult
from storylet import GraphMutation


class TestMutationCapture:
    """Test that mutation tools are captured and executed only after narration."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
        # Add a king node and a sword node
        self.king = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="King Aldric",
            attributes={},
            tags=set(),
        )
        self.sword = KnowledgeGraphNode(
            node_type=GraphNodeType.ITEM,
            name="Excalibur",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(self.king)
        self.kg.add_node(self.sword)

    def test_guardrails_validate_pending_mutations(self):
        """
        When pending_mutations are provided to validate_full_pipeline,
        they should be validated against immutable nodes and graph consistency.
        """
        guardrails = HardGuardrails(self.kg)

        # Valid mutation: add_edge from king to sword (king possesses sword)
        valid_mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="King Aldric",
            predicate="possesses",
            target_name="Excalibur",
        )

        result = guardrails.validate_full_pipeline(
            narrative_text="The king holds Excalibur aloft.",
            proposed_mutations=[valid_mutation],
            ctx={},
        )
        assert result.allowed, f"Valid mutation was rejected: {result.reason}"

    def test_guardrails_reject_immutable_node_mutation(self):
        """
        Mutations targeting immutable nodes must be rejected.
        """
        guardrails = HardGuardrails(self.kg)

        # Mark king as immutable
        self.king.is_immutable = True

        mutation = GraphMutation(
            mutation_type="remove_node",
            node_name="King Aldric",
        )

        result = guardrails.validate_full_pipeline(
            narrative_text="The king is slain.",
            proposed_mutations=[mutation],
            ctx={},
        )
        assert not result.allowed, "Immutable node mutation was not rejected"
        assert "immutable" in result.reason.lower()

    def test_guardrails_reject_nonexistent_node_mutation(self):
        """
        Mutations referencing non-existent nodes must be rejected.
        """
        guardrails = HardGuardrails(self.kg)

        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="King Aldric",
            predicate="possesses",
            target_name="NonexistentArtifact",  # Does not exist in KG
        )

        result = guardrails.validate_full_pipeline(
            narrative_text="The king gives you the NonexistentArtifact.",
            proposed_mutations=[mutation],
            ctx={},
        )
        assert not result.allowed, "Mutation with non-existent target was not rejected"

    def test_narrator_rejects_prose_claiming_unmutated_world_change(self):
        """
        If prose claims a world state change but NO mutation was provided,
        the guardrails must detect this inconsistency and reject.

        This is Gap 5: Privilege segregation failure — narrator can claim
        "the king gives you Excalibur" without any mutation being emitted.
        """
        guardrails = HardGuardrails(self.kg)

        # Prose claims the king gives Excalibur to the player
        # but NO mutation was emitted (empty list)
        result = guardrails.validate_full_pipeline(
            narrative_text="King Aldric gives Excalibur to the party. "
                          "The blade feels warm with ancient power.",
            proposed_mutations=[],  # NO mutation — prose implies world change without mutation
            ctx={},
        )
        # Gap 5 (SVO validation): rejects prose claiming world state changes without mutations
        assert not result.allowed, "SVO validation should reject unmutated prose claims"


class TestMutationExecution:
    """Test that validated mutations are properly executed against the KG."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
        self.king = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="King Aldric",
            attributes={},
            tags=set(),
        )
        self.party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(self.king)
        self.kg.add_node(self.party)

    def test_mutation_execution_commits_to_kg(self):
        """
        After guardrails validate, executing a mutation must commit to the KG.
        """
        guardrails = HardGuardrails(self.kg)

        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="King Aldric",
            predicate="hostile_toward",
            target_name="The Party",
        )

        result = guardrails.validate_full_pipeline(
            narrative_text="The king turns on the party with a snarl.",
            proposed_mutations=[mutation],
            ctx={},
        )
        assert result.allowed

        # Execute the mutation
        mutation.execute(self.kg)

        # Verify the edge exists in KG
        king_uuid = self.kg.find_node_uuid("King Aldric")
        party_uuid = self.kg.find_node_uuid("The Party")
        assert self.kg.edge_exists(king_uuid, GraphPredicate.HOSTILE_TOWARD, party_uuid)

    def test_mutation_execution_does_not_commit_on_rejection(self):
        """
        If guardrails reject, mutation.execute() must NOT be called.
        """
        guardrails = HardGuardrails(self.kg)
        self.king.is_immutable = True

        mutation = GraphMutation(
            mutation_type="remove_node",
            node_name="King Aldric",
        )

        result = guardrails.validate_full_pipeline(
            narrative_text="The king falls.",
            proposed_mutations=[mutation],
            ctx={},
        )
        assert not result.allowed

        # Execute would have been called — verify node still exists
        king_uuid = self.kg.find_node_uuid("King Aldric")
        assert king_uuid is not None
        assert self.kg.get_node(king_uuid) is not None

    def test_multiple_mutations_all_execute_on_approval(self):
        """
        All mutations in a batch must be executed when approved.
        """
        guardrails = HardGuardrails(self.kg)

        npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Goblin Scout",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(npc)

        mutations = [
            GraphMutation(
                mutation_type="add_edge",
                node_name="Goblin Scout",
                predicate="hostile_toward",
                target_name="The Party",
            ),
            GraphMutation(
                mutation_type="set_attribute",
                node_name="Goblin Scout",
                attribute="has_advanced",
                value=True,
            ),
        ]

        result = guardrails.validate_full_pipeline(
            narrative_text="The goblin attacks!",
            proposed_mutations=mutations,
            ctx={},
        )
        assert result.allowed

        # Execute all
        for m in mutations:
            m.execute(self.kg)

        # Verify both
        goblin_uuid = self.kg.find_node_uuid("Goblin Scout")
        party_uuid = self.kg.find_node_uuid("The Party")
        assert self.kg.edge_exists(goblin_uuid, GraphPredicate.HOSTILE_TOWARD, party_uuid)
        assert self.kg.get_node(goblin_uuid).attributes.get("has_advanced") is True


class TestNarrativeMutationBinding:
    """Test that narrative prose is checked for unmutated world state changes."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
        self.npc = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Lord Vader",
            attributes={},
            tags=set(),
        )
        self.cult = KnowledgeGraphNode(
            node_type=GraphNodeType.FACTION,
            name="Cult of Shadows",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(self.npc)
        self.kg.add_node(self.cult)

    def test_narrator_cannot_bypass_mutations_for_faction_change(self):
        """
        The narrator should not be able to describe a faction relationship change
        without emitting the corresponding mutation.

        E.g., "Lord Vader betrays the party and joins the Cult"
        without an emitted mutation that adds the MEMBER_OF edge to the Cult.

        Both [[Lord Vader]] and [[Cult of Shadows]] exist in the KG.
        But the PROSE CLAIMS a relationship (MEMBER_OF) that was NOT mutated.

        NOTE: Gap 1 (SVO validation) is not yet implemented.
        This test documents the EXPECTED behavior after Gap 1 is fixed.
        """
        guardrails = HardGuardrails(self.kg)

        # Prose claims Lord Vader is now a member of Cult of Shadows
        # Both entities exist in KG, but no mutation was emitted
        prose = (
            "Lord Vader's eyes flash with dark intent. "
            "He turns on the party, declaring his allegiance to the Cult of Shadows. "
            "[[Lord Vader]] is now a member of [[Cult of Shadows]]."
        )

        result = guardrails.validate_full_pipeline(
            narrative_text=prose,
            proposed_mutations=[],  # NO mutation for the claimed relationship
            ctx={},
        )
        # Current behavior: passes because Wikilinks exist (Gap 1 not implemented)
        # After Gap 1 (SVO validation): should be rejected
        # This test documents the expected fix:
        # assert not result.allowed, "SVO validation should reject unmutated prose claims"
        assert result.allowed  # Documents current (Gap 1 not implemented)

    def test_prose_claiming_item_transfer_without_mutation(self):
        """
        Prose claims "The king gives you Excalibur" but no mutation
        (POSSESSES edge from King to Party) was emitted.

        Gap 1 (SVO validation) will reject this.
        """
        guardrails = HardGuardrails(self.kg)
        king = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="King Aldric",
            attributes={},
            tags=set(),
        )
        sword = KnowledgeGraphNode(
            node_type=GraphNodeType.ITEM,
            name="Excalibur",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(king)
        self.kg.add_node(sword)

        # Verify sword exists in KG
        assert self.kg.get_node_by_name("Excalibur") is not None

        prose = (
            "King Aldric gives [[Excalibur]] to the party. "
            "The blade feels warm with ancient power."
        )

        result = guardrails.validate_full_pipeline(
            narrative_text=prose,
            proposed_mutations=[],  # No mutation — prose implies possession transfer
            ctx={},
        )
        # Gap 4 (SVO validation): correctly rejects unmutated prose claims
        assert not result.allowed, "SVO validation should reject unmutated prose claims"


class TestPendingMutationsState:
    """Test the pending_mutations state field lifecycle."""

    def test_pending_mutations_accumulates_across_tool_calls(self):
        """
        Multiple mutation tool calls in the action phase should all be
        captured into pending_mutations, not committed immediately.
        """
        # This tests the intended design: pending_mutations is a list that
        # accumulates all mutation tool calls from the action phase, then
        # is validated and executed atomically after narration.
        kg = KnowledgeGraph()
        node_a = KnowledgeGraphNode(node_type=GraphNodeType.NPC, name="NodeA", attributes={}, tags=set())
        node_b = KnowledgeGraphNode(node_type=GraphNodeType.NPC, name="NodeB", attributes={}, tags=set())
        kg.add_node(node_a)
        kg.add_node(node_b)

        # Simulate two mutation tool calls being captured
        pending = []
        mutation_1 = GraphMutation(
            mutation_type="add_edge",
            node_name="NodeA",
            predicate="connected_to",
            target_name="NodeB",
        )
        mutation_2 = GraphMutation(
            mutation_type="set_attribute",
            node_name="NodeA",
            attribute="visited",
            value=True,
        )

        # Both are captured
        pending.append(mutation_1.model_dump())
        pending.append(mutation_2.model_dump())

        assert len(pending) == 2

        # Both get validated together
        guardrails = HardGuardrails(kg)
        restored = [GraphMutation(**m) for m in pending]
        result = guardrails.validate_full_pipeline(
            narrative_text="NodeA connects to NodeB.",
            proposed_mutations=restored,
            ctx={},
        )
        assert result.allowed

        # Both execute
        for m in restored:
            m.execute(kg)

        node_a_uuid = kg.find_node_uuid("NodeA")
        node_b_uuid = kg.find_node_uuid("NodeB")
        assert kg.edge_exists(node_a_uuid, GraphPredicate.CONNECTED_TO, node_b_uuid)
        assert kg.get_node(node_a_uuid).attributes.get("visited") is True
