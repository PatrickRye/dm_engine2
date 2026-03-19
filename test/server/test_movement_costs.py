import pytest
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service, TerrainZone
from registry import clear_registry, register_entity
from tools import move_entity
import os


@pytest.fixture
def client():
    from main import app
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture(autouse=True)
def setup_engine_state():
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


@pytest.mark.asyncio
async def test_req_mov_011_additive_movement_penalties(tmp_path):
    """
    REQ-MOV-011: Movement penalties stack additively. Moving 1 foot while crawling (+1) in difficult terrain (+1) costs a total of 3 feet of movement budget.
    """
    vault_path = str(tmp_path)
    os.makedirs(os.path.join(vault_path, "Journals"), exist_ok=True)
    config = {"configurable": {"thread_id": vault_path}}

    player = Creature(
        name="Player",
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
    spatial_service.active_combatants[vault_path] = ["Player"]

    # Create a difficult terrain zone
    difficult_terrain = TerrainZone(points=[(0, -10), (10, -10), (10, 10), (0, 10)], is_difficult=True)
    spatial_service.add_terrain(difficult_terrain, vault_path=vault_path)

    # Make the player prone
    player.active_conditions.append(ActiveCondition(name="Prone"))

    # Move the player 5 feet through the difficult terrain while prone
    # Cost: 5 (base) + 5 (difficult terrain) + 5 (prone) = 15
    res = await move_entity.ainvoke(
        {"entity_name": "Player", "target_x": 5.0, "target_y": 0.0, "movement_type": "crawl"}, config=config
    )

    assert "moved from" in res
    assert player.x == 5.0
    assert player.movement_remaining == 15  # 30 - 15 = 15
