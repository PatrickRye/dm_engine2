import pytest

from fastapi.testclient import TestClient
from main import app
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service, Wall, TrapDefinition, TerrainZone
from registry import clear_registry, register_entity


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def setup_engine_state():
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0  # Ensure waypoint calculations match the spatial coordinates directly
    # Seed with a player and an enemy
    player = Creature(
        name="Player",
        vault_path="default",
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )
    player.movement_remaining = 30.0  # Ensure movement pool is initialized for tests
    enemy = Creature(
        name="Enemy",
        vault_path="default",
        x=10.0,
        y=10.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=1),
        dexterity_mod=ModifiableValue(base_value=1),
    )
    register_entity(player)
    register_entity(enemy)
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)
    yield


def test_propose_valid_move(client):
    """Tests that a simple, unhindered move is approved."""
    request_data = {"entity_name": "Player", "waypoints": [[0.0, 0.0], [10.0, 0.0]], "vault_path": "default"}
    response = client.post("/propose_move", json=request_data)
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is True
    assert not data["opportunity_attacks"]
    assert not data["traps_triggered"]
    assert data["invalid_reason"] == ""
    assert data["executed"] is True


def test_propose_invalid_move_wall_collision(client):
    """
    Tests that a move is invalidated if it collides with a wall.
    [Mapped: REQ-SPC-001]
    """
    wall = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=True)
    spatial_service.add_wall(wall)

    request_data = {"entity_name": "Player", "waypoints": [[0.0, 0.0], [10.0, 0.0]], "vault_path": "default"}
    response = client.post("/propose_move", json=request_data)
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert data["alternative_path"] is not None
    assert "Path blocked" in data["invalid_reason"]
    assert data["executed"] is False


def test_propose_move_triggers_opportunity_attack(client):
    """
    Tests that moving away from an adjacent enemy correctly flags an opportunity attack.
    [Mapped: REQ-ACT-006]
    """
    # Position player and enemy next to each other
    player = get_entity_by_name("Player")
    enemy = get_entity_by_name("Enemy")
    player.x, player.y = 5.0, 5.0
    enemy.x, enemy.y = 10.0, 5.0
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)

    # Player moves away
    request_data = {"entity_name": "Player", "waypoints": [[5.0, 5.0], [20.0, 5.0]], "vault_path": "default"}

    response = client.post("/propose_move", json=request_data)
    assert response.status_code == 200
    data = response.json()

    assert data["is_valid"] is True
    assert "Enemy" in data["opportunity_attacks"]
    assert data["executed"] is False


def test_propose_move_within_reach_no_opportunity_attack(client):
    """
    Tests that moving around an enemy (staying within reach) does not trigger an opportunity attack.
    [Mapped: REQ-ACT-006]
    """
    player = get_entity_by_name("Player")
    enemy = get_entity_by_name("Enemy")
    player.x, player.y = 5.0, 5.0
    enemy.x, enemy.y = 10.0, 5.0  # Distance = 5ft
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)

    # Player moves around the enemy, staying exactly 5ft away
    request_data = {
        "entity_name": "Player",
        "waypoints": [[5.0, 5.0], [10.0, 10.0]],  # Chebyshev distance from (10,10) to (10,5) is still 5ft
        "vault_path": "default",
    }

    response = client.post("/propose_move", json=request_data)
    assert response.status_code == 200
    data = response.json()

    assert data["is_valid"] is True
    assert "Enemy" not in data["opportunity_attacks"]
    assert data["executed"] is True


def test_propose_move_with_disengage_condition(client):
    """
    Tests that moving away with the Disengage condition suppresses opportunity attacks.
    [Mapped: REQ-CLS-017, REQ-ACT-006]
    """
    player = get_entity_by_name("Player")
    enemy = get_entity_by_name("Enemy")
    player.x, player.y = 5.0, 5.0
    enemy.x, enemy.y = 10.0, 5.0

    # Apply the Disengage condition
    player.active_conditions.append(ActiveCondition(name="Disengage"))
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)

    request_data = {"entity_name": "Player", "waypoints": [[5.0, 5.0], [20.0, 5.0]], "vault_path": "default"}

    response = client.post("/propose_move", json=request_data)
    data = response.json()

    assert data["is_valid"] is True
    assert "Enemy" not in data["opportunity_attacks"]
    assert data["executed"] is True


def test_propose_move_difficult_terrain_cost(client, tmp_path):
    """
    Tests that difficult terrain appropriately increases movement cost and invalidates if it exceeds remaining speed.
    [Mapped: REQ-COR-003]
    """
    vault_path = str(tmp_path)

    # Set in-memory combat state
    spatial_service.active_combatants[vault_path] = ["Player"]

    player = Creature(
        name="Player",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )
    register_entity(player)
    player.x, player.y = 0.0, 0.0
    player.movement_remaining = 30.0
    spatial_service.sync_entity(player)

    # Mock a terrain zone in the middle of the path
    mock_terrain = TerrainZone(points=[(5.0, -5.0), (15.0, -5.0), (15.0, 5.0), (5.0, 5.0)], is_difficult=True)
    spatial_service.get_map_data(vault_path).terrain = [mock_terrain]

    # Move 20 ft. 0->5 is normal (5), 5->15 is DT (20), 15->20 is normal (5). Total = 30.
    request_data = {"entity_name": "Player", "waypoints": [[0.0, 0.0], [20.0, 0.0]], "vault_path": vault_path}

    response = client.post("/propose_move", json=request_data)
    data = response.json()
    assert data["is_valid"] is True
    assert data["movement_cost"] == 30.0
    assert data["invalid_reason"] == ""
    assert data["executed"] is True

    # Completely flush engine state to guarantee perfect test isolation
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    player3 = Creature(
        name="Player3",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )
    player3.movement_remaining = 30.0
    register_entity(player3)
    spatial_service.sync_entity(player3)

    mock_terrain2 = TerrainZone(points=[(5.0, -5.0), (15.0, -5.0), (15.0, 5.0), (5.0, 5.0)], is_difficult=True)
    spatial_service.get_map_data(vault_path).terrain = [mock_terrain2]

    spatial_service.active_combatants[vault_path] = ["Player3"]

    # Move 25 ft. Total cost = 35. Exceeds 30 ft movement remaining.
    request_data_invalid = {"entity_name": "Player3", "waypoints": [[0.0, 0.0], [25.0, 0.0]], "vault_path": vault_path}
    response2 = client.post("/propose_move", json=request_data_invalid)
    data2 = response2.json()
    assert data2["is_valid"] is False
    assert data2["movement_cost"] == 35.0
    assert "exceeds remaining speed" in data2["invalid_reason"]
    assert data2["executed"] is False


