import os
import pytest
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition, ClassLevel
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import execute_grapple_or_shove


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_skl_009_grapple_shove_escape(setup):
    """
    Trace: REQ-SKL-009
    Validates the 2024 update: escaping a grapple or shove uses an Acrobatics or Athletics check
    against the grappler's static Escape DC (8 + STR mod + PB), NOT a contested roll.
    """
    vp = setup
    grappler = Creature(
        name="Grappler",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=4),  # STR +4
        dexterity_mod=ModifiableValue(base_value=0),
    )
    # Level 1 -> PB +2. DC should be 8 + 4 + 2 = 14
    grappler.classes.append(ClassLevel(class_name="Fighter", level=1))

    escaper = Creature(
        name="Escaper",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),  # DEX +2
        active_conditions=[ActiveCondition(name="Grappled", source_uuid=grappler.entity_uuid)],
    )

    register_entity(grappler)
    register_entity(escaper)

    config = {"configurable": {"thread_id": vp}}

    # Test Failure (Roll 5 + 2 = 7 < DC 14)
    res_fail = await execute_grapple_or_shove.ainvoke(
        {"attacker_name": "Escaper", "target_name": "Grappler", "action_type": "escape", "manual_roll_total": 7}, config=config
    )

    assert "Failure." in res_fail
    assert "Grapple Escape DC 14" in res_fail
    assert any(c.name == "Grappled" for c in escaper.active_conditions)

    # Test Success (Roll 15 + 2 = 17 >= DC 14)
    res_succ = await execute_grapple_or_shove.ainvoke(
        {"attacker_name": "Escaper", "target_name": "Grappler", "action_type": "escape", "manual_roll_total": 17},
        config=config,
    )

    assert "Success!" in res_succ
    assert not any(c.name == "Grappled" for c in escaper.active_conditions)
