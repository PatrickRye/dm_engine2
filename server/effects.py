from pydantic import BaseModel, Field
from typing import List, Union


class EffectCondition(BaseModel):
    """A condition that must be met for an effect to apply."""

    required_tag: str
    # Future enhancement: add more complex conditions like 'target_hp_below_50_percent'


class DamageEffect(BaseModel):
    """An effect that deals damage."""

    effect_type: str = "damage"
    damage_dice: str
    damage_type: str
    conditions: List[EffectCondition] = Field(default_factory=list)


class ModifierEffect(BaseModel):
    """An effect that applies a modifier to a stat."""

    effect_type: str = "modifier"
    stat: str  # e.g., "ac", "strength_mod"
    value: int
    conditions: List[EffectCondition] = Field(default_factory=list)


class ConditionEffect(BaseModel):
    """An effect that applies a condition to a target."""

    effect_type: str = "condition"
    condition: str  # e.g., "poisoned", "prone"
    duration_rounds: int = 1
    conditions: List[EffectCondition] = Field(default_factory=list)


# A union type to represent any possible effect
AnyEffect = Union[DamageEffect, ModifierEffect, ConditionEffect]
