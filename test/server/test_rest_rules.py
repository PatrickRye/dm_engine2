"""
Tests for rest rules.
REQ-RST-001: Short Rest — spend Hit Dice to regain HP (dice + CON mod, capped at max HP)
"""
import pytest

from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus
from registry import register_entity, clear_registry
from spatial_engine import spatial_service
from tools import take_rest


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _make_fighter(vault_path, current_hp=10, max_hp=40, con_mod=2, hd_current=5, hd_max=8, hd_size=10):
    """Create a fighter with Hit Dice resource."""
    c = Creature(
        name="Fighter",
        vault_path=vault_path,
        x=0, y=0,
        hp=ModifiableValue(base_value=current_hp),
        ac=ModifiableValue(base_value=16),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=1),
        constitution_mod=ModifiableValue(base_value=con_mod),
    )
    c.max_hp = max_hp
    c.resources[f"Hit Dice (d{hd_size})"] = f"{hd_current}/{hd_max}"
    register_entity(c)
    return c


# ============================================================
# REQ-RST-001: Short Rest Hit Dice Spending
# ============================================================

@pytest.mark.asyncio
async def test_req_rst_001_short_rest_hit_dice_healing(setup, mock_roll_dice):
    """
    REQ-RST-001: Spending Hit Dice during Short Rest rolls dice + CON modifier and heals HP.
    """
    vault_path = setup
    fighter = _make_fighter(vault_path, current_hp=10, max_hp=40, con_mod=2, hd_current=5, hd_max=8, hd_size=10)
    config = {"configurable": {"thread_id": vault_path}}

    # Roll returns 6 per die; 2 dice → (6+2) + (6+2) = 16 HP healed
    with mock_roll_dice(6, 6):
        await take_rest.ainvoke(
            {"character_names": ["Fighter"], "rest_type": "short", "hit_dice_to_spend": 2},
            config=config,
        )

    assert fighter.hp.base_value == 26, f"Expected 26 HP (10+16), got {fighter.hp.base_value}"
    # 5 - 2 = 3 Hit Dice remaining
    assert fighter.resources["Hit Dice (d10)"] == "3/8", f"Unexpected Hit Dice: {fighter.resources['Hit Dice (d10)']}"


@pytest.mark.asyncio
async def test_req_rst_001_healing_capped_at_max_hp(setup, mock_roll_dice):
    """
    REQ-RST-001: Healing from Hit Dice cannot exceed max HP.
    """
    vault_path = setup
    fighter = _make_fighter(vault_path, current_hp=38, max_hp=40, con_mod=2, hd_current=5, hd_max=8, hd_size=10)
    config = {"configurable": {"thread_id": vault_path}}

    # Roll returns 10; (10+2) = 12 heal, but only 2 HP headroom
    with mock_roll_dice(10):
        await take_rest.ainvoke(
            {"character_names": ["Fighter"], "rest_type": "short", "hit_dice_to_spend": 1},
            config=config,
        )

    assert fighter.hp.base_value == 40, f"Expected HP capped at 40, got {fighter.hp.base_value}"


@pytest.mark.asyncio
async def test_req_rst_001_cannot_spend_more_than_available(setup, mock_roll_dice):
    """
    REQ-RST-001: Cannot spend more Hit Dice than available — clamped to remaining count.
    """
    vault_path = setup
    fighter = _make_fighter(vault_path, current_hp=10, max_hp=40, con_mod=2, hd_current=2, hd_max=8, hd_size=10)
    config = {"configurable": {"thread_id": vault_path}}

    # Request 5 dice but only 2 available; rolls 2 dice at 4 each → (4+2)*2 = 12 HP
    with mock_roll_dice(4, 4):
        await take_rest.ainvoke(
            {"character_names": ["Fighter"], "rest_type": "short", "hit_dice_to_spend": 5},
            config=config,
        )

    assert fighter.hp.base_value == 22, f"Expected 22 HP (10+12), got {fighter.hp.base_value}"
    assert fighter.resources["Hit Dice (d10)"] == "0/8", f"Expected 0 Hit Dice, got {fighter.resources['Hit Dice (d10)']}"


@pytest.mark.asyncio
async def test_req_rst_001_zero_dice_no_healing(setup):
    """
    REQ-RST-001: Spending 0 Hit Dice during a Short Rest leaves HP unchanged.
    """
    vault_path = setup
    fighter = _make_fighter(vault_path, current_hp=10, max_hp=40, con_mod=2, hd_current=5, hd_max=8, hd_size=10)
    config = {"configurable": {"thread_id": vault_path}}

    await take_rest.ainvoke(
        {"character_names": ["Fighter"], "rest_type": "short", "hit_dice_to_spend": 0},
        config=config,
    )

    assert fighter.hp.base_value == 10, f"Expected HP unchanged at 10, got {fighter.hp.base_value}"
    assert fighter.resources["Hit Dice (d10)"] == "5/8"


@pytest.mark.asyncio
async def test_req_rst_001_long_rest_restores_half_hit_dice(setup):
    """
    Long Rest restores half max Hit Dice (rounds up minimum 1), consistent with existing behavior.
    """
    vault_path = setup
    fighter = _make_fighter(vault_path, current_hp=10, max_hp=40, con_mod=2, hd_current=2, hd_max=8, hd_size=10)
    config = {"configurable": {"thread_id": vault_path}}

    await take_rest.ainvoke(
        {"character_names": ["Fighter"], "rest_type": "long"},
        config=config,
    )

    # HP restored to max
    assert fighter.hp.base_value == 40
    # Hit Dice restored by max(1, 8//2) = 4: 2 + 4 = 6
    assert fighter.resources["Hit Dice (d10)"] == "6/8", f"Got: {fighter.resources['Hit Dice (d10)']}"
