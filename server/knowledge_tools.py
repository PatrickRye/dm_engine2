# flake8: noqa: W293, E203
"""
knowledge_tools - Bestiary, rulebook, and campaign queries
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




@tool
def query_bestiary(
    creature_name: str, specific_section: str = "", *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """
    Retrieves exact stat blocks, abilities, tactics, and lore for a specific creature.
    Optional `specific_section` (e.g., 'Legendary Actions', 'Lair Actions') focuses the output on that specific block.
    """
    vault_path = config["configurable"].get("thread_id")
    _VAULT_CACHE.build_index(vault_path)

    search_name = creature_name.lower().strip()
    files_content = _VAULT_CACHE.bestiary_cache.get(vault_path, [])

    if not files_content:
        return "Error: Bestiary directory not configured in DM_CONFIG.md."

    for file, content in files_content:
        header_pattern = rf"^(#+)\s+.*?(?:\[.*?\]\(.*?\))?.*?{re.escape(search_name)}.*?$"
        match = re.search(header_pattern, content, re.IGNORECASE | re.MULTILINE)

        if match:
            header_level = len(match.group(1))
            tail = content[match.start() :]

            # Negative lookahead ensures we don't truncate early if Lair/Legendary actions are sibling headers
            next_header_pattern = re.compile(
                rf"^#{{1,{header_level}}}\s+(?!.*(?:Lair|Legendary|Mythic|Reactions)).*$", re.MULTILINE | re.IGNORECASE
            )
            next_match = next_header_pattern.search(tail, pos=len(match.group(0)))

            if next_match:
                body = tail[: next_match.start()].strip()
            else:
                body = tail.strip()

            if specific_section:
                section_pattern = rf"^(#+)\s+.*?{re.escape(specific_section)}.*?$"
                sec_match = re.search(section_pattern, body, re.IGNORECASE | re.MULTILINE)
                if not sec_match:
                    sec_match = re.search(section_pattern, content, re.IGNORECASE | re.MULTILINE)
                    if sec_match:
                        body = content

                if sec_match:
                    sec_level = len(sec_match.group(1))
                    sec_tail = body[sec_match.start() :]
                    next_sec_pattern = re.compile(rf"^#{{1,{sec_level}}}\s+", re.MULTILINE)
                    next_sec_match = next_sec_pattern.search(sec_tail, pos=len(sec_match.group(0)))
                    if next_sec_match:
                        content_block = sec_tail[: next_sec_match.start()].strip()
                        return f"--- {specific_section.upper()} FOR {creature_name.upper()} ---\n{content_block}"[:4000]
                    else:
                        content_block = sec_tail.strip()
                        return f"--- {specific_section.upper()} FOR {creature_name.upper()} ---\n{content_block}"[:4000]
                else:
                    return f"Cache Miss: '{specific_section}' not found for {creature_name}."

            return f"--- BESTIARY ENTRY FROM {file} ---\n{body}"[:6000]

    return _search_markdown_for_keywords(vault_path, "bestiary", f"{creature_name} {specific_section}".strip(), top_n=1)


@tool
def get_creature_tactics(
    creature_name: str, *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """
    Retrieves the Ammann tactical analysis for a creature: role, engagement style,
    action priorities, targeting heuristic, retreat thresholds, phase changes,
    and synergies. Use this when planning combat, narrating NPC tactics,
    or deciding how a creature behaves mid-battle.
    Returns a human-readable tactical briefing.
    """
    vault_path = config["configurable"].get("thread_id")
    entity = _get_entity_by_name(creature_name, vault_path)
    if not entity or not hasattr(entity, "creature_role"):
        return f"No tactical data found for '{creature_name}'. It may not have been hydrated as a creature yet."

    role = entity.creature_role or []
    engagement = entity.engagement_style or "default"
    priority = entity.combat_flow_priority or "standard attacks"
    recharge = "Yes — use recharge abilities immediately on Round 1" if entity.recharge_priority else "No recharge abilities"
    synergies = ", ".join(entity.action_synergies) if entity.action_synergies else "None identified"
    targeting = entity.targeting_heuristic or "standard"
    retreat_pct = entity.retreat_threshold_hp_pct
    if retreat_pct == 0 or entity.fanaticism_override:
        retreat = "Never retreats (fanaticism override)" if entity.fanaticism_override else "Never retreats"
    else:
        retreat = f"Retreat threshold: {retreat_pct}% HP remaining"
    evasion = entity.evasion_vector or "none"
    phase_pct = entity.phase_change_trigger_hp_pct
    phase_desc = entity.phase_change_description or "no phase change"
    unexpected = entity.unexpected_tactic or "none recorded"
    metaphor = entity.metaphorical_damage or "standard damage narration"
    environment = ", ".join(entity.expected_environment) if entity.expected_environment else "any environment"

    # Derive role description from Ammann role
    role_descriptions = {
        "Artillerist": "Seeks extreme range and cover, fires from safety",
        "Brute": "Charges the center, relies on kinetic close-quarters damage",
        "Controller": "Alters terrain, inflicts conditions, shapes the battlefield",
        "Elite": "Multi-stage mechanics, often accompanied by minions",
        "Lurker": "Ambush predator — stealth, burst damage, then retreat",
        "Minion": "Fights exclusively in packs, low HP, low Intelligence",
        "Skirmisher": "Hit-and-run, uses mobility for attrition",
        "Solo": "Designed to fight groups alone with Legendary/Lair actions",
        "Support": "Heals, buffs, or mitigates damage for allies",
        "Tank": "High AC + Con, protects softer allies",
    }
    role_text = ", ".join(f"{r} ({role_descriptions.get(r, r)})" for r in role) if role else "Role not yet deduced"

    # Targeting tier description
    targeting_descriptions = {
        "reckless": "Int/Wis ≤7: Attacks nearest source of pain or largest visual target",
        "reactive": "Int/Wis 8-11: Indiscriminate targeting, minor adjustments if tactics fail",
        "strategic": "Int/Wis 12-13: Bypasses tanks to strike fragile backline, coordinates flanks",
        "master_tactician": "Int/Wis ≥14: Targets healers/spellcasters first, delays AoE until targets cluster",
    }
    targeting_text = targeting_descriptions.get(targeting.lower(), targeting)

    return f"""
