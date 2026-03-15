import pytest
import uuid
from unittest.mock import patch

from dnd_rules_engine import (
    BaseGameEntity, Creature, ModifiableValue, GameEvent, EventBus, EventStatus,
    MeleeWeapon, RangedWeapon, NumericalModifier, ModifierPriority, ActiveCondition
)
from spatial_engine import spatial_service, MapData, Wall, TerrainZone
import event_handlers
from registry import clear_registry

@pytest.fixture(autouse=True)
def reset_engine_state():
    """Clears the engine and spatial registries before every test."""
    clear_registry()
    spatial_service._uuid_to_id.clear()
    spatial_service._id_to_uuid.clear()
    spatial_service._uuid_to_bbox.clear()
    spatial_service.map_data = MapData()
    yield

# ==========================================
# 1. SPATIAL ENGINE UNIT TESTS
# ==========================================

def test_spatial_chebyshev_vs_euclidean():
    """Tests standard 5e square grid math vs realistic sphere math."""
    # 3D diagonal: 30ft X, 40ft Y, 50ft Z
    spatial_service.map_data.distance_metric = "chebyshev"
    dist_cheb = spatial_service.calculate_distance(0, 0, 0, 30, 40, 50)
    assert dist_cheb == 50.0  # Max of (30, 40, 50)

    spatial_service.map_data.distance_metric = "euclidean"
    dist_euc = spatial_service.calculate_distance(0, 0, 0, 30, 40, 50)
    assert round(dist_euc, 2) == 70.71  # sqrt(30^2 + 40^2 + 50^2)

def test_spatial_line_of_sight_and_cover():
    """Tests that bounding box intersections with walls correctly assign cover."""
    attacker = Creature(name="Archer", x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), dexterity_mod=ModifiableValue(base_value=3), strength_mod=ModifiableValue(base_value=0))
    target = Creature(name="Goblin", x=10, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), dexterity_mod=ModifiableValue(base_value=2), strength_mod=ModifiableValue(base_value=0))
    
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)

    # Create a wall that blocks exactly half the target (Y from 0 to 5)
    wall = Wall(start=(5, 0), end=(5, 5), z=0, height=10, is_solid=True)
    spatial_service.map_data.walls.append(wall)

    dist, cover = spatial_service.get_distance_and_cover(attacker.entity_uuid, target.entity_uuid)
    assert cover == "Half"  # Partial blockage yields Half cover

    # Extend wall to block entirely
    wall.end = (5, -5) # Now spans Y: -5 to 5, completely covering the 5x5 bounding box
    dist, cover = spatial_service.get_distance_and_cover(attacker.entity_uuid, target.entity_uuid)
    assert cover == "Total"

def test_spatial_difficult_terrain_overlap():
    """Tests pathing through difficult terrain applies double cost only to the overlap."""
    zone = TerrainZone(points=[(5, -5), (15, -5), (15, 5), (5, 5)], is_difficult=True)
    spatial_service.map_data.terrain.append(zone)
    
    # Moving from x=0 to x=20 straight across the zone.
    # Path: 0->5 (Normal, 5ft) + 5->15 (Difficult, 10ft * 2) + 15->20 (Normal, 5ft) = 30ft
    normal_dist, diff_dist = spatial_service.calculate_path_terrain_costs(0, 0, 0, 20, 0, 0)
    assert normal_dist == 10.0
    assert diff_dist == 10.0

def test_spatial_movement_blocked_by_wall():
    """Tests that 3D pathing math accurately detects wall collisions, including vertical bounds."""
    wall = Wall(start=(5, -5), end=(5, 5), z=0, height=10, is_solid=True)
    spatial_service.map_data.walls.append(wall)
    
    # 1. Straight path through the wall should collide
    assert spatial_service.check_path_collision(0, 0, 0, 10, 0, 0) is True
    
    # 2. Flying over the wall (Z=15) avoids the 10ft high wall entirely
    assert spatial_service.check_path_collision(0, 0, 15, 10, 0, 15) is False

# ==========================================
# 2. TIME & INITIATIVE TESTS
# ==========================================

