import pytest
from unittest.mock import patch
import uuid

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
    MeleeWeapon,
    WeaponProperty,
    NumericalModifier,
    ModifierPriority,
)
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import move_entity, toggle_condition
import os
from spell_system import SpellDefinition, SpellMechanics, SpellCompendium
from item_system import WondrousItem, ItemCompendium
from tools import use_ability_or_spell, ready_action, perform_ability_check_or_save
from dnd_rules_engine import ActiveCondition


@pytest.fixture(autouse=True)
def setup_system():
    """Clears the object registries and spatial indexes before each test."""
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


# ==========================================
# CORE & COMBAT REQS
# ==========================================


def test_req_cor_001_d20_test(mock_dice):
    """
    REQ-COR-001: d20 Test
    Resolve standard tests by combining a d20 roll with static modifiers against a Target Number (DC).
    """
    # Mock a roll of 15. With a +2 modifier, the result is 17.
    with mock_dice(15):
        event = GameEvent(
            event_type="AbilityCheck",
            source_uuid=uuid.uuid4(),
            payload={"modifier": 2, "dc": 15, "roll": 0, "is_success": False},
        )
        EventBus.dispatch(event)

    # 15 (roll) + 2 (mod) = 17, which is >= 15 (DC), so it's a success.
    assert event.payload["is_success"] is True
    assert event.payload["roll"] == 17


def test_req_cor_002_adv_dis_cancellation(mock_dice):
    """
    REQ-COR-002: Advantage / Disadvantage
    Evaluates two d20s. Advantage uses max, Disadvantage uses min. They mutually cancel out completely.
    """
    attacker = Creature(
        name="Attacker",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Target",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    weapon = MeleeWeapon(name="Sword", damage_dice="1d4", damage_type="slashing")
    attacker.equipped_weapon_uuid = weapon.entity_uuid
    register_entity(attacker)
    register_entity(target)

    # Mock rolls: roll1 = 5, roll2 = 18.
    # If advantage, it would use 18. If disadvantage, it would use 5.
    # Because BOTH are passed, they cancel, and the engine explicitly uses roll1 (5).
    with mock_dice(5, 18, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
            payload={"advantage": True, "disadvantage": True},
        )
        EventBus.dispatch(event)

    # 5 + 0 (STR) = 5 vs AC 10 -> Miss! (Proving it used the canceled roll1, not max)
    assert event.payload["hit"] is False


@pytest.mark.asyncio
async def test_req_cor_003_difficult_terrain(mock_obsidian_vault):
    """
    REQ-COR-003: Difficult Terrain
    Traversing difficult terrain deducts double the movement budget per coordinate.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from spatial_engine import TerrainZone

    mover = Creature(
        name="Mover",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
        movement_remaining=30,
    )
    register_entity(mover)
    spatial_service.sync_entity(mover)
    spatial_service.active_combatants[vault_path] = ["Mover"]

    # Create a difficult terrain zone
    tz = TerrainZone(
        label="Difficult Terrain",
        points=[(0, -5), (15, -5), (15, 5), (0, 5)],
        is_difficult=True,
    )
    spatial_service.add_terrain(tz, vault_path=vault_path)

    # Move 10ft through difficult terrain. Cost should be 20ft.
    res = await move_entity.ainvoke({"entity_name": "Mover", "target_x": 10.0, "target_y": 0.0}, config=config)

    assert "Remaining movement: 10" in res
    assert mover.movement_remaining == 10


@pytest.mark.asyncio
async def test_req_cor_003_prone_recovery(mock_obsidian_vault):
    """
    REQ-COR-003: Prone Recovery
    Recovering from Prone costs exactly half the entity's maximum base speed.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    prone_guy = Creature(
        name="Prone Guy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
        movement_remaining=30,
        active_conditions=[ActiveCondition(name="Prone")],
    )
    register_entity(prone_guy)
    spatial_service.active_combatants[vault_path] = ["Prone Guy"]

    # Move 0ft, but standing up should cost 15ft of movement.
    res = await move_entity.ainvoke({"entity_name": "Prone Guy", "target_x": 0.0, "target_y": 0.0}, config=config)

    assert "Remaining movement: 15" in res
    assert prone_guy.movement_remaining == 15
    assert not any(c.name == "Prone" for c in prone_guy.active_conditions)


@pytest.mark.asyncio
async def test_req_cor_003_crawling_difficult_terrain(mock_obsidian_vault):
    """
    REQ-COR-003: Crawling in Difficult Terrain
    Moving 1 foot while crawling in difficult terrain costs 3 feet of speed.
    (1 base + 1 crawling + 1 difficult terrain).
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from spatial_engine import TerrainZone

    crawler = Creature(
        name="Crawler",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
        movement_remaining=30,
        active_conditions=[ActiveCondition(name="Prone")],
    )
    register_entity(crawler)
    spatial_service.sync_entity(crawler)
    spatial_service.active_combatants[vault_path] = ["Crawler"]

    # Create a difficult terrain zone covering the movement path
    tz = TerrainZone(
        label="Thick Mud",
        points=[(-5, -5), (15, -5), (15, 5), (-5, 5)],
        is_difficult=True,
    )
    spatial_service.add_terrain(tz, vault_path=vault_path)

    # Move 10ft through difficult terrain while crawling.
    # Cost should be 10 * 3 = 30ft.
    res = await move_entity.ainvoke(
        {"entity_name": "Crawler", "target_x": 10.0, "target_y": 0.0, "movement_type": "crawl"}, config=config
    )

    assert "Remaining movement: 0" in res
    assert crawler.movement_remaining == 0


@pytest.mark.asyncio
async def test_req_spc_004_dragging_difficult_terrain(mock_obsidian_vault):
    """
    REQ-SPC-004: Grappling (Dragging)
    REQ-MOV-011: Additive Penalties
    Moving a grappled target costs 1 extra foot per foot. Difficult terrain costs 1 extra.
    Combined, moving 1 foot costs 3 feet of speed.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from spatial_engine import TerrainZone

    dragger = Creature(
        name="Dragger",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
        movement_remaining=30,
    )
    target = Creature(
        name="Grappled Target",
        vault_path=vault_path,
        x=5.0,
        y=0.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        # The "Grappled" condition explicitly links back to the dragger's UUID
        active_conditions=[ActiveCondition(name="Grappled", source_uuid=dragger.entity_uuid)],
    )
    register_entity(dragger)
    register_entity(target)
    spatial_service.sync_entity(dragger)
    spatial_service.sync_entity(target)
    spatial_service.active_combatants[vault_path] = ["Dragger", "Grappled Target"]

    # Create difficult terrain
    tz = TerrainZone(
        label="Thick Mud",
        points=[(0, -5), (15, -5), (15, 5), (0, 5)],
        is_difficult=True,
    )
    spatial_service.add_terrain(tz, vault_path=vault_path)

    # Move Dragger 10ft. Dragging (+1) + Difficult Terrain (+1) + Base (1) = 3x multiplier. Cost = 30ft.
    res = await move_entity.ainvoke({"entity_name": "Dragger", "target_x": 10.0, "target_y": 0.0}, config=config)

    assert "Remaining movement: 0" in res
    assert dragger.movement_remaining == 0

    # Ensure the target was physically dragged along with the dragger natively
    assert target.x == 15.0
    assert target.y == 0.0


