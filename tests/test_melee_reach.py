import pytest
from fastapi.testclient import TestClient
from main import app
from dnd_rules_engine import Creature, ModifiableValue, MeleeWeapon
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import _calculate_reach, execute_melee_attack

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture(autouse=True)
def setup_engine_state():
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield

def test_calculate_reach_permutations():
    # Base 5ft
    c = Creature(name="Test", x=0, y=0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    assert _calculate_reach(c, is_active_turn=True) == 5.0
    assert _calculate_reach(c, is_active_turn=False) == 5.0
    
    # Bugbear (+5 on active turn ONLY)
    c.tags = ["bugbear"]
    assert _calculate_reach(c, is_active_turn=True) == 10.0
    assert _calculate_reach(c, is_active_turn=False) == 5.0
    
    # Bugbear + Halberd (+5 on active, +5 always)
    c.tags = ["bugbear", "halberd"]
    assert _calculate_reach(c, is_active_turn=True) == 15.0
    assert _calculate_reach(c, is_active_turn=False) == 10.0
    
    # Bugbear + Halberd + Giant Stature
    c.tags = ["bugbear", "halberd", "giant_stature"]
    assert _calculate_reach(c, is_active_turn=True) == 20.0
    assert _calculate_reach(c, is_active_turn=False) == 15.0

@pytest.mark.asyncio
async def test_execute_melee_attack_reach_enforcement():
    attacker = Creature(name="Attacker", x=0.0, y=0.0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    target = Creature(name="Target", x=10.0, y=0.0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
    
    weapon = MeleeWeapon(name="Fists", damage_dice="1d4", damage_type="bludgeoning")
    register_entity(weapon)
    attacker.equipped_weapon_uuid = weapon.entity_uuid
    
    register_entity(attacker)
    register_entity(target)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    
    config = {"configurable": {"thread_id": "/mock/vault"}}
    
    # Dist = 10. Base reach = 5. Should fail.
    res = await execute_melee_attack.ainvoke({"attacker_name": "Attacker", "target_name": "Target"}, config=config)
    assert "out of range" in res
    
    # Add Halberd. Reach = 10. Should proceed.
    attacker.tags = ["halberd"]
    res = await execute_melee_attack.ainvoke({"attacker_name": "Attacker", "target_name": "Target"}, config=config)
    assert "out of range" not in res

    # Bugbear attacking at 10ft. Should pass on active turn.
    attacker.tags = ["bugbear"]
    res = await execute_melee_attack.ainvoke({"attacker_name": "Attacker", "target_name": "Target"}, config=config)
    assert "out of range" not in res
    
    # Bugbear doing Opportunity Attack at 10ft. is_opportunity_attack sets is_active_turn=False. Reach = 5. Should fail.
    res = await execute_melee_attack.ainvoke({"attacker_name": "Attacker", "target_name": "Target", "is_opportunity_attack": True}, config=config)
    assert "out of range" in res

def test_propose_move_opportunity_attack_reach(client):
    player = Creature(name="Player", x=0.0, y=0.0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=2))
    player.movement_remaining = 30.0
    
    # Enemy with a Halberd (10ft reach)
    enemy = Creature(name="Enemy", x=15.0, y=0.0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=12), strength_mod=ModifiableValue(base_value=1), dexterity_mod=ModifiableValue(base_value=1))
    enemy.tags = ["halberd"]
    
    register_entity(player)
    register_entity(enemy)
    spatial_service.sync_entity(player)
    spatial_service.sync_entity(enemy)
    
    # Player moves from (0,0) to (5,0). Distance to enemy goes from 15 to 10.
    # Enemy reach is 10. Player is entering reach, not leaving. No OA.
    req1 = {"entity_name": "Player", "waypoints": [[0.0, 0.0], [5.0, 0.0]], "vault_path": "/mock/vault"}
    res1 = client.post("/propose_move", json=req1)
    assert "Enemy" not in res1.json()["opportunity_attacks"]
    
    # Player is at (5,0) [dist 10]. Moves to (0,0) [dist 15].
    # Leaving 10ft reach -> Triggers OA!
    player.x = 5.0
    spatial_service.sync_entity(player)
    req2 = {"entity_name": "Player", "waypoints": [[5.0, 0.0], [0.0, 0.0]], "vault_path": "/mock/vault"}
    res2 = client.post("/propose_move", json=req2)
    assert "Enemy" in res2.json()["opportunity_attacks"]
    
    # Reset and test Bugbear Enemy
    # Bugbear reach is 5 on OA (long-limbed doesn't apply)
    enemy.tags = ["bugbear"]
    # Player moves from 5 to 0 (dist 10 -> 15). Reach is 5. Doesn't trigger because was already out of reach at 10.
    res3 = client.post("/propose_move", json=req2)
    assert "Enemy" not in res3.json()["opportunity_attacks"]
    
def test_large_creature_reach(client):
    # Medium player
    player = Creature(name="Player", x=0.0, y=0.0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=15), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=2))
    player.movement_remaining = 30.0
    
    # Large enemy (size 10.0), no reach weapons. Base reach = 5. Effective center-to-center distance boundary = 5 + (10-5)/2 = 7.5
    enemy = Creature(name="Enemy", x=12.5, y=0.0, size=10.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=12), strength_mod=ModifiableValue(base_value=1), dexterity_mod=ModifiableValue(base_value=1))
    
    register_entity(player)
    register_entity(enemy)
    
    # Moving outside the center-to-center threshold of the Large creature triggers OA!
    req = {"entity_name": "Player", "waypoints": [[5.0, 0.0], [0.0, 0.0]], "vault_path": "/mock/vault"}
    res = client.post("/propose_move", json=req)
    assert "Enemy" in res.json()["opportunity_attacks"]