# flake8: noqa: W293, E203
"""
item_tools - Inventory and equipment tools
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
        # REQ-ARM-001: Heavy armor speed penalty if Str requirement not met
        if item and isinstance(item, ArmorItem) and target_slot == "armor":
            pc_str = yaml_data.get("strength", yaml_data.get("str", 10)) if yaml_data else 10
            if int(pc_str) < item.strength_requirement:
                engine_creature.speed = max(0, engine_creature.speed - 10)

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
            slot_key = next(
                (
                    k
                    for k in resources.keys()
                    if f"level {slot_level}" in k.lower()
                    or f"{slot_level}st level" in k.lower()
                    or f"{slot_level}nd level" in k.lower()
                    or f"{slot_level}rd level" in k.lower()
                    or f"{slot_level}th level" in k.lower()
                ),
                None,
            )

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



__all__ = [
    "equip_item",
    "attune_item",
    "use_expendable_resource",
    "use_font_of_magic",
    "manage_inventory",
    "generate_random_loot",
    "encode_new_compendium_entry",
]
