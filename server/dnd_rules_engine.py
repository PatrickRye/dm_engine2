# AGENT_NOTE: When modifying this file, please use the test framework in `test_dnd_rules_engine.py` to validate your changes.
# To run the tests, execute `python test_dnd_rules_engine.py` in your terminal.

import asyncio
import sys
import uuid
import random
import re
import threading
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
        """No-op. Registration is now explicit — call `register_entity(self, vault_path)` after construction."""

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
    """
    A decorator that deals extra damage based on target properties.

    Uses model_post_init (Pydantic v2 hook) for the side-effect of subscribing
    to EventBus, rather than overriding __init__ which fights Pydantic's init
    machinery (model_copy, model_validate, etc.).
    """

    conditions: List[DamageCondition]
    # Class-level flag: shared across all instances. Reset in tests to force re-subscription.
    _subscribed: bool = False

    def model_post_init(self, __context) -> None:
        """Pydantic v2 hook — called after the model is fully initialized."""
        if not ConditionalDamageWeapon._subscribed:
            with _CDW_LOCK:
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
                sys.stderr.write(
                    f"[{threading.current_thread().name}] BONUS: {self.name} will deal extra "
                    f"{condition.extra_damage_dice} {condition.damage_type} damage to "
                    f"the {condition.required_tag}!\n"
                )


