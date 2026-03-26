# flake8: noqa: W293, E203
"""
Utility helpers for tools.py — no @tool decorators here.
Re-exported by tools.py for backward compatibility.
"""
import os
import re
import yaml
import aiofiles
from typing import Optional, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from dnd_rules_engine import BaseGameEntity

_MAX_CACHED_VAULTS = 5  # Evict oldest vault when this many are in memory


class VaultCache:
    def __init__(self):
        self.bestiary_cache = {}  # vault_path -> [ (filename, content) ]
        self.chunk_cache = {}  # vault_path -> category -> [ (filename, chunk) ]
        self.indexed_vaults = []  # ordered list; front = oldest, back = most-recently indexed
        self._index_mtimes: dict[str, float] = {}  # vault_path -> max mtime at last build
        # Per-category directory mtimes — compared before doing expensive per-file walk
        self._dir_mtimes: dict[str, dict[str, float]] = {}  # vault_path -> {cat: dir_mtime}

    def _evict_oldest(self):
        while len(self.indexed_vaults) >= _MAX_CACHED_VAULTS:
            oldest = self.indexed_vaults.pop(0)
            self.bestiary_cache.pop(oldest, None)
            self.chunk_cache.pop(oldest, None)
            self._index_mtimes.pop(oldest, None)
            self._dir_mtimes.pop(oldest, None)

    def _get_max_mtime(self, vault_path: str) -> float:
        """
        Stat-only check to find whether any compendium file has changed.

        Optimization: compares directory-level mtime first. Only walks individual
        files when a directory's mtime has changed (indicating potential changes).
        """
        if vault_path not in self._dir_mtimes:
            self._dir_mtimes[vault_path] = {}

        max_mtime = 0.0
        any_dir_changed = False

        for cat in ["bestiary", "rules", "modules"]:
            for d in _get_config_dirs(vault_path, cat):
                try:
                    dir_mtime = os.path.getmtime(d)
                except OSError:
                    continue

                prev_dir_mtime = self._dir_mtimes[vault_path].get(cat, 0.0)
                if dir_mtime > prev_dir_mtime:
                    # Directory changed — need per-file walk to find true max mtime
                    any_dir_changed = True
                if dir_mtime > max_mtime:
                    max_mtime = dir_mtime
                # Always update stored dir mtime (even if unchanged)
                self._dir_mtimes[vault_path][cat] = dir_mtime

        if not any_dir_changed:
            # No directory changed — return cached max mtime without any file stats
            return self._index_mtimes.get(vault_path, 0.0)

        # At least one directory changed — walk files to find true max mtime
        file_max = 0.0
        for cat in ["bestiary", "rules", "modules"]:
            for d in _get_config_dirs(vault_path, cat):
                for root, _, files in os.walk(d):
                    for file in files:
                        if file.endswith(".md"):
                            try:
                                mtime = os.path.getmtime(os.path.join(root, file))
                                if mtime > file_max:
                                    file_max = mtime
                            except OSError:
                                pass
        return file_max

    def build_index(self, vault_path: str, force: bool = False):
        if vault_path in self.indexed_vaults and not force:
            # Fast mtime check: only rebuild if any compendium file changed
            current_max = self._get_max_mtime(vault_path)
            if current_max <= self._index_mtimes.get(vault_path, 0):
                return
            # Files changed — fall through to rebuild
            self.indexed_vaults = [v for v in self.indexed_vaults if v != vault_path]
        elif force:
            self.indexed_vaults = [v for v in self.indexed_vaults if v != vault_path]

        self.bestiary_cache[vault_path] = []
        self.chunk_cache[vault_path] = {"rules": [], "modules": [], "bestiary": []}
        max_mtime = 0.0

        for cat in ["bestiary", "rules", "modules"]:
            dirs = _get_config_dirs(vault_path, cat)
            for d in dirs:
                for root, _, files in os.walk(d):
                    for file in files:
                        if file.endswith(".md"):
                            try:
                                filepath = os.path.join(root, file)
                                mtime = os.path.getmtime(filepath)
                                if mtime > max_mtime:
                                    max_mtime = mtime
                                with open(filepath, "r", encoding="utf-8") as f:
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
        self._index_mtimes[vault_path] = max_mtime
        self._evict_oldest()
        self.indexed_vaults.append(vault_path)


_VAULT_CACHE = VaultCache()

_CHARACTER_AUTOMATIONS = {}


def update_roll_automations(character_name: str, automations: dict):
    _CHARACTER_AUTOMATIONS[character_name] = automations


def get_roll_automations(character_name: str) -> dict:
    defaults = {"hidden_rolls": True, "saving_throws": True, "skill_checks": True, "attack_rolls": True}
    return _CHARACTER_AUTOMATIONS.get(character_name, defaults)


def _calculate_reach(entity, is_active_turn: bool = False) -> float:
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
        "---\ntags: [system, ui]\n---\n# DM Party Dashboard\n\n"
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


async def _get_entity_by_name(name: str, vault_path: str) -> Optional["BaseGameEntity"]:
    """Helper to find an active entity in the engine's memory by name, with JIT lazy loading."""
    # Import here to avoid circular imports at module load time
    from registry import _NAME_INDEX, get_all_entities, get_candidate_uuids_by_prefix, get_entity
    from vault_io import load_entity_into_engine, get_journals_dir

    name_lower = name.lower().strip()

    # 1. Check Memory (Fast Path — exact match)
    if vault_path in _NAME_INDEX and name_lower in _NAME_INDEX[vault_path]:
        return get_entity(_NAME_INDEX[vault_path][name_lower], vault_path)

    # 2. Prefix-index scan — narrow to candidates sharing first 3 chars before O(n) check
    candidate_uuids = get_candidate_uuids_by_prefix(name_lower, vault_path)
    if candidate_uuids:
        all_entities = get_all_entities(vault_path)
        for uid in candidate_uuids:
            entity = all_entities.get(uid)
            if entity is None:
                continue
            ent_name_lower = entity.name.lower()
            if name_lower in ent_name_lower or ent_name_lower in name_lower:
                return entity

    # 3. Substring scan (handles renamed entities whose old name is still in the index)
    for uid, entity in get_all_entities(vault_path).items():
        ent_name_lower = entity.name.lower()
        if name_lower in ent_name_lower or ent_name_lower in name_lower:
            return entity

    # 4. Just-In-Time (JIT) Hydration (Lazy Load)
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


async def _get_current_combat_initiative(vault_path: str) -> int:
    """Get the initiative value of the current combat turn."""
    from vault_io import get_journals_dir, read_markdown_entity

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


__all__ = [
    "VaultCache",
    "_VAULT_CACHE",
    "update_roll_automations",
    "get_roll_automations",
    "_calculate_reach",
    "_build_npc_template",
    "_build_location_template",
    "_build_faction_template",
    "_build_pc_template",
    "_build_party_tracker",
    "_get_config_tone",
    "_get_config_settings",
    "_get_config_dirs",
    "_search_markdown_for_keywords",
    "_get_entity_by_name",
    "_get_current_combat_initiative",
]
