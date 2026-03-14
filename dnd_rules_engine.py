import uuid
import random
from enum import IntEnum
from typing import ClassVar, Dict, Any, List, Callable, Optional
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
    
    # Global Registry mapping UUIDs to memory addresses
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
    OVERRIDE = 1       # e.g., Amulet of Health (Con = 19)
    ADDITIVE = 2       # e.g., +2 Shield, +5 from spell
    MULTIPLIER = 3     # e.g., Double damage vulnerability
    CONDITIONAL = 4    # Evaluated at runtime against a target
    LIMIT = 5          # Caps or floors (e.g., minimum roll of 10)

class NumericalModifier(BaseModel):
    mod_uuid: uuid.UUID = Field(default_factory=uuid.uuid4)
    priority: ModifierPriority
    value: int
    source_name: str
    duration_events: int = -1 # -1 means permanent until removed

class ModifiableValue(BaseModel):
    """Wraps core stats (AC, HP, Ability Scores) to allow dynamic recalculation."""
    base_value: int
    modifiers: List[NumericalModifier] = Field(default_factory=list)

    @property
    def total(self) -> int:
        current_val = self.base_value
        sorted_mods = sorted(self.modifiers, key=lambda m: m.priority)
        
        # 1. Overrides
        overrides = [m for m in sorted_mods if m.priority == ModifierPriority.OVERRIDE]
        if overrides:
            current_val = overrides[-1].value
            
        # 2. Additive
        for m in [m for m in sorted_mods if m.priority == ModifierPriority.ADDITIVE]:
            current_val += m.value
            
        # 3. Multiplier
        for m in [m for m in sorted_mods if m.priority == ModifierPriority.MULTIPLIER]:
            current_val = int(current_val * m.value)
            
        # (Conditions and Limits would be evaluated here by passing contextual kwargs)
        
        return current_val

    def add_modifier(self, mod: NumericalModifier):
        self.modifiers.append(mod)

    def remove_modifier(self, mod_uuid: uuid.UUID):
        self.modifiers = [m for m in self.modifiers if m.mod_uuid != mod_uuid]


# ==========================================
# 3. ENTITIES & EQUIPMENT
# ==========================================

class Weapon(BaseGameEntity):
    damage_dice: str  # e.g., "1d8"
    damage_type: str  # e.g., "slashing"
    magic_bonus: int = 0

class Creature(BaseGameEntity):
    hp: ModifiableValue
    ac: ModifiableValue
    strength_mod: ModifiableValue
    equipped_weapon_uuid: Optional[uuid.UUID] = None
    classes: List[ClassLevel] = Field(default_factory=list)
    features: List[Feature] = Field(default_factory=list) # Applied features

    @property
    def character_level(self) -> int:
        return sum(c.level for c in self.classes)

    def apply_features(self, class_def: ClassDefinition, class_level: int):
        # Logic to add features from the class_def up to the class_level
        for feature in class_def.features:
            if feature.level <= class_level and feature not in self.features:
                self.features.append(feature)

    def apply_subclass_features(self, subclass_def: SubclassDefinition, class_level: int):
        # Logic to add features from the subclass_def up to the class_level
        for feature in subclass_def.features:
            if feature.level <= class_level and feature not in self.features:
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
    _listeners: ClassVar[Dict[str, List[Callable]]] = {}

    @classmethod
    def subscribe(cls, event_type: str, handler: Callable):
        if event_type not in cls._listeners:
            cls._listeners[event_type] = []
        # Sort handlers by priority if needed, but append is fine for now
        cls._listeners[event_type].append(handler)

    @classmethod
    def dispatch(cls, event: GameEvent) -> GameEvent:
        print(f"\n--- Dispatched Event: {event.event_type} ---")
        
        # Phase 1: Pre-Event (Reactions like Shield or Counterspell)
        event.status = EventStatus.PRE_EVENT
        cls._notify(event)
        if event.status == EventStatus.CANCELLED:
            print("Event was CANCELLED during Pre-Event.")
            return event
            
        # Phase 2: Execution (Dice rolling and math)
        event.status = EventStatus.EXECUTION
        cls._notify(event)
        
        # Phase 3: Post-Event (Damage reflection, triggering traps)
        event.status = EventStatus.POST_EVENT
        cls._notify(event)
        
        event.status = EventStatus.RESOLVED
        return event

    @classmethod
    def _notify(cls, event: GameEvent):
        for handler in cls._listeners.get(event.event_type, []):
            handler(event)