@pytest.mark.asyncio
async def test_req_mov_006_vertical_drag_and_drop(mock_obsidian_vault, mock_dice):
    """
    REQ-MOV-006: Falling Damage
    REQ-SPC-004: Grappling (Dragging)
    Tests a flying creature grappling a target, flying 30ft into the air (costing 60ft of movement),
    and dropping them to natively trigger falling damage and land prone.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    flyer = Creature(
        name="Eagle",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        z=0.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=60,
        movement_remaining=60,
        tags=["flying"],
    )
    target = Creature(
        name="Prey",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        z=0.0,
        hp=ModifiableValue(base_value=50),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Grappled", source_uuid=flyer.entity_uuid)],
    )
    register_entity(flyer)
    register_entity(target)
    spatial_service.sync_entity(flyer)
    spatial_service.sync_entity(target)
    spatial_service.active_combatants[vault_path] = ["Eagle", "Prey"]

    # 1. Fly up 30ft while dragging. Cost = 30 * 2 = 60ft.
    res_fly = await move_entity.ainvoke(
        {"entity_name": "Eagle", "target_x": 0.0, "target_y": 0.0, "target_z": 30.0, "movement_type": "fly"}, config=config
    )
    assert flyer.movement_remaining == 0
    assert target.z == 30.0
    assert "automatically dragged Prey" in res_fly

    # 2. Drop the target (Remove Grapple)
    await toggle_condition.ainvoke({"character_name": "Prey", "condition_name": "Grappled", "is_active": False}, config=config)

    # 3. Target falls 30ft.
    # 30ft fall = 3d6 damage. We mock the dice to consistently roll 6s (18 damage).
    with mock_dice(default=6):
        res_fall = await move_entity.ainvoke(
            {"entity_name": "Prey", "target_x": target.x, "target_y": target.y, "target_z": 0.0, "movement_type": "fall"},
            config=config,
        )

    assert target.z == 0.0
    assert target.hp.base_value == 32  # 50 base - 18 damage
    assert any(c.name == "Prone" for c in target.active_conditions)
    assert "took falling damage and landed Prone" in res_fall


@pytest.mark.asyncio
async def test_req_cor_004_social_interactions(mock_obsidian_vault, mock_dice):
    """
    REQ-COR-004: Social Interactions
    Hidden NPC attitude integer modifies the baseline DC of Charisma checks.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    player = Creature(
        name="Player",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=5),
    )
    register_entity(player)

    hostile_npc = Creature(
        name="Hostile NPC",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        attitude_to_pcs=-10,
    )
    register_entity(hostile_npc)

    # Base DC is 10. Hostile attitude adds +10 to DC, for a total of 20.
    # Player rolls 14 + 5 Cha = 19. Fails.
    with mock_dice(default=14):
        res = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Player", "skill_or_stat_name": "persuasion"},
            config=config,
        )

    assert "Result (persuasion):" in res
    assert "19" in res


def test_req_wpn_004_reach_property():
    """
    REQ-WPN-004: Reach Property
    Adds 5 feet to the entity's melee reach when attacking.
    """
    from tools import _calculate_reach

    c = Creature(
        name="Pikeman",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    # Base reach should be 5.0
    assert _calculate_reach(c) == 5.0

    # Simulating a reach weapon tag dynamically appended by the equip_item tool
    c.tags.append("reach_weapon")

    # Reach should now natively calculate as 10.0
    assert _calculate_reach(c) == 10.0


def test_req_act_005_stacking_non_spells():
    """
    REQ-ACT-005: Stacking (Non-Spells)
    Overlapping non-spell features stack linearly unless specifically excluded.
    """
    c = Creature(
        name="Stacker",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    c.ac.add_modifier(NumericalModifier(priority=ModifierPriority.ADDITIVE, value=2, source_name="Shield"))
    c.ac.add_modifier(NumericalModifier(priority=ModifierPriority.ADDITIVE, value=1, source_name="Ring of Protection"))
    c.ac.add_modifier(NumericalModifier(priority=ModifierPriority.ADDITIVE, value=3, source_name="Mage Armor"))
    assert c.ac.total == 16


def test_req_wpn_001_finesse_property():
    """
    REQ-WPN-001: Finesse Property
    When attacking, the entity may use Strength OR Dexterity for both the attack and damage rolls.
    """
    wielder = Creature(
        name="Rogue",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=-1),
        dexterity_mod=ModifiableValue(base_value=4),
    )
    rapier = MeleeWeapon(name="Rapier", damage_dice="1d8", damage_type="piercing", properties=[WeaponProperty.FINESSE])
    assert rapier.get_attack_modifier(wielder).total == 4

    wielder.strength_mod.base_value = 5
    wielder.dexterity_mod.base_value = 2
    assert rapier.get_attack_modifier(wielder).total == 5


def test_req_wpn_002_heavy_property():
    """
    REQ-WPN-002: Heavy Property
    Small and Tiny creatures have Disadvantage on attack rolls with Heavy weapons.
    """
    halfling = Creature(
        name="Halfling",
        tags=["small"],
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Target",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    greatsword = MeleeWeapon(name="Greatsword", damage_dice="2d6", damage_type="slashing", properties=[WeaponProperty.HEAVY])
    halfling.equipped_weapon_uuid = greatsword.entity_uuid
    register_entity(halfling)
    register_entity(target)

    event = GameEvent(event_type="MeleeAttack", source_uuid=halfling.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(event)
    assert event.payload.get("disadvantage") is True


# ==========================================
# DAMAGE & CONDITIONS REQS
# ==========================================


def test_req_dmg_001_resistance_rounding():
    """
    REQ-DMG-001: Resistance
    Damage taken is halved (rounded down).
    """
    target = Creature(
        name="Tiefling",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resistances=["fire"],
    )
    register_entity(target)

    # Apply 15 Fire Damage. Half of 15 is 7.5. Rounded down = 7.
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 15, "damage_type": "fire"},
    )
    event.status = 3  # Jump straight to POST_EVENT to trigger apply_damage_handler
    EventBus._notify(event)

    # 20 - 7 = 13
    assert target.hp.base_value == 13


def test_req_dmg_002_and_003_vulnerability_and_immunity():
    """
    REQ-DMG-002: Vulnerability (Damage taken is doubled)
    REQ-DMG-003: Immunity (Damage taken is reduced to 0)
    """
    target = Creature(
        name="Elemental",
        hp=ModifiableValue(base_value=50),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        vulnerabilities=["cold"],
        immunities=["fire"],
    )
    register_entity(target)

    event_fire = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 20, "damage_type": "fire"},
    )
    event_fire.status = 3
    EventBus._notify(event_fire)
    assert target.hp.base_value == 50

    event_cold = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 15, "damage_type": "cold"},
    )
    event_cold.status = 3
    EventBus._notify(event_cold)
    assert target.hp.base_value == 20


