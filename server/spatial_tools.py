# flake8: noqa: W293, E203
"""
spatial_tools - Movement and positioning tools
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
from entity_tools import update_yaml_frontmatter
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
async def place_entity(
    entity_name: str,
    x: float,
    y: float,
    map_name: str = "",
    z: float = 0.0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Teleports an entity to an exact position and map without spending movement or triggering
    opportunity attacks.  Use this to:
      • Place a freshly created or JIT-loaded NPC at their correct starting location.
      • Move an entity between maps (e.g. from 'dungeon_floor1.jpg' to 'tavern.jpg').
      • Correct an entity that spawned at (0, 0) due to missing map/coordinate data.

    map_name — the map file name as stored in the vault (e.g. 'dungeon_floor1.jpg').
    Leave map_name blank to keep the entity on their current map (or the active map if unassigned)."""
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(entity_name, vault_path)
    if not entity:
        return f"SYSTEM ERROR: Entity '{entity_name}' not found."

    old_map = getattr(entity, "current_map", "")
    entity.x = x
    entity.y = y
    entity.z = z
    if map_name:
        entity.current_map = map_name

    spatial_service.sync_entity(entity)

    # Persist to vault so the placement survives a reload
    if hasattr(entity, "_filepath"):
        from vault_io import sync_engine_to_vault

        await sync_engine_to_vault(vault_path)

    map_info = f" on map '{entity.current_map}'" if entity.current_map else ""
    old_map_info = f" (moved from '{old_map}')" if old_map and old_map != entity.current_map else ""
    return f"Placed {entity.name} at ({x}, {y}, {z}){map_info}{old_map_info}. " f"No movement cost or opportunity attacks."



