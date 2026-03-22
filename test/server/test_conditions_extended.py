"""
Extended condition requirement tests.
Tests conditions that were not previously covered by the test suite.
"""
import pytest
from unittest.mock import patch

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
    MeleeWeapon,
    RangedWeapon,
    ActiveCondition,
    WeaponProperty,
)
from spatial_engine import spatial_service
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup_system():
    """Clear registries and spatial indexes before each test."""
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


def _make_combatants(attacker_x=0.0, attacker_y=0.0, target_x=0.0, target_y=0.0):
    """Helper: create a basic attacker/target pair with a sword."""
    attacker = Creature(
        name="Attacker",
        x=attacker_x,
        y=attacker_y,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Target",
        x=target_x,
        y=target_y,
        size=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    weapon = MeleeWeapon(name="Sword", damage_dice="1d8", damage_type="slashing")
    attacker.equipped_weapon_uuid = weapon.entity_uuid
    register_entity(attacker)
    register_entity(target)
    register_entity(weapon)
    return attacker, target, weapon


# ============================================================
# REQ-CND-005: Charmed
# ============================================================

def test_req_cnd_005_charmed_blocks_attack_on_charmer(mock_dice):
    """
    REQ-CND-005: Charmed
    An entity cannot attack or target the charmer with harmful abilities.
    """
    attacker, target, _ = _make_combatants()

    # Attacker is charmed by the target
    attacker.active_conditions.append(
        ActiveCondition(name="Charmed", source_uuid=target.entity_uuid, source_name=target.name)
    )

    with mock_dice(15, 6):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    # Attack should be blocked; hit=False, no damage rolled
    assert event.payload.get("hit") is False


def test_req_cnd_005_charmed_allows_attack_on_non_charmer(mock_dice):
    """
    REQ-CND-005: Charmed by a DIFFERENT entity does not block attack on unrelated target.
    """
    attacker, target, _ = _make_combatants()

    # Attacker is charmed by SOMEONE ELSE, not the target
    import uuid
    other_uuid = uuid.uuid4()
    attacker.active_conditions.append(
        ActiveCondition(name="Charmed", source_uuid=other_uuid, source_name="Charmer")
    )

    # Attack roll of 18 hits AC 10
    with mock_dice(18, 1, 5):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True


# ============================================================
# REQ-CND-006: Frightened
# ============================================================

def test_req_cnd_006_frightened_applies_disadvantage(mock_dice):
    """
    REQ-CND-006: Frightened
    Entity has Disadvantage on attack rolls while source is in line of sight.
    """
    attacker, target, _ = _make_combatants()

    # Attacker is frightened (generic — the handler applies disadvantage for "frightened")
    attacker.active_conditions.append(
        ActiveCondition(name="Frightened", source_name="Dragon")
    )

    # Rolls: 8 and 15. With disadvantage → uses min = 8. 8 + 3 (STR) = 11 >= AC 10 → hit.
    # If incorrectly using max: 15 + 3 = 18 → also hits.
    # Use AC 20 to distinguish: only max(8,15)=15+3=18 hits; min(8,15)=8+3=11 < 20 misses.
    target.ac = ModifiableValue(base_value=20)

    with mock_dice(8, 15, 5):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    # With disadvantage (min=8+3=11 < AC 20): miss
    assert event.payload.get("hit") is False


# ============================================================
# REQ-CND-007: Grappled (2024 Update)
# ============================================================

def test_req_cnd_007_grappled_disadvantage_vs_non_grappler(mock_dice):
    """
    REQ-CND-007: Grappled (2024)
    Grappled entity has Disadvantage on attacks against any target OTHER than the grappler.
    """
    import uuid
    attacker, target, _ = _make_combatants()

    # Grappler is someone else
    grappler_uuid = uuid.uuid4()
    attacker.active_conditions.append(
        ActiveCondition(name="Grappled", source_uuid=grappler_uuid, source_name="Grappler")
    )

    # Rolls: 5 and 18. With disadvantage → min=5. 5+3=8 < AC 20 → miss.
    # Without disadvantage: max=18. 18+3=21 >= 20 → hit.
    target.ac = ModifiableValue(base_value=20)

    with mock_dice(5, 18, 5):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is False


def test_req_cnd_007_grappled_no_disadvantage_vs_grappler(mock_dice):
    """
    REQ-CND-007: Grappled (2024)
    Grappled entity does NOT have Disadvantage when attacking the grappler itself.
    """
    attacker, target, _ = _make_combatants()

    # Target IS the grappler
    attacker.active_conditions.append(
        ActiveCondition(name="Grappled", source_uuid=target.entity_uuid, source_name=target.name)
    )

    # Rolls: 5 and 18. Without disadvantage → uses roll1=5. 5+3=8 < AC 20 → miss.
    # But we verify roll1 is used (not min), so we use a higher first roll.
    # Use roll1=15: 15+3=18 >= AC 17 → hit (proves no disadvantage was applied).
    target.ac = ModifiableValue(base_value=17)

    with mock_dice(15, 1, 5):  # roll1=15, roll2=1 — without disadvantage uses roll1=15
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True


# ============================================================
# REQ-CND-010: Paralyzed — Auto-crit within 5ft
# ============================================================

def test_req_cnd_010_paralyzed_auto_crit_within_5ft(mock_dice):
    """
    REQ-CND-010: Paralyzed
    Hits within 5 feet are automatic Critical Hits (even without rolling a 20).
    """
    attacker, target, _ = _make_combatants()  # both at 0,0 — default dist 5ft
    target.active_conditions.append(ActiveCondition(name="Paralyzed"))

    # Roll 12 (hits, not 20). Should become a crit.
    # Damage dice: 5 base + 5 crit = 10, then +3 STR mod
    with mock_dice(12, 1, 5, 5):  # d20=12, roll2 unused, base_dmg=5, crit_extra=5
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert event.payload.get("critical") is True


def test_req_cnd_010_paralyzed_no_auto_crit_beyond_5ft(mock_dice):
    """
    REQ-CND-010: Paralyzed
    Auto-crit does NOT apply when attacker is beyond 5 feet. Use a ranged weapon at 10ft.
    """
    # Place attacker and target 10ft apart in spatial service
    attacker, target, _ = _make_combatants(attacker_x=0.0, target_x=10.0)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)

    target.active_conditions.append(ActiveCondition(name="Paralyzed"))

    # Equip a ranged weapon so the 10ft distance is valid
    bow = RangedWeapon(
        name="Shortbow", damage_dice="1d6", damage_type="piercing",
        normal_range=80, long_range=320,
    )
    attacker.equipped_weapon_uuid = bow.entity_uuid
    register_entity(bow)

    # Roll 15 (hits, not 20). Should NOT be a crit because dist > 5ft.
    with mock_dice(15, 1, 5, 5):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert event.payload.get("critical") is False


