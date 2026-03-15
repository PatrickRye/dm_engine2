# AGENT_NOTE: When modifying this file, please use the test framework in `test_dnd_rules_engine.py` to validate your changes.
# To run the tests, execute `python test_dnd_rules_engine.py` in your terminal.

import uuid
import random
import math
import re
from enum import IntEnum
from typing import ClassVar, Dict, Any, List, Callable, Optional, Tuple
from pydantic import BaseModel, Field, PrivateAttr
from state import ClassLevel, Feature, ClassDefinition, SubclassDefinition
from spatial_engine import spatial_service

# ==========================================
# 1. THE REGISTRY & MEMORY DECOUPLING
# ==========================================

class BaseGameEntity(BaseModel):
    """
    The foundational object. Everything in the game (players, weapons, spells) 
    inherits from this to get a UUID and be added to the global registry.
    """
    entity_uuid: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    size: float = 5.0 # represents a standard 5x5 foot D&D grid space
    height: float = 5.0 # Defaults to size unless specifically set (e.g. Medium creature = 5x5x5)
    
    _registry: ClassVar[Dict[uuid.UUID, 'BaseGameEntity']] = {}

    def model_post_init(self, __context: Any) -> None:
        """Automatically registers the object upon creation."""
        BaseGameEntity._registry[self.entity_uuid] = self

    @classmethod
    def get(cls, uid: uuid.UUID) -> Optional['BaseGameEntity']:
        return cls._registry.get(uid)

    @classmethod
    def remove(cls, uid: uuid.UUID) -> None:
        if uid in cls._registry:
            del cls._registry[uid]


# ==========================================
# 2. THE MODIFIABLE VALUE SYSTEM
# ==========================================

class ModifierPriority(IntEnum):
    OVERRIDE = 1
    ADDITIVE = 2
    MULTIPLIER = 3
    CONDITIONAL = 4
    LIMIT = 5

class NumericalModifier(BaseModel):
    mod_uuid: uuid.UUID = Field(default_factory=uuid.uuid4)
    priority: ModifierPriority
    value: int
    source_name: str
    duration_events: int = -1
    duration_seconds: int = -1
    applied_initiative: int = 0
    source_uuid: Optional[uuid.UUID] = None

class ModifiableValue(BaseModel):
    """Wraps core stats (AC, HP, Ability Scores) to allow dynamic recalculation."""
    base_value: int
    modifiers: List[NumericalModifier] = Field(default_factory=list)

    @property
    def total(self) -> int:
        current_val = self.base_value
        sorted_mods = sorted(self.modifiers, key=lambda m: m.priority)
        
        overrides = [m for m in sorted_mods if m.priority == ModifierPriority.OVERRIDE]
        if overrides:
            current_val = overrides[-1].value
            
        for m in [m for m in sorted_mods if m.priority == ModifierPriority.ADDITIVE]:
            current_val += m.value
            
        for m in [m for m in sorted_mods if m.priority == ModifierPriority.MULTIPLIER]:
            current_val = int(current_val * m.value)
            
        return current_val

    def add_modifier(self, mod: NumericalModifier):
        self.modifiers.append(mod)

    def remove_modifier(self, mod_uuid: uuid.UUID):
        self.modifiers = [m for m in self.modifiers if m.mod_uuid != mod_uuid]


# ==========================================
# 3. ENTITIES & EQUIPMENT
# ==========================================

from enum import Enum

class WeaponProperty(str, Enum):
    LIGHT = "light"
    FINESSE = "finesse"
    THROWN = "thrown"
    VERSATILE = "versatile"
    TWO_HANDED = "two-handed"
    AMMUNITION = "ammunition"
    REACH = "reach"
    HEAVY = "heavy"
    SPECIAL = "special"

class Weapon(BaseGameEntity):
    damage_dice: str
    damage_type: str
    properties: List[WeaponProperty] = Field(default_factory=list)
    cost: str = "0 gp"
    weight: float = 0.0
    magic_bonus: int = 0

    def get_attack_modifier(self, wielder: 'Creature') -> ModifiableValue:
        if WeaponProperty.FINESSE in self.properties:
            if wielder.dexterity_mod.total > wielder.strength_mod.total:
                return wielder.dexterity_mod
        return wielder.strength_mod

    def get_damage_modifier(self, wielder: 'Creature') -> ModifiableValue:
        return self.get_attack_modifier(wielder)

class MeleeWeapon(Weapon):
    pass

class RangedWeapon(Weapon):
    normal_range: int
    long_range: int

