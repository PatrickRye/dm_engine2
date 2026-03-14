import os
import re
import yaml
import random
import math
import aiofiles
import aiofiles.os as aios
from filelock.asyncio import AsyncSoftFileLock
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from pydantic import Field
from typing import Optional, Annotated, Union

# === DETERMINISTIC ENGINE INTEGRATION ===
from dnd_rules_engine import EventBus, GameEvent, BaseGameEntity, Creature, roll_dice as roll_generic_dice
from state import PCDetails, NPCDetails, LocationDetails, FactionDetails, ClassLevel
from vault_io import get_journals_dir, write_audit_log, read_markdown_entity_no_lock, write_markdown_entity_no_lock, upsert_journal_section
from compendium_manager import CompendiumManager, CompendiumEntry, MechanicEffect



def _get_entity_by_name(name: str) -> Optional[BaseGameEntity]:
    """Helper to find an active entity in the engine's memory by name."""
    for uid, entity in BaseGameEntity._registry.items():
        if name.lower() in entity.name.lower() or entity.name.lower() in name.lower():
            return entity
    return None

def _build_npc_template(title: str, context: str, details: dict) -> str:
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

    return (f"---\ntags: [npc]\nstatus: active\norigin: Unknown\ncurrent_location: Unknown\n---\n"
            f"# {title}\n\n## Summary - Current State\n- {ctx[:150]}...\n\n"
            f"## Background & Motives\n- {ctx}\n- **Long-Term Goals**: {long_term_goals}\n- **Aliases & Titles**: {aliases}\n\n"
            f"## Appearance\n- **Base Appearance**: {appearance}\n\n"
            f"## Communication Style\n- **Dialect/Accent**: {dialect}\n- **Mannerisms**: {mannerisms}\n- **Code-Switching**: {code_switch}\n\n"
            f"## Connections\n- {connections}\n\n"
            f"## Attitude Tracker\n- **Base Attitude**: {base_attitude}\n| Entity | Disposition | Notes |\n|---|---|---|\n| Party | Neutral | Initial encounter. |\n\n"
            f"## Active Logs\n- **Current Appearance**: {current_appearance}\n- **Immediate Goals**: {immediate_goals}\n\n"
            f"## Key Knowledge\n- \n\n## Voice & Quotes\n- \n\n## Combat & Stat Block\n{stats}\n\n"
            f"## Additional Lore & Jazz\n{misc}\n")

def _build_location_template(title: str, context: str, details: dict) -> str:
    ctx = context.strip() if context else "Newly discovered area."
    demographics = details.get("demographics", "")
    government = details.get("government", "")
    establishments = details.get("establishments", "")
    landmarks = details.get("key_features_and_landmarks", "")
    misc = details.get("misc_notes", "")
    diversity = details.get("diversity", "Unknown population makeup.")
    
    return (f"---\ntags: [location]\n---\n# {title}\n\n## Summary - Current State\n- {ctx}\n\n"
            f"## Demographics & Culture\n- **Native Dialect(s)**: {demographics}\n- **Diversity**: {diversity}\n\n"
            f"## Government & Defenses\n- {government}\n\n"
            f"## Key Features & Landmarks\n- {landmarks}\n\n"
            f"## Notable Establishments (Shops/Taverns)\n- {establishments}\n\n"
            f"## Current Rumors & Events\n| Rumor | Source | Notes |\n|---|---|---|\n| | | |\n\n"
            f"## Condition & State\n- \n\n## Inhabitants\n- \n\n## Event History\n- \n\n## System Tables\n\n"
            f"## Additional Lore & Jazz\n{misc}\n")

def _build_faction_template(title: str, context: str, details: dict) -> str:
    ctx = context.strip() if context else "Newly discovered faction."
    goals = details.get("goals", "")
    assets = details.get("assets", "")
    key_npcs = details.get("key_npcs", "")
    misc = details.get("misc_notes", "")
    
    return (f"---\ntags: [faction]\nstatus: active\n---\n# {title}\n\n## Summary - Current State\n- {ctx}\n\n"
            f"## Goals\n- {goals}\n\n## Assets & Resources\n- {assets}\n\n## Key NPCs\n- {key_npcs}\n\n## Party Disposition\n- Neutral\n\n## Event History\n- \n\n"
            f"## Additional Lore & Jazz\n{misc}\n")

def _build_pc_template(title: str, details: dict) -> str:
    appearance = details.get("appearance", "")
    current_appearance = details.get("current_appearance", "")
    long_term_goals = details.get("long_term_goals", "")
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
    spells = details.get("spells", {})
    profs = details.get("proficiencies", "None")
    feats = details.get("feats_and_traits", "None")
    
    return (f"---\ntags: [pc, player]\nstatus: active\nclasses: {yaml.dump(classes, default_flow_style=True)}\nspecies: {species}\nbackground: {background}\n"
            f"level: 1\nmax_hp: 10\nac: 10\ngold: 0\n"
            f"str: {s_str}\ndex: {s_dex}\ncon: {s_con}\nint: {s_int}\nwis: {s_wis}\ncha: {s_cha}\n"
            f"attunement_slots: 0/3\n"
            f"equipment:\n"
            f"  armor: Unarmored\n"
            f"  shield: None\n"
            f"  head: None\n"
            f"  cloak: None\n"
            f"  gloves: None\n"
            f"  boots: None\n"
            f"  ring1: None\n"
            f"  ring2: None\n"
            f"  amulet: None\n"
            f"  main_hand: Unarmed\n"
            f"  off_hand: None\n"
            f"spell_save_dc: 10\nspell_atk: \"+2\"\nspell_slots: \"None\"\n"
            f"resources: {{}}\nactive_mechanics: []\n"
            f"inventory: []\n"
            f"spells:\n  cantrips: []\n  level_1: []\n"
            f"immunities: None\nresistances: None\n---\n"
            f"# {title}\n\n## Summary - Current State\n- Active party member.\n- **Aliases & Titles**: {aliases}\n\n"
            f"## Appearance\n- **Base Appearance**: {appearance}\n\n"
            f"## Goals\n- **Long-Term Goals**: {long_term_goals}\n\n"
            f"## Status & Conditions\n- Current HP: 10\n- Active Conditions: None\n- Fatigue/Exhaustion: None\n\n"
            f"## Proficiencies & Feats\n- **Proficiencies**: {profs}\n- **Feats & Traits**: {feats}\n\n"
            f"## Active Logs\n- **Current Appearance**: {current_appearance}\n- **Immediate Goals**: {immediate_goals}\n\n"
            f"## Event Log\n- \n\n"
            f"## Additional Lore & Jazz\n{misc}\n")

