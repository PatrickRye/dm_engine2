# flake8: noqa: W293, E203
"""
combat_flow_tools - Initiative and combat management
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
async def start_combat(
    pc_names: list[str],
    enemies: list[dict],
    surprised_names: list[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Creates ACTIVE_COMBAT.md. surprised_names: list of combatant names that are surprised (roll initiative twice, take lower)."""
    vault_path = config["configurable"].get("thread_id")
    j_dir = get_journals_dir(vault_path)
    combatants = []
    surprised_set = set(surprised_names or [])

    for pc in pc_names:
        file_path = os.path.join(j_dir, f"{pc}.md")
        pc_dex_mod, pc_hp, pc_ac, pc_x, pc_y, pc_z = 0, 10, 10, 0.0, 0.0, 0.0
        try:
            async with read_markdown_entity(file_path) as (yaml_data, body_text):
                pc_dex_mod = math.floor((int(yaml_data.get("dexterity", yaml_data.get("dex", 10))) - 10) / 2)
                pc_hp = int(yaml_data.get("max_hp", 10))
                pc_ac = int(yaml_data.get("ac", 10))
                pc_x = float(yaml_data.get("x", 0.0))
                pc_y = float(yaml_data.get("y", 0.0))
                pc_z = float(yaml_data.get("z", 0.0))
                hp_match = re.search(r"- Current HP:\s*(\d+)", body_text)
                if hp_match:
                    pc_hp = int(hp_match.group(1))
        except Exception:
            pass  # Ignores missing PCs or syntax errors, defaulting to 10

        # REQ-SRP-001: Surprised combatants roll initiative twice and take the lower result
        if pc in surprised_set:
            pc_init = min(random.randint(1, 20), random.randint(1, 20)) + pc_dex_mod
        else:
            pc_init = random.randint(1, 20) + pc_dex_mod

        combatants.append(
            {
                "name": pc,
                "init": pc_init,
                "hp": pc_hp,
                "max_hp": pc_hp,
                "ac": pc_ac,
                "conditions": [],
                "is_pc": True,
                "x": pc_x,
                "y": pc_y,
                "z": pc_z,
            }
        )

    for enemy in enemies:
        enemy_name = enemy.get("name", "Unknown")
        enemy_dex_mod = int(enemy.get("dex_mod", 0))
        # REQ-SRP-001: Surprised combatants roll initiative twice and take the lower result
        if enemy_name in surprised_set:
            enemy_init = min(random.randint(1, 20), random.randint(1, 20)) + enemy_dex_mod
        else:
            enemy_init = random.randint(1, 20) + enemy_dex_mod
        combatants.append(
            {
                "name": enemy_name,
                "init": enemy_init,
                "hp": int(enemy.get("hp", 10)),
                "max_hp": int(enemy.get("hp", 10)),
                "ac": int(enemy.get("ac", 10)),
                "conditions": [],
                "is_pc": False,
                "x": float(enemy.get("x", 0.0)),
                "y": float(enemy.get("y", 0.0)),
                "z": float(enemy.get("z", 0.0)),
                "surprised": enemy_name in surprised_set,
            }
        )

    # Enrich all combatants with Ammann tactical fields from the engine entity
    for c in combatants:
        if not c.get("is_pc"):
            c = await _enrich_combatant_with_ammann(c, vault_path)

    combatants = sorted(combatants, key=lambda x: x["init"], reverse=True)
    yaml_str = yaml.dump(
        {"tags": ["combat_whiteboard"], "round": 1, "current_turn_index": 0, "combatants": combatants, "readied_actions": []},
        sort_keys=False,
        default_flow_style=False,
    )
    dataview_js = (
        "```dataviewjs\n"
        "const p = dv.current(); if (!p || !p.combatants) return;\n"
        "let tbl = p.combatants.map((c, i) => [\n"
        '  i === p.current_turn_index ? "👉 "+c.init : c.init,\n'
        "  c.name,\n"
        "  `${c.hp}/${c.max_hp}`,\n"
        "  c.ac,\n"
        "  (c.ammann && c.ammann.creature_role ? c.ammann.creature_role.join(', ') : '—'),\n"
        "  `(${c.x||0}, ${c.y||0}, ${c.z||0})`,\n"
        '  c.hp <= 0 ? "💀 Dead" : (c.conditions.length ? c.conditions.join(", ") : "Healthy")\n'
        "]);\n"
        'dv.header(2, "⚔️ Active Combat Tracker ⚔️"); dv.paragraph(`**Round:** ${p.round}`);\n'
        'dv.table(["Init", "Combatant", "HP", "AC", "Role (Ammann)", "Pos (x,y,z)", "Status"], tbl);\n'
        "if (p.readied_actions && p.readied_actions.length > 0) {\n"
        '  dv.header(3, "⏱️ Readied Actions");\n'
        "  let raData = p.readied_actions.map(ra => [ra.character, ra.trigger, ra.action]);\n"
        '  dv.table(["Character", "Trigger", "Action"], raData);\n}\n```\n'
    )

    async with aiofiles.open(os.path.join(j_dir, "ACTIVE_COMBAT.md"), "w", encoding="utf-8") as f:
        await f.write(f"---\n{yaml_str}---\n\n{dataview_js}")

    # Update engine memory
    spatial_service.active_combatants[vault_path] = [c["name"] for c in combatants]

    first_ent = await _get_entity_by_name(combatants[0]["name"], vault_path)
    if first_ent and hasattr(first_ent, "speed"):
        first_ent.movement_remaining = max(0, first_ent.speed - (getattr(first_ent, "exhaustion_level", 0) * 5))
        sot_event = GameEvent(event_type="StartOfTurn", source_uuid=first_ent.entity_uuid, vault_path=vault_path)
        await EventBus.adispatch(sot_event)
        if "results" in sot_event.payload and sot_event.payload["results"]:
            return f"Combat started! {combatants[0]['name']} goes first.\n" + "\n".join(sot_event.payload["results"])

    return f"Combat started! {combatants[0]['name']} goes first."



