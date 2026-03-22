"""
Underwater and suffocation rules tests.
REQ-ENV-004: Underwater melee Disadvantage (unless weapon is underwater_safe or has swimming_speed)
REQ-ENV-005: Underwater ranged Disadvantage (unless weapon is underwater_safe)
REQ-ENV-006: Submerged entities gain Fire resistance
REQ-ENV-007: Suffocation — breath hold tracking
REQ-ENV-008: Choking — HP drops to 0 when choking rounds expire
"""
import pytest

from dnd_rules_engine import (
    Creature, ModifiableValue, MeleeWeapon, RangedWeapon, WeaponProperty,
    GameEvent, EventBus,
)
from registry import clear_registry, register_entity
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield mock_obsidian_vault


def _make_pair(vault_path, attacker_tags=None, target_tags=None):
    # Always include darkvision to suppress "unseen" adv/disadv interference
    attacker = Creature(
        name="Diver",
        vault_path=vault_path,
        tags=(attacker_tags or []) + ["darkvision"],
        x=0.0, y=0.0, size=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=2),
        constitution_mod=ModifiableValue(base_value=2),
    )
    target = Creature(
        name="Shark",
        vault_path=vault_path,
        tags=(target_tags or []) + ["darkvision"],
        x=5.0, y=0.0, size=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=8),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(attacker)
    register_entity(target)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    return attacker, target


def _attach_melee(attacker, vault_path, properties=None):
    w = MeleeWeapon(
        name="Longsword",
        damage_dice="1d8",
        damage_type="slashing",
        properties=properties or [],
        vault_path=vault_path,
    )
    register_entity(w)
    attacker.equipped_weapon_uuid = w.entity_uuid
    return w


def _attach_ranged(attacker, vault_path, properties=None):
    w = RangedWeapon(
        name="Shortbow",
        damage_dice="1d6",
        damage_type="piercing",
        normal_range=80,
        long_range=320,
        properties=properties or [],
        vault_path=vault_path,
    )
    register_entity(w)
    attacker.equipped_weapon_uuid = w.entity_uuid
    return w


# ============================================================
# REQ-ENV-004: Underwater Melee Disadvantage
# ============================================================

def test_req_env_004_underwater_melee_applies_disadvantage(setup):
    """REQ-ENV-004: Underwater attacker with non-safe melee weapon gets Disadvantage."""
    vault_path = setup
    attacker, target = _make_pair(vault_path, attacker_tags=["underwater"])
    _attach_melee(attacker, vault_path)

    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={},
    )
    EventBus.dispatch(event)
    assert event.payload.get("disadvantage") is True


def test_req_env_004_underwater_safe_weapon_no_disadvantage(setup):
    """REQ-ENV-004: Underwater attacker with an underwater_safe weapon has no Disadvantage."""
    vault_path = setup
    attacker, target = _make_pair(vault_path, attacker_tags=["underwater"])
    _attach_melee(attacker, vault_path, properties=[WeaponProperty.UNDERWATER_SAFE])

    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={},
    )
    EventBus.dispatch(event)
    assert not event.payload.get("disadvantage")


def test_req_env_004_swimming_speed_negates_disadvantage(setup):
    """REQ-ENV-004: Attacker with swimming_speed tag has no Disadvantage underwater."""
    vault_path = setup
    attacker, target = _make_pair(vault_path, attacker_tags=["underwater", "swimming_speed"])
    _attach_melee(attacker, vault_path)

    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={},
    )
    EventBus.dispatch(event)
    assert not event.payload.get("disadvantage")


def test_req_env_004_dry_land_no_penalty(setup):
    """REQ-ENV-004: Non-underwater attacker has no underwater penalty."""
    vault_path = setup
    attacker, target = _make_pair(vault_path)  # no underwater tag
    _attach_melee(attacker, vault_path)

    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={},
    )
    EventBus.dispatch(event)
    assert not event.payload.get("disadvantage")


# ============================================================
# REQ-ENV-005: Underwater Ranged Disadvantage
# ============================================================

def test_req_env_005_underwater_ranged_disadvantage(setup):
    """REQ-ENV-005: Underwater attacker with non-safe ranged weapon gets Disadvantage."""
    vault_path = setup
    attacker, target = _make_pair(vault_path, attacker_tags=["underwater"])
    # Place target within normal range
    target.x = 20.0
    spatial_service.sync_entity(target)
    _attach_ranged(attacker, vault_path)

    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={},
    )
    EventBus.dispatch(event)
    assert event.payload.get("disadvantage") is True