# Decorator Pattern for Magical Weapons
class MagicWeaponDecorator(Weapon):
    _wrapped_weapon: Weapon = PrivateAttr()

    def __init__(self, *, weapon: Weapon, **data):
        wrapped_data = weapon.model_dump(exclude={"entity_uuid", "name"})
        combined_data = {**wrapped_data, **data}
        super().__init__(**combined_data)
        object.__setattr__(self, '_wrapped_weapon', weapon)

    def __getattr__(self, name: str) -> Any:
        wrapped_weapon = super().__getattribute__('_wrapped_weapon')
        return getattr(wrapped_weapon, name)

class BonusWeapon(MagicWeaponDecorator):
    magic_bonus: int

class ViciousWeapon(MagicWeaponDecorator):
    pass

class CursedWeapon(MagicWeaponDecorator):
    curse_description: str
    pass

class DamageCondition(BaseModel):
    """A rule for applying conditional damage."""
    required_tag: str
    extra_damage_dice: str
    damage_type: str

class ConditionalDamageWeapon(MagicWeaponDecorator):
    """A decorator that deals extra damage based on target properties."""
    conditions: List[DamageCondition]
    _subscribed = False

    def __init__(self, *, weapon: Weapon, **data):
        super().__init__(weapon=weapon, **data)
        if not ConditionalDamageWeapon._subscribed:
            EventBus.subscribe("MeleeAttack", self.handle_attack, priority=50)
            ConditionalDamageWeapon._subscribed = True

    def handle_attack(self, event: 'GameEvent'):
        # This handler should run before the main resolve_attack_handler
        if event.status != EventStatus.PRE_EVENT:
            return

        attacker = BaseGameEntity.get(event.source_uuid)
        if attacker.equipped_weapon_uuid != self.entity_uuid:
            return

        target = BaseGameEntity.get(event.target_uuid)
        for condition in self.conditions:
            if condition.required_tag in target.tags or condition.required_tag == target.alignment:
                if "extra_damage_dice" not in event.payload:
                    event.payload["extra_damage_dice"] = []
                event.payload["extra_damage_dice"].append(condition.extra_damage_dice)
                print(f"[Engine] BONUS: {self.name} will deal extra {condition.extra_damage_dice} {condition.damage_type} damage to the {condition.required_tag}!")

class ActiveCondition(BaseModel):
    condition_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    duration_seconds: int = -1
    source_name: str = "Unknown"
    applied_initiative: int = 0
    source_uuid: Optional[uuid.UUID] = None

class Creature(BaseGameEntity):
    max_hp: int = 10
    hp: ModifiableValue
    ac: ModifiableValue
    strength_mod: ModifiableValue
    dexterity_mod: ModifiableValue
    constitution_mod: ModifiableValue = Field(default_factory=lambda: ModifiableValue(base_value=0))
    intelligence_mod: ModifiableValue = Field(default_factory=lambda: ModifiableValue(base_value=0))
    wisdom_mod: ModifiableValue = Field(default_factory=lambda: ModifiableValue(base_value=0))
    charisma_mod: ModifiableValue = Field(default_factory=lambda: ModifiableValue(base_value=0))
    spell_save_dc: ModifiableValue = Field(default_factory=lambda: ModifiableValue(base_value=10))
    spell_attack_bonus: ModifiableValue = Field(default_factory=lambda: ModifiableValue(base_value=0))
    equipped_weapon_uuid: Optional[uuid.UUID] = None
    classes: List[ClassLevel] = Field(default_factory=list)
    active_mechanics: List[str] = Field(default_factory=list)
    active_conditions: List[ActiveCondition] = Field(default_factory=list)
    resources: Dict[str, str] = Field(default_factory=dict)
    features: List[Feature] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    alignment: str = "neutral"
    vulnerabilities: List[str] = Field(default_factory=list)
    resistances: List[str] = Field(default_factory=list)
    immunities: List[str] = Field(default_factory=list)
    concentrating_on: str = ""
    reaction_used: bool = False
    legendary_actions_max: int = 0
    legendary_actions_current: int = 0
    speed: int = 30
    movement_remaining: int = 30

    @property
    def character_level(self) -> int:
        return sum(c.level for c in self.classes)

    def apply_features(self, class_def: 'ClassDefinition', level: int):
        for feature in class_def.features:
            if feature.level == level and feature not in self.features:
                self.features.append(feature)

    def apply_subclass_features(self, subclass_def: 'SubclassDefinition', level: int):
        for feature in subclass_def.features:
            if feature.level == level and feature not in self.features:
                self.features.append(feature)

