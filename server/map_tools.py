# flake8: noqa: W293, E203
"""
map_tools - Map geometry, terrain, traps, and lights
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
async def manage_map_geometry(
    action: str,
    label: str = "",
    start_x: float = 0.0,
    start_y: float = 0.0,
    end_x: float = 0.0,
    end_y: float = 0.0,
    z: float = 0.0,
    height: float = 10.0,
    is_solid: bool = True,
    is_visible: bool = True,
    is_locked: bool = False,
    interact_dc: int = 15,
    is_temporary: bool = True,
    hp: int = None,
    ac: int = 10,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Dynamically alters the spatial map's physical geometry (e.g., opening a door, breaking a wall, casting Wall of Stone).
    - hp / ac: Assign Hit Points and Armor Class if the wall/door is destructible (e.g. standard wooden door = hp 18, ac 15).
    - action: 'add_wall', 'remove_wall', or 'modify_wall'
    - label: A descriptive name for the wall (e.g., 'heavy oak door', 'Wall of Stone'). Use this to target an existing wall.
    - is_solid: False means entities can walk and shoot through it (like an open door).
    - is_visible: False means entities can see through it (like a glass window).
    - is_locked: True if the door/obstacle requires a key or check to open.
    - interact_dc: The DC to pick the lock or force the door.
    - is_temporary: True means the wall is a temporary effect and will be cleared on map reset.
    """
    if action.lower() == "add_wall":
        vp = config["configurable"].get("thread_id", "default")
        new_wall = Wall(
            label=label,
            start=(start_x, start_y),
            end=(end_x, end_y),
            z=z,
            height=height,
            is_solid=is_solid,
            is_visible=is_visible,
            is_locked=is_locked,
            interact_dc=interact_dc,
            hp=hp,
            max_hp=hp,
            ac=ac,
        )
        spatial_service.add_wall(new_wall, is_temporary=is_temporary, vault_path=vp)
        return (
            f"MECHANICAL TRUTH: Added new wall '{label}' from ({start_x}, {start_y}) "
            f"to ({end_x}, {end_y}). Solid: {is_solid}, Visible: {is_visible}."
        )

    elif action.lower() in ["remove_wall", "modify_wall"]:
        vp = config["configurable"].get("thread_id", "default")
        target_walls = [w for w in spatial_service.get_map_data(vp).active_walls if label.lower() in w.label.lower()]
        if not target_walls:
            return f"SYSTEM ERROR: No wall found matching label '{label}'. " "Please check the map or provide a broader label."

        target = target_walls[0]
        if action.lower() == "remove_wall":
            spatial_service.remove_wall(target.wall_id, vp)
            return f"MECHANICAL TRUTH: Removed wall/obstacle '{target.label}'."
        else:
            spatial_service.modify_wall(
                target.wall_id, is_solid=is_solid, is_visible=is_visible, is_locked=is_locked, vault_path=vp
            )
            return f"MECHANICAL TRUTH: Modified wall/obstacle '{target.label}'. " f"Solid: {is_solid}, Visible: {is_visible}."

    return "SYSTEM ERROR: Invalid action. Use 'add_wall', 'remove_wall', or 'modify_wall'."



