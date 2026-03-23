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
    """
    hours_to_advance = 8 if rest_type.lower() == "long" else 1
    await advance_time.ainvoke({"hours": hours_to_advance}, config)

    uuids = []
    for name in character_names:
        entity = await _get_entity_by_name(name, config["configurable"].get("thread_id"))
        if entity:
            uuids.append(entity.entity_uuid)

    if uuids:
        event = GameEvent(
            event_type="Rest",
            source_uuid=uuids[0],
            vault_path=config["configurable"].get("thread_id"),
            payload={
                "rest_type": rest_type.lower(),
                "target_uuids": uuids,
                "hit_dice_to_spend": hit_dice_to_spend,
            },
        )
        await EventBus.adispatch(event)

    return (
        f"MECHANICAL TRUTH: {', '.join(character_names)} completed a {rest_type} rest. "
        f"Time advanced {hours_to_advance} hours. HP and resources processed."
    )



__all__ = [
    "roll_generic_dice",
    "search_vault_by_tag",
    "advance_time",
    "refresh_vault_data",
    "report_rule_challenge",
    "perform_ability_check_or_save",
    "take_rest",
]
