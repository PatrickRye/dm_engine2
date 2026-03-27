"""
Integration tests for registry.py that run WITHOUT the auto_register_entities fixture.

These tests exercise the REAL register_entity() / get_all_entities() / clear_registry()
code paths that auto_register_entities intercepts during normal unit tests.

The fixture (conftest.py) silences registration errors by injecting register_entity()
into BaseGameEntity.__init__. These tests verify the real registration path works
correctly and catches actual registration bugs (duplicate UUIDs, missing vault_path, etc.).
"""
import pytest
import threading
import uuid
import time

# Import registry functions directly — not through tools.py which might mask issues
from registry import (
    register_entity,
    get_entity,
    get_all_entities,
    remove_entity,
    get_candidate_uuids_by_prefix,
    clear_registry,
)
from dnd_rules_engine import Creature


class TestRegistryRealRegistration:
    """Tests that register_entity / get_all_entities work without the fixture auto-wrapping."""

    def setup_method(self):
        """Isolate each test to its own vault."""
        self.vault = f"test_registry_int_{uuid.uuid4().hex[:8]}"
        clear_registry(self.vault)

    def teardown_method(self):
        clear_registry(self.vault)

    def test_register_entity_with_entity_uuid_and_name(self):
        """register_entity stores entity by UUID and name index."""
        entity = Creature.model_construct(
            vault_path=self.vault,
            name="Aldric",
            x=0.0, y=0.0, z=0.0,
            current_map="",
            icon_url="",
            height=5.0,
            max_hp=10,
            hp=10,
            temp_hp=0,
            ac=10,
            strength_mod=0,
            dexterity_mod=0,
            constitution_mod=0,
            intelligence_mod=0,
            wisdom_mod=0,
            charisma_mod=0,
            spell_save_dc=10,
            spell_attack_bonus=0,
            active_mechanics=[],
            resources={},
            active_conditions=[],
            concentrating_on="",
            reaction_used=False,
            legendary_actions_max=0,
            legendary_actions_current=0,
            speed=30,
            movement_remaining=30,
            wild_shape_hp=0,
            wild_shape_max_hp=0,
            polymorph_hp=0,
            polymorph_max_hp=0,
            death_saves_successes=0,
            death_saves_failures=0,
            exhaustion_level=0,
            tags=["npc"],
            summoned_by_uuid=None,
            summon_spell="",
            mounted_on_uuid=None,
        )

        register_entity(entity, self.vault)

        # Verify by UUID
        retrieved = get_entity(entity.entity_uuid, self.vault)
        assert retrieved is entity

        # Verify by name index
        all_ents = get_all_entities(self.vault)
        assert entity.entity_uuid in all_ents
        assert all_ents[entity.entity_uuid].name == "Aldric"

    def test_get_candidate_uuids_by_prefix(self):
        """Entities are findable by name prefix through the prefix index."""
        for name in ["Gorin", "Gorak", "Gwen", "Aldric"]:
            e = Creature.model_construct(
                vault_path=self.vault, name=name, x=0.0, y=0.0, z=0.0,
                current_map="", icon_url="", height=5.0, max_hp=10, hp=10,
                temp_hp=0, ac=10, strength_mod=0, dexterity_mod=0,
                constitution_mod=0, intelligence_mod=0, wisdom_mod=0,
                charisma_mod=0, spell_save_dc=10, spell_attack_bonus=0,
                active_mechanics=[], resources={}, active_conditions=[],
                concentrating_on="", reaction_used=False, legendary_actions_max=0,
                legendary_actions_current=0, speed=30, movement_remaining=30,
                wild_shape_hp=0, wild_shape_max_hp=0, polymorph_hp=0,
                polymorph_max_hp=0, death_saves_successes=0,
                death_saves_failures=0, exhaustion_level=0, tags=["npc"],
                summoned_by_uuid=None, summon_spell="", mounted_on_uuid=None,
            )
            register_entity(e, self.vault)

        # "gor" matches Gorin and Gorak but not Gwen or Aldric
        candidates = get_candidate_uuids_by_prefix("gor", self.vault)
        names = {get_entity(uid, self.vault).name for uid in candidates}
        assert "Gorin" in names
        assert "Gorak" in names
        assert "Gwen" not in names
        assert "Aldric" not in names

    def test_remove_entity(self):
        """remove_entity unindexes by UUID and name."""
        entity = Creature.model_construct(
            vault_path=self.vault, name="Zara", x=0.0, y=0.0, z=0.0,
            current_map="", icon_url="", height=5.0, max_hp=10, hp=10,
            temp_hp=0, ac=10, strength_mod=0, dexterity_mod=0,
            constitution_mod=0, intelligence_mod=0, wisdom_mod=0,
            charisma_mod=0, spell_save_dc=10, spell_attack_bonus=0,
            active_mechanics=[], resources={}, active_conditions=[],
            concentrating_on="", reaction_used=False, legendary_actions_max=0,
            legendary_actions_current=0, speed=30, movement_remaining=30,
            wild_shape_hp=0, wild_shape_max_hp=0, polymorph_hp=0,
            polymorph_max_hp=0, death_saves_successes=0,
            death_saves_failures=0, exhaustion_level=0, tags=["npc"],
            summoned_by_uuid=None, summon_spell="", mounted_on_uuid=None,
        )
        register_entity(entity, self.vault)
        assert get_entity(entity.entity_uuid, self.vault) is entity

        remove_entity(entity.entity_uuid)

        assert get_entity(entity.entity_uuid, self.vault) is None
        all_ents = get_all_entities(self.vault)
        assert entity.entity_uuid not in all_ents

    def test_clear_registry_removes_all_for_vault(self):
        """clear_registry(vault) removes only that vault's entities."""
        other_vault = self.vault + "_other"

        for vault, name in [(self.vault, "Alice"), (other_vault, "Bob")]:
            e = Creature.model_construct(
                vault_path=vault, name=name, x=0.0, y=0.0, z=0.0,
                current_map="", icon_url="", height=5.0, max_hp=10, hp=10,
                temp_hp=0, ac=10, strength_mod=0, dexterity_mod=0,
                constitution_mod=0, intelligence_mod=0, wisdom_mod=0,
                charisma_mod=0, spell_save_dc=10, spell_attack_bonus=0,
                active_mechanics=[], resources={}, active_conditions=[],
                concentrating_on="", reaction_used=False, legendary_actions_max=0,
                legendary_actions_current=0, speed=30, movement_remaining=30,
                wild_shape_hp=0, wild_shape_max_hp=0, polymorph_hp=0,
                polymorph_max_hp=0, death_saves_successes=0,
                death_saves_failures=0, exhaustion_level=0, tags=["npc"],
                summoned_by_uuid=None, summon_spell="", mounted_on_uuid=None,
            )
            register_entity(e, vault)

        clear_registry(self.vault)

        # Primary vault is empty
        assert get_all_entities(self.vault) == {}
        # Other vault is untouched
        other_ents = get_all_entities(other_vault)
        assert len(other_ents) == 1
        assert other_ents[list(other_ents.keys())[0]].name == "Bob"

        clear_registry(other_vault)

    def test_per_vault_isolation(self):
        """Entities registered in one vault are not accessible in another vault."""
        vault_a = self.vault + "_a"
        vault_b = self.vault + "_b"

        entity_a = Creature.model_construct(
            vault_path=vault_a, name="Carol", x=0.0, y=0.0, z=0.0,
            current_map="", icon_url="", height=5.0, max_hp=10, hp=10,
            temp_hp=0, ac=10, strength_mod=0, dexterity_mod=0,
            constitution_mod=0, intelligence_mod=0, wisdom_mod=0,
            charisma_mod=0, spell_save_dc=10, spell_attack_bonus=0,
            active_mechanics=[], resources={}, active_conditions=[],
            concentrating_on="", reaction_used=False, legendary_actions_max=0,
            legendary_actions_current=0, speed=30, movement_remaining=30,
            wild_shape_hp=0, wild_shape_max_hp=0, polymorph_hp=0,
            polymorph_max_hp=0, death_saves_successes=0,
            death_saves_failures=0, exhaustion_level=0, tags=["npc"],
            summoned_by_uuid=None, summon_spell="", mounted_on_uuid=None,
        )
        register_entity(entity_a, vault_a)

        # Carol is only in vault_a
        assert get_all_entities(vault_a) != {}
        assert get_all_entities(vault_b) == {}

        # Can retrieve Carol by UUID in vault_a
        assert get_entity(entity_a.entity_uuid, vault_a) is entity_a
        # Not in vault_b
        assert get_entity(entity_a.entity_uuid, vault_b) is None

        clear_registry(vault_a)
        clear_registry(vault_b)


