import pytest
import os
from dnd_rules_engine import (
    Creature, ModifiableValue, GameEvent, EventBus, ActiveCondition,
    MeleeWeapon, WeaponProperty, EventStatus,
)
from registry import register_entity, clear_registry
from tools import level_up_character, modify_health
from spatial_engine import spatial_service
from state import ClassLevel


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


def _make_cls_pair(attacker_tags=None, target_tags=None):
    """Helper: create a basic attacker/target pair for class feature tests.
    darkvision is always included so unlit maps don't introduce spurious visibility disadvantage.
    """
    attacker = Creature(
        name="Attacker",
        x=0.0, y=0.0, size=5.0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=2),
        tags=["darkvision"] + (attacker_tags or []),
    )
    target = Creature(
        name="Target",
        x=0.0, y=0.0, size=5.0,
        hp=ModifiableValue(base_value=40),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["darkvision"] + (target_tags or []),
    )
    weapon = MeleeWeapon(name="Sword", damage_dice="1d8", damage_type="slashing")
    attacker.equipped_weapon_uuid = weapon.entity_uuid
    register_entity(attacker)
    register_entity(target)
    register_entity(weapon)
    return attacker, target


# ============================================================
# REQ-CLS-001: Barbarian Rage Maintenance
# ============================================================

def test_req_cls_001_rage_ends_when_inactive(mock_dice):
    """
    REQ-CLS-001: Rage ends at the START of turn if neither attacked nor took damage.
    """
    barb = Creature(
        name="RagingBarb",
        hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=3), dexterity_mod=ModifiableValue(base_value=0),
    )
    barb.active_conditions.append(ActiveCondition(name="Raging"))
    barb.resources["Raged This Cycle"] = "0/1"
    register_entity(barb)

    event = GameEvent(event_type="StartOfTurn", source_uuid=barb.entity_uuid)
    EventBus.dispatch(event)

    cond_names = [c.name.lower() for c in barb.active_conditions]
    assert "raging" not in cond_names, "Rage should have ended due to inactivity"
    assert any("Rage" in r for r in event.payload.get("results", []))


def test_req_cls_001_rage_persists_after_attacking(mock_dice):
    """
    REQ-CLS-001: Rage persists if the entity attacked since its last turn.
    """
    barb = Creature(
        name="RagingBarb",
        hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=3), dexterity_mod=ModifiableValue(base_value=0),
    )
    barb.active_conditions.append(ActiveCondition(name="Raging"))
    barb.resources["Raged This Cycle"] = "1/1"
    register_entity(barb)

    event = GameEvent(event_type="StartOfTurn", source_uuid=barb.entity_uuid)
    EventBus.dispatch(event)

    cond_names = [c.name.lower() for c in barb.active_conditions]
    assert "raging" in cond_names, "Rage should persist after attacking"


def test_req_cls_001_attack_while_raging_sets_cycle_flag(mock_dice):
    """
    REQ-CLS-001: Attacking while Raging sets the 'Raged This Cycle' flag.
    """
    attacker, target = _make_cls_pair()
    attacker.active_conditions.append(ActiveCondition(name="Raging"))
    attacker.resources["Raged This Cycle"] = "0/1"

    with mock_dice(15, 1, 5):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert attacker.resources.get("Raged This Cycle") == "1/1"


def test_req_cls_001_taking_damage_while_raging_sustains_rage(mock_dice):
    """
    REQ-CLS-001: Taking damage while Raging also counts as a sustaining action.
    """
    barb = Creature(
        name="RagingBarb",
        hp=ModifiableValue(base_value=30), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=3), dexterity_mod=ModifiableValue(base_value=0),
    )
    barb.active_conditions.append(ActiveCondition(name="Raging"))
    barb.resources["Raged This Cycle"] = "0/1"
    register_entity(barb)

    damage_event = GameEvent(
        event_type="ApplyDamage",
        source_uuid=barb.entity_uuid,
        target_uuid=barb.entity_uuid,
        payload={"damage": 5, "damage_type": "slashing", "critical": False},
    )
    EventBus.dispatch(damage_event)

    assert barb.resources.get("Raged This Cycle") == "1/1"


# ============================================================
# REQ-CLS-003/004: Rogue Sneak Attack
# ============================================================

