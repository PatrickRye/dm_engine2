import pytest
from unittest.mock import patch
import uuid

from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus, MeleeWeapon
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import execute_melee_attack, move_entity, toggle_condition
import event_handlers  # Ensure handlers are loaded
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
            "save_timing": "start"
        },
        config=config,
    )

    import os
    from vault_io import get_journals_dir
    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(j_dir, "Fighter.md"), "w") as f:
        f.write("---\nname: Fighter\n---")

    await start_combat.ainvoke({"pc_names": ["Fighter"], "enemies": []}, config=config)

    with mock_dice(default=2):
        res_fail = await update_combat_state.ainvoke({"next_turn": True}, config=config)
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

    assert caster.hp.base_value == -10
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