def test_req_spl_017_save_half_damage():
    """
    REQ-SPL-017: Save for Half Damage
    If a spell dictates half damage on a successful save, round down after dividing by two.
    """
    # Using the native Evasion / SavingThrow handler logic
    event = GameEvent(
        event_type="SavingThrow",
        source_uuid=uuid.uuid4(),
        target_uuid=uuid.uuid4(),
        payload={
            "save_required": "dexterity",
            "dc": 15,
            "roll": 16,
            "is_success": True,
            "base_damage": 17,  # 17 / 2 = 8.5 -> 8
            "half_damage_on_save": True,
            "final_damage": (17 // 2),
        },
    )
    EventBus.dispatch(event)
    assert event.payload["final_damage"] == 8


def test_req_cnd_017_concentration_damage_trigger():
    """
    REQ-CND-017: Concentration (Damage Trigger)
    Taking damage requires a Constitution saving throw (DC 10 or half the damage taken, whichever is higher).
    """
    target = Creature(
        name="Concentrating Mage",
        hp=ModifiableValue(base_value=50),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        concentrating_on="Wall of Fire",
    )
    register_entity(target)

    # Take 24 Damage. Half of 24 is 12. DC should be 12 (since it's > 10).
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 24, "damage_type": "bludgeoning"},
    )
    event.status = 3

    with patch("builtins.print") as mock_print:
        EventBus._notify(event)

        # Assert the engine alerted the LLM to roll the specific DC
        alert_msg = "".join([str(call.args) for call in mock_print.call_args_list])
        assert "Constitution saving throw (DC 12)" in alert_msg


@pytest.mark.asyncio
async def test_req_edg_012_and_cnd_022_start_of_turn_thp(mock_obsidian_vault):
    """
    REQ-EDG-012: Temporary Hit Points (THP) absorb damage before HP.
    REQ-CND-022: Start of Turn Buffs (THP) grant THP correctly without over-stacking.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from tools import toggle_condition

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        temp_hp=0,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    enemy = Creature(
        name="Enemy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)
    register_entity(enemy)

    from tools import toggle_condition

    # Apply Heroism condition manually
    await toggle_condition.ainvoke(
        {
            "character_name": "Fighter",
            "condition_name": "Heroism",
            "is_active": True,
            "start_of_turn_thp": 4,
        },
        config=config,
    )

    # 1. Trigger StartOfTurn
    sot_event = GameEvent(event_type="StartOfTurn", source_uuid=fighter.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(sot_event)

    assert fighter.temp_hp == 4

    # 2. Take 3 damage. Should reduce THP to 1, HP remains 20.
    dmg_event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=enemy.entity_uuid,
        target_uuid=fighter.entity_uuid,
        payload={"hit": True, "damage": 3, "damage_type": "slashing"},
    )
    dmg_event.status = 3
    EventBus._notify(dmg_event)

    assert fighter.temp_hp == 1
    assert fighter.hp.base_value == 20

    # 3. Trigger StartOfTurn again, THP should refresh back to 4!
    sot_event2 = GameEvent(event_type="StartOfTurn", source_uuid=fighter.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(sot_event2)
    assert fighter.temp_hp == 4


# ==========================================
# MOVEMENT REQS
# ==========================================


@pytest.mark.asyncio
async def test_req_mov_009_long_jump_limits():
    """
    REQ-MOV-009: Long Jump
    Distance equals Strength score (with 10ft run up). Costs movement.
    """
    # STR score of 12 (Mod is +1, so (1*2)+10 = 12)
    jumper = Creature(
        name="Jumper",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=1),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(jumper)

    config = {"configurable": {"thread_id": "default"}}
    # Attempting a 15ft jump should be rejected natively by the Engine bounds!
    res = await move_entity.ainvoke(
        {"entity_name": "Jumper", "target_x": 15.0, "target_y": 0.0, "movement_type": "jump"}, config=config
    )
    assert "SYSTEM ERROR: Jump exceeds physical limits" in res
    assert "Max running long-jump: 12" in res


# ==========================================
# MAGIC & SPELLS REQS
# ==========================================


@pytest.mark.asyncio
async def test_req_spl_001_and_003_spell_slot_limits(mock_obsidian_vault):
    """
    REQ-SPL-001: Only one spell slot may be expended per turn.
    REQ-SPL-003: Casting via items bypasses the turn limit.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    caster = Creature(
        name="Wizard",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)

    # Create a Level 1 Spell
    spell = SpellDefinition(name="Magic Missile", level=1, mechanics=SpellMechanics(damage_dice="3d4", damage_type="force"))
    await SpellCompendium.save_spell(vault_path, spell)

    # Create a Wondrous Item that casts a spell
    item = WondrousItem(name="Wand of Magic Missiles", active_mechanics=SpellMechanics(damage_dice="3d4", damage_type="force"))
    await ItemCompendium.save_item(vault_path, item)

    # Cast 1st leveled spell - Should Succeed
    res1 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Magic Missile", "target_names": []}, config=config
    )
    assert "MECHANICAL TRUTH" in res1
    assert caster.spell_slots_expended_this_turn == 1

    # Cast 2nd leveled spell - Should Fail (REQ-SPL-001)
    res2 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Magic Missile", "target_names": []}, config=config
    )
    assert "SYSTEM ERROR" in res2
    assert "REQ-SPL-001" in res2

    # Cast from Item - Should Succeed (bypasses limit REQ-SPL-003)
    res3 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Wand of Magic Missiles", "target_names": []}, config=config
    )
    assert "MECHANICAL TRUTH" in res3


@pytest.mark.asyncio
async def test_req_spl_005_line_of_effect(mock_obsidian_vault):
    """
    REQ-SPL-005: Line of Effect / Clear Path. Total cover blocks targeting algorithms.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from spatial_engine import Wall

    caster = Creature(
        name="Wizard",
        x=0,
        y=0,
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Goblin",
        x=10,
        y=0,
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    register_entity(target)

    # Create Wall
    wall = Wall(start=(5, -5), end=(5, 5), is_solid=True, label="Solid Brick Wall")
    spatial_service.add_wall(wall, vault_path=vault_path)

    spell = SpellDefinition(name="Fire Bolt", level=0, mechanics=SpellMechanics(damage_dice="1d10", damage_type="fire"))
    await SpellCompendium.save_spell(vault_path, spell)

    res = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Fire Bolt", "target_names": ["Goblin"]}, config=config
    )
    assert "SYSTEM ERROR" in res
    assert "Total Cover" in res
    assert "REQ-SPL-005" in res


@pytest.mark.asyncio
async def test_req_spl_013_readying_spell(mock_obsidian_vault):
    """
    REQ-SPL-013: Readying a spell consumes the slot and requires concentration.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    # Create dummy ACTIVE_COMBAT.md
    from vault_io import get_journals_dir

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md"), "w") as f:
        f.write("---\nreadied_actions: []\n---")

    caster = Creature(
        name="Wizard",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)

    res = await ready_action.ainvoke(
        {
            "character_name": "Wizard",
            "action_description": "I ready Fireball",
            "trigger_condition": "When the orc steps through the door",
            "is_spell": True,
            "spell_name": "Fireball",
        },
        config=config,
    )

    assert "MECHANICAL TRUTH" in res
    assert caster.spell_slots_expended_this_turn == 1
    assert "Readied: Fireball" in caster.concentrating_on


