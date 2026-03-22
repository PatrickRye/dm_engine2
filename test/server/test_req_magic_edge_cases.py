import os
import pytest
from dnd_rules_engine import Creature, ModifiableValue
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import modify_health


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_edg_003_instant_death_hp_query(setup):
    """
    Trace: REQ-EDG-003
    Validates that spells like Power Word Kill query true HP (base_value), strictly ignoring
    any Temporary Hit Points (THP) buffer the entity might have.
    """
    vp = setup
    target = Creature(
        name="Target",
        vault_path=vp,
        hp=ModifiableValue(base_value=90),
        max_hp=150,
        temp_hp=20,  # 90 base + 20 THP = 110 total, but base is under 100!
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    config = {"configurable": {"thread_id": vp}}
    res = await modify_health.ainvoke(
        {"target_name": "Target", "hp_change": 0, "reason": "Power Word Kill", "instant_death_threshold": 100}, config=config
    )

    assert "instantly killed" in res
    assert target.hp.base_value == 0
    assert any(c.name == "Dead" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_req_edg_004_instant_death_disintegrate(setup):
    """
    Trace: REQ-EDG-004
    Validates that Disintegrate evaluates HP after damage is dealt; if HP reaches 0, the entity
    turns to dust (state.dust=True) and bypasses the standard Dying state.
    """
    vp = setup
    target = Creature(
        name="Target",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        max_hp=100,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    config = {"configurable": {"thread_id": vp}}
    res = await modify_health.ainvoke(
        {"target_name": "Target", "hp_change": -30, "reason": "Disintegrate", "disintegrate_if_zero": True}, config=config
    )

    assert "DUST" in res
    assert target.hp.base_value == 0
    assert any(c.name == "Dead" for c in target.active_conditions)
    assert any(c.name == "Dust" for c in target.active_conditions)
    assert not any(c.name == "Dying" for c in target.active_conditions)


@pytest.mark.skip(reason="Pending implementation of Target Invalidation logic in the rules engine")
def test_req_spl_006_target_invalidation():
    """
    Trace: REQ-SPL-006
    Validates that if a target becomes invalid (e.g., dies or moves out of range) between
    casting and resolution, the spell fails on that target but the spell slot is still expended.
    """
    pass
