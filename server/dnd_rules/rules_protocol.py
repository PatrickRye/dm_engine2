"""
Structural protocols (interfaces) for the Rules Engine.
These allow tools.py and other modules to depend on clean interfaces
rather than concrete implementations.

Usage:
    from rules_protocol import RulesEngineProtocol, CreatureProtocol
    def my_function(rules: RulesEngineProtocol, creature: CreatureProtocol): ...

Concrete implementations:
    dnd_rules_engine.py  — EventBus, Creature, ModifiableValue, roll_dice,
                            parse_duration_to_seconds
"""

import uuid
from typing import Any, Dict, List, Callable, Optional, Protocol, runtime_checkable


# ─────────────────────────────────────────────────────────────────────────────
# EventBusProtocol
# Concrete implementation: dnd_rules_engine.EventBus
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class EventBusProtocol(Protocol):
    """Interface for the game event dispatcher."""

    def subscribe(self, event_type: str, handler: Callable, priority: int = 10) -> None: ...
    def dispatch(self, event: Any) -> Any: ...
    async def adispatch(self, event: Any) -> Any: ...
    def clear_listeners(self) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# ModifiableValueProtocol
# The contract for stats that change dynamically (AC, HP, Ability Scores).
# Concrete implementation: dnd_rules_engine.ModifiableValue
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class NumericalModifierProtocol(Protocol):
    """A single modifier applied to a ModifiableValue."""

    mod_uuid: uuid.UUID
    priority: int          # ModifierPriority value (1=OVERRIDE, 2=ADDITIVE, ...)
    value: int
    source_name: str
    duration_events: int
    duration_seconds: int
    applied_initiative: int
    source_uuid: Optional[uuid.UUID]


@runtime_checkable
class ModifiableValueProtocol(Protocol):
    """
    Contract for dynamically-computed stats (HP, AC, ability scores).
    Tools that read/write these values should use this protocol so
    alternate implementations (e.g., mock stats in tests) can be substituted.
    """

    base_value: int
    modifiers: List[NumericalModifierProtocol]

    @property
    def total(self) -> int: ...
    def add_modifier(self, mod: NumericalModifierProtocol) -> None: ...
    def remove_modifier(self, mod_uuid: uuid.UUID) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# CreatureProtocol
# The contract for all creature entities (PCs, NPCs, monsters, summons).
# Concrete implementation: dnd_rules_engine.Creature
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class ActiveConditionProtocol(Protocol):
    """A condition effect currently applied to a creature."""

    condition_id: uuid.UUID
    name: str
    duration_seconds: int
    source_name: str
    applied_initiative: int
    source_uuid: Optional[uuid.UUID]
    save_required: str
    save_dc: int
    save_timing: str
    start_of_turn_thp: int
    end_of_turn_damage_dice: str
    end_of_turn_damage_type: str
    speed_reduction: int
    post_spell_hostility: bool


@runtime_checkable
class CreatureProtocol(Protocol):
    """
    Contract for all creature entities.
    Tools that create, read, or mutate creatures should accept this protocol
    rather than the concrete Creature class, enabling alternate implementations
    (e.g., mock creatures in tests, remote creatures over a network layer).
    """

    # ── Identity & Position ─────────────────────────────────────────────────
    entity_uuid: uuid.UUID
    name: str
    vault_path: str
    x: float
    y: float
    z: float
    current_map: str
    size: float
    height: float

    # ── Combat Stats ────────────────────────────────────────────────────────
    max_hp: int
    hp: ModifiableValueProtocol
    temp_hp: int
    ac: ModifiableValueProtocol

    # ── Ability Scores ──────────────────────────────────────────────────────
    strength_mod: ModifiableValueProtocol
    dexterity_mod: ModifiableValueProtocol
    constitution_mod: ModifiableValueProtocol
    intelligence_mod: ModifiableValueProtocol
    wisdom_mod: ModifiableValueProtocol
    charisma_mod: ModifiableValueProtocol

    # ── Spellcasting ────────────────────────────────────────────────────────
    spell_save_dc: ModifiableValueProtocol
    spell_attack_bonus: ModifiableValueProtocol
    concentrating_on: str

    # ── Equipment & State ───────────────────────────────────────────────────
    equipped_weapon_uuid: Optional[uuid.UUID]
    classes: List[Any]          # List[ClassLevel]
    active_mechanics: List[str]
    active_conditions: List[ActiveConditionProtocol]
    resources: Dict[str, str]
    features: List[Any]          # List[Feature]
    tags: List[str]
    alignment: str
    vulnerabilities: List[str]
    resistances: List[str]
    immunities: List[str]
    reaction_used: bool
    legendary_actions_max: int
    legendary_actions_current: int
    speed: int
    movement_remaining: int
    wild_shape_hp: int
    wild_shape_max_hp: int
    death_saves_successes: int
    death_saves_failures: int
    exhaustion_level: int
    spell_slots_expended_this_turn: int
    summoned_by_uuid: Optional[uuid.UUID]
    summon_spell: str
    mounted_on_uuid: Optional[uuid.UUID]

    # ── Computed / Lifecycle ─────────────────────────────────────────────────
    @property
    def character_level(self) -> int: ...
    @property
    def is_dirty(self) -> bool: ...
    def store_snapshot(self) -> None: ...
    def apply_features(self, class_def: Any, level: int) -> None: ...
    def apply_subclass_features(self, subclass_def: Any, level: int) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# RulesEngineProtocol
# The stateless contract for the rules engine — dice rolling, duration parsing,
# and event bus operations.
# Concrete implementation: dnd_rules_engine module-level functions + EventBus
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class RulesEngineProtocol(Protocol):
    """
    Contract for the deterministic rules engine.
    Exposes event-dispatch, dice rolling, and duration parsing.
    Tools that perform mechanical calculations should accept this protocol
    so implementations can vary (e.g., a mock engine for testing).
    """

    # ── Event Bus ───────────────────────────────────────────────────────────
    def subscribe(self, event_type: str, handler: Callable, priority: int = 10) -> None: ...
    def dispatch(self, event: Any) -> Any: ...
    async def adispatch(self, event: Any) -> Any: ...
    def clear_listeners(self) -> None: ...

    # ── Dice & Duration Utilities ───────────────────────────────────────────
    def roll_dice(self, notation: str) -> int: ...
    def parse_duration_to_seconds(self, duration_str: str) -> int: ...
