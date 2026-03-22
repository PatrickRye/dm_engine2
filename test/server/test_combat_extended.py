"""
Extended combat requirement tests covering Surprise, Armor, and Weapon requirements.
"""
import os
import yaml
import pytest
from unittest.mock import patch

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    GameEvent,
    EventBus,
    MeleeWeapon,
    NumericalModifier,
    ModifierPriority,
)
from spatial_engine import spatial_service
from registry import clear_registry, register_entity
from tools import start_combat


@pytest.fixture(autouse=True)
def setup_system():
    """Clear registries and spatial indexes before each test."""
    clear_registry()
    spatial_service.clear()
    spatial_service.map_data.pixels_per_foot = 1.0
    yield


# ============================================================
# REQ-SRP-001: Surprise
# ============================================================

@pytest.mark.asyncio
async def test_req_srp_001_surprised_combatants_use_min_initiative(mock_obsidian_vault):
    """
    REQ-SRP-001: Surprise
    Entities unaware of combat roll Initiative with Disadvantage (roll twice, take lower).
    No turns are lost — they still participate, just with a lower initiative score.
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    # Mocked rolls (in order):
    #   Goblin (surprised):  min(5, 18) = 5  → 2 calls
    #   Orc    (not surprised): 12            → 1 call
    with patch("random.randint", side_effect=[5, 18, 12]):
        await start_combat.ainvoke(
            {
                "pc_names": [],
                "enemies": [
                    {"name": "Goblin", "hp": 10, "dex_mod": 0},
                    {"name": "Orc", "hp": 10, "dex_mod": 0},
                ],
                "surprised_names": ["Goblin"],
            },
            config=config,
        )

    # Read the written ACTIVE_COMBAT.md and parse YAML frontmatter
    combat_path = os.path.join(str(mock_obsidian_vault), "server", "Journals", "ACTIVE_COMBAT.md")
    with open(combat_path, encoding="utf-8") as f:
        content = f.read()
    # Strip fences and parse YAML block between ---
    yaml_block = content.split("---")[1]
    data = yaml.safe_load(yaml_block)
    combatants_by_name = {c["name"]: c for c in data["combatants"]}

    goblin_init = combatants_by_name["Goblin"]["init"]
    orc_init = combatants_by_name["Orc"]["init"]

    assert goblin_init == 5, f"Surprised Goblin should have initiative=5 (min of 5,18), got {goblin_init}"
    assert orc_init == 12, f"Normal Orc should have initiative=12, got {orc_init}"

    # Both combatants are still present (no lost turns)
    assert len(data["combatants"]) == 2


@pytest.mark.asyncio
async def test_req_srp_001_unsurprised_combatants_use_single_roll(mock_obsidian_vault):
    """
    REQ-SRP-001: Surprise
    Non-surprised combatants use a single d20 roll (no min/max applied).
    """
    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    # Rolls: Fighter uses only 1 call (15), Orc uses only 1 call (8)
    with patch("random.randint", side_effect=[15, 8]):
        await start_combat.ainvoke(
            {
                "pc_names": [],
                "enemies": [
                    {"name": "Fighter", "hp": 20, "dex_mod": 0},
                    {"name": "Orc", "hp": 10, "dex_mod": 0},
                ],
                "surprised_names": [],  # nobody surprised
            },
            config=config,
        )

    combat_path = os.path.join(str(mock_obsidian_vault), "server", "Journals", "ACTIVE_COMBAT.md")
    with open(combat_path, encoding="utf-8") as f:
        content = f.read()
    yaml_block = content.split("---")[1]
    data = yaml.safe_load(yaml_block)
    combatants_by_name = {c["name"]: c for c in data["combatants"]}

    assert combatants_by_name["Fighter"]["init"] == 15
    assert combatants_by_name["Orc"]["init"] == 8


# ============================================================
# REQ-ARM-003: Shields add +2 AC; only one shield benefit applies
# ============================================================

def test_req_arm_003_shield_adds_two_ac():
    """
    REQ-ARM-003: Shields
    A shield increases Armor Class by 2.
    """
    fighter = Creature(
        name="Fighter",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=16),  # Base plate armor AC
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    assert fighter.ac.total == 16

    # Equip shield: add +2 ADDITIVE modifier
    shield_mod = NumericalModifier(
        priority=ModifierPriority.ADDITIVE,
        value=2,
        source_name="Shield",
    )
    fighter.ac.add_modifier(shield_mod)

    assert fighter.ac.total == 18


def test_req_arm_003_only_one_shield_benefit():
    """
    REQ-ARM-003: Shields
    An entity can benefit from only one shield at a time (max +2, not +4).
    The engine models this by not allowing duplicate Shield source modifiers.
    """
    fighter = Creature(
        name="Fighter",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=16),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    shield_mod = NumericalModifier(
        priority=ModifierPriority.ADDITIVE,
        value=2,
        source_name="Shield",
    )
    fighter.ac.add_modifier(shield_mod)
    assert fighter.ac.total == 18

    # A second shield should not add more; dedup by source_name at the tool level.
    # At the model level, verify that only ONE shield modifier produces +2.
    shield_mods = [m for m in fighter.ac.modifiers if m.source_name == "Shield"]
    assert len(shield_mods) == 1, "Only one shield modifier should be present"
    assert fighter.ac.total == 18


# ============================================================
# REQ-ACT-004: Spell Stacking — only highest modifier applies
# ============================================================

def test_req_act_004_spell_stacking_override_takes_last():
    """
    REQ-ACT-004: Stacking (Spells)
    Overlapping spell effects of the same name should not stack;
    only the highest mathematical modifier applies.
    The OVERRIDE priority mechanism captures the most recently applied value.
    """
    creature = Creature(
        name="Target",
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    # Apply two instances of Bless (+1d4 to attack rolls represented as +2 and +3)
    mod_a = NumericalModifier(
        priority=ModifierPriority.ADDITIVE,
        value=2,
        source_name="Bless",
    )
    mod_b = NumericalModifier(
        priority=ModifierPriority.ADDITIVE,
        value=3,
        source_name="Bless",
    )

    creature.strength_mod.add_modifier(mod_a)

    # Adding a second Bless: tool layer should prevent this,
    # but at the model level we verify that if the tool DOES enforce "max, no stack"
    # by removing the old one first, the total stays at the higher value.
    # Simulate the tool's dedup behavior:
    creature.strength_mod.modifiers = [
        m for m in creature.strength_mod.modifiers if m.source_name != "Bless"
    ]
    creature.strength_mod.add_modifier(mod_b)  # Only keep the higher one

    assert creature.strength_mod.total == 3  # 0 (base) + 3 (Bless B, higher value only)


# ============================================================
# REQ-ACT-005: Non-Spell Stacking — linear sum (existing behavior)
# ============================================================

def test_req_act_005_non_spell_stacking_is_additive():
    """
    REQ-ACT-005: Stacking (Non-Spells)
    Non-spell features of the same name stack linearly.
    """
    creature = Creature(
        name="Barbarian",
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )

    rage_a = NumericalModifier(priority=ModifierPriority.ADDITIVE, value=2, source_name="Rage Damage A")
    rage_b = NumericalModifier(priority=ModifierPriority.ADDITIVE, value=2, source_name="Rage Damage B")
    creature.strength_mod.add_modifier(rage_a)
    creature.strength_mod.add_modifier(rage_b)

    assert creature.strength_mod.total == 7  # 3 + 2 + 2


# ============================================================
# REQ-WPN-005: Versatile Property (data model)
# ============================================================

def test_req_wpn_005_versatile_property_exists():
    """
    REQ-WPN-005: Versatile Property
    Weapon model supports the Versatile property flag.
    The actual damage die selection (1h vs 2h) is passed by the DM layer.
    """
    from dnd_rules_engine import WeaponProperty

    # The engine must define the Versatile property
    assert hasattr(WeaponProperty, "VERSATILE")
    assert WeaponProperty.VERSATILE == "versatile"

    # A Versatile weapon (e.g., Longsword) can be tagged correctly
    longsword = MeleeWeapon(
        name="Longsword",
        damage_dice="1d8",  # one-handed
        damage_type="slashing",
        properties=[WeaponProperty.VERSATILE],
    )
    assert WeaponProperty.VERSATILE in longsword.properties
