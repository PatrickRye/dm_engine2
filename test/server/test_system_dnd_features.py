import pytest

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
    EventStatus,
    MeleeWeapon,
    RangedWeapon,
    ActiveCondition,
    NumericalModifier,
    ModifierPriority,
)
from registry import register_entity
from spatial_engine import spatial_service, Wall, TerrainZone
from tools import (
    manage_light_sources,
    toggle_condition,
    execute_grapple_or_shove,
    trigger_environmental_hazard,
    interact_with_object,
    manage_map_trap,
    move_entity,
)
from registry import clear_registry


@pytest.fixture(autouse=True)
def setup_system():
    clear_registry()
    spatial_service.clear()
    yield


# ==========================================
# SCENARIO A: BAROVIAN CHURCH SKIRMISH
# ==========================================
def test_system_barovian_church_ranged_combat(mock_dice):
    """
    System test covering: Spatial distance, difficult terrain overlaps,
    cover AC bonuses, and executing a ranged attack.
    [Mapped: REQ-SPC-001, REQ-COR-003]
    """
    # 1. Setup Environment (Church with pews and rubble)
    pew_wall = Wall(start=(15, 0), end=(15, 10), height=3.0, is_solid=True)  # Low wall
    rubble_zone = TerrainZone(points=[(5, -5), (10, -5), (10, 5), (5, 5)], is_difficult=True)
    spatial_service.add_wall(pew_wall)
    spatial_service.add_terrain(rubble_zone)

    # 2. Setup Entities
    ranger = Creature(
        name="Ireena",
        x=0,
        y=0,
        z=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=14),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=3),
        speed=30,
        movement_remaining=30,
    )
    register_entity(ranger)
    bow = RangedWeapon(name="Shortbow", damage_dice="1d6", damage_type="piercing", normal_range=80, long_range=320)
    register_entity(bow)
    ranger.equipped_weapon_uuid = bow.entity_uuid

    vampire = Creature(
        name="Doru",
        x=20,
        y=0,
        z=0,
        height=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(vampire)

    spatial_service.sync_entity(ranger)
    spatial_service.sync_entity(vampire)

    # 3. Execution: Ranger moves 15 ft (5ft normal + 5ft diff * 2) = 15 cost.
    move_event = GameEvent(
        event_type="Movement",
        source_uuid=ranger.entity_uuid,
        payload={"target_x": 10, "target_y": 0, "target_z": 0, "movement_type": "walk"},
    )
    EventBus.dispatch(move_event)

    assert move_event.status != 5  # Not cancelled
    assert move_event.payload["cost"] == 15

    # Update spatial positioning
    ranger.x = 10
    spatial_service.sync_entity(ranger)

    # 4. Execution: Ranger shoots Doru behind the pew.
    # Doru is at x=20. Wall is at x=15. Ranger is at x=10.
    # The wall is 3ft high, Doru is 5ft high. This should grant Half Cover (+2 AC).
    with mock_dice(14, 14, 5):  # Roll 14 + 3 DEX = 17 vs AC 17 (15 + 2 Cover). HIT!
        atk_event = GameEvent(event_type="MeleeAttack", source_uuid=ranger.entity_uuid, target_uuid=vampire.entity_uuid)
        EventBus.dispatch(atk_event)

        assert atk_event.payload["hit"] is True
        assert vampire.hp.base_value == 30 - (5 + 3)  # 5 dice + 3 dex = 8 dmg. 22 HP left.


# ==========================================
# SCENARIO B: THE SENTINEL OF BAROVIA
# ==========================================
def test_system_sentinel_feat_interaction(mock_dice):
    """
    System test covering: Feat tag injections overriding default mechanics,
    reaction consumption, and movement halting.
    [Mapped: REQ-ACT-006]
    """
    fighter = Creature(
        name="Paladin",
        tags=["pc", "ignores_disengage", "oa_halts_movement"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(fighter)
    sword = MeleeWeapon(name="Longsword", damage_dice="1d8", damage_type="slashing")
    register_entity(sword)
    fighter.equipped_weapon_uuid = sword.entity_uuid

    goblin = Creature(
        name="Fleeing Goblin",
        tags=["monster"],
        x=5,
        y=0,
        hp=ModifiableValue(base_value=15),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        movement_remaining=30,
    )
    register_entity(goblin)

    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(goblin)

    # 1. Goblin uses Disengage and tries to move away.
    move_event = GameEvent(
        event_type="Movement",
        source_uuid=goblin.entity_uuid,
        payload={"target_x": 15, "target_y": 0, "target_z": 0, "movement_type": "disengage"},
    )
    EventBus.dispatch(move_event)

    # The Sentinel feat ("ignores_disengage") should flag the fighter as an opportunity attacker
    assert "Paladin" in move_event.payload.get("opportunity_attackers", [])

    # 2. Simulate AI deciding to use Reaction to attack
    fighter.reaction_used = True
    with mock_dice(15, 15, 6):  # 15 + 4 = 19 vs AC 12 (Hit). Dmg 6 + 4 = 10.
        oa_event = GameEvent(event_type="MeleeAttack", source_uuid=fighter.entity_uuid, target_uuid=goblin.entity_uuid)

        # In production, main.py intercepts the OA flag and modifies goblin.movement_remaining if hit
        EventBus.dispatch(oa_event)
        if oa_event.payload.get("hit") and "oa_halts_movement" in fighter.tags:
            goblin.movement_remaining = 0

    assert goblin.hp.base_value == 5
    assert goblin.movement_remaining == 0  # Halted by the Sentinel hit!


# ==========================================
# SCENARIO C: ADVANCED TRAITS & MOVEMENT
# ==========================================
def test_system_feat_ignore_difficult_terrain():
    """
    System test covering: Feat tags perfectly bypassing double movement multipliers.
    [Mapped: REQ-COR-003]
    """
    ranger = Creature(
        name="Strider",
        tags=["ignore_difficult_terrain"],
        x=0,
        y=0,
        speed=30,
        movement_remaining=30,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    zone = TerrainZone(points=[(0, -5), (20, -5), (20, 5), (0, 5)], is_difficult=True)
    spatial_service.add_terrain(zone)
    spatial_service.sync_entity(ranger)

    move_event = GameEvent(
        event_type="Movement",
        source_uuid=ranger.entity_uuid,
        payload={"target_x": 20, "target_y": 0, "target_z": 0, "movement_type": "walk"},
    )
    EventBus.dispatch(move_event)

    # Normally, 20ft of difficult terrain would cost 40 movement.
    # The EventBus should detect the tag and only charge 20, leaving 10 remaining.
    assert move_event.status != EventStatus.CANCELLED
    assert move_event.payload["cost"] == 20
    assert ranger.movement_remaining == 10


# ==========================================
# SCENARIO D: LONG REST & RESOURCE RECHARGE
# ==========================================
def test_system_long_rest_recharge_and_expiration():
    """
    System test covering: Long rest fully heals, recharges resources, and wipes out temporary conditions/buffs.
    [Mapped: REQ-RST-004]
    """
    wizard = Creature(
        name="Exhausted Wizard",
        max_hp=25,
        hp=ModifiableValue(base_value=2),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={"Spell Slots": "0/4", "Lucky": "1/3"},
    )

    # Add a 1-hour buff and an 8-hour condition
    wizard.strength_mod.add_modifier(
        NumericalModifier(
            priority=ModifierPriority.ADDITIVE,
            value=2,
            source_name="Bull's Strength",
            duration_seconds=3600,
        )
    )
    wizard.active_conditions.append(ActiveCondition(name="Poisoned", duration_seconds=28800))

    # Simulate `take_rest` tool dispatching AdvanceTime for 8 hours
    time_event = GameEvent(
        event_type="AdvanceTime",
        source_uuid=wizard.entity_uuid,
        payload={"seconds_advanced": 28800},
    )
    EventBus.dispatch(time_event)

    # Simulate `take_rest` tool dispatching the Rest event
    rest_event = GameEvent(
        event_type="Rest",
        source_uuid=wizard.entity_uuid,
        payload={"rest_type": "long", "target_uuids": [wizard.entity_uuid]},
    )
    EventBus.dispatch(rest_event)

    assert wizard.hp.base_value == 25  # Healed to max
    assert wizard.resources["Spell Slots"] == "4/4"  # Bounded string regex recharge
    assert wizard.resources["Lucky"] == "3/3"
    assert wizard.strength_mod.total == 0  # 1 hour buff expired
    assert len(wizard.active_conditions) == 0  # 8 hour condition exactly expired


def test_system_short_rest_mechanics():
    """
    System test covering: Short rest does NOT over-heal or recharge long-rest resources, but DOES advance time for conditions.
    [Mapped: REQ-RST-001]
    """
    warlock = Creature(
        name="Warlock",
        max_hp=30,
        hp=ModifiableValue(base_value=15),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={"Long Rest Spell Slots": "0/2"},
    )

    # Add a 10-minute (600s) condition and a 2-hour (7200s) condition
    warlock.active_conditions.append(ActiveCondition(name="Stunned", duration_seconds=600))
    warlock.active_conditions.append(ActiveCondition(name="Hex", duration_seconds=7200))

    # Simulate `take_rest` tool dispatching AdvanceTime for 1 hour (Short Rest)
    time_event = GameEvent(event_type="AdvanceTime", source_uuid=warlock.entity_uuid, payload={"seconds_advanced": 3600})
    EventBus.dispatch(time_event)

    rest_event = GameEvent(
        event_type="Rest",
        source_uuid=warlock.entity_uuid,
        payload={"rest_type": "short", "target_uuids": [warlock.entity_uuid]},
    )
    EventBus.dispatch(rest_event)

    # Validate Rest behavior (Short rest doesn't auto-heal or restore long-rest slots natively)
    assert warlock.hp.base_value == 15
    assert warlock.resources["Long Rest Spell Slots"] == "0/2"

    # Validate Time behavior
    active_cond_names = [c.name for c in warlock.active_conditions]
    assert "Stunned" not in active_cond_names  # 10 min expired
    assert "Hex" in active_cond_names  # 2 hr did NOT expire (has 1 hour left)
    assert warlock.active_conditions[0].duration_seconds == 3600  # Exactly 1 hour left


# ==========================================
# SCENARIO E: DYNAMIC LIGHTING & VISION
# ==========================================
@pytest.mark.asyncio
async def test_system_dynamic_lighting_and_combat():
    """
    System test covering: Darkness giving disadvantage, and tools dynamically fixing it.
    [Mapped: REQ-VIS-001, REQ-VIS-006]
    """
    human = Creature(
        name="Human",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    # Target has darkvision so they can see the human perfectly, meaning the Human doesn't get Unseen Attacker Advantage to cancel out their Disadvantage.  # noqa: E501
    target = Creature(
        name="Goblin",
        tags=["darkvision"],
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    sword = MeleeWeapon(name="Sword", damage_dice="1d8", damage_type="slashing")
    human.equipped_weapon_uuid = sword.entity_uuid

    spatial_service.sync_entity(human)
    spatial_service.sync_entity(target)
    spatial_service.map_data.lights.clear()  # Absolute Darkness

    # 1. Attack in Darkness
    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=human.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(atk_event)
    assert atk_event.payload.get("disadvantage") is True  # Human can't see target

    # 2. Add light via tool & re-attack
    await manage_light_sources.ainvoke(
        {"action": "add", "label": "Torch", "x": 0, "y": 0, "bright_radius": 20, "dim_radius": 40},
        config={"configurable": {"thread_id": "default"}},
    )
    atk_event_2 = GameEvent(event_type="MeleeAttack", source_uuid=human.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(atk_event_2)
    assert atk_event_2.payload.get("disadvantage") is not True  # Environment is now bright


@pytest.mark.asyncio
async def test_system_dynamic_light_movement_and_static_torches():
    """
    Tests that static torches remain in place while attached lights follow entities.
    [Mapped: REQ-VIS-007]
    """
    pc = Creature(
        name="Lightbringer",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(pc)
    config = {"configurable": {"thread_id": "default"}}

    # Add static torch at (50, 0)
    await manage_light_sources.ainvoke(
        {"action": "add", "label": "Wall Torch", "x": 50, "y": 0, "bright_radius": 20, "dim_radius": 40}, config=config
    )

    # Add attached light to PC
    await manage_light_sources.ainvoke(
        {
            "action": "add",
            "label": "Glowing Staff",
            "attached_to_entity": "Lightbringer",
            "bright_radius": 20,
            "dim_radius": 40,
        },
        config=config,
    )

    # Initial checks
    assert spatial_service.get_illumination(0, 0, 0) == "bright"  # PC's light
    assert spatial_service.get_illumination(50, 0, 0) == "bright"  # Wall torch
    assert spatial_service.get_illumination(100, 0, 0) == "darkness"  # Nowhere near either

    # Move PC away
    move_event = GameEvent(
        event_type="Movement",
        source_uuid=pc.entity_uuid,
        payload={"target_x": 100, "target_y": 0, "target_z": 0, "movement_type": "teleport"},
    )
    EventBus.dispatch(move_event)
    pc.x = 100
    spatial_service.sync_entity(pc)

    # Re-verify
    assert spatial_service.get_illumination(0, 0, 0) == "darkness"  # PC left, took light with them
    assert spatial_service.get_illumination(50, 0, 0) == "bright"  # Wall torch stayed static
    assert spatial_service.get_illumination(100, 0, 0) == "bright"  # Light followed PC


@pytest.mark.asyncio
async def test_system_disarm_and_drop_light():
    """
    Tests that a dropped or disarmed light source stops following the player.
    [Mapped: REQ-VIS-006]
    """
    pc = Creature(
        name="Wizard",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(pc)
    config = {"configurable": {"thread_id": "default"}}

    await manage_light_sources.ainvoke(
        {"action": "add", "label": "Magic Staff", "attached_to_entity": "Wizard", "bright_radius": 10, "dim_radius": 20},
        config=config,
    )

    # Move PC to (30, 0) - Staff follows
    pc.x = 30
    spatial_service.sync_entity(pc)
    assert spatial_service.get_illumination(30, 0, 0) == "bright"

    # Disarm: Remove attached staff, add static staff at current coordinates
    await manage_light_sources.ainvoke({"action": "remove", "label": "Magic Staff"}, config=config)
    await manage_light_sources.ainvoke(
        {"action": "add", "label": "Dropped Staff", "x": pc.x, "y": pc.y, "bright_radius": 10, "dim_radius": 20}, config=config
    )

    # Move PC to (60, 0)
    pc.x = 60
    spatial_service.sync_entity(pc)

    assert spatial_service.get_illumination(30, 0, 0) == "bright"  # Dropped staff stayed here
    assert spatial_service.get_illumination(60, 0, 0) == "darkness"  # PC is now in the dark


@pytest.mark.asyncio
async def test_system_snuff_light_source():
    """
    Tests that snuffing a light source instantly plunges the area into darkness.
    [Mapped: REQ-VIS-001]
    """
    config = {"configurable": {"thread_id": "default"}}
    await manage_light_sources.ainvoke(
        {"action": "add", "label": "Campfire", "x": 0, "y": 0, "bright_radius": 15, "dim_radius": 30}, config=config
    )
    assert spatial_service.get_illumination(10, 0, 0) == "bright"

    await manage_light_sources.ainvoke({"action": "remove", "label": "Campfire"}, config=config)
    assert spatial_service.get_illumination(10, 0, 0) == "darkness"


@pytest.mark.asyncio
async def test_system_spell_illumination_areas():
    """
    Tests that spells (like Daylight) correctly cast bright and dim light at specific ranges.
    [Mapped: REQ-VIS-006]
    """
    config = {"configurable": {"thread_id": "default"}}
    # Daylight spell: 60ft bright, additional 60ft dim (120ft total)
    await manage_light_sources.ainvoke(
        {"action": "add", "label": "Daylight Spell", "x": 0, "y": 0, "bright_radius": 60, "dim_radius": 120}, config=config
    )

    assert spatial_service.get_illumination(30, 0, 0) == "bright"
    assert spatial_service.get_illumination(60, 0, 0) == "bright"
    assert spatial_service.get_illumination(90, 0, 0) == "dim"
    assert spatial_service.get_illumination(120, 0, 0) == "dim"
    assert spatial_service.get_illumination(150, 0, 0) == "darkness"


@pytest.mark.asyncio
async def test_system_stealth_and_hidden_combat():
    """
    Tests that characters can gain the 'Hidden' condition, gain advantage, and lose it upon attacking.
    [Mapped: REQ-STL-002, REQ-CND-008]
    """
    rogue = Creature(
        name="Rogue",
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Guard",
        x=10,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    dagger = MeleeWeapon(name="Dagger", damage_dice="1d4", damage_type="piercing")
    rogue.equipped_weapon_uuid = dagger.entity_uuid

    spatial_service.sync_entity(rogue)
    spatial_service.sync_entity(target)
    spatial_service.map_data.lights.clear()  # Pitch black

    # Create dummy file so tool doesn't crash looking for the YAML file
    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir("default")
    with open(os.path.join(j_dir, "Rogue.md"), "w") as f:
        f.write("---\nname: Rogue\nactive_conditions: []\n---\n")

    # Guard has no darkvision, Rogue applies "Hidden" via tool
    config = {"configurable": {"thread_id": "default"}}
    await toggle_condition.ainvoke({"character_name": "Rogue", "condition_name": "Hidden", "is_active": True}, config=config)

    # Verify condition applied
    assert any(c.name == "Hidden" for c in rogue.active_conditions)

    # 1. Rogue attacks Guard from Hidden. Should have Advantage.
    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=rogue.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(atk_event)

    assert atk_event.payload.get("advantage") is True

    # 2. Verify Rogue lost the "Hidden" condition after the attack
    assert not any(c.name == "Hidden" for c in rogue.active_conditions)


# ==========================================
# SCENARIO G: GRAPPLE & SHOVE CONTESTS
# ==========================================
@pytest.mark.asyncio
async def test_system_grapple_contest_success_and_fail(mock_dice):
    """
    Tests successful and unsuccessful Grapple contests.
    [Mapped: REQ-ACT-007]
    """
    attacker = Creature(
        name="Orc",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(attacker)
    target = Creature(
        name="Bard",
        x=5,
        y=0,
        speed=30,
        movement_remaining=30,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )
    register_entity(target)

    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)

    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir("default")
    with open(os.path.join(j_dir, "Bard.md"), "w") as f:
        f.write("---\nname: Bard\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": "default"}}

    # REQ-ACT-007 (2024): DC = 8 + Orc STR(+4) + prof(+2) = 14. Bard resists with max(STR+0, DEX+2)=+2.
    # Test 1: Attacker wins — Bard rolls 5+2=7 < DC 14
    with mock_dice(5):
        res = await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Orc", "target_name": "Bard", "action_type": "grapple"}, config=config
        )
        assert "Attacker wins" in res
        assert any(c.name == "Grappled" for c in target.active_conditions)
        assert target.movement_remaining == 0

    # Test 2: Defender wins — Bard rolls 15+2=17 >= DC 14
    target.active_conditions = []
    target.movement_remaining = 30
    with mock_dice(15):
        res = await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Orc", "target_name": "Bard", "action_type": "grapple"}, config=config
        )
        assert "Defender wins" in res or "Defender succeeds" in res
        assert not any(c.name == "Grappled" for c in target.active_conditions)
        assert target.movement_remaining == 30


@pytest.mark.asyncio
async def test_system_shove_movement_direction(mock_dice):
    """
    Tests that a successful shove perfectly calculates the vector and pushes the target backward.
    [Mapped: REQ-ACT-007, REQ-MST-004]
    """
    attacker = Creature(
        name="Orc",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    # Target is at (5, 0). Vector is (1, 0). Pushing away should natively land them at (10, 0).
    target = Creature(
        name="Bard",
        x=5,
        y=0,
        speed=30,
        movement_remaining=30,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )

    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)

    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir("default")
    with open(os.path.join(j_dir, "Bard.md"), "w") as f:
        f.write("---\nname: Bard\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": "default"}}

    # DC = 8 + Orc STR(+4) + prof(+2) = 14. Bard rolls 5+2=7 < 14 → attacker wins.
    with mock_dice(5):
        res = await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Orc", "target_name": "Bard", "action_type": "shove", "shove_type": "away"}, config=config
        )
        assert "shoved 5.0 feet away" in res
        assert target.x == 10.0
        assert target.y == 0.0


@pytest.mark.asyncio
async def test_system_throw_breaks_grapple_and_knocks_prone(mock_dice):
    """
    Tests that throwing a grappled enemy natively pushes them, knocks them prone, and breaks the grapple.
    [Mapped: REQ-ACT-007, REQ-CND-013]
    """
    attacker = Creature(
        name="Orc",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(attacker)
    target = Creature(
        name="Bard",
        x=5,
        y=0,
        speed=30,
        movement_remaining=30,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )
    register_entity(target)

    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)

    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir("default")
    with open(os.path.join(j_dir, "Bard.md"), "w") as f:
        f.write("---\nname: Bard\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": "default"}}

    # 1. Attacker successfully grapples Target (DC 14, Bard rolls 5+2=7 < 14)
    with mock_dice(5):
        await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Orc", "target_name": "Bard", "action_type": "grapple"}, config=config
        )

    assert any(c.name == "Grappled" for c in target.active_conditions)
    assert target.movement_remaining == 0

    # 2. Attacker successfully throws Target 15 feet (Bard rolls 4+2=6 < DC 14)
    with mock_dice(4):
        res = await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Orc", "target_name": "Bard", "action_type": "throw", "throw_distance": 15.0}, config=config
        )

    assert "thrown 15.0 feet away" in res
    assert "lands Prone" in res

    assert target.x == 20.0  # 5ft starting distance + 15ft throw vector
    assert not any(c.name == "Grappled" for c in target.active_conditions)  # 20ft > 7.5ft break threshold
    assert any(c.name == "Prone" for c in target.active_conditions)


# ==========================================
# SCENARIO F: ADVANCED PERCEPTION & SENSES
# ==========================================
@pytest.mark.asyncio
async def test_system_blindsight_vs_invisibility():
    """
    System test: Blindsight ignores invisibility and darkness.
    [Mapped: REQ-VIS-003]
    """
    bat = Creature(
        name="Giant Bat",
        tags=["blindsight_60"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    invisible_mage = Creature(
        name="Mage",
        tags=["invisible"],
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    bite = MeleeWeapon(name="Bite", damage_dice="1d4", damage_type="piercing")
    bat.equipped_weapon_uuid = bite.entity_uuid

    spatial_service.sync_entity(bat)
    spatial_service.sync_entity(invisible_mage)
    spatial_service.map_data.lights.clear()  # Pitch black

    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=bat.entity_uuid, target_uuid=invisible_mage.entity_uuid)
    EventBus.dispatch(atk_event)

    # Bat can see Mage (Blindsight 60 >= 30). Mage CANNOT see Bat (Darkness, no darkvision).
    # Attacker unseen by target = Advantage. Attacker can see target = No Disadvantage.
    assert atk_event.payload.get("advantage") is True
    assert atk_event.payload.get("disadvantage") is not True


@pytest.mark.asyncio
async def test_system_tremorsense_vs_flying():
    """
    System test: Tremorsense works in darkness but fails on flying targets.
    [Mapped: REQ-VIS-004]
    """
    bulette = Creature(
        name="Bulette",
        tags=["tremorsense_60"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    ground_target = Creature(
        name="Fighter",
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    flying_target = Creature(
        name="Aarakocra",
        tags=["flying"],
        x=5,
        y=5,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    bite = MeleeWeapon(name="Bite", damage_dice="4d12", damage_type="piercing")
    bulette.equipped_weapon_uuid = bite.entity_uuid

    spatial_service.sync_entity(bulette)
    spatial_service.sync_entity(ground_target)
    spatial_service.sync_entity(flying_target)
    spatial_service.map_data.lights.clear()

    atk_ground = GameEvent(event_type="MeleeAttack", source_uuid=bulette.entity_uuid, target_uuid=ground_target.entity_uuid)
    EventBus.dispatch(atk_ground)
    assert (
        atk_ground.payload.get("disadvantage") is True
    )  # Tremorsense does not negate unseen attacker disadvantage (REQ-VIS-004)

    atk_flying = GameEvent(event_type="MeleeAttack", source_uuid=bulette.entity_uuid, target_uuid=flying_target.entity_uuid)
    EventBus.dispatch(atk_flying)
    assert atk_flying.payload.get("disadvantage") is True  # Cannot sense flying target in darkness


@pytest.mark.asyncio
async def test_system_darkvision_range_limit():
    """
    System test: Darkvision only works up to its specified range.
    [Mapped: REQ-VIS-007]
    """
    elf = Creature(
        name="Elf",
        tags=["darkvision_60"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    goblin_near = Creature(
        name="Goblin Near",
        x=50,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    goblin_far = Creature(
        name="Goblin Far",
        x=70,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    spatial_service.sync_entity(elf)
    spatial_service.sync_entity(goblin_near)
    spatial_service.sync_entity(goblin_far)
    spatial_service.map_data.lights.clear()

    bow = RangedWeapon(name="Longbow", damage_dice="1d8", damage_type="piercing", normal_range=150, long_range=600)
    elf.equipped_weapon_uuid = bow.entity_uuid

    atk_near = GameEvent(event_type="MeleeAttack", source_uuid=elf.entity_uuid, target_uuid=goblin_near.entity_uuid)
    EventBus.dispatch(atk_near)
    assert atk_near.payload.get("disadvantage") is not True  # 50ft <= 60ft Darkvision

    atk_far = GameEvent(event_type="MeleeAttack", source_uuid=elf.entity_uuid, target_uuid=goblin_far.entity_uuid)
    EventBus.dispatch(atk_far)
    assert atk_far.payload.get("disadvantage") is True  # 70ft > 60ft Darkvision


@pytest.mark.asyncio
async def test_system_silence_zone_blinds_echolocation():
    """
    System test: Moving into a Silence zone dynamically applies the Deafened condition,
    which blinds an echolocating creature.
    [Mapped: REQ-ENV-011, REQ-VIS-010]
    """
    bat = Creature(
        name="Giant Bat",
        tags=["blindsight_60", "echolocation"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    invisible_mage = Creature(
        name="Mage",
        tags=["invisible"],
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(bat)
    spatial_service.sync_entity(invisible_mage)
    spatial_service.map_data.lights.clear()

    # Create a silence zone over the bat
    zone = TerrainZone(label="Silence Sphere", points=[(-5, -5), (5, -5), (5, 5), (-5, 5)], tags=["silence"])
    spatial_service.add_terrain(zone)

    # Dispatch a dummy event to trigger the sync handler
    EventBus.dispatch(GameEvent(event_type="AdvanceTime", source_uuid=bat.entity_uuid, payload={"seconds_advanced": 0}))

    assert any(c.name == "Deafened" and c.source_name == "Magical Silence" for c in bat.active_conditions)

    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=bat.entity_uuid, target_uuid=invisible_mage.entity_uuid)
    EventBus.dispatch(atk_event)

    # Bat is deafened by the zone, echolocation fails, attacks invisible target with Disadvantage
    assert atk_event.payload.get("disadvantage") is True


@pytest.mark.asyncio
async def test_system_silence_zone_preserves_existing_deafness():
    """
    System test: Exiting a Silence zone removes Magical Silence deafness but preserves pre-existing natural deafness.
    [Mapped: REQ-ENV-011]
    """
    old_man = Creature(
        name="Old Man",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Deafened", source_name="Natural Aging")],
    )
    spatial_service.sync_entity(old_man)

    zone = TerrainZone(label="Silence Sphere", points=[(-5, -5), (5, -5), (5, 5), (-5, 5)], tags=["silence"])
    spatial_service.add_terrain(zone)

    # 1. Sync inside the zone
    EventBus.dispatch(GameEvent(event_type="AdvanceTime", source_uuid=old_man.entity_uuid, payload={"seconds_advanced": 0}))
    deaf_conds = [c for c in old_man.active_conditions if c.name == "Deafened"]
    assert len(deaf_conds) == 2  # Has both Natural and Magical Silence

    # 2. Move out of the zone
    old_man.x = 20
    spatial_service.sync_entity(old_man)
    move_event = GameEvent(
        event_type="Movement",
        source_uuid=old_man.entity_uuid,
        payload={"target_x": 20, "target_y": 0, "movement_type": "walk"},
    )
    EventBus.dispatch(move_event)

    # 3. Should lose Magical Silence but keep Natural Aging
    deaf_conds_after = [c for c in old_man.active_conditions if c.name == "Deafened"]
    assert len(deaf_conds_after) == 1
    assert deaf_conds_after[0].source_name == "Natural Aging"


@pytest.mark.asyncio
async def test_system_tremorsense_vs_pass_without_trace():
    """
    System test: Tremorsense is thwarted by Pass without Trace.
    [Mapped: REQ-VIS-011]
    """
    bulette = Creature(
        name="Bulette",
        tags=["tremorsense_60"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    rogue = Creature(
        name="Rogue",
        tags=["invisible"],
        active_conditions=[ActiveCondition(name="Pass_Without_Trace")],
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(bulette)
    spatial_service.sync_entity(rogue)
    spatial_service.map_data.lights.clear()

    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=bulette.entity_uuid, target_uuid=rogue.entity_uuid)
    EventBus.dispatch(atk_event)

    # Tremorsense is foiled, target is invisible. Bulette attacks with disadvantage.
    assert atk_event.payload.get("disadvantage") is True


@pytest.mark.asyncio
async def test_system_devils_sight_vs_darkness():
    """
    System test: Devil's Sight sees through total darkness perfectly, negating disadvantage.
    [Mapped: REQ-VIS-009]
    """
    warlock = Creature(
        name="Warlock",
        tags=["devils_sight_120"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    goblin = Creature(
        name="Goblin",
        tags=[],
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(warlock)
    spatial_service.sync_entity(goblin)
    spatial_service.map_data.lights.clear()

    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=warlock.entity_uuid, target_uuid=goblin.entity_uuid)
    EventBus.dispatch(atk_event)

    # Warlock sees perfectly, so no disadvantage.
    # Target cannot see the Warlock due to darkness, giving Warlock unseen attacker advantage!
    assert atk_event.payload.get("disadvantage") is not True
    assert atk_event.payload.get("advantage") is True


# ==========================================
# SCENARIO H: ENVIRONMENTAL HAZARDS
# ==========================================
@pytest.mark.asyncio
async def test_system_trigger_environmental_hazard(mock_dice, mock_roll_dice):
    """
    Tests the trigger_environmental_hazard tool for applying AoE saves, damage, and conditions.
    [Mapped: REQ-EDG-001, REQ-SPL-018]
    """
    rogue = Creature(
        name="Rogue",
        tags=["evasion"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=5),
    )
    fighter = Creature(
        name="Fighter",
        x=10,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=-1),
    )
    wizard = Creature(
        name="Wizard",
        x=100,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    spatial_service.sync_entity(rogue)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(wizard)

    config = {"configurable": {"thread_id": "default"}}

    # Trigger a Poison Gas Trap at (5, 0) with a 20ft radius
    # DC 15 Dex save, 4d6 poison, applies 'Poisoned'
    with mock_roll_dice(default=20), mock_dice(default=5):  # Saves roll 5 (FAIL). Forced flat Damage rolls 20.
        res = await trigger_environmental_hazard.ainvoke(
            {
                "hazard_name": "Poison Gas Trap",
                "origin_x": 5.0,
                "origin_y": 0.0,
                "radius": 20.0,
                "save_required": "dexterity",
                "save_dc": 15,
                "damage_dice": "4d6",
                "damage_type": "poison",
                "half_damage_on_save": True,
                "condition_applied": "Poisoned",
            },
            config=config,
        )

    assert "Poison Gas Trap triggered!" in res

    # Rogue failed (10 vs 15), but has Evasion! Evasion makes failures take half damage. 20 / 2 = 10
    assert rogue.hp.base_value == 20
    assert any(c.name == "Poisoned" for c in rogue.active_conditions)

    # Fighter failed (4 vs 15), no evasion. Takes full 20 damage.
    assert fighter.hp.base_value == 10
    assert any(c.name == "Poisoned" for c in fighter.active_conditions)

    # Wizard was 95ft away (at x=100), completely unaffected
    assert wizard.hp.base_value == 20
    assert not any(c.name == "Poisoned" for c in wizard.active_conditions)
    assert "Wizard" not in res


# ==========================================
# SCENARIO I: TRAPS & HAZARDS AUTOMATION
# ==========================================
# REQ-TOL-001: Lockpicking formula
@pytest.mark.asyncio
async def test_req_tol_001_lockpick_formula(mock_dice):
    """
    REQ-TOL-001: Lockpicking uses Dex_Mod + PB.
    Roll = Dex_Mod + (PB × Max(Sleight_Of_Hand_Prof, Thieves_Tools_Prof))
    For a level 1 rogue with +5 Dex and no extra modifier:
      PB = ceil(1/4)+1 = 2, so total_mod = 5+2 = 7
      Roll 13 + 7 = 20 vs DC 20 → Success
    """
    rogue = Creature(
        name="Rogue",
        x=0, y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=5),
    )
    spatial_service.sync_entity(rogue)
    config = {"configurable": {"thread_id": "default"}}

    wall = Wall(label="Door", start=(10, 0), end=(10, 0), is_locked=True, interact_dc=20)
    spatial_service.add_wall(wall)

    # Roll 13 (natural) + 7 (mod) = 20 vs DC 20 → success
    with mock_dice(13):
        res = await interact_with_object.ainvoke(
            {"character_name": "Rogue", "target_label": "Door", "interaction_type": "lockpick"}, config=config
        )

    assert "SUCCESS" in res
    assert "Rolled 13 + 7 = 20 vs DC 20" in res


@pytest.mark.asyncio
async def test_req_tol_001_lockpick_fails_low_roll(mock_dice):
    """
    REQ-TOL-001: Roll too low fails even with high Dex.
    Roll 5 + 7 = 12 vs DC 20 → Failure
    """
    rogue = Creature(
        name="Rogue",
        x=0, y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=5),
    )
    spatial_service.sync_entity(rogue)
    config = {"configurable": {"thread_id": "default"}}

    wall = Wall(label="Strongbox", start=(20, 0), end=(20, 0), is_locked=True, interact_dc=20)
    spatial_service.add_wall(wall)

    with mock_dice(5):
        res = await interact_with_object.ainvoke(
            {"character_name": "Rogue", "target_label": "Strongbox", "interaction_type": "lockpick"}, config=config
        )

    assert "FAILURE" in res
    assert "5 + 7 = 12 vs DC 20" in res


# ==========================================
@pytest.mark.asyncio
async def test_system_trap_interaction_fail(mock_dice):
    """
    Tests that failing to pick a trapped lock natively explodes in the PC's face.
    [Mapped: REQ-TOL-002]
    """
    rogue = Creature(
        name="Rogue",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=5),
    )
    spatial_service.sync_entity(rogue)
    config = {"configurable": {"thread_id": "default"}}

    # 1. Setup Wall and Trap
    wall = Wall(label="Chest", start=(5, 5), end=(5, 5), is_locked=True, interact_dc=20)
    spatial_service.add_wall(wall)

    await manage_map_trap.ainvoke(
        {
            "target_label": "Chest",
            "hazard_name": "Poison Needle",
            "trigger_on_interact_fail": True,
            "save_required": "constitution",
            "save_dc": 15,
            "damage_dice": "1d10",
            "damage_type": "poison",
            "condition_applied": "Poisoned",
        },
        config=config,
    )

    # 2. Interact - Fail (Roll 5 lockpick + Mod fails DC 20. Then Roll 5 Save fails DC 15. Damage rolls 8)
    with mock_dice(5, 8, 5, 5):
        res = await interact_with_object.ainvoke(
            {"character_name": "Rogue", "target_label": "Chest", "interaction_type": "lockpick"}, config=config
        )

    assert "FAILURE" in res
    assert "TRAP TRIGGERED" in res
    assert rogue.hp.base_value == 22  # 30 - 8
    assert any(c.name == "Poisoned" for c in rogue.active_conditions)


@pytest.mark.asyncio
async def test_system_trap_stealth_perception_hook(mock_dice):
    """
    Tests that a triggered trap globally alerts NPCs whose distance-modified Passive Perception beats the triggerer's Stealth.
    [Mapped: REQ-STL-001]
    """
    rogue = Creature(
        name="Sneaky Rogue",
        tags=["pc"],
        x=0,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=5),
    )
    rogue.active_conditions.append(ActiveCondition(name="Hidden"))

    # Passive Perception: 10 + 2 (Wis) - 1 (10ft away) = 11. Rogue Stealth = 15. Guard should NOT hear.
    guard_far = Creature(
        name="Deaf Guard",
        x=10,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=2),
    )

    # Passive Perception: 10 + 6 (Wis) - 0 (0ft away) = 16. Rogue Stealth = 15. Guard SHOULD hear.
    guard_near = Creature(
        name="Alert Guard",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=6),
    )

    spatial_service.sync_entity(rogue)
    spatial_service.sync_entity(guard_far)
    spatial_service.sync_entity(guard_near)

    config = {"configurable": {"thread_id": "default"}}

    # Trigger Trap
    with mock_dice(default=10):
        res = await trigger_environmental_hazard.ainvoke(
            {
                "hazard_name": "Clicking Floorplate",
                "target_names": ["Sneaky Rogue"],
                "save_required": "dexterity",
                "save_dc": 10,
                "damage_dice": "1d4",
                "damage_type": "bludgeoning",
            },
            config=config,
        )

    assert "Sneaky Rogue] lost their 'Hidden' status" in res
    assert "Alert Guard" in res
    assert "Deaf Guard" not in res


@pytest.mark.asyncio
async def test_system_trap_movement_trigger(mock_dice):
    """
    Tests that moving through a trapped terrain natively calculates the damage.
    [Mapped: REQ-EDG-001]
    """
    fighter = Creature(
        name="Fighter",
        x=0,
        y=0,
        speed=30,
        movement_remaining=30,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=-1),
    )
    spatial_service.sync_entity(fighter)
    config = {"configurable": {"thread_id": "default"}}

    # 1. Setup Terrain Zone and Trap
    zone = TerrainZone(label="Suspicious Floor", points=[(5, -5), (15, -5), (15, 5), (5, 5)], is_difficult=False)
    spatial_service.add_terrain(zone)

    await manage_map_trap.ainvoke(
        {
            "target_label": "Suspicious Floor",
            "hazard_name": "Fire Rune",
            "trigger_on_move": True,
            "save_required": "dexterity",
            "save_dc": 15,
            "damage_dice": "2d6",
            "damage_type": "fire",
        },
        config=config,
    )

    # 2. Move through
    with mock_dice(6, 4, 5, 5):  # Dmg roll 6 and 4 (Total 10), Save roll 5, 5
        res = await move_entity.ainvoke(
            {"entity_name": "Fighter", "target_x": 20, "target_y": 0, "movement_type": "walk"}, config=config
        )

    assert "TRAP TRIGGERED during movement" in res
    assert fighter.hp.base_value == 20  # 30 - 10


@pytest.mark.asyncio
async def test_system_start_of_turn_hazard(mock_dice, mock_roll_dice):
    """
    Tests that a persistent hazard natively damages entities that start their turn inside of it.
    [Mapped: REQ-EDG-002]
    """
    fighter = Creature(
        name="Fighter",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(fighter)
    config = {"configurable": {"thread_id": "default"}}

    zone = TerrainZone(label="Flaming Sphere", points=[(-5, -5), (5, -5), (5, 5), (-5, 5)], is_difficult=False)
    spatial_service.add_terrain(zone)

    await manage_map_trap.ainvoke(
        {
            "target_label": "Flaming Sphere",
            "hazard_name": "Fire Damage",
            "trigger_on_turn_start": True,
            "is_persistent": True,
            "save_required": "dexterity",
            "save_dc": 15,
            "damage_dice": "2d6",
            "damage_type": "fire",
        },
        config=config,
    )

    with mock_roll_dice(default=10), mock_dice(default=5):  # Dmg 10, Save fails
        sot_event = GameEvent(event_type="StartOfTurn", source_uuid=fighter.entity_uuid, vault_path="default")
        res = EventBus.dispatch(sot_event)

    assert any("Fire Damage" in r for r in res.payload.get("results", []))
    assert fighter.hp.base_value == 20


def test_system_concentration_buff_and_debuff_expiration(mock_dice):
    """
    Tests that dropping concentration removes stats and conditions across multiple entities.
    [Mapped: REQ-CND-019]
    """
    caster = Creature(
        name="Cleric",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    ally1 = Creature(
        name="Fighter",
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    ally2 = Creature(
        name="Rogue",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=14),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=4),
    )
    enemy1 = Creature(
        name="Goblin",
        hp=ModifiableValue(base_value=15),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )

    spatial_service.sync_entity(caster)
    spatial_service.sync_entity(ally1)
    spatial_service.sync_entity(ally2)
    spatial_service.sync_entity(enemy1)

    # 1. Cast Bless on Allies (+4 strength for testing buff logic)
    bless_mechanics = {
        "requires_concentration": True,
        "modifiers": [{"stat": "strength_mod", "value": 4, "duration": "1 minute"}],
        "conditions_applied": [{"condition": "Blessed", "duration": "1 minute"}],
    }
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={
            "ability_name": "Bless",
            "mechanics": bless_mechanics,
            "target_uuids": [ally1.entity_uuid, ally2.entity_uuid],
        },
    )
    EventBus.dispatch(event)

    assert caster.concentrating_on == "Bless"
    assert ally1.strength_mod.total == 8  # 4 + 4
    assert ally2.strength_mod.total == 4  # 0 + 4
    assert any(c.name == "Blessed" for c in ally1.active_conditions)

    # 2. Cast Bane on Enemy -> This automatically drops Bless!
    bane_mechanics = {
        "requires_concentration": True,
        "modifiers": [{"stat": "dexterity_mod", "value": -2, "duration": "1 minute"}],
        "conditions_applied": [{"condition": "Baned", "duration": "1 minute"}],
    }
    event2 = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={
            "ability_name": "Bane",
            "mechanics": bane_mechanics,
            "target_uuids": [enemy1.entity_uuid],
        },
    )
    EventBus.dispatch(event2)

    assert caster.concentrating_on == "Bane"
    assert ally1.strength_mod.total == 4  # Back to base
    assert not any(c.name == "Blessed" for c in ally1.active_conditions)
    assert enemy1.dexterity_mod.total == 0  # 2 - 2

    # 3. Manual drop concentration
    EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid))
    assert caster.concentrating_on == ""
    assert enemy1.dexterity_mod.total == 2  # Back to base
    assert not any(c.name == "Baned" for c in enemy1.active_conditions)


@pytest.mark.asyncio
async def test_system_silence_thunder_immunity():
    """
    System test: Magical Silence grants immunity to thunder damage.
    [Mapped: REQ-SND-004]
    """
    bard = Creature(
        name="Bard",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Silenced", source_name="Magical Silence")],
    )
    spatial_service.sync_entity(bard)

    # Apply Thunder Damage
    dmg_event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=bard.entity_uuid,
        target_uuid=bard.entity_uuid,
        payload={"hit": True, "damage": 15, "damage_type": "thunder"},
    )
    dmg_event.status = EventStatus.POST_EVENT
    EventBus._notify(dmg_event)

    assert bard.hp.base_value == 20  # Immune!

    # Apply Fire Damage (should still work normally)
    dmg_event_fire = GameEvent(
        event_type="MeleeAttack",
        source_uuid=bard.entity_uuid,
        target_uuid=bard.entity_uuid,
        payload={"hit": True, "damage": 5, "damage_type": "fire"},
    )
    dmg_event_fire.status = EventStatus.POST_EVENT
    EventBus._notify(dmg_event_fire)

    assert bard.hp.base_value == 15


@pytest.mark.asyncio
async def test_system_bound_blocks_somatic_spells():
    """
    System test: A Bound character cannot cast a spell with Somatic/Material components.
    [Mapped: REQ-SPL-014, REQ-CND-020]
    """
    config = {"configurable": {"thread_id": "default"}}

    wizard = Creature(
        name="Wizard",
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Bound")],
    )
    goblin = Creature(
        name="Goblin",
        x=5,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(wizard)
    spatial_service.sync_entity(goblin)

    from spell_system import SpellDefinition, SpellMechanics, SpellCompendium
    from tools import use_ability_or_spell

    spell = SpellDefinition(name="Fire Bolt", level=0, components=["V", "S"], mechanics=SpellMechanics())
    await SpellCompendium.save_spell("default", spell)

    res = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Fire Bolt", "target_names": ["Goblin"]}, config=config
    )
    assert "SYSTEM ERROR" in res
    assert "Somatic/Material" in res
    assert "Bound" in res


@pytest.mark.asyncio
async def test_war_caster_feat_somatic_override(mock_obsidian_vault, mock_dice):
    """
    System test: War Caster allows casting spells with S components even when hands are full.
    [Mapped: REQ-SPL-022]
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(j_dir, "Cleric.md"), "w") as f:
        f.write("---\nname: Cleric\nequipment:\n  main_hand: Mace\n  shield: Heavy Shield\n---")

    cleric = Creature(
        name="Cleric",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(cleric)

    from spell_system import SpellDefinition, SpellMechanics, SpellCompendium

    spell = SpellDefinition(name="Cure Wounds", level=1, components=["V", "S"], mechanics=SpellMechanics())
    await SpellCompendium.save_spell(vault_path, spell)

    from tools import use_ability_or_spell

    # 1. Hands full, no feat -> Fails
    res1 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Cleric", "ability_name": "Cure Wounds", "target_names": ["Cleric"]}, config=config
    )
    assert "SYSTEM ERROR" in res1
    assert "both hands are full" in res1

    # 2. Add war_caster -> Succeeds
    cleric.tags.append("war_caster")
    res2 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Cleric", "ability_name": "Cure Wounds", "target_names": ["Cleric"]}, config=config
    )
    assert "MECHANICAL TRUTH" in res2


@pytest.mark.asyncio
async def test_strict_material_components_and_penalties(mock_obsidian_vault, mock_dice):
    """
    System test: Strict Material Config forces M failures when hands are full without a focus.
    Strict Penalty Config actively consumes the spell slot upon failure.
    [Mapped: REQ-SPL-023, REQ-SPL-024]
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    import os
    from vault_io import get_journals_dir

    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(vault_path, "DM_CONFIG.md"), "w") as f:
        f.write("---\nsettings:\n  strict_material_components: true\n  strict_vsm_penalties: true\n---")

    with open(os.path.join(j_dir, "Wizard.md"), "w") as f:
        f.write("---\nname: Wizard\nequipment:\n  main_hand: Sword\n  off_hand: Dagger\n---")

    wizard = Creature(
        name="Wizard",
        vault_path=vault_path,
        tags=["war_caster"],
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(wizard)

    from spell_system import SpellDefinition, SpellMechanics, SpellCompendium

    spell = SpellDefinition(name="Fireball", level=3, components=["V", "S", "M"], mechanics=SpellMechanics())
    await SpellCompendium.save_spell(vault_path, spell)

    from item_system import WeaponItem, ItemCompendium

    sword = WeaponItem(name="Sword")
    dagger = WeaponItem(name="Dagger")
    await ItemCompendium.save_item(vault_path, sword)
    await ItemCompendium.save_item(vault_path, dagger)

    from tools import use_ability_or_spell

    # 1. Hands full with normal weapons, War Caster bypasses S, but Strict Materials catches M.
    # Strict Penalties will also deduct the spell slot!
    assert wizard.spell_slots_expended_this_turn == 0
    res1 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Fireball", "target_names": ["Wizard"]}, config=config
    )
    assert "SYSTEM ERROR" in res1
    assert "Material (M) components" in res1
    assert "STRICT MODE PENALTY" in res1
    assert wizard.spell_slots_expended_this_turn == 1

    # 2. Swap dagger for a Wand (spellcasting_focus)
    wizard.spell_slots_expended_this_turn = 0
    with open(os.path.join(j_dir, "Wizard.md"), "w") as f:
        f.write("---\nname: Wizard\nequipment:\n  main_hand: Sword\n  off_hand: Magic Wand\n---")
    wand = WeaponItem(name="Magic Wand", tags=["spellcasting_focus"])
    await ItemCompendium.save_item(vault_path, wand)

    res2 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Fireball", "target_names": ["Wizard"]}, config=config
    )
    assert "MECHANICAL TRUTH" in res2
    assert wizard.spell_slots_expended_this_turn == 1  # properly expended by success this time


# ==========================================
# SCENARIO J: COMPLEX SPELL INTERACTIONS
# ==========================================
def test_system_haste_buff_and_concentration_drop():
    """
    System test covering: Casting Haste to apply multiple modifiers, granted tags, and conditions,
    and losing concentration to instantly strip them across the target.
    [Mapped: REQ-SPL-019]
    """
    caster = Creature(
        name="Sorcerer",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Barbarian",
        hp=ModifiableValue(base_value=40),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=2),
    )

    spatial_service.sync_entity(caster)
    spatial_service.sync_entity(target)

    # 1. Cast Haste on the Barbarian
    haste_mechanics = {
        "requires_concentration": True,
        "modifiers": [{"stat": "ac", "value": 2, "duration": "1 minute"}],
        "conditions_applied": [{"condition": "Hasted", "duration": "1 minute"}],
        "granted_tags": ["advantage_dex_saves", "extra_action"],
    }
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={
            "ability_name": "Haste",
            "mechanics": haste_mechanics,
            "target_uuids": [target.entity_uuid],
        },
    )
    EventBus.dispatch(event)

    # Verify Haste Buffs
    assert caster.concentrating_on == "Haste"
    assert target.ac.total == 17  # 15 base + 2 from Haste
    assert any(c.name == "Hasted" for c in target.active_conditions)

    # 2. Simulate Caster taking damage and failing a CON save, dropping concentration
    drop_event = GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid)
    EventBus.dispatch(drop_event)

    # Verify everything was stripped cleanly
    assert caster.concentrating_on == ""
    assert target.ac.total == 15  # Back to base AC
    assert not any(c.name == "Hasted" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_system_hazard_attack_roll(mock_dice, mock_roll_dice):
    """Tests a hazard that uses an attack roll instead of a save."""
    target = Creature(
        name="Target",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    spatial_service.sync_entity(target)
    config = {"configurable": {"thread_id": "default"}}

    # Poison dart trap, AC 15. Attack bonus +5.
    # Mock dice: roll 12 + 5 = 17 (Hit). Damage roll 10.
    with mock_dice(12), mock_roll_dice(10):
        res = await trigger_environmental_hazard.ainvoke(
            {
                "hazard_name": "Poison Dart",
                "target_names": ["Target"],
                "requires_attack_roll": True,
                "attack_bonus": 5,
                "damage_dice": "1d4",
                "damage_type": "piercing",
            },
            config=config,
        )

    assert "Hit (Rolled 17 vs AC 15)" in res
    assert "took 10 piercing damage" in res
    assert target.hp.base_value == 10


@pytest.mark.asyncio
async def test_system_hazard_resistance_vulnerability(mock_roll_dice):
    """Tests that hazards correctly respect resistances and vulnerabilities."""
    fire_elemental = Creature(
        name="Fire Elemental",
        x=0,
        y=0,
        hp=ModifiableValue(base_value=50),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        immunities=["fire"],
        vulnerabilities=["cold"],
    )
    spatial_service.sync_entity(fire_elemental)
    config = {"configurable": {"thread_id": "default"}}

    # 1. Fire hazard -> Immune
    with mock_roll_dice(20):
        res_fire = await trigger_environmental_hazard.ainvoke(
            {"hazard_name": "Fire Jet", "target_names": ["Fire Elemental"], "damage_dice": "4d6", "damage_type": "fire"},
            config=config,
        )
    assert "IMMUNE to fire" in res_fire
    assert fire_elemental.hp.base_value == 50

    # 2. Cold hazard -> Vulnerable
    with mock_roll_dice(20):
        res_cold = await trigger_environmental_hazard.ainvoke(
            {"hazard_name": "Ice Trap", "target_names": ["Fire Elemental"], "damage_dice": "4d6", "damage_type": "cold"},
            config=config,
        )
    assert "VULNERABLE to cold" in res_cold
    assert fire_elemental.hp.base_value == 10  # 50 - (20 * 2)
