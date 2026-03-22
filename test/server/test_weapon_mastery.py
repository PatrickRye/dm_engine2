import pytest

from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus
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
    assert "took 4 slashing damage" in res
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


@pytest.mark.asyncio
async def test_req_mst_007_topple_mastery(setup_system, mock_dice):
    """
    REQ-MST-007: Topple
    On hit, target makes a Con save or falls Prone.
    """
    vault_path = setup_system
    config = {"configurable": {"thread_id": vault_path}}

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        tags=["pc", "weapon_mastery"],
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
        spell_save_dc=ModifiableValue(base_value=14),
    )
    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)
    register_entity(target)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    topple_entry = CompendiumEntry(
        name="Topple",
        category="mastery",
        action_type="Passive",
        description="Topple the enemy.",
        mechanics=MechanicEffect(
            trigger_event="on_hit", save_required="constitution", conditions_applied=[{"condition": "Prone"}]
        ),
    )
    await CompendiumManager.save_entry(vault_path, topple_entry)

    maul = WeaponItem(name="Maul", damage_dice="2d6", damage_type="bludgeoning", mastery_name="Topple")
    await ItemCompendium.save_item(vault_path, maul)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Maul", "item_slot": "main_hand"}, config=config)

    with mock_dice(15, 2, 5):  # hit roll, save roll, damage roll
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Goblin"}, config=config)

    assert "HIT!" in res
    assert "Topple Mastery Triggered" in res
    assert "Failed Save" in res
    assert any(c.name == "Prone" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_req_mst_008_vex_mastery(setup_system, mock_dice):
    """
    REQ-MST-008: Vex
    On hit, gain Advantage on your next attack roll against that specific creature.
    """
    vault_path = setup_system
    config = {"configurable": {"thread_id": vault_path}}

    rogue = Creature(
        name="Rogue",
        vault_path=vault_path,
        tags=["pc", "weapon_mastery"],
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=4),
    )
    target = Creature(
        name="Orc",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(rogue)
    register_entity(target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Rogue.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    vex_entry = CompendiumEntry(
        name="Vex",
        category="mastery",
        action_type="Passive",
        description="Vex advantage.",
        mechanics=MechanicEffect(trigger_event="on_hit", conditions_applied=[{"condition": "Vexed", "duration": "1 round"}]),
    )
    await CompendiumManager.save_entry(vault_path, vex_entry)

    shortbow = WeaponItem(name="Shortbow", damage_dice="1d6", damage_type="piercing", mastery_name="Vex")
    await ItemCompendium.save_item(vault_path, shortbow)
    await equip_item.ainvoke({"character_name": "Rogue", "item_name": "Shortbow", "item_slot": "main_hand"}, config=config)

    # First attack hits
    with mock_dice(15, 4):
        res1 = await execute_melee_attack.ainvoke({"attacker_name": "Rogue", "target_name": "Orc"}, config=config)

    assert "HIT!" in res1
    assert "Vex Mastery Triggered" in res1
    assert any(c.name == "Vexed" for c in target.active_conditions)

    # Second attack natively consumes Vex to grant advantage
    from dnd_rules_engine import GameEvent, EventBus

    with mock_dice(1, 2, 6):  # roll1, roll2, damage. (Advantage picks 2, misses AC 10)
        event = GameEvent(
            event_type="MeleeAttack", source_uuid=rogue.entity_uuid, target_uuid=target.entity_uuid, vault_path=vault_path
        )
        EventBus.dispatch(event)

    assert event.payload.get("advantage") is True
    assert event.payload.get("hit") is False
    assert not any(c.name == "Vexed" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_req_mst_006_slow_mastery_on_hit(setup_system, mock_dice):
    """
    REQ-MST-006: Slow. On hit, reduce target's speed by 10ft. Doesn't stack.
    Condition expires at start of attacker's next turn.
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
        speed=30,
        movement_remaining=30,
    )
    target = Creature(
        name="Orc",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
        movement_remaining=30,
    )
    register_entity(fighter)
    register_entity(target)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    slow_entry = CompendiumEntry(
        name="Slow",
        category="mastery",
        action_type="Passive",
        description="Slow the enemy.",
        mechanics=MechanicEffect(trigger_event="on_hit", mastery_type="slow", speed_reduction=10),
    )
    await CompendiumManager.save_entry(vault_path, slow_entry)

    longsword = WeaponItem(name="Longsword", damage_dice="1d8", damage_type="slashing", mastery_name="Slow")
    await ItemCompendium.save_item(vault_path, longsword)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Longsword", "item_slot": "main_hand"}, config=config)

    with mock_dice(15, 5):
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Orc"}, config=config)

    assert "HIT!" in res
    assert "Slow Mastery Triggered" in res
    assert any(c.name == "Slowed" for c in target.active_conditions)
    assert target.movement_remaining == 20  # 30 - 10

    # Second hit on same target should not stack
    with mock_dice(15, 5):
        res2 = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Orc"}, config=config)
    slowed_count = sum(1 for c in target.active_conditions if c.name == "Slowed")
    assert slowed_count == 1  # no stacking

    # Start of Fighter's next turn clears the Slowed condition they imposed
    sot_event = GameEvent(event_type="StartOfTurn", source_uuid=fighter.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(sot_event)
    assert not any(c.name == "Slowed" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_req_mst_004_push_mastery_on_hit(setup_system, mock_dice):
    """
    REQ-MST-004: Push. On hit, push target 10ft away. Target must be ≤ 1 size larger.
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
        x=0.0, y=0.0, size=5.0,
    )
    target = Creature(
        name="Orc",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        x=5.0, y=0.0, size=5.0,
    )
    register_entity(fighter)
    register_entity(target)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    push_entry = CompendiumEntry(
        name="Push",
        category="mastery",
        action_type="Passive",
        description="Push the enemy.",
        mechanics=MechanicEffect(trigger_event="on_hit", mastery_type="push", push_distance=10),
    )
    await CompendiumManager.save_entry(vault_path, push_entry)

    pike = WeaponItem(name="Pike", damage_dice="1d10", damage_type="piercing", mastery_name="Push")
    await ItemCompendium.save_item(vault_path, pike)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Pike", "item_slot": "main_hand"}, config=config)

    initial_x = target.x
    with mock_dice(15, 6):
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Orc"}, config=config)

    assert "HIT!" in res
    assert "Push Mastery Triggered" in res
    # Target should have moved 10ft away (positive x direction from attacker)
    assert target.x > initial_x


@pytest.mark.asyncio
async def test_req_mst_004_push_blocked_by_size(setup_system, mock_dice):
    """REQ-MST-004: Push fails if target is more than 1 size category larger."""
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
        x=0.0, y=0.0, size=5.0,
    )
    giant = Creature(
        name="Giant",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=100),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        x=5.0, y=0.0, size=15.0,  # Huge (2 categories larger)
    )
    register_entity(fighter)
    register_entity(giant)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(giant)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    push_entry = CompendiumEntry(
        name="Push",
        category="mastery",
        action_type="Passive",
        description="Push the enemy.",
        mechanics=MechanicEffect(trigger_event="on_hit", mastery_type="push", push_distance=10),
    )
    await CompendiumManager.save_entry(vault_path, push_entry)
    pike = WeaponItem(name="Pike", damage_dice="1d10", damage_type="piercing", mastery_name="Push")
    await ItemCompendium.save_item(vault_path, pike)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Pike", "item_slot": "main_hand"}, config=config)

    initial_x = giant.x
    with mock_dice(15, 6):
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Giant"}, config=config)

    assert "HIT!" in res
    assert giant.x == initial_x  # not pushed


