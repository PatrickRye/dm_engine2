"""
Illusion wall tests.
REQ-ILL-001: Physical intersection → auto-reveal (non-phantasm only)
REQ-ILL-002: Investigation check (Int vs spell DC) to disbelieve
REQ-ILL-003: Post-reveal = transparent (no longer blocks vision)
REQ-ILL-004: Phantasm = mental only; physical pass-through does NOT auto-reveal
REQ-ILL-005: Deafened → auto-succeed auditory illusions (LLM-tracked; no engine assertion needed)
REQ-ILL-006: Truesight → auto-see through all illusions
"""
import os
import pytest

from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus
from spatial_engine import spatial_service, Wall, MapData
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield mock_obsidian_vault


def _make_creature(name, vault_path, x=0.0, y=0.0, tags=None):
    c = Creature(
        name=name,
        vault_path=vault_path,
        tags=tags or [],
        x=x,
        y=y,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=2),
    )
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


def _add_illusion_wall(vault_path, label, start, end, is_phantasm=False, spell_dc=13):
    wall = Wall(
        label=label,
        start=start,
        end=end,
        is_solid=False,
        is_visible=True,
        is_illusion=True,
        is_phantasm=is_phantasm,
        illusion_spell_dc=spell_dc,
    )
    spatial_service.add_wall(wall, is_temporary=True, vault_path=vault_path)
    return wall


# ============================================================
# REQ-ILL-001: Physical intersection auto-reveal (non-phantasm)
# ============================================================

