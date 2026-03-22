"""
Miscellaneous rules tests.
REQ-SPL-018: Evasion — on Dex save success: 0 dmg; fail: half dmg
REQ-MOV-006: Falling damage — 1d6 per 10ft, max 20d6
REQ-MOV-007: Falling (Prone) — take any falling damage → land Prone
"""
import pytest

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
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


def _make_creature(name="Hero", hp=30, tags=None):
    c = Creature(
        name=name,
        tags=tags or [],
        x=0.0, y=0.0, size=5.0,
        hp=ModifiableValue(base_value=hp),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=3),  # +3 DEX for evasion tests
    )
    register_entity(c)
    return c


# ============================================================
# REQ-SPL-018: Evasion
# ============================================================

def test_req_spl_018_evasion_success_zero_damage():
    """REQ-SPL-018: Evasion + save success → 0 damage."""
    hero = _make_creature(tags=["evasion"])

    event = GameEvent(
        event_type="SavingThrow",
        source_uuid=hero.entity_uuid,
        target_uuid=hero.entity_uuid,
        payload={
            "save_required": "dexterity",
            "is_success": True,
            "base_damage": 20,
            "final_damage": 20,
        },
    )
    EventBus.dispatch(event)

    assert event.payload.get("final_damage") == 0, (
        f"Expected 0 damage on evasion success, got {event.payload.get('final_damage')}"
    )


def test_req_spl_018_evasion_fail_half_damage():
    """REQ-SPL-018: Evasion + save failure → half damage."""
    hero = _make_creature(tags=["evasion"])

    event = GameEvent(
        event_type="SavingThrow",
        source_uuid=hero.entity_uuid,
        target_uuid=hero.entity_uuid,
        payload={
            "save_required": "dexterity",
            "is_success": False,
            "base_damage": 20,
            "final_damage": 20,
        },
    )
    EventBus.dispatch(event)

    assert event.payload.get("final_damage") == 10, (
        f"Expected 10 damage on evasion failure, got {event.payload.get('final_damage')}"
    )


def test_req_spl_018_no_evasion_unmodified():
    """REQ-SPL-018: Without evasion, save failure keeps full damage."""
    hero = _make_creature(tags=[])  # no evasion

    event = GameEvent(
        event_type="SavingThrow",
        source_uuid=hero.entity_uuid,
        target_uuid=hero.entity_uuid,
        payload={
            "save_required": "dexterity",
            "is_success": False,
            "base_damage": 20,
            "final_damage": 20,
        },
    )
    EventBus.dispatch(event)

    # No evasion handler should have modified it
    assert event.payload.get("final_damage") == 20