def test_req_cls_003_sneak_attack_fires_with_advantage(mock_dice):
    """
    REQ-CLS-003/004: Sneak Attack fires when attacker has Advantage and a finesse weapon.
    """
    attacker, target = _make_cls_pair(attacker_tags=["pc", "sneak_attack_2d6"])
    rapier = MeleeWeapon(
        name="Rapier", damage_dice="1d6", damage_type="piercing",
        properties=[WeaponProperty.FINESSE],
    )
    attacker.equipped_weapon_uuid = rapier.entity_uuid
    register_entity(rapier)

    with mock_dice(15, 10, 4, 3, 3):  # roll1=15, roll2=10, wpn_dmg=4, sneak=3+3(crit wouldn't fire)
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
            payload={"advantage": True},
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert attacker.resources.get("Sneak Attack") == "1/1"
    assert any("SNEAK ATTACK" in r for r in event.payload.get("results", []))


def test_req_cls_004_sneak_attack_fires_with_adjacent_ally(mock_dice):
    """
    REQ-CLS-004: Sneak Attack fires when a conscious ally is within 5ft of the target.
    """
    attacker, target = _make_cls_pair(attacker_tags=["pc", "sneak_attack_1d6"])
    rapier = MeleeWeapon(
        name="Rapier", damage_dice="1d6", damage_type="piercing",
        properties=[WeaponProperty.FINESSE],
    )
    attacker.equipped_weapon_uuid = rapier.entity_uuid
    register_entity(rapier)

    ally = Creature(
        name="Ally", x=0.0, y=0.0, size=5.0,
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
        tags=["pc"],
    )
    register_entity(ally)

    with mock_dice(12, 1, 4, 3):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert any("SNEAK ATTACK" in r for r in event.payload.get("results", []))


def test_req_cls_004_sneak_attack_blocked_by_disadvantage(mock_dice):
    """
    REQ-CLS-004: Disadvantage negates Sneak Attack even when advantage is also present.
    """
    attacker, target = _make_cls_pair(attacker_tags=["pc", "sneak_attack_2d6"])
    rapier = MeleeWeapon(
        name="Rapier", damage_dice="1d6", damage_type="piercing",
        properties=[WeaponProperty.FINESSE],
    )
    attacker.equipped_weapon_uuid = rapier.entity_uuid
    register_entity(rapier)

    with mock_dice(15, 12, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
            payload={"advantage": True, "disadvantage": True},
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert not any("SNEAK ATTACK" in r for r in event.payload.get("results", []))


def test_req_cls_003_sneak_attack_once_per_turn(mock_dice):
    """
    REQ-CLS-003: Sneak Attack can only fire once per turn (resource guard).
    """
    attacker, target = _make_cls_pair(attacker_tags=["pc", "sneak_attack_1d6"])
    rapier = MeleeWeapon(
        name="Rapier", damage_dice="1d6", damage_type="piercing",
        properties=[WeaponProperty.FINESSE],
    )
    attacker.equipped_weapon_uuid = rapier.entity_uuid
    register_entity(rapier)
    attacker.resources["Sneak Attack"] = "1/1"  # already used this turn

    with mock_dice(15, 1, 4):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=target.entity_uuid,
            payload={"advantage": True},
        )
        EventBus.dispatch(event)

    assert not any("SNEAK ATTACK" in r for r in event.payload.get("results", []))


def test_req_cls_003_sneak_attack_resets_at_start_of_turn(mock_dice):
    """
    REQ-CLS-003: Sneak Attack resource resets to 0/1 at the start of the rogue's turn.
    """
    rogue = Creature(
        name="Rogue",
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=2),
        tags=["pc", "sneak_attack_2d6"],
    )
    rogue.resources["Sneak Attack"] = "1/1"
    register_entity(rogue)

    event = GameEvent(event_type="StartOfTurn", source_uuid=rogue.entity_uuid)
    EventBus.dispatch(event)

    assert rogue.resources.get("Sneak Attack") == "0/1"


# ============================================================
# REQ-CLS-012: Monk Deflect Attacks
# ============================================================