def _build_party_tracker() -> str:
    return (f"---\ntags: [system, ui]\n---\n# 🛡️ DM Party Dashboard\n\n"
            f"```dataviewjs\n"
            f"const p = dv.pages('#pc or #player or #party_npc');\n"
            f"if (p.length > 0) {{\n"
            f"    let tableData = p.map(c => [\n"
            f"        c.file.link,\n"
            f"        `${{c.max_hp || '?'}}`,\n"
            f"        c.ac || 10,\n"
            f"        `10 + ${{Math.floor(((c.wisdom || c.wis || 10) - 10) / 2)}}`,\n"
            f"        c.attunement_slots || \"N/A\",\n"
            f"    ]);\n"
            f"    dv.table([\"Name\", \"Max HP\", \"AC\", \"Passive Perception\", \"Attunement\"], tableData);\n"
            f"}} else {{\n"
            f"    dv.paragraph(\"No active party members found.\");\n"
            f"}}\n"
            f"```\n")

# ============================================


@tool
def execute_melee_attack(attacker_name: str, target_name: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    STRICT REQUIREMENT: Use this tool to resolve ANY melee attack between two entities.
    Do NOT hallucinate dice rolls or damage. The engine will calculate hit/miss and exact damage.
    """
    attacker = _get_entity_by_name(attacker_name)
    target = _get_entity_by_name(target_name)
    
    if not attacker:
        return f"SYSTEM ERROR: Attacker '{attacker_name}' not found in active combat memory."
    if not target:
        return f"SYSTEM ERROR: Target '{target_name}' not found in active combat memory."
        
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker.entity_uuid,
        target_uuid=target.entity_uuid
    )
    
    result = EventBus.dispatch(event)
    
    if result.payload.get("hit"):
        dmg = result.payload.get("damage", 0)
        return f"MECHANICAL TRUTH: HIT! {attacker.name} dealt {dmg} damage to {target.name}. {target.name} has {target.hp.base_value} HP remaining."
    else:
        return f"MECHANICAL TRUTH: MISS! {attacker.name} rolled too low to beat {target.name}'s Armor Class."

@tool
def modify_health(target_name: str, hp_change: int, reason: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    Use this tool to apply guaranteed damage (traps, falling, auto-hit spells) or healing (potions, healing spells).
    Provide a negative hp_change for damage, positive for healing.
    """
    target = _get_entity_by_name(target_name)
    if not target:
        return f"SYSTEM ERROR: Target '{target_name}' not found."
        
    target.hp.base_value += hp_change
    action = "healed for" if hp_change > 0 else "took"
    return f"MECHANICAL TRUTH: {target.name} {action} {abs(hp_change)} HP from {reason}. Current HP: {target.hp.base_value}."


@tool
async def create_new_entity(entity_name: str, entity_type: str, background_context: str = "", details: Union[PCDetails, NPCDetails, LocationDetails, FactionDetails] = None, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Generates schema-compliant Markdown files."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{entity_name}.md")
    
    if os.path.exists(file_path): return f"Error: '{entity_name}.md' already exists. Use flesh_out_entity to update it instead."
    display_title = entity_name.replace("NPC_", "").replace("LOC_", "").replace("MIS_", "").replace("PC_", "").replace("_", " ")

    if details is None:
        details_dict = {}
    else:
        details_dict = details.model_dump() if hasattr(details, "model_dump") else (details.dict() if hasattr(details, "dict") else details)
        
    e_type = entity_type.upper()
    try:
        if e_type == "NPC":
            content = _build_npc_template(display_title, background_context, details_dict)
        elif e_type == "LOCATION":
            content = _build_location_template(display_title, background_context, details_dict)
        elif e_type == "FACTION":
            content = _build_faction_template(display_title, background_context, details_dict)
        elif e_type == "MISSION":
            content = f"---\ntags: [mission]\n---\n# {display_title}\n\n## Plot Summary\n- {background_context or 'Newly acquired objective.'}\n\n## Objectives\n- [ ] \n\n## Involved Entities\n- \n\n## Additional Lore & Jazz\n{details_dict.get('misc_notes', '')}\n"
        elif e_type == "CAMPAIGN":
            content = (f"---\ntags: [campaign]\ncampaign_name: {display_title}\ncurrent_date: Day 1\nin_game_time: \"08:00\"\n---\n"
                       f"# {display_title} - Master Ledger\n\n## The World State\n- (Macro-level events, political climates, or looming threats taking place in the background.)\n\n## Active Plotlines & Missions\n- \n\n"
                       f"## Alternate Routes & Consequences\n- (Track 'Fail Forward' paths here. If a party fails to find a clue, log the alternate NPC or method generated to keep the plot moving. Log the consequences of past failures.)\n\n## Major Milestones (Event Log)\n- \n\n"
                       f"## Additional Lore & Jazz\n{details_dict.get('misc_notes', '')}\n")
        elif e_type in ["PC", "PLAYER"]:
            content = _build_pc_template(display_title, details_dict)
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f: 
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
                            resourcesHtml += `<div class="ddb-vital-box" style="min-width: 80px;"><div class="label">${{res}}</div><div class="value" style="font-size:1.5em; color:#242527;">${{pc.resources[res]}}</div></div>`;
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
                                ${{pc.spell_slots && pc.spell_slots !== 'None' ? `<div class="ddb-spell-stat-pill"><strong>Slots:</strong> ${{pc.spell_slots}}</div>` : ''}}
                                ${{pc.spell_save_dc ? `<div class="ddb-spell-stat-pill"><strong>Save DC:</strong> ${{pc.spell_save_dc}}</div>` : ''}}
                                ${{pc.spell_atk ? `<div class="ddb-spell-stat-pill"><strong>Spell Atk:</strong> ${{pc.spell_atk}}</div>` : ''}}
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
                async with aiofiles.open(sheet_path, 'w', encoding='utf-8') as f: 
                    await f.write(sheet_content)
            except Exception: pass 
            return f"Success: Instantiated new log '{entity_name}.md' and UI View."
        elif e_type == "PARTY_TRACKER":
            content = _build_party_tracker()
        else:
            content = f"---\ntags: [misc]\n---\n# {display_title}\n\n{background_context}\n\n## Additional Lore & Jazz\n{details_dict.get('misc_notes', '')}\n"

        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f: 
            await f.write(content)
        return f"Success: Created {e_type} '{entity_name}.md' with context."
        
    except Exception as e: 
        return f"Error creating file: {str(e)}"


@tool
async def flesh_out_entity(entity_name: str, entity_type: str, background_context: str = "", details: Union[PCDetails, NPCDetails, LocationDetails, FactionDetails] = None, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Use this tool to completely rewrite and 'Flesh Out' an existing file."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{entity_name}.md")
    display_title = entity_name.replace("NPC_", "").replace("LOC_", "").replace("MIS_", "").replace("PC_", "").replace("_", " ")

    if details is None:
        details_dict = {}
    else:
        details_dict = details.model_dump() if hasattr(details, "model_dump") else (details.dict() if hasattr(details, "dict") else details)
        
    e_type = entity_type.upper()
    try:
        if e_type == "NPC":
            content = _build_npc_template(display_title, background_context, details_dict)
        elif e_type == "LOCATION":
            content = _build_location_template(display_title, background_context, details_dict)
        elif e_type == "FACTION":
            content = _build_faction_template(display_title, background_context, details_dict)
        elif e_type in ["PC", "PLAYER"]:
            content = _build_pc_template(display_title, details_dict)
        else:
            return f"Error: Unsupported entity type for flesh_out_entity: {e_type}"

        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f: 
            await f.write(content)
        return f"Success: Fleshed out and entirely updated {e_type} '{entity_name}.md' with deep context and jazz."
        
    except Exception as e: 
        return f"Error updating file: {str(e)}"


@tool
async def update_yaml_frontmatter(entity_name: str, updates: dict, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Safely parses a Markdown file, updates specific YAML frontmatter keys, and reconstructs the file."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{entity_name}.md")
    
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock: 
        try:
            yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        except Exception as e:
            return str(e)
            
        for key, value in updates.items(): 
            yaml_data[key] = value
            
        await write_markdown_entity_no_lock(file_path, yaml_data, body_text)
            
    return f"Success: Updated stats for {entity_name}."


@tool
async def fetch_entity_context(entity_names: list[str], full_read: bool = False, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Retrieves the YAML metadata and the 'Summary' section for given entities to gain context."""
    vault_path = config["configurable"].get("thread_id")
    j_dir = get_journals_dir(vault_path)
    context_blocks = []
    
    for name in entity_names:
        file_path = os.path.join(j_dir, f"{name}.md")
        if not await aios.path.exists(file_path):
            context_blocks.append(f"[{name}]: Entity not found in archives.")
            continue
            
        async with aiofiles.open(file_path, 'r', encoding='utf-8') as f: 
            content = await f.read()
            
        if full_read:
            context_blocks.append(f"=== {name} (FULL) ===\n{content}")
            continue
            
        yaml_data, body_text = "", content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                yaml_data, body_text = parts[1].strip(), parts[2].strip()
                
        summary_match = re.search(r'(## Summary - Current State\n.*?)(?=\n## |\Z)', body_text, re.DOTALL)
        summary_text = summary_match.group(1).strip() if summary_match else "No summary available."
        context_blocks.append(f"=== {name} (CACHED STATE) ===\nMetadata:\n{yaml_data}\n\n{summary_text}\n")
        
    return "\n\n".join(context_blocks)


@tool
async def equip_item(character_name: str, item_name: str, item_slot: str, new_ac_value: int = None, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    Equips an item to a specific slot for a character, updating their YAML file.
    This will overwrite any item currently in the specified slot.
    The AI is responsible for moving the old item back to inventory using 'manage_inventory' if needed.
    
    Valid item_slot values:
    - 'armor', 'shield', 'head', 'cloak', 'gloves', 'boots', 'amulet', 'main_hand', 'off_hand'
    - 'ring': Automatically finds an available ring slot (ring1 or ring2).
    - 'ring1' or 'ring2': To target a specific ring slot.
    """
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")

    # Map general types to specific slots
    slot_map = { "weapon": "main_hand" }
    target_slot = slot_map.get(item_slot, item_slot)

    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        try:
            yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        except Exception as e:
            return str(e)

        equipment = yaml_data.get("equipment", {})
        if not isinstance(equipment, dict):
            return f"Error: '{character_name}.md' does not have a valid 'equipment' block."

        final_slot = None
        if target_slot == "ring":
            # Auto-find an empty ring slot
            if str(equipment.get("ring1", "None")) in ["None", ""]:
                final_slot = "ring1"
            elif str(equipment.get("ring2", "None")) in ["None", ""]:
                final_slot = "ring2"
            else:
                return f"Error: Both ring slots are already occupied. You must specify 'ring1' or 'ring2' to overwrite one."
        elif target_slot in equipment:
            final_slot = target_slot
        else:
            valid_slots = list(equipment.keys())
            return f"Error: Invalid equipment slot '{item_slot}'. Valid slots are: {', '.join(valid_slots)} or 'ring'."

        equipment[final_slot] = item_name
        
        updates = {"equipment": equipment}
        if new_ac_value is not None:
            updates["ac"] = new_ac_value
        
        for key, value in updates.items(): 
            yaml_data[key] = value

        await write_markdown_entity_no_lock(file_path, yaml_data, body_text)

    ac_msg = f". Their AC is now {new_ac_value}" if new_ac_value is not None else ""
    return f"Success: {character_name} equipped {item_name} in the {final_slot} slot{ac_msg}."

@tool
async def use_expendable_resource(character_name: str, resource_name: str, amount_to_deduct: int = 1, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Deducts a use of a class feature, spell slot, or item charge (e.g. 'Second Wind', '1st Level Spell', 'Lucky')."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")
    
    log_message = ""
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    
    # 1. ACQUIRE LOCK: Read and Write the YAML
    async with lock:
        try:
            yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        except Exception as e:
            return str(e)
            
        resources = yaml_data.get("resources", {})
        if not isinstance(resources, dict): resources = {}
            
        target_key = next((k for k in resources.keys() if resource_name.lower() in k.lower()), None)
        
        if not target_key:
            return f"Error: Resource '{resource_name}' not found on {character_name}'s sheet. Available: {list(resources.keys())}"
            
        val_str = str(resources[target_key])
        match = re.match(r"(\d+)\s*/\s*(\d+)", val_str)
        if match:
            current_val = int(match.group(1))
            max_val = int(match.group(2))
            new_val = max(0, current_val - amount_to_deduct)
            resources[target_key] = f"{new_val}/{max_val}"
            
            yaml_data["resources"] = resources
            await write_markdown_entity_no_lock(file_path, yaml_data, body_text)
            
            # Prepare the log message but DO NOT call the tool inside the lock!
            log_message = f"- Used {amount_to_deduct}x {target_key}. ({new_val}/{max_val} remaining)."
        else:
            return f"Error: Resource '{target_key}' has invalid format '{val_str}'. Expected 'current/max' (e.g., '2/3')."

    # 2. LOCK RELEASED: Safely call the next tool
    if log_message:
        await upsert_journal_section.ainvoke({"entity_name": character_name, "section_header": "Event Log", "content": log_message, "mode": "append"}, config)
        return f"Success: Deducted {amount_to_deduct} from {target_key}. They now have {new_val}/{max_val} remaining."

@tool
async def update_character_status(character_name: str, hp: str, resources: str, conditions: str, fatigue: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Overwrites the 'Status & Conditions' section in a character's markdown log."""
    content = f"- Current HP: {hp}\n- Expendable Resources: {resources}\n- Active Conditions: {conditions}\n- Fatigue/Exhaustion: {fatigue}"
    # PASSING CONFIG DOWN THE CHAIN
    return await upsert_journal_section.ainvoke({"entity_name": character_name, "section_header": "Status & Conditions", "content": content, "mode": "replace"}, config)

@tool
async def level_up_character(character_name: str, class_name: str, hp_increase: int, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Updates a character's level in a specific class, increases their Max HP, and applies new features from the compendium."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")
    
    if not await aios.path.exists(file_path): 
        return f"Error: Could not locate '{character_name}.md'."
        
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        try:
            yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        except Exception as e:
            return str(e)

        pc_details = PCDetails(**yaml_data)
        
        # Find the class to level up
        class_to_level_up = None
        for c in pc_details.classes:
            if c.class_name.lower() == class_name.lower():
                class_to_level_up = c
                break
        
        if not class_to_level_up:
            return f"Error: Class '{class_name}' not found on character '{character_name}'."

        class_to_level_up.level += 1
        new_level = class_to_level_up.level
        
        # Update HP
        new_max_hp = pc_details.hp + hp_increase
        pc_details.hp = new_max_hp
        
        # Get creature from engine to apply features
        creature = _get_entity_by_name(character_name)
        if not creature or not isinstance(creature, Creature):
            return f"Error: Creature '{character_name}' not found in the deterministic engine."

        # Apply features from class definition
        class_def = await CompendiumManager.get_class_definition(vault_path, class_to_level_up.class_name)
        if class_def:
            creature.apply_features(class_def, new_level)
        
        # Apply features from subclass definition
        if class_to_level_up.subclass_name:
            subclass_def = await CompendiumManager.get_subclass_definition(vault_path, class_to_level_up.subclass_name)
            if subclass_def:
                creature.apply_subclass_features(subclass_def, new_level)
        
        # Update YAML data
        updates = {
            "level": pc_details.character_level,
            "max_hp": new_max_hp,
            "classes": [c.model_dump() for c in pc_details.classes]
        }
        for key, value in updates.items(): 
            yaml_data[key] = value

        await write_markdown_entity_no_lock(file_path, yaml_data, body_text)
    
    new_features = [f.name for f in creature.features if f.level == new_level]
    if new_features:
        feature_bullets = "\n".join([f"- **Level {new_level} ({class_name})**: {feat}" for feat in new_features])
        await upsert_journal_section.ainvoke({"entity_name": character_name, "section_header": "Event Log", "content": feature_bullets, "mode": "append"}, config)
        
    return f"Success: {character_name} leveled up to level {new_level} {class_name}. Max HP is now {new_max_hp}."

@tool
async def manage_inventory(character_name: str, item_name: str, action: str, quantity: int = 1, gold_change: int = 0, context_log: str = "", metadata: str = "", *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Adds or removes an item/gold from a character's YAML inventory."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), f"{character_name}.md")
    
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        try:
            yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        except Exception as e:
            return str(e)
            
        current_gold = int(yaml_data.get("gold", 0))
        new_gold = current_gold + gold_change
        if new_gold < 0: return f"Transaction Failed: Not enough gold."
        yaml_data["gold"] = new_gold
        
        inventory = yaml_data.get("inventory", [])
        if not isinstance(inventory, list): inventory = []
        
        if action.lower() == "add" and item_name: 
            item_str = f"{item_name} (x{quantity})"
            if metadata: item_str += f" [{metadata}]"
            inventory.append(item_str)
        elif action.lower() == "remove" and item_name:
            item_to_remove = next((item for item in inventory if item_name.lower() in item.lower()), None)
            if item_to_remove: inventory.remove(item_to_remove)
            else: return f"Error: '{item_name}' not found."
            
        yaml_data["inventory"] = inventory
        await write_markdown_entity_no_lock(file_path, yaml_data, body_text)
        
    if context_log: 
        await upsert_journal_section.ainvoke({"entity_name": character_name, "section_header": "Event Log", "content": f"- **Inventory**: {context_log}", "mode": "append"}, config)
    return f"Success. Gold is now {new_gold}. Event logged."


@tool
async def perform_ability_check_or_save(character_name: str, skill_or_stat_name: str, is_hidden: bool = False, is_passive: bool = False, advantage: bool = False, disadvantage: bool = False, extra_modifier: int = 0, bonus_dice: str = None, luck_points_used: int = 0, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
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
        "perception": "wisdom", "insight": "wisdom", "survival": "wisdom", "animal handling": "wisdom", "medicine": "wisdom",
        "investigation": "intelligence", "history": "intelligence", "religion": "intelligence", "arcana": "intelligence", "nature": "intelligence",
        "stealth": "dexterity", "acrobatics": "dexterity", "sleight of hand": "dexterity", "athletics": "strength",
        "persuasion": "charisma", "deception": "charisma", "intimidation": "charisma", "performance": "charisma"
    }
    clean_skill = skill_or_stat_name.lower().strip()
    base_stat = skill_map.get(clean_skill, clean_skill)
    
    if base_stat != "none":
        lock = AsyncSoftFileLock(f"{file_path}.lock")
        async with lock:
            try:
                yaml_data, _ = await read_markdown_entity_no_lock(file_path)
                stat_score = int(yaml_data.get(base_stat, yaml_data.get(base_stat[:3], 10)))
                stat_mod = math.floor((stat_score - 10) / 2)
            except Exception: pass
                
    total_mod = stat_mod + extra_modifier
    
    if is_passive:
        total = 10 + total_mod + (5 if advantage else (-5 if disadvantage else 0))
        result_str = f"Passive {clean_skill.capitalize()} Score: {total}.\nDM DIRECTIVE: Narrate using 'Describe to Me'."
        await write_audit_log(vault_path, "Rules Engine", "perform_ability_check_or_save Executed (Passive)", result_str)
        return result_str
        
# --- 1. RESOLVE 5.5e BOOLEAN STATES ---
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
            
    # --- 3. RESOLVE BONUS DICE (Bless/Bane) ---
    bonus_total = 0
    bonus_str = ""
    if bonus_dice:
        match = re.match(r"([+-]?)\s*(\d+)d(\d+)", bonus_dice.strip().lower())
        if match:
            sign, num, sides = match.group(1) or '+', int(match.group(2)), int(match.group(3))
            b_rolls = [random.randint(1, sides) for _ in range(num)]
            bonus_total = sum(b_rolls) if sign != '-' else -sum(b_rolls)
            bonus_str = f" + [{bonus_dice}: {bonus_total}]"
        
    total = base_roll + total_mod + bonus_total
    result_str = f"MECHANICAL TRUTH: Roll Result ({clean_skill}): {base_roll} {roll_type_str} + {total_mod} stat mod{bonus_str} = {total}. "
    result_str += "\nHIDDEN ROLL: Narrate sensory experience only." if is_hidden else "\nYou may reveal the total to the player."
       
    await write_audit_log(vault_path, "Rules Engine", "perform_ability_check_or_save Executed", result_str)
    return result_str


@tool
async def roll_generic_dice(formula: str,  reason: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """
    Parses and rolls generic D&D dice formulas (e.g., '1d8+3', '8d6').
    Use this to roll generic dice for random encounters, loot tables, or minor narrative 
    variables (e.g., '1d4 days of travel').
    
    CRITICAL: NEVER use this tool to calculate weapon damage, spell damage, or health changes. 
    Use `modify_health` or the combat tools instead.
    """
    match = re.match(r"(\d+)d(\d+)(?:\s*([+-])\s*(\d+))?", formula.strip().lower())
    if not match: return f"Error: Invalid dice format '{formula}'. Use 'XdY' or 'XdY+Z'."
    
    num_dice, die_sides = int(match.group(1)), int(match.group(2))
    modifier_op = match.group(3)
    modifier_val = int(match.group(4)) if match.group(4) else 0
    
    rolls = [random.randint(1, die_sides) for _ in range(num_dice)]
    total = sum(rolls)
    if modifier_op == '+': total += modifier_val
    elif modifier_op == '-': total -= modifier_val
    
    result = f"MECHANICAL TRUTH: Rolled {formula} for {reason}. Result:{total}"
    await write_audit_log(config["configurable"].get("thread_id"), "Rules Engine", "roll_generic_dice Executed", result)
    return result


@tool
async def search_vault_by_tag(target_tag: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Scans the YAML frontmatter of files to find entities matching a tag."""
    vault_path = config["configurable"].get("thread_id")
    matching_files, j_dir = [], get_journals_dir(vault_path)
    
    if not await aios.path.exists(j_dir): return "Error: Journal directory not found."
    
    for filename in os.listdir(j_dir):
        if not filename.endswith(".md"): continue
        file_path = os.path.join(j_dir, filename)
        
        lock = AsyncSoftFileLock(f"{file_path}.lock")
        async with lock:
            try:
                yaml_data, _ = await read_markdown_entity_no_lock(file_path)
                if target_tag.lower() in [tag.lower() for tag in yaml_data.get("tags", [])]:
                    matching_files.append(filename.replace(".md", ""))
            except Exception: pass
                    
    return f"Entities matching '{target_tag}': " + ", ".join(matching_files) if matching_files else f"No entities found with tag: {target_tag}"


@tool
async def advance_time(days: int = 0, hours: int = 0, minutes: int = 0, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Advances the in-game clock stored in CAMPAIGN_MASTER.md."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "CAMPAIGN_MASTER.md")
        
    current_day, current_hour, current_minute = 1, 8, 0
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        try:
            yaml_data, _ = await read_markdown_entity_no_lock(file_path)
            day_match = re.search(r'\d+', str(yaml_data.get("current_date", "Day 1")))
            if day_match: current_day = int(day_match.group())
            time_str = str(yaml_data.get("in_game_time", "08:00"))
            if ":" in time_str:
                h, m = time_str.split(":")
                current_hour, current_minute = int(h), int(m)
        except Exception: pass
                
    total_minutes = current_minute + minutes
    total_hours = current_hour + hours + (total_minutes // 60)
    new_day = current_day + days + (total_hours // 24)
    new_time_str = f"{total_hours % 24:02d}:{total_minutes % 60:02d}"
    
    await update_yaml_frontmatter.ainvoke({"entity_name": "CAMPAIGN_MASTER", "updates": {"current_date": f"Day {new_day}", "in_game_time": new_time_str}}, config)
    return f"Success: Time advanced. It is now Day {new_day}, {new_time_str}."


@tool
async def start_combat(pc_names: list[str], enemies: list[dict], *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Creates ACTIVE_COMBAT.md."""
    vault_path = config["configurable"].get("thread_id")
    j_dir = get_journals_dir(vault_path)
    combatants = []
    
    for pc in pc_names:
        file_path = os.path.join(j_dir, f"{pc}.md")
        pc_dex_mod, pc_hp, pc_ac = 0, 10, 10
        lock = AsyncSoftFileLock(f"{file_path}.lock")
        async with lock:
            try:
                yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
                pc_dex_mod = math.floor((int(yaml_data.get("dexterity", yaml_data.get("dex", 10))) - 10) / 2)
                pc_hp = int(yaml_data.get("max_hp", 10))
                pc_ac = int(yaml_data.get("ac", 10))
                hp_match = re.search(r'- Current HP:\s*(\d+)', body_text)
                if hp_match: pc_hp = int(hp_match.group(1))
            except Exception: pass # Ignores missing PCs or syntax errors, defaulting to 10
            
        combatants.append({"name": pc, "init": random.randint(1, 20) + pc_dex_mod, "hp": pc_hp, "max_hp": pc_hp, "ac": pc_ac, "conditions": [], "is_pc": True})
    
    for enemy in enemies:
        combatants.append({"name": enemy.get("name", "Unknown"), "init": random.randint(1, 20) + int(enemy.get("dex_mod", 0)), 
                           "hp": int(enemy.get("hp", 10)), "max_hp": int(enemy.get("hp", 10)), "ac": int(enemy.get("ac", 10)), "conditions": [], "is_pc": False})
    
    combatants = sorted(combatants, key=lambda x: x["init"], reverse=True)
    yaml_str = yaml.dump({"tags": ["combat_whiteboard"], "round": 1, "current_turn_index": 0, "combatants": combatants}, sort_keys=False, default_flow_style=False)
    dataview_js = (f"```dataviewjs\nconst p = dv.current(); if (!p || !p.combatants) return;\n"
                   f"let tableData = p.combatants.map((c, i) => [i === p.current_turn_index ? \"👉 \"+c.init : c.init, c.name, `${{c.hp}}/${{c.max_hp}}`, c.ac, c.hp <= 0 ? \"💀 Dead\" : (c.conditions.length ? c.conditions.join(\", \") : \"Healthy\")]);\n"
                   f"dv.header(2, \"⚔️ Active Combat Tracker ⚔️\"); dv.paragraph(`**Round:** ${{p.round}}`);\n"
                   f"dv.table([\"Init\", \"Combatant\", \"HP\", \"AC\", \"Status\"], tableData);\n```\n")
                   
    async with aiofiles.open(os.path.join(j_dir, "ACTIVE_COMBAT.md"), 'w', encoding='utf-8') as f: 
        await f.write(f"---\n{yaml_str}---\n\n{dataview_js}")
    return f"Combat started! {combatants[0]['name']} goes first."


@tool
async def update_combat_state(combatant_name: str = None, hp_change: int = 0, added_conditions: list[str] = [], next_turn: bool = False, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Applies damage/healing to combatants and advances the initiative turn order."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")
    if added_conditions is None: added_conditions = []
    
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        try:
            yaml_data, body_text = await read_markdown_entity_no_lock(file_path)
        except Exception as e:
            return str(e)
        
        log_msg = []
        combatants = yaml_data.get("combatants", [])
        if combatant_name:
            for c in combatants:
                if c["name"].lower() == combatant_name.lower():
                    if hp_change != 0:
                        c["hp"] = max(0, min(c["max_hp"], c["hp"] + hp_change))
                        log_msg.append(f"{c['name']} {'healed' if hp_change > 0 else 'took damage'}. HP: {c['hp']}/{c['max_hp']}.")
                    if added_conditions:
                        c["conditions"].extend(added_conditions)
                        log_msg.append(f"{c['name']} gained conditions: {', '.join(added_conditions)}.")
                        
        if next_turn:
            yaml_data["current_turn_index"] = (yaml_data.get("current_turn_index", 0) + 1) % len(combatants)
            if yaml_data["current_turn_index"] == 0: yaml_data["round"] = yaml_data.get("round", 1) + 1
            loop_counter = 0
            while combatants[yaml_data["current_turn_index"]]["hp"] <= 0 and loop_counter < len(combatants):
                 yaml_data["current_turn_index"] = (yaml_data["current_turn_index"] + 1) % len(combatants)
                 loop_counter += 1
            log_msg.append(f"Turn advanced to {combatants[yaml_data['current_turn_index']]['name']}.")
            
        yaml_data["combatants"] = combatants
        await write_markdown_entity_no_lock(file_path, yaml_data, body_text)
            
    return " | ".join(log_msg) if log_msg else "Combat updated."


@tool
async def end_combat(*, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Concludes combat, saves PC final states to permanent files, and deletes ACTIVE_COMBAT.md."""
    vault_path = config["configurable"].get("thread_id")
    file_path = os.path.join(get_journals_dir(vault_path), "ACTIVE_COMBAT.md")
    
    lock = AsyncSoftFileLock(f"{file_path}.lock")
    async with lock:
        try:
            yaml_data, _ = await read_markdown_entity_no_lock(file_path)
        except Exception as e:
            return str(e)
            
    for c in yaml_data.get("combatants", []):
        if c.get("is_pc"):
            conds = ", ".join(c["conditions"]) if c["conditions"] else "None"
            await update_character_status.ainvoke({"character_name": c["name"], "hp": str(c["hp"]), "resources": "Update Manually", "conditions": conds, "fatigue": "None"}, config)
            
    os.remove(file_path)
    return "Combat ended successfully. ACTIVE_COMBAT.md removed."

def _get_config_tone(vault_path: str) -> str:
    """Reads DM_CONFIG.md to optionally retrieve Tone & Boundaries."""
    config_path = os.path.join(vault_path, "DM_CONFIG.md")
    if not os.path.exists(config_path): return ""
    try:
        with open(config_path, 'r', encoding='utf-8') as f: content = f.read()
        if content.startswith("---"):
            yaml_data = yaml.safe_load(content.split("---", 2)[1]) or {}
            return yaml_data.get("tone_and_boundaries", "")
    except Exception: pass
    return ""

def _get_config_dirs(vault_path: str, key: str) -> list[str]:
    """Reads DM_CONFIG.md and returns a list of absolute paths for a directory key."""
    config_path = os.path.join(vault_path, "DM_CONFIG.md")
    if not os.path.exists(config_path):
        return []
    try:
        with open(config_path, 'r', encoding='utf-8') as f: content = f.read()
        if content.startswith("---"):
            yaml_data = yaml.safe_load(content.split("---", 2)[1]) or {}
            rel_dirs = yaml_data.get("directories", {}).get(key, [])
            if isinstance(rel_dirs, str): rel_dirs = [rel_dirs]
            target_dirs = []
            for rel_dir in rel_dirs:
                target_dir = os.path.join(vault_path, os.path.normpath(rel_dir))
                os.makedirs(target_dir, exist_ok=True)
                target_dirs.append(target_dir)
            return target_dirs
    except Exception as e:
        print(f"Error reading DM_CONFIG.md: {e}")
    return []

def _search_markdown_for_keywords(target_dirs: list[str], query: str, top_n: int = 3) -> str:
    """Scans all .md files in multiple directories, chunks by headers, and returns the most relevant sections."""
    keywords = set([w.lower() for w in query.replace(",", "").split() if len(w) > 3])
    if not keywords: keywords = set([query.lower()])
        
    best_chunks = []
    
    for target_dir in target_dirs:
        for root, _, files in os.walk(target_dir):
            for file in files:
                if file.endswith(".md"):
                    with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content.startswith("---"):
                            parts = content.split("---", 2)
                            if len(parts) >= 3: content = parts[2]
                                
                        chunks = re.split(r'\n(?=#+ )', content)
                        
                        for chunk in chunks:
                            score = sum(1 for k in keywords if k in chunk.lower())
                            if any(k in file.lower() for k in keywords): score += 2 
                            if score > 0: best_chunks.append((score, file, chunk.strip()))
    
    if not best_chunks:
        return f"Cache Miss: No relevant information found for '{query}'."
        
    best_chunks.sort(key=lambda x: x[0], reverse=True)
    
    result = ""
    for score, file, chunk in best_chunks[:top_n]:
        snippet = chunk[:1000] + ("\n[...Truncated]" if len(chunk) > 1000 else "")
        result += f"--- Source: {file} ---\n{snippet}\n\n"
        
    return result

@tool
def query_bestiary(creature_name: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Retrieves exact stat blocks, abilities, tactics, and lore for a specific creature."""
    vault_path = config["configurable"].get("thread_id")
    target_dirs = _get_config_dirs(vault_path, "bestiary")
    if not target_dirs: return "Error: Bestiary directory not configured in DM_CONFIG.md."
    
    search_name = creature_name.lower().strip()
    
    for target_dir in target_dirs:
        for root, _, files in os.walk(target_dir):
            for file in files:
                if not file.endswith(".md"): continue
                with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                    content = f.read().replace('\r\n', '\n')
                
                header_pattern = rf"^(#+)\s+.*?(?:\[.*?\]\(.*?\))?.*?{re.escape(search_name)}.*?$"
                match = re.search(header_pattern, content, re.IGNORECASE | re.MULTILINE)
                
                if match:
                    header_level = len(match.group(1))
                    tail = content[match.start():]
                    next_header_pattern = re.compile(rf"^#{{1,{header_level}}}\s+", re.MULTILINE | re.IGNORECASE)
                    next_match = next_header_pattern.search(tail, pos=len(match.group(0)))
                    
                    if next_match: body = tail[:next_match.start()].strip()
                    else: body = tail.strip()
                    return f"--- BESTIARY ENTRY FROM {file} ---\n{body}"[:4000]

    return _search_markdown_for_keywords(target_dirs, creature_name, top_n=1)

@tool
def query_rulebook(topic: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Searches the local D&D rules directories for game mechanics, spells, and systems."""
    vault_path = config["configurable"].get("thread_id")
    target_dirs = _get_config_dirs(vault_path, "rules")
    if not target_dirs: return "Error: Rules directory not configured in DM_CONFIG.md."
    return _search_markdown_for_keywords(target_dirs, topic, top_n=2)

@tool
def query_campaign_module(search_terms: list[str], current_chapter_context: str = "", *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Searches pre-written campaign modules, lore bibles, and published adventure notes.
    Pass a list of unique nouns and aliases (e.g. ["Strahd", "Zarovich", "Devil"]) to ensure broad coverage."""
    vault_path = config["configurable"].get("thread_id")
    target_dirs = _get_config_dirs(vault_path, "modules")
    if not target_dirs: return "Error: Modules directory not configured in DM_CONFIG.md."
    query = f"{current_chapter_context} " + " ".join(search_terms)
    return _search_markdown_for_keywords(target_dirs, query, top_n=3)



@tool
async def use_ability_or_spell(caster_name: str, ability_name: str, target_names: list[str], *, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """Use this tool whenever a character casts a spell or uses a class feature."""
    vault_path = config["configurable"].get("thread_id")
    caster = _get_entity_by_name(caster_name)
    if not caster: return f"SYSTEM ERROR: Caster '{caster_name}' not found."

    entry = await CompendiumManager.get_entry(vault_path, ability_name)
    if not entry:
        return (
            f"CACHE MISS: '{ability_name}' is not in the Engine. "
            f"Use `query_rulebook` to find the exact rules, then use `encode_new_compendium_entry` "
            f"to save it. Then try casting again."
        )
    
    targets = [_get_entity_by_name(t) for t in target_names]
    target_string = ", ".join([t.name for t in targets if t]) or "themselves"
    
    if entry.mechanics.damage_dice:
        # Parse the dice string locally to get an integer
        match = re.match(r"(\d+)d(\d+)(?:\s*([+-])\s*(\d+))?", entry.mechanics.damage_dice.strip().lower())
        dmg = 0
        if match:
            num_dice, die_sides = int(match.group(1)), int(match.group(2))
            modifier_op = match.group(3)
            modifier_val = int(match.group(4)) if match.group(4) else 0
            rolls = [random.randint(1, die_sides) for _ in range(num_dice)]
            dmg = sum(rolls)
            if modifier_op == '+': dmg += modifier_val
            elif modifier_op == '-': dmg -= modifier_val
            
        for t in targets:
            if t: t.hp.base_value -= dmg
        return f"MECHANICAL TRUTH: {caster.name} used {entry.name} on {target_string}, dealing {dmg} {entry.mechanics.damage_type} damage."
    
    return f"MECHANICAL TRUTH: {caster.name} used {entry.name} on {target_string}. Effect: {entry.description}"

@tool
async def encode_new_compendium_entry(
    name: str = Field(..., description="Exact name of the ability or spell"),
    category: str = Field(..., description="'spell', 'feature', 'feat', or 'item'"),
    action_type: str = Field(..., description="'Action', 'Bonus Action', 'Reaction', or 'Passive'"),
    description: str = Field(..., description="A concise summary of the mechanical rules."),
    source_reference: str = Field(..., description="Name of the rulebook and page number."),
    damage_dice: str = Field("", description="e.g., '8d6'. Leave empty string if no damage."),
    damage_type: str = Field("", description="e.g., 'fire'."),
    save_required: str = Field("", description="e.g., 'dexterity'."),
    *, config: Annotated[RunnableConfig, InjectedToolArg]
) -> str:
    """Teach the engine a new ability. Call ONLY when you receive a CACHE MISS."""
    vault_path = config["configurable"].get("thread_id")
    
    mechanics = MechanicEffect(
        damage_dice=damage_dice,
        damage_type=damage_type,
        save_required=save_required
    )
    
    entry = CompendiumEntry(
        name=name,
        category=category,
        action_type=action_type,
        description=description,
        references=[source_reference],
        mechanics=mechanics
    )
    
    filepath = await CompendiumManager.save_entry(vault_path, entry)
    return f"SUCCESS: '{name}' encoded to {filepath}. The engine now understands this ability. Proceed with your action."
