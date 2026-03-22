"""
Tests for Two-Weapon Fighting (Dual Wield).
REQ-WPN-003: Off-hand Bonus Action attack suppresses ability modifier on damage (unless negative).
             Requires a Light weapon in the main hand.
"""
import os
import pytest

from dnd_rules_engine import Creature, ModifiableValue, WeaponProperty, MeleeWeapon
from registry import clear_registry, register_entity, get_entity
from spatial_engine import spatial_service
from tools import execute_melee_attack
from vault_io import get_journals_dir


@pytest.fixture(autouse=True)
def setup_system(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _make_combatants(vault_path, attacker_str_mod=3, target_ac=8):
    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        tags=["pc"],
        x=0.0, y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=16),
        strength_mod=ModifiableValue(base_value=attacker_str_mod),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    goblin = Creature(
        name="Goblin",
        vault_path=vault_path,
        x=5.0, y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=target_ac),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)
    register_entity(goblin)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(goblin)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    return fighter, goblin


def _equip_light_weapon(fighter, vault_path, name="Shortsword", damage_dice="1d6"):
    """Attach a Light MeleeWeapon directly to the Fighter."""
    weapon = MeleeWeapon(
        name=name,
        damage_dice=damage_dice,
        damage_type="piercing",
        properties=[WeaponProperty.LIGHT, WeaponProperty.FINESSE],
        vault_path=vault_path,
    )
    register_entity(weapon)
    fighter.equipped_weapon_uuid = weapon.entity_uuid
    return weapon


def _equip_heavy_weapon(fighter, vault_path, name="Greataxe", damage_dice="1d12"):
    """Attach a non-Light MeleeWeapon directly to the Fighter."""
    weapon = MeleeWeapon(
        name=name,
        damage_dice=damage_dice,
        damage_type="slashing",
        properties=[],  # no Light property
        vault_path=vault_path,
    )
    register_entity(weapon)
    fighter.equipped_weapon_uuid = weapon.entity_uuid
    return weapon


# ============================================================
# REQ-WPN-003: Off-hand Damage Suppression
# ============================================================

@pytest.mark.asyncio
async def test_req_wpn_003_offhand_attack_suppresses_positive_ability_mod(setup_system, mock_dice, mock_roll_dice):
    """
    REQ-WPN-003: Off-hand attack (is_offhand=True) suppresses positive ability mod on damage.
    Normal hit with STR+3 on a Light weapon: damage = dice roll only (no +3).
    """
    vault_path = setup_system
    fighter, goblin = _make_combatants(vault_path, attacker_str_mod=3, target_ac=8)
    _equip_light_weapon(fighter, vault_path)
    config = {"configurable": {"thread_id": vault_path}}

    # Hit roll = 15 (beats AC 8). Damage die = 4.
    # Off-hand: 4 + 0 (suppressed STR +3) = 4 damage
    with mock_dice(15), mock_roll_dice(4):
        result = await execute_melee_attack.ainvoke(
            {
                "attacker_name": "Fighter",
                "target_name": "Goblin",
                "is_offhand": True,
                "force_auto_roll": True,
            },
            config=config,
        )

    assert "HIT" in result
    assert "4 damage" in result, f"Expected 4 damage (no +3 mod), got: {result}"
    assert goblin.hp.base_value == 26


@pytest.mark.asyncio
async def test_req_wpn_003_offhand_negative_mod_still_applies(setup_system, mock_dice, mock_roll_dice):
    """
    REQ-WPN-003: Off-hand attack still applies a NEGATIVE ability mod (minimum damage 1).
    STR -1 on damage is NOT suppressed. Uses a non-finesse Light weapon so STR is forced.
    """
    vault_path = setup_system
    fighter, goblin = _make_combatants(vault_path, attacker_str_mod=-1, target_ac=8)
    # Non-finesse Light weapon: damage uses STR only (-1)
    weapon = MeleeWeapon(
        name="Light Hammer",
        damage_dice="1d4",
        damage_type="bludgeoning",
        properties=[WeaponProperty.LIGHT],  # no FINESSE
        vault_path=vault_path,
    )
    register_entity(weapon)
    fighter.equipped_weapon_uuid = weapon.entity_uuid
    config = {"configurable": {"thread_id": vault_path}}

    # Hit roll = 15 (beats AC 8). Damage die = 5.
    # Off-hand: min(0, -1) = -1 kept; 5 + (-1) = 4 damage
    with mock_dice(15), mock_roll_dice(5):
        result = await execute_melee_attack.ainvoke(
            {
                "attacker_name": "Fighter",
                "target_name": "Goblin",
                "is_offhand": True,
                "force_auto_roll": True,
            },
            config=config,
        )

    assert "HIT" in result
    assert "4 damage" in result, f"Expected 4 damage (5-1), got: {result}"


@pytest.mark.asyncio
async def test_req_wpn_003_normal_attack_includes_ability_mod(setup_system, mock_dice, mock_roll_dice):
    """
    REQ-WPN-003 baseline: Normal attack (is_offhand=False) includes the ability mod on damage.
    """
    vault_path = setup_system
    fighter, goblin = _make_combatants(vault_path, attacker_str_mod=3, target_ac=8)
    _equip_light_weapon(fighter, vault_path)
    config = {"configurable": {"thread_id": vault_path}}

    # Hit roll = 15. Damage die = 4. Normal: 4 + 3 = 7 damage.
    with mock_dice(15), mock_roll_dice(4):
        result = await execute_melee_attack.ainvoke(
            {
                "attacker_name": "Fighter",
                "target_name": "Goblin",
                "is_offhand": False,
                "force_auto_roll": True,
            },
            config=config,
        )

    assert "HIT" in result
    assert "7 damage" in result, f"Expected 7 damage (4+3 mod), got: {result}"
    assert goblin.hp.base_value == 23


@pytest.mark.asyncio
async def test_req_wpn_003_non_light_weapon_blocked(setup_system, mock_dice):
    """
    REQ-WPN-003: Off-hand attack with a non-Light weapon returns an error.
    """
    vault_path = setup_system
    fighter, goblin = _make_combatants(vault_path, attacker_str_mod=3, target_ac=8)
    _equip_heavy_weapon(fighter, vault_path)
    config = {"configurable": {"thread_id": vault_path}}

    with mock_dice(15):
        result = await execute_melee_attack.ainvoke(
            {
                "attacker_name": "Fighter",
                "target_name": "Goblin",
                "is_offhand": True,
                "force_auto_roll": True,
            },
            config=config,
        )

    assert "SYSTEM ERROR" in result
    assert "Light" in result, f"Expected Light weapon error, got: {result}"
    # Goblin HP untouched
    assert goblin.hp.base_value == 30
