import os
import pytest
from unittest.mock import patch

from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service
from registry import clear_registry, register_entity, get_all_entities
from tools import manage_mount, execute_melee_attack, move_entity, toggle_condition, start_combat
from vault_io import get_journals_dir


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _create_pair(vault_path):
    j_dir = get_journals_dir(vault_path)
    os.makedirs(j_dir, exist_ok=True)
    with open(os.path.join(j_dir, "Rider.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: Rider\ntags: [pc]\n---")
    with open(os.path.join(j_dir, "Horse.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: Horse\ntags: [npc]\n---")

    rider = Creature(
        name="Rider",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
        movement_remaining=30,
        x=0.0,
        y=0.0,
    )
    horse = Creature(
        name="Horse",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=60,
        movement_remaining=60,
        x=5.0,
        y=0.0,
    )
    register_entity(rider)
    register_entity(horse)
    spatial_service.sync_entity(rider)
    spatial_service.sync_entity(horse)
    return rider, horse


@pytest.mark.asyncio
async def test_req_mnt_001_mounting_costs_half_speed(setup):
    """
    REQ-MNT-001: Mounting or dismounting costs half the entity's speed.
    """
    vp = setup
    config = {"configurable": {"thread_id": vp}}
    rider, horse = _create_pair(vp)

    res_mount = await manage_mount.ainvoke({"rider_name": "Rider", "mount_name": "Horse", "action": "mount"}, config=config)
    assert "MECHANICAL TRUTH" in res_mount
    assert "15ft" in res_mount
    assert rider.movement_remaining == 15
    assert rider.mounted_on_uuid == horse.entity_uuid
    assert rider.x == horse.x

    res_dismount = await manage_mount.ainvoke({"rider_name": "Rider", "action": "dismount"}, config=config)
    assert "MECHANICAL TRUTH" in res_dismount
    assert "15ft" in res_dismount
    assert rider.movement_remaining == 0
    assert rider.mounted_on_uuid is None


@pytest.mark.asyncio
async def test_req_mnt_002_controlled_mount_initiative(setup, mock_dice):
    """
    REQ-MNT-002: Controlled mount's initiative changes to match rider.
    """
    vp = setup
    config = {"configurable": {"thread_id": vp}}
    rider, horse = _create_pair(vp)

    # Start combat to create the ACTIVE_COMBAT file
    with mock_dice(15, 5):  # Rider 15, Horse 5
        await start_combat.ainvoke(
            {"pc_names": ["Rider"], "enemies": [{"name": "Horse", "hp": 30, "ac": 10, "dex_mod": 0}]}, config=config
        )

    # Mount it (Controlled)
    await manage_mount.ainvoke(
        {"rider_name": "Rider", "mount_name": "Horse", "action": "mount", "is_controlled": True}, config=config
    )

    # Verify initiative was updated to match the rider
    import yaml

    with open(os.path.join(get_journals_dir(vp), "ACTIVE_COMBAT.md"), "r", encoding="utf-8") as f:
        data = yaml.safe_load(f.read().split("---")[1])
        combatants = {c["name"]: c["init"] for c in data["combatants"]}

        assert combatants["Horse"] == combatants["Rider"]


@pytest.mark.asyncio
async def test_req_mnt_003_controlled_mount_actions(setup):
    """
    REQ-MNT-003: Controlled mounts can ONLY Dash, Disengage, or Dodge. Cannot attack.
    """
    vp = setup
    config = {"configurable": {"thread_id": vp}}
    rider, horse = _create_pair(vp)

    await manage_mount.ainvoke(
        {"rider_name": "Rider", "mount_name": "Horse", "action": "mount", "is_controlled": True}, config=config
    )

    # Test attack block
    target = Creature(
        name="Target",
        vault_path=vp,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    res = await execute_melee_attack.ainvoke({"attacker_name": "Horse", "target_name": "Target"}, config=config)

    assert "SYSTEM ERROR" in res
    assert "REQ-MNT-003" in res
    assert "ONLY take the Dash" in res


@pytest.mark.asyncio
async def test_req_mnt_004_independent_mount(setup):
    """
    REQ-MNT-004: Independent mount retains initiative and can attack freely.
    """
    vp = setup
    config = {"configurable": {"thread_id": vp}}
    rider, dragon = _create_pair(vp)
    dragon.name = "Dragon"

    await manage_mount.ainvoke(
        {"rider_name": "Rider", "mount_name": "Dragon", "action": "mount", "is_controlled": False}, config=config
    )

    assert "independent_mount" in dragon.tags
    assert "controlled_mount" not in dragon.tags

    # Test attack allowed
    target = Creature(
        name="Target",
        vault_path=vp,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    res = await execute_melee_attack.ainvoke({"attacker_name": "Dragon", "target_name": "Target"}, config=config)
    assert "SYSTEM ERROR" not in res


@pytest.mark.asyncio
async def test_req_mnt_005_forced_dismount(setup):
    """
    REQ-MNT-005: Forced movement or rider prone requires a DC 10 Dex Save to stay mounted.
    """
    vp = setup
    config = {"configurable": {"thread_id": vp}}
    rider, horse = _create_pair(vp)

    await manage_mount.ainvoke({"rider_name": "Rider", "mount_name": "Horse"}, config=config)

    # Test 1: Forced Movement on Mount
    with patch("random.randint", return_value=5):  # Roll 5 + 0 Dex = 5 (Fail vs DC 10)
        res_forced = await move_entity.ainvoke(
            {"entity_name": "Horse", "target_x": 15.0, "target_y": 0.0, "movement_type": "forced"}, config=config
        )

    assert "REQ-MNT-005" in res_forced
    assert "failed DC 10 Dex save" in res_forced
    assert rider.mounted_on_uuid is None
    assert any(c.name == "Prone" for c in rider.active_conditions)

    # Reset and Re-mount
    rider.active_conditions.clear()
    rider.movement_remaining = 30
    rider.x = horse.x
    rider.y = horse.y
    spatial_service.sync_entity(rider)
    res_remount = await manage_mount.ainvoke({"rider_name": "Rider", "mount_name": "Horse"}, config=config)
    assert "SYSTEM ERROR" not in res_remount

    # Test 2: Rider gets knocked Prone
    with patch("random.randint", return_value=15):  # Roll 15 + 0 = 15 (Success)
        res_prone = await toggle_condition.ainvoke(
            {"character_name": "Rider", "condition_name": "Prone", "is_active": True}, config=config
        )

    assert "REQ-MNT-005" in res_prone
    assert "succeeded the DC 10 Dex save" in res_prone
    assert rider.mounted_on_uuid == horse.entity_uuid  # Stayed on!

    with patch("random.randint", return_value=2):  # Fail
        res_prone_fail = await toggle_condition.ainvoke(
            {"character_name": "Rider", "condition_name": "Prone", "is_active": True}, config=config
        )

    assert "fell off" in res_prone_fail
    assert rider.mounted_on_uuid is None


@pytest.mark.asyncio
async def test_req_mnt_006_mount_falling_prone(setup):
    """
    REQ-MNT-006: Mount falling Prone requires the rider to use a Reaction to land on their feet,
    otherwise they also fall Prone.
    """
    vp = setup
    config = {"configurable": {"thread_id": vp}}
    rider, horse = _create_pair(vp)

    await manage_mount.ainvoke({"rider_name": "Rider", "mount_name": "Horse"}, config=config)

    # Test 1: Rider has Reaction available
    assert rider.reaction_used is False
    res1 = await toggle_condition.ainvoke(
        {"character_name": "Horse", "condition_name": "Prone", "is_active": True}, config=config
    )

    assert "REQ-MNT-006" in res1
    assert "used their Reaction to safely dismount" in res1
    assert rider.mounted_on_uuid is None
    assert rider.reaction_used is True
    assert not any(c.name == "Prone" for c in rider.active_conditions)

    # Reset
    horse.active_conditions.clear()
    rider.movement_remaining = 30
    res_remount = await manage_mount.ainvoke({"rider_name": "Rider", "mount_name": "Horse"}, config=config)
    assert "SYSTEM ERROR" not in res_remount

    # Test 2: Rider does NOT have a reaction
    rider.reaction_used = True
    res2 = await toggle_condition.ainvoke(
        {"character_name": "Horse", "condition_name": "Prone", "is_active": True}, config=config
    )

    assert "REQ-MNT-006" in res2
    assert "no Reaction available and fell Prone" in res2
    assert rider.mounted_on_uuid is None
    assert any(c.name == "Prone" for c in rider.active_conditions)


@pytest.mark.asyncio
async def test_spatial_engine_moves_riders_along_with_mounts(setup):
    """Verifies that moving a mount physically updates the rider's coordinates on the map."""
    vp = setup
    config = {"configurable": {"thread_id": vp}}
    rider, horse = _create_pair(vp)

    await manage_mount.ainvoke({"rider_name": "Rider", "mount_name": "Horse"}, config=config)

    assert rider.x == 5.0  # Snapped to mount

    await move_entity.ainvoke(
        {"entity_name": "Horse", "target_x": 25.0, "target_y": 10.0, "movement_type": "walk"}, config=config
    )

    assert horse.x == 25.0
    assert rider.x == 25.0
    assert rider.y == 10.0
