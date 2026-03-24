"""
Travel rules tests.
REQ-TRV-001: Fast pace — 30 mi/day, -5 passive Perception
REQ-TRV-002: Normal pace — 24 mi/day
REQ-TRV-003: Slow pace — 18 mi/day, stealth permitted
REQ-TRV-004: Forced march — CON save DC 10+1/hr beyond 8h; fail = 1 Exhaustion
"""
import pytest
from unittest.mock import patch

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
)
from registry import clear_registry, register_entity
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield mock_obsidian_vault


# ============================================================
# REQ-TRV-002: Normal pace — 24 miles/day
# ============================================================

@pytest.mark.asyncio
async def test_req_trv_002_normal_pace_distance(setup):
    """REQ-TRV-002: Normal pace covers 24 miles in 8 hours."""
    from tools import travel

    entity = Creature(
        name="Ranger",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=20),
        max_hp=20,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
    )
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    with patch("random.randint", return_value=10):  # dummy — no forced march for 8h
        res = await travel.ainvoke(
            {"party_names": ["Ranger"], "pace": "normal", "hours_traveled": 8},
            config=config,
        )

    assert "24" in res or "24.0" in res
    assert "Ranger" in res


# ============================================================
# REQ-TRV-001: Fast pace — 30 mi/day, -5 passive Perception
# ============================================================

@pytest.mark.asyncio
async def test_req_trv_001_fast_pace_distance(setup):
    """REQ-TRV-001: Fast pace covers 30 miles in 8 hours."""
    from tools import travel

    entity = Creature(
        name="Scout",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=15),
        max_hp=15,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
    )
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    with patch("random.randint", return_value=10):
        res = await travel.ainvoke(
            {"party_names": ["Scout"], "pace": "fast", "hours_traveled": 8},
            config=config,
        )

    assert "30" in res or "30.0" in res
    assert "fast" in res.lower()
    assert "passive perception" in res.lower()


@pytest.mark.asyncio
async def test_req_trv_001_fast_pace_notes_passive_penalty(setup):
    """REQ-TRV-001: Fast pace explicitly notes the passive Perception penalty."""
    from tools import travel

    entity = Creature(
        name="Runner",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
    )
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    with patch("random.randint", return_value=10):
        res = await travel.ainvoke(
            {"party_names": ["Runner"], "pace": "fast", "hours_traveled": 8},
            config=config,
        )

    assert "-5" in res
    assert "passive perception" in res.lower()


# ============================================================
# REQ-TRV-003: Slow pace — 18 mi/day, stealth permitted
# ============================================================

@pytest.mark.asyncio
async def test_req_trv_003_slow_pace_distance(setup):
    """REQ-TRV-003: Slow pace covers 18 miles in 8 hours."""
    from tools import travel

    entity = Creature(
        name="Stealther",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
    )
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    with patch("random.randint", return_value=10):
        res = await travel.ainvoke(
            {"party_names": ["Stealther"], "pace": "slow", "hours_traveled": 8},
            config=config,
        )

    assert "18" in res or "18.0" in res
    assert "slow" in res.lower()
    assert "Stealth" in res


# ============================================================
# REQ-TRV-004: Forced March — CON save DC 10+1/hr beyond 8h
# ============================================================

@pytest.mark.asyncio
async def test_req_trv_004_no_forced_march_at_8_hours(setup):
    """REQ-TRV-004: No CON save required for exactly 8 hours of travel."""
    from tools import travel

    entity = Creature(
        name="Hiker",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
    )
    entity.exhaustion_level = 0
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    with patch("random.randint", return_value=10):
        res = await travel.ainvoke(
            {"party_names": ["Hiker"], "pace": "normal", "hours_traveled": 8},
            config=config,
        )

    # No forced march exhaustion
    assert entity.exhaustion_level == 0
    assert "Exhaustion" not in res