# ==========================================
# 4. EVENT-DRIVEN COMBAT ARCHITECTURE
# ==========================================

class EventStatus(IntEnum):
    PENDING = 0
    PRE_EVENT = 1
    EXECUTION = 2
    POST_EVENT = 3
    RESOLVED = 4
    CANCELLED = 5

class GameEvent(BaseModel):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    source_uuid: uuid.UUID
    target_uuid: Optional[uuid.UUID] = None
    status: EventStatus = EventStatus.PENDING
    payload: Dict[str, Any] = Field(default_factory=dict)

class EventBus:
    """Manages the lifecycle and listeners for all game events."""
    _listeners: ClassVar[Dict[str, List[Tuple[Callable, int]]]] = {}

    @classmethod
    def subscribe(cls, event_type: str, handler: Callable, priority: int = 10):
        if event_type not in cls._listeners:
            cls._listeners[event_type] = []
        cls._listeners[event_type].append((handler, priority))
        # Sort by priority, lowest number first
        cls._listeners[event_type].sort(key=lambda x: x[1])

    @classmethod
    def dispatch(cls, event: GameEvent) -> GameEvent:
        print(f"\n--- Dispatched Event: {event.event_type} ---")
        
        event.status = EventStatus.PRE_EVENT
        cls._notify(event)
        if event.status == EventStatus.CANCELLED:
            print("Event was CANCELLED during Pre-Event.")
            return event
            
        event.status = EventStatus.EXECUTION
        cls._notify(event)
        
        event.status = EventStatus.POST_EVENT
        cls._notify(event)
        
        event.status = EventStatus.RESOLVED
        cls._notify(event)
        return event

    @classmethod
    def _notify(cls, event: GameEvent):
        for handler, _ in cls._listeners.get(event.event_type, []):
            handler(event)

# ==========================================
# 5. CORE MECHANICS & HANDLERS
# ==========================================

def parse_duration_to_seconds(duration_str: str) -> int:
    """Centralized utility to convert standard D&D time strings to raw seconds."""
    duration_str = str(duration_str).lower()
    if duration_str in ["-1", "instantaneous", "permanent"]: return -1
    match = re.search(r'(\d+)\s*(round|minute|hour|day)', duration_str)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        if unit == "round": return val * 6
        elif unit == "minute": return val * 60
        elif unit == "hour": return val * 3600
        elif unit == "day": return val * 86400
    return -1

def roll_dice(notation: str) -> int:
    """Parses and rolls generic D&D dice formulas (e.g., '1d8', '2d6+3')."""
    match = re.match(r"(\d+)d(\d+)(?:\s*([+-])\s*(\d+))?", notation.strip().lower())
    if not match:
        return 0
        
    count, faces = int(match.group(1)), int(match.group(2))
    modifier_op = match.group(3)
    modifier_val = int(match.group(4)) if match.group(4) else 0
    
    total = sum(random.randint(1, faces) for _ in range(count))
    
    if modifier_op == '+': total += modifier_val
    elif modifier_op == '-': total -= modifier_val
    
    return max(0, total) # Prevents negative damage