def test_initiative_clock_modifier_expiration():
    """Tests that buffs expire dynamically on the caster's specific initiative turn."""
    pc = Creature(name="Wizard", hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    # Add a buff applied on Initiative 15, lasting 12 seconds (2 rounds)
    mod = NumericalModifier(priority=ModifierPriority.ADDITIVE, value=2, source_name="Bless", duration_seconds=12, applied_initiative=15)
    pc.strength_mod.add_modifier(mod)
    
    assert pc.strength_mod.total == 2
    
    # Advance 6 seconds, target initiative 10 (Different turn). Shouldn't expire yet.
    EventBus.dispatch(GameEvent(event_type="AdvanceTime", source_uuid=pc.entity_uuid, payload={"seconds_advanced": 6, "target_initiative": 10}))
    assert pc.strength_mod.total == 2
    
    # Advance 6 more seconds, landing back on Initiative 15. Must expire now.
    EventBus.dispatch(GameEvent(event_type="AdvanceTime", source_uuid=pc.entity_uuid, payload={"seconds_advanced": 6, "target_initiative": 15}))
    assert pc.strength_mod.total == 0

def test_reaction_limit_enforcement():
    """Tests that reactions (like Shield) can only trigger once per turn cycle."""
    wizard = Creature(name="Wizard", tags=["can_cast_shield"], hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    attacker = Creature(name="Orc", hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    sword = MeleeWeapon(name="Sword", damage_dice="1d8", damage_type="slashing")
    attacker.equipped_weapon_uuid = sword.entity_uuid
    
    spatial_service.sync_entity(wizard)
    spatial_service.sync_entity(attacker)
    
    with patch('random.randint', return_value=10):
        # Attack 1: Triggers reaction. Base AC 10 -> 15.
        EventBus.dispatch(GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=wizard.entity_uuid))
        
        assert wizard.reaction_used is True
        assert wizard.ac.total == 15
        assert len(wizard.ac.modifiers) == 1 # Shield applied
        
        # Attack 2: Wizard has no reactions left. Should NOT cast shield again.
        EventBus.dispatch(GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=wizard.entity_uuid))
        
        assert wizard.ac.total == 15 # Still 15, not 20
        assert len(wizard.ac.modifiers) == 1

# ==========================================
# 3. COMBAT MECHANICS & FEATS
# ==========================================

def test_concentration_auto_drop_on_zero_hp():
    """Tests that falling unconscious automatically breaks concentration."""
    pc = Creature(name="Cleric", hp=ModifiableValue(base_value=5), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0), concentrating_on="Bane")
    attacker = Creature(name="Orc", hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    with patch.object(EventBus, 'dispatch', wraps=EventBus.dispatch) as mock_dispatch:
        # Deal 10 damage to the PC (reducing HP to 0)
        dmg_event = GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=pc.entity_uuid, payload={"hit": True, "damage": 10, "damage_type": "slashing"})
        dmg_event.status = 3 # Bypass pre/exec straight to Apply Damage Post Event
        EventBus._notify(dmg_event)
        
        assert pc.hp.base_value == -5
        # Assert a DropConcentration event was dispatched automatically
        drop_events = [call_args.args[0] for call_args in mock_dispatch.call_args_list if call_args.args[0].event_type == "DropConcentration"]
        assert len(drop_events) == 1
        assert drop_events[0].source_uuid == pc.entity_uuid

def test_ranged_attack_disadvantage_proximity():
    """Tests shooting a bow with an enemy at 5ft forces disadvantage."""
    archer = Creature(name="Archer", tags=["pc"], x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=3))
    bow = RangedWeapon(name="Longbow", damage_dice="1d8", damage_type="piercing", normal_range=150, long_range=600)
    archer.equipped_weapon_uuid = bow.entity_uuid
    
    target = Creature(name="Far Target", tags=["monster"], x=30, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    adjacent_enemy = Creature(name="Melee Thug", tags=["monster"], x=5, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    spatial_service.sync_entity(archer)
    spatial_service.sync_entity(target)
    spatial_service.sync_entity(adjacent_enemy)
    
    attack_event = GameEvent(event_type="MeleeAttack", source_uuid=archer.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(attack_event)
    
    # Because Melee Thug is hostile and at 5ft, the payload should get tagged with disadvantage
    assert attack_event.payload.get("disadvantage") is True

def test_opportunity_attack_bypass():
    """Tests that teleporting out of reach bypasses Opportunity Attack triggers."""
    pc = Creature(name="PC", tags=["pc"], x=0, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    enemy = Creature(name="Monster", tags=["monster"], x=5, y=0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    spatial_service.sync_entity(pc)
    spatial_service.sync_entity(enemy)
    
    # Standard walking 15ft away should trigger OA alert
    walk_event = GameEvent(event_type="Movement", source_uuid=pc.entity_uuid, payload={"target_x": 15, "target_y": 0, "target_z": 0, "movement_type": "walk"})
    EventBus.dispatch(walk_event)
    assert "Monster" in walk_event.payload.get("opportunity_attackers", [])
    
    # Teleporting the same distance natively prevents the trigger
    tp_event = GameEvent(event_type="Movement", source_uuid=pc.entity_uuid, payload={"target_x": 15, "target_y": 0, "target_z": 0, "movement_type": "teleport"})
    EventBus.dispatch(tp_event)
    assert "opportunity_attackers" not in tp_event.payload