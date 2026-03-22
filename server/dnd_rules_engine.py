# AGENT_NOTE: When modifying this file, please use the test framework in `test_dnd_rules_engine.py` to validate your changes.
# To run the tests, execute `python test_dnd_rules_engine.py` in your terminal.

import asyncio
import uuid
import random
import re
from enum import Enum, IntEnum
from typing import ClassVar, Dict, Any, List, Callable, Optional, Tuple
from pydantic import BaseModel, Field, PrivateAttr
from state import ClassLevel, Feature, ClassDefinition, SubclassDefinition
from registry import register_entity, get_entity, remove_entity

# ==========================================
# 1. THE REGISTRY & MEMORY DECOUPLING
# ==========================================


class BaseGameEntity(BaseModel):
    """
    The foundational object. Everything in the game (players, weapons, spells)
    inherits from this to get a UUID and be added to the global registry.
    """

    entity_uuid: uuid.UUID = Field(default_factory=uuid.uuid4)
    vault_path: str = "default"
    name: str
    icon_url: str = ""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    current_map: str = ""  # Map name (e.g. "dungeon_floor1.jpg") entity is on; "" = default/active map
    size: float = 5.0  # represents a standard 5x5 foot D&D grid space
    height: float = 5.0  # Defaults to size unless specifically set (e.g. Medium creature = 5x5x5)

    def model_post_init(self, __context: Any) -> None:
        """Automatically registers the object upon creation."""
        register_entity(self, getattr(self, "vault_path", "default"))

    @classmethod
    def get(cls, uid: uuid.UUID) -> Optional["BaseGameEntity"]:
        return get_entity(uid)

    @classmethod
    def remove(cls, uid: uuid.UUID) -> None:
        remove_entity(uid)


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
    UNDERWATER_SAFE = "underwater_safe"  # REQ-ENV-004/005: weapon usable without penalty underwater


class Weapon(BaseGameEntity):
    damage_dice: str
    damage_type: str
    properties: List[WeaponProperty] = Field(default_factory=list)
    cost: str = "0 gp"
    weight: float = 0.0
    magic_bonus: int = 0
    mastery_name: str = ""
    on_hit_mechanics: Optional[Dict[str, Any]] = None
    on_miss_mechanics: Optional[Dict[str, Any]] = None

    def get_attack_modifier(self, wielder: "Creature") -> ModifiableValue:
        if WeaponProperty.FINESSE in self.properties:
            if wielder.dexterity_mod.total > wielder.strength_mod.total:
                return wielder.dexterity_mod
        return wielder.strength_mod

    def get_damage_modifier(self, wielder: "Creature") -> ModifiableValue:
        return self.get_attack_modifier(wielder)


class MeleeWeapon(Weapon):
    pass


class RangedWeapon(Weapon):
    normal_range: int
    long_range: int

    def get_attack_modifier(self, wielder: "Creature") -> ModifiableValue:
        return wielder.dexterity_mod

    def get_damage_modifier(self, wielder: "Creature") -> ModifiableValue:
        return wielder.dexterity_mod


# Decorator Pattern for Magical Weapons
class MagicWeaponDecorator(Weapon):
    _wrapped_weapon: Weapon = PrivateAttr()

    def __init__(self, *, weapon: Weapon, **data):
        wrapped_data = weapon.model_dump(exclude={"entity_uuid", "name"})
        combined_data = {**wrapped_data, **data}
        super().__init__(**combined_data)
        object.__setattr__(self, "_wrapped_weapon", weapon)

    def __getattr__(self, name: str) -> Any:
        wrapped_weapon = super().__getattribute__("_wrapped_weapon")
        return getattr(wrapped_weapon, name)


class BonusWeapon(MagicWeaponDecorator):
    magic_bonus: int


class ViciousWeapon(MagicWeaponDecorator):
    pass