@tool
async def manage_map_terrain(
    action: str,
    label: str = "",
    center_x: float = 0.0,
    center_y: float = 0.0,
    radius: float = 5.0,
    is_difficult: bool = True,
    tags: list[str] = None,
    is_temporary: bool = True,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Dynamically adds or removes terrain zones (e.g., Grease, Ice, Webs, Water).
    - action: 'add' or 'remove'.
    - tags: Pass elemental descriptors like 'wet', 'frozen', or 'flammable'.
    """
    from spatial_engine import TerrainZone

    vp = config["configurable"].get("thread_id", "default")
    if action.lower() == "add":
        tags = tags or []
        points = []
        for i in range(6):
            angle = 2 * math.pi * i / 6
            points.append((center_x + radius * math.cos(angle), center_y + radius * math.sin(angle)))

        tz = TerrainZone(label=label, points=points, is_difficult=is_difficult, tags=tags)
        spatial_service.add_terrain(tz, is_temporary=is_temporary, vault_path=vp)
        return (
            f"MECHANICAL TRUTH: Added terrain '{label}' at ({center_x}, {center_y}) " f"with radius {radius}ft. Tags: {tags}."
        )
    elif action.lower() == "remove":
        target_zones = [t for t in spatial_service.get_map_data(vp).active_terrain if label.lower() in t.label.lower()]
        if not target_zones:
            return f"SYSTEM ERROR: Terrain '{label}' not found."
        spatial_service.remove_terrain(target_zones[0].zone_id, vp)
        return f"MECHANICAL TRUTH: Removed terrain '{target_zones[0].label}'."
    return "SYSTEM ERROR: Invalid action."



@tool
async def manage_map_trap(
    target_label: str,
    hazard_name: str,
    trigger_on_interact_fail: bool = False,
    trigger_on_move: bool = False,
    trigger_on_turn_start: bool = False,
    is_persistent: bool = False,
    is_disarmable: bool = True,
    requires_attack_roll: bool = False,
    attack_bonus: int = 5,
    save_required: str = "",
    save_dc: int = 15,
    damage_dice: str = "",
    damage_type: str = "",
    condition_applied: str = "",
    radius: float = 0.0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Attaches a trap or alarm to an existing wall, door, or terrain zone on the spatial map.
    It can trigger on interact failures, entering the zone (trigger_on_move), or starting a turn in the zone.
    """
    from spatial_engine import TrapDefinition

    vp = config["configurable"].get("thread_id", "default")

    target = None
    for w in spatial_service.get_map_data(vp).active_walls:
        if target_label.lower() in w.label.lower():
            target = w
            break
    if not target:
        for t in spatial_service.get_map_data(vp).active_terrain:
            if target_label.lower() in t.label.lower():
                target = t
                break

    if not target:
        return f"SYSTEM ERROR: No wall, door, or terrain found matching '{target_label}'."

    target.trap = TrapDefinition(
        hazard_name=hazard_name,
        requires_attack_roll=requires_attack_roll,
        attack_bonus=attack_bonus,
        save_required=save_required.lower(),
        save_dc=save_dc,
        damage_dice=damage_dice,
        damage_type=damage_type.lower(),
        condition_applied=condition_applied,
        trigger_on_interact_fail=trigger_on_interact_fail,
        trigger_on_move=trigger_on_move,
        trigger_on_turn_start=trigger_on_turn_start,
        is_persistent=is_persistent,
        is_disarmable=is_disarmable,
        radius=radius,
    )
    return f"MECHANICAL TRUTH: Successfully trapped '{target.label}' with '{hazard_name}'."



@tool
async def discover_trap(target_label: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Marks a trap as 'known_by_players' after it has been successfully detected."""
    vp = config["configurable"].get("thread_id", "default")
    target = None
    for w in spatial_service.get_map_data(vp).active_walls:
        if target_label.lower() in w.label.lower():
            target = w
            break
    if not target:
        for t in spatial_service.get_map_data(vp).active_terrain:
            if target_label.lower() in t.label.lower():
                target = t
                break

    if not target or not target.trap:
        return f"SYSTEM ERROR: No trap found on object '{target_label}'."

    target.trap.known_by_players = True
    return f"MECHANICAL TRUTH: The trap on '{target.label}' is now known to the players."



@tool
async def manage_light_sources(
    action: str,
    label: str,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 5.0,
    bright_radius: float = 20.0,
    dim_radius: float = 40.0,
    attached_to_entity: str = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Dynamically adds or removes light sources (e.g. torches, spells) from the spatial environment."""
    if action.lower() == "add":
        ent_uuid = None
        ent_msg = ""
        if attached_to_entity:
            vault_path = config["configurable"].get("thread_id")
            entity = await _get_entity_by_name(attached_to_entity, vault_path)
            if entity:
                ent_uuid = entity.entity_uuid
                x, y, z = entity.x, entity.y, entity.z
                ent_msg = f" attached to {entity.name}"
            else:
                return f"SYSTEM ERROR: Entity '{attached_to_entity}' not found."

        new_light = LightSource(
            label=label, x=x, y=y, z=z, bright_radius=bright_radius, dim_radius=dim_radius, attached_to_entity_uuid=ent_uuid
        )
        vp = config["configurable"].get("thread_id", "default")
        spatial_service.get_map_data(vp).lights.append(new_light)
        return (
            f"MECHANICAL TRUTH: Added light source '{label}' at ({x}, {y}, {z}){ent_msg} "
            f"with Bright/Dim radii ({bright_radius}/{dim_radius})."
        )
    elif action.lower() == "remove":
        vp = config["configurable"].get("thread_id", "default")
        initial_count = len(spatial_service.get_map_data(vp).lights)
        spatial_service.get_map_data(vp).lights = [
            light for light in spatial_service.get_map_data(vp).lights if light.label.lower() != label.lower()
        ]
        if len(spatial_service.get_map_data(vp).lights) < initial_count:
            return f"MECHANICAL TRUTH: Removed light source '{label}'."
        return f"SYSTEM ERROR: Light source '{label}' not found."
    return "SYSTEM ERROR: Invalid action. Use 'add' or 'remove'."



@tool
async def ingest_battlemap_json(map_json_str: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    Parses a complete JSON map payload (generated by the Vision Map Ingestion model)
    and natively bulk-loads all walls, terrain, and lights into the Spatial Engine.
    """
    import json
    from spatial_engine import MapData

    try:
        # Clean markdown code blocks if the LLM wrapped the JSON payload
        clean_json = map_json_str.strip()
        if clean_json.startswith("```json"):
            clean_json = clean_json[7:]
        elif clean_json.startswith("```"):
            clean_json = clean_json[3:]
        if clean_json.endswith("```"):
            clean_json = clean_json[:-3]

        map_dict = json.loads(clean_json.strip())
        new_map_data = MapData(**map_dict)
        vp = config["configurable"].get("thread_id", "default")
        spatial_service.load_map(new_map_data, vp)

        wall_cnt = len(new_map_data.walls)
        terr_cnt = len(new_map_data.terrain)
        lght_cnt = len(new_map_data.lights)

        return (
            f"MECHANICAL TRUTH: Successfully ingested battlemap. Loaded {wall_cnt} walls, "
            f"{terr_cnt} terrain zones, and {lght_cnt} light sources."
        )
    except Exception as e:
        return f"SYSTEM ERROR: Failed to ingest battlemap JSON. Error: {str(e)}"


# ===== REQ-ILL: Illusion Wall Tools =====



@tool
async def create_illusion_wall(
    label: str,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    spell_dc: int = 13,
    is_phantasm: bool = False,
    z: float = 0.0,
    height: float = 10.0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-ILL: Creates an illusory wall segment.
    The illusion blocks line of sight (is_visible=True) but NOT physical movement or attacks (is_solid=False).
    is_phantasm=True means physical intersection by others does NOT auto-reveal it (mental-only illusion).
    spell_dc is the Investigation DC to see through it (REQ-ILL-002)."""
    vault_path = config["configurable"].get("thread_id")
    wall = Wall(
        label=label,
        start=(start_x, start_y),
        end=(end_x, end_y),
        z=z,
        height=height,
        is_solid=False,
        is_visible=True,
        is_illusion=True,
        is_phantasm=is_phantasm,
        illusion_spell_dc=spell_dc,
    )
    spatial_service.add_wall(wall, is_temporary=True, vault_path=vault_path)
    kind = "phantasm (mental only)" if is_phantasm else "illusion"
    return (
        f"MECHANICAL TRUTH (REQ-ILL): Created {kind} wall '{label}' "
        f"from ({start_x}, {start_y}) to ({end_x}, {end_y}). "
        f"It blocks line of sight but NOT physical movement or attacks. "
        f"Investigation DC {spell_dc} to disbelieve (REQ-ILL-002). "
        f"Wall ID: {wall.wall_id}"
    )



@tool
async def investigate_illusion(
    entity_name: str,
    illusion_label: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-ILL-002: Entity spends an action to investigate a suspected illusion.
    Performs an Intelligence (Investigation) check vs the illusion's spell DC.
    On success the illusion is revealed to that entity (they see through it)."""
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(entity_name, vault_path)
    if not entity:
        return f"SYSTEM ERROR: Entity '{entity_name}' not found."

    # REQ-ILL-006: Truesight auto-succeeds
    has_truesight = any(t == "truesight" or t.startswith("truesight_") for t in getattr(entity, "tags", []))

    illusion_walls = spatial_service.get_illusion_walls(vault_path)
    target_wall = None
    for w in illusion_walls:
        if w.label.lower() == illusion_label.lower():
            target_wall = w
            break
    if not target_wall:
        return (
            f"SYSTEM ERROR: No illusion wall named '{illusion_label}' found. "
            f"Available illusions: {[w.label for w in illusion_walls] or 'none'}"
        )

    entity_uuid_str = str(entity.entity_uuid)
    if entity_uuid_str in target_wall.revealed_for:
        return f"MECHANICAL TRUTH: {entity.name} already sees through '{illusion_label}' — it is revealed to them."

    if has_truesight:
        target_wall.revealed_for.append(entity_uuid_str)
        spatial_service.invalidate_cache(vault_path)
        return (
            f"MECHANICAL TRUTH (REQ-ILL-006): {entity.name} has Truesight — "
            f"they automatically see through '{illusion_label}'. It is now revealed to them."
        )

    # Roll Intelligence (Investigation)
    int_mod = getattr(entity, "intelligence_mod", None)
    int_bonus = int_mod.total if int_mod else 0
    roll = random.randint(1, 20)
    total = roll + int_bonus
    dc = target_wall.illusion_spell_dc

    if total >= dc:
        target_wall.revealed_for.append(entity_uuid_str)
        spatial_service.invalidate_cache(vault_path)
        return (
            f"MECHANICAL TRUTH (REQ-ILL-002): {entity.name} investigates '{illusion_label}': "
            f"rolled {roll} + {int_bonus} = {total} vs DC {dc}. SUCCESS — they see through the illusion! "
            f"'{illusion_label}' is now transparent to {entity.name}."
        )
    else:
        return (
            f"MECHANICAL TRUTH (REQ-ILL-002): {entity.name} investigates '{illusion_label}': "
            f"rolled {roll} + {int_bonus} = {total} vs DC {dc}. FAILURE — the illusion holds."
        )



@tool
async def reveal_illusion(
    illusion_label: str,
    entity_name: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-ILL-003: Reveals an illusion wall (post-reveal = transparent, no longer blocks LOS).
    If entity_name is provided, reveals it only to that entity; otherwise reveals it globally (all entities).
    Use this when an illusion is dispelled, the caster is incapacitated, or a DM narrative event reveals it."""
    vault_path = config["configurable"].get("thread_id")
    illusion_walls = spatial_service.get_illusion_walls(vault_path)
    target_wall = None
    for w in illusion_walls:
        if w.label.lower() == illusion_label.lower():
            target_wall = w
            break
    if not target_wall:
        return (
            f"SYSTEM ERROR: No illusion wall named '{illusion_label}'. "
            f"Available: {[w.label for w in illusion_walls] or 'none'}"
        )

    if entity_name:
        entity = await _get_entity_by_name(entity_name, vault_path)
        if not entity:
            return f"SYSTEM ERROR: Entity '{entity_name}' not found."
        uid_str = str(entity.entity_uuid)
        if uid_str not in target_wall.revealed_for:
            target_wall.revealed_for.append(uid_str)
        spatial_service.invalidate_cache(vault_path)
        return (
            f"MECHANICAL TRUTH (REQ-ILL-003): '{illusion_label}' has been revealed to {entity.name}. "
            f"It is now transparent to them (no longer blocks their line of sight)."
        )
    else:
        # Global reveal: make wall no longer block vision for anyone
        target_wall.is_visible = False
        spatial_service.invalidate_cache(vault_path)
        return (
            f"MECHANICAL TRUTH (REQ-ILL-003): '{illusion_label}' has been globally revealed — "
            f"it is now transparent to all creatures (no longer blocks line of sight)."
        )


# =============================================================================
# STORYLET ORCHESTRATION TOOLS
# =============================================================================



__all__ = [
    "manage_map_geometry",
    "manage_map_terrain",
    "manage_map_trap",
    "discover_trap",
    "manage_light_sources",
    "ingest_battlemap_json",
    "create_illusion_wall",
    "investigate_illusion",
    "reveal_illusion",
]
