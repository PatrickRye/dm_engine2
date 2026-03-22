"""
Long rest tests.
REQ-RST-004: Long Rest — restores all HP, resets class daily resources,
             and recovers Hit Dice up to half the entity's maximum (minimum 1).
"""
import pytest

from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus
from registry import clear_registry, register_entity
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _dispatch_long_rest(entity, vault_path):
    event = GameEvent(
        event_type="Rest",
        source_uuid=entity.entity_uuid,
        vault_path=vault_path,
        payload={
            "rest_type": "long",
            "target_uuids": [entity.entity_uuid],
            "hit_dice_to_spend": 0,
        },
    )
    EventBus.dispatch(event)


# ============================================================
# REQ-RST-004: Long rest recovers HP
# ============================================================

def test_req_rst_004_long_rest_restores_hp(setup):
    """REQ-RST-004: Long rest restores all HP."""
    vp = setup
    entity = Creature(
        name="Fighter",
        vault_path=vp,
        hp=ModifiableValue(base_value=30),
        max_hp=60,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    entity.hp.base_value = 10  # damaged
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    assert entity.hp.base_value == 60


def test_req_rst_004_long_rest_resets_daily_resources(setup):
    """REQ-RST-004: Long rest resets all non-hit-dice resources to their maximum."""
    vp = setup
    entity = Creature(
        name="Wizard",
        vault_path=vp,
        hp=ModifiableValue(base_value=40),
        max_hp=40,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Spell Slots (1st)": "1/4",  # partially spent
            "Spell Slots (2nd)": "0/3",  # fully spent
            "Arcane Recovery": "0/1",
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    assert entity.resources["Spell Slots (1st)"] == "4/4"
    assert entity.resources["Spell Slots (2nd)"] == "3/3"
    assert entity.resources["Arcane Recovery"] == "1/1"


def test_req_rst_004_long_rest_recovers_hit_dice_up_to_half_max(setup):
    """REQ-RST-004: Long rest recovers Hit Dice up to half the entity's maximum (min 1)."""
    vp = setup
    entity = Creature(
        name="Barbarian",
        vault_path=vp,
        hp=ModifiableValue(base_value=80),
        max_hp=80,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Hit Dice (d12)": "2/8",  # spent 6, have 2 of 8
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    # Recover max(1, 8//2) = 4 dice. New total = min(8, 2+4) = 6.
    assert entity.resources["Hit Dice (d12)"] == "6/8"


def test_req_rst_004_long_rest_hit_dice_capped_at_max(setup):
    """REQ-RST-004: Long rest does not exceed max Hit Dice."""
    vp = setup
    entity = Creature(
        name="Rogue",
        vault_path=vp,
        hp=ModifiableValue(base_value=50),
        max_hp=50,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Hit Dice (d8)": "5/6",  # only 1 spent, recovery would be 3 but capped at 6
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    # Recover max(1, 6//2) = 3. New total = min(6, 5+3) = 6.
    assert entity.resources["Hit Dice (d8)"] == "6/6"


def test_req_rst_004_long_rest_minimum_one_hit_die_recovered(setup):
    """REQ-RST-004: Long rest always recovers at least 1 Hit Die even at level 1."""
    vp = setup
    entity = Creature(
        name="Peasant",
        vault_path=vp,
        hp=ModifiableValue(base_value=8),
        max_hp=8,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        resources={
            "Hit Dice (d8)": "0/1",  # 1 die total, 0 remaining
        },
    )
    register_entity(entity)

    _dispatch_long_rest(entity, vp)

    # max(1, 1//2) = max(1, 0) = 1. New total = min(1, 0+1) = 1.
    assert entity.resources["Hit Dice (d8)"] == "1/1"