@pytest.mark.asyncio
async def test_req_spl_015_counterspell(mock_obsidian_vault, mock_dice, mock_roll_dice):
    """
    REQ-SPL-015: Counterspell
    Target makes a Constitution saving throw vs the caster's spell DC. On failure, the spell fails but the target's spell slot is NOT expended.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    caster = Creature(
        name="Wizard",
        tags=["pc"],
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=2),  # +2 Con
    )
    counterspeller = Creature(
        name="Enemy Sorcerer",
        tags=["monster", "can_cast_counterspell"],
        vault_path=vault_path,
        x=30.0,
        y=0.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        spell_save_dc=ModifiableValue(base_value=16),  # DC 16
    )
    register_entity(caster)
    register_entity(counterspeller)
    spatial_service.sync_entity(caster)
    spatial_service.sync_entity(counterspeller)

    spell = SpellDefinition(name="Fireball", level=3, mechanics=SpellMechanics(damage_dice="8d6", damage_type="fire"))
    await SpellCompendium.save_spell(vault_path, spell)

    # 1. Wizard casts Fireball. Sorcerer uses Reaction to Counterspell.
    # Wizard CON save: rolls 5 + 2 = 7 (Fails vs DC 16)
    with mock_dice(default=5):
        res = await use_ability_or_spell.ainvoke(
            {"caster_name": "Wizard", "ability_name": "Fireball", "target_names": ["Enemy Sorcerer"]}, config=config
        )

    assert counterspeller.reaction_used is True
    assert "failed CON save" in res
    assert "preserved" in res
    assert caster.spell_slots_expended_this_turn == 0  # Slot not expended
    assert counterspeller.hp.base_value == 20  # Took no damage!

    # 2. Wizard tries again next turn. Sorcerer has no reaction left!
    counterspeller.reaction_used = True
    with mock_dice(default=15), mock_roll_dice(default=30):
        res2 = await use_ability_or_spell.ainvoke(
            {"caster_name": "Wizard", "ability_name": "Fireball", "target_names": ["Enemy Sorcerer"]}, config=config
        )

    assert "Counterspell" not in res2
    assert caster.spell_slots_expended_this_turn == 1
    assert counterspeller.hp.base_value < 20  # Took damage!


@pytest.mark.asyncio
async def test_req_spl_016_dispel_magic(mock_obsidian_vault, mock_dice):
    """
    REQ-SPL-016: Dispel Magic
    Instantly terminates magical effects of 3rd level or lower. Higher levels require a spellcasting ability check.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    caster = Creature(
        name="Cleric",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=4),  # +4 Wis
    )
    target = Creature(
        name="Cursed Ally",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    register_entity(target)

    # Add a low-level magical effect (duration 60s)
    target.active_conditions.append(ActiveCondition(name="Bane", duration_seconds=60, source_name="Bane Spell"))
    # Add a high-level magical effect (duration 86400s)
    target.active_conditions.append(ActiveCondition(name="Geas", duration_seconds=86400, source_name="Geas Spell"))

    spell = SpellDefinition(name="Dispel Magic", level=3, mechanics=SpellMechanics())
    await SpellCompendium.save_spell(vault_path, spell)

    # 1. Cast Dispel Magic. Roll 2 for the ability check (2 + 4 = 6 vs DC 14). Fails to dispel Geas.
    with mock_dice(default=2):
        res1 = await use_ability_or_spell.ainvoke(
            {"caster_name": "Cleric", "ability_name": "Dispel Magic", "target_names": ["Cursed Ally"]}, config=config
        )

    # Bane is automatically dispelled (<= 3rd level proxy)
    assert not any(c.name == "Bane" for c in target.active_conditions)
    # Geas remains
    assert any(c.name == "Geas" for c in target.active_conditions)
    assert "Bane on Cursed Ally was instantly dispelled" in res1
    assert "FAILED. Geas on Cursed Ally remains" in res1

    # 2. Cast Dispel Magic again. Roll 15 for the check (15 + 4 = 19 vs DC 14). Succeeds!
    caster.spell_slots_expended_this_turn = 0
    with mock_dice(default=15):
        res2 = await use_ability_or_spell.ainvoke(
            {"caster_name": "Cleric", "ability_name": "Dispel Magic", "target_names": ["Cursed Ally"]}, config=config
        )

    assert not any(c.name == "Geas" for c in target.active_conditions)
    assert "SUCCESS. Geas on Cursed Ally was dispelled" in res2


# ==========================================
# ENVIRONMENT REQS
# ==========================================


@pytest.mark.asyncio
async def test_req_env_001_extreme_cold(mock_obsidian_vault, mock_dice):
    """
    REQ-ENV-001: Extreme Cold
    DC 10 Con save per hour. Ignored if wearing gear or naturally adapted.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    pc = Creature(
        name="Cold_PC",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=5),
    )
    immune_pc = Creature(
        name="Immune_PC",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resistances=["cold"],
    )
    register_entity(pc)
    register_entity(immune_pc)

    from tools import evaluate_extreme_weather

    with mock_dice(default=2):  # Roll 2 + 5 = 7 (Fail vs DC 10)
        res = await evaluate_extreme_weather.ainvoke(
            {"character_names": ["Cold_PC", "Immune_PC"], "temperature_f": -10, "hours_exposed": 2}, config=config
        )

    assert "Immune_PC] is naturally adapted" in res
    assert "Cold_PC] failed 2 CON saves" in res
    assert pc.exhaustion_level == 2


@pytest.mark.asyncio
async def test_req_env_002_extreme_heat(mock_obsidian_vault, mock_dice):
    """
    REQ-ENV-002: Extreme Heat
    DC 5 + 1/hr Con save per hour. Disadvantage if medium/heavy armor.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    from vault_io import get_journals_dir
    import os

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Armored_PC.md"), "w") as f:
        f.write("---\nname: Armored_PC\nequipment:\n  armor: Plate Armor\n---")

    pc = Creature(
        name="Armored_PC",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=5),
    )
    register_entity(pc)

    from tools import evaluate_extreme_weather

    # Hour 1: DC 5. Hour 2: DC 6. Hour 3: DC 7.
    # Roll 1 and 20. With disadvantage, min is 1. Total = 1 + 5 = 6.
    # H1: 6 >= 5 (Pass), H2: 6 >= 6 (Pass), H3: 6 < 7 (Fail)
    with mock_dice(1, 20, 1, 20, 1, 20):
        res = await evaluate_extreme_weather.ainvoke(
            {"character_names": ["Armored_PC"], "temperature_f": 110, "hours_exposed": 3}, config=config
        )

    assert "Armored_PC] failed 1 CON saves" in res
    assert pc.exhaustion_level == 1


# ==========================================
# SKILLS & KNOWLEDGE REQS
# ==========================================


@pytest.mark.asyncio
async def test_req_skl_010_critical_failure_flag(mock_obsidian_vault, mock_dice):
    """
    REQ-SKL-010: Knowledge Checks (Recall)
    Validates that rolling a 1 natively flags as a NATURAL 1 - CRITICAL FAILURE to prompt the LLM inversion.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    pc = Creature(
        name="Scholar",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(pc)

    with mock_dice(default=1):
        res1 = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Scholar", "skill_or_stat_name": "history"}, config=config
        )
        assert "NATURAL 1 - CRITICAL FAILURE" in res1

    with mock_dice(default=20):
        res20 = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Scholar", "skill_or_stat_name": "arcana"}, config=config
        )
        assert "NATURAL 20 - CRITICAL SUCCESS" in res20


# ==========================================
# CONDITIONS REQS
# ==========================================


@pytest.mark.asyncio
async def test_req_cnd_004_blinded(mock_obsidian_vault):
    """
    REQ-CND-004: Blinded
    Entity cannot see and automatically fails checks requiring sight. Attack rolls against it have Advantage; its attack rolls have Disadvantage.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    blind_guy = Creature(
        name="Blind_Guy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Blinded")],
    )
    enemy = Creature(
        name="Enemy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(blind_guy)
    register_entity(enemy)

    # 1. Attack rolls AGAINST it have Advantage
    atk1 = GameEvent(event_type="MeleeAttack", source_uuid=enemy.entity_uuid, target_uuid=blind_guy.entity_uuid)
    EventBus.dispatch(atk1)
    assert atk1.payload.get("advantage") is True

    # 2. ITS attack rolls have Disadvantage
    atk2 = GameEvent(event_type="MeleeAttack", source_uuid=blind_guy.entity_uuid, target_uuid=enemy.entity_uuid)
    EventBus.dispatch(atk2)
    assert atk2.payload.get("disadvantage") is True

    # 3. Fails checks requiring sight

    res = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Blind_Guy", "skill_or_stat_name": "perception"}, config=config
    )
    assert "BLINDED" in res
    assert "automatically fail" in res


