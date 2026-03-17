import pytest

from dnd_rules_engine import Creature, ModifiableValue
from item_system import WeaponItem, ItemCompendium
from compendium_manager import CompendiumEntry, MechanicEffect, CompendiumManager
from registry import clear_registry, register_entity
from tools import equip_item, execute_melee_attack
from spatial_engine import spatial_service
from vault_io import get_journals_dir
import os


@pytest.fixture(autouse=True)
def setup_system(mock_obsidian_vault):
    """Clears the object registries and maps to the mock vault."""
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_mst_002_graze_mastery_on_miss(setup_system, mock_dice):
    """
    REQ-MST-002: Graze. If the attack misses, deal damage equal to the ability
    modifier used for the attack.
    """
    vault_path = setup_system
    config = {"configurable": {"thread_id": vault_path}}

    # Create Fighter with Weapon Mastery feature and high Strength (+4)
    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        tags=["pc", "weapon_mastery"],
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)
    register_entity(target)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    # 1. Define Graze in Compendium
    graze_entry = CompendiumEntry(
        name="Graze",
        category="mastery",
        action_type="Passive",
        description="Miss damage.",
        mechanics=MechanicEffect(trigger_event="on_miss", damage_dice="ability_mod", damage_type="weapon"),
    )
    await CompendiumManager.save_entry(vault_path, graze_entry)

    # 2. Define Greatsword
    greatsword = WeaponItem(name="Greatsword", damage_dice="2d6", damage_type="slashing", mastery_name="Graze")
    await ItemCompendium.save_item(vault_path, greatsword)

    # 3. Equip (should bind the mastery generically)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Greatsword", "item_slot": "main_hand"}, config=config)

    # 4. Attack and force a MISS (Roll 2 + 4 = 6 vs AC 18)
    with mock_dice(default=2):
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Goblin"}, config=config)

    # 5. Verify the miss occurred, but Graze dealt exactly 4 damage
    assert "MISS!" in res
    assert "Graze Mastery Triggered" in res
    assert "Took 4 slashing damage" in res
    assert target.hp.base_value == 26  # 30 - 4


@pytest.mark.asyncio
async def test_req_mst_005_sap_mastery_on_hit(setup_system, mock_dice):
    """
    REQ-MST-005: Sap. On hit, target has Disadvantage on its next attack roll.
    (Applied as the generic 'Sapped' condition).
    """
    vault_path = setup_system
    config = {"configurable": {"thread_id": vault_path}}

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        tags=["pc", "weapon_mastery"],
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Orc",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)
    register_entity(target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    sap_entry = CompendiumEntry(
        name="Sap",
        category="mastery",
        action_type="Passive",
        description="Sap penalty.",
        mechanics=MechanicEffect(trigger_event="on_hit", conditions_applied=[{"condition": "Sapped"}]),
    )
    await CompendiumManager.save_entry(vault_path, sap_entry)

    mace = WeaponItem(name="Mace", damage_dice="1d6", damage_type="bludgeoning", mastery_name="Sap")
    await ItemCompendium.save_item(vault_path, mace)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Mace", "item_slot": "main_hand"}, config=config)

    with mock_dice(18, 18, 5):  # Roll 18, 18 (Hit with adv/disadv checks), Roll 5 (Damage)
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Orc"}, config=config)

    assert "HIT!" in res
    assert "Sap Mastery Triggered" in res
    assert "is now Sapped!" in res
    assert any(c.name == "Sapped" for c in target.active_conditions)