@pytest.mark.asyncio
async def test_req_trv_004_forced_march_success_no_exhaustion(setup):
    """REQ-TRV-004: Passing the CON save (DC 11) for 9 hours grants no exhaustion."""
    from tools import travel

    entity = Creature(
        name="Tough",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=3),  # +3 CON
    )
    entity.exhaustion_level = 0
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    # Roll 12 on d20: 12 + 3 = 15 >= DC 11 → success
    with patch("dnd_rules_engine.random.randint", return_value=12):
        res = await travel.ainvoke(
            {"party_names": ["Tough"], "pace": "normal", "hours_traveled": 9},
            config=config,
        )

    assert entity.exhaustion_level == 0
    assert "made the forced march" in res.lower() or "success" in res.lower()


@pytest.mark.asyncio
async def test_req_trv_004_forced_march_failure_causes_exhaustion(setup):
    """REQ-TRV-004: Failing the CON save (DC 11) for 9 hours adds 1 Exhaustion."""
    from tools import travel

    entity = Creature(
        name="Weak",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),  # +0 CON
    )
    entity.exhaustion_level = 0
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    # Roll 5 on d20: 5 + 0 = 5 < DC 11 → failure
    with patch("dnd_rules_engine.random.randint", return_value=5):
        res = await travel.ainvoke(
            {"party_names": ["Weak"], "pace": "normal", "hours_traveled": 9},
            config=config,
        )

    assert entity.exhaustion_level == 1
    assert "failed" in res.lower()
    assert "Exhaustion" in res


@pytest.mark.asyncio
async def test_req_trv_004_forced_march_dc_increases_with_hours(setup):
    """REQ-TRV-004: DC = 10 + (hours - 8). 10 hours → DC 12."""
    from tools import travel

    entity = Creature(
        name="Endurer",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=2),  # +2 CON
    )
    entity.exhaustion_level = 0
    register_entity(entity)
    spatial_service.sync_entity(entity)

    config = {"configurable": {"thread_id": setup}}

    # Roll 10: 10+2=12 vs DC 12 (for 10h-8=2 extra hours) → success (tie goes to success)
    with patch("dnd_rules_engine.random.randint", return_value=10):
        res = await travel.ainvoke(
            {"party_names": ["Endurer"], "pace": "normal", "hours_traveled": 10},
            config=config,
        )

    # 10+2 = 12, DC = 12 → pass (no exhaustion)
    assert entity.exhaustion_level == 0
    assert "DC 12" in res

    # Roll 9: 9+2=11 < DC 12 → fail
    entity.exhaustion_level = 0
    with patch("dnd_rules_engine.random.randint", return_value=9):
        res = await travel.ainvoke(
            {"party_names": ["Endurer"], "pace": "normal", "hours_traveled": 10},
            config=config,
        )
    assert entity.exhaustion_level == 1
    assert "DC 12" in res
    assert "failed" in res.lower()


@pytest.mark.asyncio
async def test_req_trv_004_multiple_party_members_separate_saves(setup):
    """REQ-TRV-004: Each party member makes their own forced march CON save."""
    from tools import travel

    tough = Creature(
        name="ToughGuy",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=5),  # +5 CON — will pass DC 11
    )
    weak = Creature(
        name="WeakGuy",
        vault_path=setup,
        x=0.0, y=0.0,
        hp=ModifiableValue(base_value=10),
        max_hp=10,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),  # +0 CON — will fail DC 11
    )
    tough.exhaustion_level = 0
    weak.exhaustion_level = 0
    register_entity(tough)
    register_entity(weak)
    spatial_service.sync_entity(tough)
    spatial_service.sync_entity(weak)

    config = {"configurable": {"thread_id": setup}}

    # ToughGuy passes (15 vs DC 11: 15+5=20 >= 11), WeakGuy fails (5 vs DC 11: 5+0=5 < 11)
    # Patch dnd_rules_engine.random.randint so roll_dice returns 15 (passes) for ToughGuy
    # and 5 (fails) for WeakGuy. Since randint is called per-entity, we use side_effect.
    with patch("dnd_rules_engine.random.randint", side_effect=[15, 5]):
        res = await travel.ainvoke(
            {"party_names": ["ToughGuy", "WeakGuy"], "pace": "normal", "hours_traveled": 9},
            config=config,
        )

    assert tough.exhaustion_level == 0
    assert weak.exhaustion_level == 1
    assert "ToughGuy" in res
    assert "WeakGuy" in res