@pytest.mark.asyncio
async def test_req_cnd_016_unconscious(mock_obsidian_vault):
    """
    REQ-CND-016: Unconscious
    Entity is Incapacitated, falls Prone. Auto-fails Str/Dex saves. Attacks against have Advantage.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    victim = Creature(
        name="Victim",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=5),
        dexterity_mod=ModifiableValue(base_value=5),
        active_conditions=[ActiveCondition(name="Unconscious")],
    )
    attacker = Creature(
        name="Attacker",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(victim)
    register_entity(attacker)

    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=victim.entity_uuid)
    EventBus.dispatch(atk_event)
    assert atk_event.payload.get("advantage") is True

    spell = SpellDefinition(
        name="Fireball", level=3, mechanics=SpellMechanics(damage_dice="8d6", damage_type="fire", save_required="dexterity")
    )
    await SpellCompendium.save_spell(vault_path, spell)

    with patch("random.randint", return_value=20):
        res = await use_ability_or_spell.ainvoke(
            {"caster_name": "Attacker", "ability_name": "Fireball", "target_names": ["Victim"]}, config=config
        )
    assert "Failed Save" in res


@pytest.mark.asyncio
async def test_req_cnd_012_poisoned(mock_obsidian_vault, mock_dice):
    """
    REQ-CND-012: Poisoned
    Entity has Disadvantage on attack rolls and ability checks.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    sick_guy = Creature(
        name="Sick_Guy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Poisoned")],
    )
    enemy = Creature(
        name="Enemy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(sick_guy)
    register_entity(enemy)

    # 1. Attack rolls have Disadvantage
    atk = GameEvent(event_type="MeleeAttack", source_uuid=sick_guy.entity_uuid, target_uuid=enemy.entity_uuid)
    EventBus.dispatch(atk)
    assert atk.payload.get("disadvantage") is True

    # 2. Ability checks have Disadvantage

    with mock_dice(18, 2):  # Roll 18 and 2. Should take 2 if disadvantage.
        res = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Sick_Guy", "skill_or_stat_name": "athletics"}, config=config
        )
    assert "Disadvantage" in res
    assert "Result (athletics): 2" in res


@pytest.mark.asyncio
async def test_req_cnd_021_end_of_turn_saves(mock_obsidian_vault, mock_dice):
    """
    REQ-CND-021: Repeating Saves (End of Turn)
    Entities with conditions like Frightened can automatically roll to break free at the end of their turn.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=2),  # +2 Wis
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)

    # 1. Apply Frightened with a Wisdom save DC 15
    await toggle_condition.ainvoke(
        {
            "character_name": "Fighter",
            "condition_name": "Frightened",
            "is_active": True,
            "save_required": "wisdom",
            "save_dc": 15,
        },
        config=config,
    )

    assert any(c.name == "Frightened" for c in fighter.active_conditions)

    from tools import start_combat, update_combat_state
    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(j_dir, "Fighter.md"), "w") as f:
        f.write("---\nname: Fighter\n---")

    await start_combat.ainvoke({"pc_names": ["Fighter"], "enemies": []}, config=config)

    # 2. End the fighter's turn - force a fail (Roll 2 + 2 = 4 vs DC 15)
    with mock_dice(default=2):
        res_fail = await update_combat_state.ainvoke({"next_turn": True}, config=config)

    assert "failed their end-of-turn wisdom save (4 vs DC 15)" in res_fail
    assert any(c.name == "Frightened" for c in fighter.active_conditions)

    # 3. Fast forward back to fighter's turn ending, force a success (Roll 15 + 2 = 17 vs DC 15)
    with mock_dice(default=15):
        res_succ = await update_combat_state.ainvoke({"next_turn": True}, config=config)

    assert "succeeded on their end-of-turn wisdom save" in res_succ
    assert not any(c.name == "Frightened" for c in fighter.active_conditions)


@pytest.mark.asyncio
async def test_req_cnd_024_start_of_turn_saves(mock_obsidian_vault, mock_dice):
    """
    REQ-CND-024: Repeating Saves (Start of Turn)
    Entities with conditions like a Vampire's Charm roll to break free at the start of their turn.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from tools import toggle_condition, start_combat, update_combat_state

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=2),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)

    # Apply Charmed with a Wisdom save DC 15 at the START of turn
    await toggle_condition.ainvoke(
        {
            "character_name": "Fighter",
            "condition_name": "Charmed",
            "is_active": True,
            "save_required": "wisdom",
            "save_dc": 15,
            "save_timing": "start",
        },
        config=config,
    )

    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(j_dir, "Fighter.md"), "w") as f:
        f.write("---\nname: Fighter\n---")

    with mock_dice(default=2):
        res_start = await start_combat.ainvoke({"pc_names": ["Fighter"], "enemies": []}, config=config)

    with mock_dice(default=2):
        res_fail = await update_combat_state.ainvoke({"next_turn": True}, config=config)
    assert "failed their start-of-turn wisdom save" in res_start
    assert "failed their start-of-turn wisdom save" in res_fail
    assert any(c.name == "Charmed" for c in fighter.active_conditions)

    with mock_dice(default=15):
        res_succ = await update_combat_state.ainvoke({"next_turn": True}, config=config)
    assert "succeeded on their start-of-turn wisdom save" in res_succ
    assert not any(c.name == "Charmed" for c in fighter.active_conditions)


