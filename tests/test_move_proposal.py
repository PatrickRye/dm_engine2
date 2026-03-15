import os
import json
import pytest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from main import app, ProposeMoveRequest
from dnd_rules_engine import Creature, ModifiableValue
from spatial_engine import spatial_service, Wall
from registry import clear_registry, add_entity, get_entity_by_name

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture(autouse=True)
def setup_engine_state():
    clear_registry()
    spatial_service.clear()
    # Seed with a player and an enemy
    player = Creature(name="Player", x=0.0, y=0.0, size=5.0, hp=ModifiableValue(10), ac=ModifiableValue(15), strength_mod=ModifiableValue(0), dexterity_mod=ModifiableValue(2))
    enemy = Creature(name="Enemy", x=10.0, y=10.0, size=5.0, hp=ModifiableValue(10), ac=ModifiableValue(12), strength_mod=ModifiableValue(1), dexterity_mod=ModifiableValue(1))
    add_entity(player)
    add_entity(enemy)
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)
    yield

def test_propose_valid_move(client):
    """Tests that a simple, unhindered move is approved."""
    request_data = {
        "entity_name": "Player",
        "waypoints": [[0.0, 0.0], [10.0, 0.0]],
        "vault_path": "/mock/vault"
    }
    response = client.post("/propose_move", json=request_data)
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is True
    assert not data["opportunity_attacks"]
    assert not data["traps_triggered"]

def test_propose_invalid_move_wall_collision(client):
    """Tests that a move is invalidated if it collides with a wall."""
    wall = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=True)
    spatial_service.add_wall(wall)

    request_data = {
        "entity_name": "Player",
        "waypoints": [[0.0, 0.0], [10.0, 0.0]],
        "vault_path": "/mock/vault"
    }
    response = client.post("/propose_move", json=request_data)
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert data["alternative_path"] is not None

def test_propose_move_triggers_opportunity_attack(client):
    """Tests that moving away from an adjacent enemy correctly flags an opportunity attack."""
    # Position player and enemy next to each other
    player = get_entity_by_name("Player")
    enemy = get_entity_by_name("Enemy")
    player.x, player.y = 5.0, 5.0
    enemy.x, enemy.y = 10.0, 5.0
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)
    
    # Player moves away
    request_data = {
        "entity_name": "Player",
        "waypoints": [[5.0, 5.0], [20.0, 5.0]],
        "vault_path": "/mock/vault"
    }
    
    response = client.post("/propose_move", json=request_data)
    assert response.status_code == 200
    data = response.json()
    
    assert data["is_valid"] is True
    assert "Enemy" in data["opportunity_attacks"]

def get_entity_by_name(name: str):
    """Helper to find an entity by name in the registry."""
    from registry import get_all_entities
    for entity in get_all_entities().values():
        if entity.name == name:
            return entity
    return None
