import os
import yaml
import aiofiles
from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Union
from vault_io import get_journals_dir
from spell_system import SpellMechanics, StatModifier


class BaseItem(BaseModel):
    """The base class that all inventory items inherit from (Is-A relationship)."""

    name: str
    description: str = ""
    weight: float = 0.0
    cost: str = "0 gp"
    rarity: Literal["Common", "Uncommon", "Rare", "Very Rare", "Legendary", "Artifact", "Unknown"] = "Common"
    requires_attunement: bool = False
    is_magical: bool = False
    tags: List[str] = Field(default_factory=list)
    # "Has-A" composition: Items can possess passive stat modifiers (e.g., +1 to AC, or setting STR to 19)
    modifiers: List[StatModifier] = Field(default_factory=list)


class WeaponItem(BaseItem):
    """Is-A BaseItem, specifically tailored for attack resolution."""

    item_type: Literal["Weapon"] = "Weapon"
    weapon_category: Literal["Simple Melee", "Martial Melee", "Simple Ranged", "Martial Ranged", "Unknown"] = "Unknown"
    damage_dice: str = "1d4"
    damage_type: str = "bludgeoning"
    properties: List[str] = Field(default_factory=list)  # e.g. ["finesse", "light", "reach"]
    normal_range: Optional[int] = None
    long_range: Optional[int] = None
    magic_bonus: int = 0  # Applies natively to attack/damage rolls


class ArmorItem(BaseItem):
    """Is-A BaseItem, tailored for defense calculations."""

    item_type: Literal["Armor"] = "Armor"
    armor_category: Literal["Light", "Medium", "Heavy", "Shield", "Unknown"] = "Unknown"
    base_ac: int = 10
    plus_ac_bonus: int = 0
    max_dex_bonus: Optional[int] = None
    stealth_disadvantage: bool = False
    strength_requirement: int = 0


class WondrousItem(BaseItem):
    """Is-A BaseItem, representing consumables, tools, and activated magic items."""

    item_type: Literal["Wondrous", "Consumable", "Tool"] = "Wondrous"
    charges: Optional[int] = None
    max_charges: Optional[int] = None
    recharge_condition: str = ""
    consume_on_use: bool = False
    # "Has-A" composition: Wondrous items can hold a SpellMechanics object to act as a spellcaster
    active_mechanics: Optional[SpellMechanics] = None


class ItemCompendium:
    """Compendium persistence layer for strictly typed D&D Equipment."""

    @staticmethod
    async def save_item(vault_path: str, item: Union[WeaponItem, ArmorItem, WondrousItem]) -> str:
        j_dir = get_journals_dir(vault_path)
        item_dir = os.path.join(j_dir, "Compendium", "Items")
        os.makedirs(item_dir, exist_ok=True)

        file_path = os.path.join(item_dir, f"{item.name}.md")
        yaml_str = yaml.dump(item.model_dump(), sort_keys=False)

        content = f"---\ntags: [item, compendium]\n{yaml_str}---\n# {item.name}\n\n{item.description}\n"
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(content)

        return file_path

    @staticmethod
    async def load_item(vault_path: str, item_name: str) -> Optional[Union[WeaponItem, ArmorItem, WondrousItem]]:
        j_dir = get_journals_dir(vault_path)
        file_path = os.path.join(j_dir, "Compendium", "Items", f"{item_name}.md")
        if not os.path.exists(file_path):
            return None

        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                yaml_data = yaml.safe_load(parts[1])
                if yaml_data:
                    item_type = yaml_data.get("item_type", "Wondrous")
                    if item_type == "Weapon":
                        return WeaponItem(**yaml_data)
                    elif item_type == "Armor":
                        return ArmorItem(**yaml_data)
                    else:
                        return WondrousItem(**yaml_data)
        return None