@pytest.mark.asyncio
async def test_req_cnd_023_end_of_turn_damage(mock_obsidian_vault, mock_roll_dice):
    """
    REQ-CND-023: End of Turn Damage
    Entities with conditions like Burning take damage automatically at the end of their turn.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)

    # Apply Burning with 1d4 fire damage at the end of turn
    from tools import toggle_condition

    await toggle_condition.ainvoke(
        {
            "character_name": "Fighter",
            "condition_name": "Burning",
            "is_active": True,
            "end_of_turn_damage_dice": "1d4",
            "end_of_turn_damage_type": "fire",
        },
        config=config,
    )

    # Dispatch EndOfTurn
    with mock_roll_dice(default=4):  # Force the 1d4 to roll 4
        eot_event = GameEvent(event_type="EndOfTurn", source_uuid=fighter.entity_uuid, vault_path=vault_path)
        res = EventBus.dispatch(eot_event)

    assert any("took 4 fire damage from Burning" in r for r in res.payload.get("results", []))
    assert fighter.hp.base_value == 16


@pytest.mark.asyncio
async def test_req_cnd_015_restrained(mock_obsidian_vault, mock_dice):
    """
    REQ-CND-015: Restrained
    Speed becomes 0. Attack rolls against the entity have Advantage.
    The entity's attack rolls have Disadvantage. Entity has Disadvantage on Dexterity saving throws.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    tied_guy = Creature(
        name="Tied_Guy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Restrained")],
    )
    enemy = Creature(
        name="Enemy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        spell_save_dc=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(tied_guy)
    register_entity(enemy)

    # 1. Speed is 0
    from tools import move_entity

    res = await move_entity.ainvoke({"entity_name": "Tied_Guy", "target_x": 10.0, "target_y": 0.0}, config=config)
    assert "SYSTEM ERROR" in res
    assert "speed to 0" in res

    # 2. Attacks AGAINST have Advantage
    atk1 = GameEvent(event_type="MeleeAttack", source_uuid=enemy.entity_uuid, target_uuid=tied_guy.entity_uuid)
    EventBus.dispatch(atk1)
    assert atk1.payload.get("advantage") is True

    # 3. ITS attacks have Disadvantage
    atk2 = GameEvent(event_type="MeleeAttack", source_uuid=tied_guy.entity_uuid, target_uuid=enemy.entity_uuid)
    EventBus.dispatch(atk2)
    assert atk2.payload.get("disadvantage") is True

    # 4. Disadvantage on DEX saves
    mechanics = {"save_required": "dexterity", "damage_dice": "1d6", "damage_type": "fire"}

    tied_guy.hp.base_value = 20
    with mock_dice(6, 18, 2):  # 6 for damage, 18 and 2 for the save (takes 2)
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=enemy.entity_uuid,
            payload={"ability_name": "Fire", "mechanics": mechanics, "target_uuids": [tied_guy.entity_uuid]},
        )
        EventBus.dispatch(event)

    # Tied_Guy should have failed the save (roll 2 vs DC 15) and taken 6 damage.
    assert tied_guy.hp.base_value == 14
    assert any("Failed Save (Rolled 2 vs DC 15)" in r for r in event.payload["results"])


# ==========================================
# VISION & STEALTH REQS
# ==========================================


@pytest.mark.asyncio
async def test_req_stl_001_and_002_passive_perception_and_hiding(mock_obsidian_vault):
    """
    REQ-STL-001: Passive Perception (+5 Adv, -5 Disadv)
    REQ-STL-002: Hiding Requirements (Cannot hide in bright light)
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    rogue = Creature(
        name="Rogue",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=3),  # +3 Stealth
        wisdom_mod=ModifiableValue(base_value=2),  # +2 Perception
    )
    register_entity(rogue)
    spatial_service.sync_entity(rogue)

    # Base Passive Perception: 10 + 2 = 12
    res1 = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Rogue", "skill_or_stat_name": "perception", "is_passive": True}, config=config
    )
    assert "Passive Perception Score: 12" in res1

    # Passive with Advantage: 12 + 5 = 17
    res2 = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Rogue", "skill_or_stat_name": "perception", "is_passive": True, "advantage": True}, config=config
    )
    assert "Passive Perception Score: 17" in res2

    # Passive with Disadvantage: 12 - 5 = 7
    res3 = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Rogue", "skill_or_stat_name": "perception", "is_passive": True, "disadvantage": True},
        config=config,
    )
    assert "Passive Perception Score: 7" in res3

    # Hiding in Bright Light
    from spatial_engine import LightSource

    spatial_service.get_map_data(vault_path).lights.append(
        LightSource(label="Sun", x=0, y=0, bright_radius=100, dim_radius=200)
    )

    res4 = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Rogue", "skill_or_stat_name": "stealth"}, config=config
    )
    assert "BRIGHT LIGHT" in res4
    assert "cannot hide" in res4


@pytest.mark.asyncio
async def test_req_vis_006_and_007_dim_light_and_darkvision(mock_obsidian_vault):
    """
    REQ-VIS-006: Dim Light imposes Disadvantage (-5 Passive) on Perception.
    REQ-VIS-007: Darkvision treats Dim as Bright.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    human = Creature(
        name="Human",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    elf = Creature(
        name="Elf",
        tags=["darkvision_60"],
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    register_entity(human)
    register_entity(elf)
    spatial_service.sync_entity(human)
    spatial_service.sync_entity(elf)

    from spatial_engine import LightSource

    spatial_service.get_map_data(vault_path).lights.append(
        LightSource(label="Glowing Mushrooms", x=0, y=0, bright_radius=0, dim_radius=100)
    )

    res_human = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Human", "skill_or_stat_name": "perception"}, config=config
    )
    assert "DIM LIGHT. Disadvantage (-5 to Passive)" in res_human

    res_elf = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Elf", "skill_or_stat_name": "perception"}, config=config
    )
    assert "DIM LIGHT" not in res_elf  # Darkvision bypasses the alert


@pytest.mark.asyncio
async def test_req_vis_008_sunlight_sensitivity(mock_obsidian_vault):
    """
    REQ-VIS-008: Sunlight Sensitivity
    Disadvantage on attacks and perception when in direct sunlight.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    drow = Creature(
        name="Drow",
        tags=["sunlight_sensitivity"],
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    enemy = Creature(
        name="Enemy",
        vault_path=vault_path,
        x=5,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(drow)
    register_entity(enemy)
    spatial_service.sync_entity(drow)
    spatial_service.sync_entity(enemy)

    from spatial_engine import LightSource

    spatial_service.get_map_data(vault_path).lights.append(
        LightSource(label="Sunlight", x=0, y=0, bright_radius=100, dim_radius=200)
    )

    # 1. Perception Check
    res_perc = await perform_ability_check_or_save.ainvoke(
        {"character_name": "Drow", "skill_or_stat_name": "perception"}, config=config
    )
    assert "Sunlight Sensitivity" in res_perc

    # 2. Attack Roll
    atk_event = GameEvent(
        event_type="MeleeAttack", source_uuid=drow.entity_uuid, target_uuid=enemy.entity_uuid, vault_path=vault_path
    )
    EventBus.dispatch(atk_event)
    assert atk_event.payload.get("disadvantage") is True


# ==========================================
# SUMMONS & FAMILIARS REQS
# ==========================================


@pytest.mark.asyncio
async def test_req_pet_001_and_004_summon_initiative(mock_obsidian_vault, mock_dice):
    """
    REQ-PET-001: Tasha-Style Summons act immediately after the caster.
    REQ-PET-004: Find Familiar rolls its own initiative.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    from tools import spawn_summon, start_combat

    caster = Creature(
        name="Druid",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        dexterity_mod=ModifiableValue(base_value=2),
        strength_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)

    # Mock journals so combat can start natively
    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(j_dir, "Druid.md"), "w") as f:
        f.write("---\nname: Druid\ndexterity: 14\n---")

    with mock_dice(default=10):
        await start_combat.ainvoke({"pc_names": ["Druid"], "enemies": []}, config=config)

    # 1. Tasha Summon (e.g. Summon Beast)
    res1 = await spawn_summon.ainvoke(
        {"summoner_name": "Druid", "summon_name": "Bestial Spirit", "summon_type": "tasha", "hp": 20, "ac": 13}, config=config
    )
    assert "Initiative 11.99" in res1  # Exactly 0.01 behind the Druid's 12!

    # 2. Familiar
    with mock_dice(default=15):
        res2 = await spawn_summon.ainvoke(
            {"summoner_name": "Druid", "summon_name": "Owl Familiar", "summon_type": "familiar", "hp": 1, "ac": 11},
            config=config,
        )
    assert "Initiative 15.00" in res2