class TestRegistryThreadSafety:
    """Concurrent registration and retrieval under threads."""

    def setup_method(self):
        self.vault = f"test_thread_{uuid.uuid4().hex[:8]}"
        clear_registry(self.vault)

    def teardown_method(self):
        clear_registry(self.vault)

    def test_concurrent_registration_no_data_loss(self):
        """Multiple threads registering entities simultaneously — no entity is lost or duplicated."""
        errors = []
        entities = []

        def make_and_register(name_suffix: str):
            try:
                e = Creature.model_construct(
                    vault_path=self.vault, name=f"Thread_{name_suffix}",
                    x=0.0, y=0.0, z=0.0, current_map="", icon_url="",
                    height=5.0, max_hp=10, hp=10, temp_hp=0, ac=10,
                    strength_mod=0, dexterity_mod=0, constitution_mod=0,
                    intelligence_mod=0, wisdom_mod=0, charisma_mod=0,
                    spell_save_dc=10, spell_attack_bonus=0,
                    active_mechanics=[], resources={}, active_conditions=[],
                    concentrating_on="", reaction_used=False,
                    legendary_actions_max=0, legendary_actions_current=0,
                    speed=30, movement_remaining=30, wild_shape_hp=0,
                    wild_shape_max_hp=0, polymorph_hp=0, polymorph_max_hp=0,
                    death_saves_successes=0, death_saves_failures=0,
                    exhaustion_level=0, tags=["npc"], summoned_by_uuid=None,
                    summon_spell="", mounted_on_uuid=None,
                )
                register_entity(e, self.vault)
                entities.append(e.entity_uuid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=make_and_register, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Registration errors: {errors}"
        all_ents = get_all_entities(self.vault)
        # Every entity UUID that was registered should be present
        assert len(all_ents) == 20
        for uid in entities:
            assert uid in all_ents, f"Entity {uid} was registered but not found in registry"