# Module-level lock to avoid Pydantic PrivateAttr deepcopy issues.
_CDW_LOCK = threading.RLock()


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
    post_spell_hostility: bool = False


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
    # REQ-EDG-005: Polymorph THP persistence — retained when Polymorph drops, until depleted or Rest
    polymorph_hp: int = 0
    polymorph_max_hp: int = 0
    death_saves_successes: int = 0
    death_saves_failures: int = 0
    exhaustion_level: int = 0
    spell_slots_expended_this_turn: int = 0
    summoned_by_uuid: Optional[uuid.UUID] = None
    summon_spell: str = ""
    mounted_on_uuid: Optional[uuid.UUID] = None
    # REQ-RST-002/003: Rest tracking
    rest_in_progress: bool = False  # True during an in-progress rest (set at rest start, cleared at rest completion)
    rest_type: str = ""  # "short" or "long" — set at rest start
    rest_start_day: int = 0  # Campaign day when rest began
    rest_start_hour: int = 0  # Hour (0–23) when rest began
    rest_interrupted: bool = False  # REQ-RST-002: True if DM interrupted the rest via interrupt_rest
    last_long_rest_day: int = 0  # REQ-RST-003: Campaign day of the most recently completed long rest
    last_long_rest_hour: int = 0  # REQ-RST-003: Hour (0–23) of the most recently completed long rest
    # REQ-SKL-007: Heroic Inspiration — grants one reroll of any d20 test
    has_heroic_inspiration: bool = False
    # REQ-SKL-004: Tool proficiencies (stored as tag strings, e.g. "Thieves' Tools", "Smith's Tools")
    tool_proficiencies: List[str] = Field(default_factory=list)
    # REQ-LOT-005: Attunement tracking — True when entity is currently attuned to an item
    # during a short rest (blocks HP recovery from hit dice per REQ-LOT-005)
    attuned_this_short_rest: bool = False
    # REQ-SKL-008: Hide DC — set to the stealth roll when a creature hides.
    # Acts as the DC for opposed Perception checks and the floor for passive Perception.
    hide_dc: int = 0
    # REQ-PET-006: Beast Master Companion — UUID of the ranger this companion belongs to
    companion_of_uuid: Optional[uuid.UUID] = None
    # REQ-PET-006: Beast Master Companion — True if currently commanded by ranger this turn
    companion_commanded_this_turn: bool = False
    # ---- Ammann Tactical Analysis Fields ----
    # Deduced from stat blocks during hydration; used by combat director and DM narration
    creature_role: List[str] = Field(
        default_factory=list,
        description="Ammann role: Artillerist, Brute, Controller, Elite, Lurker, Minion, Skirmisher, Solo, Support, Tank",
    )
    engagement_style: str = Field(
        default="",
        description="How the creature initiates combat: ambush, charge, seek_elevation, reveal_from_cover, circle_flank, intercept, or default",
    )
    combat_flow_priority: str = Field(
        default="",
        description="Priority order for action selection: 'saving_throw_abilities > recharge > synergies > attacks' or abbreviated notes",
    )
    recharge_priority: bool = Field(
        default=False,
        description="True if creature has recharge abilities; should use them immediately on Round 1",
    )
    action_synergies: List[str] = Field(
        default_factory=list,
        description="Action combinations that create combat synergies, e.g. 'grapple then bite', 'prone then multiattack'",
    )
    targeting_heuristic: str = Field(
        default="",
        description="Ammann targeting tier: reckless (Int/Wis <=7), reactive (8-11), strategic (12-13), master_tactician (14+)",
    )
    retreat_threshold_hp_pct: int = Field(
        default=0,
        description="HP percentage at which creature retreats (0 = never). 70=moderate_wound, 40=serious_wound",
    )
    evasion_vector: str = Field(
        default="",
        description="How creature escapes when retreating: dodge, dash, burrow, fly, swim, or none",
    )
    fanaticism_override: bool = Field(
        default=False,
        description="True if mindless/undead/zealot — creature fights to 0 HP regardless of retreat_threshold",
    )
    phase_change_trigger_hp_pct: int = Field(
        default=0,
        description="HP percentage at which creature changes tactics (0 = no phase change)",
    )
    phase_change_description: str = Field(
        default="",
        description="Description of the behavioral pivot at phase_change_trigger_hp_pct",
    )
    unexpected_tactic: str = Field(
        default="",
        description="One unexpected control tactic beyond damage (forced movement, terrain, psychology, etc.)",
    )
    metaphorical_damage: str = Field(
        default="",
        description="How to narratively describe damage: e.g. 'Fire = Wrath', 'Psychic = Trauma', 'Necrotic = Despair'",
    )
    expected_environment: List[str] = Field(
        default_factory=list,
        description="Inferred preferred environments: burrow, climb_stealth, swim, fire_immune, underground, etc.",
    )
    _load_snapshot: int = PrivateAttr(default=0)

    def _compute_snapshot(self) -> int:
        return hash(
            (
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
                self.mounted_on_uuid,
                tuple(sorted(c.name for c in self.active_conditions)),
                tuple(sorted(self.resources.items())),
            )
        )

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
    # REQ-SPL-002: Distinguishes Cast_Spell (actual spellcasting) from Magic_Action (other magical uses)
    event_tag: Optional[str] = None  # "Cast_Spell" or "Magic_Action"


