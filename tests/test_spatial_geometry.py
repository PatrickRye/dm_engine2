import pytest
from fastapi.testclient import TestClient
from main import app
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition, GameEvent, EventBus
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from unittest.mock import patch


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def setup_engine_state():
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


def test_req_geo_006_end_occupied_space(client):
    """
    REQ-GEO-006: An entity can NEVER end its movement in a space occupied by another creature.
    """
    p1 = Creature(
        name="P1",
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    p2 = Creature(
        name="P2",
        x=10.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(p1)
    register_entity(p2)
    spatial_service.sync_entity(p1)
    spatial_service.sync_entity(p2)

    req = {"entity_name": "P1", "waypoints": [[0.0, 0.0], [10.0, 0.0]], "vault_path": "default"}
    res = client.post("/propose_move", json=req)
    data = res.json()

    assert data["is_valid"] is False
    assert "Cannot end movement in a space occupied by P2" in data["invalid_reason"]


def test_req_geo_007_and_008_friendly_and_hostile_space(client):
    """
    REQ-GEO-007: Moving through friendly space is Difficult Terrain.
    REQ-GEO-008: Moving through hostile space requires 2 size category difference, and is Difficult Terrain.
    """
    halfling = Creature(
        name="Halfling",
        tags=["pc", "small"],
        size=5.0,
        x=0.0,
        y=0.0,
        movement_remaining=30,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    ally = Creature(
        name="Ally",
        tags=["pc", "medium"],
        size=5.0,
        x=10.0,
        y=0.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    hostile_orc = Creature(
        name="Orc",
        tags=["monster", "medium"],
        size=5.0,
        x=20.0,
        y=0.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    hostile_ogre = Creature(
        name="Ogre",
        tags=["monster", "large"],
        size=10.0,
        x=40.0,
        y=0.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    register_entity(halfling)
    register_entity(ally)
    register_entity(hostile_orc)
    register_entity(hostile_ogre)
    for e in [halfling, ally, hostile_orc, hostile_ogre]:
        spatial_service.sync_entity(e)

    spatial_service.active_combatants["default"] = ["Halfling"]

    # Move through Friendly Ally -> Allowed, but costs extra (movement cost > base distance)
    req1 = {"entity_name": "Halfling", "waypoints": [[0.0, 0.0], [15.0, 0.0]], "vault_path": "default"}
    res1 = client.post("/propose_move", json=req1).json()
    assert res1["is_valid"] is True
    assert res1["movement_cost"] > 15.0  # Because of difficult terrain over the ally

    # Move through Hostile Orc (Small vs Medium = 1 category) -> Blocked
    req2 = {"entity_name": "Halfling", "waypoints": [[15.0, 0.0], [25.0, 0.0]], "vault_path": "default"}
    res2 = client.post("/propose_move", json=req2).json()
    assert res2["is_valid"] is False
    assert "Size difference too small" in res2["invalid_reason"]

    # Move through Hostile Ogre (Small vs Large = 2 categories) -> Allowed, costs extra
    req3 = {"entity_name": "Halfling", "waypoints": [[25.0, 0.0], [55.0, 0.0]], "vault_path": "default"}
    res3 = client.post("/propose_move", json=req3).json()
    assert res3["is_valid"] is True
    assert res3["movement_cost"] > 15.0


def test_req_geo_009_and_010_squeezing(mock_dice):
    """
    REQ-GEO-009/010: Squeezing penalties: Extra movement cost, Disadv on attacks and DEX saves, attacks against have Adv.
    """
    squeezer = Creature(
        name="Squeezer",
        x=0.0,
        y=0.0,
        speed=30,
        movement_remaining=30,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Squeezing")],
    )
    enemy = Creature(
        name="Enemy",
        x=5.0,
        y=0.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(squeezer)
    register_entity(enemy)
    spatial_service.sync_entity(squeezer)
    spatial_service.sync_entity(enemy)

    # 1. Extra movement cost (handled in validate_movement_handler)
    spatial_service.active_combatants["default"] = ["Squeezer"]
    move_event = GameEvent(
        event_type="Movement",
        source_uuid=squeezer.entity_uuid,
        payload={"target_x": 10.0, "target_y": 0.0, "movement_type": "walk"},
    )
    EventBus.dispatch(move_event)
    assert move_event.payload.get("cost", 0) == 20  # 10ft * 2

    # 2. Attacks against have Advantage
    atk1 = GameEvent(event_type="MeleeAttack", source_uuid=enemy.entity_uuid, target_uuid=squeezer.entity_uuid)
    EventBus.dispatch(atk1)
    assert atk1.payload.get("advantage") is True

    # 3. Attacks have Disadvantage
    atk2 = GameEvent(event_type="MeleeAttack", source_uuid=squeezer.entity_uuid, target_uuid=enemy.entity_uuid)
    EventBus.dispatch(atk2)
    assert atk2.payload.get("disadvantage") is True

    # 4. Disadvantage on DEX saves
    mechanics = {"save_required": "dexterity", "damage_dice": "1d6", "damage_type": "fire"}
    with mock_dice(6, 20, 2):  # dmg=6, Roll 20 and 2. Should take 2 if disadvantage.
        save_event = GameEvent(
            event_type="SpellCast",
            source_uuid=enemy.entity_uuid,
            payload={"ability_name": "Fire", "mechanics": mechanics, "target_uuids": [squeezer.entity_uuid]},
        )
        EventBus.dispatch(save_event)
        assert squeezer.hp.base_value < 10
        assert any("Failed Save (Rolled 2" in r for r in save_event.payload.get("results", []))


def test_req_geo_012_intervening_creatures():
    """
    REQ-GEO-012: Any creature between an attacker and a target provides Half Cover.
    """
    from dnd_rules_engine import RangedWeapon

    attacker = Creature(
        name="Archer",
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    bow = RangedWeapon(name="Bow", damage_dice="1d8", damage_type="piercing", normal_range=80, long_range=320)
    attacker.equipped_weapon_uuid = bow.entity_uuid
    register_entity(bow)
    target = Creature(
        name="Target",
        x=20.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    bystander = Creature(
        name="Bystander",
        x=10.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=10),
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

    # Verify spatial service natively returns the intervener
    interveners = spatial_service.get_intervening_creatures(attacker.entity_uuid, target.entity_uuid)
    assert bystander.entity_uuid in interveners

    # Verify attack pipeline natively applies Half Cover AC bonus
    atk = GameEvent(event_type="MeleeAttack", source_uuid=attacker.entity_uuid, target_uuid=target.entity_uuid)

    with patch("builtins.print") as mock_print:
        EventBus.dispatch(atk)
        alert_msg = "".join([str(call.args) for call in mock_print.call_args_list])
        assert "Applying Half Cover" in alert_msg
        assert "vs AC 12" in alert_msg  # 10 base + 2 Half Cover
