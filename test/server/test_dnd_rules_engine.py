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


class TestDnDRulesEngine(unittest.TestCase):

    def setUp(self):
        """Reset the game state before each test."""
        clear_registry()
        # Reset the subscription flag for ConditionalDamageWeapon
        ConditionalDamageWeapon._subscribed = False

    def test_demonstration_scenario(self):
        print("Initializing Deterministic Engine...")

        # 1. Create Weapons
        longsword = MeleeWeapon(name="Steel Longsword", damage_dice="1d8", damage_type="slashing")
        register_entity(longsword)

        sun_blade = ConditionalDamageWeapon(
            weapon=longsword,
            name="Sun Blade",
            magic_bonus=2,
            conditions=[DamageCondition(required_tag="undead", extra_damage_dice="1d8", damage_type="radiant")],
        )
        register_entity(sun_blade)

        # 2. Create Creatures
        fighter = Creature(
            name="Kaelen the Paladin",
            hp=ModifiableValue(base_value=35),
            ac=ModifiableValue(base_value=18),
            strength_mod=ModifiableValue(base_value=3),
            dexterity_mod=ModifiableValue(base_value=1),
            equipped_weapon_uuid=sun_blade.entity_uuid,
        )
        register_entity(fighter)

        wizard = Creature(
            name="Lyra the Wizard",
            hp=ModifiableValue(base_value=20),
            ac=ModifiableValue(base_value=12),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=2),
            tags=["can_cast_shield"],
        )
        register_entity(wizard)

        zombie = Creature(
            name="Mindless Zombie",
            hp=ModifiableValue(base_value=22),
            ac=ModifiableValue(base_value=8),
            strength_mod=ModifiableValue(base_value=2),
            dexterity_mod=ModifiableValue(base_value=-2),
            tags=["undead"],
            vulnerabilities=["slashing"],
        )
        register_entity(zombie)

        # 3. Trigger an Event
        print("\n--- Paladin attacks Zombie with Sun Blade ---")
        with patch("random.randint", side_effect=[18, 1, 4, 5]):  # hit, attack roll, damage, conditional damage
            attack_event_1 = GameEvent(
                event_type="MeleeAttack", source_uuid=fighter.entity_uuid, target_uuid=zombie.entity_uuid
            )

            EventBus.dispatch(attack_event_1)

        # Paladin's damage: 1d8 (longsword) + 1d8 (sun blade) + 3 (str) + 2 (magic)
        # 4 + 5 + 3 + 2 = 14
        # Zombie is vulnerable to slashing, so 14 * 2 = 28
        self.assertEqual(zombie.hp.base_value, 0)

        print("\n--- Paladin attacks Wizard ---")
        with patch("random.randint", side_effect=[15, 1, 4]):  # hit, attack roll, damage
            attack_event_2 = GameEvent(
                event_type="MeleeAttack",
                source_uuid=fighter.entity_uuid,
                target_uuid=wizard.entity_uuid,
                payload={"current_initiative": 10},  # Let's say wizard was attacked on init 10
            )

            EventBus.dispatch(attack_event_2)

        # Wizard AC is 12, shield makes it 17. Attack is 15 + 3 = 18. Hit.
        # Damage 4 + 3 + 2 = 9
        self.assertEqual(wizard.hp.base_value, 20 - 9)
        self.assertEqual(wizard.ac.total, 17)  # Shield wears off natively on the start of caster's next turn now!

        # Simulate advancing to Wizard's next turn to drop the Shield
        EventBus.dispatch(
            GameEvent(
                event_type="AdvanceTime",
                source_uuid=fighter.entity_uuid,
                payload={"seconds_advanced": 6, "target_initiative": 10},
            )
        )
        self.assertEqual(wizard.ac.total, 12)

    def test_advantage_and_disadvantage(self):
        """Validates that the engine correctly consumes two d20 rolls and applies advantage/disadvantage."""
        attacker = Creature(
            name="Test Attacker",
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=3),
            dexterity_mod=ModifiableValue(base_value=1),
            tags=["blindsight_60"],
        )
        register_entity(attacker)
        weapon = MeleeWeapon(name="Sword", damage_dice="1d8", damage_type="slashing")
        register_entity(weapon)
        attacker.equipped_weapon_uuid = weapon.entity_uuid

        target = Creature(
            name="Test Target",
            hp=ModifiableValue(base_value=20),
            ac=ModifiableValue(base_value=15),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
            tags=["blindsight_60"],
        )
        register_entity(target)

        with patch("random.randint", side_effect=[2, 18, 5]):  # roll1, roll2, damage
            event = GameEvent(
                event_type="MeleeAttack",
                source_uuid=attacker.entity_uuid,
                target_uuid=target.entity_uuid,
                payload={"advantage": True},
            )
            EventBus.dispatch(event)
            self.assertTrue(event.payload["hit"])

        with patch("random.randint", side_effect=[18, 2]):  # roll1, roll2. Misses, so no damage roll consumed!
            event = GameEvent(
                event_type="MeleeAttack",
                source_uuid=attacker.entity_uuid,
                target_uuid=target.entity_uuid,
                payload={"disadvantage": True},
            )
            EventBus.dispatch(event)
            self.assertFalse(event.payload["hit"])


if __name__ == "__main__":
    unittest.main()