@pytest.mark.asyncio
async def test_req_mst_001_cleave_mastery_extra_attack(setup_system, mock_dice):
    """
    REQ-MST-001: Cleave. On hit, make one extra attack against an adjacent creature
    within 5ft of the primary target and within attacker reach. Once per turn.
    Damage does not include ability modifier (unless negative).
    """
    vault_path = setup_system
    config = {"configurable": {"thread_id": vault_path}}

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        tags=["pc", "weapon_mastery", "darkvision"],
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=4),  # +4 mod — should NOT appear in cleave damage
        dexterity_mod=ModifiableValue(base_value=0),
        x=0.0, y=0.0, size=5.0,
    )
    primary_target = Creature(
        name="Orc",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["darkvision"],
        x=5.0, y=0.0, size=5.0,
    )
    cleave_target = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=8),  # low AC, easy to cleave
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["darkvision"],
        x=5.0, y=5.0, size=5.0,  # 5ft from primary target, within fighter reach
    )
    register_entity(fighter)
    register_entity(primary_target)
    register_entity(cleave_target)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(primary_target)
    spatial_service.sync_entity(cleave_target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    cleave_entry = CompendiumEntry(
        name="Cleave",
        category="mastery",
        action_type="Passive",
        description="Cleave into adjacent foe.",
        mechanics=MechanicEffect(trigger_event="on_hit", mastery_type="cleave"),
    )
    await CompendiumManager.save_entry(vault_path, cleave_entry)

    greataxe = WeaponItem(name="Greataxe", damage_dice="1d12", damage_type="slashing", mastery_name="Cleave")
    await ItemCompendium.save_item(vault_path, greataxe)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Greataxe", "item_slot": "main_hand"}, config=config)

    hp_before_cleave = cleave_target.hp.base_value
    # Rolls: hit primary (15), damage primary (8), cleave attack (15), cleave damage (6)
    with mock_dice(15, 8, 15, 6):
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Orc"}, config=config)

    assert "HIT!" in res
    assert "Cleave Mastery Triggered" in res
    # Cleave target took damage (weapon die only, no +4 ability mod)
    assert cleave_target.hp.base_value < hp_before_cleave
    # Cleave Used resource set
    assert fighter.resources.get("Cleave Used") == "1/1"


@pytest.mark.asyncio
async def test_req_mst_001_cleave_once_per_turn(setup_system, mock_dice):
    """REQ-MST-001: Cleave only fires once per turn — second hit doesn't trigger another cleave."""
    vault_path = setup_system
    config = {"configurable": {"thread_id": vault_path}}

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        tags=["pc", "weapon_mastery"],
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
        x=0.0, y=0.0, size=5.0,
    )
    fighter.resources["Cleave Used"] = "1/1"  # already used this turn

    target = Creature(
        name="Orc",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        x=5.0, y=0.0, size=5.0,
    )
    adjacent = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        x=5.0, y=5.0, size=5.0,
    )
    register_entity(fighter)
    register_entity(target)
    register_entity(adjacent)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(target)
    spatial_service.sync_entity(adjacent)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Fighter.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    cleave_entry = CompendiumEntry(
        name="Cleave",
        category="mastery",
        action_type="Passive",
        description="Cleave into adjacent foe.",
        mechanics=MechanicEffect(trigger_event="on_hit", mastery_type="cleave"),
    )
    await CompendiumManager.save_entry(vault_path, cleave_entry)
    greataxe = WeaponItem(name="Greataxe", damage_dice="1d12", damage_type="slashing", mastery_name="Cleave")
    await ItemCompendium.save_item(vault_path, greataxe)
    await equip_item.ainvoke({"character_name": "Fighter", "item_name": "Greataxe", "item_slot": "main_hand"}, config=config)

    hp_before = adjacent.hp.base_value
    with mock_dice(15, 8):
        await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Orc"}, config=config)

    assert adjacent.hp.base_value == hp_before  # no cleave damage


