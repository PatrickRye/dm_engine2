import os
import json
import aiofiles
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from state import ClassDefinition, SubclassDefinition


class MechanicEffect(BaseModel):
    """The strict mathematical definition of what an ability does."""

    requires_attack_roll: bool = False
    damage_dice: str = ""  # e.g., "8d6"
    damage_type: str = ""  # e.g., "fire"
    healing_dice: str = ""  # e.g., "1d4"
    save_required: str = ""  # e.g., "dexterity" or "constitution"
    half_damage_on_save: bool = False
    conditions_applied: List[Dict[str, Any]] = Field(
        default_factory=list
    )  # e.g., [{"condition": "Poisoned", "duration": "1 minute"}]
    modifiers: List[Dict[str, Any]] = Field(
        default_factory=list
    )  # e.g., [{"target": "self", "stat": "ac", "value": 5, "duration": "1 round"}]
    granted_tags: List[str] = Field(
        default_factory=list
    )  # e.g., ["ignore_difficult_terrain", "ignore_ranged_melee_disadvantage"]
    requires_concentration: bool = False
    terrain_effect: Optional[Dict[str, Any]] = None
    trigger_event: str = ""  # e.g., "on_hit", "on_miss"
    resource_cost: str = ""  # e.g., "Second Wind Uses [SR]:1" — consumed after successful use
    mastery_type: str = ""  # e.g., "cleave", "push", "slow", "nick" — routes to custom mastery logic
    speed_reduction: int = 0  # REQ-MST-006: Slow mastery speed reduction amount
    push_distance: float = 10.0  # REQ-MST-004: Push mastery distance in feet


class CompendiumEntry(BaseModel):
    """A single JSON file representing a spell, feat, or feature."""

    name: str
    category: str  # "spell", "feature", "feat", "item"
    action_type: str  # "Action", "Bonus Action", "Reaction", "Passive"
    description: str
    mitigation_notes: str = ""
    references: List[str] = Field(default_factory=list)  # e.g., ["PHB 2024 pg 112", "Homebrew: Strahd's Notes"]
    mechanics: MechanicEffect


class WeaponDefinition(BaseModel):
    """Defines a standard or magical weapon for the OO Deterministic Engine."""

    name: str
    weapon_type: str = "melee"  # "melee" or "ranged"
    damage_dice: str
    damage_type: str
    properties: List[str] = Field(default_factory=list)  # e.g., ["finesse", "light"]
    magic_bonus: int = 0
    is_conditional: bool = False
    conditions: List[Dict[str, str]] = Field(default_factory=list)  # For ConditionalDamageWeapon bridging


class CompendiumManager:
    """Manages the JSON registry and individual ability files."""

    @staticmethod
    def _get_paths(vault_path: str):
        compendium_dir = os.path.join(vault_path, "server", "Compendium")
        registry_path = os.path.join(compendium_dir, "registry.json")
        entries_dir = os.path.join(compendium_dir, "entries")
        weapons_dir = os.path.join(compendium_dir, "weapons")
        classes_dir = os.path.join(compendium_dir, "classes")
        subclasses_dir = os.path.join(compendium_dir, "subclasses")
        os.makedirs(entries_dir, exist_ok=True)
        os.makedirs(weapons_dir, exist_ok=True)
        os.makedirs(classes_dir, exist_ok=True)
        os.makedirs(subclasses_dir, exist_ok=True)
        return compendium_dir, registry_path, entries_dir, weapons_dir, classes_dir, subclasses_dir

    @classmethod
    async def load_registry(cls, vault_path: str) -> Dict[str, str]:
        """Loads the master index of Name -> Filepath."""
        _, registry_path, _, _, _, _ = cls._get_paths(vault_path)
        if not os.path.exists(registry_path):
            return {}
        async with aiofiles.open(registry_path, "r", encoding="utf-8") as f:
            content = await f.read()
            return json.loads(content)

    @classmethod
    async def save_entry(cls, vault_path: str, entry: CompendiumEntry) -> str:
        """Saves a new ability to its own JSON file and updates the registry."""
        _, registry_path, entries_dir, _, _, _ = cls._get_paths(vault_path)

        # 1. Format the filename safely
        safe_name = "".join([c for c in entry.name if c.isalpha() or c.isdigit() or c == " "]).rstrip()
        filename = f"{safe_name.replace(' ', '_').lower()}.json"
        filepath = os.path.join(entries_dir, filename)

        # 2. Save the individual entry
        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(entry.model_dump_json(indent=4))

        # 3. Update the registry
        registry = await cls.load_registry(vault_path)
        registry[entry.name.lower()] = filepath

        async with aiofiles.open(registry_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(registry, indent=4))

        print(f"[Compendium] Taught engine new mechanic: {entry.name}")
        return filepath

    @classmethod
    async def get_entry(cls, vault_path: str, name: str) -> Optional[CompendiumEntry]:
        """Fetches and parses an entry by name."""
        registry = await cls.load_registry(vault_path)
        filepath = registry.get(name.lower())

        if not filepath or not os.path.exists(filepath):
            return None

        async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
            content = await f.read()
            return CompendiumEntry.model_validate_json(content)

    @classmethod
    async def save_class_definition(cls, vault_path: str, class_def: ClassDefinition) -> str:
        """Saves a class definition to its own JSON file."""
        _, _, _, _, classes_dir, _ = cls._get_paths(vault_path)
        safe_name = "".join([c for c in class_def.name if c.isalpha() or c.isdigit() or c == " "]).rstrip()
        filename = f"{safe_name.replace(' ', '_').lower()}.json"
        filepath = os.path.join(classes_dir, filename)

        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(class_def.model_dump_json(indent=4))

        return filepath

    @classmethod
    async def get_class_definition(cls, vault_path: str, name: str) -> Optional[ClassDefinition]:
        """Fetches and parses a class definition by name."""
        _, _, _, _, classes_dir, _ = cls._get_paths(vault_path)
        safe_name = "".join([c for c in name if c.isalpha() or c.isdigit() or c == " "]).rstrip()
        filename = f"{safe_name.replace(' ', '_').lower()}.json"
        filepath = os.path.join(classes_dir, filename)

        if not os.path.exists(filepath):
            return None

        async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
            content = await f.read()
            return ClassDefinition.model_validate_json(content)

    @classmethod
    async def save_subclass_definition(cls, vault_path: str, subclass_def: SubclassDefinition) -> str:
        """Saves a subclass definition to its own JSON file."""
        _, _, _, _, _, subclasses_dir = cls._get_paths(vault_path)
        safe_name = "".join([c for c in subclass_def.name if c.isalpha() or c.isdigit() or c == " "]).rstrip()
        filename = f"{safe_name.replace(' ', '_').lower()}.json"
        filepath = os.path.join(subclasses_dir, filename)

        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(subclass_def.model_dump_json(indent=4))

        return filepath

    @classmethod
    async def get_subclass_definition(cls, vault_path: str, name: str) -> Optional[SubclassDefinition]:
        """Fetches and parses a subclass definition by name."""
        _, _, _, _, _, subclasses_dir = cls._get_paths(vault_path)
        safe_name = "".join([c for c in name if c.isalpha() or c.isdigit() or c == " "]).rstrip()
        filename = f"{safe_name.replace(' ', '_').lower()}.json"
        filepath = os.path.join(subclasses_dir, filename)

        if not os.path.exists(filepath):
            return None

        async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
            content = await f.read()
            return SubclassDefinition.model_validate_json(content)