@tool
async def update_combat_state(  # noqa: C901
    combatant_name: str = None,
    hp_change: int = 0,
    added_conditions: list[str] = None,
    next_turn: bool = False,
    force_advance: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Applies damage/healing to combatants and advances the initiative turn order."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")
    if added_conditions is None:
        added_conditions = []

    advance_global_clock = False
    new_init = None
    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            log_msg = []
            combatants = yaml_data.get("combatants", [])
            if combatant_name:
                for c in combatants:
                    if c["name"].lower() == combatant_name.lower():
                        if hp_change != 0:
                            c["hp"] = max(0, min(c["max_hp"], c["hp"] + hp_change))
                            log_msg.append(
                                f"{c['name']} {'healed' if hp_change > 0 else 'took damage'}. HP: {c['hp']}/{c['max_hp']}."
                            )
                        if added_conditions:
                            c["conditions"].extend(added_conditions)
                            log_msg.append(f"{c['name']} gained conditions: {', '.join(added_conditions)}.")

            if next_turn and not force_advance:
                current_combatant = combatants[yaml_data.get("current_turn_index", 0)]
                interrupts = []
                for c in combatants:
                    if c["name"] == current_combatant["name"] or c["hp"] <= 0:
                        continue
                    eng_ent = await _get_entity_by_name(c["name"], vault_path)
                    if eng_ent and isinstance(eng_ent, Creature) and eng_ent.legendary_actions_current > 0:
                        interrupts.append(f"{c['name']} ({eng_ent.legendary_actions_current} LA left)")

                if interrupts:
                    return (
                        f"SYSTEM ALERT: Turn advancement paused! {', '.join(interrupts)} have Legendary Actions. "
                        f"Use combat tools with `is_legendary_action=True` to execute them, then call "
                        f"`update_combat_state(next_turn=True, force_advance=True)` to proceed."
                    )

            if next_turn:
                current_combatant = combatants[yaml_data.get("current_turn_index", 0)]
                current_ent = await _get_entity_by_name(current_combatant["name"], vault_path)
                if current_ent and isinstance(current_ent, Creature):
                    eot_event = GameEvent(
                        event_type="EndOfTurn",
                        source_uuid=current_ent.entity_uuid,
                        vault_path=vault_path,
                    )
                    await EventBus.adispatch(eot_event)
                    if "results" in eot_event.payload and eot_event.payload["results"]:
                        log_msg.extend(eot_event.payload["results"])

                    # Refresh the conditions column in the whiteboard to show cleared conditions
                    current_combatant["conditions"] = [c.name for c in current_ent.active_conditions]

                yaml_data["current_turn_index"] = (yaml_data.get("current_turn_index", 0) + 1) % len(combatants)
                if yaml_data["current_turn_index"] == 0:
                    yaml_data["round"] = yaml_data.get("round", 1) + 1
                    advance_global_clock = True

                loop_counter = 0
                while combatants[yaml_data["current_turn_index"]]["hp"] <= 0 and loop_counter < len(combatants):
                    yaml_data["current_turn_index"] = (yaml_data["current_turn_index"] + 1) % len(combatants)
                    loop_counter += 1

                new_init = combatants[yaml_data["current_turn_index"]]["init"]
                log_msg.append(f"Turn advanced to {combatants[yaml_data['current_turn_index']]['name']}.")

                # Reset reactions and legendary actions for the character whose turn is starting
                new_turn_ent = await _get_entity_by_name(combatants[yaml_data["current_turn_index"]]["name"], vault_path)
                if new_turn_ent and isinstance(new_turn_ent, Creature):
                    new_turn_ent.reaction_used = False
                    new_turn_ent.legendary_actions_current = new_turn_ent.legendary_actions_max
                    new_turn_ent.movement_remaining = max(0, new_turn_ent.speed - (new_turn_ent.exhaustion_level * 5))
                    # REQ-PET-006: Reset companion command flag at start of ranger's turn
                    if getattr(new_turn_ent, "companion_commanded_this_turn", False):
                        new_turn_ent.companion_commanded_this_turn = False
                    # Also reset the companion's command flag if the new turn entity IS the companion
                    for ent_uuid in spatial_service._entities.get(vault_path, {}):
                        ent = spatial_service._entities[vault_path][ent_uuid]
                        if isinstance(ent, Creature) and getattr(ent, "companion_of_uuid", None) == new_turn_ent.entity_uuid:
                            ent.companion_commanded_this_turn = False
                new_turn_ent.spell_slots_expended_this_turn = 0

                sot_event = GameEvent(
                    event_type="StartOfTurn",
                    source_uuid=new_turn_ent.entity_uuid,
                    vault_path=vault_path,
                )
                await EventBus.adispatch(sot_event)
                if "results" in sot_event.payload and sot_event.payload["results"]:
                    log_msg.extend(sot_event.payload["results"])

                # Ammann tactical guidance for the creature whose turn is starting
                new_combatant = combatants[yaml_data["current_turn_index"]]
                guidance = _generate_tactical_guidance(new_combatant, vault_path)
                if guidance:
                    log_msg.append(guidance)
    except Exception as e:
        return str(e)

    if advance_global_clock:
        from world_tools import advance_time
        await advance_time.ainvoke({"seconds": 6, "trigger_events": False}, config)

    if new_init is not None:
        event = GameEvent(
            event_type="AdvanceTime", source_uuid=uuid.uuid4(), payload={"seconds_advanced": 6, "target_initiative": new_init}
        )
        await EventBus.adispatch(event)

    return " | ".join(log_msg) if log_msg else "Combat updated."



