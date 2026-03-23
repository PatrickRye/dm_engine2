"""
Tests for Gap 7: Relationship-Weighted Storylet Selection

Verifies that:
1. DramaManager._extract_storylet_npcs() correctly extracts NPC names
2. DramaManager._relationship_weight() returns correct boost values
3. select_next() boosts storylets with hostile/allied NPCs over neutral ones

Architecture under test:
  drama_manager.py::DramaManager._extract_storylet_npcs()
  drama_manager.py::DramaManager._relationship_weight()
  drama_manager.py::DramaManager.select_next() — relationship boost in sort key
"""

import pytest
import uuid
from unittest.mock import MagicMock, AsyncMock

from drama_manager import DramaManager, TensionArc
from storylet import Storylet, StoryletPrerequisites, TensionLevel
from storylet_registry import StoryletRegistry
from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge


class TestExtractNPCs:
    """Test NPC extraction from storylet metadata."""

    def setup_method(self):
        self.dm = DramaManager.__new__(DramaManager)
        self.dm.kg = MagicMock()

    def test_npc_tag_extraction(self):
        """Tags formatted 'npc:Name' are extracted."""
        s = Storylet(
            name="Test",
            tags={"npc:Goblin King", "quest:main"},
        )
        names = self.dm._extract_storylet_npcs(s)
        assert "Goblin King" in names

    def test_wikilink_extraction(self):
        """[[Wikilinks]] in content are extracted as NPC names."""
        s = Storylet(
            name="Test",
            content="[[Lord Vader]] meets the party in the forest.",
        )
        names = self.dm._extract_storylet_npcs(s)
        assert "Lord Vader" in names

    def test_name_extraction(self):
        """Storylet name is included as NPC reference."""
        s = Storylet(name="The Dragon's Lair")
        names = self.dm._extract_storylet_npcs(s)
        assert "The Dragon's Lair" in names

    def test_combined_extraction(self):
        """Multiple sources are combined (deduplicated)."""
        s = Storylet(
            name="Vader's Betrayal",
            tags={"npc:Lord Vader"},
            content="[[Lord Vader]] turns on [[The Party]].",
        )
        names = self.dm._extract_storylet_npcs(s)
        assert "Lord Vader" in names
        assert "The Party" in names


