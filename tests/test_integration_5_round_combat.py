import os
import json
import pytest
from unittest.mock import patch

from vault_io import initialize_engine_from_vault
from tools import (
    start_combat,
    update_combat_state,
    end_combat,
    move_entity,
    execute_melee_attack,
    use_ability_or_spell,
    perform_ability_check_or_save,
)
from spatial_engine import spatial_service
from registry import clear_registry, get_all_entities


@pytest.fixture(autouse=True)
def setup_engine_state():
    """Ensures a clean slate for the spatial engine and object registry."""
    clear_registry()
    spatial_service.clear()
    yield


@pytest.fixture
def mock_5_round_vault(mock_obsidian_vault):
    """Constructs the high-level PCs and Enemies, and loads the Compendium."""
    journals_dir = os.path.join(mock_obsidian_vault, "Journals")

    # PCs (~Level 10)
    pc_data = [
        ("Fighter", 100, 18, 5, 1, "Longsword"),
        ("Thief", 70, 16, 1, 5, "Dagger"),
        ("Monk", 70, 17, 1, 4, "Unarmed"),
        ("Wizard", 60, 12, 0, 2, "Quarterstaff"),
    ]
    for name, hp, ac, str_mod, dex_mod, weapon in pc_data:
        with open(os.path.join(journals_dir, f"{name}.md"), "w", encoding="utf-8") as f:
            tags = "[pc]"
            if name == "Wizard":
                tags = "[pc, can_cast_shield]"
            if name == "Thief":
                tags = "[pc, evasion]"
            f.write(
                f"---\nname: {name}\ntags: {tags}\nhp: {hp}\nmax_hp: {hp}\nac: {ac}\n"
                f"strength_mod: {str_mod}\ndexterity_mod: {dex_mod}\n"
                f"equipment: {{main_hand: {weapon}}}\nx: 0.0\ny: 0.0\n---\n"
            )

    # NPCs
    npc_data = [
        ("Troll", 84, 15, 4, 1, "Claws", "vulnerabilities: [fire]"),
        ("Orc1", 30, 13, 3, 1, "Greataxe", ""),
        ("Orc2", 30, 13, 3, 1, "Greataxe", ""),
        ("Goblin", 15, 15, -1, 2, "Scimitar", "tags: [nimble_escape]"),
    ]
    for name, hp, ac, str_mod, dex_mod, weapon, extra in npc_data:
        with open(os.path.join(journals_dir, f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(
                f"---\nname: {name}\ntags: [monster]\nhp: {hp}\nmax_hp: {hp}\nac: {ac}\n"
                f"strength_mod: {str_mod}\ndexterity_mod: {dex_mod}\n"
                f"equipment: {{main_hand: {weapon}}}\nx: 30.0\ny: 0.0\n{extra}\n---\n"
            )

    with open(os.path.join(journals_dir, "CAMPAIGN_MASTER.md"), "w", encoding="utf-8") as f:
        f.write("---\ntags: [campaign]\n---\n# Campaign Master\n\n## Major Milestones (Event Log)\n- Started.\n")

    # Create Fireball in Compendium
    comp_dir = os.path.join(mock_obsidian_vault, "Compendium", "entries")
    os.makedirs(comp_dir, exist_ok=True)

    fireball = {
        "name": "Fireball",
        "category": "spell",
        "action_type": "Action",
        "description": "Boom.",
        "mechanics": {"damage_dice": "8d6", "damage_type": "fire", "save_required": "dexterity", "half_damage_on_save": True},
    }
    with open(os.path.join(comp_dir, "fireball.json"), "w", encoding="utf-8") as f:
        json.dump(fireball, f)

    registry = {"fireball": os.path.join(comp_dir, "fireball.json")}
    with open(os.path.join(mock_obsidian_vault, "Compendium", "registry.json"), "w", encoding="utf-8") as f:
        json.dump(registry, f)

    return mock_obsidian_vault


@pytest.mark.asyncio
async def test_5_round_combat_execution(mock_5_round_vault):
    vault_path = mock_5_round_vault
    config = {"configurable": {"thread_id": vault_path}}

    # 1. Initialize Engine Context
    await initialize_engine_from_vault(vault_path)

    # Space everyone out (PCs at 0, Enemies at 30ft away)
    for e in get_all_entities(vault_path).values():
        if e.name in ["Fighter", "Thief", "Monk", "Wizard"]:
            e.x, e.y = 0, 0
        else:
            e.x, e.y = 30, 0
        spatial_service.sync_entity(e)

    res = await start_combat.ainvoke(
        {
            "pc_names": ["Fighter", "Thief", "Monk", "Wizard"],
            "enemies": [
                {"name": "Troll", "hp": 84, "ac": 15, "dex_mod": 1, "x": 30.0, "y": 0.0},
                {"name": "Orc1", "hp": 30, "ac": 13, "dex_mod": 1, "x": 30.0, "y": -5.0},
                {"name": "Orc2", "hp": 30, "ac": 13, "dex_mod": 1, "x": 30.0, "y": -10.0},
                {"name": "Goblin", "hp": 15, "ac": 15, "dex_mod": 2, "x": 30.0, "y": 5.0},
            ],
        },
        config=config,
    )
    assert "Combat started!" in res

    # MOCK BEHAVIOR: Guarantee average hit and average damage values cleanly (e.g. d20 rolls 11)
    with patch("random.randint", side_effect=lambda a, b: b // 2 + 1):

        # --- ROUND 1 ---
        res = await use_ability_or_spell.ainvoke(
            {"caster_name": "Wizard", "ability_name": "Fireball", "target_names": ["Troll", "Orc1", "Orc2"]}, config=config
        )
        assert "Troll" in res and "Orc1" in res

        await move_entity.ainvoke(
            {"entity_name": "Fighter", "target_x": 25, "target_y": 0, "movement_type": "walk"}, config=config
        )
        res = await execute_melee_attack.ainvoke({"attacker_name": "Fighter", "target_name": "Troll"}, config=config)
        assert "HIT!" in res
        await update_combat_state.ainvoke({"next_turn": True}, config=config)

        # --- ROUND 2 ---
        res = await execute_melee_attack.ainvoke({"attacker_name": "Troll", "target_name": "Fighter"}, config=config)
        await move_entity.ainvoke({"entity_name": "Orc1", "target_x": 5, "target_y": 0, "movement_type": "walk"}, config=config)

        # Orc1 attacks Wizard -> Wizard casts Shield reaction
        res = await execute_melee_attack.ainvoke({"attacker_name": "Orc1", "target_name": "Wizard"}, config=config)
        wizard = [e for e in get_all_entities(vault_path).values() if e.name == "Wizard"][0]
        assert wizard.reaction_used is True
        await update_combat_state.ainvoke({"next_turn": True}, config=config)

        # --- ROUND 3 ---
        await move_entity.ainvoke(
            {"entity_name": "Monk", "target_x": 25, "target_y": 5, "movement_type": "walk"}, config=config
        )
        await execute_melee_attack.ainvoke({"attacker_name": "Monk", "target_name": "Goblin"}, config=config)
        await update_combat_state.ainvoke({"next_turn": True}, config=config)

        # --- ROUND 4 ---
        await move_entity.ainvoke(
            {"entity_name": "Thief", "target_x": 25, "target_y": -5, "movement_type": "walk"}, config=config
        )
        await perform_ability_check_or_save.ainvoke({"character_name": "Thief", "skill_or_stat_name": "stealth"}, config=config)
        await execute_melee_attack.ainvoke({"attacker_name": "Thief", "target_name": "Orc2", "advantage": True}, config=config)
        await update_combat_state.ainvoke({"next_turn": True}, config=config)

        # --- ROUND 5 ---
        # Troll attempts to flee the Fighter, triggering an opportunity attack
        # (We don't use Orc1 fleeing Wizard, because Wizard correctly spent their reaction on Shield in Round 2!)
        res = await move_entity.ainvoke(
            {"entity_name": "Troll", "target_x": 35, "target_y": 0, "movement_type": "walk"}, config=config
        )
        assert "Opportunity Attacks from" in res
        assert "Fighter" in res

    # Cleanup
    res = await end_combat.ainvoke({}, config=config)
    assert "Combat ended successfully" in res
