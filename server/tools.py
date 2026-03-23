"""Backward-compatibility re-export. New code should import from domain modules directly.

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

# Re-export vault_io helpers that tools used to provide
from vault_io import (
    upsert_journal_section,
    write_audit_log,
    get_journals_dir,
    read_markdown_entity,
    edit_markdown_entity,
)

# Re-export everything from roll_utils for backward compatibility
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

# Re-export all @tool functions from domain modules
from combat_tools import *
from entity_tools import *
from item_tools import *
from map_tools import *
from spatial_tools import *
from combat_flow_tools import *
from narrative_tools import *
from knowledge_tools import *
from world_tools import *
