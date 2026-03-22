import pytest
import uuid
from unittest.mock import patch
from tools import (
    start_combat,
    execute_grapple_or_shove,
    perform_ability_check_or_save,
    modify_health,
)
from dnd_rules_engine import EventBus, GameEvent, Creature, ModifiableValue, ActiveCondition
from registry import register_entity


@pytest.mark.asyncio
async def test_req_cor_005_opposed_tests_ties_go_to_defender(mock_obsidian_vault):
    """
    [Mapped: REQ-COR-005]
    Tests that in contested checks like grapple, ties result in the defender winning.
    """
    vault_path = mock_obsidian_vault
    config = {"configurable": {"thread_id": vault_path}}

    attacker = Creature(
        name="Attacker",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    defender = Creature(
        name="Defender",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    # Patch randint so both roll a 10. Attacker = 10, Defender = 10. Tie!
    with patch("random.randint", side_effect=[10, 10, 10, 10]):
        res = await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Attacker", "target_name": "Defender", "action_type": "grapple"}, config=config
        )

    assert "Defender succeeds" in res
    assert "Nothing happens." in res


@pytest.mark.asyncio
async def test_req_cor_006_rounding_down(mock_obsidian_vault):
    """
    [Mapped: REQ-COR-006]
    Tests that math like resistance halves damage and rounds down.
    25 / 2 = 12.5 -> rounds down to 12.
    """
    vault_path = mock_obsidian_vault
    config = {"configurable": {"thread_id": vault_path}}

    target = Creature(
        name="ResistantTarget",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target.resistances.append("fire")

    # 25 fire damage, halved to 12
    res = await modify_health.ainvoke(
        {"target_name": "ResistantTarget", "hp_change": -25, "reason": "Fireball", "damage_type": "fire"}, config=config
    )

    assert "took 12 fire HP" in res
    assert target.hp.base_value == 18


@pytest.mark.asyncio
async def test_req_act_008_initiative_rolls(mock_obsidian_vault):
    """
    [Mapped: REQ-ACT-008]
    Tests that start_combat rolls 1d20 + dex_mod for initiative and sorts properly.
    """
    vault_path = mock_obsidian_vault
    config = {"configurable": {"thread_id": vault_path}}

    with patch("random.randint", side_effect=[10, 20]):
        # PC rolls 10, Enemy rolls 20
        res = await start_combat.ainvoke(
            {
                "pc_names": [],
                "enemies": [
                    {"name": "SlowEnemy", "hp": 10, "ac": 10, "dex_mod": 0},
                    {"name": "FastEnemy", "hp": 10, "ac": 10, "dex_mod": 5},
                ],
            },
            config=config,
        )

    assert "Combat started! FastEnemy goes first." in res


@pytest.mark.asyncio
async def test_req_act_003_simultaneous_effects_priority(mock_obsidian_vault):
    """
    [Mapped: REQ-ACT-003]
    Tests that the EventBus sorts subscribed handlers by priority to resolve simultaneous effects logically.
    """
    vault_path = mock_obsidian_vault
    execution_order = []

    def handler_low_priority(event):
        execution_order.append("Low")

    def handler_high_priority(event):
        execution_order.append("High")

    EventBus._listeners["TestEvent"] = []
    EventBus.subscribe("TestEvent", handler_low_priority, priority=100)
    EventBus.subscribe("TestEvent", handler_high_priority, priority=10)

    event = GameEvent(event_type="TestEvent", source_uuid=uuid.uuid4(), vault_path=vault_path)
    EventBus.dispatch(event)

    # A full cycle is 4 stages (Pre, Exec, Post, Resolved),
    # we expect the High Priority to trigger before Low on every stage.
    assert execution_order == ["High", "Low", "High", "Low", "High", "Low", "High", "Low"]


@pytest.mark.asyncio
async def test_req_spc_002_grappling_initiation(mock_obsidian_vault):
    """
    [Mapped: REQ-SPC-002]
    Tests that grappling correctly applies the Grappled condition to the target and reduces their speed.
    """
    vault_path = mock_obsidian_vault
    config = {"configurable": {"thread_id": vault_path}}

    attacker = Creature(
        name="Grappler",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=5),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    defender = Creature(
        name="Victim",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    defender.movement_remaining = 30

    with patch("random.randint", side_effect=[1, 1]):  # Defender rolls low
        res = await execute_grapple_or_shove.ainvoke(
            {"attacker_name": "Grappler", "target_name": "Victim", "action_type": "grapple"}, config=config
        )

    assert defender.movement_remaining == 0
    assert any(c.name.lower() == "grappled" for c in defender.active_conditions)


@pytest.mark.asyncio
async def test_req_cnd_003_deafened_condition_penalties(mock_obsidian_vault):
    """
    [Mapped: REQ-CND-003]
    Tests that the Deafened condition causes hearing-based perception checks to automatically fail (narratively enforced).
    """
    vault_path = mock_obsidian_vault
    config = {"configurable": {"thread_id": vault_path}}

    listener = Creature(
        name="Listener",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    listener.active_conditions.append(ActiveCondition(name="Deafened"))

    with patch("random.randint", return_value=15):
        res = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Listener", "skill_or_stat_name": "perception"}, config=config
        )

    assert "DEAFENED" in res
    assert "Hearing-based checks automatically fail" in res


@pytest.mark.asyncio
async def test_req_dth_007_death_saving_throws_base(mock_obsidian_vault):
    """
    [Mapped: REQ-DTH-007]
    Tests the base mechanics of death saving throws, ensuring that modify_health enforces damage failures properly.
    """
    vault_path = mock_obsidian_vault
    config = {"configurable": {"thread_id": vault_path}}

    target = Creature(
        name="DyingTarget",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=0),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    # Taking damage at 0 HP causes 1 failure
    res = await modify_health.ainvoke({"target_name": "DyingTarget", "hp_change": -5, "reason": "Bleed"}, config=config)

    assert "suffered 1 Death Save failure(s)" in res
    assert target.death_saves_failures == 1
