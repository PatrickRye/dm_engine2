import pytest
from unittest.mock import patch
from pydantic import ValidationError
from spell_system import SpellMechanics, SpellDefinition
from dnd_rules_engine import EventBus, GameEvent, Creature, ModifiableValue
from registry import register_entity, clear_registry
import event_handlers


@pytest.fixture(autouse=True)
def setup_engine():
    clear_registry()
    event_handlers.register_core_handlers()
    yield


def test_spell_mechanics_defaults():
    """Ensure all fields default correctly when instantiated with no arguments."""
    mechanics = SpellMechanics()
    assert mechanics.requires_attack_roll is False
    assert mechanics.save_required == ""
    assert mechanics.damage_dice == ""
    assert mechanics.damage_type == ""
    assert mechanics.half_damage_on_save is False
    assert mechanics.requires_concentration is False
    assert mechanics.granted_tags == []
    assert mechanics.conditions_applied == []
    assert mechanics.modifiers == []


def test_spell_definition_defaults():
    """Ensure nested Pydantic models initialize correctly when missing from the payload."""
    spell = SpellDefinition(name="Fireball")
    assert spell.name == "Fireball"
    assert spell.level == 0
    assert spell.school == "evocation"
    # Mechanics should default to an empty, valid SpellMechanics object
    assert spell.mechanics is not None
    assert spell.mechanics.requires_attack_roll is False
    assert spell.mechanics.damage_dice == ""


def test_spell_mechanics_partial_dict_validation():
    """Ensure partial dictionary parsing preserves defaults for missing keys."""
    raw_data = {"save_required": "dexterity", "damage_dice": "8d6", "half_damage_on_save": True}
    mechanics = SpellMechanics.model_validate(raw_data)
    assert mechanics.save_required == "dexterity"
    assert mechanics.damage_dice == "8d6"
    assert mechanics.half_damage_on_save is True
    # These were missing and should fall back to defaults natively
    assert mechanics.requires_attack_roll is False
    assert mechanics.granted_tags == []


def test_spell_mechanics_invalid_data():
    """Ensure Pydantic aggressively raises validation errors on bad structural data."""
    with pytest.raises(ValidationError):
        SpellMechanics.model_validate({"requires_attack_roll": "not_a_boolean_value"})

    with pytest.raises(ValidationError):
        SpellMechanics.model_validate({"conditions_applied": ["just_a_string_instead_of_dict"]})


def test_resolve_spell_cast_catches_invalid_mechanics():
    """Ensure the event handler safely catches Pydantic validation errors and cancels the event."""
    caster = Creature(
        name="Mage",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)

    # Intentionally malformed payload (e.g. string instead of boolean)
    bad_mechanics = {"requires_attack_roll": "this_should_be_a_boolean", "damage_dice": "8d6"}

    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={"ability_name": "Glitch Beam", "mechanics": bad_mechanics, "target_uuids": []},
    )
    EventBus.dispatch(event)

    assert "results" in event.payload
    assert "SYSTEM ERROR: Invalid spell mechanics payload" in event.payload["results"][0]


def test_spell_attack_critical_hit():
    """Ensure that a natural 20 on a spell attack correctly doubles the base damage dice natively."""
    caster = Creature(
        name="Sorcerer",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Orc",
        x=5,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    register_entity(target)

    mechanics = {"requires_attack_roll": True, "damage_dice": "1d10", "damage_type": "fire"}

    # Force a natural 20, and force the 1d10 dice roll to yield 10. (Base 10 + Crit 10 = 20 Damage)
    with patch("event_handlers.random.randint", return_value=20), patch("event_handlers.roll_dice", return_value=10):
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=caster.entity_uuid,
            payload={
                "ability_name": "Fire Bolt",
                "mechanics": mechanics,
                "target_uuids": [target.entity_uuid],
                "target_wall_ids": [],
            },
        )
        EventBus.dispatch(event)

    assert target.hp.base_value == 10  # 30 - 20 damage
    assert any("Critical Hit!" in res for res in event.payload["results"])
