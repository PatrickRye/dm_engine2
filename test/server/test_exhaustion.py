"""
Tests for exhaustion rules: REQ-EXH-001, REQ-EXH-002.
"""
import pytest
from dnd_rules_engine import (
    Creature, ModifiableValue, GameEvent, EventBus, ActiveCondition,
)
from registry import register_entity, clear_registry
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _make_creature(vault_path, exhaustion=0):
    c = Creature(
        name="Fighter",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
        constitution_mod=ModifiableValue(base_value=1),
        exhaustion_level=exhaustion,
    )
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


def test_req_exh_001_penalty_applied_to_d20_saves(setup):
    """REQ-EXH-001: Exhaustion level subtracts (level * 2) from d20 tests (saves checked in SpellCast)."""
    vault_path = setup

    target = Creature(
        name="Target",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        exhaustion_level=2,  # -4 penalty
    )
    register_entity(target)
    spatial_service.sync_entity(target)

    from spell_system import SpellMechanics
    # DC 10 con save; exhausted target has -4, so roll 12 + (-4) = 8 < 10 → fails
    mech = SpellMechanics(
        damage_dice="1d4",
        damage_type="thunder",
        save_required="constitution",
        half_damage_on_save=True,
    )
    with pytest.MonkeyPatch().context() as mp:
        import random
        mp.setattr(random, "randint", lambda a, b: 12)  # roll 12, with -4 penalty = 8 → fail
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=target.entity_uuid,
            vault_path=vault_path,
            payload={
                "ability_name": "Thunderwave",
                "mechanics": mech,
                "target_uuids": [target.entity_uuid],
            },
        )
        EventBus.dispatch(event)

    results_text = " ".join(event.payload.get("results", []))
    assert "Failed Save" in results_text or "failed" in results_text.lower()


def test_req_exh_002_exhaustion_6_causes_death(setup, mock_dice):
    """REQ-EXH-002: Exhaustion level 6 → entity dies at start of their turn."""
    vault_path = setup
    c = _make_creature(vault_path, exhaustion=6)

    with mock_dice(15):  # dice value doesn't matter for this test
        event = GameEvent(
            event_type="StartOfTurn",
            source_uuid=c.entity_uuid,
            vault_path=vault_path,
        )
        EventBus.dispatch(event)

    assert any(cond.name == "Dead" for cond in c.active_conditions)
    assert not any(cond.name == "Dying" for cond in c.active_conditions)


def test_req_exh_002_exhaustion_5_does_not_kill(setup, mock_dice):
    """REQ-EXH-002: Exhaustion level 5 does NOT cause death."""
    vault_path = setup
    c = _make_creature(vault_path, exhaustion=5)

    with mock_dice(15):
        event = GameEvent(
            event_type="StartOfTurn",
            source_uuid=c.entity_uuid,
            vault_path=vault_path,
        )
        EventBus.dispatch(event)

    assert not any(cond.name == "Dead" for cond in c.active_conditions)
