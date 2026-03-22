"""
Tests for Gap 4: SVO (Subject-Verb-Object) Claim Validation

Verifies that:
1. Narrative prose claiming world state changes is rejected if no mutation was emitted
2. Narrative prose is ALLOWED if a matching mutation was proposed
3. Verbs implying relationship changes are properly detected

Architecture under test:
  hard_guardrails.py::HardGuardrails.validate_svo_claims() — SVO extraction + mutation cross-check
  hard_guardrails.py::HardGuardrails.validate_full_pipeline() — now includes SVO step
"""

import pytest
from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
from hard_guardrails import HardGuardrails, GuardrailResult
from storylet import GraphMutation


class TestSVOExtraction:
    """Unit tests for the SVO triple extractor."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
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
        party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(king)
        self.kg.add_node(sword)
        self.kg.add_node(party)

        self.hg = HardGuardrails(self.kg)

    def test_gives_transfer_extracts_svo(self):
        """'King gives [[Excalibur]] to the party' should produce a SVO triple."""
        # The extractor requires [[Wikilinks]] to identify entities in sentences
        text = "King Aldric gives [[Excalibur]] to the party."
        triples = self.hg._extract_svo_triples(text)
        # Should find ('', 'gives', 'Excalibur') since the Wikilink is the object
        assert len(triples) >= 1

    def test_no_wikilinks_no_triples(self):
        """Text without [[Wikilinks]] and transfer verbs produces no triples."""
        text = "The dragon flies overhead. The party takes cover."
        triples = self.hg._extract_svo_triples(text)
        # No [[Wikilinks]] means no reliable extraction
        assert triples == []

    def test_betrayal_verb_extracted(self):
        """'Turns on [[The Party]]' should produce a SVO triple."""
        text = "Lord Vader turns on [[The Party]] and joins [[the Cult of Shadows]]."
        triples = self.hg._extract_svo_triples(text)
        # Should detect turns on → HOSTILE_TOWARD and joins → MEMBER_OF
        assert len(triples) >= 1


class TestSVOGuardrail:
    """Test the guardrail rejection when prose implies changes without mutations."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
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
        self.party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(self.king)
        self.kg.add_node(self.sword)
        self.kg.add_node(self.party)
        # King currently possesses sword
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.king.node_uuid,
                predicate=GraphPredicate.POSSESSES,
                object_uuid=self.sword.node_uuid,
            )
        )

    def test_prose_claiming_transfer_without_mutation_rejected(self):
        """
        'King gives Excalibur' without a corresponding mutation is REJECTED.
        This is the core Gap 5/Privilege Segregation fix.
        """
        guardrails = HardGuardrails(self.kg)

        prose = (
            "King Aldric gives [[Excalibur]] to the party. "
            "The blade feels warm with ancient power."
        )

        result = guardrails.validate_svo_claims(prose, [], {})
        assert not result.allowed, "SVO guardrail should reject unmutated prose claims"
        assert "no graph mutations were proposed" in result.reason.lower()

    def test_prose_claiming_transfer_with_mutation_allowed(self):
        """
        'King gives Excalibur' WITH a matching mutation is ALLOWED.
        The mutation proves the Logic Agent authorized the state change.
        """
        guardrails = HardGuardrails(self.kg)

        # Use "gives" which maps to POSSESSES — the mutation predicate matches.
        prose = (
            "King Aldric gives [[Excalibur]] to the party. "
            "The blade feels warm with ancient power."
        )

        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Excalibur",
            predicate="possesses",
            target_name="The Party",
        )

        result = guardrails.validate_svo_claims(prose, [mutation], {})
        # The mutation involves Excalibur (object of "gives") - it should be covered
        assert result.allowed, f"Mutation-covered prose should be allowed: {result.reason}"

    def test_mutation_without_prose_transfer_is_allowed(self):
        """
        A mutation emitted without a corresponding 'gives' claim in prose
        should NOT be flagged — the Logic Agent may have emitted it silently.
        """
        guardrails = HardGuardrails(self.kg)

        # No prose claiming a transfer, just a mutation
        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Excalibur",
            predicate="owned_by",
            target_name="The Party",
        )

        result = guardrails.validate_svo_claims(
            "King Aldric nods solemnly.", [mutation], {}
        )
        assert result.allowed

    def test_full_pipeline_rejects_unmutated_transfer(self):
        """
        validate_full_pipeline (used by narrator_node) must reject
        a prose claim of transfer without mutation.
        """
        guardrails = HardGuardrails(self.kg)

        prose = (
            "King Aldric gives [[Excalibur]] to the party. "
            "The blade feels warm with ancient power."
        )

        result = guardrails.validate_full_pipeline(prose, [], {})
        assert not result.allowed

    def test_full_pipeline_allows_validated_mutation(self):
        """validate_full_pipeline allows prose + mutation when mutation covers the claim."""
        guardrails = HardGuardrails(self.kg)

        # Use "gives" which maps to POSSESSES — the mutation predicate matches.
        prose = "King Aldric gives [[Excalibur]] to the party."
        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Excalibur",
            predicate="possesses",
            target_name="The Party",
        )

        result = guardrails.validate_full_pipeline(prose, [mutation], {})
        assert result.allowed, f"SVO + mutation should pass: {result.reason}"


class TestSVOBetrayalAndAlliance:
    """Test HOSTILE_TOWARD and ALLIED_WITH edge implications."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
        self.vader = KnowledgeGraphNode(
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
        self.party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(self.vader)
        self.kg.add_node(self.cult)
        self.kg.add_node(self.party)

    def test_betrayal_without_mutation_rejected(self):
        """'Lord Vader turns on [[The Party]]' without mutation is rejected."""
        guardrails = HardGuardrails(self.kg)

        prose = (
            "Lord Vader's eyes flash with dark intent. "
            "He turns on [[The Party]], declaring his allegiance to [[Cult of Shadows]]."
        )

        result = guardrails.validate_full_pipeline(prose, [], {})
        assert not result.allowed, "Betrayal without mutation should be rejected"

    def test_betrayal_with_hostile_mutation_allowed(self):
        """'Turns on' with a HOSTILE_TOWARD mutation is allowed."""
        guardrails = HardGuardrails(self.kg)

        prose = "Lord Vader turns on the party with a snarl."
        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Lord Vader",
            predicate="hostile_toward",
            target_name="The Party",
        )

        result = guardrails.validate_full_pipeline(prose, [mutation], {})
        assert result.allowed, f"Betrayal + HOSTILE_TOWARD mutation should pass: {result.reason}"

    def test_join_faction_without_mutation_rejected(self):
        """'Joins [[Cult of Shadows]]' without mutation is rejected."""
        guardrails = HardGuardrails(self.kg)

        prose = "Lord Vader reveals his true loyalties — he joins [[Cult of Shadows]]."
        result = guardrails.validate_full_pipeline(prose, [], {})
        assert not result.allowed, "Faction join without mutation should be rejected"

    def test_join_with_member_of_mutation_allowed(self):
        """'Joins [[the Cult of Shadows]]' with a MEMBER_OF mutation is allowed."""
        guardrails = HardGuardrails(self.kg)

        # NOTE: For the KG entity lookup to find "Cult of Shadows", we must use
        # [[Cult of Shadows]] (exact name) rather than [[the Cult of Shadows]].
        prose = "Lord Vader joins [[Cult of Shadows]]."
        mutation = GraphMutation(
            mutation_type="add_edge",
            node_name="Lord Vader",
            predicate="member_of",
            target_name="Cult of Shadows",
        )

        result = guardrails.validate_full_pipeline(prose, [mutation], {})
        assert result.allowed
