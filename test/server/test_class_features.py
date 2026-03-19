import pytest
import os
from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus, ActiveCondition
from registry import register_entity, clear_registry
from tools import level_up_character, modify_health
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup_engine_and_vault(mock_obsidian_vault):
    """Clears registries and provides an isolated temporary vault for testing."""
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_ent_001_multiclass_minimums(setup_engine_and_vault):
    """
    REQ-ENT-001: Multiclassing Minimums.
    Fails if the character doesn't have 13 in the target class's primary stat,
    or 13 in their current class's primary stat.
    """
    vault_path = setup_engine_and_vault
    from vault_io import get_journals_dir

    char_md = os.path.join(get_journals_dir(vault_path), "Fighter.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write("---\nclasses: [{class_name: Fighter, level: 1}]\nstrength: 15\ndexterity: 10\nintelligence: 10\n---\n")

    c = Creature(
        name="Fighter",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=2),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(c)

    config = {"configurable": {"thread_id": vault_path}}

    # 1. Try multiclassing into Wizard with Int 10 (Fails)
    res = await level_up_character.ainvoke(
        {"character_name": "Fighter", "class_name": "Wizard", "hp_increase": 4}, config=config
    )
    assert "SYSTEM ERROR" in res
    assert "target class 'Wizard'" in res

    # 2. Try multiclassing into Barbarian with Str 15 (Succeeds)
    res2 = await level_up_character.ainvoke(
        {"character_name": "Fighter", "class_name": "Barbarian", "hp_increase": 7}, config=config
    )
    assert "Success" in res2


@pytest.mark.asyncio
async def test_req_ent_003_hit_dice_pools(setup_engine_and_vault):
    """
    REQ-ENT-003: Hit Dice Pools.
    Long rests restore exactly half of maximum Hit Dice (minimum 1).
    """
    vault_path = setup_engine_and_vault
    c = Creature(
        name="Ranger",
        vault_path=vault_path,
        max_hp=40,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={"Hit Dice (d10)": "0/4"},
    )
    register_entity(c)

    # Rest event
    event = GameEvent(
        event_type="Rest",
        source_uuid=c.entity_uuid,
        payload={"rest_type": "long", "target_uuids": [c.entity_uuid]},
    )
    EventBus.dispatch(event)

    # Max is 4, Half is 2. 0 + 2 = 2.
    assert c.resources["Hit Dice (d10)"] == "2/4"


@pytest.mark.asyncio
async def test_req_cls_013_014_wild_shape_hp_buffer(setup_engine_and_vault):
    """
    REQ-CLS-013, REQ-CLS-014: Wild Shape HP Buffer & Reversion.
    Transforms grant a separate pool of HP. Damage subtracts from WS_HP first.
    If WS_HP <= 0, excess subtracts from normal form HP and reverts.
    """
    vault_path = setup_engine_and_vault
    config = {"configurable": {"thread_id": vault_path}}

    druid = Creature(
        name="Druid",
        vault_path=vault_path,
        max_hp=30,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=14),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        wild_shape_hp=20,
        wild_shape_max_hp=20,
        active_conditions=[ActiveCondition(name="Wild Shape")],
    )
    register_entity(druid)

    # 1. Take 25 damage -> Absorbs 20, reverts, Base takes 5
    await modify_health.ainvoke({"target_name": "Druid", "hp_change": -25, "reason": "Bite"}, config=config)
    assert druid.wild_shape_hp == 0
    assert druid.hp.base_value == 25
    assert not any(c.name == "Wild Shape" for c in druid.active_conditions)


def test_req_cls_002_reckless_attack(setup_engine_and_vault):
    """
    REQ-CLS-002: Reckless Attack.
    Grants Advantage on outgoing attacks and incoming attacks.
    """
    barb = Creature(
        name="Barbarian",
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=14),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Reckless")],
    )
    enemy = Creature(
        name="Enemy",
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=14),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(barb)
    register_entity(enemy)

    # Barbarian attacks (Advantage)
    atk1 = GameEvent(event_type="MeleeAttack", source_uuid=barb.entity_uuid, target_uuid=enemy.entity_uuid)
    EventBus.dispatch(atk1)
    assert atk1.payload.get("advantage") is True

    # Enemy attacks Barbarian (Advantage)
    atk2 = GameEvent(event_type="MeleeAttack", source_uuid=enemy.entity_uuid, target_uuid=barb.entity_uuid)
    EventBus.dispatch(atk2)
    assert atk2.payload.get("advantage") is True
