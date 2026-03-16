import os
import yaml
import aiofiles
from pydantic import BaseModel, Field
from typing import List, Optional
from vault_io import get_journals_dir

class AppliedCondition(BaseModel):
    condition: str
    duration: str = "-1"

class StatModifier(BaseModel):
    stat: str
    value: int
    duration: str = "-1"

class TerrainEffectDefinition(BaseModel):
    label: str
    is_difficult: bool = False
    tags: List[str] = Field(default_factory=list)
    duration: str = "-1"
    trap_hazard: Optional[dict] = None # Maps to TrapDefinition

class SpellMechanics(BaseModel):
    requires_attack_roll: bool = False
    save_required: str = ""
    damage_dice: str = ""
    damage_type: str = ""
    half_damage_on_save: bool = False
    requires_concentration: bool = False
    granted_tags: List[str] = Field(default_factory=list)
    conditions_applied: List[AppliedCondition] = Field(default_factory=list)
    modifiers: List[StatModifier] = Field(default_factory=list)
    terrain_effect: Optional[TerrainEffectDefinition] = None

class SpellDefinition(BaseModel):
    name: str
    level: int = 0
    school: str = "evocation"
    casting_time: str = "1 action"
    range_str: str = "120 feet"
    components: List[str] = Field(default_factory=list)
    duration: str = "Instantaneous"
    description: str = ""
    mechanics: SpellMechanics = Field(default_factory=SpellMechanics)

class SpellCompendium:
    """Object-Oriented interface for strictly typing and persisting spell logic."""
    
    @staticmethod
    async def save_spell(vault_path: str, spell: SpellDefinition) -> str:
        j_dir = get_journals_dir(vault_path)
        spell_dir = os.path.join(j_dir, "Compendium", "Spells")
        os.makedirs(spell_dir, exist_ok=True)
        
        file_path = os.path.join(spell_dir, f"{spell.name}.md")
        yaml_str = yaml.dump(spell.model_dump(), sort_keys=False)
        
        content = f"---\ntags: [spell, compendium]\n{yaml_str}---\n# {spell.name}\n\n{spell.description}\n"
        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
            await f.write(content)
            
        return file_path

    @staticmethod
    async def load_spell(vault_path: str, spell_name: str) -> Optional[SpellDefinition]:
        j_dir = get_journals_dir(vault_path)
        file_path = os.path.join(j_dir, "Compendium", "Spells", f"{spell_name}.md")
        if not os.path.exists(file_path): return None
            
        async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
            content = await f.read()
            
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                yaml_data = yaml.safe_load(parts[1])
                if yaml_data: return SpellDefinition(**yaml_data)
        return None