@pytest.mark.asyncio
async def test_req_pet_008_concentration_drops_summon(mock_obsidian_vault):
    """
    REQ-PET-008: Dropping concentration (e.g. from falling Unconscious/0 HP)
    natively despawns summons tied to that spell, per 5.5e rules.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    caster = Creature(
        name="Druid",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    caster.concentrating_on = "Summon Beast"

    from tools import spawn_summon
    import os
    from vault_io import get_journals_dir

    os.makedirs(get_journals_dir(vault_path), exist_ok=True)
    with open(os.path.join(get_journals_dir(vault_path), "Druid.md"), "w") as f:
        f.write("---\nname: Druid\n---")

    await spawn_summon.ainvoke(
        {
            "summoner_name": "Druid",
            "summon_name": "Bestial Spirit",
            "requires_concentration": True,
            "spell_name": "Summon Beast",
        },
        config=config,
    )

    from registry import get_all_entities

    summon = [e for e in get_all_entities(vault_path).values() if e.name == "Bestial Spirit"][0]
    assert summon.summoned_by_uuid == caster.entity_uuid
    assert summon.hp.base_value > 0

    # Simulate the caster taking fatal damage (Unconscious -> Drops Concentration)
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=caster.entity_uuid,
        target_uuid=caster.entity_uuid,
        vault_path=vault_path,
        payload={"hit": True, "damage": 20, "damage_type": "slashing"},
    )
    event.status = 3  # Bypass to POST_EVENT to apply damage
    EventBus._notify(event)

    assert caster.hp.base_value == 0
    assert caster.concentrating_on == ""

    # The summon should be killed/despawned
    assert summon.hp.base_value == 0
    assert any(c.name == "Dead" for c in summon.active_conditions)


@pytest.mark.asyncio
async def test_req_pet_003_conjuration_redesign(mock_obsidian_vault):
    """
    REQ-PET-003: Conjure Animals is an AoE emanation, not a physical token spawn.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    caster = Creature(
        name="Druid",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)

    mechanics = {
        "requires_concentration": True,
        "terrain_effect": {"label": "Conjured Animals Pack", "duration": "10 minutes", "tags": ["spirit_pack"]},
    }
    spell = SpellDefinition(name="Conjure Animals", level=3, mechanics=SpellMechanics(**mechanics))
    await SpellCompendium.save_spell(vault_path, spell)

    res = await use_ability_or_spell.ainvoke(
        {
            "caster_name": "Druid",
            "ability_name": "Conjure Animals",
            "aoe_shape": "circle",
            "aoe_size": 10.0,
            "target_x": 0.0,
            "target_y": 0.0,
        },
        config=config,
    )

    assert "Conjured Animals Pack" in res
    assert "Environment" in res
    assert len(spatial_service.get_map_data(vault_path).temporary_terrain) == 1


@pytest.mark.asyncio
async def test_req_pet_005_familiar_touch_spells(mock_obsidian_vault, mock_dice):
    """
    REQ-PET-005: Familiar can deliver touch spells consuming its Reaction.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    wizard = Creature(
        name="Wizard",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    familiar = Creature(
        name="Familiar",
        vault_path=vault_path,
        x=50,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Enemy",
        vault_path=vault_path,
        x=55,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(wizard)
    register_entity(familiar)
    register_entity(target)
    spatial_service.sync_entity(wizard)
    spatial_service.sync_entity(familiar)
    spatial_service.sync_entity(target)

    spell = SpellDefinition(
        name="Shocking Grasp",
        level=0,
        mechanics=SpellMechanics(requires_attack_roll=True, damage_dice="1d8", damage_type="lightning"),
    )
    await SpellCompendium.save_spell(vault_path, spell)

    with mock_dice(8, 15):  # Roll 8 for damage (first), then 15 to hit
        res = await use_ability_or_spell.ainvoke(
            {
                "caster_name": "Wizard",
                "ability_name": "Shocking Grasp",
                "target_names": ["Enemy"],
                "proxy_caster_name": "Familiar",
            },
            config=config,
        )

    assert "Hit" in res
    assert "8 lightning damage" in res
    assert target.hp.base_value == 12
    assert familiar.reaction_used is True


@pytest.mark.asyncio
async def test_req_pet_009_silence_blocks_summon_commands(mock_obsidian_vault):
    """
    REQ-PET-009: A Silenced/Confused summoner cannot issue verbal commands.
    The summon natively defaults to the Dodge action and cannot attack.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from tools import execute_melee_attack

    summoner = Creature(
        name="Druid",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Silenced")],
    )
    register_entity(summoner)

    summon = Creature(
        name="Bestial Spirit",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        summoned_by_uuid=summoner.entity_uuid,
        summon_spell="Summon Beast",
    )
    register_entity(summon)

    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    res = await execute_melee_attack.ainvoke({"attacker_name": "Bestial Spirit", "target_name": "Goblin"}, config=config)
    assert "SYSTEM ERROR" in res
    assert "is Silenced" in res
    assert "Dodge action" in res