def resolve_spell_cast_handler(event: GameEvent):
    """Calculates spell hits, saving throws, and damage across multiple targets."""
    if event.status != EventStatus.EXECUTION: return

    caster: Creature = BaseGameEntity.get(event.source_uuid)
    mechanics = event.payload.get("mechanics", {})
    target_uuids = event.payload.get("target_uuids", [])
    
    # Roll base damage/healing once for all targets
    damage_dice = mechanics.get("damage_dice", "")
    base_damage = roll_dice(damage_dice) if damage_dice else 0
    damage_type = mechanics.get("damage_type", "unknown")
    
    save_required = mechanics.get("save_required", "").lower()
    requires_attack_roll = mechanics.get("requires_attack_roll", False)
    half_damage_on_save = mechanics.get("half_damage_on_save", False)
    
    requires_concentration = mechanics.get("requires_concentration", False)
    if requires_concentration:
        if caster.concentrating_on:
            EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid))
        caster.concentrating_on = event.payload.get("ability_name", "Unknown")
        print(f"[Engine] {caster.name} is now concentrating on {caster.concentrating_on}.")

    current_init = event.payload.get("current_initiative", 0)
    
    results = []
    
    for t_uuid in target_uuids:
        target: Creature = BaseGameEntity.get(t_uuid)
        if not target: continue
        
        target_damage = base_damage
        hit_or_save_str = "Auto-hit"
        
        # 1. Spell Attack Roll
        if requires_attack_roll:
            attack_roll = random.randint(1, 20)
            total_attack = attack_roll + caster.spell_attack_bonus.total
            if total_attack >= target.ac.total or attack_roll == 20:
                hit_or_save_str = f"Hit (Rolled {total_attack} vs AC {target.ac.total})"
                if attack_roll == 20:
                    target_damage += roll_dice(damage_dice) # Crit
                    hit_or_save_str = "Critical Hit!"
            else:
                target_damage = 0
                hit_or_save_str = f"Miss (Rolled {total_attack} vs AC {target.ac.total})"
                
        # 2. Saving Throw
        elif save_required:
            save_mod_val = getattr(target, f"{save_required}_mod").total if hasattr(target, f"{save_required}_mod") else 0
            save_roll = random.randint(1, 20)
            total_save = save_roll + save_mod_val
            dc = caster.spell_save_dc.total
            
            if total_save >= dc:
                hit_or_save_str = f"Saved (Rolled {total_save} vs DC {dc})"
                target_damage = target_damage // 2 if half_damage_on_save else 0
            else:
                hit_or_save_str = f"Failed Save (Rolled {total_save} vs DC {dc})"

        # 3. Process Damage (Reusing apply_damage_handler logic internally for multiple targets)
        if target_damage > 0:
            event.payload["hit"] = True # Flag for potential reaction handlers
            # Apply resistances/vulnerabilities
            if damage_type in target.immunities: target_damage = 0
            elif damage_type in target.vulnerabilities: target_damage *= 2
            elif damage_type in target.resistances: target_damage = target_damage // 2
            target.hp.base_value -= target_damage
            
        # 4. Process Conditions
        applied_effects = False
        if requires_attack_roll and ("Hit" in hit_or_save_str or "Critical" in hit_or_save_str):
            applied_effects = True
        elif save_required and "Failed Save" in hit_or_save_str:
            applied_effects = True
        elif not requires_attack_roll and not save_required:
            applied_effects = True
            
        if applied_effects:
            for cond_data in mechanics.get("conditions_applied", []):
                cond_name = cond_data.get("condition", "Unknown")
                duration_secs = parse_duration_to_seconds(cond_data.get("duration", "-1"))
                
                target.active_conditions.append(ActiveCondition(
                    name=cond_name,
                    duration_seconds=duration_secs,
                    source_name=event.payload.get("ability_name", "Unknown"),
                    applied_initiative=current_init,
                    source_uuid=caster.entity_uuid
                ))
                results.append(f"[{target.name}] is now {cond_name}!")
                
            for mod_data in mechanics.get("modifiers", []):
                target_stat = mod_data.get("stat")
                duration_secs = parse_duration_to_seconds(mod_data.get("duration", "-1"))
                if hasattr(target, target_stat):
                    stat_obj = getattr(target, target_stat)
                    if isinstance(stat_obj, ModifiableValue):
                        stat_obj.add_modifier(NumericalModifier(
                            priority=ModifierPriority.ADDITIVE,
                            value=int(mod_data.get("value", 0)),
                            source_name=event.payload.get("ability_name", "Unknown"),
                            duration_seconds=duration_secs,
                            applied_initiative=current_init,
                            source_uuid=caster.entity_uuid
                        ))
                        if event.payload.get("ability_name", "Unknown") not in target.active_mechanics:
                            target.active_mechanics.append(event.payload.get("ability_name", "Unknown"))
                        results.append(f"[{target.name}] gained modifier to {target_stat}.")
                
        results.append(f"[{target.name}] {hit_or_save_str}. Took {target_damage} {damage_type} damage. HP: {target.hp.base_value}")
        
    event.payload["results"] = results

