"""
Tests for Gap 3: Engine State Checks in Storylet Prerequisites

Verifies that StoryletPrerequisites.is_met() can query runtime entity state
(HP, conditions, resources) via the engine_state_check query type.

Architecture under test:
  storylet.py::GraphQuery.evaluate() — now handles 'engine_state_check' query type
  storylet.py::StoryletPrerequisites.is_met() — passes ctx (with vault_path) through to queries
"""

import pytest
import uuid
from unittest.mock import patch, MagicMock

from storylet import GraphQuery, StoryletPrerequisites, ComparisonOp
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition


class TestEngineStateChecks:
    """Test engine_state_check queries against the entity registry."""

    def setup_method(self):
        # Create test creatures directly in the registry
        self.vault_path = "test_gap3_vault"
        self._setup_creatures()

    def _setup_creatures(self):
        """Create test creatures with known HP and conditions."""
        from registry import _ACTIVE_ENTITIES, _NAME_INDEX

        _ACTIVE_ENTITIES[self.vault_path] = {}
        if self.vault_path not in _NAME_INDEX:
            _NAME_INDEX[self.vault_path] = {}

        def make_mod(val: int) -> ModifiableValue:
            return ModifiableValue(base_value=val)

        # Kaelen: HP 30/45, has "Haste" and "Blessed" conditions
        kaelen = Creature(
            entity_uuid=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            name="Kaelen",
            vault_path=self.vault_path,
            max_hp=45,
            hp=ModifiableValue(base_value=30),
            ac=make_mod(16),
            strength_mod=make_mod(2),
            dexterity_mod=make_mod(1),
            temp_hp=0,
        )
        kaelen.active_conditions = [
            ActiveCondition(name="Haste", source="Spell", turns_remaining=3),
            ActiveCondition(name="Blessed", source="Bless", turns_remaining=2),
        ]

        # Lyra: HP 5/40, has "Prone" condition
        lyra = Creature(
            entity_uuid=uuid.UUID("22222222-2222-2222-2222-222222222222"),
            name="Lyra",
            vault_path=self.vault_path,
            max_hp=40,
            hp=ModifiableValue(base_value=5),
            ac=make_mod(13),
            strength_mod=make_mod(0),
            dexterity_mod=make_mod(2),
            temp_hp=0,
        )
        lyra.active_conditions = [
            ActiveCondition(name="Prone", source="Combat", turns_remaining=1),
        ]

        # Gideon: HP full, no conditions
        gideon = Creature(
            entity_uuid=uuid.UUID("33333333-3333-3333-3333-333333333333"),
            name="Gideon",
            vault_path=self.vault_path,
            max_hp=50,
            hp=ModifiableValue(base_value=50),
            ac=make_mod(18),
            strength_mod=make_mod(3),
            dexterity_mod=make_mod(1),
            temp_hp=0,
        )

        for ent in [kaelen, lyra, gideon]:
            _ACTIVE_ENTITIES[self.vault_path][ent.entity_uuid] = ent
            _NAME_INDEX[self.vault_path][ent.name.lower()] = ent.entity_uuid

    def teardown_method(self):
        from registry import _ACTIVE_ENTITIES, _NAME_INDEX
        _ACTIVE_ENTITIES.pop(self.vault_path, None)
        _NAME_INDEX.pop(self.vault_path, None)

    def test_hp_lt_triggers_for_wounded_character(self):
        """HP < max_hp should trigger hp_lt comparison."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Kaelen"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Kaelen",
            attribute="hp",
            op=ComparisonOp.LT,
            value=45,  # Kaelen has 30 HP, max 45
        )
        assert query.evaluate(kg, ctx) is True

    def test_hp_eq_false_when_not_at_value(self):
        """HP == value is False when current HP differs."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Lyra"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Lyra",
            attribute="hp",
            op=ComparisonOp.EQ,
            value=40,  # Lyra has 5 HP, not 40
        )
        assert query.evaluate(kg, ctx) is False

    def test_hp_eq_true_when_at_value(self):
        """HP == value is True when current HP matches."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Gideon"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Gideon",
            attribute="hp",
            op=ComparisonOp.EQ,
            value=50,  # Gideon has 50 HP
        )
        assert query.evaluate(kg, ctx) is True

    def test_active_conditions_has_tag(self):
        """HAS_TAG on active_conditions finds an active effect."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Kaelen"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Kaelen",
            attribute="active_conditions",
            op=ComparisonOp.HAS_TAG,
            value="Haste",
        )
        assert query.evaluate(kg, ctx) is True

    def test_active_conditions_not_has_tag(self):
        """NOT_HAS_TAG is True when condition is absent."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Gideon"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Gideon",
            attribute="active_conditions",
            op=ComparisonOp.NOT_HAS_TAG,
            value="Haste",
        )
        assert query.evaluate(kg, ctx) is True

    def test_active_conditions_not_has_tag_false_when_present(self):
        """NOT_HAS_TAG is False when the condition IS present."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Kaelen"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Kaelen",
            attribute="active_conditions",
            op=ComparisonOp.NOT_HAS_TAG,
            value="Haste",
        )
        assert query.evaluate(kg, ctx) is False

    def test_max_hp_check(self):
        """max_hp attribute is readable."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Kaelen"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Kaelen",
            attribute="max_hp",
            op=ComparisonOp.EQ,
            value=45,
        )
        assert query.evaluate(kg, ctx) is True

    def test_nonexistent_entity_returns_false(self):
        """Query for a non-existent entity returns False."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Nobody"}
        query = GraphQuery(
            query_type="engine_state_check",
            node_name="Nobody",
            attribute="hp",
            op=ComparisonOp.GT,
            value=0,
        )
        assert query.evaluate(kg, ctx) is False


