import os
import pytest
from unittest.mock import patch
from item_system import WondrousItem, WeaponItem, ArmorItem, ItemCompendium
from spell_system import StatModifier, SpellMechanics
from dnd_rules_engine import Creature, ModifiableValue, ModifierPriority, MeleeWeapon
from registry import register_entity, clear_registry, get_entity
from tools import equip_item, attune_item, use_ability_or_spell, execute_melee_attack
from vault_io import get_journals_dir


@pytest.fixture(autouse=True)
def setup_engine_and_vault(mock_obsidian_vault):
    """Clears registries and provides an isolated temporary vault for testing."""
    clear_registry()
    vault_path = mock_obsidian_vault
    yield vault_path
    clear_registry()


@pytest.mark.asyncio
async def test_unequip_item_removes_override(setup_engine_and_vault):
    """
    Test that equipping an item with a high StatModifier applies an OVERRIDE priority,
    and that overwriting the slot with 'None' cleanly removes the override from the Creature.
    """
    vault_path = setup_engine_and_vault
    j_dir = get_journals_dir(vault_path)

    # 1. Setup Character File and Engine Entity
    char_md = os.path.join(j_dir, "TestChar.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write("---\nequipment:\n  head: None\nattuned_items: []\n---")

    c = Creature(
        name="TestChar",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )
    register_entity(c)

    # 2. Save Item to Compendium
    item = WondrousItem(name="Headband of Intellect", modifiers=[StatModifier(stat="strength_mod", value=19)])
    await ItemCompendium.save_item(vault_path, item)

    config = {"configurable": {"thread_id": vault_path}}

    # 3. Equip the Item
    await equip_item.ainvoke(
        {"character_name": "TestChar", "item_name": "Headband of Intellect", "item_slot": "head"}, config=config
    )

    assert c.strength_mod.total == 19
    assert any(m.priority == ModifierPriority.OVERRIDE for m in c.strength_mod.modifiers)

    # 4. Unequip the Item (by overwriting with 'None')
    await equip_item.ainvoke({"character_name": "TestChar", "item_name": "None", "item_slot": "head"}, config=config)

    assert c.strength_mod.total == 0
    assert not any(m.source_name == "Headband of Intellect" for m in c.strength_mod.modifiers)


@pytest.mark.asyncio
async def test_unattune_item_removes_override(setup_engine_and_vault):
    """
    Test that a magic item requiring attunement won't apply modifiers until attuned,
    and that unattuning correctly removes the OVERRIDE priority modifiers.
    """
    vault_path = setup_engine_and_vault
    j_dir = get_journals_dir(vault_path)

    char_md = os.path.join(j_dir, "TestChar.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write("---\nequipment:\n  ring1: None\nattuned_items: []\n---")

    c = Creature(
        name="TestChar",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )
    register_entity(c)

    item = WondrousItem(name="Ring of Override", requires_attunement=True, modifiers=[StatModifier(stat="ac", value=20)])
    await ItemCompendium.save_item(vault_path, item)
    config = {"configurable": {"thread_id": vault_path}}

    await equip_item.ainvoke(
        {"character_name": "TestChar", "item_name": "Ring of Override", "item_slot": "ring1"}, config=config
    )
    assert c.ac.total == 10  # Verify not applied because it requires attunement

    await attune_item.ainvoke(
        {"character_name": "TestChar", "item_name": "Ring of Override", "action": "attune"}, config=config
    )
    assert c.ac.total == 20  # Attuned, OVERRIDE applied
    assert any(m.priority == ModifierPriority.OVERRIDE for m in c.ac.modifiers)

    await attune_item.ainvoke(
        {"character_name": "TestChar", "item_name": "Ring of Override", "action": "unattune"}, config=config
    )
    assert c.ac.total == 10  # Unattuned, OVERRIDE cleanly removed


@pytest.mark.asyncio
async def test_wondrous_item_casts_spell(setup_engine_and_vault):
    """
    Test that a WondrousItem with an active_mechanics block can be correctly
    loaded and cast using the use_ability_or_spell tool natively.
    """
    vault_path = setup_engine_and_vault

    caster = Creature(
        name="Mage",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        x=10,
        y=0,
        hp=ModifiableValue(base_value=30),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )

    register_entity(caster)
    register_entity(target)

    # WondrousItem with embedded spell mechanics
    item = WondrousItem(
        name="Wand of Fireballs",
        description="Shoots a fiery bead.",
        active_mechanics=SpellMechanics(
            damage_dice="8d6", damage_type="fire", save_required="dexterity", half_damage_on_save=True
        ),
    )
    await ItemCompendium.save_item(vault_path, item)

    config = {"configurable": {"thread_id": vault_path}}

    # Mock random rolls so the Goblin fails the save and takes exactly 30 damage
    with patch("event_handlers.roll_dice", return_value=30), patch("random.randint", return_value=5):
        res = await use_ability_or_spell.ainvoke(
            {"caster_name": "Mage", "ability_name": "Wand of Fireballs", "target_names": ["Goblin"]}, config=config
        )

    assert "Wand of Fireballs" in res
    assert "Took 30 fire damage" in res
    assert target.hp.base_value == 0


@pytest.mark.asyncio
async def test_wondrous_item_charge_variants(setup_engine_and_vault):
    """
    Test WondrousItems casting spells with different usage limits:
    1. Unlimited uses.
    2. Total limited uses (consumable charges).
    3. Rechargeable uses.
    """
    vault_path = setup_engine_and_vault

    # 1. Unlimited Uses
    unlimited_item = WondrousItem(
        name="Wand of Unlimited Sparks", active_mechanics=SpellMechanics(damage_dice="1d4", damage_type="lightning")
    )

    # 2. Total Limited Uses (no recharge)
    limited_item = WondrousItem(
        name="Amulet of Limited Power",
        charges=3,
        max_charges=3,
        consume_on_use=True,
        active_mechanics=SpellMechanics(damage_dice="2d4", damage_type="force"),
    )

    # 3. Rechargeable Uses
    recharge_item = WondrousItem(
        name="Staff of the Morning",
        charges=5,
        max_charges=5,
        recharge_condition="Recharges 1d4+1 charges daily at dawn.",
        active_mechanics=SpellMechanics(damage_dice="8d6", damage_type="radiant"),
    )

    # Save to Compendium
    await ItemCompendium.save_item(vault_path, unlimited_item)
    await ItemCompendium.save_item(vault_path, limited_item)
    await ItemCompendium.save_item(vault_path, recharge_item)

    # Assert correct loading and properties for Unlimited
    loaded_unlimited = await ItemCompendium.load_item(vault_path, "Wand of Unlimited Sparks")
    assert loaded_unlimited.charges is None
    assert loaded_unlimited.recharge_condition == ""

    # Assert correct loading and properties for Total Limited
    loaded_limited = await ItemCompendium.load_item(vault_path, "Amulet of Limited Power")
    assert loaded_limited.charges == 3
    assert loaded_limited.max_charges == 3
    assert loaded_limited.consume_on_use is True
    assert loaded_limited.recharge_condition == ""

    # Assert correct loading and properties for Rechargeable
    loaded_recharge = await ItemCompendium.load_item(vault_path, "Staff of the Morning")
    assert loaded_recharge.charges == 5
    assert loaded_recharge.max_charges == 5
    assert "dawn" in loaded_recharge.recharge_condition

    # Verify they can be used via the standard ability tool natively
    caster = Creature(
        name="Mage",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        hp=ModifiableValue(base_value=50),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    register_entity(caster)
    register_entity(target)

    config = {"configurable": {"thread_id": vault_path}}

    with patch("event_handlers.roll_dice", return_value=10):
        # Cast Unlimited
        res1 = await use_ability_or_spell.ainvoke(
            {"caster_name": "Mage", "ability_name": "Wand of Unlimited Sparks", "target_names": ["Goblin"]}, config=config
        )
        assert "Wand of Unlimited Sparks" in res1
        assert target.hp.base_value == 40  # 50 - 10

        # Cast Limited
        res2 = await use_ability_or_spell.ainvoke(
            {"caster_name": "Mage", "ability_name": "Amulet of Limited Power", "target_names": ["Goblin"]}, config=config
        )
        assert "Amulet of Limited Power" in res2
        assert target.hp.base_value == 30  # 40 - 10

        # Cast Rechargeable
        res3 = await use_ability_or_spell.ainvoke(
            {"caster_name": "Mage", "ability_name": "Staff of the Morning", "target_names": ["Goblin"]}, config=config
        )
        assert "Staff of the Morning" in res3
        assert target.hp.base_value == 20  # 30 - 10


@pytest.mark.asyncio
async def test_weapon_item_applies_magic_bonus(setup_engine_and_vault):
    """
    Test that equipping a WeaponItem creates a MeleeWeapon entity with the
    correct magic_bonus, and that the engine natively applies it to attack and damage rolls.
    """
    vault_path = setup_engine_and_vault
    j_dir = get_journals_dir(vault_path)

    char_md = os.path.join(j_dir, "Fighter.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write("---\nequipment:\n  main_hand: None\n---")

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )
    target = Creature(
        name="Goblin",
        vault_path=vault_path,
        x=5,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=12),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
    )

    register_entity(fighter)
    register_entity(target)

    weapon_item = WeaponItem(name="Longsword +2", damage_dice="1d8", damage_type="slashing", magic_bonus=2)
    await ItemCompendium.save_item(vault_path, weapon_item)

    config = {"configurable": {"thread_id": vault_path}}

    res = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Longsword +2", "item_slot": "main_hand"}, config=config
    )
    assert "Success" in res, f"Equip tool failed: {res}"

    # Verify the weapon entity was created with the right magic bonus
    weapon_uuid = fighter.equipped_weapon_uuid
    assert weapon_uuid is not None

    weapon_entity = get_entity(weapon_uuid, vault_path)
    assert isinstance(weapon_entity, MeleeWeapon)
    assert weapon_entity.magic_bonus == 2

    # Verify combat calculations natively apply the +2
    # AC is 12. Roll 10 + 0 STR + 2 Magic = 12 (Hit).
    # Damage: Roll 5 + 0 STR + 2 Magic = 7 damage.
    with patch("event_handlers.random.randint", return_value=10), patch("event_handlers.roll_dice", return_value=5):
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Goblin"}, config=config)

    assert "HIT!" in res
    assert "dealt 7 damage" in res
    assert target.hp.base_value == 13


@pytest.mark.asyncio
async def test_armor_item_restrictions(setup_engine_and_vault):
    """
    Test mapping of ArmorItems with AC max DEX validation, and attunement restrictions.
    """
    vault_path = setup_engine_and_vault
    j_dir = get_journals_dir(vault_path)

    char_md = os.path.join(j_dir, "Rogue.md")
    with open(char_md, "w", encoding="utf-8") as f:
        # High dex, low str rogue
        f.write(
            "---\nspecies: Elf\nalignment: chaotic neutral\nclasses: [{class_name: Rogue, level: 3}]\n"
            "dexterity: 18\nstrength: 8\nequipment:\n  armor: None\nattuned_items: []\n---"
        )

    rogue = Creature(
        name="Rogue",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=14),
        strength_mod=ModifiableValue(base_value=-1),
        dexterity_mod=ModifiableValue(base_value=4),
        constitution_mod=ModifiableValue(base_value=0),
        intelligence_mod=ModifiableValue(base_value=0),
        wisdom_mod=ModifiableValue(base_value=0),
        charisma_mod=ModifiableValue(base_value=0),
        tags=["pc"],
    )
    register_entity(rogue)

    # 1. Medium Armor (Half Plate) -> Max Dex +2. Base AC 15 + 2 = 17 (instead of 15 + 4 = 19)
    half_plate = ArmorItem(name="Half Plate", armor_category="Medium", base_ac=15)
    await ItemCompendium.save_item(vault_path, half_plate)

    config = {"configurable": {"thread_id": vault_path}}
    res = await equip_item.ainvoke({"character_name": "Rogue", "item_name": "Half Plate", "item_slot": "armor"}, config=config)
    assert "Success" in res, f"Equip tool failed: {res}"

    # Verify Engine and file are 17 AC
    assert rogue.ac.base_value == 17

    # 2. Heavy Armor (Plate) -> Max Dex 0. Base AC 18.
    plate = ArmorItem(name="Plate Armor", armor_category="Heavy", base_ac=18, strength_requirement=15)
    await ItemCompendium.save_item(vault_path, plate)

    await equip_item.ainvoke({"character_name": "Rogue", "item_name": "None", "item_slot": "armor"}, config=config)
    await equip_item.ainvoke({"character_name": "Rogue", "item_name": "Plate Armor", "item_slot": "armor"}, config=config)
    assert rogue.ac.base_value == 18  # Dex is ignored

    # 3. Attunement Restriction (e.g., Dwarven Plate requires Dwarf)
    dwarven_plate = ArmorItem(
        name="Dwarven Plate",
        armor_category="Heavy",
        base_ac=18,
        plus_ac_bonus=2,
        requires_attunement=True,
        tags=["requires_attunement_by_dwarf"],
    )
    await ItemCompendium.save_item(vault_path, dwarven_plate)

    # Try to attune as Elf
    res = await attune_item.ainvoke(
        {"character_name": "Rogue", "item_name": "Dwarven Plate", "action": "attune"}, config=config
    )
    assert "SYSTEM ERROR" in res
    assert "dwarf" in res


