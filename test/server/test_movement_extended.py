"""
Extended movement, object, and environment requirement tests.
REQ-MOV-005, REQ-OBJ-001, REQ-OBJ-003, REQ-OBJ-004
"""
import pytest
from unittest.mock import patch

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
    EventStatus,
)
from spatial_engine import spatial_service, Wall
from registry import clear_registry, register_entity
from spell_system import SpellMechanics


@pytest.fixture(autouse=True)
def setup_system():
    """Clear registries and spatial indexes before each test."""
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


def _make_creature(name="Fighter", movement_remaining=30, speed=30, tags=None):
    c = Creature(
        name=name,
        x=0.0,
        y=0.0,
        size=5.0,
        speed=speed,
        movement_remaining=movement_remaining,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=1),
        tags=tags or [],
    )
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


# ============================================================
# REQ-MOV-005: Climbing / Swimming / Crawling extra cost
# ============================================================

def test_req_mov_005_climbing_costs_double_movement():
    """
    REQ-MOV-005: Climbing costs 1 extra foot per foot moved (2x total).
    An entity with 15ft remaining cannot climb 10ft (would cost 20ft).
    """
    entity = _make_creature(movement_remaining=15)

    # Fire a Movement pre-event directly (simulates what move_entity does)
    event = GameEvent(
        event_type="Movement",
        source_uuid=entity.entity_uuid,
        payload={
            "target_x": 10.0,
            "target_y": 0.0,
            "target_z": 0.0,
            "movement_type": "climb",
            "ignore_budget": False,  # Enforce budget check
        },
    )
    EventBus.dispatch(event)

    assert event.status == EventStatus.CANCELLED, (
        "Climbing 10ft should require 20ft movement, exceeding the 15ft remaining"
    )


def test_req_mov_005_climbing_succeeds_with_enough_movement():
    """
    REQ-MOV-005: Climbing 10ft costs 20ft movement. Entity with 25ft can climb 10ft.
    """
    entity = _make_creature(movement_remaining=25)

    event = GameEvent(
        event_type="Movement",
        source_uuid=entity.entity_uuid,
        payload={
            "target_x": 10.0,
            "target_y": 0.0,
            "target_z": 0.0,
            "movement_type": "climb",
            "ignore_budget": False,
        },
    )
    EventBus.dispatch(event)

    assert event.status != EventStatus.CANCELLED, (
        "Entity with 25ft remaining should be able to climb 10ft (costs 20ft)"
    )
    assert event.payload.get("cost") == 20


def test_req_mov_005_native_climb_speed_no_extra_cost():
    """
    REQ-MOV-005: Entity with native climb_speed tag does NOT pay extra for climbing.
    Climbing 10ft costs only 10ft for such an entity.
    """
    entity = _make_creature(movement_remaining=15, tags=["climb_speed"])

    event = GameEvent(
        event_type="Movement",
        source_uuid=entity.entity_uuid,
        payload={
            "target_x": 10.0,
            "target_y": 0.0,
            "target_z": 0.0,
            "movement_type": "climb",
            "ignore_budget": False,
        },
    )
    EventBus.dispatch(event)

    # 10ft normal cost for native climber — should succeed with 15ft remaining
    assert event.status != EventStatus.CANCELLED, (
        "An entity with climb_speed should pay normal cost (10ft) not double (20ft)"
    )
    assert event.payload.get("cost") == 10


def test_req_mov_005_native_swim_speed_no_extra_cost():
    """
    REQ-MOV-005: Entity with native swim_speed tag does NOT pay extra for swimming.
    """
    entity = _make_creature(movement_remaining=15, tags=["swim_speed"])

    event = GameEvent(
        event_type="Movement",
        source_uuid=entity.entity_uuid,
        payload={
            "target_x": 10.0,
            "target_y": 0.0,
            "target_z": 0.0,
            "movement_type": "swim",
            "ignore_budget": False,
        },
    )
    EventBus.dispatch(event)

    assert event.status != EventStatus.CANCELLED
    assert event.payload.get("cost") == 10


def test_req_mov_005_crawl_always_costs_double():
    """
    REQ-MOV-005: Crawling always costs 1 extra foot (no native crawl speed exemption).
    """
    entity = _make_creature(movement_remaining=15)

    event = GameEvent(
        event_type="Movement",
        source_uuid=entity.entity_uuid,
        payload={
            "target_x": 10.0,
            "target_y": 0.0,
            "target_z": 0.0,
            "movement_type": "crawl",
            "ignore_budget": False,
        },
    )
    EventBus.dispatch(event)

    assert event.status == EventStatus.CANCELLED, (
        "Crawling 10ft should cost 20ft, cancelling with only 15ft remaining"
    )