# ==========================================
# 5. CORE MECHANICS & HANDLERS
# ==========================================

def roll_dice(notation: str) -> int:
    """Simple parser for XdY (e.g., 1d8, 2d6). Replace with `dyce` library for production."""
    count, faces = map(int, notation.lower().split('d'))
    return sum(random.randint(1, faces) for _ in range(count))

def resolve_attack_handler(event: GameEvent):
    """The deterministic core of an attack roll. Listens to EXECUTION phase."""
    if event.status != EventStatus.EXECUTION: return

    attacker: Creature = BaseGameEntity.get(event.source_uuid)
    target: Creature = BaseGameEntity.get(event.target_uuid)
    weapon: Weapon = BaseGameEntity.get(attacker.equipped_weapon_uuid)

    # Calculate modifiers
    attack_bonus = attacker.strength_mod.total + weapon.magic_bonus
    d20_roll = random.randint(1, 20)
    total_attack = d20_roll + attack_bonus

    target_ac = target.ac.total
    print(f"[Engine] {attacker.name} rolls a {d20_roll} + {attack_bonus} = {total_attack} vs AC {target_ac}")

    if total_attack >= target_ac:
        damage = roll_dice(weapon.damage_dice) + attacker.strength_mod.total + weapon.magic_bonus
        target.hp.base_value -= damage
        print(f"[Engine] HIT! {target.name} takes {damage} {weapon.damage_type} damage. HP remaining: {target.hp.base_value}")
        event.payload["hit"] = True
        event.payload["damage"] = damage
    else:
        print(f"[Engine] MISS! The attack glances off {target.name}'s armor.")
        event.payload["hit"] = False


def shield_spell_reaction_handler(event: GameEvent):
    """
    Demonstrates the 'Time Travel' Pre-Event problem resolution. 
    Intercepts an attack BEFORE it resolves and magically raises AC.
    """
    if event.status != EventStatus.PRE_EVENT: return
    
    target: Creature = BaseGameEntity.get(event.target_uuid)
    # Mock conditional logic: If target is Lyra, she casts Shield
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
EventBus.subscribe("MeleeAttack", resolve_attack_handler)
EventBus.subscribe("MeleeAttack", shield_spell_reaction_handler)


# ==========================================
# 6. DEMONSTRATION SCRIPT
# ==========================================
if __name__ == "__main__":
    print("Initializing Deterministic Engine...")

    # 1. Create Data Objects (In production, this is pulled from Obsidian via Pydantic)
    longsword = Weapon(name="Steel Longsword", damage_dice="1d8", damage_type="slashing")
    
    fighter = Creature(
        name="Kaelen the Fighter",
        hp=ModifiableValue(base_value=35),
        ac=ModifiableValue(base_value=16),
        strength_mod=ModifiableValue(base_value=3),
        equipped_weapon_uuid=longsword.entity_uuid,
        classes=[ClassLevel(class_name="Fighter", level=5)]
    )

    wizard = Creature(
        name="Lyra the Wizard",
        hp=ModifiableValue(base_value=14),
        ac=ModifiableValue(base_value=12),  # Low base AC
        strength_mod=ModifiableValue(base_value=-1),
        classes=[ClassLevel(class_name="Wizard", level=3)]
    )

    # 2. Trigger an Event (Simulating an LLM tool call)
    print("\n--- Combat Round 1 ---")
    attack_event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=fighter.entity_uuid,
        target_uuid=wizard.entity_uuid
    )
    
    # The EventBus handles the flow: Shield Spell interrupts -> Math is calculated -> State Updates
    final_event = EventBus.dispatch(attack_event)
    
    print("\n--- Final Output for LangGraph ---")
    print(final_event.model_dump_json(indent=2))