=== AMMANN TACTICAL BRIEFING: {entity.name} ===
{'':═<50}

ROLE(S): {role_text}
ENGAGEMENT: {engagement}
ACTION PRIORITY: {priority}
RECHARGE ABILITIES: {recharge}
ACTION SYNERGIES: {synergies}
TARGETING: {targeting_text}
RETREAT: {retreat}
EVASION VECTOR: {evasion}
ENVIRONMENT: Prefers {environment}

METAPHORICAL DAMAGE: {metaphor}
UNEXPECTED TACTIC: {unexpected}

PHASE CHANGE: {"At " + str(phase_pct) + "% HP — " + phase_desc if phase_pct > 0 else "No phase change"}
{'':═<50}
""".strip()


@tool
def query_rulebook(topic: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Searches the local D&D rules directories for game mechanics, spells, and systems."""
    vault_path = config["configurable"].get("thread_id")
    return _search_markdown_for_keywords(vault_path, "rules", topic, top_n=2)



@tool
def query_campaign_module(
    search_terms: list[str], current_chapter_context: str = "", *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """Searches pre-written campaign modules, lore bibles, and published adventure notes.
    Pass a list of unique nouns and aliases (e.g. ["Strahd", "Zarovich", "Devil"]) to ensure broad coverage."""
    vault_path = config["configurable"].get("thread_id")
    query = f"{current_chapter_context} " + " ".join(search_terms)
    return _search_markdown_for_keywords(vault_path, "modules", query, top_n=3)



__all__ = [
    "query_bestiary",
    "query_rulebook",
    "query_campaign_module",
]
