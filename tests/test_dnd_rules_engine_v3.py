import unittest
import uuid
from unittest.mock import patch

from dnd_rules_engine import (
    BaseGameEntity,
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
    resolve_attack_handler,
    apply_damage_handler,
    Weapon,
    Item,
)
from effects import DamageEffect, EffectCondition

class TestDnDRulesEngineV3(unittest.TestCase):
    def setUp(self):
        """Reset the game state before each test."""
        BaseGameEntity._registry.clear()
        EventBus._listeners.clear()

        EventBus.subscribe("MeleeAttack", resolve_attack_handler, priority=10)
        EventBus.subscribe("MeleeAttack", apply_damage_handler, priority=100)

    def test_weapon_with_conditional_damage_effect(self):
        attacker = Creature(
            name="Attacker",
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=3),
            dexterity_mod=ModifiableValue(base_value=1),
        )
        
        sun_blade_effect = DamageEffect(
            damage_dice="1d8",
            damage_type="radiant",
            conditions=[EffectCondition(required_tag="undead")]
        )
        
        sun_blade = Weapon(
            name="Sun Blade",
            damage_dice="1d8",
            damage_type="slashing",
            magic_bonus=2,
            effects=[sun_blade_effect]
        )
        attacker.equipped_weapon_uuid = sun_blade.entity_uuid

        target = Creature(
            name="Undead Target",
            hp=ModifiableValue(base_value=50),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
            tags=["undead"]
        )

        with patch('random.randint', side_effect=[18, 1, 6, 5]):  # Hit, attack roll, 6 base, 5 conditional
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
        
        sun_blade_effect = DamageEffect(
            damage_dice="1d8",
            damage_type="radiant",
            conditions=[EffectCondition(required_tag="undead")]
        )
        
        sun_blade = Weapon(
            name="Sun Blade",
            damage_dice="1d8",
            damage_type="slashing",
            magic_bonus=2,
            effects=[sun_blade_effect]
        )
        attacker.equipped_weapon_uuid = sun_blade.entity_uuid

        target = Creature(
            name="Undead Target",
            hp=ModifiableValue(base_value=50),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
            tags=["undead"]
        )

        with patch('random.randint', side_effect=[20, 1, 6, 5, 6, 5]):  # Crit, attack roll, 6 base, 5 cond, 6 crit base, 5 crit cond
            event = GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=target.entity_uuid)
            EventBus.dispatch(event)
        
        # Damage: (6 base + 5 cond + 3 mod + 2 magic) + (6 crit base + 5 crit cond) = 27
        self.assertEqual(target.hp.base_value, 23)

if __name__ == '__main__':
    unittest.main()
