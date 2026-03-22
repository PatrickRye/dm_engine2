"""
Tests for death and dying rules: REQ-DTH-001 through REQ-DTH-006.
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


def _make_pair(vault_path, attacker_hp=20, target_hp=10, target_max_hp=10):
    attacker = Creature(
        name="Attacker",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=attacker_hp),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Target",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=target_hp),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        max_hp=target_max_hp,
    )
    register_entity(attacker)
    register_entity(target)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    return attacker, target


def _apply_damage(vault_path, target, damage, damage_type="slashing", critical=False):
    event = GameEvent(
        event_type="ApplyDamage",
        source_uuid=target.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={"damage": damage, "damage_type": damage_type, "critical": critical},
    )
    EventBus.dispatch(event)
    return event


def test_req_dth_001_massive_damage_instant_death(setup):
    """REQ-DTH-001: Single hit bringing HP from positive to <= -max_hp → instant death."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=10)

    # 10 HP, max_hp 10 → instant death threshold: damage >= 10 + 10 = 20
    _apply_damage(vault_path, target, 20)
    assert any(c.name == "Dead" for c in target.active_conditions)
    assert not any(c.name == "Dying" for c in target.active_conditions)


def test_req_dth_001_massive_damage_from_full(setup):
    """REQ-DTH-001: Single hit reducing HP by more than max_hp from positive HP → instant death."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=10)

    # Single hit: 10 HP remaining, 20+ damage → (10 - 21) = -11 <= -10 max_hp
    _apply_damage(vault_path, target, 21)
    assert any(c.name == "Dead" for c in target.active_conditions)
    assert not any(c.name == "Dying" for c in target.active_conditions)


def test_req_dth_002_fall_to_zero_gains_dying(setup):
    """REQ-DTH-002: Non-instant-death fall to 0 HP → Dying + Unconscious."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)

    _apply_damage(vault_path, target, 10)
    assert target.hp.base_value == 0
    assert any(c.name == "Dying" for c in target.active_conditions)
    assert any(c.name == "Unconscious" for c in target.active_conditions)
    assert not any(c.name == "Dead" for c in target.active_conditions)


def test_req_dth_003_death_save_success(setup, mock_dice):
    """REQ-DTH-003: Roll ≥10 on death save = 1 success; 3 successes = Stable."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)
    _apply_damage(vault_path, target, 10)

    # Three successes → Stable
    for _ in range(3):
        with mock_dice(15):
            event = GameEvent(
                event_type="StartOfTurn",
                source_uuid=target.entity_uuid,
                vault_path=vault_path,
            )
            EventBus.dispatch(event)

    assert any(c.name == "Stable" for c in target.active_conditions)
    assert not any(c.name == "Dying" for c in target.active_conditions)
    assert target.death_saves_successes == 0  # reset after stabilizing


def test_req_dth_003_death_save_failure(setup, mock_dice):
    """REQ-DTH-003: Roll <10 on death save = 1 failure; 3 failures = Dead."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)
    _apply_damage(vault_path, target, 10)

    for _ in range(3):
        with mock_dice(5):
            event = GameEvent(
                event_type="StartOfTurn",
                source_uuid=target.entity_uuid,
                vault_path=vault_path,
            )
            EventBus.dispatch(event)

    assert any(c.name == "Dead" for c in target.active_conditions)
    assert not any(c.name == "Dying" for c in target.active_conditions)


def test_req_dth_004_natural_1_counts_two_failures(setup, mock_dice):
    """REQ-DTH-004: Rolling a natural 1 on death save = 2 failures."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)
    _apply_damage(vault_path, target, 10)
    assert target.death_saves_failures == 0

    with mock_dice(1):
        event = GameEvent(
            event_type="StartOfTurn",
            source_uuid=target.entity_uuid,
            vault_path=vault_path,
        )
        EventBus.dispatch(event)

    assert target.death_saves_failures == 2


def test_req_dth_004_natural_20_restores_1hp(setup, mock_dice):
    """REQ-DTH-004: Rolling a natural 20 on death save → regain 1 HP, remove Dying."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)
    _apply_damage(vault_path, target, 10)

    with mock_dice(20):
        event = GameEvent(
            event_type="StartOfTurn",
            source_uuid=target.entity_uuid,
            vault_path=vault_path,
        )
        EventBus.dispatch(event)

    assert target.hp.base_value == 1
    assert not any(c.name == "Dying" for c in target.active_conditions)
    assert not any(c.name == "Unconscious" for c in target.active_conditions)


def test_req_dth_005_damage_at_zero_hp_adds_failure(setup):
    """REQ-DTH-005: Taking damage at 0 HP adds 1 death save failure."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)
    _apply_damage(vault_path, target, 10)  # drop to 0

    initial_fails = target.death_saves_failures
    _apply_damage(vault_path, target, 1)
    assert target.death_saves_failures == initial_fails + 1


def test_req_dth_005_critical_at_zero_hp_adds_two_failures(setup):
    """REQ-DTH-005: Critical hit at 0 HP adds 2 death save failures."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)
    _apply_damage(vault_path, target, 10)  # drop to 0

    initial_fails = target.death_saves_failures
    _apply_damage(vault_path, target, 1, critical=True)
    assert target.death_saves_failures == initial_fails + 2


def test_req_dth_006_healing_at_zero_removes_dying(setup):
    """REQ-DTH-006: Healing at 0 HP removes Dying/Stable/Unconscious and resets counters."""
    vault_path = setup
    _, target = _make_pair(vault_path, target_hp=10, target_max_hp=20)
    _apply_damage(vault_path, target, 10)

    # Accumulate some death save failures
    target.death_saves_failures = 2
    target.death_saves_successes = 1

    # Heal via SpellCast / healing_dice mechanic (direct model edit is sufficient to test condition removal)
    from spell_system import SpellMechanics
    from compendium_manager import MechanicEffect

    mech_obj = SpellMechanics(healing_dice="1d4")
    heal_event = GameEvent(
        event_type="SpellCast",
        source_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={
            "ability_name": "Cure Wounds",
            "mechanics": mech_obj,
            "target_uuids": [target.entity_uuid],
        },
    )
    EventBus.dispatch(heal_event)

    assert target.hp.base_value > 0
    assert not any(c.name == "Dying" for c in target.active_conditions)
    assert not any(c.name == "Stable" for c in target.active_conditions)
    assert not any(c.name == "Unconscious" and c.source_name == "0 HP" for c in target.active_conditions)
    assert target.death_saves_successes == 0
    assert target.death_saves_failures == 0
