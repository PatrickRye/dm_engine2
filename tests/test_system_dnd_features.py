import pytest
from unittest.mock import patch

from dnd_rules_engine import (
    BaseGameEntity, Creature, ModifiableValue, GameEvent, EventBus, EventStatus,
    MeleeWeapon, RangedWeapon, ActiveCondition, NumericalModifier, ModifierPriority
)
from spatial_engine import spatial_service, MapData, Wall, TerrainZone
import event_handlers
from tools import manage_light_sources, toggle_condition, execute_grapple_or_shove, trigger_environmental_hazard, interact_with_object, manage_map_geometry, manage_map_trap, move_entity
from registry import clear_registry

@pytest.fixture(autouse=True)
def setup_system():
    clear_registry()
    spatial_service.clear()
    yield

# ==========================================
# SCENARIO A: BAROVIAN CHURCH SKIRMISH
# ==========================================
def test_system_barovian_church_ranged_combat():
    """
    System test covering: Spatial distance, difficult terrain overlaps, 
    cover AC bonuses, and executing a ranged attack.
    """
    # 1. Setup Environment (Church with pews and rubble)
    pew_wall = Wall(start=(15, 0), end=(15, 10), height=3.0, is_solid=True) # Low wall
    rubble_zone = TerrainZone(points=[(5, -5), (10, -5), (10, 5), (5, 5)], is_difficult=True)
    spatial_service.add_wall(pew_wall)
    spatial_service.add_terrain(rubble_zone)
    
    # 2. Setup Entities
    ranger = Creature(name="Ireena", x=0, y=0, z=0, hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=14), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=3), speed=30, movement_remaining=30)
    bow = RangedWeapon(name="Shortbow", damage_dice="1d6", damage_type="piercing", normal_range=80, long_range=320)
    ranger.equipped_weapon_uuid = bow.entity_uuid
    
    vampire = Creature(name="Doru", x=20, y=0, z=0, height=5.0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    spatial_service.sync_entity(ranger)
    spatial_service.sync_entity(vampire)
    
    # 3. Execution: Ranger moves 15 ft (5ft normal + 5ft diff * 2) = 15 cost.
    move_event = GameEvent(event_type="Movement", source_uuid=ranger.entity_uuid, payload={"target_x": 10, "target_y": 0, "target_z": 0, "movement_type": "walk"})
    EventBus.dispatch(move_event)
    
    assert move_event.status != 5 # Not cancelled
    assert move_event.payload["cost"] == 15
    
    # Update spatial positioning
    ranger.x = 10
    spatial_service.sync_entity(ranger)
    
    # 4. Execution: Ranger shoots Doru behind the pew.
    # Doru is at x=20. Wall is at x=15. Ranger is at x=10.
    # The wall is 3ft high, Doru is 5ft high. This should grant Half Cover (+2 AC).
    with patch('random.randint', side_effect=[14, 14, 5]): # Roll 14 + 3 DEX = 17 vs AC 17 (15 + 2 Cover). HIT!
        atk_event = GameEvent(event_type="MeleeAttack", source_uuid=ranger.entity_uuid, target_uuid=vampire.entity_uuid)
        EventBus.dispatch(atk_event)
        
        assert atk_event.payload["hit"] is True
        assert vampire.hp.base_value == 30 - (5 + 3) # 5 dice + 3 dex = 8 dmg. 22 HP left.

# ==========================================
# SCENARIO B: THE SENTINEL OF BAROVIA
# ==========================================
def test_system_sentinel_feat_interaction():
    """
    System test covering: Feat tag injections overriding default mechanics,
    reaction consumption, and movement halting.
    """
    fighter = Creature(name="Paladin", tags=["pc", "ignores_disengage", "oa_halts_movement"], x=0, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=18), strength_mod=ModifiableValue(base_value=4), dexterity_mod=ModifiableValue(base_value=0))
    sword = MeleeWeapon(name="Longsword", damage_dice="1d8", damage_type="slashing")
    fighter.equipped_weapon_uuid = sword.entity_uuid
    
    goblin = Creature(name="Fleeing Goblin", tags=["monster"], x=5, y=0, hp=ModifiableValue(base_value=15), ac=ModifiableValue(base_value=12), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0), movement_remaining=30)
    
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(goblin)
    
    # 1. Goblin uses Disengage and tries to move away.
    move_event = GameEvent(event_type="Movement", source_uuid=goblin.entity_uuid, payload={"target_x": 15, "target_y": 0, "target_z": 0, "movement_type": "disengage"})
    EventBus.dispatch(move_event)
    
    # The Sentinel feat ("ignores_disengage") should flag the fighter as an opportunity attacker
    assert "Paladin" in move_event.payload.get("opportunity_attackers", [])
    
    # 2. Simulate AI deciding to use Reaction to attack
    fighter.reaction_used = True
    with patch('random.randint', side_effect=[15, 15, 6]): # 15 + 4 = 19 vs AC 12 (Hit). Dmg 6 + 4 = 10.
        oa_event = GameEvent(event_type="MeleeAttack", source_uuid=fighter.entity_uuid, target_uuid=goblin.entity_uuid)
        
        # In production, main.py intercepts the OA flag and modifies goblin.movement_remaining if hit
        EventBus.dispatch(oa_event)
        if oa_event.payload.get("hit") and "oa_halts_movement" in fighter.tags:
            goblin.movement_remaining = 0
            
    assert goblin.hp.base_value == 5
    assert goblin.movement_remaining == 0 # Halted by the Sentinel hit!