class TestStoryletPrerequisitesWithEngineState:
    """Test StoryletPrerequisites.is_met() using engine_state_check queries."""

    def setup_method(self):
        self.vault_path = "test_gap3_prereqs"
        self._setup_creatures()

    def _setup_creatures(self):
        from registry import _ACTIVE_ENTITIES, _NAME_INDEX

        _ACTIVE_ENTITIES[self.vault_path] = {}
        _NAME_INDEX[self.vault_path] = {}

        def make_mod(val: int) -> ModifiableValue:
            return ModifiableValue(base_value=val)

        goblin = Creature(
            entity_uuid=uuid.UUID("44444444-4444-4444-4444-444444444444"),
            name="Goblin",
            vault_path=self.vault_path,
            max_hp=10,
            hp=ModifiableValue(base_value=10),
            ac=make_mod(15),
            strength_mod=make_mod(1),
            dexterity_mod=make_mod(2),
            temp_hp=0,
        )

        _ACTIVE_ENTITIES[self.vault_path][goblin.entity_uuid] = goblin
        _NAME_INDEX[self.vault_path][goblin.name.lower()] = goblin.entity_uuid

    def teardown_method(self):
        from registry import _ACTIVE_ENTITIES, _NAME_INDEX
        _ACTIVE_ENTITIES.pop(self.vault_path, None)
        _NAME_INDEX.pop(self.vault_path, None)

    def test_all_of_with_engine_state(self):
        """Storylet fires only when all_of (including engine checks) are met."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Goblin"}

        # Prerequisites: HP must be 10 AND max_hp must be 10
        prereqs = StoryletPrerequisites(
            all_of=[
                GraphQuery(
                    query_type="engine_state_check",
                    node_name="Goblin",
                    attribute="hp",
                    op=ComparisonOp.EQ,
                    value=10,
                ),
                GraphQuery(
                    query_type="engine_state_check",
                    node_name="Goblin",
                    attribute="max_hp",
                    op=ComparisonOp.EQ,
                    value=10,
                ),
            ]
        )
        assert prereqs.is_met(kg, ctx) is True

    def test_none_of_engine_state_excludes(self):
        """none_of with engine_state_check excludes characters by condition."""
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        ctx = {"vault_path": self.vault_path, "active_character": "Goblin"}

        # Set up a cursed goblin
        from registry import _ACTIVE_ENTITIES
        goblin = _ACTIVE_ENTITIES[self.vault_path][uuid.UUID("44444444-4444-4444-4444-444444444444")]
        goblin.active_conditions.append(ActiveCondition(name="Charmed", source="Spell", turns_remaining=5))

        prereqs = StoryletPrerequisites(
            none_of=[
                GraphQuery(
                    query_type="engine_state_check",
                    node_name="Goblin",
                    attribute="active_conditions",
                    op=ComparisonOp.HAS_TAG,
                    value="Charmed",
                )
            ]
        )
        # The goblin IS charmed, so none_of should fail → is_met is False
        assert prereqs.is_met(kg, ctx) is False