def resolve_attack_handler(event: GameEvent):
    """Calculates if an attack hits and its base damage. Listens to EXECUTION phase."""
    if event.status != EventStatus.EXECUTION: return

    attacker: Creature = BaseGameEntity.get(event.source_uuid)
    target: Creature = BaseGameEntity.get(event.target_uuid)
    weapon: Weapon = BaseGameEntity.get(attacker.equipped_weapon_uuid)

    attack_mod = weapon.get_attack_modifier(attacker)
    attack_bonus = attack_mod.total + weapon.magic_bonus
    
    # Evaluate Spatial Logic: Range & Cover (Gracefully falls back if shapely is not installed)
    dist, cover = spatial_service.get_distance_and_cover(attacker.entity_uuid, target.entity_uuid)
    if cover == "Total":
        print(f"[Engine] {attacker.name} cannot hit {target.name}. Target has TOTAL COVER.")
        event.payload["hit"] = False
        return
        
    # Enforce Range Limits for Ranged Weapons
    if hasattr(weapon, 'normal_range') and hasattr(weapon, 'long_range'):
        if dist > weapon.long_range:
            print(f"[Engine] {attacker.name} cannot hit {target.name}. Target is out of maximum range ({dist:.1f}ft > {weapon.long_range}ft).")
            event.payload["hit"] = False
            return
        elif dist > weapon.normal_range:
            print(f"[Engine] Target is at long range ({dist:.1f}ft > {weapon.normal_range}ft). Applying DISADVANTAGE.")
            event.payload["disadvantage"] = True
            
        # Check for hostile creatures within 5 feet of the attacker's edge
        check_radius = (attacker.size / 2.0) + 5.0
        nearby_uuids = spatial_service.get_targets_in_radius(attacker.x, attacker.y, check_radius)
        for uid in nearby_uuids:
            if uid == attacker.entity_uuid:
                continue
            nearby_ent = BaseGameEntity.get(uid)
            if isinstance(nearby_ent, Creature) and nearby_ent.hp.base_value > 0:
                is_pc = any(t in attacker.tags for t in ["pc", "player", "party_npc"])
                is_nearby_pc = any(t in nearby_ent.tags for t in ["pc", "player", "party_npc"])
                if is_pc != is_nearby_pc:
                    # Generic Mechanics Check
                    if "ignore_ranged_melee_disadvantage" in attacker.tags:
                        print(f"[Engine] {attacker.name} has a mechanic mitigating ranged disadvantage from nearby hostiles.")
                    else:
                        print(f"[Engine] Hostile creature ({nearby_ent.name}) is within 5 feet of {attacker.name}. Applying DISADVANTAGE to ranged attack.")
                        event.payload["disadvantage"] = True
                break
    else:
        # Enforce Reach Limits for Melee Weapons
        # Multiply by 1.5 to safely account for Euclidean distance on diagonals (5ft reach allows ~7.07ft diagonal distance)
        base_reach = 10.0 if WeaponProperty.REACH in weapon.properties else 5.0
        if dist > (base_reach * 1.5):
            print(f"[Engine] {attacker.name} cannot hit {target.name}. Target is out of melee reach ({dist:.1f}ft > {base_reach}ft).")
            event.payload["hit"] = False
            return

    cover_ac_bonus = 2 if cover == "Half" else (5 if cover == "Three-Quarters" else 0)
    target_ac = target.ac.total + cover_ac_bonus

    # Handle advantage and disadvantage
    roll1 = random.randint(1, 20)
    roll2 = random.randint(1, 20)
    
    if event.payload.get("advantage"):
        d20_roll = max(roll1, roll2)
        print("[Engine] Attack has ADVANTAGE, rolling twice and taking the higher roll.")
    elif event.payload.get("disadvantage"):
        d20_roll = min(roll1, roll2)
        print("[Engine] Attack has DISADVANTAGE, rolling twice and taking the lower roll.")
    else:
        d20_roll = roll1

    total_attack = d20_roll + attack_bonus

    is_critical_hit = d20_roll == 20
    is_hit = is_critical_hit or total_attack >= target_ac

    cover_msg = f" (Includes +{cover_ac_bonus} {cover} Cover)" if cover_ac_bonus > 0 else ""
    print(f"[Engine] {attacker.name} rolls a {d20_roll} ({roll1}, {roll2} if adv/disadv) + {attack_bonus} = {total_attack} vs AC {target_ac}{cover_msg}")

    if is_hit:
        if is_critical_hit:
            print("[Engine] CRITICAL HIT!")
        print(f"[Engine] HIT!")
        
        damage_mod = weapon.get_damage_modifier(attacker)
        # Roll base damage dice
        base_damage = roll_dice(weapon.damage_dice) + damage_mod.total + weapon.magic_bonus
        
        # Add conditional damage dice
        extra_damage = 0
        if "extra_damage_dice" in event.payload:
            for dice in event.payload["extra_damage_dice"]:
                extra_damage += roll_dice(dice)

        total_damage = base_damage + extra_damage

        # Double all dice on a critical hit
        if is_critical_hit:
            crit_damage = roll_dice(weapon.damage_dice)
            if "extra_damage_dice" in event.payload:
                for dice in event.payload["extra_damage_dice"]:
                    crit_damage += roll_dice(dice)
            total_damage += crit_damage
        
        event.payload["hit"] = True
        event.payload["damage"] = total_damage
        event.payload["damage_type"] = weapon.damage_type
        event.payload["critical"] = is_critical_hit
    else:
        print(f"[Engine] MISS! The attack glances off {target.name}'s armor.")
        event.payload["hit"] = False

    # --- Sentinel / Protector Reaction Check ---
    protectors = []
    for uid, pot_protector in BaseGameEntity._registry.items():
        if uid == attacker.entity_uuid or uid == target.entity_uuid: continue
        if not isinstance(pot_protector, Creature) or pot_protector.hp.base_value <= 0: continue
        
        has_reaction = not pot_protector.reaction_used
        has_legendary = pot_protector.legendary_actions_current > 0
        if not has_reaction and not has_legendary: continue
        
        if "protector_reaction_attack" not in pot_protector.tags: continue
        
        # Must be friendly to target, hostile to attacker
        is_attacker_pc = any(t in attacker.tags for t in ["pc", "player", "party_npc"])
        is_protector_pc = any(t in pot_protector.tags for t in ["pc", "player", "party_npc"])
        if is_attacker_pc == is_protector_pc: continue
        
        # Must be within 5ft of the attacker
        dist_to_attacker = spatial_service.calculate_distance(pot_protector.x, pot_protector.y, pot_protector.z, attacker.x, attacker.y, attacker.z)
        if dist_to_attacker <= 7.5: # 5ft + diagonal allowance
            protectors.append(pot_protector.name)
            
    if protectors:
        event.payload["protector_alerts"] = protectors