@tool
async def move_entity(  # noqa: C901
    entity_name: str,
    target_x: float,
    target_y: float,
    target_z: float = None,
    movement_type: str = "walk",
    standing_jump: bool = False,
    target_map: str = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Moves an entity to a new (X, Y, Z) coordinate on the spatial grid and visually updates the combat whiteboard.
    Valid movement_type values: 'walk', 'jump', 'climb', 'fly', 'teleport', 'crawl', 'disengage', 'forced', 'fall', 'travel'.
    'walk' and 'crawl' will be blocked by solid walls in a straight line.
    Set standing_jump=True for a jump without a 10ft running start (REQ-MOV-010: halves both long and high jump limits).
    Set target_map to move the entity to a different map (implies movement_type='teleport' for the map transition)."""
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(entity_name, vault_path)
    if not entity:
        return f"SYSTEM ERROR: Entity '{entity_name}' not found in active memory."

    if target_z is None:
        target_z = entity.z
    old_x, old_y, old_z = entity.x, entity.y, entity.z

    dz = target_z - old_z
    dist_3d = spatial_service.calculate_distance(old_x, old_y, old_z, target_x, target_y, target_z, vault_path)

    # --- Identify Dragged Entities & Riders ---
    dragged_entities = []
    riders = []
    for uid, ent in get_all_entities(vault_path).items():
        if isinstance(ent, Creature):
            if movement_type.lower() not in ["teleport", "fall", "forced"]:
                for cond in ent.active_conditions:
                    if cond.name.lower() == "grappled" and cond.source_uuid == entity.entity_uuid:
                        dragged_entities.append(ent)
            if getattr(ent, "mounted_on_uuid", None) == entity.entity_uuid:
                riders.append(ent)

    # --- REQ-GEO-006 & REQ-SPC-003: Cannot end movement in an occupied space ---
    forced_collision_msg = ""
    if movement_type.lower() not in ["teleport", "fall"]:
        dragged_uuids = {d.entity_uuid for d in dragged_entities}
        rider_uuids = {r.entity_uuid for r in riders}
        occupants = spatial_service.get_entities_at_position(
            target_x, target_y, entity.size, vault_path, exclude_uuid=entity.entity_uuid
        )
        valid_occupants = [
            occ
            for occ in occupants
            if occ.entity_uuid not in dragged_uuids
            and occ.entity_uuid not in rider_uuids
            and hasattr(occ, "hp")
            and getattr(occ.hp, "base_value", 0) > 0
        ]

        if valid_occupants:
            if movement_type.lower() == "forced":
                # REQ-SPC-003: Forced Displacement Collision (Shunt and Prone)
                dx, dy = target_x - old_x, target_y - old_y
                dist_val = math.hypot(dx, dy)
                if dist_val > 0:
                    nx, ny = dx / dist_val, dy / dist_val
                    for step in range(1, int(dist_val) + 5):
                        check_x = target_x - (nx * step)
                        check_y = target_y - (ny * step)
                        occ = spatial_service.get_entities_at_position(
                            check_x, check_y, entity.size, vault_path, exclude_uuid=entity.entity_uuid
                        )
                        v_occ = [
                            o
                            for o in occ
                            if o.entity_uuid not in dragged_uuids
                            and o.entity_uuid not in rider_uuids
                            and hasattr(o, "hp")
                            and getattr(o.hp, "base_value", 0) > 0
                        ]
                        if not v_occ:
                            target_x, target_y = check_x, check_y
                            break
                if not any(c.name.lower() == "prone" for c in entity.active_conditions):
                    entity.active_conditions.append(ActiveCondition(name="Prone", source_name="Forced Collision"))
                forced_collision_msg = (
                    f"\nSYSTEM ALERT (REQ-SPC-003): {entity.name} was forced into an occupied space! "
                    f"They were shunted to ({round(target_x, 1)}, {round(target_y, 1)}) and fell Prone."
                )
            else:
                return (
                    f"SYSTEM ERROR (REQ-GEO-006): {entity.name} cannot end their movement in "
                    f"{valid_occupants[0].name}'s occupied space. Choose an adjacent unoccupied square instead."
                )

    # --- Check for Wall Collisions ---
    if movement_type.lower() in ["walk", "crawl", "disengage"]:
        if dz > 1.5:
            return "SYSTEM ERROR: Cannot walk up vertical distances. Use 'jump', 'climb', or 'fly'."
        blocking_wall = spatial_service.check_path_collision(
            old_x, old_y, old_z, target_x, target_y, target_z, entity.height, vault_path=vault_path
        )
        if blocking_wall:
            str_score = (entity.strength_mod.total * 2) + 10
            run_jump, stand_jump = str_score, str_score // 2
            run_high, stand_high = 3 + entity.strength_mod.total, max(1, (3 + entity.strength_mod.total) // 2)
            lock_msg = (
                f" It is locked (DC {blocking_wall.interact_dc}). Use `interact_with_object` to pick/force it."
                if blocking_wall.is_locked
                else ""
            )
            return (
                f"SYSTEM ERROR: Movement blocked! Collided with '{blocking_wall.label}'.{lock_msg}\n"
                f"DM DIRECTIVE: You must inform the player their path is blocked and discuss options:\n"
                f"1. **Interact/Walk Around/Climb:** Execute multiple shorter `move_entity` calls to navigate corners, "
                f"or open the obstacle if it is a door.\n"
                f"2. **Jump:** {entity.name} can running-long-jump {run_jump}ft (standing {stand_jump}ft) and "  # noqa: E501
                f"running-high-jump {run_high}ft (standing {stand_high}ft). Running jumps require 10ft of prior movement. "  # noqa: E501
                f"Call `perform_ability_check_or_save` for Athletics if it exceeds bounds, then `move_entity` with "
                f"`movement_type='jump'`.\n"
                f"3. **Crawl/Squeeze:** If there's a gap, they can crawl (costs double movement).\n"
                f"4. **Magic:** Spells like Misty Step can `teleport` past obstacles."
            )
    elif movement_type.lower() == "jump":
        # REQ-MOV-010: Standing jump halves both long and high jump limits
        str_score = (entity.strength_mod.total * 2) + 10
        run_high = 3 + entity.strength_mod.total
        if standing_jump:
            max_long = str_score // 2
            max_high = max(1, run_high // 2)
            limit_note = f"standing long-jump: {max_long}ft, standing high-jump: {max_high}ft (halved — no running start)"
        else:
            max_long = str_score
            max_high = run_high
            limit_note = f"running long-jump: {str_score}ft, running high-jump: {run_high}ft"
        if dz > max_high or dist_3d > max_long:
            return (
                f"SYSTEM ERROR: Jump exceeds physical limits. Max {limit_note}. "
                f"Call perform_ability_check_or_save for Athletics to push limits."
            )

    # --- Determine if in active combat to enforce budget (Paradigm check) ---
    active_list = spatial_service.active_combatants.get(vault_path, [])
    in_combat = any(c.lower() == entity_name.lower() for c in active_list)

    # --- NEW: Check for Opportunity Attacks via EventBus ---
    event = GameEvent(
        event_type="Movement",
        source_uuid=entity.entity_uuid,
        vault_path=vault_path,
        payload={
            "target_x": target_x,
            "target_y": target_y,
            "target_z": target_z,
            "movement_type": movement_type,
            "dragged_uuids": [e.entity_uuid for e in dragged_entities],
            "ignore_budget": not in_combat,
        },
    )
    result = await EventBus.adispatch(event)

    if result.status == EventStatus.CANCELLED:
        error_msg = result.payload.get("error", "Movement cancelled by rules engine.")
        return (
            f"SYSTEM ERROR: {error_msg} Ask the player if they want to use their Action to 'Dash' "
            f"(call `use_dash_action`), pick a shorter route, or do something else."
        )

    # REQ-ENV-011: Save terrain zones BEFORE position update to detect enter/leave
    old_terrain_zones = list(spatial_service.get_entity_terrain_zones(entity.entity_uuid, vault_path))

    entity.x = target_x
    entity.y = target_y
    entity.z = target_z
    if target_map is not None:
        entity.current_map = target_map

    spatial_service.sync_entity(entity)

    # REQ-ENV-011: Terrain aura — apply/remove conditions based on zone tags like "aura:Deafened"
    terrain_aura_msgs = []
    new_terrain_zones = list(spatial_service.get_entity_terrain_zones(entity.entity_uuid, vault_path))
    old_zone_ids = {z.zone_id for z in old_terrain_zones}
    new_zone_ids = {z.zone_id for z in new_terrain_zones}
    entered_zones = [z for z in new_terrain_zones if z.zone_id not in old_zone_ids]
    left_zones = [z for z in old_terrain_zones if z.zone_id not in new_zone_ids]
    for zone in entered_zones:
        for tag in zone.tags:
            if tag.startswith("aura:"):
                cond_name = tag[5:]
                already = any(
                    c.name.lower() == cond_name.lower() and c.source_name == zone.label for c in entity.active_conditions
                )
                if not already:
                    entity.active_conditions.append(
                        ActiveCondition(name=cond_name, source_name=zone.label, duration_seconds=-1)
                    )
                    terrain_aura_msgs.append(
                        f"\nSYSTEM ALERT (REQ-ENV-011): {entity.name} entered '{zone.label}' "
                        f"— gained {cond_name} condition (aura). Will be removed when they leave."
                    )
    for zone in left_zones:
        for tag in zone.tags:
            if tag.startswith("aura:"):
                cond_name = tag[5:]
                before = len(entity.active_conditions)
                entity.active_conditions = [
                    c
                    for c in entity.active_conditions
                    if not (c.name.lower() == cond_name.lower() and c.source_name == zone.label)
                ]
                if len(entity.active_conditions) < before:
                    terrain_aura_msgs.append(
                        f"\nSYSTEM ALERT (REQ-ENV-011): {entity.name} left '{zone.label}' "
                        f"— lost {cond_name} condition (aura)."
                    )

    # REQ-ILL-001: Physical intersection auto-reveals non-phantasm illusion walls the entity passed through
    illusion_reveal_msgs = []
    if movement_type.lower() not in ["teleport", "fall"] and HAS_GIS:
        from shapely.geometry import LineString as _LineString

        path_line = _LineString([(old_x, old_y), (target_x, target_y)])
        entity_uuid_str = str(entity.entity_uuid)
        for illusion_wall in spatial_service.get_illusion_walls(vault_path):
            if illusion_wall.is_phantasm:
                continue  # Phantasm: physical pass-through doesn't auto-reveal (REQ-ILL-004)
            if (
                entity_uuid_str not in illusion_wall.revealed_for
                and illusion_wall.line
                and path_line.intersects(illusion_wall.line)
            ):
                illusion_wall.revealed_for.append(entity_uuid_str)
                spatial_service.invalidate_cache(vault_path)
                illusion_reveal_msgs.append(
                    f"\nSYSTEM ALERT (REQ-ILL-001): {entity.name} physically passed through illusion "
                    f"'{illusion_wall.label}' — it is now revealed to them as an illusion."
                )

    # REQ-MNT-005: Forced movement triggers Dex Save for all riders to stay mounted
    forced_dismount_msgs = []
    if movement_type.lower() == "forced" and riders:
        for r in riders:
            roll = random.randint(1, 20)
            total = roll + r.dexterity_mod.total
            if total < 10:
                r.mounted_on_uuid = None
                r.active_conditions.append(ActiveCondition(name="Prone", source_name="Forced Dismount"))
                forced_dismount_msgs.append(
                    f"\nSYSTEM ALERT (REQ-MNT-005): {r.name} failed DC 10 Dex save ({total}) and was thrown from their mount, landing Prone!"
                )
            else:
                forced_dismount_msgs.append(
                    f"\nSYSTEM ALERT (REQ-MNT-005): {r.name} succeeded DC 10 Dex save ({total}) and held onto their mount."
                )

    # Apply movement to dragged entities
    dx, dy = target_x - old_x, target_y - old_y
    drag_msg_parts = []
    for dragged in dragged_entities:
        new_dx, new_dy, new_dz = dragged.x + dx, dragged.y + dy, dragged.z + dz
        dragged.x, dragged.y, dragged.z = new_dx, new_dy, new_dz
        spatial_service.sync_entity(dragged)
        if hasattr(dragged, "_filepath"):
            await update_yaml_frontmatter.ainvoke(
                {"entity_name": dragged.name, "updates": {"x": new_dx, "y": new_dy, "z": new_dz}}, config
            )
        drag_msg_parts.append(dragged.name)

    rider_msg_parts = []
    for r in riders:
        if getattr(r, "mounted_on_uuid", None) == entity.entity_uuid:
            new_dx, new_dy, new_dz = r.x + dx, r.y + dy, r.z + dz
            r.x, r.y, r.z = new_dx, new_dy, new_dz
            spatial_service.sync_entity(r)
            if hasattr(r, "_filepath"):
                await update_yaml_frontmatter.ainvoke(
                    {"entity_name": r.name, "updates": {"x": new_dx, "y": new_dy, "z": new_dz}}, config
                )
            rider_msg_parts.append(r.name)

    # Persist the change to the entity's file
    if hasattr(entity, "_filepath"):
        await update_yaml_frontmatter.ainvoke(
            {"entity_name": entity.name, "updates": {"x": target_x, "y": target_y, "z": target_z}}, config
        )

    # Visually update the active combat board if it exists
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")
    if os.path.exists(file_path):
        try:
            async with edit_markdown_entity(file_path) as state:
                yaml_data = state["yaml_data"]
                for c in yaml_data.get("combatants", []):
                    if c.get("name", "").lower() == entity_name.lower():
                        c["x"], c["y"] = target_x, target_y
                    elif c.get("name", "") in drag_msg_parts:
                        c["x"], c["y"] = c.get("x", 0) + dx, c.get("y", 0) + dy
                    elif c.get("name", "") in rider_msg_parts:
                        c["x"], c["y"] = c.get("x", 0) + dx, c.get("y", 0) + dy
        except Exception:
            pass

    rem = int(entity.movement_remaining) if float(entity.movement_remaining).is_integer() else entity.movement_remaining
    base_msg = (
        f"MECHANICAL TRUTH: {entity.name} moved from ({old_x}, {old_y}) to ({target_x}, {target_y}) via {movement_type}. "
        f"Remaining movement: {rem}"
    )

    if drag_msg_parts:
        base_msg += f" They automatically dragged {', '.join(drag_msg_parts)} with them."
    if rider_msg_parts:
        base_msg += f" Their riders {', '.join(rider_msg_parts)} moved with them."
    for msg in forced_dismount_msgs:
        base_msg += msg
    if forced_collision_msg:
        base_msg += forced_collision_msg

    for aura_msg in terrain_aura_msgs:
        base_msg += aura_msg

    for ill_msg in illusion_reveal_msgs:
        base_msg += ill_msg

    if movement_type.lower() == "teleport":
        can_see = spatial_service.has_line_of_sight_to_point(entity.entity_uuid, target_x, target_y)
        if not can_see:
            base_msg += (
                "\nSYSTEM NOTE: The entity does NOT have line of sight to the teleport destination. Ensure the "
                "specific spell allows teleporting to unseen locations, otherwise this move is invalid."
            )

    attackers = result.payload.get("opportunity_attackers", [])

    # Fallback to alert DM of spatial triggers even if the engine suppressed them (e.g. reaction_used = True)
    # execute_melee_attack will strictly enforce the reaction limits if they try to attack.
    if movement_type.lower() not in ["teleport", "forced", "fall", "disengage"]:
        for other_entity in get_all_entities(vault_path).values():
            if (
                other_entity.entity_uuid != entity.entity_uuid
                and hasattr(other_entity, "hp")
                and getattr(other_entity.hp, "base_value", 0) > 0
            ):
                if other_entity.name not in attackers:
                    base_reach = _calculate_reach(other_entity, is_active_turn=False)
                    # REQ-GEO-011: Reach from bounding-box edge
                    eff_reach = base_reach + (other_entity.size / 2.0) + (entity.size / 2.0)

                    dist_before = spatial_service.calculate_distance(
                        old_x, old_y, old_z, other_entity.x, other_entity.y, other_entity.z, vault_path
                    )
                    dist_after = spatial_service.calculate_distance(
                        target_x, target_y, target_z, other_entity.x, other_entity.y, other_entity.z, vault_path
                    )
                    if dist_before < eff_reach and dist_after >= eff_reach:
                        attackers.append(other_entity.name)

    if attackers:
        base_msg += (
            f"\nSYSTEM ALERT: Movement provoked Opportunity Attacks from: {', '.join(attackers)}. You MUST ask "
            f"player(s) if they want to use their Reaction. For NPCs, you choose. Use "
            f"execute_melee_attack(is_reaction=True) to resolve."
        )

    if "trap_results" in result.payload and result.payload["trap_results"]:
        trap_msg = "\n".join(result.payload["trap_results"])
        base_msg += f"\nSYSTEM ALERT: TRAP TRIGGERED during movement!\n{trap_msg}"

    # --- FALLING DAMAGE AUTOMATION (REQ-MOV-006 & REQ-MOV-007) ---
    if movement_type.lower() == "fall":
        # Lazy import to avoid circular dependency
        from combat_tools import modify_health, toggle_condition
        fall_dist = old_z - target_z
        if fall_dist >= 10.0:
            dice_count = min(20, int(fall_dist // 10))
            dmg = sum(random.randint(1, 6) for _ in range(dice_count))

            # Apply the damage natively
            dmg_res = await modify_health.ainvoke(
                {"target_name": entity.name, "hp_change": -dmg, "reason": "Falling", "damage_type": "bludgeoning"},
                config=config,
            )
            base_msg += f"\n{dmg_res}"

            if dmg > 0 and not any(c.name.lower() == "prone" for c in getattr(entity, "active_conditions", [])):
                await toggle_condition.ainvoke(
                    {"character_name": entity.name, "condition_name": "Prone", "is_active": True}, config=config
                )
                base_msg += f"\nSYSTEM ALERT: {entity.name} took falling damage and landed Prone."

    return base_msg



@tool
async def manage_skill_challenge(
    action: str,
    name: str = "",
    max_successes: int = 3,
    max_failures: int = 3,
    successes_delta: int = 0,
    failures_delta: int = 0,
    note: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Manages a Skill Challenge or Progress Clock for complex non-combat encounters (chases, negotiations, puzzles).
    - action: 'start', 'update', or 'end'.
    - name: The title of the challenge (required for 'start').
    - max_successes / max_failures: The threshold to win or lose (used in 'start').
    - successes_delta / failures_delta: Add to the current totals (used in 'update').
    - note: A brief log of what happened (e.g. "Rogue successfully picked the lock (+1 Success)").
    """
    vault_path = config["configurable"].get("thread_id")
    j_dir = get_journals_dir(vault_path)
    file_path = os.path.join(j_dir, "ACTIVE_CHALLENGE.md")

    if action.lower() == "start":
        yaml_data = {
            "tags": ["challenge_whiteboard"],
            "challenge_name": name,
            "successes": 0,
            "max_successes": max_successes,
            "failures": 0,
            "max_failures": max_failures,
            "history": [note] if note else [],
        }
        dataview_js = (
            "```dataviewjs\nconst p = dv.current(); if (!p) return;\n"
            "dv.header(2, `⏱️ Skill Challenge: ${p.challenge_name}`);\n"
            "dv.paragraph(`**Successes:** ${p.successes} / ${p.max_successes} 🟩`);\n"
            "dv.paragraph(`**Failures:** ${p.failures} / ${p.max_failures} 🟥`);\n"
            'if (p.history && p.history.length > 0) {\n    dv.header(3, "📜 History");\n    dv.list(p.history);\n}\n```\n'
        )
        yaml_str = yaml.dump(yaml_data, sort_keys=False, default_flow_style=False)
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(f"---\n{yaml_str}---\n\n{dataview_js}")
        return (
            f"MECHANICAL TRUTH: Skill Challenge '{name}' started. "
            f"Target: {max_successes} Successes before {max_failures} Failures."
        )

    elif action.lower() == "update":
        if not os.path.exists(file_path):
            return "SYSTEM ERROR: No active skill challenge found. Use action='start' first."
        try:
            async with edit_markdown_entity(file_path) as state:
                yaml_data = state["yaml_data"]
                yaml_data["successes"] += successes_delta
                yaml_data["failures"] += failures_delta
                if note:
                    yaml_data.setdefault("history", []).append(note)
                s, ms = yaml_data["successes"], yaml_data["max_successes"]
                f, mf = yaml_data["failures"], yaml_data["max_failures"]

            outcome = "VICTORY" if s >= ms else ("DEFEAT" if f >= mf else "")
        except Exception as e:
            return str(e)

        if outcome:
            return (
                f"MECHANICAL TRUTH: Challenge updated. [{s}/{ms} Successes] | [{f}/{mf} Failures]. "
                f"SYSTEM ALERT: The challenge has reached a {outcome} condition! Use "
                f"manage_skill_challenge(action='end') to close it out and resolve the narrative consequences."
            )
        return f"MECHANICAL TRUTH: Challenge updated. [{s}/{ms} Successes] | [{f}/{mf} Failures]. Log: {note}"

    elif action.lower() == "end":
        if not os.path.exists(file_path):
            return "SYSTEM ERROR: No active skill challenge found."
        try:
            async with read_markdown_entity(file_path) as (yaml_data, _):
                s, f, c_name = (
                    yaml_data.get("successes", 0),
                    yaml_data.get("failures", 0),
                    yaml_data.get("challenge_name", "Challenge"),
                )
        except Exception:
            s, f, c_name = 0, 0, "Unknown"

        os.remove(file_path)
        summary = f"Completed Skill Challenge: '{c_name}' with {s} Successes and {f} Failures. {note}"
        await upsert_journal_section.ainvoke(
            {
                "entity_name": "CAMPAIGN_MASTER",
                "section_header": "Major Milestones (Event Log)",
                "content": f"- {summary}",
                "mode": "append",
            },
            config,
        )
        return (
            f"MECHANICAL TRUTH: Skill Challenge '{c_name}' ended and removed from whiteboard. "
            f"Event logged to CAMPAIGN_MASTER."
        )

    return "SYSTEM ERROR: Invalid action. Use 'start', 'update', or 'end'."


# Standard D&D 5e creature space (footprint) by size category
# Tiny: 2.5ft (1/4 of a 5-ft grid square)
# Small/Medium: 5ft (1 square)
# Large: 10ft (2×2 squares)
# Huge: 15ft (3×3 squares)
# Gargantuan: 20ft (4×4 squares)
_SIZE_TO_SPACE = {
    (0, 3.0): (2.5, 2.5),   # Tiny: 2.5 × 2.5 ft
    (3.0, 6.0): (5.0, 5.0),  # Small/Medium: 5 × 5 ft
    (6.0, 11.0): (10.0, 10.0),  # Large: 10 × 10 ft
    (11.0, 16.0): (15.0, 15.0),  # Huge: 15 × 15 ft
    (16.0, float("inf")): (20.0, 20.0),  # Gargantuan: 20 × 20 ft
}


@tool
async def get_entity_space(
    entity_name: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    REQ-GEO-001/002/003/004/005: Returns the D&D space (footprint) occupied by a creature
    based on its size category, per PHB Ch. 9.

    Returns width × depth in feet, and the number of 5-ft squares occupied.

    Size → Space:
      Tiny         (≤3ft) : 2.5 × 2.5 ft   (¼ square)
      Small/Medium (3–6ft) : 5 × 5 ft        (1 square)
      Large        (6–11ft): 10 × 10 ft      (2×2 squares)
      Huge        (11–16ft): 15 × 15 ft     (3×3 squares)
      Gargantuan   (16ft+) : 20 × 20 ft      (4×4 squares)
    """
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(entity_name, vault_path)
    if not entity:
        return f"SYSTEM ERROR: Entity '{entity_name}' not found."

    size = getattr(entity, "size", 5.0)
    footprint = None
    squares = 0
    for (low, high), dims in _SIZE_TO_SPACE.items():
        if low < size <= high:
            footprint = dims
            break

    if footprint is None:
        footprint = (5.0, 5.0)  # fallback

    width, depth = footprint
    squares = math.ceil(width / 5.0) * math.ceil(depth / 5.0)

    return (
        f"{entity.name} (size={size}ft): "
        f"Space = {width} × {depth} ft ({squares} five-foot square{'s' if squares > 1 else ''})."
    )


__all__ = [
    "place_entity",
    "move_entity",
    "manage_skill_challenge",
    "get_entity_space",
]