@tool
async def end_combat(*, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Concludes combat, saves PC final states to permanent files, and deletes ACTIVE_COMBAT.md."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")

    try:
        async with read_markdown_entity(file_path) as (yaml_data, _):
            combatants = yaml_data.get("combatants", [])
    except Exception as e:
        return str(e)

    # Lazy import to avoid circular dependency
    from entity_tools import update_character_status
    for c in combatants:
        if c.get("is_pc"):
            conds = ", ".join(c["conditions"]) if c["conditions"] else "None"
            await update_character_status.ainvoke(
                {
                    "character_name": c["name"],
                    "hp": str(c["hp"]),
                    "resources": "Update Manually",
                    "conditions": conds,
                    "fatigue": "None",
                },
                config,
            )

        # Flush in-memory combat states
        ent = await _get_entity_by_name(c["name"], vault_path)
        if ent and isinstance(ent, Creature):
            ent.reaction_used = False
            ent.legendary_actions_current = getattr(ent, "legendary_actions_max", 0)
            ent.movement_remaining = ent.speed
            ent.spell_slots_expended_this_turn = 0

    if vault_path in spatial_service.active_combatants:
        del spatial_service.active_combatants[vault_path]
    os.remove(file_path)
    return "Combat ended successfully. ACTIVE_COMBAT.md removed."



__all__ = [
    "start_combat",
    "update_combat_state",
    "end_combat",
]


# ---------------------------------------------------------------------------
# Ammann Tactical Helpers
# ---------------------------------------------------------------------------


