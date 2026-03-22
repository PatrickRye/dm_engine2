"""
Armor requirement tests.
REQ-ARM-001: Heavy Armor Speed Penalty (Str requirement not met)
REQ-ARM-002: Heavy Armor Stealth Disadvantage
"""
import os
import pytest

from dnd_rules_engine import Creature, ModifiableValue
from item_system import ArmorItem, ItemCompendium
from registry import clear_registry, register_entity
from tools import equip_item, perform_ability_check_or_save
from vault_io import get_journals_dir


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    yield mock_obsidian_vault


# ============================================================
# REQ-ARM-001: Heavy Armor Speed Penalty
# ============================================================

@pytest.mark.asyncio
async def test_req_arm_001_speed_penalty_when_str_too_low(setup):
    """
    REQ-ARM-001: If heavy armor has a Str requirement and PC Str < requirement,
    speed is reduced by 10 when the armor is equipped.
    """
    vault_path = setup
    j_dir = get_journals_dir(vault_path)
    char_md = os.path.join(j_dir, "Warrior.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write("---\nstrength: 13\nequipment:\n  armor: None\nattuned_items: []\n---")

    c = Creature(
        name="Warrior",
        vault_path=vault_path,
        x=0, y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=1),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
    )
    register_entity(c)

    # Plate armor requires Str 15
    plate = ArmorItem(
        name="Plate Armor",
        armor_category="Heavy",
        base_ac=18,
        strength_requirement=15,
        stealth_disadvantage=True,
    )
    await ItemCompendium.save_item(vault_path, plate)

    config = {"configurable": {"thread_id": vault_path}}
    await equip_item.ainvoke(
        {"character_name": "Warrior", "item_name": "Plate Armor", "item_slot": "armor"},
        config=config,
    )

    assert c.speed == 20, f"Expected speed 20 after penalty, got {c.speed}"


@pytest.mark.asyncio
async def test_req_arm_001_no_penalty_when_str_meets_requirement(setup):
    """
    REQ-ARM-001: If PC Str >= armor Str requirement, speed is NOT reduced.
    """
    vault_path = setup
    j_dir = get_journals_dir(vault_path)
    char_md = os.path.join(j_dir, "StrongWarrior.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write("---\nstrength: 16\nequipment:\n  armor: None\nattuned_items: []\n---")

    c = Creature(
        name="StrongWarrior",
        vault_path=vault_path,
        x=0, y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
        speed=30,
    )
    register_entity(c)

    plate = ArmorItem(
        name="Plate Armor",
        armor_category="Heavy",
        base_ac=18,
        strength_requirement=15,
    )
    await ItemCompendium.save_item(vault_path, plate)

    config = {"configurable": {"thread_id": vault_path}}
    await equip_item.ainvoke(
        {"character_name": "StrongWarrior", "item_name": "Plate Armor", "item_slot": "armor"},
        config=config,
    )

    assert c.speed == 30, f"Expected speed unchanged at 30, got {c.speed}"


@pytest.mark.asyncio
async def test_req_arm_001_light_armor_no_str_requirement(setup):
    """
    REQ-ARM-001: Light armor with no Str requirement never reduces speed.
    """
    vault_path = setup
    j_dir = get_journals_dir(vault_path)
    char_md = os.path.join(j_dir, "Scout.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write("---\nstrength: 8\nequipment:\n  armor: None\nattuned_items: []\n---")

    c = Creature(
        name="Scout",
        vault_path=vault_path,
        x=0, y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=-1),
        dexterity_mod=ModifiableValue(base_value=3),
        speed=30,
    )
    register_entity(c)

    leather = ArmorItem(
        name="Leather Armor",
        armor_category="Light",
        base_ac=11,
        strength_requirement=0,
    )
    await ItemCompendium.save_item(vault_path, leather)

    config = {"configurable": {"thread_id": vault_path}}
    await equip_item.ainvoke(
        {"character_name": "Scout", "item_name": "Leather Armor", "item_slot": "armor"},
        config=config,
    )

    assert c.speed == 30, f"Expected speed unchanged at 30 for light armor, got {c.speed}"


# ============================================================
# REQ-ARM-002: Heavy Armor Stealth Disadvantage
# ============================================================

@pytest.mark.asyncio
async def test_req_arm_002_heavy_armor_stealth_disadvantage(setup):
    """
    REQ-ARM-002: Heavy armor with stealth_disadvantage=True imposes Disadvantage
    on Stealth checks, reflected in the perform_ability_check_or_save result.
    """
    vault_path = setup
    j_dir = get_journals_dir(vault_path)
    char_md = os.path.join(j_dir, "Paladin.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "dexterity: 10\n"
            "equipment:\n"
            "  armor: Plate Armor\n"
            "attuned_items: []\n"
            "---"
        )

    c = Creature(
        name="Paladin",
        vault_path=vault_path,
        x=0, y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(c)

    plate = ArmorItem(
        name="Plate Armor",
        armor_category="Heavy",
        base_ac=18,
        strength_requirement=15,
        stealth_disadvantage=True,
    )
    await ItemCompendium.save_item(vault_path, plate)

    config = {"configurable": {"thread_id": vault_path}}
    result = await perform_ability_check_or_save.ainvoke(
        {
            "character_name": "Paladin",
            "skill_or_stat_name": "stealth",
            "force_auto_roll": True,
        },
        config=config,
    )

    assert "Disadvantage" in result, f"Expected Disadvantage in result, got: {result}"
    assert "Plate Armor" in result, f"Expected armor name in alert, got: {result}"


@pytest.mark.asyncio
async def test_req_arm_002_no_disadvantage_without_flag(setup):
    """
    REQ-ARM-002: Armor without stealth_disadvantage=True does NOT impose disadvantage.
    """
    vault_path = setup
    j_dir = get_journals_dir(vault_path)
    char_md = os.path.join(j_dir, "Ranger.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "dexterity: 16\n"
            "equipment:\n"
            "  armor: Studded Leather\n"
            "attuned_items: []\n"
            "---"
        )

    c = Creature(
        name="Ranger",
        vault_path=vault_path,
        x=0, y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=1),
        dexterity_mod=ModifiableValue(base_value=3),
    )
    register_entity(c)

    studded = ArmorItem(
        name="Studded Leather",
        armor_category="Light",
        base_ac=12,
        stealth_disadvantage=False,
    )
    await ItemCompendium.save_item(vault_path, studded)

    config = {"configurable": {"thread_id": vault_path}}
    result = await perform_ability_check_or_save.ainvoke(
        {
            "character_name": "Ranger",
            "skill_or_stat_name": "stealth",
            "force_auto_roll": True,
        },
        config=config,
    )

    assert "REQ-ARM-002" not in result, f"Unexpected ARM-002 alert: {result}"
    # Should NOT show armor disadvantage message
    assert "stealth_disadvantage" not in result.lower()
