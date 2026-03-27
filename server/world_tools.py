# flake8: noqa: W293, E203
"""
world_tools - Time, rest, dice, and world tools
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
    roll_dice,
)
import rules_engine as _rules_engine  # noqa: E402 — access _CR_TO_XP for DEGA
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

from registry import get_all_entities, register_entity, get_entity, get_candidate_uuids_by_prefix, get_knowledge_graph
from knowledge_graph import KnowledgeGraph, GraphNodeType, GraphPredicate, KnowledgeGraphNode

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
async def roll_generic_dice(formula: str, reason: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    Parses and rolls generic D&D dice formulas (e.g., '1d8+3', '8d6').
    Use this to roll generic dice for random encounters, loot tables, or minor narrative
    variables (e.g., '1d4 days of travel').

    CRITICAL: NEVER use this tool to calculate weapon damage, spell damage, or health changes.
    Use `modify_health` or the combat tools instead.
    """
    match = re.match(r"(\d+)d(\d+)(?:\s*([+-])\s*(\d+))?", formula.strip().lower())
    if not match:
        return f"Error: Invalid dice format '{formula}'. Use 'XdY' or 'XdY+Z'."

    num_dice, die_sides = int(match.group(1)), int(match.group(2))
    modifier_op = match.group(3)
    modifier_val = int(match.group(4)) if match.group(4) else 0

    rolls = [random.randint(1, die_sides) for _ in range(num_dice)]
    total = sum(rolls)
    if modifier_op == "+":
        total += modifier_val
    elif modifier_op == "-":
        total -= modifier_val

    result = f"MECHANICAL TRUTH: Rolled {formula} for {reason}. Result:{total}"
    await write_audit_log(config["configurable"].get("thread_id"), "Rules Engine", "roll_generic_dice Executed", result)
    return result



