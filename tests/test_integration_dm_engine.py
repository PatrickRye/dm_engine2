import os
import yaml
import json
import pytest
from unittest.mock import patch

from dnd_rules_engine import BaseGameEntity, Creature, MeleeWeapon, EventBus
from vault_io import initialize_engine_from_vault
from tools import equip_item, execute_melee_attack, modify_health, perform_ability_check_or_save, level_up_character, move_entity, manage_skill_challenge
from spatial_engine import spatial_service
from event_handlers import resolve_attack_handler, apply_damage_handler
from registry import clear_registry, get_all_entities, get_entity

@pytest.fixture(autouse=True)
def setup_engine_state():
    clear_registry()
    yield

@pytest.fixture
def mock_entities(mock_obsidian_vault):
    """Helper fixture to seed the mocked vault with specific entities for this test file."""
    journals_dir = os.path.join(mock_obsidian_vault, "Journals")
    char_name, target_name = "Tharion", "Goblin"
    
    with open(os.path.join(journals_dir, f"{char_name}.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {char_name}\ntags: [pc]\nhp: 25\nmax_hp: 25\nac: 16\nstrength_mod: 3\ndexterity_mod: 1\nequipment: {{main_hand: Unarmed}}\nclasses: [{{class_name: Fighter, level: 2, subclass_name: Champion}}]\n---\n")
        
    with open(os.path.join(journals_dir, f"{target_name}.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {target_name}\ntags: [monster]\nhp: 7\nac: 15\nstrength_mod: -1\ndexterity_mod: 2\nequipment: {{main_hand: Dagger}}\n---\n")
        
    with open(os.path.join(journals_dir, "CAMPAIGN_MASTER.md"), "w", encoding="utf-8") as f:
        f.write("---\ntags: [campaign]\n---\n# Campaign Master\n\n## Major Milestones (Event Log)\n- Started.\n")
        
    # Setup mock JSON compendium files for the Level Up Test
    comp_dir = os.path.join(mock_obsidian_vault, "Compendium")
    os.makedirs(os.path.join(comp_dir, "classes"), exist_ok=True)
    os.makedirs(os.path.join(comp_dir, "subclasses"), exist_ok=True)
    
    fighter_data = {
        "name": "Fighter",
        "features": [{"name": "Martial Archetype", "level": 3, "description": "You choose an archetype."}]
    }
    champion_data = {
        "name": "Champion",
        "parent_class": "Fighter",
        "features": [{"name": "Improved Critical", "level": 3, "description": "Crit on 19 or 20."}]
    }
    
    with open(os.path.join(comp_dir, "classes", "fighter.json"), "w", encoding="utf-8") as f:
        json.dump(fighter_data, f)
    with open(os.path.join(comp_dir, "subclasses", "champion.json"), "w", encoding="utf-8") as f:
        json.dump(champion_data, f)
        
    return mock_obsidian_vault, char_name, target_name

@pytest.mark.asyncio
async def test_vault_to_engine_initialization(mock_entities):
    """Tests that YAML stats load correctly into the OO Engine, including weapon bridging."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    
    entities = [e for e in get_all_entities().values() if e.name == char_name]
    assert len(entities) == 1
    tharion: Creature = entities[0]
    
    assert tharion.hp.base_value == 25
    assert tharion.ac.base_value == 16
    assert tharion.dexterity_mod.base_value == 1
    
    assert tharion.equipped_weapon_uuid is not None
    weapon = get_entity(tharion.equipped_weapon_uuid)
    assert isinstance(weapon, MeleeWeapon)
    assert weapon.damage_dice == "1d4"

@pytest.mark.asyncio
async def test_tool_to_engine_sync(mock_entities):
    """Tests that 'equip_item' updates both the YAML file and the active Engine memory."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    
    config = {"configurable": {"thread_id": vault_path}}
    result = await equip_item.ainvoke({"character_name": char_name, "item_name": "Silver Longsword", "item_slot": "main_hand", "new_ac_value": 17}, config=config)
    
    assert "Success" in result
    
    entities = [e for e in get_all_entities().values() if e.name == char_name]
    tharion: Creature = entities[0]
    assert tharion.ac.base_value == 17 
    
    weapon = get_entity(tharion.equipped_weapon_uuid)
    assert weapon.name == "Silver Longsword"
    assert weapon.damage_dice == "1d8"
    
    with open(os.path.join(vault_path, "Journals", f"{char_name}.md"), "r", encoding="utf-8") as f:
        content = f.read()
    assert "main_hand: Silver Longsword" in content
    assert "ac: 17" in content

@pytest.mark.asyncio
async def test_tool_to_eventbus_combat(mock_entities):
    """Tests the execution of a tool triggering the OO EventBus and altering state."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    config = {"configurable": {"thread_id": vault_path}}
    
    with patch('random.randint', side_effect=[20, 20, 4, 4, 4]): 
        result = await execute_melee_attack.ainvoke({"attacker_name": char_name, "target_name": target_name}, config=config)
    
    assert "MECHANICAL TRUTH: HIT!" in result
    assert "remaining" in result

@pytest.mark.asyncio
async def test_tool_level_up_character(mock_entities):
    """Tests that a character levels up, max_hp increases, and JSON compendium features are applied."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    
    config = {"configurable": {"thread_id": vault_path}}
    
    # Level up Tharion from Fighter 2 -> Fighter 3
    result = await level_up_character.ainvoke({
        "character_name": char_name, 
        "class_name": "Fighter", 
        "hp_increase": 8
    }, config=config)
    
    assert "Success" in result
    assert "level 3 Fighter" in result
    
    # 1. Verify in-memory entity updated
    entities = [e for e in get_all_entities().values() if e.name == char_name]
    tharion: Creature = entities[0]
    
    assert tharion.max_hp == 33
    assert tharion.hp.base_value == 33
    
    # Verify features were pulled dynamically from the JSON files
    feature_names = [f.name for f in tharion.features]
    assert "Martial Archetype" in feature_names
    assert "Improved Critical" in feature_names

@pytest.mark.asyncio
async def test_tool_ability_check_engine_coupling(mock_entities):
    """Tests that perform_ability_check_or_save respects live in-memory Engine modifiers, not just static YAML."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    config = {"configurable": {"thread_id": vault_path}}
    
    # Add an active buff directly to the engine
    entities = [e for e in get_all_entities().values() if e.name == char_name]
    tharion: Creature = entities[0]
    from dnd_rules_engine import NumericalModifier, ModifierPriority
    
    # Buff dexterity by +5 (e.g., from a magical effect like Cat's Grace)
    tharion.dexterity_mod.add_modifier(NumericalModifier(priority=ModifierPriority.ADDITIVE, value=5, source_name="Cat's Grace"))
    
    # Tharion's base DEX mod in YAML is 1. With buff, it should be 6.
    with patch('random.randint', return_value=10):
        res = await perform_ability_check_or_save.ainvoke({"character_name": char_name, "skill_or_stat_name": "stealth"}, config=config)
        
    assert "10 normally + 6 stat mod" in res
    
@pytest.mark.asyncio
async def test_tool_modify_health_concentration_coupling(mock_entities):
    """Tests that modify_health successfully alerts the LLM and/or drops concentration when bypassing the EventBus."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    config = {"configurable": {"thread_id": vault_path}}
    
    entities = [e for e in get_all_entities().values() if e.name == char_name]
    tharion: Creature = entities[0]
    tharion.concentrating_on = "Haste"
    
    # Test 1: Damage triggers alert
    res = modify_health.invoke({"target_name": char_name, "hp_change": -10, "reason": "Falling"}, config=config)
    assert "SYSTEM ALERT" in res
    assert "prompt a Constitution saving throw" in res
    assert tharion.concentrating_on == "Haste" # Still concentrating, waiting on roll
    
    # Test 2: Fatal damage auto-drops
    res2 = modify_health.invoke({"target_name": char_name, "hp_change": -20, "reason": "Falling"}, config=config)
    assert "dropped to 0 HP and lost concentration" in res2
    assert tharion.concentrating_on == "" # Dropped automatically!

@pytest.mark.asyncio
async def test_tool_modify_health_respects_resistances(mock_entities):
    """Tests that modify_health enforces damage resistances and immunities without hallucinating."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    config = {"configurable": {"thread_id": vault_path}}
    
    entities = [e for e in get_all_entities().values() if e.name == char_name]
    tharion: Creature = entities[0]
    tharion.resistances.append("fire")
    
    # 20 Fire damage should be halved to 10
    res = modify_health.invoke({"target_name": char_name, "hp_change": -20, "reason": "Lava Pit", "damage_type": "fire"}, config=config)
    assert "took 10 fire HP" in res
    assert tharion.hp.base_value == 15 # 25 base - 10

@pytest.mark.asyncio
async def test_tool_opportunity_attack_integration(mock_entities):
    """Tests that moving out of reach triggers an OA alert and execute_melee_attack resolves it as an async task."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    config = {"configurable": {"thread_id": vault_path}}
    
    tharion = [e for e in get_all_entities().values() if e.name == char_name][0]
    goblin = [e for e in get_all_entities().values() if e.name == target_name][0]
    
    # Setup spatial positioning (adjacent)
    tharion.x, tharion.y = 0.0, 0.0
    goblin.x, goblin.y = 5.0, 0.0
    spatial_service.sync_entity(tharion)
    spatial_service.sync_entity(goblin)
    
    # 1. Goblin moves away (triggers OA)
    move_result = await move_entity.ainvoke({
        "entity_name": target_name, "target_x": 15.0, "target_y": 0.0, "movement_type": "walk"
    }, config=config)
    
    assert "Opportunity Attacks from" in move_result
    assert char_name in move_result
    
    # 2. Execute the Opportunity Attack
    with patch('random.randint', side_effect=[18, 18, 4]): # Attack roll 18, Damage roll 4
        oa_result = await execute_melee_attack.ainvoke({"attacker_name": char_name, "target_name": target_name, "is_opportunity_attack": True}, config=config)
        
    assert "MECHANICAL TRUTH: HIT!" in oa_result
    assert tharion.reaction_used is True

@pytest.mark.asyncio
async def test_tool_manage_skill_challenge(mock_entities):
    """Tests that a skill challenge creates a whiteboard, tracks stats, and logs completion to the master log."""
    vault_path, _, _ = mock_entities
    config = {"configurable": {"thread_id": vault_path}}
    
    res1 = await manage_skill_challenge.ainvoke({"action": "start", "name": "Escape the Cave", "max_successes": 3, "max_failures": 3}, config=config)
    assert "started" in res1
    
    res2 = await manage_skill_challenge.ainvoke({"action": "update", "successes_delta": 1, "note": "Jumped a chasm"}, config=config)
    assert "[1/3 Successes]" in res2
    
    res3 = await manage_skill_challenge.ainvoke({"action": "update", "successes_delta": 2}, config=config)
    assert "VICTORY" in res3
    
    res4 = await manage_skill_challenge.ainvoke({"action": "end", "note": "The party escaped safely."}, config=config)
    assert "ended" in res4
    
    with open(os.path.join(vault_path, "Journals", "CAMPAIGN_MASTER.md"), "r", encoding="utf-8") as f:
        assert "The party escaped safely." in f.read()