class EventBus:
    """
    Manages the lifecycle and listeners for all game events.

    Thread-safety: uses a reentrant lock so that synchronous dispatch cycles
    (dispatch -> _notify -> handler -> dispatch) never deadlock. The lock also
    allows concurrent dispatches from different threads (via asyncio.to_thread)
    to safely coexist since they each acquire the lock independently.

    Per-vault routing: handlers are scoped to a vault_path. When dispatch() is called,
    only handlers registered to event.vault_path are notified. This prevents events
    in one campaign vault from triggering handlers registered by another vault.
    """

    # { vault_path: { event_type: [ (handler, priority) ] } }
    _listeners: ClassVar[Dict[str, Dict[str, List[Tuple[Callable, int]]]]] = {}
    _lock: ClassVar[threading.RLock] = threading.RLock()

    @classmethod
    def subscribe(
        cls, event_type: str, handler: Callable, priority: int = 10, vault_path: str = "default"
    ):
        """
        Register a handler for an event type within a specific vault.

        The same handler+event_type combination is deduplicated within each vault.
        A handler can be registered to multiple vaults independently if needed.
        """
        with cls._lock:
            if vault_path not in cls._listeners:
                cls._listeners[vault_path] = {}
            if event_type not in cls._listeners[vault_path]:
                cls._listeners[vault_path][event_type] = []
            # Deduplicate within this vault
            if any(h == handler for h, _ in cls._listeners[vault_path][event_type]):
                return
            cls._listeners[vault_path][event_type].append((handler, priority))
            cls._listeners[vault_path][event_type].sort(key=lambda x: x[1])

    @classmethod
    def dispatch(cls, event: GameEvent) -> GameEvent:
        """
        Dispatch an event to the handlers registered for event.vault_path.

        Vault isolation: only handlers registered for the event's vault_path are invoked.
        Fallback to "default" vault: if the specific vault has no handlers for this
        event_type, the "default" vault's handlers are used as a fallback. This lets
        tools dispatch to arbitrary vault paths while still reaching globally-registered
        handlers (e.g. from register_event_handlers called at module init time).
        """
        sys.stderr.write(
            f"[{threading.current_thread().name}] --- Event: {event.event_type} (vault={event.vault_path}) ---\n"
        )

        vault_path = event.vault_path or "default"
        with cls._lock:
            # Collect handlers from the specific vault, with fallback to "default"
            vault_listeners = cls._listeners.get(vault_path, {})
            default_listeners = cls._listeners.get("default", {})

            # Per-vault handlers take priority; "default" handlers fill gaps
            all_handlers = vault_listeners.get(event.event_type, [])
            if not all_handlers and vault_path != "default":
                all_handlers = default_listeners.get(event.event_type, [])

            event.status = EventStatus.PRE_EVENT
            cls._notify_handlers(all_handlers, event)
            if event.status == EventStatus.CANCELLED:
                sys.stderr.write(f"[{threading.current_thread().name}] Event was CANCELLED during Pre-Event.\n")
                return event

            event.status = EventStatus.EXECUTION
            cls._notify_handlers(all_handlers, event)

            event.status = EventStatus.POST_EVENT
            cls._notify_handlers(all_handlers, event)

            event.status = EventStatus.RESOLVED
            cls._notify_handlers(all_handlers, event)
            return event

    @classmethod
    async def adispatch(cls, event: "GameEvent") -> "GameEvent":
        """Async-safe dispatch: offloads the synchronous handler chain to a worker thread
        so it never blocks the asyncio event loop."""
        return await asyncio.to_thread(cls.dispatch, event)

    @classmethod
    def _notify_handlers(cls, handlers: List[Tuple[Callable, int]], event: GameEvent):
        """Notify registered handlers. Called while holding _lock."""
        for handler, _ in handlers:
            handler(event)

    @classmethod
    def _notify(cls, event: GameEvent):
        """
        Backward-compatible direct notify.

        Notifies handlers for event.event_type *at whatever phase the event's
        status is already set to*, bypassing the normal PRE→EXEC→POST→RESOLVED
        phase sequence of dispatch().

        Tests use this to jump straight to POST_EVENT (event.status=3) so that
        apply_damage_handler runs without re-running attack resolution.

        Uses per-vault + "default" fallback routing.
        """
        vault_path = event.vault_path or "default"
        with cls._lock:
            vault_listeners = cls._listeners.get(vault_path, {})
            default_listeners = cls._listeners.get("default", {})
            handlers = vault_listeners.get(event.event_type, [])
            if not handlers and vault_path != "default":
                handlers = default_listeners.get(event.event_type, [])
            cls._notify_handlers(handlers, event)

    @classmethod
    def clear_listeners(cls, vault_path: Optional[str] = None) -> None:
        """
        Thread-safe clear of listeners.

        - clear_listeners()           → clear all vaults (backward compat, used by tests)
        - clear_listeners("my_vault")  → clear only that vault's listeners
        """
        with cls._lock:
            if vault_path is None:
                cls._listeners.clear()
            elif vault_path in cls._listeners:
                del cls._listeners[vault_path]


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