@pytest.mark.asyncio
async def test_req_mst_003_nick_mastery_alert(setup_system, mock_dice):
    """
    REQ-MST-003: Nick. On hit with a Light weapon, engine alerts that the extra attack
    doesn't cost a Bonus Action. Once per turn.
    """
    vault_path = setup_system
    config = {"configurable": {"thread_id": vault_path}}

    rogue = Creature(
        name="Rogue",
        vault_path=vault_path,
        tags=["pc", "weapon_mastery"],
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=3),
        x=0.0, y=0.0,
    )
    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        x=5.0, y=0.0,
    )
    register_entity(rogue)
    register_entity(target)
    spatial_service.sync_entity(rogue)
    spatial_service.sync_entity(target)

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Rogue.md"), "w") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    nick_entry = CompendiumEntry(
        name="Nick",
        category="mastery",
        action_type="Passive",
        description="Nick mastery for Light weapons.",
        mechanics=MechanicEffect(trigger_event="on_hit", mastery_type="nick"),
    )
    await CompendiumManager.save_entry(vault_path, nick_entry)

    dagger = WeaponItem(name="Dagger", damage_dice="1d4", damage_type="piercing", mastery_name="Nick")
    await ItemCompendium.save_item(vault_path, dagger)
    await equip_item.ainvoke({"character_name": "Rogue", "item_name": "Dagger", "item_slot": "main_hand"}, config=config)

    with mock_dice(15, 3):
        res = await execute_melee_attack.ainvoke({"attacker_name": "Rogue", "target_name": "Goblin"}, config=config)

    assert "HIT!" in res
    assert "Nick Mastery Triggered" in res
    assert "no Bonus Action cost" in res
    assert rogue.resources.get("Nick Used") == "1/1"