# ==========================================
# SCENARIO C: ADVANCED TRAITS & MOVEMENT
# ==========================================
def test_system_feat_ignore_difficult_terrain():
    """System test covering: Feat tags perfectly bypassing double movement multipliers."""
    ranger = Creature(name="Strider", tags=["ignore_difficult_terrain"], x=0, y=0, speed=30, movement_remaining=30, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    zone = TerrainZone(points=[(0, -5), (20, -5), (20, 5), (0, 5)], is_difficult=True)
    spatial_service.add_terrain(zone)
    spatial_service.sync_entity(ranger)
    
    move_event = GameEvent(event_type="Movement", source_uuid=ranger.entity_uuid, payload={"target_x": 20, "target_y": 0, "target_z": 0, "movement_type": "walk"})
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
    """System test covering: Long rest fully heals, recharges resources, and wipes out temporary conditions/buffs."""
    wizard = Creature(name="Exhausted Wizard", max_hp=25, hp=ModifiableValue(base_value=2), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0), resources={"Spell Slots": "0/4", "Lucky": "1/3"})
    
    # Add a 1-hour buff and an 8-hour condition
    wizard.strength_mod.add_modifier(NumericalModifier(priority=ModifierPriority.ADDITIVE, value=2, source_name="Bull's Strength", duration_seconds=3600))
    wizard.active_conditions.append(ActiveCondition(name="Poisoned", duration_seconds=28800))
    
    # Simulate `take_rest` tool dispatching AdvanceTime for 8 hours
    time_event = GameEvent(event_type="AdvanceTime", source_uuid=wizard.entity_uuid, payload={"seconds_advanced": 28800})
    EventBus.dispatch(time_event)
    
    # Simulate `take_rest` tool dispatching the Rest event
    rest_event = GameEvent(event_type="Rest", source_uuid=wizard.entity_uuid, payload={"rest_type": "long", "target_uuids": [wizard.entity_uuid]})
    EventBus.dispatch(rest_event)
    
    assert wizard.hp.base_value == 25 # Healed to max
    assert wizard.resources["Spell Slots"] == "4/4" # Bounded string regex recharge
    assert wizard.resources["Lucky"] == "3/3"
    assert wizard.strength_mod.total == 0 # 1 hour buff expired
    assert len(wizard.active_conditions) == 0 # 8 hour condition exactly expired

