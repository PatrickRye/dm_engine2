import pytest
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition, GameEvent, EventBus
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import use_ability_or_spell, perform_ability_check_or_save
from spell_system import SpellDefinition, SpellMechanics, SpellCompendium
from unittest.mock import patch


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


@pytest.mark.asyncio
async def test_req_enc_001_damage_breaks_charm(setup):
    """
    REQ-ENC-001: Damage Breaks Charm
    Charm instantly terminates if the caster or the caster's allies deal damage to the target.
    """
    vp = setup
    caster = Creature(
        name="Bard",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["pc"],
    )
    ally = Creature(
        name="Fighter",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["pc"],
    )
    target = Creature(
        name="Goblin",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["monster"],
    )

    register_entity(caster)
    register_entity(ally)
    register_entity(target)

    # Apply Charmed condition
    target.active_conditions.append(
        ActiveCondition(name="Charmed", source_uuid=caster.entity_uuid, source_name="Charm Person")
    )
    caster.concentrating_on = "Charm Person"

    # Ally damages target
    dmg_event = GameEvent(
        event_type="ApplyDamage",
        source_uuid=ally.entity_uuid,
        target_uuid=target.entity_uuid,
        payload={"damage": 5, "damage_type": "slashing"},
    )
    EventBus.dispatch(dmg_event)

    assert target.hp.base_value == 15
    assert not any(c.name == "Charmed" for c in target.active_conditions)
    # The event_bus should have dispatched DropConcentration for the caster
    assert caster.concentrating_on == ""


@pytest.mark.asyncio
async def test_req_enc_002_harmful_commands_auto_fail(setup):
    """
    REQ-ENC-002: Harmful Commands (Auto-Fail)
    Spells like Suggestion or Command automatically fail if the command directs target to harm itself.
    """
    vp = setup
    caster = Creature(
        name="Cleric",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Bandit",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    register_entity(target)

    spell = SpellDefinition(name="Command", level=1, school="enchantment", mechanics=SpellMechanics(save_required="wisdom"))
    await SpellCompendium.save_spell(vp, spell)

    config = {"configurable": {"thread_id": vp}}
    res = await use_ability_or_spell.ainvoke(
        {"caster_name": "Cleric", "ability_name": "Command", "target_names": ["Bandit"], "command_invokes_self_harm": True},
        config=config,
    )

    assert "SYSTEM ERROR" in res
    assert "REQ-ENC-002" in res


@pytest.mark.asyncio
async def test_req_enc_003_post_spell_hostility(setup):
    """
    REQ-ENC-003: Post-Spell Hostility
    Target realizes they were charmed when the spell ends, immediately dropping Attitude to Hostile.
    """
    vp = setup
    caster = Creature(
        name="Warlock",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Merchant",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    register_entity(target)

    # Cast Friends (grants realizes_charm tag)
    spell = SpellDefinition(
        name="Friends",
        level=0,
        mechanics=SpellMechanics(
            requires_concentration=True,
            granted_tags=["realizes_charm"],
            conditions_applied=[{"condition": "Charmed", "duration": "1 minute"}],
        ),
    )
    await SpellCompendium.save_spell(vp, spell)

    config = {"configurable": {"thread_id": vp}}
    with patch("random.randint", return_value=10):
        await use_ability_or_spell.ainvoke(
            {"caster_name": "Warlock", "ability_name": "Friends", "target_names": ["Merchant"]}, config=config
        )

    assert any(c.name == "Charmed" for c in target.active_conditions)

    # Drop concentration
    drop_event = GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid, vault_path=vp)
    EventBus.dispatch(drop_event)

    assert not any(c.name == "Charmed" for c in target.active_conditions)
    assert any("REQ-ENC-003" in r for r in drop_event.payload.get("results", []))


@pytest.mark.asyncio
async def test_req_enc_004_charmed_vs_social_checks(setup, mock_dice):
    """
    REQ-ENC-004: Charmed grants Advantage on social interaction checks.
    """
    vp = setup
    caster = Creature(
        name="Bard",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Guard",
        vault_path=vp,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    register_entity(target)

    target.active_conditions.append(ActiveCondition(name="Charmed", source_uuid=caster.entity_uuid))
    config = {"configurable": {"thread_id": vp}}

    with mock_dice(10, 15):  # Advantage uses 15
        res = await perform_ability_check_or_save.ainvoke(
            {"character_name": "Bard", "skill_or_stat_name": "persuasion", "target_names": ["Guard"]}, config=config
        )

    assert "REQ-ENC-004" in res
    assert "Advantage" in res
    assert "15" in res