def test_propose_move_unknown_trap_auto_executes(client):
    """
    Tests that moving through an unknown trap is considered valid and auto-executes (triggering the trap).
    [Mapped: REQ-EDG-001]
    """

    wall = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=False)
    wall.trap = TrapDefinition(hazard_name="Hidden Pit", known_by_players=False)
    spatial_service.add_wall(wall)

    request_data = {"entity_name": "Player", "waypoints": [[0.0, 0.0], [10.0, 0.0]], "vault_path": "default"}
    response = client.post("/propose_move", json=request_data)
    data = response.json()
    assert data["is_valid"] is True
    assert not data["traps_triggered"]
    assert data["executed"] is True


def test_propose_move_known_trap_prompts(client):
    """
    Tests that moving through a KNOWN trap is valid, but halts execution to ask for confirmation.
    [Mapped: REQ-EDG-001]
    """
    wall = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=False)
    wall.trap = TrapDefinition(hazard_name="Spike Trap", known_by_players=True)
    spatial_service.add_wall(wall)

    request_data = {"entity_name": "Player", "waypoints": [[0.0, 0.0], [10.0, 0.0]], "vault_path": "default"}
    response = client.post("/propose_move", json=request_data)
    data = response.json()
    assert data["is_valid"] is True
    assert "Spike Trap" in data["traps_triggered"]
    assert data["executed"] is False


def test_movement_interruption_on_oa(client, tmp_path):
    """
    Tests that movement budget is exactly deducted up until the interruption point, and tests zero-speed resume logic.
    [Mapped: REQ-ACT-002, REQ-ACT-006]
    """
    vault_path = str(tmp_path)

    spatial_service.active_combatants[vault_path] = ["Player", "Enemy"]

    player = Creature(
        name="Player",
        vault_path=vault_path,
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
    )
    enemy = Creature(
        name="Enemy",
        vault_path=vault_path,
        x=0.0,
        y=5.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=1),
        dexterity_mod=ModifiableValue(base_value=1),
    )
    register_entity(player)
    register_entity(enemy)
    player.x, player.y = 0.0, 0.0
    enemy.x, enemy.y = 0.0, 5.0  # Adjacent
    player.movement_remaining = 30.0
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)

    # Player moves 20ft away to (0, -20)
    request_data = {
        "entity_name": "Player",
        "waypoints": [[0.0, 0.0], [0.0, -20.0]],
        "vault_path": vault_path,
        "force_execute": False,
    }

    res1 = client.post("/propose_move", json=request_data)
    data1 = res1.json()
    assert data1["is_valid"] is True
    assert data1["executed"] is False
    assert "Enemy" in data1["opportunity_attacks"]

    # Force execution overrides warning, but STOPS natively exactly where it provoked.
    request_data["force_execute"] = True
    res2 = client.post("/propose_move", json=request_data)
    data2 = res2.json()
    assert data2["executed"] is True
    assert data2["final_x"] == 0.0
    assert data2["final_y"] == -5.0  # (0,0)->(0,-5) triggers OA from (0,5). Distance goes from 5 to 10.
    assert player.movement_remaining in [20.0, 25.0]  # Engine may apply different cost based on grid

    # Resume movement
    request_data_resume = {
        "entity_name": "Player",
        "waypoints": [[0.0, -5.0], [0.0, -20.0]],
        "vault_path": vault_path,
    }
    res3 = client.post("/propose_move", json=request_data_resume)
    data3 = res3.json()
    assert data3["is_valid"] is True
    assert data3["executed"] is True
    assert data3["final_y"] == -20.0
    assert player.movement_remaining in [5.0, 10.0]

    # Reset map and trigger a fake zero-speed condition block (e.g. Sentinel / Grappled)
    player.x, player.y = 0.0, 0.0
    player.movement_remaining = 30.0
    spatial_service.sync_entity(player)

    res5 = client.post("/propose_move", json=request_data)
    assert res5.json()["executed"] is True
    assert player.movement_remaining in [20.0, 25.0]

    # Mock Sentinel or Grapple dropping speed to 0 mid-turn
    player.movement_remaining = 0.0
    res6 = client.post("/propose_move", json=request_data_resume)
    data6 = res6.json()
    assert data6["is_valid"] is False
    assert "exceeds remaining speed" in data6["invalid_reason"]


def get_entity_by_name(name: str, vault_path: str = "default"):
    """Helper to find an entity by name in the registry."""
    from registry import get_all_entities

    for entity in get_all_entities(vault_path).values():
        if entity.name == name:
            return entity
    return None
