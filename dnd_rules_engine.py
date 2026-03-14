import uuid
import random
from enum import IntEnum
from typing import ClassVar, Dict, Any, List, Callable, Optional, Tuple
from pydantic import BaseModel, Field, PrivateAttr
from state import ClassLevel, Feature, ClassDefinition, SubclassDefinition

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

    def __init__(self, *, weapon: Weapon, **data):
        super().__init__(weapon=weapon, **data)
        EventBus.subscribe("MeleeAttack", self.handle_attack, priority=50)

    def handle_attack(self, event: 'GameEvent'):
        if event.status != EventStatus.POST_EVENT or not event.payload.get("hit"):
            return

        attacker = BaseGameEntity.get(event.source_uuid)
        if attacker.equipped_weapon_uuid != self.entity_uuid:
            return

        target = BaseGameEntity.get(event.target_uuid)
        extra_damage = 0
        for condition in self.conditions:
            if condition.required_tag in target.tags or condition.required_tag == target.alignment:
                damage = roll_dice(condition.extra_damage_dice)
                print(f"[Engine] BONUS: {self.name} glows, dealing an extra {damage} {condition.damage_type} damage to the {condition.required_tag}!")
                extra_damage += damage
        
        if extra_damage > 0:
            event.payload["damage"] = event.payload.get("damage", 0) + extra_damage

class Creature(BaseGameEntity):
    hp: ModifiableValue
    ac: ModifiableValue
    strength_mod: ModifiableValue
    dexterity_mod: ModifiableValue
    equipped_weapon_uuid: Optional[uuid.UUID] = None
    classes: List[ClassLevel] = Field(default_factory=list)
    features: List[Feature] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    alignment: str = "neutral"

    @property
    def character_level(self) -> int:
        return sum(c.level for c in self.classes)

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
        return event

    @classmethod
    def _notify(cls, event: GameEvent):
        for handler, _ in cls._listeners.get(event.event_type, []):
            handler(event)

# ==========================================
# 5. CORE MECHANICS & HANDLERS
# ==========================================

def roll_dice(notation: str) -> int:
    """Simple parser for XdY (e.g., 1d8, 2d6)."""
    count, faces = map(int, notation.lower().split('d'))
    return sum(random.randint(1, faces) for _ in range(count))

def resolve_attack_handler(event: GameEvent):
    """Calculates if an attack hits and its base damage. Listens to EXECUTION phase."""
    if event.status != EventStatus.EXECUTION: return

    attacker: Creature = BaseGameEntity.get(event.source_uuid)
    target: Creature = BaseGameEntity.get(event.target_uuid)
    weapon: Weapon = BaseGameEntity.get(attacker.equipped_weapon_uuid)

    attack_mod = weapon.get_attack_modifier(attacker)
    attack_bonus = attack_mod.total + weapon.magic_bonus
    d20_roll = random.randint(1, 20)
    total_attack = d20_roll + attack_bonus
    target_ac = target.ac.total

    print(f"[Engine] {attacker.name} rolls a {d20_roll} + {attack_bonus} = {total_attack} vs AC {target_ac}")

    if total_attack >= target_ac:
        print(f"[Engine] HIT!")
        damage_mod = weapon.get_damage_modifier(attacker)
        base_damage = roll_dice(weapon.damage_dice) + damage_mod.total + weapon.magic_bonus
        
        event.payload["hit"] = True
        event.payload["damage"] = base_damage
        event.payload["damage_type"] = weapon.damage_type
    else:
        print(f"[Engine] MISS! The attack glances off {target.name}'s armor.")
        event.payload["hit"] = False

def apply_damage_handler(event: GameEvent):
    """Applies final damage to a target. Listens to POST_EVENT phase."""
    if event.status != EventStatus.POST_EVENT or not event.payload.get("hit"):
        return
        
    target: Creature = BaseGameEntity.get(event.target_uuid)
    damage = event.payload.get("damage", 0)
    damage_type = event.payload.get("damage_type", "unknown")

    if damage > 0:
        target.hp.base_value -= damage
        print(f"[Engine] {target.name} takes {damage} {damage_type} damage. HP remaining: {target.hp.base_value}")

def shield_spell_reaction_handler(event: GameEvent):
    """Intercepts an attack BEFORE it resolves and magically raises AC."""
    if event.status != EventStatus.PRE_EVENT: return
    
    target: Creature = BaseGameEntity.get(event.target_uuid)
    if target.name == "Lyra the Wizard":
        print(f"[Engine] REACTION TRIGGERED: {target.name} casts Shield!")
        shield_mod = NumericalModifier(
            priority=ModifierPriority.ADDITIVE, 
            value=5, 
            source_name="Shield Spell"
        )
        target.ac.add_modifier(shield_mod)
        print(f"[Engine] {target.name}'s AC is temporarily raised to {target.ac.total}")

# Register handlers to the Event Bus
# Higher priority numbers run first.
EventBus.subscribe("MeleeAttack", resolve_attack_handler, priority=10)
EventBus.subscribe("MeleeAttack", shield_spell_reaction_handler, priority=1)
EventBus.subscribe("MeleeAttack", apply_damage_handler, priority=100)

# ==========================================
# 6. DEMONSTRATION SCRIPT
# ==========================================
if __name__ == "__main__":
    print("Initializing Deterministic Engine...")

    # 1. Create Weapons
    longsword = MeleeWeapon(name="Steel Longsword", damage_dice="1d8", damage_type="slashing")
    
    sun_blade = ConditionalDamageWeapon(
        weapon=longsword,
        name="Sun Blade",
        magic_bonus=2,
        conditions=[
            DamageCondition(required_tag="undead", extra_damage_dice="1d8", damage_type="radiant")
        ]
    )

    # 2. Create Creatures
    fighter = Creature(
        name="Kaelen the Paladin",
        hp=ModifiableValue(base_value=35),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=1),
        equipped_weapon_uuid=sun_blade.entity_uuid
    )

    zombie = Creature(
        name="Mindless Zombie",
        hp=ModifiableValue(base_value=22),
        ac=ModifiableValue(base_value=8),
        strength_mod=ModifiableValue(base_value=2),
        dexterity_mod=ModifiableValue(base_value=-2),
        tags=["undead"]
    )

    # 3. Trigger an Event
    print("\n--- Paladin attacks Zombie with Sun Blade ---")
    attack_event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=fighter.entity_uuid,
        target_uuid=zombie.entity_uuid
    )
    
    final_event = EventBus.dispatch(attack_event)
    
    print("\n--- Final Output for LangGraph ---")
    print(final_event.model_dump_json(indent=2))