@pytest.mark.asyncio
async def test_req_ill_001_pass_through_reveals_illusion(setup):
    """REQ-ILL-001: Moving through an illusion wall reveals it to the entity."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Hero", vp, x=0.0, y=0.0)

    wall = _add_illusion_wall(vp, "Illusion Wall", start=(5.0, -5.0), end=(5.0, 5.0))
    assert str(entity.entity_uuid) not in wall.revealed_for

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Hero.md"), "w") as f:
        f.write("---\nname: Hero\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    # Move through the illusion wall (from x=0 to x=10, crossing x=5)
    res = await move_entity.ainvoke(
        {"entity_name": "Hero", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    assert "REQ-ILL-001" in res
    assert str(entity.entity_uuid) in wall.revealed_for


@pytest.mark.asyncio
async def test_req_ill_001_illusion_does_not_block_movement(setup):
    """REQ-ILL-001: Illusion wall is_solid=False, so movement is NOT blocked."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Hero", vp, x=0.0, y=0.0)
    _add_illusion_wall(vp, "Fake Wall", start=(5.0, -5.0), end=(5.0, 5.0))

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Hero.md"), "w") as f:
        f.write("---\nname: Hero\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await move_entity.ainvoke(
        {"entity_name": "Hero", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    # Must NOT return a wall-blocked SYSTEM ERROR
    assert "collided" not in res.lower() or "SYSTEM ERROR" not in res


# ============================================================
# REQ-ILL-002: Investigation check to disbelieve
# ============================================================

@pytest.mark.asyncio
async def test_req_ill_002_investigation_success_reveals(setup, monkeypatch):
    """REQ-ILL-002: Successful Investigation check reveals the illusion to the entity."""
    import random
    from tools import investigate_illusion
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Wizard", vp)
    wall = _add_illusion_wall(vp, "Fake Doorway", start=(5.0, -5.0), end=(5.0, 5.0), spell_dc=13)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Wizard.md"), "w") as f:
        f.write("---\nname: Wizard\nactive_conditions: []\n---\n")

    # Force roll of 20 (guaranteed success vs DC 13)
    monkeypatch.setattr(random, "randint", lambda a, b: 20)
    config = {"configurable": {"thread_id": vp}}
    res = await investigate_illusion.ainvoke(
        {"entity_name": "Wizard", "illusion_label": "Fake Doorway"},
        config=config,
    )
    assert "SUCCESS" in res
    assert str(entity.entity_uuid) in wall.revealed_for


@pytest.mark.asyncio
async def test_req_ill_002_investigation_failure_does_not_reveal(setup, monkeypatch):
    """REQ-ILL-002: Failed Investigation check leaves the illusion intact."""
    import random
    from tools import investigate_illusion
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Warrior", vp)
    wall = _add_illusion_wall(vp, "Illusion Wall", start=(5.0, -5.0), end=(5.0, 5.0), spell_dc=20)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Warrior.md"), "w") as f:
        f.write("---\nname: Warrior\nactive_conditions: []\n---\n")

    monkeypatch.setattr(random, "randint", lambda a, b: 1)
    config = {"configurable": {"thread_id": vp}}
    res = await investigate_illusion.ainvoke(
        {"entity_name": "Warrior", "illusion_label": "Illusion Wall"},
        config=config,
    )
    assert "FAILURE" in res
    assert str(entity.entity_uuid) not in wall.revealed_for


# ============================================================
# REQ-ILL-003: Revealed illusion is transparent (no LOS block)
# ============================================================

def test_req_ill_003_revealed_illusion_skipped_in_vision_check(setup):
    """REQ-ILL-003: After reveal, illusion wall is transparent to that viewer in check_path_collision."""
    vp = setup
    entity = _make_creature("Scout", vp, x=0.0, y=0.0)

    wall = _add_illusion_wall(vp, "See-Through Wall", start=(5.0, -5.0), end=(5.0, 5.0))

    # Before reveal: wall blocks vision
    blocked = spatial_service.check_path_collision(
        0.0, 0.0, 0.0, 10.0, 0.0, 0.0, check_vision=True,
        viewer_uuid=entity.entity_uuid, vault_path=vp
    )
    assert blocked is not None, "Unrevealed illusion should block vision"

    # Reveal the illusion for this entity
    wall.revealed_for.append(str(entity.entity_uuid))
    spatial_service.invalidate_cache(vp)

    # After reveal: vision check passes through
    blocked_after = spatial_service.check_path_collision(
        0.0, 0.0, 0.0, 10.0, 0.0, 0.0, check_vision=True,
        viewer_uuid=entity.entity_uuid, vault_path=vp
    )
    assert blocked_after is None, "Revealed illusion should NOT block vision"


def test_req_ill_003_revealed_for_one_still_blocks_other(setup):
    """REQ-ILL-003: Revealing to entity A doesn't affect entity B's LOS."""
    vp = setup
    entity_a = _make_creature("Scout", vp, x=0.0, y=0.0)
    entity_b = _make_creature("Guard", vp, x=0.0, y=3.0)

    wall = _add_illusion_wall(vp, "Partial Wall", start=(5.0, -5.0), end=(5.0, 5.0))
    wall.revealed_for.append(str(entity_a.entity_uuid))
    spatial_service.invalidate_cache(vp)

    # A can see through
    blocked_a = spatial_service.check_path_collision(
        0.0, 0.0, 0.0, 10.0, 0.0, 0.0, check_vision=True,
        viewer_uuid=entity_a.entity_uuid, vault_path=vp
    )
    assert blocked_a is None

    # B cannot see through
    blocked_b = spatial_service.check_path_collision(
        0.0, 3.0, 0.0, 10.0, 3.0, 0.0, check_vision=True,
        viewer_uuid=entity_b.entity_uuid, vault_path=vp
    )
    assert blocked_b is not None


# ============================================================
# REQ-ILL-004: Phantasm — physical pass-through does NOT auto-reveal
# ============================================================

@pytest.mark.asyncio
async def test_req_ill_004_phantasm_not_revealed_by_pass_through(setup):
    """REQ-ILL-004: Phantasm walls are not auto-revealed when physically passed through."""
    from tools import move_entity
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Hero", vp, x=0.0, y=0.0)
    wall = _add_illusion_wall(vp, "Phantasm Wall", start=(5.0, -5.0), end=(5.0, 5.0), is_phantasm=True)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Hero.md"), "w") as f:
        f.write("---\nname: Hero\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await move_entity.ainvoke(
        {"entity_name": "Hero", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
        config=config,
    )
    # Physical pass-through of phantasm must NOT reveal it
    assert str(entity.entity_uuid) not in wall.revealed_for
    assert "REQ-ILL-001" not in res


# ============================================================
# REQ-ILL-006: Truesight auto-disbelieves all illusions
# ============================================================

@pytest.mark.asyncio
async def test_req_ill_006_truesight_auto_reveals(setup):
    """REQ-ILL-006: Entities with Truesight tag auto-succeed Investigation vs illusions."""
    from tools import investigate_illusion
    from vault_io import get_journals_dir

    vp = setup
    entity = _make_creature("Seer", vp, tags=["truesight"])
    wall = _add_illusion_wall(vp, "Illusion", start=(5.0, -5.0), end=(5.0, 5.0), spell_dc=25)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Seer.md"), "w") as f:
        f.write("---\nname: Seer\nactive_conditions: []\n---\n")

    config = {"configurable": {"thread_id": vp}}
    res = await investigate_illusion.ainvoke(
        {"entity_name": "Seer", "illusion_label": "Illusion"},
        config=config,
    )
    assert "REQ-ILL-006" in res
    assert str(entity.entity_uuid) in wall.revealed_for


@pytest.mark.asyncio
async def test_req_ill_006_reveal_tool_global_clears_visibility(setup):
    """REQ-ILL-003: reveal_illusion with no entity_name globally disables is_visible."""
    from tools import reveal_illusion
    from vault_io import get_journals_dir

    vp = setup
    wall = _add_illusion_wall(vp, "Global Illusion", start=(5.0, -5.0), end=(5.0, 5.0))
    assert wall.is_visible is True

    config = {"configurable": {"thread_id": vp}}
    res = await reveal_illusion.ainvoke(
        {"illusion_label": "Global Illusion"},
        config=config,
    )
    assert "globally revealed" in res.lower()
    assert wall.is_visible is False
