import uuid
import random
import re
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
    vulnerabilities: List[str] = Field(default_factory=list)
    resistances: List[str] = Field(default_factory=list)
    immunities: List[str] = Field(default_factory=list)

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
        cls._notify(event)
        return event

    @classmethod
    def _notify(cls, event: GameEvent):
        for handler, _ in cls._listeners.get(event.event_type, []):
            handler(event)

# ==========================================
# 5. CORE MECHANICS & HANDLERS
# ==========================================

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

def resolve_attack_handler(event: GameEvent):
    """Calculates if an attack hits and its base damage. Listens to EXECUTION phase."""
    if event.status != EventStatus.EXECUTION: return

    attacker: Creature = BaseGameEntity.get(event.source_uuid)
    target: Creature = BaseGameEntity.get(event.target_uuid)
    weapon: Weapon = BaseGameEntity.get(attacker.equipped_weapon_uuid)

    attack_mod = weapon.get_attack_modifier(attacker)
    attack_bonus = attack_mod.total + weapon.magic_bonus
    
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
    target_ac = target.ac.total

    is_critical_hit = d20_roll == 20
    is_hit = is_critical_hit or total_attack >= target_ac

    print(f"[Engine] {attacker.name} rolls a {d20_roll} ({roll1}, {roll2} if adv/disadv) + {attack_bonus} = {total_attack} vs AC {target_ac}")

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

def shield_spell_reaction_handler(event: GameEvent):
    """Intercepts an attack BEFORE it resolves and magically raises AC."""
    if event.status != EventStatus.PRE_EVENT: return
    
    target: Creature = BaseGameEntity.get(event.target_uuid)
    if "can_cast_shield" in target.tags:
        print(f"[Engine] REACTION TRIGGERED: {target.name} casts Shield!")
        shield_mod = NumericalModifier(
            priority=ModifierPriority.ADDITIVE, 
            value=5, 
            source_name="Shield Spell"
        )
        target.ac.add_modifier(shield_mod)
        print(f"[Engine] {target.name}'s AC is temporarily raised to {target.ac.total}")

        # Add the modifier's UUID to the event payload to be removed later
        if "temp_mods" not in event.payload:
            event.payload["temp_mods"] = []
        event.payload["temp_mods"].append(shield_mod.mod_uuid)

def cleanup_temp_mods_handler(event: GameEvent):
    """Removes temporary modifiers after an event is resolved."""
    if event.status != EventStatus.RESOLVED: return

    if "temp_mods" in event.payload:
        for mod_uuid in event.payload["temp_mods"]:
            # This is a simplification. In a real game, you'd need to know
            # which creature and which stat the modifier was applied to.
            # For this example, we'll assume it's the target's AC.
            target: Creature = BaseGameEntity.get(event.target_uuid)
            if target:
                target.ac.remove_modifier(mod_uuid)
                print(f"[Engine] The Shield spell fades. {target.name}'s AC returns to {target.ac.total}")


# Register handlers to the Event Bus
# Lower priority numbers run first.
EventBus.subscribe("MeleeAttack", shield_spell_reaction_handler, priority=1)
EventBus.subscribe("MeleeAttack", resolve_attack_handler, priority=10)
EventBus.subscribe("MeleeAttack", apply_damage_handler, priority=100)
EventBus.subscribe("MeleeAttack", cleanup_temp_mods_handler, priority=200)

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

    wizard = Creature(
        name="Lyra the Wizard",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=2),
        tags=["can_cast_shield"]
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
    attack_event_1 = GameEvent(
        event_type="MeleeAttack",
        source_uuid=fighter.entity_uuid,
        target_uuid=zombie.entity_uuid
    )
    
    final_event_1 = EventBus.dispatch(attack_event_1)
    
    print("\n--- Paladin attacks Wizard ---")
    attack_event_2 = GameEvent(
        event_type="MeleeAttack",
        source_uuid=fighter.entity_uuid,
        target_uuid=wizard.entity_uuid
    )

    final_event_2 = EventBus.dispatch(attack_event_2)

    print("\n--- Final Output for LangGraph ---")
    print(final_event_1.model_dump_json(indent=2))
    print(final_event_2.model_dump_json(indent=2))