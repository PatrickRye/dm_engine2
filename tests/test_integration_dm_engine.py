import os
import yaml
import pytest
from unittest.mock import patch

from dnd_rules_engine import BaseGameEntity, Creature, MeleeWeapon, EventBus, resolve_attack_handler, apply_damage_handler
from vault_io import initialize_engine_from_vault
from tools import equip_item, execute_melee_attack

@pytest.fixture(autouse=True)
def setup_engine_state():
    BaseGameEntity._registry.clear()
    EventBus._listeners.clear()
    # Subscribe standard handlers for the engine test
    EventBus.subscribe("MeleeAttack", resolve_attack_handler, priority=10)
    EventBus.subscribe("MeleeAttack", apply_damage_handler, priority=100)
    yield

@pytest.fixture
def mock_entities(mock_obsidian_vault):
    """Helper fixture to seed the mocked vault with specific entities for this test file."""
    journals_dir = os.path.join(mock_obsidian_vault, "Journals")
    char_name, target_name = "Tharion", "Goblin"
    
    with open(os.path.join(journals_dir, f"{char_name}.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {char_name}\ntags: [pc]\nhp: 25\nac: 16\nstrength_mod: 3\ndexterity_mod: 1\nequipment: {{main_hand: Unarmed}}\n---\n")
        
    with open(os.path.join(journals_dir, f"{target_name}.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {target_name}\ntags: [monster]\nhp: 7\nac: 15\nstrength_mod: -1\ndexterity_mod: 2\nequipment: {{main_hand: Dagger}}\n---\n")
        
    return mock_obsidian_vault, char_name, target_name

@pytest.mark.asyncio
async def test_vault_to_engine_initialization(mock_entities):
    """Tests that YAML stats load correctly into the OO Engine, including weapon bridging."""
    vault_path, char_name, target_name = mock_entities
    await initialize_engine_from_vault(vault_path)
    
    entities = [e for e in BaseGameEntity._registry.values() if e.name == char_name]
    assert len(entities) == 1
    tharion: Creature = entities[0]
    
    assert tharion.hp.base_value == 25
    assert tharion.ac.base_value == 16
    assert tharion.dexterity_mod.base_value == 1
    
    assert tharion.equipped_weapon_uuid is not None
    weapon = BaseGameEntity.get(tharion.equipped_weapon_uuid)
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
    
    entities = [e for e in BaseGameEntity._registry.values() if e.name == char_name]
    tharion: Creature = entities[0]
    assert tharion.ac.base_value == 17 
    
    weapon = BaseGameEntity.get(tharion.equipped_weapon_uuid)
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
    
    with patch('random.randint', side_effect=[20, 10, 4, 4]): 
        result = await execute_melee_attack.ainvoke({"attacker_name": char_name, "target_name": target_name}, config=config)
    
    assert "MECHANICAL TRUTH: HIT!" in result
    assert "remaining" in result
