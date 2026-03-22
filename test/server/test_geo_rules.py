"""
Geometry / spatial rules tests.
REQ-GEO-001 through 005: Entity footprints by size — handled by entity.size field (data requirement)
REQ-GEO-006: Cannot end turn in occupied space
REQ-GEO-011: Melee reach from bounding-box edge (not center)
"""
import os
import pytest

from dnd_rules_engine import Creature, ModifiableValue
from spatial_engine import spatial_service
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield mock_obsidian_vault


def _make_creature(name, vault_path, x=0.0, y=0.0, size=5.0, tags=None, hp=20):
    c = Creature(
        name=name,
        vault_path=vault_path,
        tags=tags or [],
        x=x,
        y=y,
        size=size,
        hp=ModifiableValue(base_value=hp),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=2),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


# ============================================================
# REQ-GEO-006: Cannot end turn in occupied space
# ============================================================

@pytest.mark.asyncio
async def test_req_geo_006_blocked_by_occupant(setup):
    """REQ-GEO-006: Moving into a space occupied by a living creature is blocked."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    mover = _make_creature("Mover", vp, x=0.0, y=0.0)
    blocker = _make_creature("Blocker", vp, x=5.0, y=0.0)

    j_dir = get_journals_dir(vp)
    for name in ["Mover", "Blocker"]:
        with open(os.path.join(j_dir, f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await move_entity.ainvoke(
        {"entity_name": "Mover", "target_x": 5.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "SYSTEM ERROR" in res
    assert "REQ-GEO-006" in res
    assert "Blocker" in res


@pytest.mark.asyncio
async def test_req_geo_006_dead_creature_does_not_block(setup):
    """REQ-GEO-006: Dead creature (hp=0) does NOT block movement."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    mover = _make_creature("Mover", vp, x=0.0, y=0.0)
    corpse = _make_creature("Corpse", vp, x=5.0, y=0.0, hp=0)
    corpse.hp.base_value = 0

    j_dir = get_journals_dir(vp)
    for name in ["Mover", "Corpse"]:
        with open(os.path.join(j_dir, f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await move_entity.ainvoke(
        {"entity_name": "Mover", "target_x": 5.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "SYSTEM ERROR" not in res or "REQ-GEO-006" not in res


@pytest.mark.asyncio
async def test_req_geo_006_empty_space_allows_movement(setup):
    """REQ-GEO-006: Moving to an unoccupied square is allowed."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    mover = _make_creature("Mover", vp, x=0.0, y=0.0)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Mover.md"), "w") as f:
        f.write("---\nname: Mover\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await move_entity.ainvoke(
        {"entity_name": "Mover", "target_x": 15.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "REQ-GEO-006" not in res


# ============================================================
# REQ-GEO-011: Melee reach from bounding-box edge
# ============================================================

@pytest.mark.asyncio
async def test_req_geo_011_medium_vs_medium_adjacent_can_attack(setup):
    """REQ-GEO-011: Two Medium creatures at 5ft center-to-center ARE in melee range."""
    from tools import execute_melee_attack
    from vault_io import get_journals_dir

    vp = setup
    attacker = _make_creature("Fighter", vp, x=0.0, y=0.0, size=5.0)
    target = _make_creature("Goblin", vp, x=5.0, y=0.0, size=5.0)

    for name in ["Fighter", "Goblin"]:
        with open(os.path.join(get_journals_dir(vp), f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await execute_melee_attack.ainvoke(
        {"attacker_name": "Fighter", "target_name": "Goblin"},
        config=config,
    )
    assert "out of range" not in res.lower()


@pytest.mark.asyncio
async def test_req_geo_011_large_vs_medium_adjacent_can_attack(setup):
    """REQ-GEO-011: Large (size=10) at x=0 vs Medium (size=5) at x=7.5 are adjacent — in range."""
    from tools import execute_melee_attack
    from vault_io import get_journals_dir

    vp = setup
    # Large creature center at 0, edge at 5. Medium center at 7.5, edge at 5. Adjacent.
    attacker = _make_creature("Ogre", vp, x=0.0, y=0.0, size=10.0)
    target = _make_creature("Peasant", vp, x=7.5, y=0.0, size=5.0)

    for name in ["Ogre", "Peasant"]:
        with open(os.path.join(get_journals_dir(vp), f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await execute_melee_attack.ainvoke(
        {"attacker_name": "Ogre", "target_name": "Peasant"},
        config=config,
    )
    assert "out of range" not in res.lower()


@pytest.mark.asyncio
async def test_req_geo_011_large_vs_medium_within_5ft_reach_can_attack(setup):
    """REQ-GEO-011: Large (size=10) with 5ft reach vs Medium (size=5) at edge-to-edge 4ft is in range."""
    from tools import execute_melee_attack
    from vault_io import get_journals_dir

    vp = setup
    # Large center at 0, edge at 5. Target center at 5+4+2.5=11.5, edge-to-edge=4ft → in range.
    attacker = _make_creature("Ogre", vp, x=0.0, y=0.0, size=10.0)
    target = _make_creature("Peasant", vp, x=11.5, y=0.0, size=5.0)

    for name in ["Ogre", "Peasant"]:
        with open(os.path.join(get_journals_dir(vp), f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await execute_melee_attack.ainvoke(
        {"attacker_name": "Ogre", "target_name": "Peasant"},
        config=config,
    )
    assert "out of range" not in res.lower()


@pytest.mark.asyncio
async def test_req_geo_011_medium_vs_medium_not_adjacent_blocked(setup):
    """REQ-GEO-011: Two Medium creatures at 10ft center (edge-to-edge 5ft gap) are NOT in 5ft reach."""
    from tools import execute_melee_attack
    from vault_io import get_journals_dir

    vp = setup
    attacker = _make_creature("Fighter", vp, x=0.0, y=0.0, size=5.0)
    target = _make_creature("Goblin", vp, x=10.0, y=0.0, size=5.0)

    for name in ["Fighter", "Goblin"]:
        with open(os.path.join(get_journals_dir(vp), f"{name}.md"), "w") as f:
            f.write(f"---\nname: {name}\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await execute_melee_attack.ainvoke(
        {"attacker_name": "Fighter", "target_name": "Goblin"},
        config=config,
    )
    assert "out of range" in res.lower() or "SYSTEM ERROR" in res
