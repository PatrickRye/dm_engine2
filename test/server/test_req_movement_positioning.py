import os
import pytest
from fastapi.testclient import TestClient

from main import app
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import execute_grapple_or_shove


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def setup_engine_state(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_spc_003_forced_displacement_collision(setup_engine_state, mock_dice):
    vp = setup_engine_state
    config = {"configurable": {"thread_id": vp}}

    attacker = Creature(
        name="Orc",
        vault_path=vp,
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Goblin",
        vault_path=vp,
        x=5.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    bystander = Creature(
        name="Wall_of_Meat",
        vault_path=vp,
        x=10.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=100),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    register_entity(attacker)
    register_entity(target)
    register_entity(bystander)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    spatial_service.sync_entity(bystander)

    # Attacker shoves Target 5ft (from x=5 to x=10).
    # x=10 is occupied by bystander.
    # Target should be shunted backward and fall prone.
    with mock_dice(default=5):  # Orc wins shove (DC 14, Goblin rolls 5)
        res = await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Orc", "target_name": "Goblin", "action_type": "shove", "shove_type": "away"}, config=config
        )

    assert "REQ-SPC-003" in res
    assert "fell Prone" in res
    assert any(c.name == "Prone" for c in target.active_conditions)
    assert target.x < 10.0  # Shunted


def test_req_mov_012_occupied_spaces_difficult_terrain(client, setup_engine_state):
    vp = setup_engine_state
    pc = Creature(
        name="Player",
        vault_path=vp,
        tags=["pc"],
        x=0.0,
        y=0.0,
        size=5.0,
        speed=30,
        movement_remaining=30,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    normal = Creature(
        name="NormalEnemy",
        vault_path=vp,
        tags=["monster"],
        x=10.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    tiny = Creature(
        name="TinyEnemy",
        vault_path=vp,
        tags=["monster", "tiny"],
        x=0.0,
        y=10.0,
        size=2.5,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    incap = Creature(
        name="IncapEnemy",
        vault_path=vp,
        tags=["monster"],
        x=0.0,
        y=-10.0,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Unconscious")],
    )
    for e in [pc, normal, tiny, incap]:
        register_entity(e)
        spatial_service.sync_entity(e)
    spatial_service.active_combatants[vp] = ["Player"]

    res1 = client.post(
        "/propose_move", json={"entity_name": "Player", "waypoints": [[0.0, 0.0], [20.0, 0.0]], "vault_path": vp}
    ).json()
    assert res1["is_valid"] is False and "Size difference too small" in res1["invalid_reason"]
    res2 = client.post(
        "/propose_move",
        json={"entity_name": "Player", "waypoints": [[0.0, 0.0], [0.0, 20.0]], "vault_path": vp, "force_execute": True},
    ).json()
    assert res2["is_valid"] is True and res2["movement_cost"] > 20.0

    pc.x = 0.0
    pc.y = 0.0
    pc.movement_remaining = 30
    spatial_service.sync_entity(pc)

    res3 = client.post(
        "/propose_move",
        json={"entity_name": "Player", "waypoints": [[0.0, 0.0], [0.0, -20.0]], "vault_path": vp, "force_execute": True},
    ).json()
    assert res3["is_valid"] is True and res3["movement_cost"] > 20.0