# ============================================================
# REQ-OBJ-001: Object AC by material
# ============================================================

def test_req_obj_001_wall_ac_field_exists():
    """
    REQ-OBJ-001: Object AC is determined by material composition.
    The Wall model supports an `ac` field that the DM sets per material:
    Wood=15, Stone=17, Iron=19, etc.
    """
    wood_wall = Wall(start=(0, 0), end=(10, 0), ac=15, hp=30, max_hp=30)
    stone_wall = Wall(start=(0, 0), end=(10, 0), ac=17, hp=50, max_hp=50)
    iron_wall = Wall(start=(0, 0), end=(10, 0), ac=19, hp=80, max_hp=80)

    assert wood_wall.ac == 15
    assert stone_wall.ac == 17
    assert iron_wall.ac == 19


# ============================================================
# REQ-OBJ-003: Damage Threshold
# ============================================================

def _make_caster_and_dispatch_spell_to_wall(wall, damage_dice, damage_type, vault_path="default"):
    """Helper: dispatch a SpellCast event targeting a wall, return the event."""
    caster = Creature(
        name="Caster",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    spatial_service.add_wall(wall, vault_path=vault_path)

    mechanics = SpellMechanics(damage_dice=damage_dice, damage_type=damage_type)
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        vault_path=vault_path,
        payload={
            "mechanics": mechanics.model_dump(),
            "target_uuids": [],
            "target_wall_ids": [wall.wall_id],
        },
    )
    with patch("event_handlers.roll_dice", return_value=3):
        EventBus.dispatch(event)
    return event


def test_req_obj_003_damage_below_threshold_is_ignored():
    """
    REQ-OBJ-003: Damage Threshold
    If incoming damage does not meet or exceed the threshold, it is reduced to 0.
    """
    wall = Wall(start=(0, 0), end=(10, 0), hp=30, max_hp=30, damage_threshold=5)

    # Roll 3 damage, threshold is 5 → applied_damage = 0 → hp stays 30
    _make_caster_and_dispatch_spell_to_wall(wall, damage_dice="1d6", damage_type="bludgeoning")

    assert wall.hp == 30, f"Damage below threshold should be ignored; hp={wall.hp}"


def test_req_obj_003_damage_meeting_threshold_is_applied():
    """
    REQ-OBJ-003: Damage Threshold
    If incoming damage meets or exceeds the threshold, full damage is applied.
    """
    wall = Wall(start=(0, 0), end=(10, 0), hp=30, max_hp=30, damage_threshold=3)

    # Roll exactly 3 damage, threshold is 3 → applied_damage = 3 → hp = 27
    _make_caster_and_dispatch_spell_to_wall(wall, damage_dice="1d6", damage_type="bludgeoning")

    assert wall.hp == 27, f"Damage meeting threshold should be applied in full; hp={wall.hp}"


# ============================================================
# REQ-OBJ-004: Object Immunities (Poison, Psychic)
# ============================================================

def test_req_obj_004_wall_immune_to_poison_damage():
    """
    REQ-OBJ-004: Objects are immune to Poison damage.
    Poison damage should never be applied to wall objects.
    """
    wall = Wall(start=(0, 0), end=(10, 0), hp=30, max_hp=30, damage_threshold=0)
    _make_caster_and_dispatch_spell_to_wall(wall, damage_dice="1d8", damage_type="poison")

    assert wall.hp == 30, "Poison damage must not be applied to wall objects"


def test_req_obj_004_wall_immune_to_psychic_damage():
    """
    REQ-OBJ-004: Objects are immune to Psychic damage.
    """
    wall = Wall(start=(0, 0), end=(10, 0), hp=30, max_hp=30, damage_threshold=0)
    _make_caster_and_dispatch_spell_to_wall(wall, damage_dice="1d8", damage_type="psychic")

    assert wall.hp == 30, "Psychic damage must not be applied to wall objects"


def test_req_obj_004_wall_takes_non_immune_damage():
    """
    REQ-OBJ-004: Objects ARE affected by non-immune damage types (e.g., fire, slashing).
    """
    wall = Wall(start=(0, 0), end=(10, 0), hp=30, max_hp=30, damage_threshold=0)
    _make_caster_and_dispatch_spell_to_wall(wall, damage_dice="1d8", damage_type="fire")

    assert wall.hp == 27, f"Fire damage should be applied to wall; hp={wall.hp}"