@pytest.mark.asyncio
async def test_attune_item_enforces_restrictions(setup_engine_and_vault):
    """
    Test that the attune_item tool enforces class, species, and alignment restrictions.
    """
    vault_path = setup_engine_and_vault
    j_dir = get_journals_dir(vault_path)

    char_md = os.path.join(j_dir, "Paladin.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write(
            "---\nspecies: Human\nalignment: lawful good\nclasses: [{class_name: Paladin, level: 5}]\n"
            "equipment:\n  main_hand: None\nattuned_items: []\n---"
        )

    paladin = Creature(
        name="Paladin",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=40),
        ac=ModifiableValue(base_value=18),
        strength_mod=ModifiableValue(base_value=4),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["pc"],
    )
    register_entity(paladin)

    config = {"configurable": {"thread_id": vault_path}}

    # 1. Class Restriction (Requires Paladin)
    holy_avenger = WeaponItem(
        name="Holy Avenger",
        damage_dice="1d8",
        damage_type="slashing",
        requires_attunement=True,
        tags=["requires_attunement_by_paladin"],
    )
    await ItemCompendium.save_item(vault_path, holy_avenger)

    # Paladin attunes -> Success
    res1 = await attune_item.ainvoke(
        {"character_name": "Paladin", "item_name": "Holy Avenger", "action": "attune"}, config=config
    )
    assert "Success" in res1

    # Unattune to free slot
    await attune_item.ainvoke({"character_name": "Paladin", "item_name": "Holy Avenger", "action": "unattune"}, config=config)

    # 2. Species Restriction (Requires Elf)
    elven_chain = ArmorItem(
        name="Elven Chain", armor_category="Medium", requires_attunement=True, tags=["requires_attunement_by_elf"]
    )
    await ItemCompendium.save_item(vault_path, elven_chain)

    # Paladin (Human) tries to attune -> Fails
    res2 = await attune_item.ainvoke(
        {"character_name": "Paladin", "item_name": "Elven Chain", "action": "attune"}, config=config
    )
    assert "SYSTEM ERROR" in res2
    assert "elf" in res2

    # 3. Alignment Restriction (Requires Evil)
    evil_sword = WeaponItem(
        name="Sword of Evil",
        damage_dice="1d8",
        damage_type="slashing",
        requires_attunement=True,
        tags=["requires_attunement_by_evil"],
    )
    await ItemCompendium.save_item(vault_path, evil_sword)

    # Paladin (Good) tries to attune -> Fails
    res3 = await attune_item.ainvoke(
        {"character_name": "Paladin", "item_name": "Sword of Evil", "action": "attune"}, config=config
    )
    assert "SYSTEM ERROR" in res3
    assert "evil" in res3