def apply_damage_handler(event: GameEvent):
    """Applies final damage to a target, considering immunities, resistances, and vulnerabilities."""
    if event.status != EventStatus.POST_EVENT or not event.payload.get("hit"):
        return
        
    target: Creature = BaseGameEntity.get(event.target_uuid)
    damage = event.payload.get("damage", 0)
    damage_type = event.payload.get("damage_type", "unknown")

    if damage > 0:
        # Check for immunities first
        if damage_type in target.immunities:
            damage = 0
            print(f"[Engine] {target.name} is IMMUNE to {damage_type}!")
        else:
            # Then check for vulnerabilities and resistances
            if damage_type in target.vulnerabilities:
                damage *= 2
                print(f"[Engine] {target.name} is VULNERABLE to {damage_type}! Damage is doubled.")
            elif damage_type in target.resistances:
                damage = damage // 2 # Halve the damage, rounding down
                print(f"[Engine] {target.name} is RESISTANT to {damage_type}! Damage is halved.")

        target.hp.base_value -= damage
        print(f"[Engine] {target.name} takes {damage} {damage_type} damage. HP remaining: {target.hp.base_value}")

        if target.hp.base_value <= 0:
            if target.concentrating_on:
                EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=target.entity_uuid))
        elif damage > 0 and target.concentrating_on:
            dc = max(10, damage // 2)
            print(f"[Engine] SYSTEM ALERT: {target.name} took damage while concentrating on '{target.concentrating_on}'. LLM MUST prompt a Constitution saving throw (DC {dc}). Use `drop_concentration` tool if failed.")

def handle_rest_event(event: GameEvent):
    """Deterministically recharges resources and resets HP on a rest."""
    if event.status != EventStatus.EXECUTION: return
    
    rest_type = event.payload.get("rest_type", "short")
    target_uuids = event.payload.get("target_uuids", [])
    
    for uid in target_uuids:
        target = BaseGameEntity.get(uid)
        if not isinstance(target, Creature): continue
        
        if rest_type == "long":
            target.hp.base_value = target.max_hp
            for res_name, res_val in target.resources.items():
                match = re.match(r"(\d+)\s*/\s*(\d+)", str(res_val))
                if match:
                    maximum = match.group(2)
                    target.resources[res_name] = f"{maximum}/{maximum}"
            print(f"[Engine] {target.name} finished a Long Rest. HP and resources fully restored.")

def handle_advance_time_event(event: GameEvent):
    """Deterministically expires temporary modifiers when time advances."""
    if event.status != EventStatus.EXECUTION: return
    
    seconds_advanced = event.payload.get("seconds_advanced", 0)
    target_init = event.payload.get("target_initiative", None)
    if seconds_advanced <= 0: return
    
    for uid, entity in BaseGameEntity._registry.items():
        if isinstance(entity, Creature):
            # Check all ModifiableValue attributes for temporary modifiers
            for field_name in entity.model_fields:
                stat_val = getattr(entity, field_name)
                if isinstance(stat_val, ModifiableValue):
                    expired_mods = []
                    for mod in stat_val.modifiers:
                        if mod.duration_seconds > 0:
                            if target_init is None or mod.applied_initiative == target_init:
                                mod.duration_seconds -= seconds_advanced
                                if mod.duration_seconds <= 0:
                                    expired_mods.append(mod)
                    
                    for mod in expired_mods:
                        stat_val.remove_modifier(mod.mod_uuid)
                        if mod.source_name in entity.active_mechanics:
                            entity.active_mechanics.remove(mod.source_name)
                        print(f"[Engine] {entity.name}'s temporary mechanic '{mod.source_name}' on '{field_name}' has expired.")
            
            # Clean up active conditions
            expired_conditions = []
            for cond in entity.active_conditions:
                if cond.duration_seconds > 0:
                    if target_init is None or cond.applied_initiative == target_init:
                        cond.duration_seconds -= seconds_advanced
                        if cond.duration_seconds <= 0:
                            expired_conditions.append(cond)
            for cond in expired_conditions:
                entity.active_conditions.remove(cond)
                print(f"[Engine] {entity.name} is no longer {cond.name}.")

def shield_spell_reaction_handler(event: GameEvent):
    """Intercepts an attack BEFORE it resolves and magically raises AC."""
    if event.status != EventStatus.PRE_EVENT: return
    
    target: Creature = BaseGameEntity.get(event.target_uuid)
    if "can_cast_shield" in target.tags and not target.reaction_used:
        print(f"[Engine] REACTION TRIGGERED: {target.name} casts Shield!")
        target.reaction_used = True
        current_init = event.payload.get("current_initiative", 0)
        shield_mod = NumericalModifier(
            priority=ModifierPriority.ADDITIVE, 
            value=5, 
            source_name="Shield Spell",
            duration_seconds=6, # Shield lasts 1 round
            applied_initiative=current_init
        )
        target.ac.add_modifier(shield_mod)
        if "Shield Spell" not in target.active_mechanics:
            target.active_mechanics.append("Shield Spell")
        print(f"[Engine] {target.name}'s AC is temporarily raised to {target.ac.total}")

def handle_drop_concentration_event(event: GameEvent):
    """Removes all modifiers and conditions applied by a concentrated spell."""
    if event.status != EventStatus.EXECUTION: return
    
    caster: Creature = BaseGameEntity.get(event.source_uuid)
    if not caster or not caster.concentrating_on: return
    
    spell_name = caster.concentrating_on
    print(f"[Engine] {caster.name} lost concentration on {spell_name}.")
    
    for uid, entity in BaseGameEntity._registry.items():
        if isinstance(entity, Creature):
            for field_name in entity.model_fields:
                stat_val = getattr(entity, field_name)
                if isinstance(stat_val, ModifiableValue):
                    expired_mods = [m for m in stat_val.modifiers if m.source_name == spell_name and (m.source_uuid == caster.entity_uuid or m.source_uuid is None)]
                    for mod in expired_mods:
                        stat_val.remove_modifier(mod.mod_uuid)
                        if mod.source_name in entity.active_mechanics:
                            entity.active_mechanics.remove(mod.source_name)
                        print(f"[Engine] {entity.name}'s {spell_name} modifier on '{field_name}' faded.")
            
            expired_conditions = [c for c in entity.active_conditions if c.source_name == spell_name and (c.source_uuid == caster.entity_uuid or c.source_uuid is None)]
            for cond in expired_conditions:
                entity.active_conditions.remove(cond)
                print(f"[Engine] {entity.name} is no longer {cond.name} from {spell_name}.")
                
    caster.concentrating_on = ""

def validate_movement_handler(event: GameEvent):
    """Validates movement bounds, speed limits, and difficult terrain costs."""
    if event.status != EventStatus.PRE_EVENT: return
    movement_type = event.payload.get("movement_type", "walk").lower()
    
    # Teleport, fall, forced don't consume standard movement points
    if movement_type in ["teleport", "forced", "fall"]: 
        return
        
    entity = BaseGameEntity.get(event.source_uuid)
    if not isinstance(entity, Creature): return
    
    target_x = event.payload.get("target_x")
    target_y = event.payload.get("target_y")
    target_z = event.payload.get("target_z", entity.z)
    
    normal_dist, diff_dist = spatial_service.calculate_path_terrain_costs(entity.x, entity.y, entity.z, target_x, target_y, target_z)
    
    # Evaluate Character Traits/Feats/Items
    if "ignore_difficult_terrain" in entity.tags:
        normal_dist += diff_dist
        diff_dist = 0.0
        
    total_cost = math.ceil(normal_dist + (diff_dist * 2)) # Difficult terrain costs twice as much
    
    if total_cost > entity.movement_remaining:
        event.status = EventStatus.CANCELLED
        event.payload["error"] = f"Movement cost ({total_cost}ft) exceeds remaining speed ({entity.movement_remaining}ft). Normal dist: {normal_dist:.1f}ft, Difficult dist: {diff_dist:.1f}ft."
        return
        
    event.payload["cost"] = total_cost

def resolve_movement_handler(event: GameEvent):
    """Evaluates movement to see if it provokes opportunity attacks."""
    if event.status != EventStatus.EXECUTION: return
    
    movement_type = event.payload.get("movement_type", "walk").lower()
    
    # Teleportation and forced movement do not provoke opportunity attacks natively
    if movement_type in ["teleport", "forced"]:
        return
        
    entity: Creature = BaseGameEntity.get(event.source_uuid)
    target_x = event.payload.get("target_x")
    target_y = event.payload.get("target_y")
    target_z = event.payload.get("target_z", entity.z)
    
    opportunity_attackers = []
    for uid, potential_attacker in BaseGameEntity._registry.items():
        if uid == entity.entity_uuid: continue
        if not isinstance(potential_attacker, Creature): continue
        if potential_attacker.hp.base_value <= 0: continue
        
        has_reaction = not potential_attacker.reaction_used
        has_legendary = potential_attacker.legendary_actions_current > 0
        if not has_reaction and not has_legendary: continue
        
        # Check hostility
        is_entity_pc = any(t in entity.tags for t in ["pc", "player", "party_npc"])
        is_attacker_pc = any(t in potential_attacker.tags for t in ["pc", "player", "party_npc"])
        if is_entity_pc == is_attacker_pc: continue
        
        # Disengage Check
        if movement_type == "disengage" and "ignores_disengage" not in potential_attacker.tags:
            continue
            
        # Reach
        reach = 5.0
        if potential_attacker.equipped_weapon_uuid:
            weapon = BaseGameEntity.get(potential_attacker.equipped_weapon_uuid)
            if hasattr(weapon, 'properties') and WeaponProperty.REACH in weapon.properties:
                reach = 10.0
        reach *= 1.5 # Diagonal allowance
        
        dist_before = spatial_service.calculate_distance(potential_attacker.x, potential_attacker.y, potential_attacker.z, entity.x, entity.y, entity.z)
        dist_after = spatial_service.calculate_distance(potential_attacker.x, potential_attacker.y, potential_attacker.z, target_x, target_y, target_z)
        
        if dist_before <= reach and dist_after > reach:
            opportunity_attackers.append(potential_attacker.name)
            
    if opportunity_attackers:
        event.payload["opportunity_attackers"] = opportunity_attackers

def consume_movement_handler(event: GameEvent):
    """Deducts movement speed after a successful, un-cancelled move."""
    if event.status != EventStatus.POST_EVENT: return
    entity = BaseGameEntity.get(event.source_uuid)
    if isinstance(entity, Creature) and "cost" in event.payload:
        entity.movement_remaining -= event.payload["cost"]

# Register handlers to the Event Bus
# Lower priority numbers run first.
EventBus.subscribe("MeleeAttack", shield_spell_reaction_handler, priority=1)
EventBus.subscribe("MeleeAttack", resolve_attack_handler, priority=10)
EventBus.subscribe("MeleeAttack", apply_damage_handler, priority=100)
EventBus.subscribe("SpellCast", shield_spell_reaction_handler, priority=1) # Magic Missile uses AC, Shield works!
EventBus.subscribe("SpellCast", resolve_spell_cast_handler, priority=10)
EventBus.subscribe("Rest", handle_rest_event, priority=10)
EventBus.subscribe("AdvanceTime", handle_advance_time_event, priority=10)
EventBus.subscribe("DropConcentration", handle_drop_concentration_event, priority=10)
EventBus.subscribe("Movement", validate_movement_handler, priority=5)
EventBus.subscribe("Movement", resolve_movement_handler, priority=10)
EventBus.subscribe("Movement", consume_movement_handler, priority=100)