def test_system_short_rest_mechanics():
    """System test covering: Short rest does NOT over-heal or recharge long-rest resources, but DOES advance time for conditions."""
    warlock = Creature(name="Warlock", max_hp=30, hp=ModifiableValue(base_value=15), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0), resources={"Long Rest Spell Slots": "0/2"})
    
    # Add a 10-minute (600s) condition and a 2-hour (7200s) condition
    warlock.active_conditions.append(ActiveCondition(name="Stunned", duration_seconds=600))
    warlock.active_conditions.append(ActiveCondition(name="Hex", duration_seconds=7200))
    
    # Simulate `take_rest` tool dispatching AdvanceTime for 1 hour (Short Rest)
    time_event = GameEvent(event_type="AdvanceTime", source_uuid=warlock.entity_uuid, payload={"seconds_advanced": 3600})
    EventBus.dispatch(time_event)
    
    rest_event = GameEvent(event_type="Rest", source_uuid=warlock.entity_uuid, payload={"rest_type": "short", "target_uuids": [warlock.entity_uuid]})
    EventBus.dispatch(rest_event)
    
    # Validate Rest behavior (Short rest doesn't auto-heal or restore long-rest slots natively)
    assert warlock.hp.base_value == 15 
    assert warlock.resources["Long Rest Spell Slots"] == "0/2"
    
    # Validate Time behavior
    active_cond_names = [c.name for c in warlock.active_conditions]
    assert "Stunned" not in active_cond_names # 10 min expired
    assert "Hex" in active_cond_names # 2 hr did NOT expire (has 1 hour left)
    assert warlock.active_conditions[0].duration_seconds == 3600 # Exactly 1 hour left