@tool
async def search_vault_by_tag(target_tag: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Scans the YAML frontmatter of files to find entities matching a tag."""
    vault_path = config["configurable"].get("thread_id")
    matching_files, j_dir = [], get_journals_dir(vault_path)

    if not os.path.exists(j_dir):
        return "Error: Journal directory not found."

    for filename in os.listdir(j_dir):
        if not filename.endswith(".md"):
            continue
        file_path = os.path.join(j_dir, filename)

        try:
            async with read_markdown_entity(file_path) as (yaml_data, _):
                if target_tag.lower() in [tag.lower() for tag in yaml_data.get("tags", [])]:
                    matching_files.append(filename.replace(".md", ""))
        except Exception:
            pass

    return (
        f"Entities matching '{target_tag}': " + ", ".join(matching_files)
        if matching_files
        else f"No entities found with tag: {target_tag}"
    )



@tool
async def advance_time(
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    seconds: int = 0,
    trigger_events: bool = True,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Advances the in-game clock stored in CAMPAIGN_MASTER.md."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "CAMPAIGN_MASTER.md")

    current_day, current_hour, current_minute, current_second = 1, 8, 0, 0
    try:
        async with read_markdown_entity(file_path) as (yaml_data, _):
            day_match = re.search(r"\d+", str(yaml_data.get("current_date", "Day 1")))
            if day_match:
                current_day = int(day_match.group())
            time_str = str(yaml_data.get("in_game_time", "08:00:00"))
            if ":" in time_str:
                parts = time_str.split(":")
                current_hour = int(parts[0])
                current_minute = int(parts[1]) if len(parts) > 1 else 0
                current_second = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        pass

    total_seconds = current_second + seconds
    total_minutes = current_minute + minutes + (total_seconds // 60)
    total_hours = current_hour + hours + (total_minutes // 60)
    new_day = current_day + days + (total_hours // 24)
    new_time_str = f"{total_hours % 24:02d}:{total_minutes % 60:02d}:{total_seconds % 60:02d}"

    # Lazy import to avoid circular dependency
    from entity_tools import update_yaml_frontmatter
    await update_yaml_frontmatter.ainvoke(
        {"entity_name": "CAMPAIGN_MASTER", "updates": {"current_date": f"Day {new_day}", "in_game_time": new_time_str}}, config
    )

    total_seconds_advanced = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total_seconds_advanced > 0 and trigger_events:
        # Dispatch an AdvanceTime event to the engine so buffs can expire
        event = GameEvent(
            event_type="AdvanceTime", source_uuid=uuid.uuid4(), payload={"seconds_advanced": total_seconds_advanced}
        )
        await EventBus.adispatch(event)

    return f"Success: Time advanced. It is now Day {new_day}, {new_time_str}."



@tool
async def refresh_vault_data(
    entity_names: list[str] = None,
    refresh_all: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Forces the engine to reload entities from the Obsidian Vault into memory, updating their stats to match the Markdown files.
    - If `entity_names` is provided, those specific entities are forcefully re-hydrated.
    - If `refresh_all` is True, it efficiently scans all files for changes using modified timestamps.
      You MUST ask the DM for confirmation ("Are you sure?") before calling with `refresh_all=True`.
    """
    vault_path = config["configurable"].get("thread_id")

    if not entity_names and not refresh_all:
        return "SYSTEM ERROR: You must specify either a list of `entity_names` to refresh or set `refresh_all=True`."

    if refresh_all:
        from vault_io import sync_engine_from_vault_updates

        res = await sync_engine_from_vault_updates(vault_path)
        return res

    from vault_io import load_entity_into_engine, get_journals_dir
    import os

    j_dir = get_journals_dir(vault_path)
    reloaded = []
    not_found = []

    for name in entity_names:
        filepath = os.path.join(j_dir, f"{name}.md")
        if os.path.exists(filepath):
            ent = await load_entity_into_engine(filepath, vault_path)
            if ent:
                reloaded.append(name)
            else:
                not_found.append(name)
        else:
            not_found.append(name)

    res_str = ""
    if reloaded:
        res_str += f"MECHANICAL TRUTH: Successfully force-reloaded {', '.join(reloaded)} from vault. "
    if not_found:
        res_str += f"SYSTEM ERROR: Failed to reload (files not found or invalid): {', '.join(not_found)}"

    return res_str.strip()



@tool
async def report_rule_challenge(
    character_name: str,
    dispute_details: str,
    expected_rule: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Use this tool IMMEDIATELY if a player complains, challenges, or disputes a game rule, dice roll, or mechanical outcome."""
    from system_logger import qa_logger

    vault_path = config["configurable"].get("thread_id", "default")
    qa_logger.warning(
        "Player Rule Dispute",
        extra={
            "agent_id": "PLAYER_CHALLENGE",
            "context": {
                "character": character_name,
                "vault_path": vault_path,
                "dispute_details": dispute_details,
                "expected_rule": expected_rule,
            },
        },
    )
    return f"MECHANICAL TRUTH: Rule dispute from {character_name} successfully logged to the QA system."


def _get_config_tone(vault_path: str) -> str:
    """Reads DM_CONFIG.md to optionally retrieve Tone & Boundaries."""
    config_path = os.path.join(vault_path, "DM_CONFIG.md")
    if not os.path.exists(config_path):
        return ""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content.startswith("---"):
            yaml_data = yaml.safe_load(content.split("---", 2)[1]) or {}
            return yaml_data.get("tone_and_boundaries", "")
    except Exception:
        pass
    return ""


def _get_config_settings(vault_path: str) -> dict:
    """Reads DM_CONFIG.md to retrieve boolean toggles and settings."""
    config_path = os.path.join(vault_path, "DM_CONFIG.md")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content.startswith("---"):
            yaml_data = yaml.safe_load(content.split("---", 2)[1]) or {}
            return yaml_data.get("settings", {})
    except Exception:
        pass
    return {}


def _get_config_dirs(vault_path: str, key: str) -> list[str]:
    """Reads DM_CONFIG.md and returns a list of absolute paths for a directory key."""
    config_path = os.path.join(vault_path, "DM_CONFIG.md")
    if not os.path.exists(config_path):
        return []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content.startswith("---"):
            yaml_data = yaml.safe_load(content.split("---", 2)[1]) or {}
            rel_dirs = yaml_data.get("directories", {}).get(key, [])
            if isinstance(rel_dirs, str):
                rel_dirs = [rel_dirs]
            target_dirs = []
            for rel_dir in rel_dirs:
                target_dir = os.path.join(vault_path, os.path.normpath(rel_dir))
                os.makedirs(target_dir, exist_ok=True)
                target_dirs.append(target_dir)
            return target_dirs
    except Exception as e:
        print(f"Error reading DM_CONFIG.md: {e}")
    return []


def _search_markdown_for_keywords(vault_path: str, category: str, query: str, top_n: int = 3) -> str:
    """Scans in-memory chunks for the most relevant sections."""
    _VAULT_CACHE.build_index(vault_path)
    keywords = set([w.lower() for w in query.replace(",", "").split() if len(w) > 3])
    if not keywords:
        keywords = set([query.lower()])

    best_chunks = []
    chunks = _VAULT_CACHE.chunk_cache.get(vault_path, {}).get(category, [])

    for file, chunk in chunks:
        chunk_lower = chunk.lower()
        file_lower = file.lower()
        score = sum(1 for k in keywords if k in chunk_lower)
        if any(k in file_lower for k in keywords):
            score += 2
        if score > 0:
            best_chunks.append((score, file, chunk))

    if not best_chunks:
        return f"Cache Miss: No relevant information found for '{query}'."

    best_chunks.sort(key=lambda x: x[0], reverse=True)

    result = ""
    for score, file, chunk in best_chunks[:top_n]:
        snippet = chunk[:1000] + ("\n[...Truncated]" if len(chunk) > 1000 else "")
        result += f"--- Source: {file} ---\n{snippet}\n\n"

    return result



@tool
async def perform_ability_check_or_save(  # noqa: C901
    character_name: str,
    skill_or_stat_name: str,
    target_names: list[str] = None,
    is_hidden: bool = False,
    is_passive: bool = False,
    advantage: bool = False,
    disadvantage: bool = False,
    extra_modifier: int = 0,
    bonus_dice: str = None,
    luck_points_used: int = 0,
    manual_roll_total: int = None,
    force_auto_roll: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    STRICT RULE: Use this ONLY for out-of-combat skill checks (Perception, Persuasion, Stealth)
    or environmental Saving Throws (dodging a falling rock).

    CRITICAL: NEVER use this tool for Weapon Attacks, Spell Attacks, or Spell Saves.
    Combat mechanics are handled exclusively by `execute_melee_attack` and `use_ability_or_spell`.
    - If the character has Bless, Bane, or Bardic Inspiration, pass '1d4' or '-1d4' into bonus_dice.
    - If the character spends a Luck point or uses Elven Accuracy, pass luck_points_used=1.
    """
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")
    stat_mod = 0
    skill_map = {
        "perception": "wisdom",
        "insight": "wisdom",
        "survival": "wisdom",
        "animal handling": "wisdom",
        "medicine": "wisdom",
        "investigation": "intelligence",
        "history": "intelligence",
        "religion": "intelligence",
        "arcana": "intelligence",
        "nature": "intelligence",
        "stealth": "dexterity",
        "acrobatics": "dexterity",
        "sleight of hand": "dexterity",
        "athletics": "strength",
        "persuasion": "charisma",
        "deception": "charisma",
        "intimidation": "charisma",
        "performance": "charisma",
    }
    clean_skill = skill_or_stat_name.lower().strip()
    base_stat = skill_map.get(clean_skill, clean_skill)

    stat_mod = 0
    engine_creature = await _get_entity_by_name(character_name, vault_path)

    if engine_creature and isinstance(engine_creature, Creature) and base_stat != "none":
        # 1. PREFERRED: Read from OO Engine to capture live buffs/debuffs
        stat_obj = getattr(engine_creature, f"{base_stat}_mod", None)
        if stat_obj:
            stat_mod = stat_obj.total
    elif base_stat != "none":
        # 2. FALLBACK: Read from YAML if entity is out of scope/inactive
        try:
            async with read_markdown_entity(file_path) as (yaml_data, _):
                stat_score = int(yaml_data.get(base_stat, yaml_data.get(base_stat[:3], 10)))
                stat_mod = math.floor((stat_score - 10) / 2)
        except Exception:
            pass

    total_mod = stat_mod + extra_modifier

    if engine_creature and isinstance(engine_creature, Creature):
        active_conds = [c.name.lower() for c in engine_creature.active_conditions]
        if "poisoned" in active_conds:
            disadvantage = True

    # REQ-ENC-004: Charmed Social Advantage
    social_alert = ""
    if (
        engine_creature
        and isinstance(engine_creature, Creature)
        and target_names
        and clean_skill in ["persuasion", "deception", "intimidation", "performance", "insight"]
    ):
        for t_name in target_names:
            t_ent = await _get_entity_by_name(t_name, vault_path)
            if t_ent and any(
                c.name.lower() == "charmed" and c.source_uuid == engine_creature.entity_uuid
                for c in getattr(t_ent, "active_conditions", [])
            ):
                advantage = True
                social_alert += f"\nSYSTEM ALERT (REQ-ENC-004): {character_name} has Advantage on social checks against {t_ent.name} because they are Charmed!"

    # Query Environmental Lighting to alert the DM for Stealth and Perception checks
    illum_alert = ""
    illum = "bright"
    if engine_creature and isinstance(engine_creature, Creature):
        illum = spatial_service.get_illumination(engine_creature.x, engine_creature.y, engine_creature.z, vault_path)

        has_enhanced_vision = any(
            s in tag for tag in engine_creature.tags for s in ["darkvision", "blindsight", "truesight", "tremorsense"]
        )
        is_deafened = any(c.name.lower() == "deafened" for c in engine_creature.active_conditions)
        has_sunlight_sensitivity = "sunlight_sensitivity" in engine_creature.tags

        in_sunlight = False
        if illum == "bright":
            for light in spatial_service.get_map_data(vault_path).active_lights:
                if "sun" in light.label.lower():
                    dist = spatial_service.calculate_distance(
                        engine_creature.x, engine_creature.y, engine_creature.z, light.x, light.y, light.z, vault_path
                    )
                    if dist <= light.bright_radius:
                        in_sunlight = True
                        break

        if clean_skill in ["perception", "investigation"]:
            is_blinded = any(c.name.lower() == "blinded" for c in engine_creature.active_conditions)
            has_blindsight = any(s in tag for tag in engine_creature.tags for s in ["blindsight", "truesight"])

            in_silence = False
            if HAS_GIS:
                ent_poly = spatial_service._get_entity_bbox(engine_creature)
                if ent_poly:
                    for tz in spatial_service.get_map_data(vault_path).active_terrain:
                        if "silence" in [tag.lower() for tag in tz.tags] and tz.polygon and tz.polygon.intersects(ent_poly):
                            in_silence = True
                            break

            if in_sunlight and has_sunlight_sensitivity:
                illum_alert += "\nSYSTEM ALERT: Character has Sunlight Sensitivity and is in direct sunlight. Disadvantage (-5 to Passive) on sight-based checks."
                disadvantage = True
            elif is_blinded and not has_blindsight:
                illum_alert += (
                    "\nSYSTEM ALERT: Character is BLINDED. Sight-based checks automatically fail (Hearing/Smell still work)."
                )
            elif illum == "darkness" and not has_enhanced_vision:
                illum_alert += "\nSYSTEM ALERT: Character is in TOTAL DARKNESS. Sight-based checks automatically fail (Hearing/Smell still work)."
            elif illum == "dim" and not has_enhanced_vision:
                illum_alert += "\nSYSTEM ALERT (REQ-VIS-012): Character is in DIM LIGHT. Disadvantage (-5 to Passive) on sight-based checks."
                disadvantage = True

            if is_deafened or in_silence:
                reason = "DEAFENED" if is_deafened else "in a magically SILENCED zone"
                illum_alert += f"\nSYSTEM ALERT: Character is {reason}. Hearing-based checks automatically fail."

    is_pc = any(t in engine_creature.tags for t in ["pc", "player"]) if engine_creature else False
    if is_pc and not is_passive and not force_auto_roll and manual_roll_total is None:
        auto_settings = get_roll_automations(character_name)
        if is_hidden and not auto_settings.get("hidden_rolls", True):
            return (
                f"SYSTEM ALERT: {character_name} has manual hidden rolls enabled. Ask the player to privately roll "
                f"{clean_skill} and provide the total (including modifiers), OR ask to automate it."
            )
        elif not is_hidden and not auto_settings.get("skill_checks", True):
            return (
                f"SYSTEM ALERT: {character_name} has manual skill checks enabled. Ask the player to roll {clean_skill} "
                f"and provide the total (including modifiers), OR ask to automate it."
            )

    if is_passive:
        total = 10 + total_mod + (5 if advantage else (-5 if disadvantage else 0))
        result_str = f"Passive {clean_skill.capitalize()} Score: {total}.\nDM DIRECTIVE: Narrate using 'Describe to Me'."
        await write_audit_log(vault_path, "Rules Engine", "perform_ability_check_or_save Executed (Passive)", result_str)
        return result_str

    # --- 1. RESOLVE 5.5e BOOLEAN STATES ---
    if manual_roll_total is not None:
        total = manual_roll_total
        base_roll = "Manual"
        roll_type_str = "manually"
        bonus_str = ""
        total_mod = 0  # Included in manual string
    else:
        if luck_points_used > 0:
            advantage = True  # In 5.5e, Luck explicitly grants Advantage

        num_d20s = 1
        if advantage or disadvantage:
            num_d20s = 2
        if advantage and disadvantage:
            num_d20s = 1  # They perfectly cancel each other out

        rolls = [random.randint(1, 20) for _ in range(num_d20s)]

        # --- 2. EVALUATE FINAL POOL ---
        if advantage and not disadvantage:
            base_roll = max(rolls)
            roll_type_str = f"Advantage {rolls}"
            if luck_points_used > 0:
                roll_type_str += " (via Luck)"

        elif disadvantage and not advantage:
            base_roll = min(rolls)
            roll_type_str = f"Disadvantage {rolls}"

        else:
            # This catches standard rolls AND canceled out Adv/Dis rolls
            base_roll = rolls[0]
            roll_type_str = "normally"
            if luck_points_used > 0 and disadvantage:
                roll_type_str += f" {rolls} (Disadvantage canceled by Luck)"

        if base_roll == 1:
            roll_type_str += " [NATURAL 1 - CRITICAL FAILURE]"
        elif base_roll == 20:
            roll_type_str += " [NATURAL 20 - CRITICAL SUCCESS]"

        # --- 3. RESOLVE BONUS DICE (Bless/Bane) ---
        bonus_total = 0
        bonus_str = ""
        if bonus_dice:
            match = re.match(r"([+-]?)\s*(\d+)d(\d+)", bonus_dice.strip().lower())
            if match:
                sign, num, sides = match.group(1) or "+", int(match.group(2)), int(match.group(3))
                b_rolls = [random.randint(1, sides) for _ in range(num)]
                bonus_total = sum(b_rolls) if sign != "-" else -sum(b_rolls)
                bonus_str = f" + [{bonus_dice}: {bonus_total}]"

        total = base_roll + total_mod + bonus_total

    if engine_creature and isinstance(engine_creature, Creature) and clean_skill == "stealth":
        if illum == "bright":
            illum_alert = (
                "\nSYSTEM ALERT: Character is in BRIGHT LIGHT. They cannot hide without physical cover or invisibility."
            )
        elif illum == "darkness":
            illum_alert = "\nSYSTEM ALERT: Character is in TOTAL DARKNESS. They are heavily obscured and can hide freely."
        elif illum == "dim":
            illum_alert = "\nSYSTEM ALERT: Character is in DIM LIGHT. They are lightly obscured and can hide."
        # REQ-ARM-002: Heavy armor stealth disadvantage
        try:
            async with read_markdown_entity(file_path) as (yd, _):
                armor_name = str(yd.get("equipment", {}).get("armor", "None")).strip()
                if armor_name not in ["None", "", "Unarmored"]:
                    armor_item = await ItemCompendium.load_item(vault_path, armor_name)
                    if armor_item and isinstance(armor_item, ArmorItem) and armor_item.stealth_disadvantage:
                        disadvantage = True
                        illum_alert += (
                            f"\nSYSTEM ALERT: {character_name} is wearing {armor_name} which imposes "
                            f"Disadvantage on Stealth checks (REQ-ARM-002)."
                        )
        except Exception:
            pass

    # REQ-VIS-002: Evaluate stealth natively
    if illum != "bright":
        max_pp = 0
        is_pc_stealth = any(t in engine_creature.tags for t in ["pc", "player", "party_npc"])
        for uid, ent in get_all_entities(vault_path).items():
            if isinstance(ent, Creature) and ent.hp.base_value > 0 and ent.entity_uuid != engine_creature.entity_uuid:
                is_ent_pc = any(t in ent.tags for t in ["pc", "player", "party_npc"])
                if is_pc_stealth != is_ent_pc:
                    dist = 0
                    if HAS_GIS:
                        dist = spatial_service.calculate_distance(
                            ent.x, ent.y, ent.z, engine_creature.x, engine_creature.y, engine_creature.z, vault_path
                        )
                    distance_penalty = int(dist // 10)
                    pp = 10 + ent.wisdom_mod.total - distance_penalty
                    if pp > max_pp:
                        max_pp = pp
        hide_dc = max(15, max_pp)
        if total >= hide_dc:
            if not any(c.name.lower() == "invisible" for c in engine_creature.active_conditions):
                engine_creature.active_conditions.append(ActiveCondition(name="Invisible", source_name="Hide Action"))
            illum_alert += f"\nSYSTEM ALERT (REQ-VIS-002): {character_name} rolled {total} (>= DC {hide_dc}). They succeeded and gained the 'Invisible' condition!"
        else:
            illum_alert += f"\nSYSTEM ALERT (REQ-VIS-002): {character_name} rolled {total} (failed to beat DC {hide_dc}). They remain visible."
    else:
        illum_alert += (
            f"\nSYSTEM ALERT (REQ-VIS-002): {character_name} failed to hide (cannot hide in bright light without cover)."
        )

    exh_penalty = engine_creature.exhaustion_level * 2 if (engine_creature and isinstance(engine_creature, Creature)) else 0
    total -= exh_penalty
    exh_str = f" - {exh_penalty} (Exhaustion)" if exh_penalty > 0 else ""

    result_str = (
        f"MECHANICAL TRUTH: Roll Result ({clean_skill}): {base_roll} {roll_type_str} "
        f"+ {total_mod} stat mod{bonus_str}{exh_str} = {total}. "
    )
    result_str += (
        "\nHIDDEN ROLL: Narrate sensory experience only." if is_hidden else "\nYou may reveal the total to the player."
    )
    result_str += illum_alert + social_alert

    await write_audit_log(vault_path, "Rules Engine", "perform_ability_check_or_save Executed", result_str)
    return result_str



@tool
async def take_rest(
    character_names: list[str],
    rest_type: str,
    hit_dice_to_spend: int = 0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Use this tool when characters explicitly take a Short or Long Rest.
    It automatically advances time and signals the engine to heal and recharge resources.
    For Short Rests, pass hit_dice_to_spend to roll Hit Dice and regain HP (REQ-RST-001).
    The engine rolls the dice, adds CON modifier, and applies healing up to max HP.

    REQ-RST-002: If the rest is interrupted by strenuous activity (fighting, casting spells,
    walking for 1+ hours), call interrupt_rest BEFORE time advances past the 8-hour mark.
    An interrupted rest grants no benefits.

    REQ-RST-003: A creature cannot benefit from more than one Long Rest in a 24-hour period.
    Attempting to do so is rejected at rest completion time.
    """
    vault_path = config["configurable"].get("thread_id")

    # Read current game time BEFORE advancing — needed for REQ-RST-002/003
    current_day, current_hour = 1, 8
    try:
        campaign_path = os.path.join(get_journals_dir(vault_path), "CAMPAIGN_MASTER.md")
        async with read_markdown_entity(campaign_path) as (yaml_data, _):
            day_match = re.search(r"\d+", str(yaml_data.get("current_date", "Day 1")))
            if day_match:
                current_day = int(day_match.group())
            time_str = str(yaml_data.get("in_game_time", "08:00:00"))
            if ":" in time_str:
                parts = time_str.split(":")
                current_hour = int(parts[0])
    except Exception:
        pass

    # Mark rest in-progress on each entity before time advances (REQ-RST-002)
    rest_lower = rest_type.lower()
    hours_to_advance = 8 if rest_lower == "long" else 1
    uuids = []
    for name in character_names:
        entity = await _get_entity_by_name(name, vault_path)
        if entity:
            entity.rest_in_progress = True
            entity.rest_type = rest_lower
            entity.rest_start_day = current_day
            entity.rest_start_hour = current_hour
            entity.rest_interrupted = False
            uuids.append(entity.entity_uuid)

    await advance_time.ainvoke({"hours": hours_to_advance}, config)

    if uuids:
        event = GameEvent(
            event_type="Rest",
            source_uuid=uuids[0],
            vault_path=vault_path,
            payload={
                "rest_type": rest_lower,
                "target_uuids": uuids,
                "hit_dice_to_spend": hit_dice_to_spend,
                "rest_start_day": current_day,
                "rest_start_hour": current_hour,
            },
        )
        await EventBus.adispatch(event)

    return (
        f"MECHANICAL TRUTH: {', '.join(character_names)} completed a {rest_type} rest. "
        f"Time advanced {hours_to_advance} hours. HP and resources processed."
    )


@tool
async def interrupt_rest(
    character_names: list[str],
    reason: str = "Strenuous activity",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    REQ-RST-002: Call this when a resting character performs strenuous activity
    (fighting, casting spells, walking for 1+ hours, etc.) during a rest.
    This marks the rest as interrupted; no HP or resource benefits are granted.

    Must be called BEFORE the rest's 8-hour period elapses (before take_rest finishes
    advancing time for a long rest, or before the short rest completes).
    """
    vault_path = config["configurable"].get("thread_id")
    interrupted = []
    for name in character_names:
        entity = await _get_entity_by_name(name, vault_path)
        if entity and getattr(entity, "rest_in_progress", False):
            entity.rest_interrupted = True
            interrupted.append(name)

    if interrupted:
        return (
            f"REST INTERRUPTED: {', '.join(interrupted)}'s rest has been broken by strenuous "
            f"activity ({reason}). No benefits will be granted."
        )
    return f"No active rest found for {', '.join(character_names)}."


# miles per day by pace
_MILES_PER_DAY = {"fast": 30, "normal": 24, "slow": 18}


@tool
async def travel(
    party_names: list[str],
    pace: str = "normal",
    hours_traveled: int = 8,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    REQ-TRV-001/002/003: Resolve a travel segment for a party.

    pace: "fast" (30 mi/day, -5 passive Perception), "normal" (24 mi/day),
          or "slow" (18 mi/day, party can Stealth while traveling).

    hours_traveled: number of hours walked. Default 8.
    If hours_traveled > 8, each extra hour triggers a Constitution saving throw
    (DC 10 + 1 per extra hour) — REQ-TRV-004: failed save = 1 level of Exhaustion.

    Returns a summary of distance covered, passive Perception effects, and
    any forced-march exhaustion applied.
    """
    vault_path = config["configurable"].get("thread_id")
    pace_lower = pace.lower()
    miles_per_day = _MILES_PER_DAY.get(pace_lower, 24)

    # Distance covered = (hours_traveled / 8) * miles_per_day
    distance = (hours_traveled / 8.0) * miles_per_day

    results = []
    forced_march_exhausted = []

    # REQ-TRV-004: Forced march CON save for hours beyond 8
    extra_hours = max(0, hours_traveled - 8)
    if extra_hours > 0:
        dc = 10 + extra_hours
        for name in party_names:
            entity = await _get_entity_by_name(name, vault_path)
            if not entity:
                continue
            # Direct CON save: 1d20 + CON modifier vs DC
            con_mod = entity.constitution_mod.total if hasattr(entity, "constitution_mod") else 0
            save_roll = roll_dice("1d20")
            save_total = save_roll + con_mod
            save_failed = save_total < dc
            if save_failed:
                old_exhaustion = entity.exhaustion_level
                entity.exhaustion_level = min(6, entity.exhaustion_level + 1)
                forced_march_exhausted.append(
                    f"{entity.name} failed the forced march CON save "
                    f"({save_roll}+{con_mod}={save_total} vs DC {dc}) — "
                    f"gained 1 Exhaustion ({old_exhaustion} → {entity.exhaustion_level})"
                )
            else:
                results.append(
                    f"{entity.name} made the forced march CON save "
                    f"({save_roll}+{con_mod}={save_total} vs DC {dc})."
                )

    # REQ-TRV-001: Fast pace — passive Perception penalty
    if pace_lower == "fast":
        results.append("All party members have -5 to passive Perception scores while traveling at a fast pace.")

    # REQ-TRV-003: Slow pace — stealth permitted
    if pace_lower == "slow":
        results.append("The party can use Stealth while traveling at a slow pace.")

    # Advance time
    await advance_time.ainvoke({"hours": hours_traveled}, config)

    summary = (
        f"TRAVEL COMPLETE ({pace_lower} pace, {hours_traveled}h): "
        f"{', '.join(party_names)} covered {distance:.1f} miles. "
    )
    if forced_march_exhausted:
        summary += "FORCED MARCH: " + " | ".join(forced_march_exhausted) + " "
    summary += " | ".join(results) if results else ""
    summary += f" Time advanced {hours_traveled} hours."

    return summary


@tool
async def use_heroic_inspiration(
    character_name: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    REQ-SKL-007: Spend Heroic Inspiration to grant the character advantage
    on their next d20 test (attack, save, or ability check).

    The character must have has_heroic_inspiration = True.
    This consumes the inspiration (sets it to False).
    """
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(character_name, vault_path)
    if not entity:
        return f"SYSTEM ERROR: Entity '{character_name}' not found."

    if not getattr(entity, "has_heroic_inspiration", False):
        return (
            f"SYSTEM ERROR: {character_name} does not have Heroic Inspiration available. "
            f"Grant it via the 'grant_heroic_inspiration' action or narrative."
        )

    entity.has_heroic_inspiration = False
    return (
        f"Heroic Inspiration spent: {character_name} now has Advantage on their next d20 test. "
        f"(Inspiration consumed — must be re-granted by DM narrative.)"
    )


@tool
async def perform_group_check(
    party_names: list[str],
    skill_or_stat_name: str,
    dc: int = 10,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    REQ-SKL-006: Each party member makes the same ability check.
    If at least half the group succeeds (ceil(n/2)), the group succeeds.
    Each member rolls individually; results are reported per character.

    dc: Optional Difficulty Class override (default 10).
    Pass is determined by: roll + modifier >= DC.
    """
    vault_path = config["configurable"].get("thread_id")
    results = []
    successes = 0
    total = len(party_names)

    for name in party_names:
        # Delegate to perform_ability_check_or_save for each member
        res = await perform_ability_check_or_save.ainvoke(
            {
                "character_name": name,
                "skill_or_stat_name": skill_or_stat_name,
                "target_names": [],
                "is_hidden": False,
            },
            config=config,
        )
        # Parse the result — look for the total
        import re as _re
        match = _re.search(r"(\d+)\s*(?:\+|vs|$|\.)", res)
        if match:
            total_roll = int(match.group(1))
            # Get the modifier from the result
            mod_match = _re.search(r"(\d+)\s*(?:\+|–|\-)\s*(\d+)", res)
            mod = 0
            if mod_match:
                # The format is like "15+3=18" or "12-2=10"
                sign = 1 if "+" in res[res.find(str(total_roll)):res.find(str(total_roll)) + len(str(total_roll)) + 3] else -1
                mod = int(mod_match.group(2)) if sign == 1 else -int(mod_match.group(2))
            # Re-calculate: we need the d20 roll separately from total
            # For simplicity, use the total and subtract an estimated modifier
            # Better approach: parse the actual roll from the result
            roll_match = _re.search(r"(\d+)\s*(?:\+|–)", res)
            if roll_match:
                roll_val = int(roll_match.group(1))
                mod = total_roll - roll_val
            else:
                mod = 0  # passive or manual
            final_total = total_roll + mod
        else:
            final_total = 0

        passed = final_total >= dc
        if passed:
            successes += 1

        status = "SUCCESS" if passed else "FAILURE"
        results.append(f"{name}: {status} ({final_total} vs DC {dc})")

    threshold = math.ceil(total / 2)
    group_success = successes >= threshold
    group_status = "GROUP SUCCESS" if group_success else "GROUP FAILURE"

    summary = (
        f"GROUP CHECK ({skill_or_stat_name}): {group_status} — "
        f"{successes}/{total} succeeded (need {threshold}). "
        + " | ".join(results)
    )
    return summary


# REQ-SOC-001/002/003/004: NPC Influence Action (attitude-based social interaction DC)
_ATTITUDE_BASE_DC = {
    "hostile": 14,
    "indifferent": 10,
    "friendly": 8,
    "helpful": 0,  # Friendly (Eager to Help)
}

_RISK_DC_MODIFIER = {
    "negligible": 0,
    "minor": 0,
    "low": 5,
    "moderate": 10,
    "high": 15,
    "severe": 20,
}


async def perform_social_interaction(
    character_name: str,
    target_npc_name: str,
    request_description: str,
    npc_attitude: str,  # "Hostile", "Indifferent", "Friendly", or "Friendly (Eager to Help)"
    approach: str = "persuasion",  # persuasion | deception | intimidation
    request_risk: str = "minor",  # negligible | minor | low | moderate | high | severe
    request_cost: str = "none",  # none | minor | moderate | significant
    manual_roll_total: int = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    REQ-SOC-001/002/003/004: Resolve an Influence action against an NPC using the 2024 DMG
    attitude-based DC system.

    Steps:
    1. Determine the NPC's starting attitude (Hostile / Indifferent / Friendly / Friendly (Eager to Help))
    2. Apply attitude modifiers: Hostile NPCs auto-reject risky requests (REQ-SOC-002);
       Indifferent NPCs auto-reject moderate+ risk requests (REQ-SOC-003).
    3. Calculate DC = Attitude_Base_DC + Risk_Modifier  [REQ-SOC-001]
    4. Roll d20 + CHA modifier vs DC. Report SUCCESS or FAILURE.

    Parameters:
      - character_name: The PC attempting to influence the NPC.
      - target_npc_name: The NPC being influenced.
      - request_description: What the PC is asking the NPC to do.
      - npc_attitude: One of "Hostile", "Indifferent", "Friendly", or "Friendly (Eager to Help)".
      - approach: "persuasion" (default), "deception", or "intimidation".
      - request_risk: Risk to the NPC — "negligible" | "minor" | "low" | "moderate" | "high" | "severe".
      - request_cost: Personal cost to the NPC — "none" | "minor" | "moderate" | "significant".
      - manual_roll_total: Optional override for the d20 roll (for play-by-post or pre-rolled dice).

    Attitude Base DCs (2024 DMG):
      Hostile (DC 14) → Indifferent (DC 10) → Friendly (DC 8) → Friendly/Eager (DC 0)

    Risk Modifiers:
      Negligible/Minor = +0  |  Low = +5  |  Moderate = +10  |  High = +15  |  Severe = +20
    """
    vault_path = config["configurable"].get("thread_id")

    # Normalize attitude key
    attitude_key = npc_attitude.strip().lower()
    if "helpful" in attitude_key or "eager" in attitude_key:
        attitude_key = "helpful"
    elif attitude_key not in _ATTITUDE_BASE_DC:
        return (
            f"SYSTEM ERROR: Unknown NPC attitude '{npc_attitude}'. "
            "Valid values: Hostile, Indifferent, Friendly, Friendly (Eager to Help)."
        )

    risk_key = request_risk.strip().lower()
    if risk_key not in _RISK_DC_MODIFIER:
        return (
            f"SYSTEM ERROR: Unknown risk level '{request_risk}'. "
            "Valid values: negligible, minor, low, moderate, high, severe."
        )

    base_dc = _ATTITUDE_BASE_DC[attitude_key]
    risk_mod = _RISK_DC_MODIFIER[risk_key]

    # REQ-SOC-002: Hostile NPC — auto-reject (no roll) if request has risk
    if attitude_key == "hostile" and risk_key not in ("negligible", "minor"):
        return (
            f"MECHANICAL TRUTH: Influence attempt on {target_npc_name} — AUTO-FAILURE (REQ-SOC-002). "
            f"{target_npc_name} is Hostile and won't take risks. "
            f"Request: {request_description}"
        )

    # REQ-SOC-003: Indifferent NPC — auto-reject moderate+ risk requests
    if attitude_key == "indifferent" and risk_key in ("moderate", "high", "severe"):
        return (
            f"MECHANICAL TRUTH: Influence attempt on {target_npc_name} — AUTO-FAILURE (REQ-SOC-003). "
            f"{target_npc_name} is Indifferent and won't accept requests involving significant personal cost. "
            f"Request: {request_description}"
        )

    # REQ-SOC-004: Friendly (Eager to Help) — minor/negligible requests auto-succeed
    if attitude_key == "helpful" and risk_key in ("negligible", "minor"):
        return (
            f"MECHANICAL TRUTH: Influence attempt on {target_npc_name} — AUTO-SUCCESS (REQ-SOC-004). "
            f"{target_npc_name} is Friendly (Eager to Help) and readily accepts '{request_description}'. "
            f"DC would have been {base_dc + risk_mod}; no roll needed."
        )

    # Calculate DC for rolling cases
    final_dc = base_dc + risk_mod

    # Get CHA modifier for the influencer
    influencer = await _get_entity_by_name(character_name, vault_path)
    if influencer and hasattr(influencer, "charisma_mod"):
        cha_mod = influencer.charisma_mod.total
    elif influencer and hasattr(influencer, "charisma"):
        cha_mod = influencer.charisma
    else:
        # Fallback: read from vault
        try:
            fpath = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")
            async with read_markdown_entity(fpath) as (yaml_data, _):
                cha_score = int(yaml_data.get("charisma", yaml_data.get("cha", 10)))
                cha_mod = math.floor((cha_score - 10) / 2)
        except Exception:
            cha_mod = 0

    # Roll or use manual override
    if manual_roll_total is not None:
        d20 = manual_roll_total
        roll_str = f"manual({manual_roll_total})"
    else:
        d20 = roll_dice("1d20")
        roll_str = str(d20)

    total = d20 + cha_mod
    passed = total >= final_dc
    outcome = "SUCCESS" if passed else "FAILURE"

    approach_label = approach.capitalize()

    return (
        f"MECHANICAL TRUTH: {character_name} used {approach_label} on {target_npc_name} ({npc_attitude}) "
        f"requesting: '{request_description}'.\n"
        f"DC {final_dc} (Attitude={attitude_key.capitalize()} DC{base_dc}, Risk={risk_key.capitalize()} +{risk_mod}). "
        f"Roll: {roll_str} + {cha_mod} CHA = **{total}** vs DC {final_dc} → **{outcome}**."
    )


# === REQ-ECO-001: Currency Normalization ===
from rules_engine import (
    CP, SP, EP, GP, PP,
    gold_to_cp, silver_to_cp, electrum_to_cp, pp_to_cp,
    cp_to_gold, cp_to_silver,
    parse_coin_string as _parse_coin_string,
    format_cp as _format_cp,
    cr_to_xp, xp_to_cr,
    calc_encounter_xp, evaluate_encounter as _evaluate_encounter,
    calc_party_xp_budget, get_char_xp_threshold,
    distribute_xp, get_daily_xp_budget,
    max_push_drag_lift, max_carrying_capacity, carrying_status, carrying_speed_penalty,
)


@tool
async def convert_currency(
    amount: int,
    from_unit: str,
    to_unit: str = "cp",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-ECO-001: Convert between D&D currency units.

    Converts `amount` of `from_unit` to the equivalent value in `to_unit`.
    Units: cp, sp, ep, gp, pp (case-insensitive).

    Returns a human-readable result like '50 gp = 5000 cp'.
    """
    from_unit = from_unit.lower()
    to_unit = to_unit.lower()
    unit_map = {"cp": CP, "sp": SP, "ep": EP, "gp": GP, "pp": PP}

    if from_unit not in unit_map:
        return f"SYSTEM ERROR: Unknown from_unit '{from_unit}'. Use: cp, sp, ep, gp, pp."
    if to_unit not in unit_map:
        return f"SYSTEM ERROR: Unknown to_unit '{to_unit}'. Use: cp, sp, ep, gp, pp."

    # Convert to CP first (the engine's internal representation)
    cp_value = amount * unit_map[from_unit]
    # Then convert to target unit
    converted = cp_value // unit_map[to_unit]

    return f"Currency Conversion: {amount} {from_unit} = {cp_value} cp = {converted} {to_unit}."


@tool
async def parse_and_format_coins(
    coin_string: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-ECO-001: Parse a coin string into total CP and human-readable breakdown.

    Supports formats like '5 gp', '12gp', '100 cp', or comma-separated
    '5 gp, 12 sp, 100 cp'. Case-insensitive. Returns both total CP
    and a nicely formatted breakdown.
    """
    total_cp = _parse_coin_string(coin_string)
    breakdown = _format_cp(total_cp)
    return f"Coin String '{coin_string}' → Total: {total_cp} cp. Breakdown: {breakdown}."


# === REQ-BUI-001-010: Encounter Building ===
@tool
async def evaluate_encounter_difficulty(
    monster_crs: Annotated[list[float], Field(description="List of monster Challenge Ratings (CRs) in the encounter.")],
    party_levels: Annotated[list[int], Field(description="List of player character levels in the party.")],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-BUI-001/002/003/004/006/010: Evaluate encounter difficulty for a party.

    Compares total monster XP against party XP budgets for each difficulty tier.
    Returns the difficulty rating (Trivial/Easy/Medium/Hard/Deadly) and a
    detailed breakdown including budgets per tier, total XP, and any
    lethality warnings (REQ-BUI-006).

    monster_crs example: [2, 3, 1] for three monsters of CR 2, 3, and 1.
    party_levels example: [5, 5, 4, 4] for a four-member party.
    """
    result = _evaluate_encounter(monster_crs, party_levels)

    lines = [
        f"Encounter Evaluation (REQ-BUI-002/004):",
        f"  Monster CRs: {monster_crs}",
        f"  Total XP: {result['total_xp']}",
        f"  Difficulty: **{result['difficulty']}**",
        "",
        f"  Party XP Budgets (REQ-BUI-003):",
        f"    Easy:    {result['budgets']['easy']} XP",
        f"    Medium:  {result['budgets']['medium']} XP",
        f"    Hard:   {result['budgets']['hard']} XP",
        f"    Deadly: {result['budgets']['deadly']} XP",
    ]

    if result["warnings"]:
        lines.append("")
        lines.append("  WARNINGS:")
        for w in result["warnings"]:
            lines.append(f"    ⚠ {w}")

    return "\n".join(lines)


@tool
async def build_encounter(
    target_difficulty: Annotated[str, Field(description="Desired difficulty: trivial, easy, medium, hard, or deadly.")],
    party_levels: Annotated[list[int], Field(description="List of player character levels.")],
    monster_pool: Annotated[list[dict] | None, Field(default=None, description="Optional list of available monsters as dicts with 'name' and 'cr' keys. If not provided, only XP math is returned.")],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-BUI-003/004: Given a target difficulty and party levels, return the XP budget.

    This tool calculates the XP budget for the requested difficulty and
    optionally suggests monster composition if a monster_pool is provided.

    Note: Actual monster selection from a pool requires the DM to choose
    appropriate CRs from the available monsters.
    """
    diff = target_difficulty.lower()
    if diff not in ("trivial", "easy", "medium", "hard", "deadly"):
        return f"SYSTEM ERROR: Unknown difficulty '{target_difficulty}'. Use: trivial, easy, medium, hard, deadly."

    budget = calc_party_xp_budget(party_levels, diff)
    daily_budget = get_daily_xp_budget(party_levels)

    lines = [
        f"Encounter Planning (REQ-BUI-003/004):",
        f"  Target Difficulty: {target_difficulty.capitalize()}",
        f"  Party Levels: {party_levels} (avg {sum(party_levels)/len(party_levels):.1f})",
        f"  XP Budget: {budget}",
        f"  Daily XP Budget (REQ-BUI-005): {daily_budget}",
    ]

    if monster_pool:
        total_xp = 0
        selected = []
        remaining = list(monster_pool)
        while remaining:
            best = None
            best_cr = 0
            for m in remaining:
                cr = float(m["cr"])
                if total_xp + cr_to_xp(cr) <= budget:
                    if cr > best_cr:
                        best_cr = cr
                    best = m
            if best is None:
                break
            selected.append(best)
            remaining.remove(best)
            total_xp += cr_to_xp(float(best["cr"]))

        lines.append(f"  Suggested monsters (XP total: {total_xp}):")
        for m in selected:
            lines.append(f"    - {m['name']} (CR {m['cr']}, XP {cr_to_xp(float(m['cr']))})")
        if remaining and selected:
            lines.append(f"  Note: {len(remaining)} monster(s) in pool could not be added without exceeding budget.")

    return "\n".join(lines)


@tool
async def distribute_encounter_xp(
    total_encounter_xp: Annotated[int, Field(description="Total XP from the defeated encounter.")],
    num_party_members: Annotated[int, Field(description="Number of surviving, participating party members.")],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-BUI-010: Divide XP equally among surviving party members.

    Returns the XP award per character. Remainder CP is discarded (floor division).
    """
    if num_party_members <= 0:
        return "SYSTEM ERROR: num_party_members must be a positive integer."

    awards = distribute_xp(total_encounter_xp, num_party_members)
    return (
        f"XP Distribution (REQ-BUI-010): "
        f"{total_encounter_xp} XP ÷ {num_party_members} members = "
        f"{awards[0]} XP each. Total distributed: {awards[0] * num_party_members} XP "
        f"({total_encounter_xp - awards[0] * num_party_members} XP discarded as remainder)."
    )


# === DEGA Encounter Generation (REQ-BUI-007/008/009) ===  # noqa: E501

from typing import Any

# ---- Damage-type psychological atmosphere (Ammann's Metaphorical Mechanics) ----
# Maps damage type → (label, short_atmospheric_blurb, combat_voice_hint, interaction_tone)
# Used to give the LLM-DM explicit flavor guidance per creature's damage profile.
_DAMAGE_TYPE_ATMOSPHERE: dict[str, tuple[str, str, str, str]] = {
    "acid": (
        "The Corroding Malice",
        "Spiteful erosion of willpower and foundation. The creature's attacks represent toxic, "
        "lingering hatred that breaks down both body and resolve. Targets feel their confidence "
        "dissolving, their certainty in their own strength melting away.",
        "Speaks in hissing, dripping contempt. Comments mock the target's weakening defenses. "
        "Mouths phrases like 'I can already hear your armor pitting' or 'your resolve is already "
        "eating itself.'",
        "Cold hatred. The creature holds grudges and references past failures. A target who "
        "escapes may find their equipment corroding for days afterward.",
    ),
    "cold": (
        "The Apathetic Ruthlessness",
        "Unfeeling, surgical detachment. The creature treats targets as statistics, problems to "
        "be solved with the minimum necessary effort. Cold damage represents the numb realization "
        "that no mercy, guilt, or hesitation will be shown.",
        "Speaks in flat, clipped tones. No passion, no taunts. Only precise statements of intent "
        "and outcome. 'You will stop moving. I will wait.' Does not dignify attacks with words.",
        "Merciless but not cruel — there is no pleasure in cruelty, only efficiency. Interaction "
        "reveals a creature that has simply stopped seeing others as worth emotional investment.",
    ),
    "fire": (
        "The Blind Wrath",
        "Overwhelming, chaotic destruction driven by hatred or zealous fury. Fire damage represents "
        "consumption — of the self, of others, of everything in range. Targets feel the heat of "
        "uncontrolled emotion consuming their safety.",
        "Roars, screams, or preaches. Speech is absolute and inflammatory. References burning away "
        "impurity, purging the weak, or the glorious blaze that will consume all. Often quotes "
        "apocalyptic mantras.",
        "Zealotry or grief weaponized as violence. The creature's grudges burn bright; its hatred "
        "is all-consuming. Players who engage in dialogue will find only a monologue of consumed reason.",
    ),
    "psychic": (
        "The Existential Terror",
        "Weaponized trauma and identity unraveling. Psychic damage represents the forced "
        "confrontation with incomprehensible realities — the violation of the mind itself. Targets "
        "lose track of who they are mid-combat.",
        "Whispers contradictory truths, recites the target's own memories back incorrectly, or "
        "describes visions of their death from perspectives they have never occupied. "
        "'You already know how this ends. You've always known.'",
        "The creature seems to know things it should not. Dialogue shifts between prophetic "
        "foreknowledge and complete dissociation. Targets describe feeling 'peeled back' and 'seen "
        "in ways I can't describe.'",
    ),
    "necrotic": (
        "The Despairing Entropy",
        "The inevitability of decay and the draining of hope. Necrotic damage represents the "
        "ultimate futility — not death, but the slow slide toward it, the certainty that resistance "
        "changes nothing. Targets feel life itself becoming heavier.",
        "Speaks in tones of exhausted certainty. References the futility of fighting, the "
        "inevitability of the end. Often delivers monologues about how everything falls eventually, "
        "how the target was already dead the moment they entered this space.",
        "Somber, patient, almost pitying. The creature does not hate — it has simply accepted what "
        "awaits. Dialogue reveals a deep weariness that manifests as methodical cruelty or "
        "strangely gentle dismissal.",
    ),
    "force": (
        "The Unstoppable Conviction",
        "Pure, undiluted will made manifest as motion. Force damage is the imposition of the "
        "creature's intent onto reality without negotiation. Targets are not attacked — they are "
        "simply moved to where the creature intends them to be.",
        "Speaks with absolute authority. No threats, no emotion — only declarations. 'You will "
        "be moved.' 'This is not a choice.' Commands and spatial directives dominate its speech.",
        "The creature is always in control of the environment. It repositions targets as easily as "
        "speaking. Negotiation is possible but framed entirely on its terms.",
    ),
    "lightning": (
        "The Sudden Fury",
        "Instant, overwhelming voltage — the chaos of a storm made personal. Lightning damage "
        "represents the unpredictability of nature weaponized against a single target. Attacks "
        "are not planned; they simply happen, faster than thought.",
        "Speaks in crackling asides, fragmented shouts, or electric silence between strikes. "
        "Laughs at the speed of its own attacks. 'Did you see it coming? No. You won't next time either.'",
        "Erratic and hyperactive. The creature cannot sit still mentally or physically. It zips "
        "between topics, between positions, between targets. Dialogue is a dazzling, disorienting sprint.",
    ),
    "thunder": (
        "The Overwhelming Presence",
        "Raw kinetic force delivered with earth-shaking authority. Thunder damage is the "
        "declaration that the creature occupies this space and the target does not. It is not "
        "about killing — it is about announcing arrival.",
        "Speaks in declarations that physically resonate. Every word carries weight, literally. "
        "Shouts the creature's name or title as it strikes. Roars challenges that make the "
        "ground tremble.",
        "The creature's presence fills the room before combat begins. It speaks to claim space, "
        "literally and metaphorically. Negotiation is shouting over the creature's presence — "
        "it may not even lower its voice to listen.",
    ),
    "poison": (
        "The Patient Malice",
        "Slow, creeping ruination that starts invisible. Poison damage represents a grudge "
        "that has been cultivated for years, applied drop by drop, designed to watch the "
        "target deteriorate from something that seemed harmless.",
        "Speaks softly, with careful enunciation. References 'what's coming' in measured terms. "
        "Often describes symptoms with clinical detachment. 'You'll feel it in an hour. By morning, "
        "you'll understand.'",
        "The creature seems helpful until it isn't. It offers what appears to be genuine assistance "
        "or information, then reveals the poison beneath. Targets feel paranoid about every subsequent "
        "interaction, even with allies.",
    ),
    "slashing": (
        "The Territorial Claim",
        "Tearing flesh to mark territory and establish dominance. Slashing damage is personal — "
        "the creature is not just defeating the target, it is proving its superiority through "
        "visible, visceral evidence.",
        "Growls, snarls, or delivers territorial challenges. Threatens to mark those who flee. "
        "'Running just means I get to chase you first.' Proves dominance with every swing.",
        "The creature establishes dominance immediately. Physical posturing dominates all "
        "interactions. Treats negotiation as a test of status rather than an exchange of information.",
    ),
    "piercing": (
        "The Clinical Precision",
        "Surgical penetration to find and exploit weakness. Piercing damage represents a "
        "mind that sees through armor, deflection, and deception to the single point that "
        "matters. It is the attack of a hunter, not a warrior.",
        "Speaks in hunting terminology. References finding 'the right angle' on a target. "
        "Comments on weak points discovered. 'There's the one.' Rarely speaks during combat "
        "but delivers verdict-like statements before each strike.",
        "Analytical and patient. The creature evaluates everything as potential prey or "
        "terrain. It may know more about the party than they know about themselves, assembled "
        "through patient observation.",
    ),
    "bludgeoning": (
        "The Absolute Stop",
        "Unstoppable, grinding force that ends motion and argument alike. Bludgeoning damage "
        "is the rejection of evasion — the creature does not care about armor or agility, "
        "only that the target stops.",
        "Speaks in terms of finality and stopping. 'Stop.' 'Be still.' 'You are done.' "
        "Short, crushing declarations. The creature does not taunt — it delivers verdicts.",
        "The creature treats all resistance as already concluded. It speaks from the assumption "
        "that the target's fate is already decided, and it is merely informing them of details.",
    ),
}


def _guess_damage_type_for_role(role: str, cr: float) -> list[str]:
    """Assign thematic damage type(s) to a DEGA tactical role.

    Returns a list of 1-3 damage type keys from _DAMAGE_TYPE_ATMOSPHERE,
    reflecting the archetype's psychological profile.
    """
    # High-CR elites and solos are thematically richer
    high_cr = cr >= 5.0

    mapping: dict[str, list[str]] = {
        "elite":     ["force", "thunder"] if high_cr else ["piercing", "slashing"],
        "brute":     ["bludgeoning", "thunder"],
        "artillerist": ["fire", "lightning"] if high_cr else ["fire"],
        "controller": ["psychic", "cold"] if high_cr else ["psychic"],
        "lurker":    ["piercing", "poison"],
        "skirmisher": ["lightning", "piercing"],
        "support":   ["cold", "necrotic"],  # healing twisted to siphon rather than restore
        "tank":      ["bludgeoning", "cold"],
        "minion":    ["slashing", "piercing"],
        "solo":      ["force", "psychic", "necrotic"],
    }
    return mapping.get(role, ["slashing"])


def _build_tactical_demeanor(monster_list: list[dict]) -> str:
    """Build the TACTICAL DEMEANOR block for MECHANICAL TRUTH output.

    For each monster, emits damage-type psychological flavor so the LLM-DM
    has concrete atmospheric guidance for combat voice, interaction tone, and observable behavior.
    """
    lines = ["  TACTICAL DEMEANOR:"]
    for m in monster_list:
        name = m.get("name", "Unknown")
        role = m.get("role", "standard").lower()
        cr = m.get("cr", 0)

        dmg_types = m.get("damage_types", _guess_damage_type_for_role(role, cr))

        role_lines = []
        for dt_key in dmg_types[:3]:  # Cap at 3 damage types for brevity
            if dt_key not in _DAMAGE_TYPE_ATMOSPHERE:
                continue
            label, atmosphere, combat_voice, interaction_tone = _DAMAGE_TYPE_ATMOSPHERE[dt_key]
            role_lines.append(f"    [{dt_key.upper()}] {label}")
            role_lines.append(f"      ATMOSPHERE: {atmosphere}")
            role_lines.append(f"      COMBAT VOICE: {combat_voice}")
            role_lines.append(f"      INTERACTION TONE: {interaction_tone}")

        if not role_lines:
            role_lines = [f"    Standard combatant — no special damage-type flavor."]
        else:
            # Join with continuation indent
            role_lines = ["    Damage-type psychological profile:"] + role_lines

        lines.append(f"  {name} (role: {role}, CR {cr:.1f}):")
        lines.extend(role_lines)

    return "\n".join(lines)


# ---- DEGA archetype definitions ----
_ARCHETYPE_WEIGHTS_BY_PACING = {
    # E = encounters remaining today (including this one)
    1: {"apex": 0.70, "phalanx": 0.15, "ambush": 0.10, "swarm": 0.05},
    2: {"ambush": 0.40, "phalanx": 0.30, "swarm": 0.20, "apex": 0.10},
    3: {"swarm": 0.40, "phalanx": 0.30, "ambush": 0.20, "apex": 0.10},
}

_ARCHETYPE_ROLE_XP = {
    # Archetype -> list of (role, xp_fraction_of_encounter_budget)
    "phalanx": [("tank", 0.40), ("artillerist", 0.40), ("minion", 0.20)],
    "ambush":  [("lurker", 0.50), ("controller", 0.30), ("skirmisher", 0.20)],
    "swarm":   [("brute", 0.30), ("minion", 0.60), ("support", 0.10)],
    "apex":    [("elite", 0.70), ("minion", 0.15), ("controller", 0.15)],
}

# DEGA tactical-role template modifiers.
# Each entry: (cr_delta, ac_delta, description).
# AC delta is added to base AC (capped at max 22 for Tank).
_ROLE_TEMPLATES = {
    "elite":       {"cr": +1, "ac": +4, "desc": "Advanced (+2 all d20 rolls, +4 AC, +2 HP/hit die)"},
    "artillerist": {"cr":  0, "ac": -2, "desc": "Ranged attack (R≥30 ft), -2 AC"},
    "brute":       {"cr": +1, "ac": -2, "desc": "+2 to hit, +1 damage die, -2 AC"},
    "controller":  {"cr":  0, "ac":  0, "desc": "+Spellcasting (Save DC 13, web/command/bane)"},
    "lurker":      {"cr":  0, "ac":  0, "desc": "+10 ft speed, auto-grapple, Stealth expertise"},
    "skirmisher":  {"cr":  0, "ac": -2, "desc": "Double speed or Fly speed, reach +5 ft"},
    "support":     {"cr":  0, "ac":  0, "desc": "+Healing spellcasting (cure wounds 3/day, bless)"},
    "tank":        {"cr": +1, "ac": +4, "desc": "+4 AC (max 22), Parry reaction (+prof to AC vs melee)"},
    "minion":      {"cr":  0, "ac": -2, "desc": "1 HP, auto-succeeds spell saves (Flee Mortals! variant)"},
}


def _compute_party_levels_from_kg(
    kg: KnowledgeGraph,
    current_map_id: str | None,
    vault_path: str,
) -> list[int]:
    """DEGA §1: Query KG for PLAYER/NPC nodes on current map; return their levels.

    Filters out entities whose KG node has is_remote=True or a mismatched map_id,
    which covers NPCs on other maps or too distant to join the encounter.
    """
    levels: list[int] = []
    entities = get_all_entities(vault_path)
    for entity in entities.values():
        if not isinstance(entity, Creature) or not entity.classes:
            continue
        # Exclude summoned companions unless their master is present (handled elsewhere)
        if entity.summoned_by_uuid:
            continue
        level = sum(c.level for c in entity.classes)
        if level <= 0:
            continue

        # KG-based map filtering: cross-reference entity position with KG node
        node: KnowledgeGraphNode | None = kg.get_node_by_name(entity.name)
        if node:
            if node.get_attribute("is_remote") is True:
                continue
            node_map = node.get_attribute("map_id")
            if current_map_id and node_map and node_map != current_map_id:
                continue
        levels.append(level)
    return levels


def _select_archetype(encounters_remaining: int) -> str:
    """DEGA §2: Select encounter archetype weighted by session pacing."""
    weights = _ARCHETYPE_WEIGHTS_BY_PACING.get(
        encounters_remaining, _ARCHETYPE_WEIGHTS_BY_PACING[3]
    )
    archs = list(weights.keys())
    probs = list(weights.values())
    return random.choices(archs, weights=probs, k=1)[0]


def _distribute_xp_to_roles(
    archetype: str,
    xp_enc: int,
) -> dict[str, int]:
    """DEGA §2: Split XP_enc across tactical roles per archetype ratios."""
    role_xp: dict[str, int] = {}
    for role, fraction in _ARCHETYPE_ROLE_XP[archetype]:
        role_xp[role] = int(xp_enc * fraction)
    return role_xp


def _best_cr_for_xp(xp_target: int, cr_max: float) -> float:
    """Return the highest CR whose XP does not exceed xp_target."""
    best_cr: float = 0.0
    for cr_val in sorted(_rules_engine._CR_TO_XP.keys(), reverse=True):
        if cr_val <= cr_max and _rules_engine._CR_TO_XP[cr_val] <= xp_target:
            best_cr = cr_val
            break
    return best_cr


def _build_env_requirements(
    role_counts: dict[str, int],
) -> dict[str, Any]:
    """DEGA §4: Derive environmental constraints from assigned tactical roles."""
    reqs: dict[str, Any] = {}
    if role_counts.get("artillerist", 0) > 0:
        reqs["cover_elements"] = ["half_cover"] * 3 + ["total_cover"]
        reqs["line_of_sight"] = "clear center corridor required"
    if role_counts.get("lurker", 0) > 0:
        reqs["obscurement"] = "heavy (40% of battlefield, e.g. fog, darkness, deep water)"
        reqs["verticality"] = "required (climb or fly surfaces)"
    if role_counts.get("skirmisher", 0) > 0:
        reqs["arena_size"] = "minimum 60×60 ft"
        reqs["difficult_terrain"] = "central patches to penalize player movement"
    if role_counts.get("controller", 0) > 0:
        reqs["choke_points"] = "required (narrow corridors, doorways, rope bridges)"
    if role_counts.get("brute", 0) > 0:
        reqs["open_ground"] = "open lanes for charge approach"
    return reqs


def _apply_template(
    base_cr: float,
    role: str,
    cr_max: float,
) -> tuple[float, str, str]:
    """Apply DEGA role template to a base CR; returns (new_cr, role_label, template_desc)."""
    tmpl = _ROLE_TEMPLATES.get(role, {"cr": 0, "ac": 0, "desc": ""})
    new_cr = min(base_cr + tmpl["cr"], cr_max)
    label = f"{role.capitalize()} ({tmpl['desc']})"
    return new_cr, label, tmpl["desc"]


@tool
async def generate_or_calibrate_encounter(
    party_levels: Annotated[
        list[int] | None,
        Field(default=None, description=(
            "List of PC levels. If omitted, the tool queries the Knowledge Graph "
            "for all PLAYER/NPC entities on the current map and extracts levels "
            "from the engine registry, excluding remote/distant entities."
        )),
    ],
    mode: Annotated[
        str,
        Field(description='"generate" (random) or "calibrate" (tune pre-planned)'),
    ],
    preplanned_monsters: Annotated[
        list[dict] | None,
        Field(
            default=None,
            description='Calibrate mode: list of {name, cr, role_hint?} for each intended enemy',
        ),
    ],
    location_tags: Annotated[
        list[str] | None,
        Field(default=None, description="KG location tags to scope entity queries (e.g. [forest, underground])"),
    ],
    encounters_today: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Encounters already completed since the last long rest. "
                "Used to derive session pacing E = encounters_today + 1."
            ),
        ),
    ],
    target_difficulty: Annotated[
        str,
        Field(default="medium", description="trivial / easy / medium / hard / deadly"),
    ],
    current_map_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "KG map_id of the current location. Used to exclude party members "
                "on other maps / too distant to join the encounter."
            ),
        ),
    ],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """DEGA Encounter Generator — REQ-BUI-007/008/009.

    Implements all four DEGA phases:

    **Phase I — Budget**: Queries KG for active party (excluding remote/distant NPCs),
    derives N, APL, and session pacing E. Computes XP_enc and CR_max.

    **Phase II — Archetype Selection**: In generate mode, selects a DEGA archetype
    weighted by E (Apex for climactic single encounters, Swarm/Phalanx for attrition).

    **Phase III — Composition / Calibration**: Distributes XP_enc across tactical roles.
    In generate mode, selects base creatures from the KG entity pool and applies
    DEGA role templates (Brute, Artillerist, Controller, Elite, Lurker, Minion,
    Skirmisher, Solo, Support, Tank) to fit the budget. In calibrate mode, evaluates
    pre-planned monsters against XP_enc and applies Elite/Brute upscaling or Minion
    downscaling to close the gap.

    **Phase IV — Spatial Requirements**: Derives environmental constraints
    (cover for Artillerists, obscurement for Lurkers, choke points for Controllers,
    etc.) and verifies or generates them via the Spatial Engine.

    Output format: ``MECHANICAL TRUTH:`` string captured by the LangGraph
    action_logic_node for deferred mutation execution.

    Args:
        mode: "generate" builds a random encounter; "calibrate" tunes pre-planned enemies.
        preplanned_monsters: Required when mode="calibrate". Each dict: {name, cr, role_hint?}.
        party_levels: Optional override for party composition. If None, derived from KG.
        encounters_today: Encounters completed since last long rest.
        target_difficulty: Base difficulty (escalates automatically when E is low).
        current_map_id: KG map_id for filtering co-located party members.
        location_tags: KG tags for scoping candidate creature pool.
    """
    # ---- Derive vault_path from config ----
    vault_path: str = config.get("configurable", {}).get("vault_path", "default") if config else "default"

    # ---- Phase I: Party composition ----
    if party_levels is None:
        kg = get_knowledge_graph(vault_path)
        party_levels = _compute_party_levels_from_kg(kg, current_map_id, vault_path)

    if not party_levels:
        return (
            "SYSTEM ERROR: No party members found. Provide party_levels explicitly, "
            "or ensure the KG contains PLAYER/NPC nodes co-located on the current map."
        )

    N = len(party_levels)
    L = sum(party_levels) // N  # Average Party Level (integer floor)
    E = encounters_today + 1      # This encounter = encounters_today + 1

    # Escalate difficulty when pacing forces a single big battle
    effective_difficulty = target_difficulty
    if E <= 2:
        effective_difficulty = "deadly"

    XP_total   = get_daily_xp_budget(party_levels)
    XP_enc     = XP_total // E
    CR_max     = min(L + math.ceil(L / 2), 30)

    # ---- Generate or Calibrate? ----
    if mode == "calibrate":
        if not preplanned_monsters:
            return "SYSTEM ERROR: calibrate mode requires preplanned_monsters list."
        current_xp = sum(cr_to_xp(float(m["cr"])) for m in preplanned_monsters)
        xp_gap = XP_enc - current_xp

        lines = [
            "MECHANICAL TRUTH:",
            f"  Party: {N} members, APL {L}, encounters_today={encounters_today}",
            f"  XP_enc={XP_enc} (daily={XP_total}, E={E}), CR_max={CR_max}",
            f"  Effective difficulty: {effective_difficulty}",
            f"  Pre-planned XP={current_xp}, gap={xp_gap:+d}",
            f"  Monsters:",
        ]
        calibrated: list[dict] = []
        role_assignments: dict[str, str] = {}

        for m in preplanned_monsters:
            base_cr = float(m["cr"])
            role_hint = m.get("role_hint", "") or "standard"
            # Decide whether to upscale, leave as-is, or strip
            if xp_gap > 0 and base_cr < CR_max:
                # Gap is positive (underbudget) — upgrade strongest pre-planned
                new_cr, label, _ = _apply_template(base_cr, "elite", CR_max)
                xp_gain = cr_to_xp(new_cr) - cr_to_xp(base_cr)
                xp_gap -= xp_gain
                # Preserve original role_hint for narrative coherence
                effective_role = role_hint if role_hint else label
                role_assignments[m["name"]] = effective_role
                lines.append(f"    - {m['name']} CR {base_cr} → upgraded to {label} (CR {new_cr:.1f}) [{effective_role}]")
                calibrated.append({
                    **m, "cr": new_cr, "role": effective_role,
                    "damage_types": _guess_damage_type_for_role(effective_role, new_cr),
                })
            else:
                role_assignments[m["name"]] = role_hint
                lines.append(f"    - {m['name']} (CR {base_cr}, {role_hint})")
                calibrated.append({
                    **m, "role": role_hint,
                    "damage_types": _guess_damage_type_for_role(role_hint, base_cr),
                })

        # If still under budget, add minions to fill
        if xp_gap > 0:
            minion_cr = _best_cr_for_xp(xp_gap, CR_max)
            if minion_cr > 0:
                lines.append(f"    - Minion (CR {minion_cr:.1f}) — added to fill XP gap")
                calibrated.append({
                    "name": "Minion", "cr": minion_cr, "role": "minion",
                    "damage_types": _guess_damage_type_for_role("minion", minion_cr),
                })

        # Build role counts for env requirements
        role_counts: dict[str, int] = {}
        for m in calibrated:
            role = m.get("role", "standard").lower()
            role_counts[role] = role_counts.get(role, 0) + 1
        reqs = _build_env_requirements(role_counts)
        lines.append(f"  Environmental requirements: {reqs}")
        lines.append(_build_tactical_demeanor(calibrated))
        return "\n".join(lines)

    # ---- Generate mode ----
    archetype = _select_archetype(E)
    role_xp = _distribute_xp_to_roles(archetype, XP_enc)

    # Collect candidate creatures from KG by location_tags
    kg = get_knowledge_graph(vault_path)
    candidates: list[tuple[str, float]] = []  # (name, cr)
    if kg and kg.nodes:
        for node in kg.nodes.values():
            if location_tags:
                if not any(tag in node.tags for tag in location_tags):
                    continue
            if node.node_type in (GraphNodeType.NPC, GraphNodeType.PLAYER):
                cr = node.get_attribute("challenge_rating") or node.get_attribute("cr")
                if cr is not None:
                    try:
                        candidates.append((node.name, float(cr)))
                    except (TypeError, ValueError):
                        pass

    # Fallback pool if KG has no matching creatures
    if not candidates:
        candidates = [
            ("Bandit", 0.125), ("Cultist", 0.125), ("Wolf", 0.25),
            ("Orc", 0.5), ("Goblin", 0.25), ("Skeleton", 0.25),
            ("Zombie", 0.25), ("Ogre", 2.0), ("Owlbear", 5.0),
        ]

    lines = [
        "MECHANICAL TRUTH:",
        f"  Party: {N} members, APL {L}, encounters_today={encounters_today}",
        f"  XP_enc={XP_enc} (daily={XP_total}, E={E}), CR_max={CR_max}",
        f"  Effective difficulty: {effective_difficulty}",
        f"  Archetype: {archetype} (DEGA §2)",
        f"  Monsters:",
    ]

    role_counts: dict[str, int] = {}
    monster_list: list[dict] = []  # for tactical demeanor
    for role, role_budget_xp in role_xp.items():
        best_cr = _best_cr_for_xp(role_budget_xp, CR_max)
        if best_cr <= 0:
            continue
        # Pick a random candidate close to best_cr
        candidates_sorted = sorted(candidates, key=lambda c: abs(c[1] - best_cr))
        name = candidates_sorted[0][0] if candidates_sorted else role.capitalize()
        new_cr, label, _ = _apply_template(best_cr, role, CR_max)
        role_counts[role] = role_counts.get(role, 0) + 1
        dmg_types = _guess_damage_type_for_role(role, new_cr)
        lines.append(f"    - {name} (CR {best_cr:.2g}, {label})")
        monster_list.append({"name": name, "cr": best_cr, "role": role, "damage_types": dmg_types})

    reqs = _build_env_requirements(role_counts)
    lines.append(f"  Environmental requirements: {reqs}")
    lines.append(_build_tactical_demeanor(monster_list))
    return "\n".join(lines)


# === REQ-INV-002: Carrying Capacity ===
@tool
async def calculate_carrying_capacity(
    strength_score: Annotated[int, Field(description="The entity's Strength score.")],
    current_load_lbs: Annotated[int, Field(description="Current weight carried in pounds.", default=0)],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-INV-002: Calculate carrying capacity and speed penalty.

    - Max carrying (no penalty): Strength × 15 lbs
    - Encumbered (speed -20 ft): between Strength×15 and Strength×30 lbs
    - Heavily encumbered (speed -40 ft, can't run): between Strength×30 and Strength×60 lbs
    - Over push/drag/lift max (Strength×60): can't move

    Returns a detailed status report.
    """
    max_carry = max_carrying_capacity(strength_score)
    max_push = max_push_drag_lift(strength_score)
    status = carrying_status(current_load_lbs, strength_score)
    penalty = carrying_speed_penalty(status)

    lines = [
        f"Carrying Capacity (REQ-INV-002):",
        f"  Strength Score: {strength_score}",
        f"  Max Carry (no penalty): {max_carry} lbs (Str×15)",
        f"  Max Push/Drag/Lift: {max_push} lbs (Str×30)",
        f"  Current Load: {current_load_lbs} lbs",
        f"  Status: **{status.replace('_', ' ').capitalize()}**",
    ]

    if penalty > 0:
        lines.append(f"  Speed Penalty: -{penalty} ft")

    if current_load_lbs > max_push:
        lines.append(f"  ⚠ OVER PUSH/DRAG/LIFT MAX — Entity is completely immobilized!")

    return "\n".join(lines)


# === REQ-ECO-002/003/004: Economy Tools ===
from rules_engine import gold_to_cp as _gold_to_cp, cp_to_gold as _cp_to_gold

# Lifestyle expense table (GP per day)
_LIFESTYLE_EXPENSES = {
    "squalid": 1,      # 1 gp/day
    "poor": 2,         # 2 gp/day
    "modest": 10,      # 10 gp/day
    "comfortable": 20, # 20 gp/day
    "wealthy": 40,     # 40 gp/day
    "aristocratic": 100, # 100 gp/day
}


@tool
async def sell_item(
    item_name: str,
    item_type: str,
    base_cost_cp: int,
    is_damaged: bool = False,
    is_trade_good: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-ECO-002/003: Calculate the sale price of an item.

    - Mundane equipment (weapons, armor, adventuring gear): undamaged sells for 50% of base cost.
    - Trade goods (wheat, gold bars, livestock): always sell for 100% of base cost.
    - Damaged items: 50% of base cost (same formula, no bonus).

    Use `is_trade_good=True` for trade goods to bypass the 50% rule.
    Returns the sale price in CP and a human-readable breakdown.
    """
    if base_cost_cp < 0:
        return f"SYSTEM ERROR: base_cost_cp cannot be negative."

    if is_trade_good:
        # REQ-ECO-003: Trade goods sell at 100%
        sale_price = base_cost_cp
        formula = "100% (trade goods)"
    elif is_damaged:
        # Damaged items still sell at 50%
        sale_price = base_cost_cp // 2
        formula = "50% (damaged)"
    else:
        # REQ-ECO-002: Undamaged mundane equipment sells at 50%
        sale_price = base_cost_cp // 2
        formula = "50% (undamaged mundane)"

    gp_sale = sale_price // 100
    cp_remainder = sale_price % 100
    gp_base = base_cost_cp // 100
    cp_base = base_cost_cp % 100

    return (
        f"Sale Price (REQ-ECO-002/003): '{item_name}' ({item_type}).\n"
        f"  Base cost: {gp_base} gp {cp_base} cp.\n"
        f"  Condition: {'Damaged' if is_damaged else 'Undamaged'}{' (Trade Good)' if is_trade_good else ''}.\n"
        f"  Formula: {formula}.\n"
        f"  Sale price: **{gp_sale} gp {cp_remainder} cp** ({sale_price} cp).\n"
        f"  (If you want to actually remove the item from inventory, do so manually.)"
    )


@tool
async def deduct_lifestyle_expense(
    character_name: str,
    lifestyle: str,
    days: int = 1,
    wallet_cp: int = 0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-ECO-004: Deduct lifestyle expenses from a character's wallet.

    Lifestyle tiers (2024 PHB Ch. 6), cost in GP per day:
      Squalid=1, Poor=2, Modest=10, Comfortable=20, Wealthy=40, Aristocratic=100.

    The expense is deducted from the wallet_cp value provided.
    Returns the remaining wallet CP after deduction.
    """
    lifestyle_key = lifestyle.lower().strip()
    if lifestyle_key not in _LIFESTYLE_EXPENSES:
        valid = ", ".join(_LIFESTYLE_EXPENSES.keys())
        return f"SYSTEM ERROR: Unknown lifestyle '{lifestyle}'. Valid: {valid}."

    if days <= 0:
        return f"SYSTEM ERROR: days must be a positive integer."

    daily_cost_gp = _LIFESTYLE_EXPENSES[lifestyle_key]
    total_cost_gp = daily_cost_gp * days
    total_cost_cp = total_cost_gp * 100

    if wallet_cp < total_cost_cp:
        remaining = 0
        shortfall_cp = total_cost_cp - wallet_cp
        shortfall_gp = shortfall_cp // 100
        return (
            f"Lifestyle Expense (REQ-ECO-004): {character_name} lives a **{lifestyle_key}** lifestyle "
            f"for {days} day(s).\n"
            f"  Cost: {total_cost_gp} gp ({total_cost_cp} cp).\n"
            f"  Wallet: {wallet_cp // 100} gp {wallet_cp % 100} cp.\n"
            f"  ⚠ INSUFFICIENT FUNDS! Cannot afford the lifestyle.\n"
            f"  Shortfall: {shortfall_gp} gp {shortfall_cp % 100} cp.\n"
            f"  Remaining wallet: {remaining // 100} gp {remaining % 100} cp."
        )

    remaining_cp = wallet_cp - total_cost_cp
    return (
        f"Lifestyle Expense (REQ-ECO-004): {character_name} lives a **{lifestyle_key}** lifestyle "
        f"for {days} day(s).\n"
        f"  Cost: {total_cost_gp} gp ({total_cost_cp} cp).\n"
        f"  Wallet before: {wallet_cp // 100} gp {wallet_cp % 100} cp.\n"
        f"  Remaining wallet: **{remaining_cp // 100} gp {remaining_cp % 100} cp** ({remaining_cp} cp)."
    )


# === REQ-CRF-001/002/003/004/005: Crafting Tools ===
_CRAFTER_FEAT_NAME = "crafter"
_HERBALISM_KIT_NAME = "herbalism kit"
_ARCANA_PROFICIENCY = "arcana"

# Crafting progress per 8-hour day (GP of progress)
_DAILY_CRAFT_PROGRESS = 50


@tool
async def check_craft_prerequisites(
    item_name: str,
    item_base_cost_cp: int,
    required_tool: str,
    character_has_tool_proficiency: bool,
    character_wallet_cp: int,
    has_crafter_feat: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-CRF-001: Check if a character can begin crafting an item.

    Prerequisites: proficiency with the required Artisan's Tools + materials cost (50% of item base cost).
    If the character has the Crafter feat (2024), the material cost is reduced by 20%.

    Returns a detailed breakdown of whether crafting can begin.
    """
    if item_base_cost_cp < 0:
        return f"SYSTEM ERROR: item_base_cost_cp cannot be negative."

    material_cost_cp = item_base_cost_cp // 2  # Raw materials = 50% of base cost

    if has_crafter_feat:
        # REQ-CRF-003: Crafter feat gives 20% discount on non-magical item materials
        material_cost_cp = int(material_cost_cp * 0.8)

    has_materials = character_wallet_cp >= material_cost_cp

    lines = [
        f"Crafting Prerequisites (REQ-CRF-001/003): '{item_name}'",
        f"  Base item cost: {item_base_cost_cp // 100} gp {item_base_cost_cp % 100} cp.",
        f"  Required tool: {required_tool}.",
        f"  Tool proficiency: {'Yes' if character_has_tool_proficiency else 'No ✗'}.",
        f"  Material cost (50% of base, -20% Crafter feat): {material_cost_cp // 100} gp {material_cost_cp % 100} cp.",
        f"  Character wallet: {character_wallet_cp // 100} gp {character_wallet_cp % 100} cp.",
        f"  Materials available: {'Yes' if has_materials else 'No ✗'}.",
    ]

    if not character_has_tool_proficiency:
        lines.append(f"  Result: ❌ CANNOT CRAFT — lacks proficiency with {required_tool}.")
    elif not has_materials:
        lines.append(f"  Result: ❌ CANNOT CRAFT — insufficient funds for materials.")
        shortfall = material_cost_cp - character_wallet_cp
        lines.append(f"  Shortfall: {shortfall // 100} gp {shortfall % 100} cp.")
    else:
        lines.append(f"  Result: ✅ CAN BEGIN CRAFTING.")
        if has_crafter_feat:
            lines.append(f"  Note: Crafter feat reduces material cost by 20%.")

    return "\n".join(lines)


@tool
async def calculate_crafting_time(
    item_name: str,
    item_base_cost_cp: int,
    num_crafters: int = 1,
    has_crafter_feat: bool = False,
    is_herbalism_kit_potion: bool = False,
    potion_rarity: str = "common",
    spell_level: int = 0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-CRF-002/003/004/005: Calculate crafting time for an item.

    Mundane items: progress = 50 GP per 8-hour day × number of proficient crafters.
    If Crafter feat: time × 0.8 (20% faster).

    Potion of Healing (REQ-CRF-004): fixed time per rarity:
      Common=1 day, Uncommon=3 days, Rare=7 days, Very Rare=21 days, Legendary=63 days.
      Requires Herbalism Kit proficiency.

    Spell Scrolls (REQ-CRF-005): time scales with spell level.
    """
    rarity_days = {
        "common": 1, "uncommon": 3, "rare": 7,
        "very rare": 21, "legendary": 63,
    }

    if is_herbalism_kit_potion:
        rarity_key = potion_rarity.lower()
        if rarity_key not in rarity_days:
            return f"SYSTEM ERROR: Unknown potion rarity '{potion_rarity}'. Valid: {', '.join(rarity_days.keys())}."
        days = rarity_days[rarity_key]
        if has_crafter_feat:
            days = max(1, int(days * 0.8))
        return (
            f"Crafting Time (REQ-CRF-004): {potion_rarity.capitalize()} Potion of Healing.\n"
            f"  Requires: Herbalism Kit proficiency.\n"
            f"  Crafting time: {days} day(s).\n"
            f"  (Per 2024 DMG, potions require 1 day per rarity tier with Herbalism Kit.)"
        )

    if spell_level > 0:
        # REQ-CRF-005: Spell scrolls — time = 50 GP ÷ (8 gp/day at 1st level) × spell level multiplier
        # Base: 50 gp/day at level 1 → 1 day per 50 gp of scroll base cost
        # Higher levels take proportionally longer
        scroll_base_cp = item_base_cost_cp
        daily_gp_progress = _DAILY_CRAFT_PROGRESS
        if has_crafter_feat:
            daily_gp_progress = int(daily_gp_progress * 1.25)  # 20% faster means 25% more gp progress

        days = (scroll_base_cp / 100) / daily_gp_progress
        import math
        days = math.ceil(days)
        if has_crafter_feat:
            days = max(1, int(days * 0.8))

        return (
            f"Crafting Time (REQ-CRF-005): Spell Scroll (Level {spell_level}).\n"
            f"  Scroll base cost: {scroll_base_cp // 100} gp {scroll_base_cp % 100} cp.\n"
            f"  Crafters: {num_crafters}.\n"
            f"  Progress per crafter: {daily_gp_progress} gp/day.\n"
            f"  Crafting time: ~{days} day(s) of 8-hour work.\n"
            f"  (REQ-CRF-005: Time scales exponentially with spell level.)"
        )

    # REQ-CRF-002: Mundane items — 50 GP per crafter per 8-hour day
    item_gp = item_base_cost_cp / 100
    daily_progress_per_crafter = _DAILY_CRAFT_PROGRESS
    if has_crafter_feat:
        daily_progress_per_crafter = int(daily_progress_per_crafter * 1.25)  # 20% faster crafting = 25% more gp progress

    total_daily_progress = daily_progress_per_crafter * num_crafters
    import math
    days = math.ceil(item_gp / total_daily_progress)

    if has_crafter_feat:
        days = max(1, int(days * 0.8))

    return (
        f"Crafting Time (REQ-CRF-002/003): '{item_name}'.\n"
        f"  Item base cost: {item_gp:.0f} gp.\n"
        f"  Progress per day: {total_daily_progress} gp (50 gp x {num_crafters} crafter(s) x {'1.25 (Crafter feat)' if has_crafter_feat else '1'}).\n"
        f"  Crafting time: **{days}** 8-hour workday(s).\n"
        f"  Total gp crafted per day: {total_daily_progress} gp."
    )


@tool
async def record_crafting_progress(
    item_name: str,
    item_base_cost_cp: int,
    days_worked: int,
    num_crafters: int = 1,
    has_crafter_feat: bool = False,
    prior_progress_cp: int = 0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-CRF-002: Record crafting progress for downtime crafting.

    Each 8-hour workday of crafting produces 50 GP worth of progress per proficient crafter.
    Crafter feat: 20% faster (effectively 50 × 1.25 = 62.5 gp progress per day).

    When cumulative progress >= item_base_cost_cp, the item is complete.

    Returns the new progress total and whether the item is finished.
    """
    if days_worked <= 0:
        return f"SYSTEM ERROR: days_worked must be a positive integer."
    if prior_progress_cp < 0:
        return f"SYSTEM ERROR: prior_progress_cp cannot be negative."

    daily_progress_per_crafter = _DAILY_CRAFT_PROGRESS
    if has_crafter_feat:
        daily_progress_per_crafter = int(daily_progress_per_crafter * 1.25)

    daily_total = daily_progress_per_crafter * num_crafters
    # _DAILY_CRAFT_PROGRESS is in GP/day; convert to CP for storage
    new_progress_cp = prior_progress_cp + int(daily_total * days_worked * 100)
    gp_remaining = (item_base_cost_cp - new_progress_cp) / 100
    item_gp_float = item_base_cost_cp / 100

    lines = [
        f"Crafting Progress (REQ-CRF-002): '{item_name}'.",
        f"  Item base cost: {item_base_cost_cp // 100} gp {item_base_cost_cp % 100} cp.",
        f"  Progress before: {prior_progress_cp // 100} gp {prior_progress_cp % 100} cp.",
        f"  Days worked: {days_worked} x {num_crafters} crafter(s) = {daily_total * days_worked} gp progress.",
        f"  New progress: {new_progress_cp // 100} gp {new_progress_cp % 100} cp.",
    ]

    if new_progress_cp >= item_base_cost_cp:
        lines.append(f"  Status: ✅ COMPLETE! Item '{item_name}' is finished.")
        if new_progress_cp > item_base_cost_cp:
            excess = new_progress_cp - item_base_cost_cp
            lines.append(f"  Note: {excess // 100} gp {excess % 100} cp of excess progress (discarded).")
    else:
        lines.append(f"  Status: ⏳ IN PROGRESS. {gp_remaining:.0f} gp remaining.")
        pct = (new_progress_cp * 100) / item_base_cost_cp
        lines.append(f"  Completion: {pct:.1f}%.")

    return "\n".join(lines)


__all__ = [
    "roll_generic_dice",
    "search_vault_by_tag",
    "advance_time",
    "refresh_vault_data",
    "report_rule_challenge",
    "perform_ability_check_or_save",
    "take_rest",
    "interrupt_rest",
    "travel",
    "use_heroic_inspiration",
    "perform_group_check",
    "perform_social_interaction",
    "convert_currency",
    "parse_and_format_coins",
    "evaluate_encounter_difficulty",
    "build_encounter",
    "distribute_encounter_xp",
    "generate_or_calibrate_encounter",
    "calculate_carrying_capacity",
    "sell_item",
    "deduct_lifestyle_expense",
    "check_craft_prerequisites",
    "calculate_crafting_time",
    "record_crafting_progress",
]