@pytest.mark.asyncio
async def test_equip_item_slot_management(setup_engine_and_vault):
    """
    Test that the equip_item tool accurately blocks equipping into occupied slots
    (e.g., all ring slots full, boots, armor, and aliases like helmet/necklace/bracers),
    and that it allows for more than one (like rings) when appropriate.
    """
    vault_path = setup_engine_and_vault
    j_dir = get_journals_dir(vault_path)

    char_md = os.path.join(j_dir, "Fighter.md")
    with open(char_md, "w", encoding="utf-8") as f:
        f.write(
            "---\nequipment:\n  armor: Chainmail\n  shield: None\n  head: Leather Cap\n  cloak: None\n"
            "  gloves: None\n  boots: Leather Boots\n  ring1: None\n  ring2: None\n  amulet: None\n"
            "  main_hand: None\n  off_hand: None\n---"
        )

    fighter = Creature(
        name="Fighter",
        vault_path=vault_path,
        x=0,
        y=0,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        tags=["pc"],
    )
    register_entity(fighter)

    config = {"configurable": {"thread_id": vault_path}}

    # 1. Allow for more than one (like rings) when appropriate
    res1 = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Ring of Protection", "item_slot": "ring"}, config=config
    )
    assert "Success" in res1
    assert "ring1" in res1

    res2 = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Ring of Fire Resistance", "item_slot": "ring"}, config=config
    )
    assert "Success" in res2
    assert "ring2" in res2

    # 2. Block equipping when all ring slots are full
    res3 = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Ring of Water Walking", "item_slot": "ring"}, config=config
    )
    assert "Error" in res3
    assert "Both ring slots are already occupied" in res3

    # 3. Block equipping into occupied standard slots (and test aliases!)
    res_boots = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Boots of Speed", "item_slot": "boots"}, config=config
    )
    assert "Error" in res_boots
    assert "already occupied by 'Leather Boots'" in res_boots

    res_armor = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Plate Armor", "item_slot": "armor"}, config=config
    )
    assert "Error" in res_armor
    assert "already occupied by 'Chainmail'" in res_armor

    res_helmet = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Iron Helmet", "item_slot": "helmet"}, config=config
    )
    assert "Error" in res_helmet
    assert "already occupied by 'Leather Cap'" in res_helmet

    # 4. Equip into empty slots using aliases
    res_amulet = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Magic Necklace", "item_slot": "necklace"}, config=config
    )
    assert "Success" in res_amulet

    res_bracers = await equip_item.ainvoke(
        {"character_name": "Fighter", "item_name": "Leather Bracers", "item_slot": "bracers"}, config=config
    )
    assert "Success" in res_bracers
