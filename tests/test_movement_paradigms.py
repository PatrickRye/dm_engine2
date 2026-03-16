import pytest
import os
from main import app
from fastapi.testclient import TestClient
from dnd_rules_engine import Creature, ModifiableValue
from spatial_engine import spatial_service, Wall
from registry import clear_registry, register_entity, get_all_entities
from tools import move_entity, end_combat, start_combat


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def setup_engine_state():
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


@pytest.mark.asyncio
async def test_paradigm_1_travel(tmp_path):
    """Paradigm 1/2: Travel completely bypasses walls and budgets."""
    vault_path = str(tmp_path)
    os.makedirs(os.path.join(vault_path, "Journals"), exist_ok=True)
    config = {"configurable": {"thread_id": vault_path}}

    player = Creature(
        name="Traveler",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
    )
    register_entity(player)
    spatial_service.sync_entity(player)

    # Create an impassable wall
    wall = Wall(start=(5.0, -50.0), end=(5.0, 50.0), is_solid=True)
    spatial_service.add_wall(wall)

    # Move 1000 ft through the wall using "travel"
    res = await move_entity.ainvoke(
        {"entity_name": "Traveler", "target_x": 1000.0, "target_y": 0.0, "movement_type": "travel"}, config=config
    )

    assert "moved from" in res
    assert "SYSTEM ERROR" not in res
    assert player.x == 1000.0


@pytest.mark.asyncio
async def test_paradigm_3_dungeon_crawl(tmp_path):
    """Paradigm 3: Out of combat 'walk' respects walls but ignores the 30ft budget."""
    vault_path = str(tmp_path)
    os.makedirs(os.path.join(vault_path, "Journals"), exist_ok=True)
    config = {"configurable": {"thread_id": vault_path}}

    player = Creature(
        name="Crawler",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
    )
    register_entity(player)
    spatial_service.sync_entity(player)

    # 1. Verify wall blocking still works
    wall = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=True)
    spatial_service.add_wall(wall, vault_path=vault_path)

    res_wall = await move_entity.ainvoke(
        {"entity_name": "Crawler", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"}, config=config
    )
    assert "Movement blocked" in res_wall
    spatial_service.remove_wall(wall.wall_id, vault_path=vault_path)

    # 2. Verify moving 100ft (ignoring 30ft budget) succeeds out of combat
    res_dist = await move_entity.ainvoke(
        {"entity_name": "Crawler", "target_x": 100.0, "target_y": 0.0, "movement_type": "walk"}, config=config
    )
    assert "moved from" in res_dist
    assert "SYSTEM ERROR" not in res_dist
    assert player.x == 100.0


@pytest.mark.asyncio
async def test_paradigm_4_and_end_combat_reset(tmp_path):
    """Paradigm 4: Enters combat, enforces budget, uses reaction, then exits combat and flushes state."""
    vault_path = str(tmp_path)
    journals = os.path.join(vault_path, "Journals")
    os.makedirs(journals, exist_ok=True)
    config = {"configurable": {"thread_id": vault_path}}

    with open(os.path.join(journals, "Fighter.md"), "w") as f:
        f.write("---\nname: Fighter\ntags: [pc]\nmax_hp: 10\nhp: 10\nspeed: 30\n---")

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
    )
    register_entity(fighter)

    # Trigger Paradigm 4 (Combat)
    await start_combat.ainvoke({"pc_names": ["Fighter"], "enemies": []}, config=config)

    fighter = [e for e in get_all_entities(vault_path).values() if e.name == "Fighter"][0]
    fighter.reaction_used = True

    # Test Combat strictness (Should fail)
    res = await move_entity.ainvoke(
        {"entity_name": "Fighter", "target_x": 40.0, "target_y": 0.0, "movement_type": "walk"}, config=config
    )
    assert "exceeds remaining speed" in res

    # Exit combat and verify flush
    await end_combat.ainvoke({}, config=config)
    assert fighter.movement_remaining == 30
    assert fighter.reaction_used is False