# ==========================================
# SCENARIO E: DYNAMIC LIGHTING & VISION
# ==========================================
@pytest.mark.asyncio
async def test_system_dynamic_lighting_and_combat():
    """System test covering: Darkness giving disadvantage, and tools dynamically fixing it."""
    human = Creature(name="Human", x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    # Target has darkvision so they can see the human perfectly, meaning the Human doesn't get Unseen Attacker Advantage to cancel out their Disadvantage.
    target = Creature(name="Goblin", tags=["darkvision"], x=5, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    sword = MeleeWeapon(name="Sword", damage_dice="1d8", damage_type="slashing")
    human.equipped_weapon_uuid = sword.entity_uuid

    spatial_service.sync_entity(human)
    spatial_service.sync_entity(target)
    spatial_service.map_data.lights.clear() # Absolute Darkness
    
    # 1. Attack in Darkness
    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=human.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(atk_event)
    assert atk_event.payload.get("disadvantage") is True # Human can't see target
    
    # 2. Add light via tool & re-attack
    await manage_light_sources.ainvoke({"action": "add", "label": "Torch", "x": 0, "y": 0, "bright_radius": 20, "dim_radius": 40}, config={"configurable": {"thread_id": "default"}})
    atk_event_2 = GameEvent(event_type="MeleeAttack", source_uuid=human.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(atk_event_2)
    assert atk_event_2.payload.get("disadvantage") is not True # Environment is now bright

@pytest.mark.asyncio
async def test_system_dynamic_light_movement_and_static_torches():
    """Tests that static torches remain in place while attached lights follow entities."""
    pc = Creature(name="Lightbringer", x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    spatial_service.sync_entity(pc)
    config = {"configurable": {"thread_id": "default"}}
    config = {"configurable": {"thread_id": "default"}}
    
    # Add static torch at (50, 0)
    await manage_light_sources.ainvoke({"action": "add", "label": "Wall Torch", "x": 50, "y": 0, "bright_radius": 20, "dim_radius": 40}, config=config)
    
    # Add attached light to PC
    await manage_light_sources.ainvoke({"action": "add", "label": "Glowing Staff", "attached_to_entity": "Lightbringer", "bright_radius": 20, "dim_radius": 40}, config=config)
    
    # Initial checks
    assert spatial_service.get_illumination(0, 0, 0) == "bright" # PC's light
    assert spatial_service.get_illumination(50, 0, 0) == "bright" # Wall torch
    assert spatial_service.get_illumination(100, 0, 0) == "darkness" # Nowhere near either
    
    # Move PC away
    move_event = GameEvent(event_type="Movement", source_uuid=pc.entity_uuid, payload={"target_x": 100, "target_y": 0, "target_z": 0, "movement_type": "teleport"})
    EventBus.dispatch(move_event)
    pc.x = 100
    spatial_service.sync_entity(pc)
    
    # Re-verify
    assert spatial_service.get_illumination(0, 0, 0) == "darkness" # PC left, took light with them
    assert spatial_service.get_illumination(50, 0, 0) == "bright" # Wall torch stayed static
    assert spatial_service.get_illumination(100, 0, 0) == "bright" # Light followed PC

@pytest.mark.asyncio
async def test_system_disarm_and_drop_light():
    """Tests that a dropped or disarmed light source stops following the player."""
    pc = Creature(name="Wizard", x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    spatial_service.sync_entity(pc)
    config = {"configurable": {"thread_id": "default"}}
    
    await manage_light_sources.ainvoke({"action": "add", "label": "Magic Staff", "attached_to_entity": "Wizard", "bright_radius": 10, "dim_radius": 20}, config=config)
    
    # Move PC to (30, 0) - Staff follows
    pc.x = 30
    spatial_service.sync_entity(pc)
    assert spatial_service.get_illumination(30, 0, 0) == "bright"
    
    # Disarm: Remove attached staff, add static staff at current coordinates
    await manage_light_sources.ainvoke({"action": "remove", "label": "Magic Staff"}, config=config)
    await manage_light_sources.ainvoke({"action": "add", "label": "Dropped Staff", "x": pc.x, "y": pc.y, "bright_radius": 10, "dim_radius": 20}, config=config)
    
    # Move PC to (60, 0)
    pc.x = 60
    spatial_service.sync_entity(pc)
    
    assert spatial_service.get_illumination(30, 0, 0) == "bright" # Dropped staff stayed here
    assert spatial_service.get_illumination(60, 0, 0) == "darkness" # PC is now in the dark
    
@pytest.mark.asyncio
async def test_system_snuff_light_source():
    """Tests that snuffing a light source instantly plunges the area into darkness."""
    config = {"configurable": {"thread_id": "default"}}
    await manage_light_sources.ainvoke({"action": "add", "label": "Campfire", "x": 0, "y": 0, "bright_radius": 15, "dim_radius": 30}, config=config)
    assert spatial_service.get_illumination(10, 0, 0) == "bright"
    
    await manage_light_sources.ainvoke({"action": "remove", "label": "Campfire"}, config=config)
    assert spatial_service.get_illumination(10, 0, 0) == "darkness"

@pytest.mark.asyncio
async def test_system_spell_illumination_areas():
    """Tests that spells (like Daylight) correctly cast bright and dim light at specific ranges."""
    config = {"configurable": {"thread_id": "default"}}
    # Daylight spell: 60ft bright, additional 60ft dim (120ft total)
    await manage_light_sources.ainvoke({"action": "add", "label": "Daylight Spell", "x": 0, "y": 0, "bright_radius": 60, "dim_radius": 120}, config=config)
    
    assert spatial_service.get_illumination(30, 0, 0) == "bright"
    assert spatial_service.get_illumination(60, 0, 0) == "bright"
    assert spatial_service.get_illumination(90, 0, 0) == "dim"
    assert spatial_service.get_illumination(120, 0, 0) == "dim"
    assert spatial_service.get_illumination(150, 0, 0) == "darkness"

@pytest.mark.asyncio
async def test_system_stealth_and_hidden_combat():
    """Tests that characters can gain the 'Hidden' condition, gain advantage, and lose it upon attacking."""
    rogue = Creature(name="Rogue", x=5, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    target = Creature(name="Guard", x=10, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    dagger = MeleeWeapon(name="Dagger", damage_dice="1d4", damage_type="piercing")
    rogue.equipped_weapon_uuid = dagger.entity_uuid

    spatial_service.sync_entity(rogue)
    spatial_service.sync_entity(target)
    spatial_service.map_data.lights.clear() # Pitch black
    
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
async def test_system_grapple_contest_success_and_fail():
    """Tests successful and unsuccessful Grapple contests."""
    attacker = Creature(name="Orc", x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=4), dexterity_mod=ModifiableValue(base_value=0))
    target = Creature(name="Bard", x=5, y=0, speed=30, movement_remaining=30, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=2))
    
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    
    import os
    from vault_io import get_journals_dir
    j_dir = get_journals_dir("default")
    with open(os.path.join(j_dir, "Bard.md"), "w") as f:
        f.write("---\nname: Bard\nactive_conditions: []\n---\n")
        
    config = {"configurable": {"thread_id": "default"}}
    
    # Test 1: Attacker Wins (Roll 15 vs 5)
    with patch('random.randint', side_effect=[15, 15, 5]):
        res = await execute_grapple_or_shove.ainvoke({"attacker_name": "Orc", "target_name": "Bard", "action_type": "grapple"}, config=config)
        assert "Attacker wins" in res
        assert any(c.name == "Grappled" for c in target.active_conditions)
        assert target.movement_remaining == 0
        
    # Test 2: Defender Wins Tie (Roll 10 vs 12, tying total at 14)
    target.active_conditions = []
    target.movement_remaining = 30
    with patch('random.randint', side_effect=[10, 10, 12]):
        res = await execute_grapple_or_shove.ainvoke({"attacker_name": "Orc", "target_name": "Bard", "action_type": "grapple"}, config=config)
        assert "Defender wins" in res
        assert not any(c.name == "Grappled" for c in target.active_conditions)
        assert target.movement_remaining == 30

@pytest.mark.asyncio
async def test_system_shove_movement_direction():
    """Tests that a successful shove perfectly calculates the vector and pushes the target backward."""
    attacker = Creature(name="Orc", x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=4), dexterity_mod=ModifiableValue(base_value=0))
    # Target is at (5, 0). Vector is (1, 0). Pushing away should natively land them at (10, 0).
    target = Creature(name="Bard", x=5, y=0, speed=30, movement_remaining=30, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=2))
    
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    
    import os
    from vault_io import get_journals_dir
    j_dir = get_journals_dir("default")
    with open(os.path.join(j_dir, "Bard.md"), "w") as f:
        f.write("---\nname: Bard\nactive_conditions: []\n---\n")
        
    config = {"configurable": {"thread_id": "default"}}
    
    with patch('random.randint', side_effect=[15, 15, 5]):
        res = await execute_grapple_or_shove.ainvoke({"attacker_name": "Orc", "target_name": "Bard", "action_type": "shove", "shove_type": "away"}, config=config)
        assert "shoved 5.0 feet away" in res
        assert target.x == 10.0
        assert target.y == 0.0

@pytest.mark.asyncio
async def test_system_throw_breaks_grapple_and_knocks_prone():
    """Tests that throwing a grappled enemy natively pushes them, knocks them prone, and breaks the grapple."""
    attacker = Creature(name="Orc", x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=4), dexterity_mod=ModifiableValue(base_value=0))
    target = Creature(name="Bard", x=5, y=0, speed=30, movement_remaining=30, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=2))
    
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    
    import os
    from vault_io import get_journals_dir
    j_dir = get_journals_dir("default")
    with open(os.path.join(j_dir, "Bard.md"), "w") as f:
        f.write("---\nname: Bard\nactive_conditions: []\n---\n")
        
    config = {"configurable": {"thread_id": "default"}}
    
    # 1. Attacker successfully grapples Target
    with patch('random.randint', side_effect=[15, 15, 5]):
        await execute_grapple_or_shove.ainvoke({"attacker_name": "Orc", "target_name": "Bard", "action_type": "grapple"}, config=config)
        
    assert any(c.name == "Grappled" for c in target.active_conditions)
    assert target.movement_remaining == 0
    
    # 2. Attacker successfully throws Target 15 feet
    with patch('random.randint', side_effect=[18, 18, 4]):
        res = await execute_grapple_or_shove.ainvoke({"attacker_name": "Orc", "target_name": "Bard", "action_type": "throw", "throw_distance": 15.0}, config=config)
        
    assert "thrown 15.0 feet away" in res
    assert "lands Prone" in res
    
    assert target.x == 20.0 # 5ft starting distance + 15ft throw vector
    assert not any(c.name == "Grappled" for c in target.active_conditions) # 20ft > 7.5ft break threshold
    assert any(c.name == "Prone" for c in target.active_conditions)

# ==========================================
# SCENARIO F: ADVANCED PERCEPTION & SENSES
# ==========================================
@pytest.mark.asyncio
async def test_system_blindsight_vs_invisibility():
    """System test: Blindsight ignores invisibility and darkness."""
    bat = Creature(name="Giant Bat", tags=["blindsight_60"], x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    invisible_mage = Creature(name="Mage", tags=["invisible"], x=5, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    bite = MeleeWeapon(name="Bite", damage_dice="1d4", damage_type="piercing")
    bat.equipped_weapon_uuid = bite.entity_uuid

    spatial_service.sync_entity(bat)
    spatial_service.sync_entity(invisible_mage)
    spatial_service.map_data.lights.clear() # Pitch black
    
    atk_event = GameEvent(event_type="MeleeAttack", source_uuid=bat.entity_uuid, target_uuid=invisible_mage.entity_uuid)
    EventBus.dispatch(atk_event)
    
    # Bat can see Mage (Blindsight 60 >= 30). Mage CANNOT see Bat (Darkness, no darkvision). 
    # Attacker unseen by target = Advantage. Attacker can see target = No Disadvantage.
    assert atk_event.payload.get("advantage") is True
    assert atk_event.payload.get("disadvantage") is not True

@pytest.mark.asyncio
async def test_system_tremorsense_vs_flying():
    """System test: Tremorsense works in darkness but fails on flying targets."""
    bulette = Creature(name="Bulette", tags=["tremorsense_60"], x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    ground_target = Creature(name="Fighter", x=5, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    flying_target = Creature(name="Aarakocra", tags=["flying"], x=5, y=5, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    bite = MeleeWeapon(name="Bite", damage_dice="4d12", damage_type="piercing")
    bulette.equipped_weapon_uuid = bite.entity_uuid

    spatial_service.sync_entity(bulette)
    spatial_service.sync_entity(ground_target)
    spatial_service.sync_entity(flying_target)
    spatial_service.map_data.lights.clear()
    
    atk_ground = GameEvent(event_type="MeleeAttack", source_uuid=bulette.entity_uuid, target_uuid=ground_target.entity_uuid)
    EventBus.dispatch(atk_ground)
    assert atk_ground.payload.get("disadvantage") is not True # Can sense ground target
    
    atk_flying = GameEvent(event_type="MeleeAttack", source_uuid=bulette.entity_uuid, target_uuid=flying_target.entity_uuid)
    EventBus.dispatch(atk_flying)
    assert atk_flying.payload.get("disadvantage") is True # Cannot sense flying target in darkness

@pytest.mark.asyncio
async def test_system_darkvision_range_limit():
    """System test: Darkvision only works up to its specified range."""
    elf = Creature(name="Elf", tags=["darkvision_60"], x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    goblin_near = Creature(name="Goblin Near", x=50, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    goblin_far = Creature(name="Goblin Far", x=70, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    spatial_service.sync_entity(elf)
    spatial_service.sync_entity(goblin_near)
    spatial_service.sync_entity(goblin_far)
    spatial_service.map_data.lights.clear()
    
    bow = RangedWeapon(name="Longbow", damage_dice="1d8", damage_type="piercing", normal_range=150, long_range=600)
    elf.equipped_weapon_uuid = bow.entity_uuid
    
    atk_near = GameEvent(event_type="MeleeAttack", source_uuid=elf.entity_uuid, target_uuid=goblin_near.entity_uuid)
    EventBus.dispatch(atk_near)
    assert atk_near.payload.get("disadvantage") is not True # 50ft <= 60ft Darkvision
    
    atk_far = GameEvent(event_type="MeleeAttack", source_uuid=elf.entity_uuid, target_uuid=goblin_far.entity_uuid)
    EventBus.dispatch(atk_far)
    assert atk_far.payload.get("disadvantage") is True # 70ft > 60ft Darkvision

# ==========================================
# SCENARIO H: ENVIRONMENTAL HAZARDS
# ==========================================
@pytest.mark.asyncio
async def test_system_trigger_environmental_hazard():
    """Tests the trigger_environmental_hazard tool for applying AoE saves, damage, and conditions."""
    rogue = Creature(name="Rogue", tags=["evasion"], x=0, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=5))
    fighter = Creature(name="Fighter", x=10, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=18), strength_mod=ModifiableValue(base_value=4), dexterity_mod=ModifiableValue(base_value=-1))
    wizard = Creature(name="Wizard", x=100, y=0, hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    spatial_service.sync_entity(rogue)
    spatial_service.sync_entity(fighter)
    spatial_service.sync_entity(wizard)
    
    config = {"configurable": {"thread_id": "default"}}
    
    # Trigger a Poison Gas Trap at (5, 0) with a 20ft radius
    # DC 15 Dex save, 4d6 poison, applies 'Poisoned'
    with patch('random.randint', return_value=5): # Saves roll 5 (Rogue 10 FAIL, Fighter 4 FAIL). Damage rolls 5 (4d6 = 20).
        res = await trigger_environmental_hazard.ainvoke({
            "hazard_name": "Poison Gas Trap",
            "origin_x": 5.0, "origin_y": 0.0, "radius": 20.0,
            "save_required": "dexterity", "save_dc": 15,
            "damage_dice": "4d6", "damage_type": "poison", "half_damage_on_save": True,
            "condition_applied": "Poisoned"
        }, config=config)
        
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
@pytest.mark.asyncio
async def test_system_trap_interaction_fail():
    """Tests that failing to pick a trapped lock natively explodes in the PC's face."""
    rogue = Creature(name="Rogue", x=0, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=5))
    spatial_service.sync_entity(rogue)
    config = {"configurable": {"thread_id": "default"}}
    
    # 1. Setup Wall and Trap
    wall = Wall(label="Chest", start=(5, 5), end=(5, 5), is_locked=True, interact_dc=20)
    spatial_service.add_wall(wall)
    
    await manage_map_trap.ainvoke({
        "target_label": "Chest", "hazard_name": "Poison Needle",
        "trigger_on_interact_fail": True, "save_required": "constitution", "save_dc": 15,
        "damage_dice": "1d10", "damage_type": "poison", "condition_applied": "Poisoned"
    }, config=config)
    
    # 2. Interact - Fail (Roll 5 lockpick + Mod fails DC 20. Then Roll 5 Save fails DC 15. Damage rolls 8)
    with patch('random.randint', side_effect=[5, 8, 5, 5]):
        res = await interact_with_object.ainvoke({"character_name": "Rogue", "target_label": "Chest", "interaction_type": "lockpick"}, config=config)
        
    assert "FAILURE" in res
    assert "TRAP TRIGGERED" in res
    assert rogue.hp.base_value == 22 # 30 - 8
    assert any(c.name == "Poisoned" for c in rogue.active_conditions)

@pytest.mark.asyncio
async def test_system_trap_stealth_perception_hook():
    """Tests that a triggered trap globally alerts NPCs whose distance-modified Passive Perception beats the triggerer's Stealth."""
    rogue = Creature(name="Sneaky Rogue", tags=["pc"], x=0, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=5))
    rogue.active_conditions.append(ActiveCondition(name="Hidden"))
    
    # Passive Perception: 10 + 2 (Wis) - 1 (10ft away) = 11. Rogue Stealth = 15. Guard should NOT hear.
    guard_far = Creature(name="Deaf Guard", x=10, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=18), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0), wisdom_mod=ModifiableValue(base_value=2))
    
    # Passive Perception: 10 + 6 (Wis) - 0 (0ft away) = 16. Rogue Stealth = 15. Guard SHOULD hear.
    guard_near = Creature(name="Alert Guard", x=0, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=18), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0), wisdom_mod=ModifiableValue(base_value=6))
    
    spatial_service.sync_entity(rogue)
    spatial_service.sync_entity(guard_far)
    spatial_service.sync_entity(guard_near)
    
    config = {"configurable": {"thread_id": "default"}}
    
    # Trigger Trap
    with patch('random.randint', return_value=10):
        res = await trigger_environmental_hazard.ainvoke({
            "hazard_name": "Clicking Floorplate",
            "target_names": ["Sneaky Rogue"],
            "save_required": "dexterity", "save_dc": 10,
            "damage_dice": "1d4", "damage_type": "bludgeoning"
        }, config=config)
        
    assert "Sneaky Rogue] lost their 'Hidden' status" in res
    assert "Alert Guard" in res
    assert "Deaf Guard" not in res

@pytest.mark.asyncio
async def test_system_trap_movement_trigger():
    """Tests that moving through a trapped terrain natively calculates the damage."""
    fighter = Creature(name="Fighter", x=0, y=0, hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=18), strength_mod=ModifiableValue(base_value=4), dexterity_mod=ModifiableValue(base_value=-1), speed=30, movement_remaining=30)
    spatial_service.sync_entity(fighter)
    config = {"configurable": {"thread_id": "default"}}
    
    # 1. Setup Terrain Zone and Trap
    zone = TerrainZone(label="Suspicious Floor", points=[(5, -5), (15, -5), (15, 5), (5, 5)], is_difficult=False)
    spatial_service.add_terrain(zone)
    
    await manage_map_trap.ainvoke({"target_label": "Suspicious Floor", "hazard_name": "Fire Rune", "trigger_on_move": True, "save_required": "dexterity", "save_dc": 15, "damage_dice": "2d6", "damage_type": "fire"}, config=config)
    
    # 2. Move through
    with patch('random.randint', side_effect=[6, 4, 5, 5]): # Dmg roll 6 and 4 (Total 10), Save roll 5, 5
        res = await move_entity.ainvoke({"entity_name": "Fighter", "target_x": 20, "target_y": 0, "movement_type": "walk"}, config=config)
        
    assert "TRAP TRIGGERED during movement" in res
    assert fighter.hp.base_value == 20 # 30 - 10

def test_system_concentration_buff_and_debuff_expiration():
    """Tests that dropping concentration removes stats and conditions across multiple entities."""
    caster = Creature(name="Cleric", hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    ally1 = Creature(name="Fighter", hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=4), dexterity_mod=ModifiableValue(base_value=0))
    ally2 = Creature(name="Rogue", hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=14), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=4))
    enemy1 = Creature(name="Goblin", hp=ModifiableValue(base_value=15), ac=ModifiableValue(base_value=12), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=2))
    
    spatial_service.sync_entity(caster)
    spatial_service.sync_entity(ally1)
    spatial_service.sync_entity(ally2)
    spatial_service.sync_entity(enemy1)
    
    # 1. Cast Bless on Allies (+4 strength for testing buff logic)
    bless_mechanics = {
        "requires_concentration": True,
        "modifiers": [{"stat": "strength_mod", "value": 4, "duration": "1 minute"}],
        "conditions_applied": [{"condition": "Blessed", "duration": "1 minute"}]
    }
    event = GameEvent(event_type="SpellCast", source_uuid=caster.entity_uuid, payload={"ability_name": "Bless", "mechanics": bless_mechanics, "target_uuids": [ally1.entity_uuid, ally2.entity_uuid]})
    EventBus.dispatch(event)
    
    assert caster.concentrating_on == "Bless"
    assert ally1.strength_mod.total == 8 # 4 + 4
    assert ally2.strength_mod.total == 4 # 0 + 4
    assert any(c.name == "Blessed" for c in ally1.active_conditions)
    
    # 2. Cast Bane on Enemy -> This automatically drops Bless!
    bane_mechanics = {
        "requires_concentration": True,
        "modifiers": [{"stat": "dexterity_mod", "value": -2, "duration": "1 minute"}],
        "conditions_applied": [{"condition": "Baned", "duration": "1 minute"}]
    }
    event2 = GameEvent(event_type="SpellCast", source_uuid=caster.entity_uuid, payload={"ability_name": "Bane", "mechanics": bane_mechanics, "target_uuids": [enemy1.entity_uuid]})
    EventBus.dispatch(event2)
    
    assert caster.concentrating_on == "Bane"
    assert ally1.strength_mod.total == 4 # Back to base
    assert not any(c.name == "Blessed" for c in ally1.active_conditions)
    assert enemy1.dexterity_mod.total == 0 # 2 - 2
    
    # 3. Manual drop concentration
    EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid))
    assert caster.concentrating_on == ""
    assert enemy1.dexterity_mod.total == 2 # Back to base
    assert not any(c.name == "Baned" for c in enemy1.active_conditions)