def test_req_cls_012_deflect_attacks_reduces_damage(mock_dice):
    """
    REQ-CLS-012: Monk with deflect_attacks tag reduces weapon damage by 1d10 + dex + monk_level.
    """
    attacker, monk = _make_cls_pair(target_tags=["deflect_attacks"])
    monk.classes.append(ClassLevel(class_name="Monk", level=5))
    monk.dexterity_mod = ModifiableValue(base_value=3)

    # d20=15 (hit AC 10), roll2=1 (unused), wpn_dmg=8, deflect_1d10=6
    # base_damage = 8(roll) + 3(attacker STR) = 11; reduction = 6+3+5 = 14; net=0
    with mock_dice(15, 1, 8, 6):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=monk.entity_uuid,
        )
        EventBus.dispatch(event)

    assert event.payload.get("hit") is True
    assert monk.reaction_used is True
    assert monk.hp.base_value == 40, "Fully deflected — monk HP should be untouched"
    assert any("Deflect Attacks" in r for r in event.payload.get("results", []))


def test_req_cls_012_no_deflect_if_reaction_used(mock_dice):
    """
    REQ-CLS-012: Deflect Attacks does not fire if the Monk already used their Reaction.
    """
    attacker, monk = _make_cls_pair(target_tags=["deflect_attacks"])
    monk.classes.append(ClassLevel(class_name="Monk", level=3))
    monk.reaction_used = True

    with mock_dice(15, 1, 8, 6):
        event = GameEvent(
            event_type="MeleeAttack",
            source_uuid=attacker.entity_uuid,
            target_uuid=monk.entity_uuid,
        )
        EventBus.dispatch(event)

    assert not any("Deflect Attacks" in r for r in event.payload.get("results", []))


# ============================================================
# REQ-CLS-015: Wild Shape blocks spell casting
# ============================================================

def test_req_cls_015_wild_shape_blocks_casting():
    """
    REQ-CLS-015: Entity cannot cast new spells while Wild Shaped (wild_shape_hp > 0).
    """
    druid = Creature(
        name="WildDruid",
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
        wild_shape_hp=10,
    )
    register_entity(druid)

    spell_event = GameEvent(
        event_type="SpellCast",
        source_uuid=druid.entity_uuid,
        payload={
            "ability_name": "Fireball",
            "mechanics": {"damage_dice": "8d6", "damage_type": "fire"},
            "target_uuids": [],
        },
    )
    result = EventBus.dispatch(spell_event)

    assert result.status == EventStatus.CANCELLED
    assert any("Wild Shaped" in r for r in result.payload.get("results", []))


def test_req_cls_015_normal_form_can_cast():
    """
    REQ-CLS-015: Entity with wild_shape_hp == 0 (not transformed) can cast normally.
    """
    druid = Creature(
        name="NormalDruid",
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
        wild_shape_hp=0,
    )
    register_entity(druid)

    spell_event = GameEvent(
        event_type="SpellCast",
        source_uuid=druid.entity_uuid,
        payload={
            "ability_name": "Shillelagh",
            "mechanics": {"damage_dice": "", "damage_type": ""},
            "target_uuids": [],
        },
    )
    result = EventBus.dispatch(spell_event)

    assert result.status != EventStatus.CANCELLED


# ============================================================
# REQ-SPL-002: Magic Action vs Cast Spell distinction
# ============================================================

def test_req_spl_002_magic_action_not_blocked_by_wild_shape():
    """
    REQ-SPL-002: Features triggering on 'Cast a Spell' do NOT trigger on a
    generic 'Magic Action'. A Wild Shaped druid can still use class features
    tagged Magic_Action (e.g. Wild Shape itself, Dragonborn Breath) without
    being blocked by the Wild Shape spell-block handler.
    """
    druid = Creature(
        name="WildDruid",
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
        wild_shape_hp=10,
    )
    register_entity(druid)

    # Magic_Action event (e.g. using Wild Shape to transform again, or Dragonborn Breath)
    magic_action_event = GameEvent(
        event_type="SpellCast",
        source_uuid=druid.entity_uuid,
        event_tag="Magic_Action",  # REQ-SPL-002: not an actual spell cast
        payload={
            "ability_name": "Wild Shape",
            "mechanics": {},
            "target_uuids": [],
        },
    )
    result = EventBus.dispatch(magic_action_event)

    # Magic_Action should NOT be cancelled by the Wild Shape spell-block handler
    assert result.status != EventStatus.CANCELLED


