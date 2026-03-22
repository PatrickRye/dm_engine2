import os
import pytest
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service, Wall
from registry import clear_registry, register_entity
from tools import modify_health, use_ability_or_spell, move_entity
from spell_system import SpellDefinition, SpellMechanics, SpellCompendium


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_edg_003_instant_death_hp_query(setup):
    """
    Trace: REQ-EDG-003
    Validates that spells like Power Word Kill query true HP (base_value), strictly ignoring
    any Temporary Hit Points (THP) buffer the entity might have.
    """
    vp = setup
    target = Creature(
        name="Target",
        vault_path=vp,
        hp=ModifiableValue(base_value=90),
        max_hp=150,
        temp_hp=20,  # 90 base + 20 THP = 110 total, but base is under 100!
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    config = {"configurable": {"thread_id": vp}}
    res = await modify_health.ainvoke(
        {"target_name": "Target", "hp_change": 0, "reason": "Power Word Kill", "instant_death_threshold": 100}, config=config
    )

    assert "instantly killed" in res
    assert target.hp.base_value == 0
    assert any(c.name == "Dead" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_req_edg_004_instant_death_disintegrate(setup):
    """
    Trace: REQ-EDG-004
    Validates that Disintegrate evaluates HP after damage is dealt; if HP reaches 0, the entity
    turns to dust (state.dust=True) and bypasses the standard Dying state.
    """
    vp = setup
    target = Creature(
        name="Target",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        max_hp=100,
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    config = {"configurable": {"thread_id": vp}}
    res = await modify_health.ainvoke(
        {"target_name": "Target", "hp_change": -30, "reason": "Disintegrate", "disintegrate_if_zero": True}, config=config
    )

    assert "DUST" in res
    assert target.hp.base_value == 0
    assert any(c.name == "Dead" for c in target.active_conditions)
    assert any(c.name == "Dust" for c in target.active_conditions)
    assert not any(c.name == "Dying" for c in target.active_conditions)


@pytest.mark.asyncio
async def test_req_spl_006_target_invalidation(setup):
    """
    Trace: REQ-SPL-006
    Validates that if a target becomes invalid (e.g., dies or moves out of range) between
    casting and resolution, the spell fails on that target but the spell slot is still expended.
    """
    vp = setup
    caster = Creature(
        name="Wizard",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Dead Goblin",
        vault_path=vp,
        hp=ModifiableValue(base_value=0),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Dead")],
    )
    register_entity(caster)
    register_entity(target)

    spell = SpellDefinition(name="Magic Missile", level=1, mechanics=SpellMechanics(damage_dice="3d4", damage_type="force"))
    await SpellCompendium.save_spell(vp, spell)

    config = {"configurable": {"thread_id": vp}}
    res = await use_ability_or_spell.ainvoke(
        {"caster_name": "Wizard", "ability_name": "Magic Missile", "target_names": ["Dead Goblin"]}, config=config
    )

    assert "is dead and an invalid target" in res
    assert "REQ-SPL-006" in res
    assert caster.spell_slots_expended_this_turn == 1


@pytest.mark.asyncio
async def test_req_edg_007_illusion_bypass(setup):
    """
    Trace: REQ-EDG-007
    Validates that physical intersection with an illusion instantly reveals it, bypassing investigation checks.
    """
    vp = setup
    hero = Creature(
        name="Hero",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        x=0.0,
        y=0.0,
        size=5.0,
    )
    register_entity(hero)
    spatial_service.sync_entity(hero)

    wall = Wall(
        label="Fake Wall",
        start=(5.0, -5.0),
        end=(5.0, 5.0),
        is_solid=False,
        is_visible=True,
        is_illusion=True,
        illusion_spell_dc=15,
    )
    spatial_service.add_wall(wall, vault_path=vp)

    config = {"configurable": {"thread_id": vp}}
    res = await move_entity.ainvoke(
        {"entity_name": "Hero", "target_x": 10.0, "target_y": 0.0, "movement_type": "walk"}, config=config
    )

    assert "REQ-ILL-001" in res
    assert "revealed to them as an illusion" in res
    assert str(hero.entity_uuid) in wall.revealed_for


@pytest.mark.asyncio
async def test_req_spl_020_touch_unwilling(setup, mock_dice):
    """
    Trace: REQ-SPL-020
    Validates that Touch spells against unwilling targets enforce range and require an attack roll.
    """
    vp = setup
    caster = Creature(
        name="Cleric",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["pc"],
        x=0.0,
        y=0.0,
    )
    target = Creature(
        name="Goblin",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["monster"],
        x=10.0,
        y=0.0,
    )
    register_entity(caster)
    register_entity(target)
    spatial_service.sync_entity(caster)
    spatial_service.sync_entity(target)

    # 1. Out of range touch spell
    spell = SpellDefinition(
        name="Inflict Wounds", level=1, range_str="Touch", mechanics=SpellMechanics(damage_dice="3d10", damage_type="necrotic")
    )
    await SpellCompendium.save_spell(vp, spell)

    config = {"configurable": {"thread_id": vp}}
    res1 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Cleric", "ability_name": "Inflict Wounds", "target_names": ["Goblin"]}, config=config
    )
    assert "SYSTEM ERROR" in res1
    assert "out of Touch range" in res1
    assert "REQ-SPL-020" in res1

    # 2. In range, hostile -> forces attack roll
    target.x = 5.0
    spatial_service.sync_entity(target)
    with mock_dice(default=15):
        res2 = await use_ability_or_spell.ainvoke(
            {"caster_name": "Cleric", "ability_name": "Inflict Wounds", "target_names": ["Goblin"], "force_auto_roll": True},
            config=config,
        )
    assert "Hit" in res2 or "Miss" in res2
    assert "Auto-hit" not in res2


@pytest.mark.asyncio
async def test_req_spl_021_self_area_excludes_caster(setup, mock_dice):
    """
    Trace: REQ-SPL-021
    Validates that Self (Area) spells originate from the caster and exclude them from damage by default.
    """
    vp = setup
    caster = Creature(
        name="Paladin",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["pc"],
        x=10.0,
        y=10.0,
    )
    target = Creature(
        name="Ghoul",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["monster"],
        x=15.0,
        y=10.0,
    )
    register_entity(caster)
    register_entity(target)
    spatial_service.sync_entity(caster)
    spatial_service.sync_entity(target)

    spell = SpellDefinition(
        name="Spirit Guardians",
        level=3,
        range_str="Self (15-foot radius)",
        mechanics=SpellMechanics(damage_dice="3d8", damage_type="radiant", save_required="wisdom"),
    )
    await SpellCompendium.save_spell(vp, spell)

    config = {"configurable": {"thread_id": vp}}
    with mock_dice(default=5):
        res = await use_ability_or_spell.ainvoke(
            {
                "caster_name": "Paladin",
                "ability_name": "Spirit Guardians",
                "aoe_shape": "sphere",
                "aoe_size": 15.0,
            },
            config=config,
        )

    assert "originating from Paladin" in res
    assert "Ghoul" in res
    assert "Paladin] Saved" not in res and "Paladin] Failed" not in res  # Paladin is excluded
