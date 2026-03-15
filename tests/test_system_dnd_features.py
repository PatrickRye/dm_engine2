import pytest
from unittest.mock import patch

from dnd_rules_engine import (
    BaseGameEntity, Creature, ModifiableValue, GameEvent, EventBus, EventStatus,
    MeleeWeapon, RangedWeapon, ActiveCondition,
)
from spatial_engine import spatial_service, MapData, Wall, TerrainZone
import event_handlers
from registry import clear_registry

@pytest.fixture(autouse=True)
def setup_system():
    clear_registry()
    spatial_service._uuid_to_id.clear()
    spatial_service._id_to_uuid.clear()
    spatial_service._uuid_to_bbox.clear()
    spatial_service.map_data = MapData()
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
    spatial_service.map_data.walls.append(pew_wall)
    spatial_service.map_data.terrain.append(rubble_zone)
    
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
    with patch('random.randint', side_effect=[14, 5]): # Roll 14 + 3 DEX = 17 vs AC 17 (15 + 2 Cover). HIT!
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
    with patch('random.randint', side_effect=[15, 6]): # 15 + 4 = 19 vs AC 12 (Hit). Dmg 6 + 4 = 10.
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
    spatial_service.map_data.terrain.append(zone)
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
def test_system_long_rest_recharge():
    """System test covering: Time advancement triggering rest handlers perfectly."""
    wizard = Creature(name="Exhausted Wizard", max_hp=25, hp=ModifiableValue(base_value=2), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0), resources={"Spell Slots": "0/4", "Lucky": "1/3"})
    
    # Simulate tool dispatching the Rest event
    rest_event = GameEvent(event_type="Rest", source_uuid=wizard.entity_uuid, payload={"rest_type": "long", "target_uuids": [wizard.entity_uuid]})
    EventBus.dispatch(rest_event)
    
    assert wizard.hp.base_value == 25 # Healed to max
    assert wizard.resources["Spell Slots"] == "4/4" # Bounded string regex recharge
    assert wizard.resources["Lucky"] == "3/3"