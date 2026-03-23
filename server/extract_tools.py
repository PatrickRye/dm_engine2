"""
Script to extract @tool functions from tools.py into domain modules.
"""
import re
import os

# Read tools.py
with open('tools.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Get the module-level imports and helper code (before first @tool)
lines = content.split('\n')

# Find the first @tool
first_tool_line = None
for i, line in enumerate(lines):
    if '@tool' in line and not line.strip().startswith('#'):
        first_tool_line = i
        break

# Extract module header (imports and helper code up to first @tool)
module_header = '\n'.join(lines[:first_tool_line])

# Find all @tool decorated functions and their boundaries
tool_starts = []
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('@tool'):
        # Find the def on the next line(s)
        for j in range(i+1, min(i+10, len(lines))):
            if 'def ' in lines[j]:
                tool_starts.append((i, j))
                break

# Add end marker
tool_starts.append((len(lines), len(lines)))

# Helper to extract function code
def extract_func(idx):
    """Extract function code (0-indexed idx)"""
    at_line, def_line = tool_starts[idx]
    end_line = tool_starts[idx + 1][0]
    return '\n'.join(lines[at_line:end_line])

# Domain mapping (0-indexed indices)
domain_map = {
    'combat_tools': [0, 1, 28, 43, 45, 35, 33, 34, 32, 46, 38, 37, 14],
    'entity_tools': [2, 3, 4, 5, 11, 10, 30, 67, 68, 54],
    'item_tools': [6, 7, 8, 9, 12, 47, 29],
    'map_tools': [39, 40, 41, 42, 36, 48, 49, 50, 51],
    'spatial_tools': [21, 22, 44],
    'combat_flow_tools': [18, 19, 20],
    'narrative_tools': [52, 53, 55, 56, 57, 58, 59, 61, 60, 64, 65, 66, 63, 62],
    'knowledge_tools': [25, 26, 27],
    'world_tools': [15, 16, 17, 23, 24, 13, 31],
}

# Module header template for each domain module
MODULE_HEADER = '''# flake8: noqa: W293, E203
"""
{domain} - {description}
"""
import os
import re
import yaml
import random
import math
import aiofiles
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from pydantic import Field
from typing import Optional, Annotated, Union, Dict
import uuid

# === DETERMINISTIC ENGINE INTEGRATION ===
from dnd_rules_engine import (
    EventBus,
    GameEvent,
    EventStatus,
    BaseGameEntity,
    Creature,
    MeleeWeapon,
    ActiveCondition,
    ModifiableValue,
    NumericalModifier,
    ModifierPriority,
    WeaponProperty,
)
from state import ClassLevel, PCDetails, NPCDetails, LocationDetails, FactionDetails
from vault_io import (
    get_journals_dir,
    write_audit_log,
    upsert_journal_section,
    read_markdown_entity,
    edit_markdown_entity,
)
from compendium_manager import CompendiumManager, CompendiumEntry, MechanicEffect
from spatial_engine import spatial_service, LightSource, Wall, HAS_GIS
from spell_system import SpellDefinition, SpellMechanics, SpellCompendium
from item_system import WeaponItem, ArmorItem, WondrousItem, ItemCompendium

from registry import get_all_entities, register_entity, get_entity, get_candidate_uuids_by_prefix

# Import helpers from roll_utils
from roll_utils import (
    VaultCache,
    _VAULT_CACHE,
    update_roll_automations,
    get_roll_automations,
    _calculate_reach,
    _build_npc_template,
    _build_location_template,
    _build_faction_template,
    _build_pc_template,
    _build_party_tracker,
    _get_config_tone,
    _get_config_settings,
    _get_config_dirs,
    _search_markdown_for_keywords,
    _get_entity_by_name,
    _get_current_combat_initiative,
)


'''

domain_descriptions = {
    'combat_tools': 'Combat and action tools - attack, damage, conditions',
    'entity_tools': 'Entity lifecycle tools - create, spawn, update entities',
    'item_tools': 'Inventory and equipment tools',
    'map_tools': 'Map geometry, terrain, traps, and lights',
    'spatial_tools': 'Movement and positioning tools',
    'combat_flow_tools': 'Initiative and combat management',
    'narrative_tools': 'Story, graph mutations, and backstory tools',
    'knowledge_tools': 'Bestiary, rulebook, and campaign queries',
    'world_tools': 'Time, rest, dice, and world tools',
}

# Generate each domain module
for domain, indices in domain_map.items():
    funcs = []
    for idx in indices:
        func_code = extract_func(idx)
        funcs.append(func_code)

    # Get function names for __all__
    func_names = []
    for idx in indices:
        _, def_line = tool_starts[idx]
        func_def = lines[def_line]
        func_name = func_def.split('(')[0].replace('async def ', '').replace('def ', '').strip()
        func_names.append(func_name)

    module_content = MODULE_HEADER.format(
        domain=domain,
        description=domain_descriptions.get(domain, '')
    )

    # Add all functions
    for func_code in funcs:
        module_content += '\n\n' + func_code

    # Add __all__
    module_content += '\n\n__all__ = [\n'
    for name in func_names:
        module_content += f'    "{name}",\n'
    module_content += ']\n'

    # Write module
    with open(f'{domain}.py', 'w', encoding='utf-8') as f:
        f.write(module_content)

    print(f"Created {domain}.py with {len(indices)} functions")

# Create tools.py stub
stub_content = '''"""Backward-compatibility re-export. New code should import from domain modules directly.

New modules:
  roll_utils       — VaultCache, roll helpers, template builders
  combat_tools     — attack, damage, conditions, combat actions
  entity_tools     — create/spawn/update entities
  item_tools       — equipment, inventory, items
  map_tools        — map geometry, terrain, traps, lights
  spatial_tools    — movement, positioning
  combat_flow_tools — initiative, combat state
  narrative_tools  — storylets, graph mutations, backstory
  knowledge_tools  — bestiary, rulebook, campaign queries
  world_tools      — time, rest, dice
"""

# Re-export everything from domain modules for backward compatibility
from roll_utils import (
    VaultCache,
    _VAULT_CACHE,
    update_roll_automations,
    get_roll_automations,
    _calculate_reach,
    _build_npc_template,
    _build_location_template,
    _build_faction_template,
    _build_pc_template,
    _build_party_tracker,
    _get_config_tone,
    _get_config_settings,
    _get_config_dirs,
    _search_markdown_for_keywords,
)

from combat_tools import *
from entity_tools import *
from item_tools import *
from map_tools import *
from spatial_tools import *
from combat_flow_tools import *
from narrative_tools import *
from knowledge_tools import *
from world_tools import *
'''

with open('tools.py', 'w', encoding='utf-8') as f:
    f.write(stub_content)

print("\\nCreated tools.py stub")
print("\\nDone! Total functions distributed:")
total = sum(len(v) for v in domain_map.values())
print(f"  Total: {total} functions in {len(domain_map)} domains")
