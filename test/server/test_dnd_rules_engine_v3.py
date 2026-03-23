import unittest
from unittest.mock import patch

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
    MeleeWeapon,
    ConditionalDamageWeapon,
    DamageCondition,
)
from registry import clear_registry, register_entity


class TestDnDRulesEngineV3(unittest.TestCase):
    def setUp(self):
        """Reset the game state before each test."""
        clear_registry()
        # Reset the subscription flag for ConditionalDamageWeapon
        ConditionalDamageWeapon._subscribed = False

    def test_weapon_with_conditional_damage_effect(self):
        attacker = Creature(
            name="Attacker",
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=3),
            dexterity_mod=ModifiableValue(base_value=1),
        )
        register_entity(attacker)

        longsword = MeleeWeapon(name="Steel Longsword", damage_dice="1d8", damage_type="slashing")
        register_entity(longsword)
        sun_blade = ConditionalDamageWeapon(
            weapon=longsword,
            name="Sun Blade",
            magic_bonus=2,
            conditions=[DamageCondition(required_tag="undead", extra_damage_dice="1d8", damage_type="radiant")],
        )
        register_entity(sun_blade)
        attacker.equipped_weapon_uuid = sun_blade.entity_uuid

        target = Creature(
            name="Undead Target",
            hp=ModifiableValue(base_value=50),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
            tags=["undead"],
        )
        register_entity(target)

        with patch("random.randint", side_effect=[18, 1, 6, 5]):
            event = GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=target.entity_uuid)
            EventBus.dispatch(event)

        # Damage: (6 base + 5 cond + 3 mod + 2 magic) = 16
        self.assertEqual(target.hp.base_value, 34)

    def test_critical_hit_with_conditional_damage_effect(self):
        attacker = Creature(
            name="Attacker",
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=3),
            dexterity_mod=ModifiableValue(base_value=1),
        )
        register_entity(attacker)

        longsword = MeleeWeapon(name="Steel Longsword", damage_dice="1d8", damage_type="slashing")
        register_entity(longsword)
        sun_blade = ConditionalDamageWeapon(
            weapon=longsword,
            name="Sun Blade",
            magic_bonus=2,
            conditions=[DamageCondition(required_tag="undead", extra_damage_dice="1d8", damage_type="radiant")],
        )
        register_entity(sun_blade)
        attacker.equipped_weapon_uuid = sun_blade.entity_uuid

        target = Creature(
            name="Undead Target",
            hp=ModifiableValue(base_value=50),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
            tags=["undead"],
        )
        register_entity(target)

        # Crit, attack roll, 6 base, 5 cond, 6 crit base, 5 crit cond
        with patch("random.randint", side_effect=[20, 1, 6, 5, 6, 5]):
            event = GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=target.entity_uuid)
            EventBus.dispatch(event)

        # Damage: (6 base + 5 cond + 3 mod + 2 magic) + (6 crit base + 5 crit cond) = 27
        self.assertEqual(target.hp.base_value, 23)


if __name__ == "__main__":
    unittest.main()
