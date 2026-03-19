# flake8: noqa: W293, E203
import os
import re
import yaml
import random
import math
import aiofiles
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from pydantic import Field
from typing import Optional, Annotated, Union
import uuid

# === DETERMINISTIC ENGINE INTEGRATION ===
from server.dnd_rules_engine import (
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
)
from state import ClassLevel, PCDetails, NPCDetails, LocationDetails, FactionDetails
from server.vault_io import (
    get_journals_dir,
    write_audit_log,
    upsert_journal_section,
    read_markdown_entity,
    edit_markdown_entity,
)
from server.compendium_manager import CompendiumManager, CompendiumEntry, MechanicEffect
from server.spatial_engine import spatial_service, LightSource, Wall, HAS_GIS
from server.spell_system import SpellDefinition, SpellMechanics, SpellCompendium
from server.item_system import WeaponItem, ArmorItem, WondrousItem, ItemCompendium

from server.registry import get_all_entities, register_entity, get_entity


class VaultCache:
    def __init__(self):
        self.bestiary_cache = {}  # vault_path -> [ (filename, content) ]
        self.chunk_cache = {}  # vault_path -> category -> [ (filename, chunk) ]
        self.indexed_vaults = set()

    def build_index(self, vault_path: str, force: bool = False):
        if vault_path in self.indexed_vaults and not force:
            return
        if force:
            self.indexed_vaults.discard(vault_path)

        self.bestiary_cache[vault_path] = []
        self.chunk_cache[vault_path] = {"rules": [], "modules": [], "bestiary": []}

        for cat in ["bestiary", "rules", "modules"]:
            dirs = _get_config_dirs(vault_path, cat)
            for d in dirs:
                for root, _, files in os.walk(d):
                    for file in files:
                        if file.endswith(".md"):
                            try:
                                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                                    content = f.read().replace("\r\n", "\n")
                                    if cat == "bestiary":
                                        self.bestiary_cache[vault_path].append((file, content))

                                    # Pre-chunk for keyword search
                                    body = content
                                    if body.startswith("---"):
                                        parts = body.split("---", 2)
                                        if len(parts) >= 3:
                                            body = parts[2]

                                    chunks = re.split(r"\n(?=#+ )", body)
                                    for chunk in chunks:
                                        if chunk.strip():
                                            self.chunk_cache[vault_path][cat].append((file, chunk.strip()))

                            except Exception:
                                pass
        self.indexed_vaults.add(vault_path)


_VAULT_CACHE = VaultCache()

_CHARACTER_AUTOMATIONS = {}


def update_roll_automations(character_name: str, automations: dict):
    _CHARACTER_AUTOMATIONS[character_name] = automations


def get_roll_automations(character_name: str) -> dict:
    defaults = {"hidden_rolls": True, "saving_throws": True, "skill_checks": True, "attack_rolls": True}
    return _CHARACTER_AUTOMATIONS.get(character_name, defaults)


async def _get_entity_by_name(name: str, vault_path: str) -> Optional[BaseGameEntity]:
    """Helper to find an active entity in the engine's memory by name, with JIT lazy loading."""
    from server.registry import _NAME_INDEX
    from server.vault_io import load_entity_into_engine, get_journals_dir

    name_lower = name.lower().strip()

    # 1. Check Memory (Fast Path)
    if vault_path in _NAME_INDEX and name_lower in _NAME_INDEX[vault_path]:
        return get_entity(_NAME_INDEX[vault_path][name_lower], vault_path)

    for uid, entity in get_all_entities(vault_path).items():
        ent_name_lower = entity.name.lower()
        if name_lower in ent_name_lower or ent_name_lower in name_lower:
            return entity

    # 2. Just-In-Time (JIT) Hydration (Lazy Load)
    j_dir = get_journals_dir(vault_path)
    exact_path = os.path.join(j_dir, f"{name}.md")
    if os.path.exists(exact_path):
        ent = await load_entity_into_engine(exact_path, vault_path)
        if ent:
            return ent

    if os.path.exists(j_dir):
        for filename in os.listdir(j_dir):
            if filename.endswith(".md"):
                file_base = filename[:-3].lower()
                if name_lower in file_base or file_base in name_lower:
                    ent = await load_entity_into_engine(os.path.join(j_dir, filename), vault_path)
                    if ent:
                        return ent

    return None


def _calculate_reach(entity: BaseGameEntity, is_active_turn: bool = False) -> float:
    """Calculates effective melee reach based on weapon and traits."""
    reach = getattr(entity, "base_reach", 5.0)
    tags = [t.lower() for t in getattr(entity, "tags", [])]

    # Check equipped weapon if available on sheet
    if hasattr(entity, "equipment"):
        main_hand = str(getattr(entity, "equipment", {}).get("main_hand", "")).lower()
        if any(w in main_hand for w in ["halberd", "glaive", "pike", "whip", "lance", "reach"]):
            reach += 5.0

    # Explicit weapon tags
    if any(w in tags for w in ["reach_weapon", "halberd", "glaive", "pike", "whip", "lance"]):
        reach += 5.0

    # Class features
    if any(f in tags for f in ["giant_stature", "path_of_the_giant"]):
        reach += 5.0

    # Species traits (Bugbear's Long-Limbed only applies on their own turn)
    if is_active_turn and ("bugbear" in tags or "long_limbed" in tags):
        reach += 5.0

    return reach


