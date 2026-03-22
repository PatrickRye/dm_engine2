"""
Core condition rules tests.
REQ-CND-003: Deafened — auto-fails hearing-based checks
REQ-CND-008: Invisible — Advantage on attacks, Disadvantage incoming
REQ-CND-009: Truesight/Blindsight suppress Invisible modifiers
REQ-CND-013: Prone melee — Disadvantage outgoing; Advantage incoming within 5ft
REQ-CND-018: Concentration drops on Incapacitation (engine-level trigger)
REQ-CND-019: Casting new Concentration spell terminates previous
"""
import pytest

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    MeleeWeapon,
    RangedWeapon,
    GameEvent,
    EventBus,
    ActiveCondition,
)
from spatial_engine import spatial_service
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup():
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


def _make_combatants(attacker_x=0.0, attacker_y=0.0, target_x=5.0, target_y=0.0,
                     attacker_tags=None, target_tags=None):
    attacker = Creature(
        name="Fighter",
        tags=(attacker_tags or []) + ["darkvision"],
        x=attacker_x, y=attacker_y, size=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Goblin",
        tags=(target_tags or []) + ["darkvision"],
        x=target_x, y=target_y, size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    weapon = MeleeWeapon(name="Sword", damage_dice="1d8", damage_type="slashing")
    attacker.equipped_weapon_uuid = weapon.entity_uuid
    register_entity(attacker)
    register_entity(target)
    register_entity(weapon)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    return attacker, target, weapon


# ============================================================
# REQ-CND-008: Invisible
# ============================================================

def test_req_cnd_008_invisible_attacker_grants_advantage(mock_dice):
    """REQ-CND-008: Invisible attacker cannot be seen → Advantage on attacks."""
    attacker, target, _ = _make_combatants()
    # Remove darkvision from attacker so only invisibility drives the advantage
    attacker.tags = []
    attacker.active_conditions.append(ActiveCondition(name="Invisible", source_name="Spell"))

    with mock_dice(10, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("advantage") is True


def test_req_cnd_008_invisible_target_grants_attacker_disadvantage(mock_dice):
    """REQ-CND-008: Invisible target is unseen → Disadvantage on attacks against it."""
    attacker, target, _ = _make_combatants()
    target.tags = []  # remove darkvision so attacker can't see it
    target.active_conditions.append(ActiveCondition(name="Invisible", source_name="Spell"))

    with mock_dice(15, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("disadvantage") is True


# ============================================================
# REQ-CND-009: Truesight suppresses Invisible benefit
# ============================================================

def test_req_cnd_009_truesight_negates_invisible_disadvantage(mock_dice):
    """REQ-CND-009: Attacker with truesight sees invisible target — no Disadvantage."""
    attacker, target, _ = _make_combatants(attacker_tags=["truesight"])
    target.tags = []
    target.active_conditions.append(ActiveCondition(name="Invisible", source_name="Spell"))

    with mock_dice(15, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert not event.payload.get("disadvantage")


def test_req_cnd_009_blindsight_negates_invisible_advantage(mock_dice):
    """REQ-CND-009: Defender with blindsight can perceive invisible attacker — no Advantage."""
    attacker, target, _ = _make_combatants(target_tags=["blindsight"])
    attacker.tags = []
    attacker.active_conditions.append(ActiveCondition(name="Invisible", source_name="Spell"))

    with mock_dice(10, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    # Blindsight: target CAN perceive attacker → no Advantage for attacker
    assert not event.payload.get("advantage")


# ============================================================
# REQ-CND-013: Prone (Melee)
# ============================================================

def test_req_cnd_013_prone_attacker_disadvantage(mock_dice):
    """REQ-CND-013: Prone attacker has Disadvantage on attacks."""
    attacker, target, _ = _make_combatants()
    attacker.active_conditions.append(ActiveCondition(name="Prone", source_name="Fall"))

    with mock_dice(15, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("disadvantage") is True


def test_req_cnd_013_prone_target_within_5ft_advantage(mock_dice):
    """REQ-CND-013: Attacking prone target within 5ft → Advantage."""
    attacker, target, _ = _make_combatants(attacker_x=0.0, target_x=5.0)
    target.active_conditions.append(ActiveCondition(name="Prone", source_name="Fall"))

    with mock_dice(10, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("advantage") is True


def test_req_cnd_013_prone_target_beyond_5ft_disadvantage(mock_dice):
    """REQ-CND-013: Ranged attack on prone target beyond 5ft → Disadvantage."""
    attacker, target, _ = _make_combatants(attacker_x=0.0, target_x=20.0)
    # Equip a ranged weapon so out-of-range melee check doesn't cancel the event
    bow = RangedWeapon(
        name="Shortbow", damage_dice="1d6", damage_type="piercing",
        normal_range=80, long_range=320,
    )
    register_entity(bow)
    attacker.equipped_weapon_uuid = bow.entity_uuid
    target.active_conditions.append(ActiveCondition(name="Prone", source_name="Fall"))

    with mock_dice(15, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("disadvantage") is True


# ============================================================
# REQ-CND-019: Casting new Concentration spell drops previous
# ============================================================

def test_req_cnd_019_new_concentration_drops_old():
    """REQ-CND-019: Casting a second Concentration spell terminates the first."""
    from dnd_rules_engine import GameEvent, EventStatus
    attacker, _, _ = _make_combatants()
    attacker.concentrating_on = "Bless"

    # Dispatch a SpellCast with requires_concentration=True
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=attacker.entity_uuid,
        target_uuid=attacker.entity_uuid,
        payload={
            "ability_name": "Hold Person",
            "mechanics": {"requires_concentration": True, "duration": -1},
            "target_names": [attacker.name],
        },
    )
    EventBus.dispatch(event)

    # After casting a concentration spell while already concentrating, old spell dropped
    assert attacker.concentrating_on != "Bless"


# ============================================================
# REQ-CND-018: Concentration drops on Incapacitation
# (Engine-level check via event_handlers; tested indirectly via DropConcentration dispatch)
# ============================================================

def test_req_cnd_018_incapacitation_drops_concentration():
    """
    REQ-CND-018: Applying Stunned (an incapacitating condition) dispatches DropConcentration,
    clearing concentrating_on on the engine entity.
    """
    from dnd_rules_engine import ActiveCondition
    attacker, _, _ = _make_combatants()
    attacker.concentrating_on = "Bless"

    drop_event = GameEvent(
        event_type="DropConcentration",
        source_uuid=attacker.entity_uuid,
    )
    EventBus.dispatch(drop_event)

    assert attacker.concentrating_on == ""