class TestRelationshipWeight:
    """Test relationship weight calculation from KG edges."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
        self.dm = DramaManager.__new__(DramaManager)
        self.dm.kg = self.kg

        # Create entities
        self.party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        self.goblin = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Goblin Chief",
            attributes={},
            tags=set(),
        )
        self.ally = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Sir Cedric",
            attributes={},
            tags=set(),
        )
        self.neutral = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Merchant",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(self.party)
        self.kg.add_node(self.goblin)
        self.kg.add_node(self.ally)
        self.kg.add_node(self.neutral)

    def _make_storylet(self, name: str, npc_tag: str = None) -> Storylet:
        tags = {npc_tag} if npc_tag else set()
        return Storylet(name=name, tags=tags)

    def test_hostile_npc_gives_weight_boost(self):
        """HOSTILE_TOWARD edge → +2.0 weight boost."""
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.goblin.node_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=self.party.node_uuid,
            )
        )
        storylet = self._make_storylet("Goblin Ambush", npc_tag="npc:Goblin Chief")
        weight = self.dm._relationship_weight(storylet, "The Party")
        assert weight == 2.0

    def test_allied_npc_gives_small_boost(self):
        """ALLIED_WITH edge → +0.5 weight boost."""
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.ally.node_uuid,
                predicate=GraphPredicate.ALLIED_WITH,
                object_uuid=self.party.node_uuid,
            )
        )
        storylet = self._make_storylet("Sir Cedric's Plea", npc_tag="npc:Sir Cedric")
        weight = self.dm._relationship_weight(storylet, "The Party")
        assert weight == 0.5

    def test_serves_edge_gives_medium_boost(self):
        """SERVES edge → +1.0 weight boost."""
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.ally.node_uuid,
                predicate=GraphPredicate.SERVES,
                object_uuid=self.party.node_uuid,
            )
        )
        storylet = self._make_storylet("Loyal Aid", npc_tag="npc:Sir Cedric")
        weight = self.dm._relationship_weight(storylet, "The Party")
        assert weight == 1.0

    def test_neutral_npc_gives_zero_weight(self):
        """No relationship edge → 0.0 weight."""
        storylet = self._make_storylet("Merchant Deal", npc_tag="npc:Merchant")
        weight = self.dm._relationship_weight(storylet, "The Party")
        assert weight == 0.0

    def test_unknown_active_character_returns_zero(self):
        """Unknown character → no weight (KG lookup fails)."""
        storylet = self._make_storylet("Goblin Fight", npc_tag="npc:Goblin Chief")
        weight = self.dm._relationship_weight(storylet, "Nobody")
        assert weight == 0.0

    def test_no_npc_tags_returns_zero(self):
        """Storylet with no NPC references → 0.0."""
        storylet = Storylet(name="Ambient Description")
        weight = self.dm._relationship_weight(storylet, "The Party")
        assert weight == 0.0

    def test_multiple_npcs_sum_weights(self):
        """Multiple NPCs with relationships → sum of weights."""
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.goblin.node_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=self.party.node_uuid,
            )
        )
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.ally.node_uuid,
                predicate=GraphPredicate.ALLIED_WITH,
                object_uuid=self.party.node_uuid,
            )
        )
        storylet = Storylet(
            name="Battle",
            tags={"npc:Goblin Chief", "npc:Sir Cedric"},
        )
        weight = self.dm._relationship_weight(storylet, "The Party")
        assert weight == 2.5  # 2.0 (hostile) + 0.5 (allied)


class TestSelectNextWithRelationships:
    """Integration test: select_next boosts by relationship weight."""

    def setup_method(self):
        self.kg = KnowledgeGraph()
        self.party = KnowledgeGraphNode(
            node_type=GraphNodeType.PLAYER,
            name="The Party",
            attributes={},
            tags=set(),
        )
        self.goblin = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Goblin Chief",
            attributes={},
            tags=set(),
        )
        self.ally = KnowledgeGraphNode(
            node_type=GraphNodeType.NPC,
            name="Sir Cedric",
            attributes={},
            tags=set(),
        )
        self.kg.add_node(self.party)
        self.kg.add_node(self.goblin)
        self.kg.add_node(self.ally)

        # Hostile edge
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.goblin.node_uuid,
                predicate=GraphPredicate.HOSTILE_TOWARD,
                object_uuid=self.party.node_uuid,
            )
        )
        # Allied edge
        self.kg.add_edge(
            KnowledgeGraphEdge(
                subject_uuid=self.ally.node_uuid,
                predicate=GraphPredicate.ALLIED_WITH,
                object_uuid=self.party.node_uuid,
            )
        )

    @pytest.mark.asyncio
    async def test_select_next_prefers_hostile_storylet(self):
        """Between equal-priority storylets, the hostile NPC storylet wins."""
        # Storylet A: neutral (weight=0), Storylet B: hostile (weight=2)
        # B should be selected even with same priority
        neutral = Storylet(
            name="Ambient Encounter",
            tension_level=TensionLevel.MEDIUM,
            tags=set(),  # No NPC
        )
        hostile = Storylet(
            name="Goblin Showdown",
            tension_level=TensionLevel.MEDIUM,
            tags={"npc:Goblin Chief"},
        )

        # Build a minimal registry that returns both
        storylets = [neutral, hostile]
        mock_registry = MagicMock(spec=StoryletRegistry)
        mock_registry.poll = AsyncMock(return_value=storylets)

        dm = DramaManager(mock_registry, self.kg)
        dm.arc = TensionArc(target_tension=TensionLevel.MEDIUM)

        selected = await dm.select_next({"active_character": "The Party"})
        # Hostile has higher weight → selected
        assert selected.name == "Goblin Showdown"

    @pytest.mark.asyncio
    async def test_select_next_tiebreak_on_priority(self):
        """Higher priority_override wins regardless of relationship."""
        high_priority = Storylet(
            name="Critical Story Beat",
            tension_level=TensionLevel.MEDIUM,
            priority_override=10,
            tags=set(),  # No NPC → weight 0
        )
        low_priority = Storylet(
            name="Goblin Fight",
            tension_level=TensionLevel.MEDIUM,
            priority_override=1,
            tags={"npc:Goblin Chief"},  # weight 2
        )

        mock_registry = MagicMock(spec=StoryletRegistry)
        mock_registry.poll = AsyncMock(return_value=[high_priority, low_priority])

        dm = DramaManager(mock_registry, self.kg)
        dm.arc = TensionArc(target_tension=TensionLevel.MEDIUM)

        selected = await dm.select_next({"active_character": "The Party"})
        # Priority wins over relationship weight
        assert selected.name == "Critical Story Beat"