def test_req_spl_002_cast_spell_still_blocked_by_wild_shape():
    """
    REQ-SPL-002: A Wild Shaped druid casting an actual spell (Cast_Spell) IS
    still blocked — Magic_Action and Cast_Spell are distinct.
    """
    druid = Creature(
        name="WildDruid",
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
        wild_shape_hp=10,
    )
    register_entity(druid)

    spell_event = GameEvent(
        event_type="SpellCast",
        source_uuid=druid.entity_uuid,
        event_tag="Cast_Spell",  # REQ-SPL-002: actual spellcasting
        payload={
            "ability_name": "Fireball",
            "mechanics": {"damage_dice": "8d6", "damage_type": "fire"},
            "target_uuids": [],
        },
    )
    result = EventBus.dispatch(spell_event)

    assert result.status == EventStatus.CANCELLED
    assert any("Wild Shaped" in r for r in result.payload.get("results", []))


# ============================================================
# REQ-CLS-016: Fighter Second Wind (healing_dice branch)
# ============================================================

def test_req_cls_016_healing_dice_restores_hp(mock_dice):
    """
    REQ-CLS-016: SpellCast with healing_dice heals the target.
    """
    fighter = Creature(
        name="Fighter",
        hp=ModifiableValue(base_value=12),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
    )
    fighter.max_hp = 20
    register_entity(fighter)

    with mock_dice(7):  # roll_dice("1d10") → 7
        spell_event = GameEvent(
            event_type="SpellCast",
            source_uuid=fighter.entity_uuid,
            payload={
                "ability_name": "Second Wind",
                "mechanics": {"healing_dice": "1d10", "damage_dice": "", "damage_type": ""},
                "target_uuids": [fighter.entity_uuid],
            },
        )
        EventBus.dispatch(spell_event)

    assert fighter.hp.base_value == 19, "12 + 7 = 19 HP"


def test_req_cls_016_healing_caps_at_max_hp(mock_dice):
    """
    REQ-CLS-016: Healing cannot exceed max_hp.
    """
    fighter = Creature(
        name="Fighter",
        hp=ModifiableValue(base_value=18),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
    )
    fighter.max_hp = 20
    register_entity(fighter)

    with mock_dice(10):  # 18 + 10 = 28, capped at 20
        spell_event = GameEvent(
            event_type="SpellCast",
            source_uuid=fighter.entity_uuid,
            payload={
                "ability_name": "Second Wind",
                "mechanics": {"healing_dice": "1d10", "damage_dice": "", "damage_type": ""},
                "target_uuids": [fighter.entity_uuid],
            },
        )
        EventBus.dispatch(spell_event)

    assert fighter.hp.base_value == 20


# ============================================================
# REQ-CLS-008/011: Short Rest [SR] resource reset
# ============================================================

def test_req_cls_008_short_rest_resets_sr_resources():
    """
    REQ-CLS-008/011: Resources tagged [SR] (Short Rest) reset fully on short rest.
    Covers Warlock Pact Magic and Monk Focus Points.
    """
    warlock = Creature(
        name="Warlock",
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
    )
    warlock.resources["Pact Magic Slots [SR]"] = "0/2"
    warlock.resources["Focus Points [SR]"] = "1/4"
    warlock.resources["Hit Dice (d8)"] = "2/5"  # should NOT reset on short rest
    register_entity(warlock)

    rest_event = GameEvent(
        event_type="Rest",
        source_uuid=warlock.entity_uuid,
        payload={"rest_type": "short", "target_uuids": [warlock.entity_uuid]},
    )
    EventBus.dispatch(rest_event)

    assert warlock.resources["Pact Magic Slots [SR]"] == "2/2"
    assert warlock.resources["Focus Points [SR]"] == "4/4"
    assert warlock.resources["Hit Dice (d8)"] == "2/5", "Hit Dice should not reset on short rest"


def test_req_cls_008_long_rest_also_resets_sr_resources():
    """
    [SR] resources also reset on a Long Rest (long rest covers all rests).
    """
    warlock = Creature(
        name="Warlock",
        hp=ModifiableValue(base_value=20), ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0),
    )
    warlock.resources["Pact Magic Slots [SR]"] = "0/2"
    warlock.max_hp = 20
    register_entity(warlock)

    rest_event = GameEvent(
        event_type="Rest",
        source_uuid=warlock.entity_uuid,
        payload={"rest_type": "long", "target_uuids": [warlock.entity_uuid]},
    )
    EventBus.dispatch(rest_event)

    assert warlock.resources["Pact Magic Slots [SR]"] == "2/2"
