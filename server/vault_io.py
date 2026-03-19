import os
import re
import uuid
import yaml
import math
import asyncio
import aiofiles
from filelock.asyncio import AsyncSoftFileLock
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool, InjectedToolArg
from typing import Annotated, Optional
from contextlib import asynccontextmanager
import glob
from server.dnd_rules_engine import (
    Creature,
    ModifiableValue,
    MeleeWeapon,
    NumericalModifier,
    ModifierPriority,
    ActiveCondition,
    parse_duration_to_seconds,
)
from server.compendium_manager import CompendiumManager
from server.spatial_engine import spatial_service
from server.registry import clear_registry, get_all_entities
from server.item_system import ItemCompendium, WeaponItem
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
from prompts import VISION_MAP_INGESTION_PROMPT
import base64
import json


async def auto_ingest_maps_from_vault(vault_path: str):
    """
    Scans the vault for images that look like battlemaps.
    If a .json sidecar doesn't exist, it uses the Vision API to extract geometry.
    """
    print("Scanning vault for un-processed battlemaps...")
    vision_llm = None

    search_pattern = os.path.join(vault_path, "**", "*")
    for filepath in glob.glob(search_pattern, recursive=True):
        ext = os.path.splitext(filepath)[1].lower()
        if ext in [".png", ".jpg", ".jpeg"] and "map" in os.path.basename(filepath).lower():
            json_sidecar = f"{filepath}.json"

            if not os.path.exists(json_sidecar):
                print(f"[Vision AI] Processing new map: {os.path.basename(filepath)}...")
                if not vision_llm:
                    vision_llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.1)

                try:
                    with open(filepath, "rb") as image_file:
                        image_data = base64.b64encode(image_file.read()).decode("utf-8")

                    message = HumanMessage(
                        content=[
                            {"type": "text", "text": VISION_MAP_INGESTION_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/{ext[1:]};base64,{image_data}"},
                            },
                        ]
                    )

                    response = await vision_llm.ainvoke([message])

                    # Clean markdown formatting from JSON output
                    clean_json = response.content.strip()
                    if clean_json.startswith("```json"):
                        clean_json = clean_json[7:]
                    elif clean_json.startswith("```"):
                        clean_json = clean_json[3:]
                    if clean_json.endswith("```"):
                        clean_json = clean_json[:-3]

                    map_dict = json.loads(clean_json.strip())

                    # Attach the image paths
                    map_dict["map_name"] = os.path.basename(filepath)
                    if "player" in filepath.lower():
                        map_dict["player_map_image_path"] = filepath
                    else:
                        map_dict["dm_map_image_path"] = filepath

                    with open(json_sidecar, "w", encoding="utf-8") as f:
                        json.dump(map_dict, f, indent=4)

                    print(f"   -> Successfully extracted geometry to {os.path.basename(json_sidecar)}")
                except Exception as e:
                    print(f"   -> Failed to process {os.path.basename(filepath)}: {e}")

            # Auto-load the first map we find into the active engine just for initialization
            if not spatial_service.map_data.walls and os.path.exists(json_sidecar):
                try:
                    from server.spatial_engine import MapData

                    with open(json_sidecar, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    spatial_service.load_map(MapData(**data))
                    print(f"Loaded {os.path.basename(filepath)} into active spatial memory.")
                except Exception:
                    pass


async def load_entity_into_engine(filepath: str, vault_path: str) -> Optional[Creature]:
    """Parses a specific markdown file and hydrates it into the engine registry."""
    try:
        yaml_data, _ = await read_markdown_entity_no_lock(filepath)
        if not yaml_data:
            return None

        tags = yaml_data.get("tags", [])
        # Only load entities that have stats
        if not any(t in tags for t in ["pc", "npc", "monster", "creature"]):
            return None

        summoned_by_uuid_str = yaml_data.get("summoned_by_uuid")
        summoned_by_uuid = uuid.UUID(summoned_by_uuid_str) if summoned_by_uuid_str else None
        summon_spell = yaml_data.get("summon_spell", "")

        entity = Creature(
            vault_path=vault_path,
            name=yaml_data.get("name", os.path.basename(filepath).replace(".md", "")),
            x=float(yaml_data.get("x", 0.0)),
            y=float(yaml_data.get("y", 0.0)),
            z=float(yaml_data.get("z", 0.0)),
            icon_url=yaml_data.get("icon_url", ""),
            height=float(yaml_data.get("height", yaml_data.get("size", 5.0))),
            max_hp=int(yaml_data.get("max_hp", yaml_data.get("hp", 10))),
            hp=ModifiableValue(base_value=yaml_data.get("hp", 10)),
            temp_hp=int(yaml_data.get("temp_hp", 0)),
            ac=ModifiableValue(base_value=yaml_data.get("ac", 10)),
            strength_mod=ModifiableValue(
                base_value=yaml_data.get(
                    "strength_mod", math.floor((yaml_data.get("strength", yaml_data.get("str", 10)) - 10) / 2)
                )
            ),
            dexterity_mod=ModifiableValue(
                base_value=yaml_data.get(
                    "dexterity_mod", math.floor((yaml_data.get("dexterity", yaml_data.get("dex", 10)) - 10) / 2)
                )
            ),
            constitution_mod=ModifiableValue(
                base_value=yaml_data.get(
                    "constitution_mod", math.floor((yaml_data.get("constitution", yaml_data.get("con", 10)) - 10) / 2)
                )
            ),
            intelligence_mod=ModifiableValue(
                base_value=yaml_data.get(
                    "intelligence_mod", math.floor((yaml_data.get("intelligence", yaml_data.get("int", 10)) - 10) / 2)
                )
            ),
            wisdom_mod=ModifiableValue(
                base_value=yaml_data.get(
                    "wisdom_mod", math.floor((yaml_data.get("wisdom", yaml_data.get("wis", 10)) - 10) / 2)
                )
            ),
            charisma_mod=ModifiableValue(
                base_value=yaml_data.get(
                    "charisma_mod", math.floor((yaml_data.get("charisma", yaml_data.get("cha", 10)) - 10) / 2)
                )
            ),
            spell_save_dc=ModifiableValue(base_value=yaml_data.get("spell_save_dc", 10)),
            spell_attack_bonus=ModifiableValue(base_value=int(str(yaml_data.get("spell_atk", "0")).replace("+", ""))),
            active_mechanics=yaml_data.get("active_mechanics", []),
            resources=yaml_data.get("resources", {}),
            active_conditions=(
                [ActiveCondition(**c) for c in yaml_data.get("active_conditions", [])]
                if isinstance(yaml_data.get("active_conditions", []), list)
                else []
            ),
            concentrating_on=yaml_data.get("concentrating_on", ""),
            reaction_used=bool(yaml_data.get("reaction_used", False)),
            legendary_actions_max=int(yaml_data.get("legendary_actions_max", yaml_data.get("legendary_actions", 0))),
            legendary_actions_current=int(yaml_data.get("legendary_actions_current", yaml_data.get("legendary_actions", 0))),
            speed=int(yaml_data.get("speed", 30)),
            movement_remaining=int(yaml_data.get("movement_remaining", yaml_data.get("speed", 30))),
            wild_shape_hp=int(yaml_data.get("wild_shape_hp", 0)),
            wild_shape_max_hp=int(yaml_data.get("wild_shape_max_hp", 0)),
            death_saves_successes=int(yaml_data.get("death_saves_successes", 0)),
            death_saves_failures=int(yaml_data.get("death_saves_failures", 0)),
            exhaustion_level=int(yaml_data.get("exhaustion_level", 0)),
            tags=yaml_data.get("tags", []),
            summoned_by_uuid=summoned_by_uuid,
            summon_spell=summon_spell,
        )

        # Bridge: Initialize and Equip the Object-Oriented Weapon
        equipment = yaml_data.get("equipment", {})
        main_hand = equipment.get("main_hand", "Unarmed")

        dmg_dice = "1d4" if "Unarmed" in main_hand else "1d8"
        dmg_type = "bludgeoning" if "Unarmed" in main_hand else "slashing"

        weapon = MeleeWeapon(name=main_hand, damage_dice=dmg_dice, damage_type=dmg_type, vault_path=vault_path)

        weapon_item = await ItemCompendium.load_item(vault_path, main_hand)
        if weapon_item and isinstance(weapon_item, WeaponItem):
            weapon.damage_dice = weapon_item.damage_dice
            weapon.damage_type = weapon_item.damage_type
            weapon.magic_bonus = weapon_item.magic_bonus
            weapon.mastery_name = getattr(weapon_item, "mastery_name", "")

            if weapon.mastery_name and "weapon_mastery" in entity.tags:
                mastery_entry = await CompendiumManager.get_entry(vault_path, weapon.mastery_name)
                if mastery_entry and mastery_entry.mechanics:
                    dumped = mastery_entry.mechanics.model_dump()
                    if mastery_entry.mechanics.trigger_event == "on_hit":
                        weapon.on_hit_mechanics = dumped
                    elif mastery_entry.mechanics.trigger_event == "on_miss":
                        weapon.on_miss_mechanics = dumped

        entity.equipped_weapon_uuid = weapon.entity_uuid

        for mechanic_name in entity.active_mechanics:
            entry = await CompendiumManager.get_entry(vault_path, mechanic_name)
            if entry and entry.mechanics:
                entity.tags.extend(entry.mechanics.granted_tags)
                for mod_data in entry.mechanics.modifiers:
                    target_stat = mod_data.get("stat")
                    duration_secs = parse_duration_to_seconds(mod_data.get("duration", "-1"))

                    if hasattr(entity, target_stat):
                        stat_obj = getattr(entity, target_stat)
                        if isinstance(stat_obj, ModifiableValue):
                            stat_obj.add_modifier(
                                NumericalModifier(
                                    priority=ModifierPriority.ADDITIVE,
                                    value=int(mod_data.get("value", 0)),
                                    source_name=mechanic_name,
                                    duration_seconds=duration_secs,
                                )
                            )

        entity._filepath = filepath
        print(f"Loaded to Engine: {entity.name} (HP: {entity.hp.base_value})")

        spatial_service.sync_entity(entity)
        return entity
    except Exception as e:
        print(f"Failed to load entity {filepath}: {e}")
        return None


async def initialize_engine_from_vault(vault_path: str):
    """Reads characters and active combatants from the vault into the Deterministic Engine. Employs lazy hydration for everything else."""
    await auto_ingest_maps_from_vault(vault_path)

    print("Loading core entities into Deterministic Engine...")
    clear_registry()  # Reset memory for the new turn

    j_dir = get_journals_dir(vault_path)
    if not os.path.exists(j_dir):
        return

    # 1. Determine active combatants to eagerly load
    active_combatants = set()
    combat_file = os.path.join(j_dir, "ACTIVE_COMBAT.md")
    if os.path.exists(combat_file):
        try:
            yaml_data, _ = await read_markdown_entity_no_lock(combat_file)
            for c in yaml_data.get("combatants", []):
                active_combatants.add(c.get("name", "").lower())
        except Exception:
            pass

    # 2. Quick scan of Journals to load PCs and active combatants
    for filename in os.listdir(j_dir):
        if not filename.endswith(".md"):
            continue
        if filename in ["ACTIVE_COMBAT.md", "CAMPAIGN_MASTER.md", "DM_CONFIG.md"]:
            continue

        filepath = os.path.join(j_dir, filename)
        entity_name = filename[:-3].lower()

        if entity_name in active_combatants:
            await load_entity_into_engine(filepath, vault_path)
            continue

        try:
            async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
                content = await f.read(250)  # Just enough to grab tags
                if "tags:" in content and ("pc" in content.lower() or "player" in content.lower()):
                    await load_entity_into_engine(filepath, vault_path)
        except Exception:
            pass


async def sync_engine_to_vault(vault_path: str):
    """Writes current Engine state (HP, etc.) back to the Obsidian files."""
    print("Syncing Engine state back to Vault...")
    for uid, entity in get_all_entities(vault_path).items():
        if not hasattr(entity, "_filepath"):
            continue

        filepath = entity._filepath
        try:
            yaml_data, markdown_body = await read_markdown_entity_no_lock(filepath)

            # Update the YAML with the new deterministic values
            if isinstance(entity, Creature):
                yaml_data["hp"] = entity.hp.base_value
                yaml_data["temp_hp"] = entity.temp_hp
                yaml_data["ac"] = entity.ac.base_value
                yaml_data["x"] = entity.x
                yaml_data["y"] = entity.y
                yaml_data["z"] = entity.z
                yaml_data["height"] = entity.height
                if entity.icon_url:
                    yaml_data["icon_url"] = entity.icon_url
                if entity.resources:
                    yaml_data["resources"] = entity.resources
                if entity.active_conditions:
                    yaml_data["active_conditions"] = [c.model_dump(exclude={"condition_id"}) for c in entity.active_conditions]
                elif "active_conditions" in yaml_data:
                    yaml_data["active_conditions"] = []
            yaml_data["concentrating_on"] = entity.concentrating_on
            yaml_data["reaction_used"] = entity.reaction_used
            yaml_data["legendary_actions_max"] = entity.legendary_actions_max
            yaml_data["legendary_actions_current"] = entity.legendary_actions_current
            yaml_data["speed"] = entity.speed
            yaml_data["movement_remaining"] = entity.movement_remaining
            yaml_data["wild_shape_hp"] = entity.wild_shape_hp
            yaml_data["wild_shape_max_hp"] = entity.wild_shape_max_hp
            yaml_data["death_saves_successes"] = entity.death_saves_successes
            yaml_data["death_saves_failures"] = entity.death_saves_failures
            yaml_data["exhaustion_level"] = entity.exhaustion_level
            if entity.summoned_by_uuid:
                yaml_data["summoned_by_uuid"] = str(entity.summoned_by_uuid)
                yaml_data["summon_spell"] = entity.summon_spell

            # Reconstruct the file
            new_yaml_str = await asyncio.to_thread(yaml.dump, yaml_data, sort_keys=False)
            new_content = f"---\n{new_yaml_str}---\n{markdown_body}"

            async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                await f.write(new_content)
        except Exception as e:
            print(f"Failed to save {entity.name}: {e}")


def get_journals_dir(vault_path: str):
    """Helper to dynamically locate or create the Journals folder for the active campaign."""
    j_dir = os.path.join(vault_path, "server", "Journals")
    os.makedirs(j_dir, exist_ok=True)
    return j_dir


async def read_markdown_entity_no_lock(file_path: str) -> tuple[dict, str]:
    """Reads a markdown file, safely parses YAML. Must be called within an AsyncSoftFileLock."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Error: Could not locate '{os.path.basename(file_path)}'.")

    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
        content = await f.read()

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                yaml_data = await asyncio.to_thread(yaml.safe_load, parts[1]) or {}
                return yaml_data, parts[2]
            except yaml.YAMLError as e:
                raise ValueError(f"Error: YAML syntax issue in {os.path.basename(file_path)}.") from e
    return {}, content


async def write_markdown_entity_no_lock(file_path: str, yaml_data: dict, body_text: str):
    """Writes a markdown file with YAML frontmatter. Must be called within an AsyncSoftFileLock."""
    new_yaml_str = await asyncio.to_thread(yaml.dump, yaml_data, sort_keys=False, default_flow_style=False)
    if body_text.startswith("\n"):
        body_text = body_text[1:]
    new_content = f"---\n{new_yaml_str}---\n{body_text}"

    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
        await f.write(new_content)


@asynccontextmanager
async def read_markdown_entity(file_path: str):
    """Async context manager that locks a markdown file and yields (yaml_data, body_text) for reading."""
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        yield yaml_data, body_text


@asynccontextmanager
async def edit_markdown_entity(file_path: str):
    """
    Async context manager that locks a markdown file, yields a mutable state dict,
    and automatically writes it back when the block exits.
    Set state['save'] = False to abort writing.
    """
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        state = {"yaml_data": yaml_data, "body_text": body_text, "save": True}

        yield state

        if state.get("save", True):
            await write_markdown_entity_no_lock(file_path, state["yaml_data"], state["body_text"])


async def write_audit_log(vault_path: str, agent_name: str, action: str, details: str):
    log_path = os.path.join(vault_path, "server", "Journals", "AUDIT_LOG.md")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    if not os.path.exists(log_path):
        async with aiofiles.open(log_path, "w", encoding="utf-8") as f:
            await f.write("---\ntags: [system, audit]\n---\n# AI DM Audit Trail\n\n")

    async with aiofiles.open(log_path, "a", encoding="utf-8") as f:
        await f.write(f"**[{agent_name}]** - *{action}*\n> {details}\n\n")


@tool
async def upsert_journal_section(  # noqa: C901
    entity_name: str,
    section_header: str,
    content: str,
    mode: str = "append",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Safely edits a specific section of a Markdown journal without breaking Obsidian formatting or code blocks."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{entity_name}.md")

    if not os.path.exists(file_path):
        return f"Error: Could not locate '{entity_name}.md'. Ensure the entity exists."

    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            lines = await f.readlines()

        out_lines = []
        in_target_section = False
        section_found = False
        target_depth = 0

        # --- STATE TRACKERS ---
        in_frontmatter = False
        in_code_block = False

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # 1. Toggle Frontmatter State
            if i == 0 and stripped == "---":
                in_frontmatter = True
                out_lines.append(line)
                i += 1
                continue
            elif in_frontmatter and stripped == "---":
                in_frontmatter = False
                out_lines.append(line)
                i += 1
                continue

            # 2. Toggle Code Block State
            if stripped.startswith("```"):
                in_code_block = not in_code_block

            # 3. Header Parsing (ONLY if outside frontmatter and code blocks)
            header_match = re.match(r"^(#{1,6})\s+(.*)", stripped)

            if header_match and not in_frontmatter and not in_code_block:
                current_depth = len(header_match.group(1))
                current_title = header_match.group(2).strip()

                if in_target_section:
                    # We hit a new header. Did we exit our target section?
                    if current_depth <= target_depth:
                        if mode == "append":
                            out_lines.append(f"{content.strip()}\n\n")
                        in_target_section = False
                        out_lines.append(line)
                    else:
                        # It's a sub-header inside our target section
                        if mode == "append":
                            out_lines.append(line)
                else:
                    if current_title.lower() == section_header.lower():
                        # We found the target section!
                        in_target_section = True
                        section_found = True
                        target_depth = current_depth
                        out_lines.append(line)
                        if mode == "replace":
                            out_lines.append(f"\n{content.strip()}\n\n")
                    else:
                        out_lines.append(line)
            else:
                # Standard line processing
                if in_target_section:
                    if mode == "append":
                        out_lines.append(line)
                else:
                    out_lines.append(line)

            i += 1

        # 4. End of File Cleanup (If the target section was the very last thing in the file)
        if in_target_section and mode == "append":
            out_lines.append(f"\n{content.strip()}\n")

        if not section_found:
            return f"Error: The section heading '{section_header}' was not found in {entity_name}.md."

        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.writelines(out_lines)

    return f"Success: {mode.capitalize()}ed content to '{section_header}' in {entity_name}.md."