def test_req_env_005_underwater_safe_ranged_no_disadvantage(setup):
    """REQ-ENV-005: Underwater attacker with underwater_safe ranged weapon has no extra Disadvantage."""
    vault_path = setup
    attacker, target = _make_pair(vault_path, attacker_tags=["underwater"])
    target.x = 20.0
    spatial_service.sync_entity(target)
    _attach_ranged(attacker, vault_path, properties=[WeaponProperty.UNDERWATER_SAFE])

    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={},
    )
    EventBus.dispatch(event)
    assert not event.payload.get("disadvantage")


# ============================================================
# REQ-ENV-006: Submerged Fire Resistance
# ============================================================

def test_req_env_006_submerged_fire_resistance(setup):
    """REQ-ENV-006: Submerged entities take half fire damage."""
    vault_path = setup
    attacker, target = _make_pair(vault_path, target_tags=["submerged"])

    event = GameEvent(
        event_type="ApplyDamage",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={"damage": 20, "damage_type": "fire"},
    )
    EventBus.dispatch(event)

    # 20 fire damage halved to 10
    assert target.hp.base_value == 20, f"Expected 20 HP (30-10), got {target.hp.base_value}"


def test_req_env_006_submerged_non_fire_full_damage(setup):
    """REQ-ENV-006: Submerged entities take full damage from non-fire sources."""
    vault_path = setup
    attacker, target = _make_pair(vault_path, target_tags=["submerged"])

    event = GameEvent(
        event_type="ApplyDamage",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={"damage": 10, "damage_type": "slashing"},
    )
    EventBus.dispatch(event)

    assert target.hp.base_value == 20, f"Expected 20 HP (30-10), got {target.hp.base_value}"


# ============================================================
# REQ-ENV-007: Suffocation — Breath Hold Tracking
# ============================================================

def test_req_env_007_breath_hold_decrements_each_turn(setup):
    """REQ-ENV-007: Breath Hold decrements by 1 each StartOfTurn while submerged."""
    vault_path = setup
    entity = Creature(
        name="Swimmer",
        vault_path=vault_path,
        tags=["underwater"],
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=2),  # CON +2 → max hold = (1+2)*10 = 30
    )
    register_entity(entity)

    # First turn: initializes at 30/30, decrements to 29
    event = GameEvent(event_type="StartOfTurn", source_uuid=entity.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(event)

    assert "Breath Hold" in entity.resources
    hb = entity.resources["Breath Hold"]
    current, maximum = map(int, hb.split("/"))
    assert maximum == 30
    assert current == 29


def test_req_env_007_water_breathing_skips_suffocation(setup):
    """REQ-ENV-007: Entity with water_breathing tag is unaffected by submersion."""
    vault_path = setup
    entity = Creature(
        name="Merfolk",
        vault_path=vault_path,
        tags=["underwater", "water_breathing"],
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=2),
    )
    register_entity(entity)

    event = GameEvent(event_type="StartOfTurn", source_uuid=entity.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(event)

    assert "Breath Hold" not in entity.resources


# ============================================================
# REQ-ENV-008: Choking — Death After Breath Runs Out
# ============================================================

def test_req_env_008_choking_starts_when_breath_runs_out(setup):
    """REQ-ENV-008: When Breath Hold hits 0, Choking Rounds resource is initialized."""
    vault_path = setup
    entity = Creature(
        name="Swimmer",
        vault_path=vault_path,
        tags=["underwater"],
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=1),  # CON +1 → max choke = 2
    )
    entity.resources["Breath Hold"] = "0/5"  # already at 0
    register_entity(entity)

    event = GameEvent(event_type="StartOfTurn", source_uuid=entity.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(event)

    assert "Choking Rounds" in entity.resources
    choke = entity.resources["Choking Rounds"]
    current, maximum = map(int, choke.split("/"))
    assert maximum == 2  # max(1, 1+1)
    assert current == 2


def test_req_env_008_death_when_choking_rounds_expire(setup):
    """REQ-ENV-008: When choking rounds hit 0, entity drops to 0 HP and gains Dying."""
    vault_path = setup
    entity = Creature(
        name="Swimmer",
        vault_path=vault_path,
        tags=["underwater"],
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
    )
    entity.resources["Breath Hold"] = "0/5"
    entity.resources["Choking Rounds"] = "0/1"  # already on last round
    register_entity(entity)

    event = GameEvent(event_type="StartOfTurn", source_uuid=entity.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(event)

    assert entity.hp.base_value == 0
    assert any(c.name == "Dying" for c in entity.active_conditions)