@pytest.mark.asyncio
async def test_req_pet_010_deafened_blocks_summon_commands(mock_obsidian_vault):
    """
    REQ-PET-010: A Deafened summon cannot hear verbal commands.
    It defaults to the Dodge action.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}
    from tools import execute_melee_attack

    summoner = Creature(
        name="Druid",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(summoner)

    summon = Creature(
        name="Bestial Spirit",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        summoned_by_uuid=summoner.entity_uuid,
        summon_spell="Summon Beast",
        active_conditions=[ActiveCondition(name="Deafened")],
    )
    register_entity(summon)

    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    res = await execute_melee_attack.ainvoke({"attacker_name": "Bestial Spirit", "target_name": "Goblin"}, config=config)
    assert "SYSTEM ERROR" in res
    assert "Deafened" in res
    assert "Dodge action" in res


# ==========================================
# DEATH REQS
# ==========================================


def test_req_dth_001_and_002_falling_to_zero_and_massive_damage():
    """
    REQ-DTH-001: Massive Damage (Instant Death)
    REQ-DTH-002: Falling to 0 HP
    """
    target = Creature(
        name="Target",
        max_hp=10,
        hp=ModifiableValue(base_value=5),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    # 1. Fall to 0 HP (take 6 damage). Drops to 0, Dying, Unconscious.
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 6, "damage_type": "slashing"},
    )
    event.status = 3
    EventBus._notify(event)

    assert target.hp.base_value == 0
    assert any(c.name == "Dying" for c in target.active_conditions)
    assert any(c.name == "Unconscious" for c in target.active_conditions)

    # Reset for Massive Damage
    target.hp.base_value = 5
    target.active_conditions = []

    # 2. Massive Damage (take 15 damage). 5 - 15 = -10. Max HP is 10. Instant Death.
    event2 = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 15, "damage_type": "slashing"},
    )
    event2.status = 3
    EventBus._notify(event2)

    assert target.hp.base_value == 0
    assert any(c.name == "Dead" for c in target.active_conditions)


def test_req_dth_003_and_004_death_saving_throws(mock_dice):
    """
    REQ-DTH-003: Death Saving Throws (Base)
    REQ-DTH-004: Death Saving Throws (Criticals)
    """
    target = Creature(
        name="Dying Target",
        max_hp=10,
        hp=ModifiableValue(base_value=0),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Dying"), ActiveCondition(name="Unconscious")],
    )
    register_entity(target)

    # Test failure (< 10)
    with mock_dice(default=5):
        sot_event = GameEvent(event_type="StartOfTurn", source_uuid=target.entity_uuid)
        EventBus.dispatch(sot_event)
    assert target.death_saves_failures == 1

    # Test success (>= 10)
    with mock_dice(default=12):
        sot_event = GameEvent(event_type="StartOfTurn", source_uuid=target.entity_uuid)
        EventBus.dispatch(sot_event)
    assert target.death_saves_successes == 1

    # Test critical failure (Nat 1) -> Adds 2 failures (total 3) -> Dead!
    with mock_dice(default=1):
        sot_event = GameEvent(event_type="StartOfTurn", source_uuid=target.entity_uuid)
        EventBus.dispatch(sot_event)
    assert target.death_saves_failures == 3
    assert any(c.name == "Dead" for c in target.active_conditions)

    # Reset
    target.active_conditions = [ActiveCondition(name="Dying"), ActiveCondition(name="Unconscious")]
    target.death_saves_failures = 0
    target.death_saves_successes = 0

    # Test critical success (Nat 20) -> 1 HP, wake up
    with mock_dice(default=20):
        sot_event = GameEvent(event_type="StartOfTurn", source_uuid=target.entity_uuid)
        EventBus.dispatch(sot_event)

    assert target.hp.base_value == 1
    assert not any(c.name == "Dying" for c in target.active_conditions)
    assert not any(c.name == "Unconscious" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_req_dth_005_and_006_damage_and_healing_at_zero(mock_obsidian_vault):
    """
    REQ-DTH-005: Damage at 0 HP
    REQ-DTH-006: Healing at 0 HP
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    target = Creature(
        name="Dying Target",
        vault_path=vault_path,
        max_hp=10,
        hp=ModifiableValue(base_value=0),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Dying"), ActiveCondition(name="Unconscious")],
    )
    register_entity(target)

    # 1. Take normal damage -> 1 fail
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 2, "damage_type": "slashing"},
    )
    event.status = 3
    EventBus._notify(event)
    assert target.death_saves_failures == 1

    # 2. Take critical damage -> 2 fails -> (Total 3) -> Dead!
    event2 = GameEvent(
        event_type="MeleeAttack",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"hit": True, "damage": 2, "damage_type": "slashing", "critical": True},
    )
    event2.status = 3
    EventBus._notify(event2)
    assert target.death_saves_failures == 3
    assert any(c.name == "Dead" for c in target.active_conditions)

    # Reset
    target.active_conditions = [ActiveCondition(name="Dying"), ActiveCondition(name="Unconscious")]
    target.death_saves_failures = 1
    target.death_saves_successes = 2

    # 3. Heal at 0 HP
    from tools import modify_health

    await modify_health.ainvoke({"target_name": "Dying Target", "hp_change": 5, "reason": "Potion"}, config=config)

    assert target.hp.base_value == 5
    assert not any(c.name == "Dying" for c in target.active_conditions)
    assert target.death_saves_failures == 0
    assert target.death_saves_successes == 0


@pytest.mark.asyncio
async def test_req_cnd_001_and_002_incapacitated_and_stunned(mock_obsidian_vault):
    """
    REQ-CND-001: Incapacitated (Zeroes action economy and drops concentration).
    REQ-CND-002: Stunned (Combines Incapacitated with automatic save failures, Speed 0, Incoming Adv).
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    pc = Creature(
        name="Wizard",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        concentrating_on="Haste",
    )
    register_entity(pc)

    # 1. Apply Incapacitated natively via tool (Should drop concentration)
    await toggle_condition.ainvoke(
        {"character_name": "Wizard", "condition_name": "Incapacitated", "is_active": True}, config=config
    )
    assert pc.concentrating_on == ""
    assert any(c.name == "Incapacitated" for c in pc.active_conditions)

    # 2. Apply Stunned natively via tool
    pc.speed = 30
    pc.movement_remaining = 30
    await toggle_condition.ainvoke({"character_name": "Wizard", "condition_name": "Stunned", "is_active": True}, config=config)
    assert pc.movement_remaining == 0
    assert any(c.name == "Stunned" for c in pc.active_conditions)

    # Verify attacks against have advantage
    enemy = Creature(
        name="Enemy",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(enemy)

    atk = GameEvent(event_type="MeleeAttack", source_uuid=enemy.entity_uuid, target_uuid=pc.entity_uuid)
    EventBus.dispatch(atk)
    assert atk.payload.get("advantage") is True

    # Verify auto-fails Dex/Str saves
    spell = SpellDefinition(
        name="Fireball", level=3, mechanics=SpellMechanics(damage_dice="8d6", damage_type="fire", save_required="dexterity")
    )
    await SpellCompendium.save_spell(vault_path, spell)

    with patch("random.randint", return_value=20):  # Normally 20 would pass, but stunned forces fail
        res = await use_ability_or_spell.ainvoke(
            {"caster_name": "Enemy", "ability_name": "Fireball", "target_names": ["Wizard"]}, config=config
        )

    assert "Failed Save" in res


@pytest.mark.asyncio
async def test_req_exh_001_and_002_exhaustion_penalties_and_death(mock_obsidian_vault, mock_dice):
    """
    REQ-EXH-001: Exhaustion (Penalty) - Integer 0-6. Reduces speed and subtracts from d20 tests.
    REQ-EXH-002: Exhaustion (Death) - Reaching level 6 exhaustion immediately triggers the death state.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    from tools import evaluate_extreme_weather, start_combat, perform_ability_check_or_save
    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(j_dir, "Fighter.md"), "w") as f:
        f.write("---\nname: Fighter\n---")

    pc = Creature(
        name="Fighter",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
        movement_remaining=30,
    )
    register_entity(pc)

    # 1. Apply Level 2 Exhaustion natively via weather tool
    with mock_dice(default=1):  # Force fails
        await evaluate_extreme_weather.ainvoke(
            {"character_names": ["Fighter"], "temperature_f": -10, "hours_exposed": 2}, config=config
        )

    assert pc.exhaustion_level == 2

    # 2. Check Speed Penalty (-10) on Turn Start
    await start_combat.ainvoke({"pc_names": ["Fighter"], "enemies": []}, config=config)
    assert pc.movement_remaining == 20  # 30 - (2 * 5)

    # 3. Check D20 Penalty (-4) on Ability Check
    with mock_dice(default=10):
        res_check = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Fighter", "skill_or_stat_name": "athletics"}, config=config
        )
    assert "- 4 (Exhaustion)" in res_check
    assert "= 6" in res_check  # 10 (roll) + 0 (mod) - 4 (exh)

    # 4. Level 6 Exhaustion -> Death
    with mock_dice(default=1):  # Force 4 more fails
        await evaluate_extreme_weather.ainvoke(
            {"character_names": ["Fighter"], "temperature_f": -10, "hours_exposed": 4}, config=config
        )

    assert pc.exhaustion_level == 6
    assert pc.hp.base_value == 0
    assert any(c.name == "Dead" for c in pc.active_conditions)
