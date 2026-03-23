# flake8: noqa: W293, E203
"""
combat_tools - Combat and action tools - attack, damage, conditions
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
async def execute_melee_attack(
    attacker_name: str,
    target_name: str,
    advantage: bool = False,
    disadvantage: bool = False,
    is_reaction: bool = False,
    is_legendary_action: bool = False,
    is_opportunity_attack: bool = False,
    is_offhand: bool = False,
    manual_roll_total: int = None,
    is_critical: bool = False,
    force_auto_roll: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    STRICT REQUIREMENT: Use this tool to resolve ANY melee attack between two entities.
    Do NOT hallucinate dice rolls or damage. The engine will calculate hit/miss and exact damage.
    Set is_reaction=True for Reactions or Readied Actions. Set is_opportunity_attack=True for Opportunity Attacks.
    Set is_legendary_action=True for Legendary Actions.
    Set is_offhand=True for a Two-Weapon Fighting off-hand Bonus Action attack (REQ-WPN-003) — suppresses ability
    modifier on damage (unless negative). The off-hand weapon must have the Light property.
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
    # REQ-GEO-011: Reach from bounding-box edge — eff_reach = weapon_reach + attacker_radius + target_radius
    eff_reach = base_reach + (attacker.size / 2.0) + (target.size / 2.0)

    if dist >= eff_reach:
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

    if "controlled_mount" in attacker.tags:
        return (
            f"SYSTEM ERROR: {attacker.name} is a controlled mount. "
            f"It can ONLY take the Dash, Disengage, or Dodge actions (REQ-MNT-003)."
        )

    # REQ-WPN-003: Off-hand attack requires a Light weapon
    if is_offhand and isinstance(attacker, Creature) and attacker.equipped_weapon_uuid:
        offhand_weapon = get_entity(attacker.equipped_weapon_uuid, vault_path)
        if offhand_weapon and hasattr(offhand_weapon, "properties"):
            if WeaponProperty.LIGHT not in offhand_weapon.properties:
                return (
                    f"SYSTEM ERROR: {attacker.name}'s equipped weapon ({offhand_weapon.name}) does not have the "
                    f"Light property. Two-Weapon Fighting off-hand attacks require a Light weapon (REQ-WPN-003)."
                )

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
            "suppress_ability_mod_damage": is_offhand,
        },
    )

    result = await EventBus.adispatch(event)

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
    instant_death_threshold: int = None,
    disintegrate_if_zero: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Use this tool to apply guaranteed damage (traps, falling, auto-hit spells) or healing (potions, healing spells).
    Provide a negative hp_change for damage, positive for healing.
    Specify damage_type (e.g., 'fire', 'bludgeoning', 'falling') so the engine can check resistances.
    Use `instant_death_threshold` for spells like Power Word Kill.
    Use `disintegrate_if_zero` for spells like Disintegrate.
    """
    vault_path = config["configurable"].get("thread_id")
    target = await _get_entity_by_name(target_name, vault_path)
    if not target:
        return f"SYSTEM ERROR: Target '{target_name}' not found."

    current_hp = target.hp.base_value

    if instant_death_threshold is not None:
        if current_hp <= instant_death_threshold:
            target.hp.base_value = 0
            target.active_conditions = [
                c for c in target.active_conditions if c.name not in ["Dying", "Stable", "Unconscious"]
            ]
            if not any(c.name == "Dead" for c in target.active_conditions):
                target.active_conditions.append(ActiveCondition(name="Dead"))
            return f"MECHANICAL TRUTH: {target.name} had {current_hp} HP (<= {instant_death_threshold}) and was instantly killed by {reason}!"
        else:
            return f"MECHANICAL TRUTH: {target.name} has {current_hp} HP (> {instant_death_threshold}) and was unaffected by {reason}."

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
            if disintegrate_if_zero:
                target.hp.base_value = 0
                target.active_conditions = [c for c in target.active_conditions if c.name not in ["Dying", "Stable"]]
                if not any(c.name == "Dead" for c in target.active_conditions):
                    target.active_conditions.append(ActiveCondition(name="Dead"))
                if not any(c.name == "Dust" for c in target.active_conditions):
                    target.active_conditions.append(ActiveCondition(name="Dust"))
                result_msg += f"\nSYSTEM ALERT: {target.name} drops to 0 HP and is turned to DUST (Instantly Killed)!"
            elif (current_hp - damage) <= -target.max_hp:
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
            await EventBus.adispatch(GameEvent(event_type="DropConcentration", source_uuid=target.entity_uuid))
            result_msg += (
                f"\nSYSTEM ALERT: {target.name} dropped to 0 HP and lost concentration " f"on '{target.concentrating_on}'."
            )

    if target.hp.base_value > 0:
        target.active_conditions = [
            c
            for c in target.active_conditions
            if c.name not in ["Dying", "Stable"] and not (c.name == "Unconscious" and c.source_name in ["0 HP", "Unknown"])
        ]
        target.death_saves_successes = 0
        target.death_saves_failures = 0
        if hp_change > 0 and current_hp <= 0:
            result_msg += f"\nSYSTEM ALERT: {target.name} is healed from 0 HP! They regain consciousness."

    return result_msg



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
    command_invokes_self_harm: bool = False,
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

    if "controlled_mount" in origin_ent.tags:
        return (
            f"SYSTEM ERROR: {origin_ent.name} is a controlled mount. "
            f"It can ONLY take the Dash, Disengage, or Dodge actions (REQ-MNT-003)."
        )

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
        caster_conds = [c.name.lower() for c in getattr(caster, "active_conditions", [])]
        caster_tags = getattr(caster, "tags", [])
        is_underwater_no_breath = (
            (
                any(t in caster_tags for t in ["underwater", "submerged"])
                or any(t in caster_conds for t in ["underwater", "submerged"])
            )
            and "water_breathing" not in caster_tags
            and not any(c == "water breathing" for c in caster_conds)
        )
        if v_req and (any(c in caster_conds for c in ["silenced", "gagged"]) or is_underwater_no_breath):
            reason = (
                "Silenced/Gagged"
                if any(c in caster_conds for c in ["silenced", "gagged"])
                else "submerged underwater without Water Breathing"
            )
            vsm_error = f"SYSTEM ERROR: {caster.name} cannot cast '{ability_display_name}' because it requires Verbal (V) components and they are {reason}. (REQ-SND-001)"

        if not vsm_error and v_req:
            # REQ-SND-002: Verbal components break stealth
            hidden_conds = [
                c
                for c in getattr(origin_ent, "active_conditions", [])
                if c.name.lower() in ["hidden", "invisible"] and c.source_name in ["Hide Action", "Manual", "Unknown"]
            ]
            if hidden_conds:
                for c in hidden_conds:
                    origin_ent.active_conditions.remove(c)
                mitigation_notes += f"\nSYSTEM ALERT: {origin_ent.name} spoke a Verbal component and lost their Invisible/Hidden status (REQ-SND-002)!"

        # REQ-ENC-002: Harmful Commands
        if command_invokes_self_harm and spell_def and spell_def.school.lower() == "enchantment":
            vsm_error = f"SYSTEM ERROR: {caster.name} commanded the target to harm themselves. Spells like Command or Suggestion automatically fail and terminate if the command invokes direct self-harm! (REQ-ENC-002)"

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
    range_val = getattr(spell_def, "range_str", "").lower() if spell_def else ""

    if aoe_shape and aoe_size and (target_x is not None and target_y is not None or "self" in range_val):
        shape = aoe_shape.lower()
        tz = target_z

        if "self" in range_val:
            ox, oy, oz = origin_ent.x, origin_ent.y, origin_ent.z
            if shape in ["circle", "sphere", "cylinder", "cube"]:
                tx, ty, tz = ox, oy, oz
            else:
                tx = target_x if target_x is not None else origin_ent.x
                ty = target_y if target_y is not None else origin_ent.y
                if tz is None:
                    tz = oz
        else:
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

        if "self" in range_val and origin_ent.entity_uuid in target_uuids:
            if isinstance(mechanics_dump, dict) and mechanics_dump.get("exclude_self", True):
                target_uuids.remove(origin_ent.entity_uuid)

        target_wall_ids.extend(walls)
        target_terrain_ids.extend(terrains)
        if "self" in range_val:
            target_string = f"{aoe_size}ft {shape} originating from {origin_ent.name}"
        else:
            target_string = f"{aoe_size}ft {shape} at coordinates ({target_x}, {target_y})"
    else:
        for name in target_names or []:
            ent = await _get_entity_by_name(name, vault_path)
            if ent:
                if "touch" in range_val:
                    dist = spatial_service.calculate_distance(
                        origin_ent.x, origin_ent.y, origin_ent.z, ent.x, ent.y, ent.z, vault_path
                    )
                    if dist > 7.5:
                        return f"SYSTEM ERROR: {ent.name} is out of Touch range ({dist:.1f}ft > 5ft). (REQ-SPL-020)"

                    is_caster_pc = any(t in origin_ent.tags for t in ["pc", "player", "party_npc"])
                    is_target_pc = any(t in ent.tags for t in ["pc", "player", "party_npc"])
                    is_unwilling = is_caster_pc != is_target_pc

                    if is_unwilling:
                        requires_attack_roll = True
                        if isinstance(mechanics_dump, dict):
                            mechanics_dump["requires_attack_roll"] = True

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

    # Resource gate: validate and reserve resource_cost before dispatch
    resource_cost_str = mechanics_dump.get("resource_cost", "") if isinstance(mechanics_dump, dict) else ""
    resource_name_to_deduct = None
    resource_amount_to_deduct = 0
    if resource_cost_str:
        import re as _re

        rc_match = _re.match(r"^(.+):(\d+)$", resource_cost_str.strip())
        if rc_match:
            resource_name_to_deduct = rc_match.group(1).strip()
            resource_amount_to_deduct = int(rc_match.group(2))
            current_res = caster.resources.get(resource_name_to_deduct, "")
            rm = _re.match(r"(\d+)\s*/\s*(\d+)", str(current_res))
            if not rm:
                return (
                    f"SYSTEM ERROR: {caster.name} does not have the resource '{resource_name_to_deduct}' "
                    f"required for {ability_name}. Add it via update_entity_stats first."
                )
            available = int(rm.group(1))
            if available < resource_amount_to_deduct:
                return (
                    f"SYSTEM ERROR: {caster.name} has insufficient '{resource_name_to_deduct}' "
                    f"({available}/{rm.group(2)}) to use {ability_name} (costs {resource_amount_to_deduct})."
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

    result = await EventBus.adispatch(event)

    results_list = result.payload.get("results", [])

    if is_spell and requires_slot and not is_reaction and not is_legendary_action and result.status != EventStatus.CANCELLED:
        caster.spell_slots_expended_this_turn += 1

    # Deduct resource_cost after successful dispatch
    if resource_name_to_deduct and result.status != EventStatus.CANCELLED:
        import re as _re

        current_res = caster.resources.get(resource_name_to_deduct, "")
        rm = _re.match(r"(\d+)\s*/\s*(\d+)", str(current_res))
        if rm:
            new_val = max(0, int(rm.group(1)) - resource_amount_to_deduct)
            caster.resources[resource_name_to_deduct] = f"{new_val}/{rm.group(2)}"

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
                await EventBus.adispatch(
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
                # Lazy import to avoid circular dependency
                from spatial_tools import move_entity
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

        # REQ-MNT-006: Mount knocks rider Prone unless they have a reaction
        if cond_lower == "prone" and engine_creature:
            riders = [
                e
                for e in get_all_entities(vault_path).values()
                if isinstance(e, Creature) and getattr(e, "mounted_on_uuid", None) == engine_creature.entity_uuid
            ]
            for rider in riders:
                rider.mounted_on_uuid = None
                if not rider.reaction_used:
                    rider.reaction_used = True
                    result_msg += f"\nSYSTEM ALERT (REQ-MNT-006): {engine_creature.name} fell Prone! {rider.name} used their Reaction to safely dismount and land on their feet."
                else:
                    if not any(c.name.lower() == "prone" for c in rider.active_conditions):
                        rider.active_conditions.append(ActiveCondition(name="Prone", source_name="Mount Fell"))
                    result_msg += f"\nSYSTEM ALERT (REQ-MNT-006): {engine_creature.name} fell Prone! {rider.name} had no Reaction available and fell Prone!"

        # REQ-MNT-005: Rider knocked Prone triggers DC 10 save to stay mounted
        if cond_lower == "prone" and getattr(engine_creature, "mounted_on_uuid", None):
            roll = random.randint(1, 20)
            total = roll + engine_creature.dexterity_mod.total
            if total < 10:
                engine_creature.mounted_on_uuid = None
                result_msg += f"\nSYSTEM ALERT (REQ-MNT-005): {character_name} was knocked Prone while mounted. Failed DC 10 Dex save ({total}) and fell off!"
            else:
                result_msg += f"\nSYSTEM ALERT (REQ-MNT-005): {character_name} was knocked Prone while mounted, but succeeded the DC 10 Dex save ({total}) to stay on!"

        elif cond_lower in ["dazed", "confused"]:
            result_msg += (
                f"\nSYSTEM ALERT: '{condition_name.capitalize()}' restricts actions and movement. "
                f"Review the specific ability rules."
            )
        elif cond_lower == "low oxygen":
            # REQ-ENV-003: Low Oxygen Environment (smoke, altitude, thin air, etc.)
            result_msg += (
                f"\nSYSTEM ALERT (REQ-ENV-003): {character_name} is in a low-oxygen environment. "
                f"Each StartOfTurn they will begin tracking Breath Hold (same as underwater suffocation rules). "
                f"Constructs, undead, and creatures with Water Breathing or similar traits are immune — "
                f"do NOT apply this condition to them."
            )

    return result_msg



@tool
async def execute_grapple_or_shove(
    attacker_name: str,
    target_name: str,
    action_type: str,
    shove_type: str = "prone",
    throw_distance: float = 10.0,
    advantage: bool = False,
    disadvantage: bool = False,
    manual_roll_total: int = None,
    force_auto_roll: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Resolves a Grapple, Shove, Throw, or Escape. action_type must be 'grapple', 'shove', 'throw', or 'escape'.
    - For grapple/shove/throw: Target makes a Save vs Attacker's DC.
    - For escape: Attacker (escaping entity) makes a Check vs Target's (grappler's) DC.
    """
    vault_path = config["configurable"].get("thread_id")
    attacker = await _get_entity_by_name(attacker_name, vault_path)
    target = await _get_entity_by_name(target_name, vault_path)

    if not attacker or not target:
        return "SYSTEM ERROR: Attacker or Target not found in active memory."

    if action_type.lower() == "escape":
        # REQ-SKL-009: Escaping a grapple is an Acrobatics or Athletics check against the grappler's static Escape DC
        grappler = target
        escaper = attacker

        char_level = grappler.character_level if hasattr(grappler, "character_level") and grappler.character_level > 0 else 1
        prof_bonus = max(2, (char_level - 1) // 4 + 2)
        escape_dc = 8 + grappler.strength_mod.total + prof_bonus

        is_pc = any(t in escaper.tags for t in ["pc", "player"])
        if is_pc and not force_auto_roll and manual_roll_total is None:
            auto_settings = get_roll_automations(escaper.name)
            if not auto_settings.get("skill_checks", True):
                return (
                    f"SYSTEM ALERT: {escaper.name} has manual skill checks enabled. Ask the player to roll "
                    f"Acrobatics or Athletics against DC {escape_dc} and provide the total."
                )

        if manual_roll_total is not None:
            escaper_total = manual_roll_total
            log = f"Grapple Escape DC {escape_dc}: {escaper.name} manually rolled {escaper_total}. "
        else:
            tgt_roll1, tgt_roll2 = random.randint(1, 20), random.randint(1, 20)
            if advantage and not disadvantage:
                tgt_roll = max(tgt_roll1, tgt_roll2)
            elif disadvantage and not advantage:
                tgt_roll = min(tgt_roll1, tgt_roll2)
            else:
                tgt_roll = tgt_roll1

            escaper_mod = max(escaper.strength_mod.total, escaper.dexterity_mod.total)
            escaper_total = tgt_roll + escaper_mod
            log = f"Grapple Escape DC {escape_dc}: {escaper.name} rolls {tgt_roll} + {escaper_mod} = {escaper_total}. "

        if escaper_total >= escape_dc:
            log += f"Success! {escaper.name} escapes the grapple."
            await toggle_condition.ainvoke(
                {"character_name": escaper.name, "condition_name": "Grappled", "is_active": False}, config=config
            )
        else:
            log += f"Failure. {escaper.name} remains grappled."

        return f"MECHANICAL TRUTH: {log}"

    # REQ-ACT-007: 2024 update — target makes Str or Dex SAVE vs attacker's DC
    # DC = 8 + attacker STR mod + proficiency bonus (derived from character level)
    char_level = attacker.character_level if hasattr(attacker, "character_level") and attacker.character_level > 0 else 1
    prof_bonus = max(2, (char_level - 1) // 4 + 2)
    save_dc = 8 + attacker.strength_mod.total + prof_bonus

    is_pc = any(t in target.tags for t in ["pc", "player"])
    if is_pc and not force_auto_roll and manual_roll_total is None:
        auto_settings = get_roll_automations(target.name)
        if not auto_settings.get("saving_throws", True):
            return (
                f"SYSTEM ALERT: {target.name} has manual saving throws enabled. Ask the player to roll "
                f"a Strength or Dexterity Save against DC {save_dc} and provide the total."
            )

    if manual_roll_total is not None:
        tgt_total = manual_roll_total
        log = f"Grapple/Shove DC {save_dc}: {target.name} manually rolled {tgt_total}. "
    else:
        tgt_roll1, tgt_roll2 = random.randint(1, 20), random.randint(1, 20)
        if advantage and not disadvantage:
            tgt_roll = max(tgt_roll1, tgt_roll2)
        elif disadvantage and not advantage:
            tgt_roll = min(tgt_roll1, tgt_roll2)
        else:
            tgt_roll = tgt_roll1

        target_mod = max(target.strength_mod.total, target.dexterity_mod.total)
        tgt_total = tgt_roll + target_mod

        log = f"Grapple/Shove DC {save_dc}: {target.name} rolls {tgt_roll} + {target_mod} = {tgt_total}. "

    # Target fails the save (total < DC) → attacker succeeds
    if tgt_total < save_dc:
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
                # Lazy import to avoid circular dependency
                from spatial_tools import move_entity
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
        log += f"Defender succeeds (rolled {tgt_total} vs DC {save_dc})! Nothing happens."

    return f"MECHANICAL TRUTH: {log}"



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
                    await EventBus.adispatch(
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
    await EventBus.adispatch(event)

    return (
        f"MECHANICAL TRUTH: {entity.name} dropped concentration on {spell_name}. " "All associated effects have been cleared."
    )



@tool
async def manage_mount(
    rider_name: str,
    mount_name: str = "",
    action: str = "mount",
    is_controlled: bool = True,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Handles mounting and dismounting a creature (REQ-MNT-001 to REQ-MNT-004).
    Costs half of the rider's speed.
    If is_controlled=True, the mount's initiative changes to match the rider and it can only Dash/Disengage/Dodge.
    """
    vault_path = config["configurable"].get("thread_id")
    rider = await _get_entity_by_name(rider_name, vault_path)
    if not rider:
        return f"SYSTEM ERROR: Rider '{rider_name}' not found."

    move_cost = max(1, rider.speed // 2)

    if action.lower() == "mount":
        if not mount_name:
            return "SYSTEM ERROR: Must provide mount_name."
        mount = await _get_entity_by_name(mount_name, vault_path)
        if not mount:
            return f"SYSTEM ERROR: Mount '{mount_name}' not found."

        dist = spatial_service.calculate_distance(rider.x, rider.y, rider.z, mount.x, mount.y, mount.z, vault_path)
        if dist > (mount.size / 2 + 5.0):
            return "SYSTEM ERROR: Mount is too far away to mount."

        if rider.movement_remaining < move_cost:
            return f"SYSTEM ERROR: Not enough movement to mount (requires {move_cost}ft)."

        rider.movement_remaining -= move_cost
        rider.mounted_on_uuid = mount.entity_uuid

        if is_controlled:
            if "controlled_mount" not in mount.tags:
                mount.tags.append("controlled_mount")
            if "independent_mount" in mount.tags:
                mount.tags.remove("independent_mount")
        else:
            if "independent_mount" not in mount.tags:
                mount.tags.append("independent_mount")
            if "controlled_mount" in mount.tags:
                mount.tags.remove("controlled_mount")

        rider.x, rider.y, rider.z = mount.x, mount.y, mount.z
        spatial_service.sync_entity(rider)

        combat_file = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")
        if is_controlled and os.path.exists(combat_file):
            try:
                async with edit_markdown_entity(combat_file) as state:
                    yaml_data = state["yaml_data"]
                    combatants = yaml_data.get("combatants", [])
                    rider_init = None
                    mount_idx = -1
                    for i, c in enumerate(combatants):
                        if c["name"].lower() == rider.name.lower():
                            rider_init = c["init"]
                        if c["name"].lower() == mount.name.lower():
                            mount_idx = i

                    if rider_init is not None and mount_idx != -1:
                        combatants[mount_idx]["init"] = float(rider_init)
                        yaml_data["combatants"] = sorted(combatants, key=lambda x: float(x["init"]), reverse=True)
            except Exception:
                pass

        return f"MECHANICAL TRUTH: {rider.name} mounted {mount.name}. Movement cost: {move_cost}ft."

    elif action.lower() == "dismount":
        if not getattr(rider, "mounted_on_uuid", None):
            return "SYSTEM ERROR: Rider is not mounted."
        if rider.movement_remaining < move_cost:
            return f"SYSTEM ERROR: Not enough movement to dismount (requires {move_cost}ft)."

        rider.movement_remaining -= move_cost
        rider.mounted_on_uuid = None
        return f"MECHANICAL TRUTH: {rider.name} dismounted. Movement cost: {move_cost}ft."

    return "SYSTEM ERROR: Invalid action. Use 'mount' or 'dismount'."



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
    # Explicit registration (BaseGameEntity no longer auto-registers)
    register_entity(trap_source, vault_path)

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

    result = await EventBus.adispatch(event)
    BaseGameEntity.remove(trap_source.entity_uuid)

    results_list = result.payload.get("results", [])
    if results_list:
        return f"MECHANICAL TRUTH: {hazard_name} triggered!\n" + "\n".join(results_list)

    return f"MECHANICAL TRUTH: {hazard_name} triggered but had no effect."



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



__all__ = [
    "execute_melee_attack",
    "modify_health",
    "use_ability_or_spell",
    "toggle_condition",
    "execute_grapple_or_shove",
    "use_dash_action",
    "ready_action",
    "clear_readied_action",
    "drop_concentration",
    "manage_mount",
    "trigger_environmental_hazard",
    "interact_with_object",
    "evaluate_extreme_weather",
]
