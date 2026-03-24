"""
Long rest tests.
REQ-RST-004: Long Rest — restores all HP, resets class daily resources,
             and recovers Hit Dice up to half the entity's maximum (minimum 1).
"""
import pytest

from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus
from registry import clear_registry, register_entity
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _dispatch_long_rest(entity, vault_path):
    event = GameEvent(
        event_type="Rest",
        source_uuid=entity.entity_uuid,
        vault_path=vault_path,
        payload={
            "rest_type": "long",
            "target_uuids": [entity.entity_uuid],
            "hit_dice_to_spend": 0,
        },
    )
    EventBus.dispatch(event)


# ============================================================
# REQ-RST-004: Long rest recovers HP
# ============================================================

def test_req_rst_004_long_rest_restores_hp(setup):
    """REQ-RST-004: Long rest restores all HP."""
    vp = setup
    entity = Creature(
        name="Fighter",
        vault_path=vp,
        hp=ModifiableValue(base_value=30),
        max_hp=60,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    entity.hp.base_value = 10  # damaged
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    assert entity.hp.base_value == 60


def test_req_rst_004_long_rest_resets_daily_resources(setup):
    """REQ-RST-004: Long rest resets all non-hit-dice resources to their maximum."""
    vp = setup
    entity = Creature(
        name="Wizard",
        vault_path=vp,
        hp=ModifiableValue(base_value=40),
        max_hp=40,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Spell Slots (1st)": "1/4",  # partially spent
            "Spell Slots (2nd)": "0/3",  # fully spent
            "Arcane Recovery": "0/1",
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    assert entity.resources["Spell Slots (1st)"] == "4/4"
    assert entity.resources["Spell Slots (2nd)"] == "3/3"
    assert entity.resources["Arcane Recovery"] == "1/1"


def test_req_rst_004_long_rest_recovers_hit_dice_up_to_half_max(setup):
    """REQ-RST-004: Long rest recovers Hit Dice up to half the entity's maximum (min 1)."""
    vp = setup
    entity = Creature(
        name="Barbarian",
        vault_path=vp,
        hp=ModifiableValue(base_value=80),
        max_hp=80,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Hit Dice (d12)": "2/8",  # spent 6, have 2 of 8
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    # Recover max(1, 8//2) = 4 dice. New total = min(8, 2+4) = 6.
    assert entity.resources["Hit Dice (d12)"] == "6/8"


def test_req_rst_004_long_rest_hit_dice_capped_at_max(setup):
    """REQ-RST-004: Long rest does not exceed max Hit Dice."""
    vp = setup
    entity = Creature(
        name="Rogue",
        vault_path=vp,
        hp=ModifiableValue(base_value=50),
        max_hp=50,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Hit Dice (d8)": "5/6",  # only 1 spent, recovery would be 3 but capped at 6
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    # Recover max(1, 6//2) = 3. New total = min(6, 5+3) = 6.
    assert entity.resources["Hit Dice (d8)"] == "6/6"


def test_req_rst_004_long_rest_minimum_one_hit_die_recovered(setup):
    """REQ-RST-004: Long rest always recovers at least 1 Hit Die even at level 1."""
    vp = setup
    entity = Creature(
        name="Peasant",
        vault_path=vp,
        hp=ModifiableValue(base_value=8),
        max_hp=8,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Hit Dice (d8)": "0/1",  # 1 die total, 0 remaining
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    # max(1, 1//2) = max(1, 0) = 1. New total = min(1, 0+1) = 1.
    assert entity.resources["Hit Dice (d8)"] == "1/1"


# ============================================================
# REQ-RST-002: Long Rest Interruption
# ============================================================

def test_req_rst_002_interrupted_long_rest_grants_no_benefits(setup):
    """
    REQ-RST-002: An interrupted long rest (1+ hours of strenuous activity)
    grants no HP or resource benefits.
    """
    vp = setup
    entity = Creature(
        name="Fighter",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        max_hp=50,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={"Spell Slots (1st)": "0/4"},
    )
    entity.hp.base_value = 15  # damaged
    register_entity(entity)

    # Mark rest in progress
    entity.rest_in_progress = True
    entity.rest_type = "long"
    entity.rest_start_day = 1
    entity.rest_start_hour = 8
    entity.rest_interrupted = True  # DM called interrupt_rest

    _dispatch_long_rest(entity, vp)

    # HP unchanged (was 15, still 15 — no healing)
    assert entity.hp.base_value == 15
    # Spell slots unchanged
    assert entity.resources["Spell Slots (1st)"] == "0/4"
    # Rest flags cleared
    assert entity.rest_in_progress is False
    assert entity.rest_type == ""


def test_req_rst_002_interrupted_short_rest_grants_no_benefits(setup):
    """
    REQ-RST-002: An interrupted short rest grants no HP or resource benefits.
    """
    vp = setup
    entity = Creature(
        name="Warlock",
        vault_path=vp,
        hp=ModifiableValue(base_value=10),
        max_hp=30,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Pact Magic Slots (1st) [SR]": "0/2",
            "Hit Dice (d8)": "1/5",
        },
    )
    entity.hp.base_value = 5
    register_entity(entity)

    entity.rest_in_progress = True
    entity.rest_type = "short"
    entity.rest_start_day = 1
    entity.rest_start_hour = 8
    entity.rest_interrupted = True

    event = GameEvent(
        event_type="Rest",
        source_uuid=entity.entity_uuid,
        vault_path=vp,
        payload={
            "rest_type": "short",
            "target_uuids": [entity.entity_uuid],
            "hit_dice_to_spend": 1,
            "rest_start_day": 1,
            "rest_start_hour": 8,
        },
    )
    EventBus.dispatch(event)

    # No HP healed
    assert entity.hp.base_value == 5
    # No resource restored
    assert entity.resources["Pact Magic Slots (1st) [SR]"] == "0/2"
    # No hit dice spent
    assert entity.resources["Hit Dice (d8)"] == "1/5"
    # Flags cleared
    assert entity.rest_in_progress is False


# ============================================================
# REQ-RST-003: Long Rest Frequency (24 hours)
# ============================================================

def _dispatch_long_rest_with_time(entity, vault_path, rest_start_day, rest_start_hour):
    """Helper that dispatches a long rest with explicit start time."""
    event = GameEvent(
        event_type="Rest",
        source_uuid=entity.entity_uuid,
        vault_path=vault_path,
        payload={
            "rest_type": "long",
            "target_uuids": [entity.entity_uuid],
            "hit_dice_to_spend": 0,
            "rest_start_day": rest_start_day,
            "rest_start_hour": rest_start_hour,
        },
    )
    EventBus.dispatch(event)


def test_req_rst_003_second_long_rest_within_24_hours_rejected(setup):
    """
    REQ-RST-003: Cannot benefit from more than one Long Rest in a 24-hour period.
    Second long rest within 24 hours grants no benefits.
    """
    vp = setup
    entity = Creature(
        name="Rogue",
        vault_path=vp,
        hp=ModifiableValue(base_value=10),
        max_hp=40,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    entity.hp.base_value = 15
    register_entity(entity)

    # First long rest: day 1, hour 8
    entity.last_long_rest_day = 1
    entity.last_long_rest_hour = 8

    # Second long rest attempted: day 1, hour 20 (12 hours later — within 24h)
    entity.hp.base_value = 20  # fresh damage
    _dispatch_long_rest_with_time(entity, vp, rest_start_day=1, rest_start_hour=20)

    # No HP restored — rejected due to 24h frequency
    assert entity.hp.base_value == 20
    # Flags cleared
    assert entity.rest_in_progress is False
    assert entity.rest_type == ""


def test_req_rst_003_exactly_24_hours_between_long_rests_allowed(setup):
    """
    REQ-RST-003: Exactly 24 hours between long rests is permitted (hours_since >= 24).
    """
    vp = setup
    entity = Creature(
        name="Cleric",
        vault_path=vp,
        hp=ModifiableValue(base_value=10),
        max_hp=50,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    entity.hp.base_value = 20
    register_entity(entity)

    # First long rest: day 1, hour 8
    entity.last_long_rest_day = 1
    entity.last_long_rest_hour = 8

    # Second long rest: day 2, hour 8 (exactly 24 hours later)
    entity.hp.base_value = 25
    _dispatch_long_rest_with_time(entity, vp, rest_start_day=2, rest_start_hour=8)

    # HP fully restored
    assert entity.hp.base_value == 50


def test_req_rst_003_more_than_24_hours_between_long_rests_allowed(setup):
    """
    REQ-RST-003: More than 24 hours between long rests is permitted.
    """
    vp = setup
    entity = Creature(
        name="Paladin",
        vault_path=vp,
        hp=ModifiableValue(base_value=10),
        max_hp=60,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    entity.hp.base_value = 30
    register_entity(entity)

    # First long rest: day 1, hour 8
    entity.last_long_rest_day = 1
    entity.last_long_rest_hour = 8

    # Second long rest: day 3, hour 9 (more than 24h)
    entity.hp.base_value = 40
    _dispatch_long_rest_with_time(entity, vp, rest_start_day=3, rest_start_hour=9)

    # HP fully restored
    assert entity.hp.base_value == 60
    # 24h tracking updated
    assert entity.last_long_rest_day == 3
    assert entity.last_long_rest_hour == 9