# ============================================================
# REQ-CND-011: Petrified — Auto-crit within 5ft
# ============================================================

def test_req_cnd_011_petrified_auto_crit_within_5ft(mock_dice):
    """
    REQ-CND-011: Petrified
    Hits within 5 feet are automatic Critical Hits.
    """
    attacker, target, _ = _make_combatants()
    target.active_conditions.append(ActiveCondition(name="Petrified"))

    with mock_dice(14, 1, 4, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert event.payload.get("critical") is True


# ============================================================
# REQ-CND-014: Prone (Ranged interaction)
# ============================================================

def test_req_cnd_014_prone_ranged_disadvantage_beyond_5ft(mock_dice):
    """
    REQ-CND-014: Prone (Ranged)
    Attack rolls against a Prone entity have Disadvantage if attacker is farther than 5 feet.
    """
    # Give both entities darkvision so they can perceive each other in the unlit default map.
    # Without this, mutual "unseen" advantage/disadvantage would cancel, masking the prone modifier.
    attacker = Creature(
        name="Archer",
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=3),
        tags=["darkvision"],
    )
    target = Creature(
        name="ProneTarget",
        x=30.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=20),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["darkvision"],
    )
    target.active_conditions.append(ActiveCondition(name="Prone"))

    bow = RangedWeapon(
        name="Shortbow",
        damage_dice="1d6",
        damage_type="piercing",
        normal_range=80,
        long_range=320,
    )
    attacker.equipped_weapon_uuid = bow.entity_uuid

    register_entity(attacker)
    register_entity(target)
    register_entity(bow)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)

    # Rolls: 18 and 5. With disadvantage → min=5. 5+3=8 < AC 20 → miss.
    # Without disadvantage: 18+3=21 >= 20 → hit. Proves disadvantage applied.
    # Note: both fire as MeleeAttack (the engine's single attack event type)
    with mock_dice(18, 5, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is False


def test_req_cnd_014_prone_ranged_no_disadvantage_within_5ft(mock_dice):
    """
    REQ-CND-014: Prone (Ranged)
    Attacks within 5ft do NOT have Disadvantage (prone melee gives ADVANTAGE at <=5ft).
    """
    attacker, target, _ = _make_combatants(attacker_x=0.0, target_x=0.0)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)

    target.active_conditions.append(ActiveCondition(name="Prone"))

    # At 5ft (default fallback dist), prone target gives ADVANTAGE to melee attacker.
    # We test: roll1=5, roll2=18 with advantage → uses max=18. 18+3=21 >= AC 20 → hit.
    target.ac = ModifiableValue(base_value=20)

    with mock_dice(5, 18, 5):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True


# ============================================================
# REQ-CND-016: Unconscious — Auto-crit within 5ft
# ============================================================

def test_req_cnd_016_unconscious_auto_crit_within_5ft(mock_dice):
    """
    REQ-CND-016: Unconscious
    Hits within 5 feet are automatic Critical Hits (same as Paralyzed/Petrified).
    """
    attacker, target, _ = _make_combatants()
    target.active_conditions.append(ActiveCondition(name="Unconscious", source_name="0 HP"))

    with mock_dice(14, 1, 4, 4):  # hit, miss (adv picks 14), damage doubled
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert event.payload.get("critical") is True


def test_req_cnd_016_unconscious_no_auto_crit_beyond_5ft(mock_dice):
    """
    REQ-CND-016: Unconscious
    Auto-crit only applies within 5 feet. Beyond that range, no forced crit.
    """
    attacker = Creature(
        name="Attacker",
        x=0.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["darkvision"],
    )
    target = Creature(
        name="UnconsciousTarget",
        x=30.0,
        y=0.0,
        size=5.0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["darkvision"],
    )
    register_entity(attacker)
    register_entity(target)
    spatial_service.sync_entity(attacker)
    spatial_service.sync_entity(target)
    target.active_conditions.append(ActiveCondition(name="Unconscious", source_name="0 HP"))

    with mock_dice(14, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("_auto_crit_on_hit") is not True