class CursedWeapon(MagicWeaponDecorator):
    curse_description: str


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

    def handle_attack(self, event: "GameEvent"):
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
                print(
                    f"[Engine] BONUS: {self.name} will deal extra {condition.extra_damage_dice} "
                    f"{condition.damage_type} damage to the {condition.required_tag}!"
                )


class ActiveCondition(BaseModel):
    condition_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    duration_seconds: int = -1
    source_name: str = "Unknown"
    applied_initiative: int = 0
    source_uuid: Optional[uuid.UUID] = None
    save_required: str = ""
    save_dc: int = 0
    save_timing: str = "end"
    start_of_turn_thp: int = 0
    end_of_turn_damage_dice: str = ""
    end_of_turn_damage_type: str = ""
    speed_reduction: int = 0  # REQ-MST-006: Slow mastery reduces target speed by this amount


class Creature(BaseGameEntity):
    max_hp: int = 10
    hp: ModifiableValue
    temp_hp: int = 0
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
    wild_shape_hp: int = 0
    wild_shape_max_hp: int = 0
    death_saves_successes: int = 0
    death_saves_failures: int = 0
    exhaustion_level: int = 0
    spell_slots_expended_this_turn: int = 0
    summoned_by_uuid: Optional[uuid.UUID] = None
    summon_spell: str = ""
    _load_snapshot: int = PrivateAttr(default=0)

    def _compute_snapshot(self) -> int:
        return hash((
            self.hp.base_value,
            self.temp_hp,
            self.x,
            self.y,
            self.z,
            self.concentrating_on,
            self.reaction_used,
            self.movement_remaining,
            self.death_saves_successes,
            self.death_saves_failures,
            self.exhaustion_level,
            tuple(sorted(c.name for c in self.active_conditions)),
            tuple(sorted(self.resources.items())),
        ))

    def store_snapshot(self):
        """Record current state as the clean baseline. Call after loading from disk."""
        object.__setattr__(self, "_load_snapshot", self._compute_snapshot())

    @property
    def is_dirty(self) -> bool:
        """True if any tracked field changed since store_snapshot() was last called."""
        return self._compute_snapshot() != self._load_snapshot

    @property
    def character_level(self) -> int:
        return sum(c.level for c in self.classes)

    def apply_features(self, class_def: "ClassDefinition", level: int):
        for feature in class_def.features:
            if feature.level == level and feature not in self.features:
                self.features.append(feature)

    def apply_subclass_features(self, subclass_def: "SubclassDefinition", level: int):
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
    vault_path: str = "default"
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
    async def adispatch(cls, event: "GameEvent") -> "GameEvent":
        """Async-safe dispatch: offloads the synchronous handler chain to a worker thread
        so it never blocks the asyncio event loop."""
        return await asyncio.to_thread(cls.dispatch, event)

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
    if duration_str in ["-1", "instantaneous", "permanent"]:
        return -1
    match = re.search(r"(\d+)\s*(round|minute|hour|day)", duration_str)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        if unit == "round":
            return val * 6
        elif unit == "minute":
            return val * 60
        elif unit == "hour":
            return val * 3600
        elif unit == "day":
            return val * 86400
    return -1


DICE_REGEX = re.compile(r"(\d+)d(\d+)(?:\s*([+-])\s*(\d+))?")


def roll_dice(notation: str) -> int:
    """Parses and rolls generic D&D dice formulas (e.g., '1d8', '2d6+3')."""
    notation = str(notation).strip().lower()
    if not notation:
        return 0

    # Support flat integers being passed as dice natively
    if re.match(r"^[+-]?\d+$", notation):
        return int(notation)

    match = DICE_REGEX.match(notation)
    if not match:
        return 0

    count, faces = int(match.group(1)), int(match.group(2))
    modifier_op = match.group(3)
    modifier_val = int(match.group(4)) if match.group(4) else 0

    total = sum(random.randint(1, faces) for _ in range(count))

    if modifier_op == "+":
        total += modifier_val
    elif modifier_op == "-":
        total -= modifier_val

    return max(0, total)  # Prevents negative damage
