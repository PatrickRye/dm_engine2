import pytest
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import use_ability_or_spell, perform_ability_check_or_save, execute_melee_attack
from spell_system import SpellDefinition, SpellMechanics, SpellCompendium


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_snd_001_verbal_components(setup):
    vp = setup
    caster = Creature(
        name="Bard",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Silenced")],
    )
    register_entity(caster)

    spell = SpellDefinition(
        name="Vicious Mockery", level=0, components=["V"], mechanics=SpellMechanics(damage_dice="1d4", damage_type="psychic")
    )
    await SpellCompendium.save_spell(vp, spell)

    config = {"configurable": {"thread_id": vp}}

    # Test 1: Silenced
    res1 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Bard", "ability_name": "Vicious Mockery", "target_names": ["Bard"]}, config=config
    )
    assert "SYSTEM ERROR" in res1
    assert "REQ-SND-001" in res1

    # Test 2: Underwater without Water Breathing
    caster.active_conditions.clear()
    caster.tags.append("underwater")

    res2 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Bard", "ability_name": "Vicious Mockery", "target_names": ["Bard"]}, config=config
    )
    assert "SYSTEM ERROR" in res2
    assert "REQ-SND-001" in res2
    assert "submerged underwater" in res2

    # Test 3: Underwater WITH Water Breathing
    caster.tags.append("water_breathing")

    res3 = await use_ability_or_spell.ainvoke(
        {"caster_name": "Bard", "ability_name": "Vicious Mockery", "target_names": ["Bard"]}, config=config
    )
    assert "MECHANICAL TRUTH" in res3


@pytest.mark.asyncio
async def test_req_vis_002_stealth_invisibility(setup, mock_dice):
    vp = setup
    rogue = Creature(
        name="Rogue",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=5),
        tags=["pc"],
    )
    enemy = Creature(
        name="Guard",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=2),  # Passive Perception = 12
    )
    register_entity(rogue)
    register_entity(enemy)
    spatial_service.map_data.lights.clear()  # Darkness

    config = {"configurable": {"thread_id": vp}}

    # DC should be max(15, 12) = 15.
    # Roll 15 + 5 = 20. >= 15 -> Success!
    with mock_dice(default=15):
        res = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Rogue", "skill_or_stat_name": "stealth"}, config=config
        )

    assert "REQ-VIS-002" in res
    assert "gained the 'Invisible' condition" in res
    assert any(c.name == "Invisible" for c in rogue.active_conditions)

    # Test failure
    rogue.active_conditions.clear()
    with mock_dice(default=2):  # Roll 2 + 5 = 7. < 15 -> Fail!
        res_fail = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Rogue", "skill_or_stat_name": "stealth"}, config=config
        )

    assert "failed to beat DC 15" in res_fail
    assert not any(c.name == "Invisible" for c in rogue.active_conditions)


@pytest.mark.asyncio
async def test_req_snd_002_stealth_break_noise(setup):
    vp = setup
    rogue = Creature(
        name="Rogue",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        active_conditions=[ActiveCondition(name="Invisible", source_name="Hide Action")],
    )
    register_entity(rogue)

    spell = SpellDefinition(name="Healing Word", level=1, components=["V"], mechanics=SpellMechanics())
    await SpellCompendium.save_spell(vp, spell)

    config = {"configurable": {"thread_id": vp}}

    res = await use_ability_or_spell.ainvoke(
        {"caster_name": "Rogue", "ability_name": "Healing Word", "target_names": ["Rogue"]}, config=config
    )

    assert "REQ-SND-002" in res
    assert not any(c.name == "Invisible" for c in rogue.active_conditions)

    # Test that attacking breaks it too
    rogue.active_conditions.append(ActiveCondition(name="Invisible", source_name="Hide Action"))
    target = Creature(
        name="Guard",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(target)

    from dnd_rules_engine import GameEvent, EventBus

    event = GameEvent(event_type="MeleeAttack", source_uuid=rogue.entity_uuid, target_uuid=target.entity_uuid)
    EventBus.dispatch(event)

    assert not any(c.name == "Invisible" for c in rogue.active_conditions)