def _build_npc_template(title: str, context: str, details: dict, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> str:
    ctx = context.strip() if context else "Newly encountered individual. No prior background established."
    appearance = details.get("appearance", "")
    current_appearance = details.get("current_appearance", "")
    long_term_goals = details.get("long_term_goals", "")
    immediate_goals = details.get("immediate_goals", "")
    aliases = details.get("aliases_and_titles", "")
    base_attitude = details.get("base_attitude", "")
    dialect = details.get("dialect", "")
    mannerisms = details.get("mannerisms", "")
    connections = details.get("connections", "")
    stats = details.get("stat_block", "No stat block provided.")
    misc = details.get("misc_notes", "")
    code_switch = details.get("code_switching", "Unknown.")
    icon_url = details.get("icon_url", "")

    legendary_max = details.get("legendary_actions_max", 0)
    legendary_actions = details.get("legendary_actions", [])
    lair_actions = details.get("lair_actions", [])

    extra_actions_text = ""
    if legendary_max > 0 or legendary_actions:
        extra_actions_text += (
            f"\n### Legendary Actions ({legendary_max}/Round)\n" + "\n".join(f"- {a}" for a in legendary_actions) + "\n"
        )
    if lair_actions:
        extra_actions_text += "\n### Lair Actions (Initiative 20)\n" + "\n".join(f"- {a}" for a in lair_actions) + "\n"

    return (
        f"---\ntags: [npc]\nstatus: active\norigin: Unknown\ncurrent_location: Unknown\n"
        f'x: {x}\ny: {y}\nz: {z}\nicon_url: "{icon_url}"\n'
        f"legendary_actions_max: {legendary_max}\n"
        f"legendary_actions_current: {legendary_max}\n---\n"
        f"# {title}\n\n## Summary - Current State\n- {ctx[:150]}...\n\n"
        f"## Background & Motives\n- {ctx}\n- **Long-Term Goals**: {long_term_goals}\n"
        f"- **Aliases & Titles**: {aliases}\n\n"
        f"## Appearance\n- **Base Appearance**: {appearance}\n\n"
        f"## Communication Style\n- **Dialect/Accent**: {dialect}\n- **Mannerisms**: {mannerisms}\n"
        f"- **Code-Switching**: {code_switch}\n\n"
        f"## Connections\n- {connections}\n\n"
        f"## Attitude Tracker\n- **Base Attitude**: {base_attitude}\n| Entity | Disposition | Notes |\n"
        f"|---|---|---|\n| Party | Neutral | Initial encounter. |\n\n"
        f"## Active Logs\n- **Current Appearance**: {current_appearance}\n- **Immediate Goals**: {immediate_goals}\n\n"
        f"## Key Knowledge\n- \n\n## Voice & Quotes\n- \n\n## Combat & Stat Block\n{stats}\n{extra_actions_text}\n"
        f"## Additional Lore & Jazz\n{misc}\n"
    )


def _build_location_template(title: str, context: str, details: dict) -> str:
    ctx = context.strip() if context else "Newly discovered area."
    demographics = details.get("demographics", "")
    icon_url = details.get("icon_url", "")
    government = details.get("government", "")
    establishments = details.get("establishments", "")
    landmarks = details.get("key_features_and_landmarks", "")
    misc = details.get("misc_notes", "")
    diversity = details.get("diversity", "Unknown population makeup.")

    return (
        f'---\ntags: [location]\nicon_url: "{icon_url}"\n---\n# {title}\n\n## Summary - Current State\n- {ctx}\n\n'
        f"## Demographics & Culture\n- **Native Dialect(s)**: {demographics}\n- **Diversity**: {diversity}\n\n"
        f"## Government & Defenses\n- {government}\n\n"
        f"## Key Features & Landmarks\n- {landmarks}\n\n"
        f"## Notable Establishments (Shops/Taverns)\n- {establishments}\n\n"
        f"## Current Rumors & Events\n| Rumor | Source | Notes |\n|---|---|---|\n| | | |\n\n"
        f"## Condition & State\n- \n\n## Inhabitants\n- \n\n## Event History\n- \n\n## System Tables\n\n"
        f"## Additional Lore & Jazz\n{misc}\n"
    )


def _build_faction_template(title: str, context: str, details: dict) -> str:
    ctx = context.strip() if context else "Newly discovered faction."
    goals = details.get("goals", "")
    icon_url = details.get("icon_url", "")
    assets = details.get("assets", "")
    key_npcs = details.get("key_npcs", "")
    misc = details.get("misc_notes", "")

    return (
        f'---\ntags: [faction]\nstatus: active\nicon_url: "{icon_url}"\n---\n# {title}\n\n## Summary - Current State\n- {ctx}\n\n'
        f"## Goals\n- {goals}\n\n## Assets & Resources\n- {assets}\n\n## Key NPCs\n- {key_npcs}\n\n## Party Disposition\n- Neutral\n\n## Event History\n- \n\n"
        f"## Additional Lore & Jazz\n{misc}\n"
    )


def _build_pc_template(title: str, details: dict, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> str:
    appearance = details.get("appearance", "")
    current_appearance = details.get("current_appearance", "")
    long_term_goals = details.get("long_term_goals", "")
    icon_url = details.get("icon_url", "")
    immediate_goals = details.get("immediate_goals", "")
    aliases = details.get("aliases_and_titles", "")
    misc = details.get("misc_notes", "")

    s_str = details.get("str_score", 10)
    s_dex = details.get("dex_score", 10)
    s_con = details.get("con_score", 10)
    s_int = details.get("int_score", 10)
    s_wis = details.get("wis_score", 10)
    s_cha = details.get("cha_score", 10)

    species = details.get("species", "Unknown")
    background = details.get("background", "Unknown")
    classes = details.get("classes", [{"class_name": "Commoner", "level": 1}])
    profs = details.get("proficiencies", "None")
    feats = details.get("feats_and_traits", "None")

    return (
        f'---\ntags: [pc, player]\nstatus: active\nx: {x}\ny: {y}\nz: {z}\nicon_url: "{icon_url}"\n'
        f"classes: {yaml.dump(classes, default_flow_style=True)}\nspecies: {species}\nbackground: {background}\n"
        "level: 1\nmax_hp: 10\nac: 10\ngold: 0\ncurrency:\n  cp: 0\n  sp: 0\n  ep: 0\n  gp: 0\n  pp: 0\n"
        f"str: {s_str}\ndex: {s_dex}\ncon: {s_con}\nint: {s_int}\nwis: {s_wis}\ncha: {s_cha}\n"
        "attunement_slots: 0/3\nattuned_items: []\n"
        "attunement_slots: 0/3\nattuned_items: []\n"
        "equipment:\n"
        "  armor: Unarmored\n"
        "  shield: None\n"
        "  head: None\n"
        "  cloak: None\n"
        "  gloves: None\n"
        "  boots: None\n"
        "  ring1: None\n"
        "  ring2: None\n"
        "  amulet: None\n"
        "  main_hand: Unarmed\n"
        "  off_hand: None\n"
        'spell_save_dc: 10\nspell_atk: "+2"\nspell_slots: "None"\n'
        "resources: {}\nactive_mechanics: []\n"
        "inventory: []\n"
        "spells:\n  cantrips: []\n  level_1: []\n"
        "immunities: None\nresistances: None\n---\n"
        f"# {title}\n\n## Summary - Current State\n- Active party member.\n- **Aliases & Titles**: {aliases}\n\n"
        f"## Appearance\n- **Base Appearance**: {appearance}\n\n"
        f"## Goals\n- **Long-Term Goals**: {long_term_goals}\n\n"
        "## Status & Conditions\n- Current HP: 10\n- Active Conditions: None\n- Fatigue/Exhaustion: None\n\n"
        f"## Proficiencies & Feats\n- **Proficiencies**: {profs}\n- **Feats & Traits**: {feats}\n\n"
        f"## Active Logs\n- **Current Appearance**: {current_appearance}\n- **Immediate Goals**: {immediate_goals}\n\n"
        "## Event Log\n- \n\n"
        f"## Additional Lore & Jazz\n{misc}\n"
    )


def _build_party_tracker() -> str:
    return (
        "---\ntags: [system, ui]\n---\n# 🛡️ DM Party Dashboard\n\n"
        "```dataviewjs\n"
        "const p = dv.pages('#pc or #player or #party_npc');\n"
        "if (p.length > 0) {\n"
        "    let tableData = p.map(c => [\n"
        "        c.file.link,\n"
        "        `${c.max_hp || '?'}`,\n"
        "        c.ac || 10,\n"
        "        `10 + ${Math.floor(((c.wisdom || c.wis || 10) - 10) / 2)}`,\n"
        '        c.attunement_slots || "N/A",\n'
        "    ]);\n"
        '    dv.table(["Name", "Max HP", "AC", "Passive Perception", "Attunement"], tableData);\n'
        "} else {\n"
        '    dv.paragraph("No active party members found.");\n'
        "}\n"
        "```\n"
    )


async def _get_current_combat_initiative(vault_path: str) -> int:
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")
    if os.path.exists(file_path):
        try:
            async with read_markdown_entity(file_path) as (yaml_data, _):
                combatants = yaml_data.get("combatants", [])
                idx = yaml_data.get("current_turn_index", 0)
                if combatants and idx < len(combatants):
                    return int(combatants[idx].get("init", 0))
        except Exception:
            pass
    return 0


# ============================================


@tool
async def execute_melee_attack(
    attacker_name: str,
    target_name: str,
    advantage: bool = False,
    disadvantage: bool = False,
    is_reaction: bool = False,
    is_legendary_action: bool = False,
    is_opportunity_attack: bool = False,
    manual_roll_total: int = None,
    is_critical: bool = False,
    force_auto_roll: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    STRICT REQUIREMENT: Use this tool to resolve ANY melee attack between two entities.
    Do NOT hallucinate dice rolls or damage. The engine will calculate hit/miss and exact damage.
    Set is_reaction=True for Reactions or Readied Actions. Set is_opportunity_attack=True for Opportunity Attacks. Set is_legendary_action=True for Legendary Actions.
    """
    vault_path = config["configurable"].get("thread_id")
    attacker = await _get_entity_by_name(attacker_name, vault_path)
    target = await _get_entity_by_name(target_name, vault_path)

    if not attacker:
        return f"SYSTEM ERROR: Attacker '{attacker_name}' not found in active combat memory."
    if not target:
        return f"SYSTEM ERROR: Target '{target_name}' not found in active combat memory."

    # --- VERBAL COMMAND ENFORCEMENT FOR SUMMONS ---
    if getattr(attacker, "summoned_by_uuid", None) and getattr(attacker, "summon_spell", "").lower() != "find familiar":
        summoner = get_entity(attacker.summoned_by_uuid, vault_path)
        if summoner:
            summoner_conds = [c.name.lower() for c in getattr(summoner, "active_conditions", [])]
            attacker_conds = [c.name.lower() for c in getattr(attacker, "active_conditions", [])]
            reason = ""
            if "silenced" in summoner_conds:
                reason = f"its summoner ({summoner.name}) is Silenced"
            elif "confused" in summoner_conds:
                reason = f"its summoner ({summoner.name}) is Confused"
            elif "unconscious" in summoner_conds or "incapacitated" in summoner_conds:
                reason = f"its summoner ({summoner.name}) is Incapacitated"
            elif "deafened" in attacker_conds:
                reason = "it is Deafened and cannot hear verbal commands"

            if reason:
                return f"SYSTEM ERROR: {attacker.name} cannot attack because {reason}. Per 5.5e rules, without verbal commands it must take the Dodge action and use its move to avoid danger."

    # --- NEW RANGE VALIDATION ---
    dist = spatial_service.calculate_distance(attacker.x, attacker.y, attacker.z, target.x, target.y, target.z, vault_path)
    is_active_turn = not (is_reaction or is_opportunity_attack or is_legendary_action)
    base_reach = _calculate_reach(attacker, is_active_turn=is_active_turn)
    eff_reach = base_reach + max(0, (attacker.size - 5.0) / 2.0) + max(0, (target.size - 5.0) / 2.0)

    if dist > eff_reach:
        return (
            f"SYSTEM ERROR: Target '{target.name}' is out of range. "
            f"Distance is {dist:.1f}ft, but {attacker.name}'s effective reach is {eff_reach:.1f}ft."
        )

    is_pc = any(t in attacker.tags for t in ["pc", "player"])
    if is_pc and not force_auto_roll and manual_roll_total is None:
        auto_settings = get_roll_automations(attacker.name)
        if not auto_settings.get("attack_rolls", True):
            return (
                f"SYSTEM ALERT: {attacker.name} has manual attack rolls enabled. Ask the player to roll the attack "
                f"(including modifiers) and provide the total, OR ask if they want the engine to automate it."
            )

    if is_opportunity_attack:
        is_reaction = True

    if is_reaction:
        if getattr(attacker, "reaction_used", False):
            return f"SYSTEM ERROR: {attacker.name} has already used their reaction this round."
        attacker.reaction_used = True

    if is_legendary_action:
        if getattr(attacker, "legendary_actions_current", 0) <= 0:
            return f"SYSTEM ERROR: {attacker.name} has no Legendary Actions remaining."
        attacker.legendary_actions_current -= 1

    current_init = await _get_current_combat_initiative(config["configurable"].get("thread_id"))
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid,
        vault_path=vault_path,
        payload={
            "advantage": advantage,
            "disadvantage": disadvantage,
            "current_initiative": current_init,
            "is_opportunity_attack": is_opportunity_attack,
            "manual_roll_total": manual_roll_total,
            "is_critical": is_critical,
        },
    )

    result = EventBus.dispatch(event)

    base_msg = ""
    if result.payload.get("hit"):
        dmg = result.payload.get("damage", 0)
        base_msg = (
            f"MECHANICAL TRUTH: HIT! {attacker.name} dealt {dmg} damage to {target.name}. "
            f"{target.name} has {target.hp.base_value} HP remaining."
        )

        if is_opportunity_attack and "oa_halts_movement" in attacker.tags and isinstance(target, Creature):
            target.movement_remaining = 0
            base_msg += (
                f"\nSYSTEM ALERT: Because {attacker.name} hit with an Opportunity Attack and has a halting feat "
                f"(like Sentinel), {target.name}'s speed is reduced to 0! If they were moving, you MUST use "
                f"`move_entity` to immediately move them back to the square they were in when the attack triggered."
            )
    else:
        base_msg = f"MECHANICAL TRUTH: MISS! {attacker.name} rolled too low to beat {target.name}'s Armor Class."

    protectors = result.payload.get("protector_alerts", [])
    if protectors:
        base_msg += (
            f"\nSYSTEM ALERT: This attack provoked a Protector Reaction Attack (e.g. Sentinel) from: "
            f"{', '.join(protectors)}. Ask the player(s) if they want to use their reaction to attack {attacker.name}!"
        )

    if "results" in result.payload and result.payload["results"]:
        base_msg += "\n" + "\n".join(result.payload["results"])

    return base_msg


@tool
async def modify_health(
    target_name: str,
    hp_change: int,
    reason: str,
    damage_type: str = "untyped",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Use this tool to apply guaranteed damage (traps, falling, auto-hit spells) or healing (potions, healing spells).
    Provide a negative hp_change for damage, positive for healing.
    Specify damage_type (e.g., 'fire', 'bludgeoning', 'falling') so the engine can check resistances.
    """
    vault_path = config["configurable"].get("thread_id")
    target = await _get_entity_by_name(target_name, vault_path)
    if not target:
        return f"SYSTEM ERROR: Target '{target_name}' not found."

    if hp_change < 0:
        # Route damage through engine resistance checks natively
        dmg = abs(hp_change)
        dt = damage_type.lower()
        if dt in target.immunities:
            dmg = 0
        elif dt in target.vulnerabilities:
            dmg *= 2
        elif dt in target.resistances:
            dmg //= 2

        if dmg > 0 and target.wild_shape_hp > 0:
            if dmg >= target.wild_shape_hp:
                dmg -= target.wild_shape_hp
                target.wild_shape_hp = 0
                target.active_conditions = [c for c in target.active_conditions if c.name != "Wild Shape"]
            else:
                target.wild_shape_hp -= dmg
                dmg = 0

            if dmg > 0 and target.temp_hp > 0:
                if dmg >= target.temp_hp:
                    dmg -= target.temp_hp
                    target.temp_hp = 0
                else:
                    target.temp_hp -= dmg
                    dmg = 0

        hp_change = -dmg

    current_hp = target.hp.base_value
    target.hp.base_value += hp_change
    action = "healed for" if hp_change > 0 else "took"
    result_msg = (
        f"MECHANICAL TRUTH: {target.name} {action} {abs(hp_change)} {damage_type} HP from {reason}. "
        f"Current HP: {target.hp.base_value}."
    )

    if hp_change < 0:
        damage = abs(hp_change)
        if current_hp <= 0 and damage > 0:
            fails = 2 if reason.lower() == "critical" else 1
            target.death_saves_failures += fails
            target.hp.base_value = 0
            result_msg += f"\nSYSTEM ALERT: {target.name} took damage at 0 HP and suffered {fails} Death Save failure(s)!"
            if target.death_saves_failures >= 3:
                target.active_conditions = [c for c in target.active_conditions if c.name not in ["Dying", "Stable"]]
                if not any(c.name == "Dead" for c in target.active_conditions):
                    target.active_conditions.append(ActiveCondition(name="Dead"))
                result_msg += f"\nSYSTEM ALERT: {target.name} is DEAD."
        elif target.hp.base_value <= 0:
            if (current_hp - damage) <= -target.max_hp:
                target.hp.base_value = 0
                target.active_conditions = [c for c in target.active_conditions if c.name not in ["Dying", "Stable"]]
                if not any(c.name == "Dead" for c in target.active_conditions):
                    target.active_conditions.append(ActiveCondition(name="Dead"))
                result_msg += f"\nSYSTEM ALERT: {target.name} takes massive damage and is INSTANTLY KILLED!"
            else:
                target.hp.base_value = 0
                if not any(c.name == "Dying" for c in target.active_conditions):
                    target.active_conditions.append(ActiveCondition(name="Dying"))
                    target.active_conditions.append(ActiveCondition(name="Unconscious", source_name="0 HP"))
                result_msg += f"\nSYSTEM ALERT: {target.name} drops to 0 HP and is Dying/Unconscious."

        if target.concentrating_on:
            dc = max(10, abs(hp_change) // 2)
            result_msg += (
                f"\nSYSTEM ALERT: {target.name} took damage while concentrating on '{target.concentrating_on}'. "
                f"You MUST prompt a Constitution saving throw (DC {dc}). If they fail, use `drop_concentration`."
            )

        if target.hp.base_value <= 0 and target.concentrating_on:
            EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=target.entity_uuid))
            result_msg += (
                f"\nSYSTEM ALERT: {target.name} dropped to 0 HP and lost concentration " f"on '{target.concentrating_on}'."
            )

    if target.hp.base_value > 0:
        target.active_conditions = [
            c for c in target.active_conditions
            if c.name not in ["Dying", "Stable"] and not (c.name == "Unconscious" and c.source_name in ["0 HP", "Unknown"])
        ]
        target.death_saves_successes = 0
        target.death_saves_failures = 0
        if hp_change > 0 and current_hp <= 0:
            result_msg += f"\nSYSTEM ALERT: {target.name} is healed from 0 HP! They regain consciousness."

    return result_msg


@tool
async def create_new_entity(
    entity_name: str,
    entity_type: str,
    background_context: str = "",
    details: Union[PCDetails, NPCDetails, LocationDetails, FactionDetails] = None,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Generates schema-compliant Markdown files."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{entity_name}.md")

    if os.path.exists(file_path):
        return f"Error: '{entity_name}.md' already exists. Use flesh_out_entity to update it instead."
    display_title = (
        entity_name.replace("NPC_", "").replace("LOC_", "").replace("MIS_", "").replace("PC_", "").replace("_", " ")
    )

    if details is None:
        details_dict = {}
    else:
        details_dict = (
            details.model_dump()
            if hasattr(details, "model_dump")
            else (details.dict() if hasattr(details, "dict") else details)
        )

    e_type = entity_type.upper()
    try:
        if e_type == "NPC":
            content = _build_npc_template(display_title, background_context, details_dict, x, y, z)
        elif e_type == "LOCATION":
            content = _build_location_template(display_title, background_context, details_dict)
        elif e_type == "FACTION":
            content = _build_faction_template(display_title, background_context, details_dict)
        elif e_type == "MISSION":
            content = (
                f"---\ntags: [mission]\n---\n# {display_title}\n\n## Plot Summary\n- "
                f"{background_context or 'Newly acquired objective.'}\n\n## Objectives\n- [ ] \n\n"
                f"## Involved Entities\n- \n\n## Additional Lore & Jazz\n{details_dict.get('misc_notes', '')}\n"
            )
        elif e_type == "CAMPAIGN":
            content = (
                f"---\ntags: [campaign]\ncampaign_name: {display_title}\ncurrent_date: Day 1\n"
                f'in_game_time: "08:00"\n---\n# {display_title} - Master Ledger\n\n## The World State\n'
                f"- (Macro-level events, political climates, or looming threats taking place in the background.)\n\n"
                f"## Active Plotlines & Missions\n- \n\n## Alternate Routes & Consequences\n"
                f"- (Track 'Fail Forward' paths here. If a party fails to find a clue, "
                f"log the alternate NPC or method generated to keep the plot moving. "
                f"Log the consequences of past failures.)\n\n"
                f"## Major Milestones (Event Log)\n- \n\n"
                f"## Additional Lore & Jazz\n{details_dict.get('misc_notes', '')}\n"
            )
        elif e_type in ["PC", "PLAYER"]:
            content = _build_pc_template(display_title, details_dict, x, y, z)
            async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                await f.write(content)
            # AUTOMATED D&D BEYOND DATAVIEW SHEET
            sheet_content = f"""```dataviewjs
                const pcName = "{display_title}";
                const pc = dv.page(pcName);

                if (!pc) {{
                    dv.paragraph("Error: Could not find data file for " + pcName);
                }} else {{
                    const mod = (score) => {{
                        let m = Math.floor((score - 10) / 2);
                        return m >= 0 ? "+" + m : m;
                    }};

                    let spellsHtml = '';
                    if (pc.spells) {{
                        for (let level of Object.keys(pc.spells)) {{
                            let spellList = pc.spells[level];
                            if (spellList) {{
                                let title = level === 'cantrips' ? 'Cantrips' : level.replace('_', ' ').toUpperCase();
                                let spellsListStr = dv.isArray(spellList) ? spellList.join(', ') : spellList;
                                spellsHtml += `<li><strong>${{title}}:</strong> ${{spellsListStr}}</li>`;
                            }}
                        }}
                    }}

                    let resourcesHtml = '';
                    if (pc.resources) {{
                        for (let res of Object.keys(pc.resources)) {{
                            resourcesHtml += `<div class="ddb-vital-box" style="min-width: 80px;">` +
                                             `<div class="label">${{res}}</div>` +
                                             `<div class="value" style="font-size:1.5em; color:#242527;">` +
                                             `${{pc.resources[res]}}</div></div>`;
                        }}
                    }}

                    const html = `
                    <div class="ddb-sheet">
                        <div class="ddb-header">
                            <div class="ddb-char-name">${{pc.file.name}}</div>
                            <div class="ddb-char-info">
                                <span>${{pc.class || 'Unknown Class'}} ${{pc.level || 1}}</span> •
                                <span>${{pc.species || 'Unknown Species'}}</span> •
                                <span>${{pc.background || 'Unknown Background'}}</span>
                            </div>
                        </div>
                        <div class="ddb-abilities">
                            ${{['str', 'dex', 'con', 'int', 'wis', 'cha'].map(stat => `
                                <div class="ddb-ability">
                                    <div class="ddb-ability-name">${{stat.toUpperCase()}}</div>
                                    <div class="ddb-ability-score">${{pc[stat] || 10}}</div>
                                    <div class="ddb-ability-mod">${{mod(pc[stat] || 10)}}</div>
                                </div>
                            `).join('')}}
                        </div>
                        <div class="ddb-vitals">
                            <div class="ddb-vital-box">
                                <div class="label">Armor Class</div>
                                <div class="value">${{pc.ac || 10}}</div>
                            </div>
                            <div class="ddb-vital-box">
                                <div class="label">Hit Points</div>
                                <div class="value">${{pc.max_hp || 10}}</div>
                            </div>
                            <div class="ddb-vital-box">
                                <div class="label">Initiative</div>
                                <div class="value">${{mod(pc.dex || 10)}}</div>
                            </div>
                            <div class="ddb-vital-box">
                                <div class="label">Speed</div>
                                <div class="value">30ft</div>
                            </div>
                            ${{resourcesHtml}}
                        </div>
                        
                        ${{pc.active_mechanics && pc.active_mechanics.length > 0 ? `
                        <div class="ddb-inventory-section">
                            <h3>Active Mechanics (Feats & Items)</h3>
                            <ul class="ddb-inventory-list">
                                ${{pc.active_mechanics.map(m => `<li>${{m}}</li>`).join('')}}
                            </ul>
                        </div>` : ''}}

                        <div class="ddb-inventory-section" style="margin-top:20px;">
                            <h3>Equipment & Inventory</h3>
                            <ul class="ddb-inventory-list">
                                ${{(pc.inventory || []).map(i => `<li>${{i}}</li>`).join('')}}
                            </ul>
                        </div>
                        
                        ${{spellsHtml ? `
                        <div class="ddb-spells-section">
                            <h3>Spells & Magic</h3>
                            <div class="ddb-spell-stats">
                                ${{pc.spell_slots && pc.spell_slots !== 'None' ? `<div class="ddb-spell-stat-pill"><strong>Slots:</strong> ${{pc.spell_slots}}</div>` : ''}}  /* noqa: E501 */
                                ${{pc.spell_save_dc ? `<div class="ddb-spell-stat-pill"><strong>Save DC:</strong> ` + `${{pc.spell_save_dc}}</div>` : ''}}  /* noqa: E501 */
                                ${{pc.spell_atk ? `<div class="ddb-spell-stat-pill"><strong>Spell Atk:</strong> ` + `${{pc.spell_atk}}</div>` : ''}}  /* noqa: E501 */
                            </div>
                            <ul class="ddb-spell-list">
                                ${{spellsHtml}}
                            </ul>
                        </div>` : ''}}
                        
                    </div>
                    `;

                    dv.container.innerHTML = html;
                }}
                ```"""
            sheet_path = os.path.join(get_journals_dir(vault_path), f"{display_title} - Character Sheet.md")
            try:
                async with aiofiles.open(sheet_path, "w", encoding="utf-8") as f:
                    await f.write(sheet_content)
            except Exception:
                pass
            return f"Success: Instantiated new log '{entity_name}.md' and UI View."
        elif e_type == "PARTY_TRACKER":
            content = _build_party_tracker()
        else:
            content = (
                f"---\ntags: [misc]\n---\n# {display_title}\n\n{background_context}\n\n"
                f"## Additional Lore & Jazz\n{details_dict.get('misc_notes', '')}\n"
            )

        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(content)
        return f"Success: Created {e_type} '{entity_name}.md' with context."

    except Exception as e:
        return f"Error creating file: {str(e)}"


@tool
async def flesh_out_entity(
    entity_name: str,
    entity_type: str,
    background_context: str = "",
    details: Union[PCDetails, NPCDetails, LocationDetails, FactionDetails] = None,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Use this tool to completely rewrite and 'Flesh Out' an existing file."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{entity_name}.md")
    display_title = entity_name.replace("NPC_", "").replace("LOC_", "")
    display_title = display_title.replace("MIS_", "").replace("PC_", "")
    display_title = display_title.replace("_", " ")

    if details is None:
        details_dict = {}
    else:
        details_dict = (
            details.model_dump()
            if hasattr(details, "model_dump")
            else (details.dict() if hasattr(details, "dict") else details)
        )

    e_type = entity_type.upper()
    try:
        if e_type == "NPC":
            content = _build_npc_template(display_title, background_context, details_dict, x, y, z)
        elif e_type == "LOCATION":
            content = _build_location_template(display_title, background_context, details_dict)
        elif e_type == "FACTION":
            content = _build_faction_template(display_title, background_context, details_dict)
        elif e_type in ["PC", "PLAYER"]:
            content = _build_pc_template(display_title, details_dict, x, y, z)
        else:
            return f"Error: Unsupported entity type for flesh_out_entity: {e_type}"

        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(content)
        return f"Success: Fleshed out and entirely updated {e_type} '{entity_name}.md' with deep context and jazz."

    except Exception as e:
        return f"Error updating file: {str(e)}"


@tool
async def update_yaml_frontmatter(
    entity_name: str, updates: dict, *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """Safely parses a Markdown file, updates specific YAML frontmatter keys, and reconstructs the file."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{entity_name}.md")

    try:
        async with edit_markdown_entity(file_path) as state:
            for key, value in updates.items():
                state["yaml_data"][key] = value
    except Exception as e:
        return str(e)

    return f"Success: Updated stats for {entity_name}."


@tool
async def fetch_entity_context(
    entity_names: list[str], full_read: bool = False, *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """Retrieves the YAML metadata and the 'Summary' section for given entities to gain context."""
    vault_path = config["configurable"].get("thread_id")
    j_dir = get_journals_dir(vault_path)
    context_blocks = []

    for name in entity_names:
        file_path = os.path.join(j_dir, f"{name}.md")
        if not os.path.exists(file_path):
            context_blocks.append(f"[{name}]: Entity not found in archives.")
            continue

        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()

        if full_read:
            context_blocks.append(f"=== {name} (FULL) ===\n{content}")
            continue

        yaml_data, body_text = "", content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                yaml_data, body_text = parts[1].strip(), parts[2].strip()

        summary_match = re.search(r"(## Summary - Current State\n.*?)(?=\n## |\Z)", body_text, re.DOTALL)
        summary_text = summary_match.group(1).strip() if summary_match else "No summary available."
        context_blocks.append(f"=== {name} (CACHED STATE) ===\nMetadata:\n{yaml_data}\n\n{summary_text}\n")

    return "\n\n".join(context_blocks)


@tool
async def equip_item(  # noqa: C901
    character_name: str,
    item_name: str,
    item_slot: str,
    attune: bool = False,
    new_ac_value: int = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Equips an item to a specific slot for a character, updating their YAML file.
    If a slot is already occupied, it will return an error. You must un-equip the old item first by equipping 'None'.
    If 'attune' is True, it will also consume an attunement slot and apply magical modifiers natively.
    The AI is responsible for moving the old item back to inventory using 'manage_inventory' if needed.

    Valid item_slot values:
    - 'armor', 'shield', 'head', 'cloak', 'gloves', 'boots', 'amulet', 'main_hand', 'off_hand'
    - 'ring': Automatically finds an available ring slot (ring1 or ring2).
    - 'ring1' or 'ring2': To target a specific ring slot.
    """
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    # Map general types to specific slots
    slot_map = {
        "weapon": "main_hand",
        "helmet": "head",
        "hat": "head",
        "hood": "head",
        "tiara": "head",
        "necklace": "amulet",
        "bracers": "gloves",
    }
    target_slot = slot_map.get(item_slot.lower(), item_slot)

    item = await ItemCompendium.load_item(vault_path, item_name)
    old_item = None
    old_item_name = None

    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            equipment = yaml_data.get("equipment", {})
            if not isinstance(equipment, dict):
                state["save"] = False
                return f"Error: '{character_name}.md' does not have a valid 'equipment' block."

            final_slot = None
            if target_slot == "ring":
                if str(equipment.get("ring1", "None")) in ["None", ""]:
                    final_slot = "ring1"
                elif str(equipment.get("ring2", "None")) in ["None", ""]:
                    final_slot = "ring2"
                else:
                    state["save"] = False
                    return "Error: Both ring slots are already occupied. You must specify 'ring1' or 'ring2' to overwrite one."
            elif target_slot in equipment:
                final_slot = target_slot
            else:
                valid_slots = list(equipment.keys())
                state["save"] = False
                return f"Error: Invalid equipment slot '{item_slot}'. Valid slots are: {', '.join(valid_slots)} or 'ring'."

            old_item_name = equipment.get(final_slot, "None")

            if str(old_item_name).strip() not in ["None", "", "Unarmed"] and str(item_name).strip() not in ["None", ""]:
                if target_slot != "ring":
                    state["save"] = False
                    return (
                        f"Error: The {final_slot} slot is already occupied by '{old_item_name}'. "
                        f"You must un-equip it first (equip 'None') before equipping '{item_name}'."
                    )
            equipment[final_slot] = item_name if not item else item.name

            attuned_items = yaml_data.get("attuned_items", [])
            if not isinstance(attuned_items, list):
                attuned_items = []

            if attune and item and getattr(item, "requires_attunement", False):
                for tag in item.tags:
                    if tag.startswith("requires_attunement_by_"):
                        req = tag.replace("requires_attunement_by_", "").lower()
                        classes = (
                            [c.get("class_name", "").lower() for c in yaml_data.get("classes", [])]
                            if isinstance(yaml_data.get("classes", []), list)
                            else []
                        )
                        species = str(yaml_data.get("species", "")).lower()
                        alignment = str(yaml_data.get("alignment", "")).lower()
                        if req not in classes and req not in species and req not in alignment:
                            state["save"] = False
                            return (
                                f"SYSTEM ERROR: {character_name} does not meet the "
                                f"attunement requirements ({req}) for '{item.name}'."
                            )

                if len(attuned_items) >= 3 and item.name not in attuned_items:
                    state["save"] = False
                    return "SYSTEM ERROR: Character is already attuned to 3 items. Unattune something first."
                if item.name not in attuned_items:
                    attuned_items.append(item.name)
                    yaml_data["attuned_items"] = attuned_items
                    yaml_data["attunement_slots"] = f"{len(attuned_items)}/3"

            if new_ac_value is not None:
                yaml_data["ac"] = new_ac_value
            elif item and isinstance(item, ArmorItem) and target_slot == "armor":
                pc_dex = yaml_data.get("dexterity", yaml_data.get("dex", 10))
                dex_mod = math.floor((int(pc_dex) - 10) / 2)

                max_dex = item.max_dex_bonus
                if max_dex is None:
                    if item.armor_category.lower() == "medium":
                        max_dex = 2
                    elif item.armor_category.lower() == "heavy":
                        max_dex = 0

                if item.armor_category.lower() == "heavy":
                    allowed_dex = 0
                else:
                    allowed_dex = min(dex_mod, max_dex) if max_dex is not None and dex_mod > 0 else dex_mod

                yaml_data["ac"] = item.base_ac + item.plus_ac_bonus + allowed_dex
                new_ac_value = yaml_data["ac"]
    except Exception as e:
        return str(e)

    if old_item_name and old_item_name != "None":
        old_item = await ItemCompendium.load_item(vault_path, old_item_name)

    # Sync OO Engine state dynamically if the entity is active in memory
    engine_creature = await _get_entity_by_name(character_name, vault_path)
    if engine_creature and isinstance(engine_creature, Creature):
        if target_slot == "main_hand":
            dmg_dice = "1d4" if "Unarmed" in item_name else "1d8"
            dmg_type = "bludgeoning" if "Unarmed" in item_name else "slashing"
            new_weapon = MeleeWeapon(name=item_name, damage_dice=dmg_dice, damage_type=dmg_type, vault_path=vault_path)
            if item and isinstance(item, WeaponItem):
                new_weapon = MeleeWeapon(
                    name=item.name, damage_dice=item.damage_dice, damage_type=item.damage_type, vault_path=vault_path
                )
                if hasattr(item, "magic_bonus"):
                    new_weapon.magic_bonus = item.magic_bonus
                if hasattr(item, "mastery_name") and item.mastery_name:
                    new_weapon.mastery_name = item.mastery_name
                    if "weapon_mastery" in engine_creature.tags:
                        mastery_entry = await CompendiumManager.get_entry(vault_path, item.mastery_name)
                        if mastery_entry and mastery_entry.mechanics:
                            dumped = mastery_entry.mechanics.model_dump()
                            if mastery_entry.mechanics.trigger_event == "on_hit":
                                new_weapon.on_hit_mechanics = dumped
                            elif mastery_entry.mechanics.trigger_event == "on_miss":
                                new_weapon.on_miss_mechanics = dumped
            else:
                dmg_dice = "1d4" if "Unarmed" in item_name else "1d8"
                dmg_type = "bludgeoning" if "Unarmed" in item_name else "slashing"
                new_weapon = MeleeWeapon(name=item_name, damage_dice=dmg_dice, damage_type=dmg_type, vault_path=vault_path)
            register_entity(new_weapon)
            engine_creature.equipped_weapon_uuid = new_weapon.entity_uuid
        if new_ac_value is not None:
            engine_creature.ac.base_value = new_ac_value

        if old_item and (not getattr(old_item, "requires_attunement", False) or old_item.name in attuned_items):
            for mod in getattr(old_item, "modifiers", []):
                if hasattr(engine_creature, mod.stat):
                    stat_obj = getattr(engine_creature, mod.stat)
                    if isinstance(stat_obj, ModifiableValue):
                        to_remove = [m for m in stat_obj.modifiers if m.source_name == old_item.name]
                        for m in to_remove:
                            stat_obj.remove_modifier(m.mod_uuid)
                        if old_item.name in engine_creature.active_mechanics:
                            engine_creature.active_mechanics.remove(old_item.name)

        if item:
            if not getattr(item, "requires_attunement", False) or attune or item.name in attuned_items:
                for mod in getattr(item, "modifiers", []):
                    if hasattr(engine_creature, mod.stat):
                        stat_obj = getattr(engine_creature, mod.stat)
                        if isinstance(stat_obj, ModifiableValue):
                            priority = ModifierPriority.OVERRIDE if mod.value >= 10 else ModifierPriority.ADDITIVE
                            stat_obj.add_modifier(
                                NumericalModifier(
                                    priority=priority, value=mod.value, source_name=item.name, duration_seconds=-1
                                )
                            )
                            if item.name not in engine_creature.active_mechanics:
                                engine_creature.active_mechanics.append(item.name)

    ac_msg = f". Their AC is now {new_ac_value}" if new_ac_value is not None else ""
    attune_msg = " and attuned to it" if attune else ""
    return_item_name = item_name if not item else item.name
    return f"Success: {character_name} equipped {return_item_name} in the {final_slot} slot{attune_msg}{ac_msg}."


@tool
async def attune_item(  # noqa: C901
    character_name: str, item_name: str, action: str = "attune", *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """
    Attunes or unattunes a magical item to a character.
    Valid actions: 'attune', 'unattune'.
    Automatically consumes an attunement slot and applies the item's passive modifiers to the native engine.
    """
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    item = await ItemCompendium.load_item(vault_path, item_name)
    if not item:
        return f"SYSTEM ERROR: Item '{item_name}' not found in Compendium."

    if not getattr(item, "requires_attunement", False) and action.lower() == "attune":
        return f"MECHANICAL TRUTH: '{item.name}' does not require attunement. You can just equip it."

    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            attuned_items = yaml_data.get("attuned_items", [])
            if not isinstance(attuned_items, list):
                attuned_items = []

            max_slots = 3

            if action.lower() == "attune":
                for tag in item.tags:
                    if tag.startswith("requires_attunement_by_"):
                        req = tag.replace("requires_attunement_by_", "").lower()
                        classes = (
                            [c.get("class_name", "").lower() for c in yaml_data.get("classes", [])]
                            if isinstance(yaml_data.get("classes", []), list)
                            else []
                        )
                        species = str(yaml_data.get("species", "")).lower()
                        alignment = str(yaml_data.get("alignment", "")).lower()
                        if req not in classes and req not in species and req not in alignment:
                            state["save"] = False
                            return (
                                f"SYSTEM ERROR: {character_name} does not meet the "
                                f"attunement requirements ({req}) for '{item.name}'."
                            )

                if len(attuned_items) >= max_slots:
                    state["save"] = False
                    return f"SYSTEM ERROR: {character_name} is already attuned to {max_slots} items."
                if item.name not in attuned_items:
                    attuned_items.append(item.name)
            elif action.lower() == "unattune":
                if item.name in attuned_items:
                    attuned_items.remove(item.name)
                else:
                    state["save"] = False
                    return f"SYSTEM ERROR: {character_name} is not attuned to '{item.name}'."

            yaml_data["attuned_items"] = attuned_items
            yaml_data["attunement_slots"] = f"{len(attuned_items)}/{max_slots}"
    except Exception as e:
        return str(e)

    engine_creature = await _get_entity_by_name(character_name, vault_path)
    if engine_creature and isinstance(engine_creature, Creature):
        for mod in getattr(item, "modifiers", []):
            if hasattr(engine_creature, mod.stat):
                stat_obj = getattr(engine_creature, mod.stat)
                if isinstance(stat_obj, ModifiableValue):
                    to_remove = [m for m in stat_obj.modifiers if m.source_name == item.name]
                    for m in to_remove:
                        stat_obj.remove_modifier(m.mod_uuid)

                    if action.lower() == "attune":
                        priority = ModifierPriority.OVERRIDE if mod.value >= 10 else ModifierPriority.ADDITIVE
                        stat_obj.add_modifier(
                            NumericalModifier(priority=priority, value=mod.value, source_name=item.name, duration_seconds=-1)
                        )
                        if item.name not in engine_creature.active_mechanics:
                            engine_creature.active_mechanics.append(item.name)
                    else:
                        if item.name in engine_creature.active_mechanics:
                            engine_creature.active_mechanics.remove(item.name)

    if action.lower() == "attune":
        return (
            f"Success: {character_name} attuned to {item.name}. "
            f"Active modifiers applied. Slots used: {len(attuned_items)}/{max_slots}."
        )
    else:
        return (
            f"Success: {character_name} unattuned from {item.name}. "
            f"Active modifiers removed. Slots used: {len(attuned_items)}/{max_slots}."
        )


@tool
async def use_expendable_resource(
    character_name: str, resource_name: str, amount_to_deduct: int = 1, *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """Deducts a use of a class feature, spell slot, or item charge (e.g. 'Second Wind', '1st Level Spell', 'Lucky')."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    log_message = ""

    # 1. ACQUIRE LOCK: Read and Write the YAML
    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            resources = yaml_data.get("resources", {})
            if not isinstance(resources, dict):
                resources = {}

            target_key = next((k for k in resources.keys() if resource_name.lower() in k.lower()), None)

            if not target_key:
                state["save"] = False
                avail = list(resources.keys())
                return f"Error: Resource '{resource_name}' not found on {character_name}'s sheet. Available: {avail}"

            val_str = str(resources[target_key])
            match = re.match(r"(\d+)\s*/\s*(\d+)", val_str)
            if match:
                current_val = int(match.group(1))
                max_val = int(match.group(2))
                new_val = max(0, current_val - amount_to_deduct)
                resources[target_key] = f"{new_val}/{max_val}"

                log_message = f"- Used {amount_to_deduct}x {target_key}. ({new_val}/{max_val} remaining)."
            else:
                state["save"] = False
                return f"Error: Resource '{target_key}' has invalid format '{val_str}'. Expected 'current/max'."
    except Exception as e:
        return str(e)

    # 2. LOCK RELEASED: Safely call the next tool
    # Sync OO Engine state dynamically if the entity is active in memory
    engine_creature = await _get_entity_by_name(character_name, vault_path)
    if engine_creature and isinstance(engine_creature, Creature) and log_message:
        engine_creature.resources[target_key] = f"{new_val}/{max_val}"

    if log_message:
        await upsert_journal_section.ainvoke(
            {"entity_name": character_name, "section_header": "Event Log", "content": log_message, "mode": "append"}, config
        )
        return f"Success: Deducted {amount_to_deduct} from {target_key}. They now have {new_val}/{max_val} remaining."


@tool
async def use_font_of_magic(
    character_name: str,
    action: str,
    slot_level: int = 1,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Sorcerer feature: Convert Sorcery Points to a Spell Slot ('create_slot')
    or a Spell Slot to Sorcery Points ('convert_slot').
    """
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    cost_map = {1: 2, 2: 3, 3: 5, 4: 6, 5: 7}
    if slot_level not in cost_map and action == "create_slot":
        return "SYSTEM ERROR: Can only create spell slots of 1st through 5th level."
        
    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            resources = yaml_data.get("resources", {})
            
            sp_key = next((k for k in resources.keys() if "sorcery point" in k.lower()), None)
            slot_key = next((k for k in resources.keys() if f"level {slot_level}" in k.lower() or f"{slot_level}st level" in k.lower() or f"{slot_level}nd level" in k.lower() or f"{slot_level}rd level" in k.lower() or f"{slot_level}th level" in k.lower()), None)

            if not sp_key:
                state["save"] = False
                return f"SYSTEM ERROR: No Sorcery Points found on {character_name}."
                
            sp_match = re.match(r"(\d+)\s*/\s*(\d+)", str(resources[sp_key]))
            sp_cur, sp_max = int(sp_match.group(1)), int(sp_match.group(2))
            
            if not slot_key:
                slot_key = f"Level {slot_level} Spell Slots"
                resources[slot_key] = "0/0"
                
            slot_match = re.match(r"(\d+)\s*/\s*(\d+)", str(resources[slot_key]))
            slot_cur, slot_max = int(slot_match.group(1)), int(slot_match.group(2))
            
            if action == "create_slot":
                cost = cost_map[slot_level]
                if sp_cur < cost:
                    state["save"] = False
                    return f"SYSTEM ERROR: Not enough Sorcery Points ({sp_cur}/{sp_max}) to create a Level {slot_level} slot (costs {cost})."
                sp_cur -= cost
                slot_cur += 1
                resources[sp_key] = f"{sp_cur}/{sp_max}"
                resources[slot_key] = f"{slot_cur}/{max(slot_cur, slot_max)}"
                log = f"Spent {cost} Sorcery Points to create a Level {slot_level} Spell Slot."
            
            elif action == "convert_slot":
                if slot_cur < 1:
                    state["save"] = False
                    return f"SYSTEM ERROR: No Level {slot_level} Spell Slots available to convert."
                slot_cur -= 1
                sp_cur = min(sp_max, sp_cur + slot_level)
                resources[sp_key] = f"{sp_cur}/{sp_max}"
                resources[slot_key] = f"{slot_cur}/{slot_max}"
                log = f"Converted a Level {slot_level} Spell Slot into {slot_level} Sorcery Points."
            else:
                state["save"] = False
                return "SYSTEM ERROR: Invalid action. Use 'create_slot' or 'convert_slot'."
    except Exception as e:
        return str(e)

    engine_creature = await _get_entity_by_name(character_name, vault_path)
    if engine_creature and isinstance(engine_creature, Creature):
        engine_creature.resources[sp_key] = f"{sp_cur}/{sp_max}"
        engine_creature.resources[slot_key] = f"{slot_cur}/{max(slot_cur, slot_max)}"

    await upsert_journal_section.ainvoke(
        {"entity_name": character_name, "section_header": "Event Log", "content": f"- {log}", "mode": "append"}, config
    )
    return f"MECHANICAL TRUTH: {character_name} {log} (SP: {sp_cur}/{sp_max}, Lvl {slot_level} Slots: {slot_cur}/{max(slot_cur, slot_max)})"


@tool
async def update_character_status(
    character_name: str,
    hp: str,
    resources: str,
    conditions: str,
    fatigue: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Overwrites the 'Status & Conditions' section in a character's markdown log."""
    content = (
        f"- Current HP: {hp}\n- Expendable Resources: {resources}\n"
        f"- Active Conditions: {conditions}\n- Fatigue/Exhaustion: {fatigue}"
    )
    # PASSING CONFIG DOWN THE CHAIN
    return await upsert_journal_section.ainvoke(
        {"entity_name": character_name, "section_header": "Status & Conditions", "content": content, "mode": "replace"}, config
    )


@tool
async def level_up_character(
    character_name: str, class_name: str, hp_increase: int, *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """Updates a character's level in a specific class, increases their Max HP, and applies new features from the compendium."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    if not os.path.exists(file_path):
        return f"Error: Could not locate '{character_name}.md'."

    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            pc_details = PCDetails(**yaml_data)

            class_to_level_up = None
            for c in pc_details.classes:
                if c.class_name.lower() == class_name.lower():
                    class_to_level_up = c
                    break

            if not class_to_level_up:

                def meets_req(c_name, stats):
                    MULTICLASS_REQS = {
                        "barbarian": [("strength", 13)],
                        "bard": [("charisma", 13)],
                        "cleric": [("wisdom", 13)],
                        "druid": [("wisdom", 13)],
                        "fighter": [("strength", 13), ("dexterity", 13)],
                        "monk": [("dexterity", 13), ("wisdom", 13)],
                        "paladin": [("strength", 13), ("charisma", 13)],
                        "ranger": [("dexterity", 13), ("wisdom", 13)],
                        "rogue": [("dexterity", 13)],
                        "sorcerer": [("charisma", 13)],
                        "warlock": [("charisma", 13)],
                        "wizard": [("intelligence", 13)],
                    }
                    reqs = MULTICLASS_REQS.get(c_name.lower())
                    if not reqs:
                        return True
                    if c_name.lower() == "fighter":
                        return stats.get("strength", 10) >= 13 or stats.get("dexterity", 10) >= 13
                    for stat_name, min_val in reqs:
                        if stats.get(stat_name, 10) < min_val:
                            return False
                    return True

                stats_dict = {
                    "strength": pc_details.strength,
                    "dexterity": pc_details.dexterity,
                    "constitution": pc_details.constitution,
                    "intelligence": pc_details.intelligence,
                    "wisdom": pc_details.wisdom,
                    "charisma": pc_details.charisma,
                }
                for existing_c in pc_details.classes:
                    if not meets_req(existing_c.class_name, stats_dict):
                        state["save"] = False
                        return f"SYSTEM ERROR: Cannot multiclass. {character_name} does not meet the minimum stat requirements for their current class '{existing_c.class_name}'."
                if not meets_req(class_name, stats_dict):
                    state["save"] = False
                    return f"SYSTEM ERROR: Cannot multiclass. {character_name} does not meet the minimum stat requirements for the target class '{class_name}'."
                class_to_level_up = ClassLevel(class_name=class_name, level=0)
                pc_details.classes.append(class_to_level_up)

            class_to_level_up.level += 1
            new_level = class_to_level_up.level

            new_max_hp = pc_details.hp + hp_increase
            pc_details.hp = new_max_hp

            creature = await _get_entity_by_name(character_name, vault_path)
            if not creature or not isinstance(creature, Creature):
                state["save"] = False
                return f"Error: Creature '{character_name}' not found in the deterministic engine."

            creature.max_hp = new_max_hp
            creature.hp.base_value += hp_increase

            found_in_creature = False
            for c_in_c in creature.classes:
                if c_in_c.class_name.lower() == class_name.lower():
                    c_in_c.level = new_level
                    found_in_creature = True
            if not found_in_creature:
                creature.classes.append(ClassLevel(class_name=class_name, level=new_level))

            class_def = await CompendiumManager.get_class_definition(vault_path, class_to_level_up.class_name)
            if class_def:
                creature.apply_features(class_def, new_level)

            if class_to_level_up.subclass_name:
                subclass_def = await CompendiumManager.get_subclass_definition(vault_path, class_to_level_up.subclass_name)
                if subclass_def:
                    creature.apply_subclass_features(subclass_def, new_level)

            yaml_data.update(
                {
                    "level": pc_details.character_level,
                    "max_hp": new_max_hp,
                    "classes": [c.model_dump() for c in pc_details.classes],
                }
            )
    except Exception as e:
        return str(e)

    new_features = [f.name for f in creature.features if f.level == new_level]
    if new_features:
        feature_bullets = "\n".join([f"- **Level {new_level} ({class_name})**: {feat}" for feat in new_features])
        await upsert_journal_section.ainvoke(
            {"entity_name": character_name, "section_header": "Event Log", "content": feature_bullets, "mode": "append"},
            config,
        )

    return f"Success: {character_name} leveled up to level {new_level} {class_name}. Max HP is now {new_max_hp}."


@tool
async def manage_inventory(  # noqa: C901
    character_name: str,
    item_name: str,
    action: str,
    quantity: int = 1,
    gold_change: int = 0,
    cp_change: int = 0,
    sp_change: int = 0,
    ep_change: int = 0,
    gp_change: int = 0,
    pp_change: int = 0,
    context_log: str = "",
    metadata: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Adds or removes an item and/or currency from a character's YAML inventory.
    Handles quantity stacking (e.g. 'Torch (x5)') and partial removals.
    It automatically exchanges currencies if spending exceeds the specific denomination.
    """
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]

            # Handle Currency
            if "currency" in yaml_data and isinstance(yaml_data["currency"], dict):
                curr = yaml_data["currency"]
                cp, sp, ep, gp, pp = (
                    int(curr.get("cp", 0)),
                    int(curr.get("sp", 0)),
                    int(curr.get("ep", 0)),
                    int(curr.get("gp", 0)),
                    int(curr.get("pp", 0)),
                )
            else:
                cp, sp, ep, gp, pp = 0, 0, 0, int(yaml_data.get("gold", 0)), 0

            gp += gold_change + gp_change
            cp += cp_change
            sp += sp_change
            ep += ep_change
            pp += pp_change

            total_copper = cp + (sp * 10) + (ep * 50) + (gp * 100) + (pp * 1000)
            if total_copper < 0:
                state["save"] = False
                return "Transaction Failed: Not enough currency to cover the cost."

            if cp < 0 or sp < 0 or ep < 0 or gp < 0:
                pp = total_copper // 1000
                rem = total_copper % 1000
                gp = rem // 100
                rem %= 100
                ep = rem // 50
                rem %= 50
                sp = rem // 10
                cp = rem % 10

            yaml_data["currency"] = {"cp": cp, "sp": sp, "ep": ep, "gp": gp, "pp": pp}
            if "gold" in yaml_data or "gold" not in yaml_data:
                yaml_data["gold"] = gp

            inventory = yaml_data.get("inventory", [])
            if not isinstance(inventory, list):
                inventory = []

            def parse_item(i_str):
                qty, meta = 1, ""
                name_part = str(i_str)
                meta_match = re.search(r"\[(.*?)\]", name_part)
                if meta_match:
                    meta = meta_match.group(1)
                    name_part = name_part.replace(f"[{meta}]", "").strip()
                qty_match = re.search(r"\(\s*x(\d+)\s*\)", name_part)
                if qty_match:
                    qty = int(qty_match.group(1))
                    name_part = name_part.replace(f"(x{qty_match.group(1)})", "").strip()
                return name_part.strip(), qty, meta

            if action.lower() == "add" and item_name:
                found = False
                for i, item_str in enumerate(inventory):
                    i_name, i_qty, i_meta = parse_item(item_str)
                    if i_name.lower() == item_name.lower() and i_meta == metadata:
                        new_qty = i_qty + quantity
                        new_str = f"{i_name} (x{new_qty})"
                        if metadata:
                            new_str += f" [{metadata}]"
                        inventory[i] = new_str
                        found = True
                        break
                if not found:
                    item_str = f"{item_name}"
                    if quantity > 1:
                        item_str += f" (x{quantity})"
                    if metadata:
                        item_str += f" [{metadata}]"
                    inventory.append(item_str)

            elif action.lower() == "remove" and item_name:
                item_index = -1
                for i, item_str in enumerate(inventory):
                    i_name, _, _ = parse_item(item_str)
                    if item_name.lower() in i_name.lower():
                        item_index = i
                        break

                if item_index != -1:
                    i_name, i_qty, i_meta = parse_item(inventory[item_index])
                    if i_qty > quantity:
                        new_qty = i_qty - quantity
                        new_str = f"{i_name}"
                        if new_qty > 1:
                            new_str += f" (x{new_qty})"
                        if i_meta:
                            new_str += f" [{i_meta}]"
                        inventory[item_index] = new_str
                    else:
                        inventory.pop(item_index)
                else:
                    state["save"] = False
                    return f"Error: '{item_name}' not found in inventory."

            yaml_data["inventory"] = inventory
    except Exception as e:
        return str(e)

    if context_log:
        await upsert_journal_section.ainvoke(
            {
                "entity_name": character_name,
                "section_header": "Event Log",
                "content": f"- **Inventory**: {context_log}",
                "mode": "append",
            },
            config,
        )

    curr_str = f"{gp}gp"
    if pp > 0 or ep > 0 or sp > 0 or cp > 0:
        curr_str = f"{pp}pp, {gp}gp, {ep}ep, {sp}sp, {cp}cp"
    return f"Success. Currency is now {curr_str}. Event logged."


@tool
async def perform_ability_check_or_save(  # noqa: C901
    character_name: str,
    skill_or_stat_name: str,
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

    # Query Environmental Lighting to alert the DM for Stealth and Perception checks
    illum_alert = ""
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
            elif is_blinded and not has_blindsight:
                illum_alert += (
                    "\nSYSTEM ALERT: Character is BLINDED. Sight-based checks automatically fail (Hearing/Smell still work)."
                )
            elif illum == "darkness" and not has_enhanced_vision:
                illum_alert += "\nSYSTEM ALERT: Character is in TOTAL DARKNESS. Sight-based checks automatically fail (Hearing/Smell still work)."
            elif illum == "dim" and not has_enhanced_vision:
                illum_alert += "\nSYSTEM ALERT: Character is in DIM LIGHT. Disadvantage (-5 to Passive) on sight-based checks."

            if is_deafened or in_silence:
                reason = "DEAFENED" if is_deafened else "in a magically SILENCED zone"
                illum_alert += f"\nSYSTEM ALERT: Character is {reason}. Hearing-based checks automatically fail."

        elif clean_skill == "stealth":
            if illum == "bright":
                illum_alert = (
                    "\nSYSTEM ALERT: Character is in BRIGHT LIGHT. They cannot hide without physical cover or invisibility."
                )
            elif illum == "darkness":
                illum_alert = (
                    "\nSYSTEM ALERT: Character is in TOTAL DARKNESS. They are heavily obscured and can hide freely. "
                    "If successful against enemy passive perception, use `toggle_condition` to apply 'Hidden'."
                )
            elif illum == "dim":
                illum_alert = (
                    "\nSYSTEM ALERT: Character is in DIM LIGHT. They are lightly obscured and can hide. "
                    "If successful against enemy passive perception, use `toggle_condition` to apply 'Hidden'."
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
    result_str += illum_alert

    await write_audit_log(vault_path, "Rules Engine", "perform_ability_check_or_save Executed", result_str)
    return result_str


@tool
async def evaluate_extreme_weather(
    character_names: list[str],
    temperature_f: int,
    hours_exposed: int = 1,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Evaluates Constitution saving throws for extreme heat (>= 100 F) or extreme cold (<= 0 F).
    Automatically applies Exhaustion levels for each failed hour of exposure.
    """
    vault_path = config["configurable"].get("thread_id")
    results = []

    if 0 < temperature_f < 100:
        return f"MECHANICAL TRUTH: Temperature is {temperature_f}°F, which is not extreme. No checks needed."

    for char_name in character_names:
        ent = await _get_entity_by_name(char_name, vault_path)
        if not ent or not isinstance(ent, Creature):
            continue

        # Load YAML for equipment check
        j_dir = get_journals_dir(vault_path)
        file_path = os.path.join(j_dir, f"{ent.name}.md")
        yaml_data = {}
        if os.path.exists(file_path):
            try:
                async with read_markdown_entity(file_path) as (yd, _):
                    yaml_data = yd
            except Exception:
                pass

        is_immune_to_weather = False
        disadvantage = False

        if temperature_f <= 0:
            if "cold" in ent.resistances or "cold" in ent.immunities:
                is_immune_to_weather = True

            inv = yaml_data.get("inventory", [])
            if any("cold weather gear" in str(i).lower() for i in inv):
                is_immune_to_weather = True
            if any("cold_weather_gear" in t.lower() for t in ent.tags):
                is_immune_to_weather = True

            base_dc = 10
            dc_increment = 0
            weather_type = "Extreme Cold"

        elif temperature_f >= 100:
            if "fire" in ent.resistances or "fire" in ent.immunities:
                is_immune_to_weather = True

            equipment = yaml_data.get("equipment", {})
            armor_name = equipment.get("armor", "None")
            if str(armor_name).strip() not in ["None", "", "Unarmored"]:
                armor_item = await ItemCompendium.load_item(vault_path, str(armor_name))
                if armor_item and hasattr(armor_item, "armor_category"):
                    if armor_item.armor_category.lower() in ["medium", "heavy"]:
                        disadvantage = True
                else:
                    if any(w in str(armor_name).lower() for w in ["plate", "mail", "scale", "splint", "half"]):
                        disadvantage = True

            base_dc = 5
            dc_increment = 1
            weather_type = "Extreme Heat"

        if is_immune_to_weather:
            results.append(f"[{ent.name}] is naturally adapted or geared for {weather_type} and ignores the effects.")
            continue

        failures = 0
        for h in range(hours_exposed):
            dc = base_dc + (h * dc_increment)
            roll1 = random.randint(1, 20)
            roll2 = random.randint(1, 20)
            save_roll = min(roll1, roll2) if disadvantage else roll1
            total_save = save_roll + ent.constitution_mod.total
            if total_save < dc:
                failures += 1

        if failures > 0:
            ent.exhaustion_level += failures
            exhaustion_cond = next((c for c in ent.active_conditions if c.name == "Exhaustion"), None)
            if not exhaustion_cond:
                ent.active_conditions.append(ActiveCondition(name="Exhaustion", source_name=weather_type))

            if ent.exhaustion_level >= 6:
                ent.hp.base_value = 0
                if not any(c.name == "Dead" for c in ent.active_conditions):
                    ent.active_conditions.append(ActiveCondition(name="Dead"))
                results.append(
                    f"[{ent.name}] failed {failures} CON saves. "
                    f"Reached Exhaustion Level {ent.exhaustion_level} and is DEAD."
                )
            else:
                results.append(
                    f"[{ent.name}] failed {failures} CON saves vs {weather_type}. "
                    f"They gained {failures} levels of Exhaustion (Current Level: {ent.exhaustion_level})."
                )
        else:
            results.append(f"[{ent.name}] succeeded all CON saves vs {weather_type} for {hours_exposed} hours.")

    return f"MECHANICAL TRUTH: Evaluated {hours_exposed} hours of {temperature_f}°F weather.\n" + "\n".join(results)


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

    await update_yaml_frontmatter.ainvoke(
        {"entity_name": "CAMPAIGN_MASTER", "updates": {"current_date": f"Day {new_day}", "in_game_time": new_time_str}}, config
    )

    total_seconds_advanced = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total_seconds_advanced > 0 and trigger_events:
        # Dispatch an AdvanceTime event to the engine so buffs can expire
        event = GameEvent(
            event_type="AdvanceTime", source_uuid=uuid.uuid4(), payload={"seconds_advanced": total_seconds_advanced}
        )
        EventBus.dispatch(event)

    return f"Success: Time advanced. It is now Day {new_day}, {new_time_str}."


@tool
async def start_combat(pc_names: list[str], enemies: list[dict], *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Creates ACTIVE_COMBAT.md."""
    vault_path = config["configurable"].get("thread_id")
    j_dir = get_journals_dir(vault_path)
    combatants = []

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

        combatants.append(
            {
                "name": pc,
                "init": random.randint(1, 20) + pc_dex_mod,
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
        combatants.append(
            {
                "name": enemy.get("name", "Unknown"),
                "init": random.randint(1, 20) + int(enemy.get("dex_mod", 0)),
                "hp": int(enemy.get("hp", 10)),
                "max_hp": int(enemy.get("hp", 10)),
                "ac": int(enemy.get("ac", 10)),
                "conditions": [],
                "is_pc": False,
                "x": float(enemy.get("x", 0.0)),
                "y": float(enemy.get("y", 0.0)),
                "z": float(enemy.get("z", 0.0)),
            }
        )

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
        "  c.name, `${c.hp}/${c.max_hp}`, c.ac, `(${c.x||0}, ${c.y||0}, ${c.z||0})`,\n"
        '  c.hp <= 0 ? "💀 Dead" : (c.conditions.length ? c.conditions.join(", ") : "Healthy")\n'
        "]);\n"
        'dv.header(2, "⚔️ Active Combat Tracker ⚔️"); dv.paragraph(`**Round:** ${p.round}`);\n'
        'dv.table(["Init", "Combatant", "HP", "AC", "Pos (x,y,z)", "Status"], tbl);\n'
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
        EventBus.dispatch(sot_event)
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
                    EventBus.dispatch(eot_event)
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
                new_turn_ent.spell_slots_expended_this_turn = 0

                sot_event = GameEvent(
                    event_type="StartOfTurn",
                    source_uuid=new_turn_ent.entity_uuid,
                    vault_path=vault_path,
                )
                EventBus.dispatch(sot_event)
                if "results" in sot_event.payload and sot_event.payload["results"]:
                    log_msg.extend(sot_event.payload["results"])
    except Exception as e:
        return str(e)

    if advance_global_clock:
        await advance_time.ainvoke({"seconds": 6, "trigger_events": False}, config)

    if new_init is not None:
        event = GameEvent(
            event_type="AdvanceTime", source_uuid=uuid.uuid4(), payload={"seconds_advanced": 6, "target_initiative": new_init}
        )
        EventBus.dispatch(event)

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


@tool
async def move_entity(  # noqa: C901
    entity_name: str,
    target_x: float,
    target_y: float,
    target_z: float = None,
    movement_type: str = "walk",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Moves an entity to a new (X, Y, Z) coordinate on the spatial grid and visually updates the combat whiteboard.
    Valid movement_type values: 'walk', 'jump', 'climb', 'fly', 'teleport', 'crawl', 'disengage', 'forced', 'fall', 'travel'.
    'walk' and 'crawl' will be blocked by solid walls in a straight line."""
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(entity_name, vault_path)
    if not entity:
        return f"SYSTEM ERROR: Entity '{entity_name}' not found in active memory."

    if target_z is None:
        target_z = entity.z
    old_x, old_y, old_z = entity.x, entity.y, entity.z

    dz = target_z - old_z
    dist_3d = spatial_service.calculate_distance(old_x, old_y, old_z, target_x, target_y, target_z, vault_path)

    # --- Identify Dragged Entities ---
    dragged_entities = []
    if movement_type.lower() not in ["teleport", "fall", "forced"]:
        for uid, ent in get_all_entities(vault_path).items():
            if isinstance(ent, Creature):
                for cond in ent.active_conditions:
                    if cond.name.lower() == "grappled" and cond.source_uuid == entity.entity_uuid:
                        dragged_entities.append(ent)

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
        str_score = (entity.strength_mod.total * 2) + 10
        run_high = 3 + entity.strength_mod.total
        if dz > run_high or dist_3d > str_score:
            return (
                f"SYSTEM ERROR: Jump exceeds physical limits. Max running long-jump: {str_score}ft. "  # noqa: E501
                f"Max running high-jump: {run_high}ft. Call perform_ability_check_or_save for Athletics to push limits."  # noqa: E501
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
    result = EventBus.dispatch(event)

    if result.status == EventStatus.CANCELLED:
        error_msg = result.payload.get("error", "Movement cancelled by rules engine.")
        return (
            f"SYSTEM ERROR: {error_msg} Ask the player if they want to use their Action to 'Dash' "
            f"(call `use_dash_action`), pick a shorter route, or do something else."
        )

    entity.x = target_x
    entity.y = target_y
    entity.z = target_z

    spatial_service.sync_entity(entity)

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
        except Exception:
            pass

    rem = int(entity.movement_remaining) if float(entity.movement_remaining).is_integer() else entity.movement_remaining
    base_msg = (
        f"MECHANICAL TRUTH: {entity.name} moved from ({old_x}, {old_y}) to ({target_x}, {target_y}) via {movement_type}. "
        f"Remaining movement: {rem}"
    )

    if drag_msg_parts:
        base_msg += f" They automatically dragged {', '.join(drag_msg_parts)} with them."

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
                    eff_reach = base_reach + max(0, (other_entity.size - 5.0) / 2.0) + max(0, (entity.size - 5.0) / 2.0)

                    dist_before = spatial_service.calculate_distance(
                        old_x, old_y, old_z, other_entity.x, other_entity.y, other_entity.z, vault_path
                    )
                    dist_after = spatial_service.calculate_distance(
                        target_x, target_y, target_z, other_entity.x, other_entity.y, other_entity.z, vault_path
                    )
                    if dist_before <= eff_reach and dist_after > eff_reach:
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
        from server.vault_io import sync_engine_from_vault_updates
        res = await sync_engine_from_vault_updates(vault_path)
        return res

    from server.vault_io import load_entity_into_engine, get_journals_dir
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


@tool
async def use_ability_or_spell(  # noqa: C901
    caster_name: str,
    ability_name: str,
    target_names: list[str] = None,
    target_x: float = None,
    target_y: float = None,
    target_z: float = None,
    aoe_shape: str = None,
    aoe_size: float = None,
    is_reaction: bool = False,
    is_legendary_action: bool = False,
    manual_attack_roll: int = None,
    is_critical: bool = False,
    manual_saves: dict = None,
    force_auto_roll: bool = False,
    proxy_caster_name: str = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Use this tool whenever a character casts a spell or uses a class feature.
    If the spell is an Area of Effect (AoE) and coordinates are provided, pass target_x, target_y, aoe_shape, and aoe_size.
    The engine will override 'target_names' and compute the true targets and structural damage using line-of-effect raycasting.
    """
    vault_path = config["configurable"].get("thread_id")
    caster = await _get_entity_by_name(caster_name, vault_path)
    if not caster:
        return f"SYSTEM ERROR: Caster '{caster_name}' not found."

    proxy = None
    if proxy_caster_name:
        proxy = await _get_entity_by_name(proxy_caster_name, vault_path)
        if not proxy:
            return f"SYSTEM ERROR: Proxy caster '{proxy_caster_name}' not found."
        if getattr(proxy, "reaction_used", False):
            return f"SYSTEM ERROR: Proxy '{proxy.name}' has already used their reaction this round."

    origin_ent = proxy if proxy else caster

    # --- VERBAL COMMAND ENFORCEMENT FOR SUMMONS ---
    if getattr(origin_ent, "summoned_by_uuid", None) and getattr(origin_ent, "summon_spell", "").lower() != "find familiar":
        summoner = get_entity(origin_ent.summoned_by_uuid, vault_path)
        if summoner:
            summoner_conds = [c.name.lower() for c in getattr(summoner, "active_conditions", [])]
            ent_conds = [c.name.lower() for c in getattr(origin_ent, "active_conditions", [])]
            reason = ""
            if "silenced" in summoner_conds:
                reason = f"its summoner ({summoner.name}) is Silenced"
            elif "confused" in summoner_conds:
                reason = f"its summoner ({summoner.name}) is Confused"
            elif "unconscious" in summoner_conds or "incapacitated" in summoner_conds:
                reason = f"its summoner ({summoner.name}) is Incapacitated"
            elif "deafened" in ent_conds:
                reason = "it is Deafened and cannot hear verbal commands"

            if reason:
                return f"SYSTEM ERROR: {origin_ent.name} cannot execute this action because {reason}. Per 5.5e rules, without verbal commands it must take the Dodge action and use its move to avoid danger."

    mitigation_notes = ""
    spell_def = await SpellCompendium.load_spell(vault_path, ability_name)
    if spell_def:
        mechanics_dump = spell_def.mechanics.model_dump()
        granted_tags = spell_def.mechanics.granted_tags
        description = spell_def.description
        requires_attack_roll = spell_def.mechanics.requires_attack_roll
        save_required = spell_def.mechanics.save_required
        ability_display_name = spell_def.name
        mitigation_notes = getattr(spell_def, "mitigation_notes", "")

        v_req = any("V" in comp.upper() for comp in spell_def.components)
        s_req = any("S" in comp.upper() for comp in spell_def.components)
        m_req = any("M" in comp.upper() for comp in spell_def.components)

        config_settings = _get_config_settings(vault_path)
        strict_materials = config_settings.get("strict_material_components", False)
        strict_penalties = config_settings.get("strict_vsm_penalties", False)

        vsm_error = ""

        # REQ-SND-001: Verbal Verification
        if v_req and any(c.name.lower() in ["silenced", "gagged"] for c in getattr(caster, "active_conditions", [])):
            vsm_error = f"SYSTEM ERROR: {caster.name} cannot cast '{ability_display_name}' because it requires Verbal (V) components and they are Silenced/Gagged. (REQ-SND-001)"

        # REQ-SPL-014: Bound check
        if (
            not vsm_error
            and (s_req or m_req)
            and any(c.name.lower() == "bound" for c in getattr(caster, "active_conditions", []))
        ):
            vsm_error = f"SYSTEM ERROR: {caster.name} cannot cast '{ability_display_name}' because it requires Somatic/Material components, but their hands are Bound. (REQ-SPL-014)"

        # REQ-SPL-022, REQ-SPL-023: Hands Full Check (War Caster & Strict Materials)
        if not vsm_error and (s_req or (m_req and strict_materials)):
            file_path = os.path.join(get_journals_dir(vault_path), f"{caster.name}.md")
            equipment = {}
            if os.path.exists(file_path):
                try:
                    async with read_markdown_entity(file_path) as (yaml_data, _):
                        equipment = yaml_data.get("equipment", {})
                except Exception:
                    pass

            def is_occupied(slot_val):
                return str(slot_val).strip() not in ["None", "", "Unarmed"]

            main_hand = equipment.get("main_hand", "None")
            off_hand = equipment.get("off_hand", "None")
            shield_slot = equipment.get("shield", "None")

            hands_full = is_occupied(main_hand) and (is_occupied(off_hand) or is_occupied(shield_slot))

            if hands_full:
                if s_req and "war_caster" not in getattr(caster, "tags", []):
                    vsm_error = f"SYSTEM ERROR: {caster.name} cannot cast '{ability_display_name}' because it requires Somatic (S) components and both hands are full (missing War Caster feat). (REQ-SPL-022)"

                if not vsm_error and m_req and strict_materials:
                    has_focus = False
                    for item_name in [main_hand, off_hand, shield_slot]:
                        if is_occupied(item_name):
                            item_def = await ItemCompendium.load_item(vault_path, item_name)
                            if item_def and any(
                                t.lower() in ["spellcasting_focus", "holy_symbol"] for t in getattr(item_def, "tags", [])
                            ):
                                has_focus = True
                                break
                    if not has_focus:
                        vsm_error = f"SYSTEM ERROR: {caster.name} cannot cast '{ability_display_name}' because it requires Material (M) components, their hands are full, and they do not have a spellcasting focus equipped. (REQ-SPL-023)"

        if vsm_error:
            if strict_penalties:
                if spell_def.level > 0 and not is_reaction and not is_legendary_action:
                    caster.spell_slots_expended_this_turn += 1
                if is_reaction:
                    caster.reaction_used = True
                return f"{vsm_error}\nSTRICT MODE PENALTY: The spell failed but the action/reaction and spell slot were still consumed! (REQ-SPL-024)"
            else:
                return vsm_error
    else:
        item_def = await ItemCompendium.load_item(vault_path, ability_name)
        if item_def and getattr(item_def, "active_mechanics", None):
            mechanics_dump = item_def.active_mechanics.model_dump()
            granted_tags = item_def.active_mechanics.granted_tags
            description = item_def.description
            requires_attack_roll = item_def.active_mechanics.requires_attack_roll
            save_required = item_def.active_mechanics.save_required
            ability_display_name = item_def.name
            mitigation_notes = getattr(item_def, "mitigation_notes", "")
        else:
            entry = await CompendiumManager.get_entry(vault_path, ability_name)
            if not entry:
                return (
                    f"CACHE MISS: '{ability_name}' is not in the Engine. "
                    f"Use `query_rulebook` to find the exact rules, then use `encode_new_compendium_entry` "
                    f"to save it. Then try casting again."
                )
            mechanics_dump = entry.mechanics.model_dump() if hasattr(entry.mechanics, "model_dump") else entry.mechanics
            granted_tags = getattr(entry.mechanics, "granted_tags", []) if getattr(entry, "mechanics", None) else []
            description = entry.description
            requires_attack_roll = getattr(entry.mechanics, "requires_attack_roll", False)
            save_required = getattr(entry.mechanics, "save_required", "")
            ability_display_name = entry.name
            mitigation_notes = getattr(entry, "mitigation_notes", "")

    target_uuids = []
    target_wall_ids = []
    target_terrain_ids = []

    ignore_walls = "ignore_walls" in granted_tags
    penetrates = "penetrates_destructible" in granted_tags

    is_spell = False
    requires_slot = False
    if spell_def:
        is_spell = True
        if spell_def.level > 0:
            requires_slot = True

    # REQ-SPL-001 & REQ-SPL-003: Spell Slot Limit & Magic Items
    if is_spell and requires_slot and not is_reaction and not is_legendary_action:
        if caster.spell_slots_expended_this_turn > 0:
            return f"SYSTEM ERROR: {caster.name} has already expended a spell slot this turn. (REQ-SPL-001)"

    ox, oy, oz, tx, ty, tz = None, None, None, None, None, None
    if aoe_shape and aoe_size and target_x is not None and target_y is not None:
        shape = aoe_shape.lower()
        tz = target_z
        if shape in ["circle", "sphere", "cylinder", "cube"]:
            ox, oy, tx, ty = target_x, target_y, target_x, target_y
            oz = target_z if target_z is not None else 0.0
        else:  # cone, line originate from the caster
            ox, oy, tx, ty = origin_ent.x, origin_ent.y, target_x, target_y
            oz = origin_ent.z
            if tz is None:
                tz = origin_ent.z

        hits, walls, terrains = spatial_service.get_aoe_targets(
            shape,
            aoe_size,
            ox,
            oy,
            tx,
            ty,
            origin_z=oz,
            target_z=tz,
            ignore_walls=ignore_walls,
            penetrates_destructible=penetrates,
            vault_path=vault_path,
        )
        target_uuids.extend(hits)
        target_wall_ids.extend(walls)
        target_terrain_ids.extend(terrains)
        target_string = f"{aoe_size}ft {shape} at coordinates ({target_x}, {target_y})"
    else:
        for name in target_names or []:
            ent = await _get_entity_by_name(name, vault_path)
            if ent:
                if hasattr(spatial_service, "check_path_collision") and not ignore_walls:
                    collision = spatial_service.check_path_collision(
                        origin_ent.x,
                        origin_ent.y,
                        origin_ent.z,
                        ent.x,
                        ent.y,
                        ent.z,
                        entity_height=getattr(ent, "height", 5.0),
                        check_vision=False,
                        vault_path=vault_path,
                    )
                    if collision and collision.is_solid:
                        return f"SYSTEM ERROR: Target '{ent.name}' has Total Cover (blocked by '{collision.label}'). Spells require an unbroken line of effect. (REQ-SPL-005)"

                target_uuids.append(ent.entity_uuid)
        target_string = ", ".join(target_names or []) or "themselves"

    manual_saves = manual_saves or {}
    is_caster_pc = any(t in caster.tags for t in ["pc", "player"])

    if requires_attack_roll and is_caster_pc and not force_auto_roll and manual_attack_roll is None:
        auto_settings = get_roll_automations(caster.name)
        if not auto_settings.get("attack_rolls", True):
            return (
                f"SYSTEM ALERT: {caster.name} has manual attack rolls enabled. Ask the player to roll the "
                f"spell attack (including modifiers) and provide the total. Then call this tool again with "
                f"`manual_attack_roll=X` (and `is_critical=True` if natural 20) or `force_auto_roll=True`."
            )

    if save_required:
        missing_saves = []
        for t_uid in target_uuids:
            ent = get_entity(t_uid, vault_path)
            if ent and any(tag in ent.tags for tag in ["pc", "player"]):
                auto_settings = get_roll_automations(ent.name)
                if not auto_settings.get("saving_throws", True) and ent.name not in manual_saves and not force_auto_roll:
                    missing_saves.append(ent.name)
        if missing_saves:
            return (
                f"SYSTEM ALERT: The following players have manual saving throws enabled: {', '.join(missing_saves)}. "
                f"Ask them to roll their {save_required} saving throws (including modifiers) and provide the totals. "
                f"Then call this tool again with `manual_saves={{'PlayerName': 15, ...}}` or `force_auto_roll=True`."
            )

    current_init = await _get_current_combat_initiative(vault_path)
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        vault_path=vault_path,
        payload={
            "ability_name": ability_name,
            "mechanics": mechanics_dump,
            "target_uuids": target_uuids,
            "target_wall_ids": target_wall_ids,
            "target_terrain_ids": target_terrain_ids,
            "current_initiative": current_init,
            "manual_attack_roll": manual_attack_roll,
            "is_critical": is_critical,
            "manual_saves": manual_saves,
            "aoe_shape": aoe_shape,
            "aoe_size": aoe_size,
            "origin_x": ox,
            "origin_y": oy,
            "origin_z": oz,
            "target_x": tx,
            "target_y": ty,
            "target_z": tz,
        },
    )

    result = EventBus.dispatch(event)

    results_list = result.payload.get("results", [])

    if is_spell and requires_slot and not is_reaction and not is_legendary_action and result.status != EventStatus.CANCELLED:
        caster.spell_slots_expended_this_turn += 1

    if proxy and result.status != EventStatus.CANCELLED:
        proxy.reaction_used = True

    if results_list:
        res_msg = "\n".join(results_list)
        ret = f"MECHANICAL TRUTH: {caster.name} cast {ability_display_name} on {target_string}.\n{res_msg}"
    else:
        ret = f"MECHANICAL TRUTH: {caster.name} used {ability_display_name} on {target_string}. Effect: {description}"

    if mitigation_notes:
        ret += f"\nDM DIRECTIVE (Mitigation/Counter): {mitigation_notes}"
    return ret


@tool
async def encode_new_compendium_entry(
    name: str = Field(..., description="Exact name of the ability or spell"),
    category: str = Field(..., description="'spell', 'feature', 'feat', 'item', 'weapon', or 'armor'"),
    action_type: str = Field(..., description="'Action', 'Bonus Action', 'Reaction', or 'Passive'"),
    description: str = Field(..., description="A concise summary of the mechanical rules."),
    mitigation_notes: str = Field(
        "", description="Explain how to counter/thwart this ability (e.g. 'Defeated by Silence' for echolocation)."
    ),
    source_reference: str = Field(..., description="Name of the rulebook and page number."),
    damage_dice: str = Field("", description="e.g., '8d6'. Leave empty string if no damage."),
    damage_type: str = Field("", description="e.g., 'fire'."),
    save_required: str = Field("", description="e.g., 'dexterity'."),
    granted_tags: list[str] = Field(
        default_factory=list,
        description="List of boolean tags granted by this feature (e.g. ['ignore_difficult_terrain']).",
    ),
    requires_engine_update: bool = Field(
        False, description="Set to True if this feat introduces logic the engine does not natively support yet."
    ),
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Teach the engine a new ability. Call ONLY when you receive a CACHE MISS."""
    vault_path = config["configurable"].get("thread_id")

    if category.lower() == "spell":
        spell_mech = SpellMechanics(
            requires_attack_roll=action_type.lower() == "attack",
            save_required=save_required,
            damage_dice=damage_dice,
            damage_type=damage_type,
            granted_tags=granted_tags,
        )
        spell_def = SpellDefinition(
            name=name,
            casting_time=action_type,
            description=description,
            mitigation_notes=mitigation_notes,
            mechanics=spell_mech,
        )
        filepath = await SpellCompendium.save_spell(vault_path, spell_def)
    elif category.lower() in ["item", "weapon", "armor"]:
        if category.lower() == "weapon" or action_type.lower() == "attack":
            item_def = WeaponItem(
                name=name,
                description=description,
                mitigation_notes=mitigation_notes,
                damage_dice=damage_dice,
                damage_type=damage_type,
                tags=granted_tags,
            )
        elif category.lower() == "armor":
            item_def = ArmorItem(name=name, description=description, mitigation_notes=mitigation_notes, tags=granted_tags)
        else:
            mech = (
                SpellMechanics(
                    save_required=save_required, damage_dice=damage_dice, damage_type=damage_type, granted_tags=granted_tags
                )
                if (save_required or damage_dice)
                else None
            )
            item_def = WondrousItem(
                name=name, description=description, mitigation_notes=mitigation_notes, tags=granted_tags, active_mechanics=mech
            )
        filepath = await ItemCompendium.save_item(vault_path, item_def)
    else:
        mechanics = MechanicEffect(
            damage_dice=damage_dice, damage_type=damage_type, save_required=save_required, granted_tags=granted_tags
        )

        entry = CompendiumEntry(
            name=name,
            category=category,
            action_type=action_type,
            description=description,
            mitigation_notes=mitigation_notes,
            references=[source_reference],
            mechanics=mechanics,
        )
        filepath = await CompendiumManager.save_entry(vault_path, entry)

    warning = ""
    if requires_engine_update:
        warning = (
            "\n[SYSTEM ALERT]: This feat introduces novel logic. "
            "The human DM must manually update `dnd_rules_engine.py` to evaluate these new tags!"
        )
    return (
        f"SUCCESS: '{name}' encoded to {filepath}. The engine now understands this ability."
        f"{warning} Proceed with your action."
    )


@tool
async def spawn_summon(
    summoner_name: str,
    summon_name: str,
    summon_type: str = "tasha",
    hp: int = 10,
    ac: int = 10,
    x: float = 0.0,
    y: float = 0.0,
    requires_concentration: bool = False,
    spell_name: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Spawns a summoned creature or familiar into the active combat tracker and spatial map.
    - summon_type="tasha": Acts immediately after the summoner (Initiative - 0.01).
    - summon_type="familiar": Rolls its own independent initiative.
    """
    vault_path = config["configurable"].get("thread_id")

    ent = await _get_entity_by_name(summon_name, vault_path)
    if not ent:
        details = NPCDetails(stat_block=f"AC {ac}\nHP {hp}", base_attitude="Friendly to Summoner")
        await create_new_entity.ainvoke(
            {
                "entity_name": summon_name,
                "entity_type": "NPC",
                "background_context": f"Summoned by {summoner_name}",
                "details": details.model_dump(),
                "x": x,
                "y": y,
            },
            config=config,
        )
        ent = await _get_entity_by_name(summon_name, vault_path)
    else:
        ent.x = x
        ent.y = y
        spatial_service.sync_entity(ent)

    if ent:
        updates = {"tags": getattr(ent, "tags", [])}
        if "party_npc" not in updates["tags"]:
            updates["tags"].append("party_npc")
            if "party_npc" not in ent.tags:
                ent.tags.append("party_npc")

        if requires_concentration and spell_name:
            summoner = await _get_entity_by_name(summoner_name, vault_path)
            if summoner:
                ent.summoned_by_uuid = summoner.entity_uuid
                ent.summon_spell = spell_name
                updates["summoned_by_uuid"] = str(summoner.entity_uuid)
                updates["summon_spell"] = spell_name

        await update_yaml_frontmatter.ainvoke({"entity_name": summon_name, "updates": updates}, config=config)

    combat_file = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")
    if not os.path.exists(combat_file):
        return f"MECHANICAL TRUTH: {summon_name} spawned out of combat at ({x}, {y})."

    new_init = 0.0
    try:
        async with edit_markdown_entity(combat_file) as state:
            yaml_data = state["yaml_data"]
            combatants = yaml_data.get("combatants", [])

            summoner_init = 0.0
            for c in combatants:
                if c["name"].lower() == summoner_name.lower():
                    summoner_init = float(c.get("init", 0))
                    break

            if summon_type.lower() == "tasha":
                new_init = summoner_init - 0.01
            else:
                dex_mod = ent.dexterity_mod.total if ent else 0
                new_init = float(random.randint(1, 20) + dex_mod)

            combatants.append(
                {
                    "name": summon_name,
                    "init": new_init,
                    "hp": hp,
                    "max_hp": hp,
                    "ac": ac,
                    "conditions": [],
                    "is_pc": False,
                    "x": x,
                    "y": y,
                    "z": getattr(ent, "z", 0.0),
                }
            )

            # Sort highest to lowest initiative
            yaml_data["combatants"] = sorted(combatants, key=lambda c: float(c["init"]), reverse=True)

            if vault_path in spatial_service.active_combatants:
                spatial_service.active_combatants[vault_path].append(summon_name)
    except Exception as e:
        return str(e)

    return f"MECHANICAL TRUTH: {summon_name} spawned at ({x}, {y}) with Initiative {new_init:.2f}."


@tool
async def take_rest(character_names: list[str], rest_type: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    Use this tool when characters explicitly take a Short or Long Rest.
    It automatically advances time and signals the engine to heal and recharge resources.
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
            payload={"rest_type": rest_type.lower(), "target_uuids": uuids},
        )
        EventBus.dispatch(event)

    return (
        f"MECHANICAL TRUTH: {', '.join(character_names)} completed a {rest_type} rest. "
        f"Time advanced {hours_to_advance} hours. HP and resources processed."
    )


@tool
async def drop_concentration(character_name: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Use this tool to drop a character's concentration on a spell voluntarily or after a failed Constitution save."""
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(character_name, vault_path)
    if not entity or not isinstance(entity, Creature):
        return f"SYSTEM ERROR: '{character_name}' not found."

    if not entity.concentrating_on:
        return f"MECHANICAL TRUTH: {entity.name} is not currently concentrating on any spell."

    spell_name = entity.concentrating_on

    event = GameEvent(event_type="DropConcentration", source_uuid=entity.entity_uuid, vault_path=vault_path)
    EventBus.dispatch(event)

    return (
        f"MECHANICAL TRUTH: {entity.name} dropped concentration on {spell_name}. " "All associated effects have been cleared."
    )


@tool
async def ready_action(
    character_name: str,
    action_description: str,
    trigger_condition: str,
    is_spell: bool = False,
    spell_name: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Saves a readied action to the ACTIVE_COMBAT.md whiteboard. The AI Planner will monitor this trigger."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")

    try:
        # REQ-SPL-013: Readying a spell requires concentration and expends the slot
        if is_spell and spell_name:
            entity = await _get_entity_by_name(character_name, vault_path)
            if entity and isinstance(entity, Creature):
                if entity.spell_slots_expended_this_turn > 0:
                    return f"SYSTEM ERROR: {character_name} has already expended a spell slot this turn. (REQ-SPL-001)"

                entity.spell_slots_expended_this_turn += 1
                if entity.concentrating_on:
                    EventBus.dispatch(
                        GameEvent(event_type="DropConcentration", source_uuid=entity.entity_uuid, vault_path=vault_path)
                    )

                entity.concentrating_on = f"Readied: {spell_name}"
                action_description += f" [Concentrating on {spell_name}]"

        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            readied = yaml_data.get("readied_actions", [])
            if not isinstance(readied, list):
                readied = []

            readied = [ra for ra in readied if ra.get("character") != character_name]
            readied.append({"character": character_name, "action": action_description, "trigger": trigger_condition})
            yaml_data["readied_actions"] = readied
    except Exception as e:
        return str(e)

    return f"MECHANICAL TRUTH: {character_name} readied an action. Trigger: '{trigger_condition}'."


@tool
async def clear_readied_action(character_name: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Removes a readied action from the whiteboard after it is triggered or cancelled."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")

    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            readied = yaml_data.get("readied_actions", [])
            if not isinstance(readied, list):
                readied = []

            readied = [ra for ra in readied if ra.get("character") != character_name]
            yaml_data["readied_actions"] = readied
    except Exception as e:
        return str(e)

    return f"Success: Cleared readied action for {character_name}."


@tool
async def use_dash_action(entity_name: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Allows an entity to use their action to double their movement speed for the turn."""
    vault_path = config["configurable"].get("thread_id")
    entity = await _get_entity_by_name(entity_name, vault_path)
    if not entity or not isinstance(entity, Creature):
        return f"SYSTEM ERROR: Entity '{entity_name}' not found."

    entity.movement_remaining += entity.speed
    return (
        f"MECHANICAL TRUTH: {entity.name} took the Dash action. Their remaining movement is now {entity.movement_remaining}ft."
    )


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
async def interact_with_object(
    character_name: str,
    target_label: str,
    interaction_type: str,
    stat_used: str = "dexterity",
    extra_modifier: int = 0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Resolves a character trying to pick a lock, force open a door, or disarm a trap on the spatial map.
    - interaction_type: 'lockpick' (uses Dex), 'force' (uses Str), 'disarm' (uses Dex or Int).
    - stat_used: 'dexterity', 'strength', or 'intelligence'.
    """
    vault_path = config["configurable"].get("thread_id")
    character = await _get_entity_by_name(character_name, vault_path)
    if not character:
        return f"SYSTEM ERROR: Character '{character_name}' not found."

    target_walls = [
        w for w in spatial_service.get_map_data(vault_path).active_walls if target_label.lower() in w.label.lower()
    ]
    if not target_walls:
        return f"SYSTEM ERROR: No object found matching '{target_label}'. Check the map."

    target = target_walls[0]

    if not target.is_locked and interaction_type in ["lockpick", "force"]:
        spatial_service.modify_wall(target.wall_id, is_solid=False, is_visible=True, vault_path=vault_path)
        return f"MECHANICAL TRUTH: {target.label} was not locked. {character.name} simply opened it."

    if target.interact_dc is None:
        return (
            f"MECHANICAL TRUTH: {target.label} cannot be interacted with in that way (No DC assigned). "
            f"It might require a specific key or magical mechanism."
        )

    # Calculate modifier
    stat_mod = getattr(character, f"{stat_used.lower()}_mod").total if hasattr(character, f"{stat_used.lower()}_mod") else 0

    # Give standard proficiency bonus if using lockpicks or forcing
    prof_bonus = math.ceil(character.character_level / 4) + 1 if character.character_level > 0 else 2
    total_mod = stat_mod + prof_bonus + extra_modifier

    roll = random.randint(1, 20)
    total = roll + total_mod

    log = (
        f"{character.name} attempts to {interaction_type} the {target.label}. "
        f"Rolled {roll} + {total_mod} = {total} vs DC {target.interact_dc}. "
    )

    if total >= target.interact_dc:
        if interaction_type in ["lockpick", "force"]:
            spatial_service.modify_wall(
                target.wall_id, is_locked=False, is_solid=False, is_visible=True, vault_path=vault_path
            )
            if target.trap:
                target.trap.is_active = False  # Bypassed successfully
            return f"MECHANICAL TRUTH: SUCCESS! {log} The {target.label} is now unlocked and opened."
        else:
            if target.trap and not target.trap.is_disarmable:
                return f"MECHANICAL TRUTH: FAILURE! {log} The {target.label} cannot be conventionally disarmed (it may be a magical effect)."
            if target.trap:
                target.trap.is_active = False
            return f"MECHANICAL TRUTH: SUCCESS! {log} The {target.label} is safely disarmed/resolved."
    else:
        msg = f"MECHANICAL TRUTH: FAILURE! {log} The attempt failed."
        if target.trap and target.trap.is_active and target.trap.trigger_on_interact_fail:
            target.trap.is_active = False
            trap = target.trap
            trap_msg = await trigger_environmental_hazard.ainvoke(
                {
                    "hazard_name": trap.hazard_name,
                    "target_names": [character.name],
                    "origin_x": target.start[0],
                    "origin_y": target.start[1],
                    "radius": trap.radius if trap.radius > 0 else None,
                    "requires_attack_roll": trap.requires_attack_roll,
                    "attack_bonus": trap.attack_bonus,
                    "save_required": trap.save_required,
                    "save_dc": trap.save_dc,
                    "damage_dice": trap.damage_dice,
                    "damage_type": trap.damage_type,
                    "half_damage_on_save": trap.half_damage_on_save,
                    "condition_applied": trap.condition_applied,
                },
                config=config,
            )
            msg += f"\nSYSTEM ALERT: TRAP TRIGGERED!\n{trap_msg}"

        return msg


@tool
async def trigger_environmental_hazard(
    hazard_name: str,
    target_names: list[str] = None,
    origin_x: Optional[float] = None,
    origin_y: Optional[float] = None,
    radius: Optional[float] = None,
    requires_attack_roll: bool = False,
    attack_bonus: int = 5,
    save_required: str = "",
    save_dc: int = 15,
    damage_dice: str = "",
    damage_type: str = "",
    half_damage_on_save: bool = True,
    condition_applied: str = "",
    manual_attack_roll: int = None,
    is_critical: bool = False,
    manual_saves: dict = None,
    force_auto_roll: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Triggers a trap or environmental hazard (e.g. Fireball trap, Poison Darts, Cave-in).
    You can target specific entities OR an area (using origin_x, origin_y, radius).
    Automatically resolves attack rolls or saving throws, applies damage/conditions, and checks concentration.
    """
    vault_path = config["configurable"].get("thread_id", "default")
    manual_saves = manual_saves or {}
    if target_names is None:
        target_names = []

    target_uuids = set()
    for name in target_names:
        ent = await _get_entity_by_name(name, vault_path)
        if ent:
            target_uuids.add(ent.entity_uuid)

    if origin_x is not None and origin_y is not None and radius is not None:
        spatial_hits = spatial_service.get_targets_in_radius(origin_x, origin_y, radius, vault_path)
        target_uuids.update(spatial_hits)

    if not target_uuids:
        return f"MECHANICAL TRUTH: {hazard_name} triggered, but no valid targets were in range."

    if save_required:
        missing_saves = []
        for t_uuid in target_uuids:
            ent = BaseGameEntity.get(t_uuid)
            if ent and any(tag in ent.tags for tag in ["pc", "player"]):
                auto_settings = get_roll_automations(ent.name)
                if not auto_settings.get("saving_throws", True) and ent.name not in manual_saves and not force_auto_roll:
                    missing_saves.append(ent.name)
        if missing_saves:
            return (
                f"SYSTEM ALERT: The following players have manual saving throws enabled: "
                f"{', '.join(missing_saves)}. Ask them to roll their {save_required} "
                f"saving throws (including modifiers) and provide the totals. Then call "
                f"this tool again with `manual_saves={{'PlayerName': 15, ...}}` or `force_auto_roll=True`."
            )

    trap_source = Creature(
        vault_path=vault_path,
        name=hazard_name,
        tags=["trap"],
        spell_save_dc=ModifiableValue(base_value=save_dc),
        spell_attack_bonus=ModifiableValue(base_value=attack_bonus),
        hp=ModifiableValue(base_value=1),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    cond_list = [{"condition": condition_applied, "duration": "1 minute"}] if condition_applied else []
    mechanics = {
        "requires_attack_roll": requires_attack_roll,
        "save_required": save_required.lower(),
        "damage_dice": damage_dice,
        "damage_type": damage_type.lower(),
        "half_damage_on_save": half_damage_on_save,
        "conditions_applied": cond_list,
    }

    current_init = await _get_current_combat_initiative(config["configurable"].get("thread_id"))

    event = GameEvent(
        event_type="SpellCast",
        source_uuid=trap_source.entity_uuid,
        vault_path=vault_path,
        payload={
            "ability_name": hazard_name,
            "mechanics": mechanics,
            "target_uuids": list(target_uuids),
            "current_initiative": current_init,
            "manual_attack_roll": manual_attack_roll,
            "is_critical": is_critical,
            "manual_saves": manual_saves,
        },
    )

    result = EventBus.dispatch(event)
    BaseGameEntity.remove(trap_source.entity_uuid)

    results_list = result.payload.get("results", [])
    if results_list:
        return f"MECHANICAL TRUTH: {hazard_name} triggered!\n" + "\n".join(results_list)

    return f"MECHANICAL TRUTH: {hazard_name} triggered but had no effect."


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
    from server.spatial_engine import TerrainZone

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
    from server.spatial_engine import TrapDefinition

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
async def toggle_condition(  # noqa: C901
    character_name: str,
    condition_name: str,
    is_active: bool,
    source_character_name: str = None,
    save_required: str = "",
    save_dc: int = 0,
    save_timing: str = "end",
    start_of_turn_thp: int = 0,
    end_of_turn_damage_dice: str = "",
    end_of_turn_damage_type: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Applies or removes a condition (e.g. 'Hidden', 'Prone', 'Poisoned') from an entity's sheet."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    source_ent = await _get_entity_by_name(source_character_name, vault_path) if source_character_name else None
    source_uuid = source_ent.entity_uuid if source_ent else None
    source_name = source_ent.name if source_ent else "Manual"

    alerts = []
    # Update memory
    engine_creature = await _get_entity_by_name(character_name, vault_path)
    if engine_creature and isinstance(engine_creature, Creature):
        if is_active:
            if not any(c.name.lower() == condition_name.lower() for c in engine_creature.active_conditions):
                engine_creature.active_conditions.append(
                    ActiveCondition(
                        name=condition_name.capitalize(),
                        source_name=source_name,
                        source_uuid=source_uuid,
                        save_required=save_required.lower(),
                        save_dc=save_dc,
                        save_timing=save_timing.lower(),
                        start_of_turn_thp=start_of_turn_thp,
                        end_of_turn_damage_dice=end_of_turn_damage_dice,
                        end_of_turn_damage_type=end_of_turn_damage_type.lower(),
                    )
                )

            # Instant mechanical enforcement for 0-speed conditions
            zero_speed = {"grappled", "restrained", "stunned", "paralyzed", "petrified", "unconscious"}
            if condition_name.lower() in zero_speed:
                engine_creature.movement_remaining = 0

            incap_conds = {"incapacitated", "stunned", "paralyzed", "petrified", "unconscious", "dead"}
            if condition_name.lower() in incap_conds and engine_creature.concentrating_on:
                EventBus.dispatch(
                    GameEvent(event_type="DropConcentration", source_uuid=engine_creature.entity_uuid, vault_path=vault_path)
                )
                alerts.append(f"\nSYSTEM ALERT: {character_name} lost concentration because they are {condition_name}.")

        else:
            engine_creature.active_conditions = [
                c for c in engine_creature.active_conditions if c.name.lower() != condition_name.lower()
            ]

    # Update YAML
    try:
        async with edit_markdown_entity(file_path) as state:
            yaml_data = state["yaml_data"]
            conds = yaml_data.get("active_conditions", [])
            if isinstance(conds, list):
                if is_active:
                    if not any(isinstance(c, dict) and c.get("name", "").lower() == condition_name.lower() for c in conds):
                        new_cond = {
                            "name": condition_name.capitalize(),
                            "duration_seconds": -1,
                            "source_name": source_name,
                            "applied_initiative": 0,
                            "save_required": save_required.lower(),
                            "save_dc": save_dc,
                            "save_timing": save_timing.lower(),
                            "start_of_turn_thp": start_of_turn_thp,
                            "end_of_turn_damage_dice": end_of_turn_damage_dice,
                            "end_of_turn_damage_type": end_of_turn_damage_type.lower(),
                        }
                        if source_uuid:
                            new_cond["source_uuid"] = str(source_uuid)
                        conds.append(new_cond)
                else:
                    conds = [
                        c for c in conds if not (isinstance(c, dict) and c.get("name", "").lower() == condition_name.lower())
                    ]

            yaml_data["active_conditions"] = conds
            if engine_creature:
                yaml_data["movement_remaining"] = engine_creature.movement_remaining
    except Exception as e:
        return str(e)

    state = "applied to" if is_active else "removed from"
    result_msg = f"MECHANICAL TRUTH: Condition '{condition_name}' was {state} {character_name}."
    for a in alerts:
        result_msg += a

    # Add Contextual DM Alerts based on D&D 5e Rules
    if is_active:
        cond_lower = condition_name.lower()
        zero_speed = {"grappled", "restrained", "stunned", "paralyzed", "petrified", "unconscious"}

        # --- STALLING FLYERS (REQ-MOV-008) ---
        if engine_creature:
            is_flying = "flying" in engine_creature.tags or any(
                c.name.lower() == "flying" for c in engine_creature.active_conditions
            )
            has_hover = "hover" in engine_creature.tags
            if (cond_lower in zero_speed or cond_lower == "prone") and is_flying and not has_hover and engine_creature.z > 0.0:
                result_msg += f"\nSYSTEM ALERT: {character_name} loses flying stability and falls to the ground!"
                fall_res = await move_entity.ainvoke(
                    {
                        "entity_name": character_name,
                        "target_x": engine_creature.x,
                        "target_y": engine_creature.y,
                        "target_z": 0.0,
                        "movement_type": "fall",
                    },
                    config=config,
                )
                result_msg += f"\n{fall_res}"

        if cond_lower in zero_speed:
            result_msg += (
                f"\nSYSTEM ALERT: '{condition_name.capitalize()}' reduces " f"speed to 0. They cannot move until freed."
            )
        elif cond_lower == "prone":
            result_msg += (
                "\nSYSTEM ALERT: 'Prone' means standing up costs half their movement speed. "
                "Melee attacks against them have Advantage."
            )
        elif cond_lower == "frightened":
            result_msg += (
                "\nSYSTEM ALERT: 'Frightened' means they have Disadvantage on attacks/checks "
                "while the source is visible, and CANNOT willingly move closer to it."
            )
        elif cond_lower in ["dazed", "confused"]:
            result_msg += (
                f"\nSYSTEM ALERT: '{condition_name.capitalize()}' restricts actions and movement. "
                f"Review the specific ability rules."
            )

    return result_msg


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


@tool
async def execute_grapple_or_shove(
    attacker_name: str,
    target_name: str,
    action_type: str,
    shove_type: str = "prone",
    throw_distance: float = 10.0,
    advantage: bool = False,
    disadvantage: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Resolves a contested Athletics check for a Grapple, Shove, or Throw. action_type must be 'grapple', 'shove', or 'throw'.
    The engine automatically calculates the correct Modifiers based on the entities' stats.
    """
    vault_path = config["configurable"].get("thread_id")
    attacker = await _get_entity_by_name(attacker_name, vault_path)
    target = await _get_entity_by_name(target_name, vault_path)

    if not attacker or not target:
        return "SYSTEM ERROR: Attacker or Target not found in active memory."

    att_roll1, att_roll2 = random.randint(1, 20), random.randint(1, 20)
    if advantage and not disadvantage:
        att_roll = max(att_roll1, att_roll2)
    elif disadvantage and not advantage:
        att_roll = min(att_roll1, att_roll2)
    else:
        att_roll = att_roll1

    tgt_roll = random.randint(1, 20)

    # Force engine to evaluate accurate modifiers natively
    attacker_mod = attacker.strength_mod.total
    target_mod = max(
        target.strength_mod.total, target.dexterity_mod.total
    )  # Target resists with better of Athletics/Acrobatics

    att_total = att_roll + attacker_mod
    tgt_total = tgt_roll + target_mod

    log = f"Contest: {attacker.name} ({att_roll} + {attacker_mod} = {att_total}) vs {target.name} ({tgt_roll} + {target_mod} = {tgt_total}). "

    # In D&D 5e, ties result in the situation remaining unchanged (defender wins ties).
    if att_total > tgt_total:
        log += "Attacker wins! "
        if action_type.lower() == "grapple":
            await toggle_condition.ainvoke(
                {
                    "character_name": target_name,
                    "condition_name": "Grappled",
                    "is_active": True,
                    "source_character_name": attacker.name,
                },
                config=config,
            )
            log += f"{target.name} is now Grappled by {attacker.name}. (Target speed reduced to 0). "
        elif action_type.lower() in ["shove", "throw"]:
            if shove_type.lower() == "prone" and action_type.lower() == "shove":
                await toggle_condition.ainvoke(
                    {"character_name": target_name, "condition_name": "Prone", "is_active": True}, config=config
                )
                log += f"{target.name} is knocked Prone. "
            else:
                dist_to_move = 5.0 if action_type.lower() == "shove" else throw_distance
                dx, dy = target.x - attacker.x, target.y - attacker.y
                dist = math.hypot(dx, dy)
                if dist == 0:
                    dx, dy, dist = 1, 0, 1  # Fallback if exactly stacked
                nx, ny = target.x + (dx / dist) * dist_to_move, target.y + (dy / dist) * dist_to_move
                move_res = await move_entity.ainvoke(
                    {
                        "entity_name": target_name,
                        "target_x": round(nx, 1),
                        "target_y": round(ny, 1),
                        "movement_type": "forced",
                    },
                    config=config,
                )
                log += (
                    f"{target.name} is {'shoved' if action_type.lower() == 'shove' else 'thrown'} {dist_to_move} feet away. "
                    + move_res
                )
                if action_type.lower() == "throw":
                    await toggle_condition.ainvoke(
                        {"character_name": target_name, "condition_name": "Prone", "is_active": True}, config=config
                    )
                    log += f" {target.name} lands Prone."
    else:
        log += "Defender wins! Nothing happens."

    return f"MECHANICAL TRUTH: {log}"


@tool
async def generate_random_loot(  # noqa: C901
    challenge_rating: int = 1, loot_type: str = "hoard", *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """
    Generates random D&D loot based on Challenge Rating (CR) and loot type ('individual' or 'hoard').
    Returns a formatted string of generated currency and items.
    Use this ONLY for improvising unscripted, homebrew rooms or random encounters. Do NOT use if the campaign module explicitly specifies the loot.
    """
    cp, sp, ep, gp, pp = 0, 0, 0, 0, 0
    items = []

    cr = challenge_rating
    if loot_type.lower() == "individual":
        if cr <= 4:
            roll = random.randint(1, 100)
            if roll <= 30:
                cp = sum(random.randint(1, 6) for _ in range(5))
            elif roll <= 60:
                sp = sum(random.randint(1, 6) for _ in range(4))
            elif roll <= 70:
                ep = sum(random.randint(1, 6) for _ in range(3))
            elif roll <= 95:
                gp = sum(random.randint(1, 6) for _ in range(3))
            else:
                pp = sum(random.randint(1, 6) for _ in range(1))
        elif cr <= 10:
            roll = random.randint(1, 100)
            if roll <= 30:
                cp, ep = sum(random.randint(1, 6) for _ in range(4)) * 100, sum(random.randint(1, 6) for _ in range(1)) * 10
            elif roll <= 60:
                sp, gp = sum(random.randint(1, 6) for _ in range(6)) * 10, sum(random.randint(1, 6) for _ in range(2)) * 10
            elif roll <= 70:
                ep, gp = sum(random.randint(1, 6) for _ in range(3)) * 10, sum(random.randint(1, 6) for _ in range(2)) * 10
            elif roll <= 95:
                gp = sum(random.randint(1, 6) for _ in range(4)) * 10
            else:
                gp, pp = sum(random.randint(1, 6) for _ in range(2)) * 10, sum(random.randint(1, 6) for _ in range(3))
        else:  # 11+
            gp = sum(random.randint(1, 6) for _ in range(4)) * 100
            pp = sum(random.randint(1, 6) for _ in range(1)) * 10
    else:  # Hoard
        if cr <= 4:
            cp, sp, gp = (
                sum(random.randint(1, 6) for _ in range(6)) * 100,
                sum(random.randint(1, 6) for _ in range(3)) * 100,
                sum(random.randint(1, 6) for _ in range(2)) * 10,
            )
            roll = random.randint(1, 100)
            if 37 <= roll <= 78:
                items.append(f"10gp Gem (x{random.randint(2, 12)})")
            elif 79 <= roll <= 100:
                items.append(f"25gp Art Object (x{random.randint(2, 8)})")
            if roll > 50:
                items.append("Potion of Healing")
            if roll > 90:
                items.append(random.choice(["+1 Weapon", "Bag of Holding", "Wand of Magic Missiles"]))
        elif cr <= 10:
            cp, sp = sum(random.randint(1, 6) for _ in range(2)) * 100, sum(random.randint(1, 6) for _ in range(2)) * 1000
            gp, pp = sum(random.randint(1, 6) for _ in range(6)) * 100, sum(random.randint(1, 6) for _ in range(3)) * 10
            roll = random.randint(1, 100)
            if 29 <= roll <= 68:
                items.append(f"50gp Gem (x{random.randint(3, 18)})")
            elif 69 <= roll <= 100:
                items.append(f"250gp Art Object (x{random.randint(2, 8)})")
            if roll > 60:
                items.append(random.choice(["Potion of Greater Healing", "Scroll of Fireball"]))
            if roll > 80:
                items.append(random.choice(["+2 Weapon", "Cloak of Displacement", "Ring of Protection"]))
        else:  # 11+
            gp, pp = sum(random.randint(1, 6) for _ in range(4)) * 1000, sum(random.randint(1, 6) for _ in range(5)) * 100
            items.append(random.choice(["1000gp Gem", "2500gp Art Object"]) + f" (x{random.randint(1, 4)})")
            items.append(random.choice(["+3 Weapon", "Staff of Power", "Robe of the Archmagi"]))

    loot_str = []
    for cur, name in [(pp, "pp"), (gp, "gp"), (ep, "ep"), (sp, "sp"), (cp, "cp")]:
        if cur > 0:
            loot_str.append(f"{cur} {name}")

    res = "Loot Generated:\n- Currency: " + (", ".join(loot_str) if loot_str else "None")
    if items:
        res += "\n- Items: " + ", ".join(items)

    await write_audit_log(config["configurable"].get("thread_id"), "Rules Engine", "Generated Random Loot", res)
    return (
        f"MECHANICAL TRUTH: {res}\nDM DIRECTIVE: Describe the characters finding this loot. "
        f"If they take it, use `manage_inventory` to add the currency/items to their sheets."
    )


@tool
async def ingest_battlemap_json(map_json_str: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    Parses a complete JSON map payload (generated by the Vision Map Ingestion model)
    and natively bulk-loads all walls, terrain, and lights into the Spatial Engine.
    """
    import json
    from server.spatial_engine import MapData

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