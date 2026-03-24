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
    "calculate_carrying_capacity",
]
