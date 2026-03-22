"""
Terrain aura tests.
REQ-ENV-011: Entities entering/leaving a terrain zone dynamically gain/lose its associated conditions.
Zone tags "aura:ConditionName" define which conditions the zone applies.
"""
import os
import pytest

from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service, TerrainZone
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield mock_obsidian_vault


def _make_creature(name, vault_path, x=0.0, y=0.0):
    c = Creature(
        name=name,
        vault_path=vault_path,
        x=x,
        y=y,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


def _silence_zone(vault_path, points=None):
    """Creates a silence zone that applies Deafened aura."""
    if points is None:
        points = [(5.0, -10.0), (20.0, -10.0), (20.0, 10.0), (5.0, 10.0)]
    zone = TerrainZone(
        label="Silence",
        points=points,
        is_difficult=False,
        tags=["aura:Deafened"],
    )
    spatial_service.add_terrain(zone, is_temporary=True, vault_path=vault_path)
    return zone


# ============================================================
# REQ-ENV-011: Enter zone → gain condition
# ============================================================

@pytest.mark.asyncio
async def test_req_env_011_enter_zone_applies_condition(setup):
    """REQ-ENV-011: Moving into a zone with aura:Deafened applies Deafened condition."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Rogue", vp, x=0.0, y=0.0)
    _silence_zone(vp)  # zone from x=5 to x=20

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Rogue.md"), "w") as f:
        f.write("---\nname: Rogue\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await move_entity.ainvoke(
        {"entity_name": "Rogue", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "REQ-ENV-011" in res
    assert any(c.name.lower() == "deafened" for c in entity.active_conditions)


@pytest.mark.asyncio
async def test_req_env_011_leave_zone_removes_condition(setup):
    """REQ-ENV-011: Moving out of a zone removes its aura condition."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    # Start inside the silence zone
    entity = _make_creature("Rogue", vp, x=10.0, y=0.0)
    zone = _silence_zone(vp)
    # Manually pre-apply the aura condition (as if already in the zone)
    entity.active_conditions.append(
        ActiveCondition(name="Deafened", source_name="Silence", duration_seconds=-1)
    )
    spatial_service.sync_entity(entity)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Rogue.md"), "w") as f:
        f.write("---\nname: Rogue\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    # Move outside the zone (x=0 is outside zone starting at x=5)
    res = await move_entity.ainvoke(
        {"entity_name": "Rogue", "target_x": 0.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "REQ-ENV-011" in res
    assert "lost" in res.lower() or "left" in res.lower()
    assert not any(
        c.name.lower() == "deafened" and c.source_name == "Silence"
        for c in entity.active_conditions
    )


@pytest.mark.asyncio
async def test_req_env_011_zone_condition_not_removed_if_other_source(setup):
    """REQ-ENV-011: Only zone-sourced conditions are removed; permanent Deafened stays."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Rogue", vp, x=10.0, y=0.0)
    _silence_zone(vp)
    # Entity has Deafened from a different source (e.g. ear damage) AND from the zone
    entity.active_conditions.append(
        ActiveCondition(name="Deafened", source_name="Ear Damage", duration_seconds=-1)
    )
    entity.active_conditions.append(
        ActiveCondition(name="Deafened", source_name="Silence", duration_seconds=-1)
    )
    spatial_service.sync_entity(entity)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Rogue.md"), "w") as f:
        f.write("---\nname: Rogue\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    await move_entity.ainvoke(
        {"entity_name": "Rogue", "target_x": 0.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    # Zone-sourced Deafened removed; ear-damage Deafened remains
    remaining = [c for c in entity.active_conditions if c.name.lower() == "deafened"]
    assert len(remaining) == 1
    assert remaining[0].source_name == "Ear Damage"


@pytest.mark.asyncio
async def test_req_env_011_multiple_auras_applied(setup):
    """REQ-ENV-011: Zone with multiple aura tags applies all of them."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Hero", vp, x=0.0, y=0.0)
    zone = TerrainZone(
        label="Haunted Ground",
        points=[(5.0, -10.0), (20.0, -10.0), (20.0, 10.0), (5.0, 10.0)],
        is_difficult=False,
        tags=["aura:Frightened", "aura:Poisoned"],
    )
    spatial_service.add_terrain(zone, is_temporary=True, vault_path=vp)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Hero.md"), "w") as f:
        f.write("---\nname: Hero\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    await move_entity.ainvoke(
        {"entity_name": "Hero", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    cond_names = [c.name.lower() for c in entity.active_conditions]
    assert "frightened" in cond_names
    assert "poisoned" in cond_names


@pytest.mark.asyncio
async def test_req_env_011_no_aura_tag_no_condition(setup):
    """REQ-ENV-011: A zone without aura tags does not apply conditions (only difficult terrain)."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Hero", vp, x=0.0, y=0.0)
    zone = TerrainZone(
        label="Mud",
        points=[(5.0, -10.0), (20.0, -10.0), (20.0, 10.0), (5.0, 10.0)],
        is_difficult=True,
        tags=[],  # no aura tags
    )
    spatial_service.add_terrain(zone, is_temporary=True, vault_path=vp)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Hero.md"), "w") as f:
        f.write("---\nname: Hero\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    await move_entity.ainvoke(
        {"entity_name": "Hero", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert len(entity.active_conditions) == 0