async def _enrich_combatant_with_ammann(combatant: dict, vault_path: str) -> dict:
    """
    Load an enemy combatant's entity and enrich their dict with Ammann tactical fields.
    These are read-only per-encounter annotations stored on ACTIVE_COMBAT.md.
    """
    entity = await _get_entity_by_name(combatant["name"], vault_path)
    if not entity or not hasattr(entity, "creature_role"):
        return combatant

    # Phase-change tracking: reset for a fresh encounter
    phase_trigger = getattr(entity, "phase_change_trigger_hp_pct", 0)
    combatant["ammann"] = {
        "creature_role": getattr(entity, "creature_role", []) or [],
        "engagement_style": getattr(entity, "engagement_style", "") or "default",
        "combat_flow_priority": getattr(entity, "combat_flow_priority", "") or "",
        "recharge_priority": getattr(entity, "recharge_priority", False),
        "action_synergies": getattr(entity, "action_synergies", []) or [],
        "targeting_heuristic": getattr(entity, "targeting_heuristic", "") or "standard",
        "retreat_threshold_hp_pct": getattr(entity, "retreat_threshold_hp_pct", 0),
        "evasion_vector": getattr(entity, "evasion_vector", "") or "none",
        "fanaticism_override": getattr(entity, "fanaticism_override", False),
        "phase_change_trigger_hp_pct": phase_trigger,
        "phase_change_description": getattr(entity, "phase_change_description", "") or "",
        "unexpected_tactic": getattr(entity, "unexpected_tactic", "") or "",
        "metaphorical_damage": getattr(entity, "metaphorical_damage", "") or "",
        "expected_environment": getattr(entity, "expected_environment", []) or [],
        # Per-encounter state (reset each combat)
        "phase_used": False,
        "retreat_attempted": False,
        "surprised": combatant.get("surprised", False),
    }
    return combatant


def _generate_tactical_guidance(combatant: dict, vault_path: str) -> str:
    """
    Generate Ammann-based tactical guidance for a creature's turn.
    Returns a human-readable advisory string (or "" if nothing notable).
    """
    ammann = combatant.get("ammann")
    if not ammann:
        return ""

    hp_pct = int((combatant["hp"] / max(combatant["max_hp"], 1)) * 100)
    role = ammann.get("creature_role", [])
    engagement = ammann.get("engagement_style", "")
    priority = ammann.get("combat_flow_priority", "")
    recharge = ammann.get("recharge_priority", False)
    synergies = ammann.get("action_synergies", [])
    targeting = ammann.get("targeting_heuristic", "")
    retreat_pct = ammann.get("retreat_threshold_hp_pct", 0)
    evasion = ammann.get("evasion_vector", "")
    fanaticism = ammann.get("fanaticism_override", False)
    phase_trigger = ammann.get("phase_change_trigger_hp_pct", 0)
    phase_desc = ammann.get("phase_change_description", "")
    unexpected = ammann.get("unexpected_tactic", "")
    metaphor = ammann.get("metaphorical_damage", "")
    phase_used = ammann.get("phase_used", False)
    retreat_attempted = ammann.get("retreat_attempted", False)

    msgs = []

    # Phase change check
    if phase_trigger > 0 and hp_pct <= phase_trigger and not phase_used:
        msgs.append(f"[AMMANN] {combatant['name']} has reached {phase_trigger}% HP — activating phase change: {phase_desc}")

    # Recharge ability alert
    if recharge:
        msgs.append(f"[AMMANN] {combatant['name']} (Brute/Controller) — prioritize recharge abilities immediately.")

    # Action synergy reminder
    if synergies:
        msgs.append(f"[AMMANN] {combatant['name']} synergies: {' | '.join(synergies)}")

    # Targeting tier
    if targeting == "master_tactician":
        msgs.append(f"[AMMANN] {combatant['name']} (Master Tactician) — targets healers/spellcasters first. Coordinate flanks before committing AoE.")
    elif targeting == "strategic":
        msgs.append(f"[AMMANN] {combatant['name']} (Strategic) — bypasses tanks to strike fragile backline.")
    elif targeting == "reckless":
        msgs.append(f"[AMMANN] {combatant['name']} (Reckless) — attacks nearest large target indiscriminately.")

    # Retreat check (non-fanatics only)
    if retreat_pct > 0 and not fanaticism and not retreat_attempted and hp_pct <= retreat_pct:
        msgs.append(f"[AMMANN] {combatant['name']} has reached retreat threshold ({hp_pct}% ≤ {retreat_pct}%).")
        if evasion and evasion != "none":
            msgs.append(f"[AMMANN] Evasion: use {evasion} to disengage.")
        else:
            msgs.append(f"[AMMANN] No evasion vector — considers Dodge to Disengage.")

    # Engagement style reminders
    if engagement == "ambush":
        msgs.append(f"[AMMANN] {combatant['name']} (Ambush/Lurker) — seeks stealth positioning for burst on next round.")
    elif engagement == "seek_elevation":
        msgs.append(f"[AMMANN] {combatant['name']} (Artillerist) — seeks high ground or cover before attacking.")

    return " ".join(msgs)
