# flake8: noqa: W293, E203
"""
entity_tools - Entity lifecycle tools - create, spawn, update entities
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
async def propose_entity_creation(
    entity_name: str,
    entity_type: str = "npc",
    player_description: str = "",
    proposed_by_player: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Propose a new entity for creation in the Knowledge Graph.

    Use this when narrative references an entity that doesn't exist yet.
    This creates a proposal that the DM can approve, modify, or reject.

    If approved: the entity is added to KG and EmergentWorldBuilder is triggered.
    If modified: the modified description is used instead.

    Args:
        entity_name: The proposed entity name (e.g., "Mary", "The Prancing Pony")
        entity_type: KG node type — one of: npc, location, item, faction
        player_description: What the player or narrator said about this entity
        proposed_by_player: True if a player suggested this entity (vs DM invention)
    """
    import asyncio

    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    llm = config["configurable"].get("_llm")

    # Check if entity already exists
    from registry import get_knowledge_graph
    kg = get_knowledge_graph(vault_path)
    if kg.get_node_by_name(entity_name) is not None:
        return f"SYSTEM ERROR: Entity '{entity_name}' already exists in the Knowledge Graph."

    # Build the entity with minimal info; emergent builder will flesh it out
    from knowledge_graph import KnowledgeGraphNode, GraphNodeType
    import uuid

    node = KnowledgeGraphNode(
        node_uuid=uuid.uuid4(),
        node_type=GraphNodeType(entity_type),
        name=entity_name,
        attributes={
            "description": player_description or f"Emergent entity {entity_name}.",
            "proposed_by_player": str(proposed_by_player),
        },
        is_immutable=False,  # Player-created entities are mutable by default
    )
    kg.add_node(node)

    # Trigger EmergentWorldBuilder to flesh out the entity
    if llm is not None:
        try:
            from ingestion_pipeline import EmergentWorldBuilder
            loop = asyncio.get_event_loop()

            async def _run():
                builder = EmergentWorldBuilder(llm=llm, vault_path=vault_path)
                return await builder.on_entity_created(
                    entity_name=entity_name,
                    entity_type=entity_type,
                    context=player_description,
                    player_proposed=proposed_by_player,
                )

            if loop.is_running():
                task = loop.create_task(_run())
                report = loop.run_until_complete(task)
            else:
                report = loop.run_until_complete(_run())

            return (
                f"MECHANICAL TRUTH: Entity '{entity_name}' proposed and created.\n"
                f"  Type: {entity_type}\n"
                f"  Proposed by player: {proposed_by_player}\n"
                f"  Side quest storylets generated: {report.storylets_created}\n"
                f"  World edges inferred: {report.edges_created}\n"
                f"  Description: {report.description or '(fleshed out by LLM)'}"
                + (f"\n  Warnings: {report.warnings}" if report.warnings else "")
            )
        except Exception as e:
            return (
                f"MECHANICAL TRUTH: Entity '{entity_name}' created (side quest generation failed: {e}).\n"
                f"  Type: {entity_type}. The DM should flesh out this entity manually."
            )

    return (
        f"MECHANICAL TRUTH: Entity '{entity_name}' created (no LLM — manual fleshing recommended).\n"
        f"  Type: {entity_type}. Call generate_side_quests_for_entity when LLM is available."
    )



@tool
async def generate_side_quests_for_entity(
    entity_name: str,
    quest_count: int = 3,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Generate side quest storylets for an existing KG entity.

    Use when the DM wants to flesh out an NPC's potential arcs
    without waiting for players to naturally engage with them.
    The entity must already exist in the Knowledge Graph.

    Args:
        entity_name: Name of the existing KG entity to generate quests for
        quest_count: Number of side quest stubs to generate (default 3, max 5)
    """
    import asyncio

    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    llm = config["configurable"].get("_llm")
    if llm is None:
        return (
            "SYSTEM ERROR: No LLM available in session context. "
            "Side quest generation requires an active LLM session."
        )

    from registry import get_knowledge_graph
    kg = get_knowledge_graph(vault_path)
    node = kg.get_node_by_name(entity_name)
    if node is None:
        return f"SYSTEM ERROR: Entity '{entity_name}' not found in Knowledge Graph."

    try:
        from ingestion_pipeline import EmergentWorldBuilder
        loop = asyncio.get_event_loop()

        async def _run():
            builder = EmergentWorldBuilder(llm=llm, vault_path=vault_path)
            return await builder.on_entity_created(
                entity_name=entity_name,
                entity_type=node.node_type.value,
                context=node.attributes.get("description", "") if node.attributes else "",
                player_proposed=False,
            )

        if loop.is_running():
            task = loop.create_task(_run())
            report = loop.run_until_complete(task)
        else:
            report = loop.run_until_complete(_run())

        return (
            f"MECHANICAL TRUTH: Side quests generated for '{entity_name}'.\n"
            f"  Storylets created: {report.storylets_created}\n"
            f"  World edges inferred: {report.edges_created}"
            + (f"\n  Warnings: {report.warnings}" if report.warnings else "")
        )
    except Exception as e:
        return f"SYSTEM ERROR: Side quest generation failed: {e}"



@tool
async def mark_entity_immutable(
    entity_name: str,
    reason: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Marks an entity as immutable in the Knowledge Graph. Immutable entities
    cannot be modified or deleted by storylet effects — this is a hard guardrail
    for plot-critical NPCs, artifacts, or locations.

    Args:
        entity_name: The name of the entity in the Knowledge Graph.
        reason: Why is this entity immutable? (logged for audit)
    """
    from registry import get_knowledge_graph

    vault_path = config["configurable"].get("thread_id")
    kg = get_knowledge_graph(vault_path)

    node = kg.get_node_by_name(entity_name)
    if not node:
        return f"SYSTEM ERROR: Entity '{entity_name}' not found in Knowledge Graph."

    node.is_immutable = True
    return (
        f"MECHANICAL TRUTH: '{entity_name}' is now immutable in the Knowledge Graph. "
        f"Reason: {reason or 'Not specified'}. "
        f"Storylet effects attempting to modify this entity will be rejected by Hard Guardrails."
    )



__all__ = [
    "create_new_entity",
    "flesh_out_entity",
    "update_yaml_frontmatter",
    "fetch_entity_context",
    "level_up_character",
    "update_character_status",
    "spawn_summon",
    "propose_entity_creation",
    "generate_side_quests_for_entity",
    "mark_entity_immutable",
]
