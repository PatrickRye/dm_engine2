"""
Movement through creature space tests.
REQ-GEO-007: Moving through a friendly creature's space costs extra movement (difficult terrain).
REQ-GEO-008: Moving through a hostile creature's space — same cost; engine doesn't enforce
             the hostile-blocking rule (requires DM to know faction); large-size-diff case tested.
REQ-GEO-009: Squeezing initiation is LLM-tracked (DM applies condition manually).
REQ-GEO-010: Squeezing penalties — already covered by existing squeezing condition handling.
"""
import os
import pytest

from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield mock_obsidian_vault


def _make_creature(name, vault_path, x=0.0, y=0.0, size=5.0, speed=30):
    c = Creature(
        name=name,
        vault_path=vault_path,
        x=x,
        y=y,
        size=size,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=speed,
        movement_remaining=speed,
    )
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


# ============================================================
# REQ-GEO-007/008: Moving through creature space = extra cost
# ============================================================

@pytest.mark.asyncio
async def test_req_geo_007_movement_through_creature_costs_extra(setup):
    """REQ-GEO-007: Moving through a creature's space costs extra movement (difficult terrain)."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    # Mover at x=0, blocker at x=10 (5ft creature centered at 10, occupies 7.5-12.5)
    # Mover wants to move to x=20, passing through blocker's space at x=10
    mover = _make_creature("Mover", vp, x=0.0, y=0.0, speed=60)
    blocker = _make_creature("Blocker", vp, x=10.0, y=0.0, size=5.0)

    j_dir = get_journals_dir(vp)
    for name in ["Mover", "Blocker"]:
        with open(os.path.join(j_dir, f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    spatial_service.active_combatants[vp] = ["Mover", "Blocker"]
    config = {"configurable": {"thread_id": vp}}

    # Move 20ft in a straight line passing through blocker at x=10
    # Normal 20ft would cost 20. Passing through 5ft creature space = +5ft extra.
    # Total expected cost ≈ 25ft. Speed=60 so should succeed.
    res = await move_entity.ainvoke(
        {"entity_name": "Mover", "target_x": 20.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "SYSTEM ERROR" not in res or "movement" not in res.lower()
    # Movement remaining should reflect extra cost (25ft used of 60ft)
    assert mover.movement_remaining < 60 - 20  # less than if no penalty


@pytest.mark.asyncio
async def test_req_geo_007_not_enough_speed_through_creature_blocked(setup):
    """REQ-GEO-007: If speed isn't sufficient for the extra cost, movement is cancelled."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    # Mover at x=0, speed=20. Wants to move 15ft passing through a creature at x=7.5.
    # Normal cost ≈ 15ft, extra 5ft for creature = 20ft total, just barely makes it.
    # Move 20ft: normal 20, + 5 extra = 25 > speed 20 → should be cancelled.
    mover = _make_creature("Mover", vp, x=0.0, y=0.0, speed=20)
    blocker = _make_creature("Blocker", vp, x=10.0, y=0.0, size=5.0)

    j_dir = get_journals_dir(vp)
    for name in ["Mover", "Blocker"]:
        with open(os.path.join(j_dir, f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    spatial_service.active_combatants[vp] = ["Mover", "Blocker"]
    config = {"configurable": {"thread_id": vp}}

    res = await move_entity.ainvoke(
        {"entity_name": "Mover", "target_x": 20.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "SYSTEM ERROR" in res


@pytest.mark.asyncio
async def test_req_geo_007_dead_creature_no_extra_cost(setup):
    """REQ-GEO-007: Moving through a dead creature's space (hp=0) does NOT cost extra."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    mover = _make_creature("Mover", vp, x=0.0, y=0.0, speed=30)
    corpse = _make_creature("Corpse", vp, x=10.0, y=0.0, size=5.0)
    corpse.hp.base_value = 0

    j_dir = get_journals_dir(vp)
    for name in ["Mover", "Corpse"]:
        with open(os.path.join(j_dir, f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    spatial_service.active_combatants[vp] = ["Mover"]
    config = {"configurable": {"thread_id": vp}}

    res = await move_entity.ainvoke(
        {"entity_name": "Mover", "target_x": 20.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    # Dead creature should not block — 20ft costs only 20ft, within speed=30
    assert "SYSTEM ERROR" not in res
    assert mover.movement_remaining == 10  # 30 - 20 = 10


# ============================================================
# REQ-GEO-010: Squeezing penalties (condition already implemented)
# ============================================================

@pytest.mark.asyncio
async def test_req_geo_010_squeezing_doubles_movement_cost(setup):
    """REQ-GEO-010: While Squeezing, movement costs 1 extra foot per foot (double total)."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Halfling", vp, x=0.0, y=0.0, speed=25)
    entity.active_conditions.append(ActiveCondition(name="Squeezing", source_name="DM"))

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Halfling.md"), "w") as f:
        f.write("---\nname: Halfling\nactive_conditions: []\n---\n")

    spatial_service.active_combatants[vp] = ["Halfling"]
    config = {"configurable": {"thread_id": vp}}

    # Moving 10ft while squeezing should cost 20ft. Speed=25, should succeed with 5 left.
    res = await move_entity.ainvoke(
        {"entity_name": "Halfling", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "SYSTEM ERROR" not in res
    assert entity.movement_remaining == 5  # 25 - 20 = 5
