import os
import pytest
import yaml
from unittest.mock import patch

from tools import manage_inventory, generate_random_loot


@pytest.fixture
def mock_inventory_entity(mock_obsidian_vault):
    """Helper fixture to seed the mocked vault with specific entities for inventory tests."""
    journals_dir = os.path.join(mock_obsidian_vault, "server", "Journals")
    os.makedirs(journals_dir, exist_ok=True)
    char_name = "Rogue_Thief"

    with open(os.path.join(journals_dir, f"{char_name}.md"), "w", encoding="utf-8") as f:
        yaml_frontmatter = (
            f"---\nname: {char_name}\ntags: [pc]\ngold: 15\ncurrency:\n  cp: 50\n  sp: 20\n  ep: 0\n  gp: 15\n  "
            f"pp: 1\ninventory:\n  - Thieves Tools\n  - Potion of Healing (x2) [Red Liquid]\n  - Shortbow\n  "
            f"- Arrow (x20)\n---\n# {char_name}\n\n## Event Log\n- Initialized.\n"
        )
        f.write(yaml_frontmatter)

    npc_name = "Merchant"
    with open(os.path.join(journals_dir, f"{npc_name}.md"), "w", encoding="utf-8") as f:
        npc_yaml_frontmatter = (
            f"---\nname: {npc_name}\ntags: [npc]\ngold: 100\ncurrency:\n  cp: 0\n  sp: 0\n  ep: 0\n  "
            f"gp: 100\n  pp: 0\ninventory:\n  - Torch (x50)\n  - Health Potion (x5)\n---\n"
            f"# {npc_name}\n\n## Event Log\n- Initialized.\n"
        )
        f.write(npc_yaml_frontmatter)

    return mock_obsidian_vault, char_name, npc_name


@pytest.mark.asyncio
async def test_inventory_buying_and_selling(mock_inventory_entity):
    vault_path, char_name, npc_name = mock_inventory_entity
    config = {"configurable": {"thread_id": vault_path}}

    # Rogue buys 5 torches for 5cp total from the Merchant
    res_buy_pc = await manage_inventory.ainvoke(
        {
            "character_name": char_name,
            "item_name": "Torch",
            "action": "add",
            "quantity": 5,
            "cp_change": -5,
            "context_log": "Bought 5 torches from Merchant.",
        },
        config=config,
    )

    # Merchant sells 5 torches for 5cp total
    res_buy_npc = await manage_inventory.ainvoke(
        {
            "character_name": npc_name,
            "item_name": "Torch",
            "action": "remove",
            "quantity": 5,
            "cp_change": 5,
            "context_log": "Sold 5 torches to Rogue.",
        },
        config=config,
    )

    assert "Success" in res_buy_npc
    assert "Success" in res_buy_pc

    # Check PC YAML
    with open(os.path.join(vault_path, "server", "Journals", f"{char_name}.md"), "r", encoding="utf-8") as f:
        pc_yaml = yaml.safe_load(f.read().split("---")[1])
        assert pc_yaml["currency"]["cp"] == 45  # Started with 50
        assert "Torch (x5)" in pc_yaml["inventory"]

    # Check NPC YAML
    with open(os.path.join(vault_path, "server", "Journals", f"{npc_name}.md"), "r", encoding="utf-8") as f:
        npc_yaml = yaml.safe_load(f.read().split("---")[1])
        assert npc_yaml["currency"]["cp"] == 5
        assert "Torch (x45)" in npc_yaml["inventory"]  # Successfully deducted 5 from 50


@pytest.mark.asyncio
async def test_inventory_currency_exchange_downconversion(mock_inventory_entity):
    """Tests that spending more silver than available dips into gold/platinum automatically to make change."""
    vault_path, char_name, npc_name = mock_inventory_entity
    config = {"configurable": {"thread_id": vault_path}}

    # Rogue has 1 pp, 15 gp, 0 ep, 20 sp, 50 cp. (Total copper = 1000 + 1500 + 0 + 200 + 50 = 2750 cp)
    # The Rogue tries to spend 30 sp, but they only have 20 sp in their pocket.
    # The engine should combine total wealth and break down the remaining change.

    res = await manage_inventory.ainvoke(
        {
            "character_name": char_name,
            "item_name": "Silver Dagger",
            "action": "add",
            "sp_change": -30,
            "context_log": "Bought a dagger.",
        },
        config=config,
    )

    assert "Success" in res
    with open(os.path.join(vault_path, "server", "Journals", f"{char_name}.md"), "r", encoding="utf-8") as f:
        pc_yaml = yaml.safe_load(f.read().split("---")[1])

        # 2750 cp - 300 cp (the 30 sp cost) = 2450 total remaining copper.
        # Mathematical breakdown: 2450 cp = 2 pp, 4 gp, 1 ep, 0 sp, 0 cp.
        curr = pc_yaml["currency"]
        assert curr["pp"] == 2
        assert curr["gp"] == 4
        assert curr["ep"] == 1
        assert curr["sp"] == 0
        assert curr["cp"] == 0


@pytest.mark.asyncio
async def test_inventory_insufficient_funds(mock_inventory_entity):
    vault_path, char_name, npc_name = mock_inventory_entity
    config = {"configurable": {"thread_id": vault_path}}

    # Rogue has 27.5 gp total wealth. Try to spend 50 gp.
    res = await manage_inventory.ainvoke(
        {
            "character_name": char_name,
            "item_name": "Plate Armor",
            "action": "add",
            "gp_change": -50,
            "context_log": "Tried to buy Plate Armor.",
        },
        config=config,
    )

    assert "Transaction Failed" in res


@pytest.mark.asyncio
async def test_inventory_consumable_usage(mock_inventory_entity):
    """Tests partial stack removal for consumables like arrows and potions."""
    vault_path, char_name, npc_name = mock_inventory_entity
    config = {"configurable": {"thread_id": vault_path}}

    # Rogue uses 1 Potion of Healing (Started with 2)
    await manage_inventory.ainvoke(
        {
            "character_name": char_name,
            "item_name": "Potion of Healing",
            "action": "remove",
            "quantity": 1,
            "context_log": "Drank a potion.",
        },
        config=config,
    )

    # Rogue shoots 5 Arrows (Started with 20)
    await manage_inventory.ainvoke(
        {
            "character_name": char_name,
            "item_name": "Arrow",
            "action": "remove",
            "quantity": 5,
            "context_log": "Shot 5 arrows in combat.",
        },
        config=config,
    )

    with open(os.path.join(vault_path, "server", "Journals", f"{char_name}.md"), "r", encoding="utf-8") as f:
        pc_yaml = yaml.safe_load(f.read().split("---")[1])
        inv = pc_yaml["inventory"]

        # Should elegantly strip the (x1) tag when reaching a single item!
        assert "Potion of Healing [Red Liquid]" in inv
        assert "Arrow (x15)" in inv


@pytest.mark.asyncio
async def test_inventory_stealing_looting_and_bartering(mock_inventory_entity):
    """Tests transferring items between entities with no currency involved."""
    vault_path, char_name, npc_name = mock_inventory_entity
    config = {"configurable": {"thread_id": vault_path}}

    # Rogue trades their Shortbow directly for 10 Torches from the Merchant
    await manage_inventory.ainvoke(
        {"character_name": char_name, "item_name": "Shortbow", "action": "remove", "quantity": 1}, config=config
    )
    await manage_inventory.ainvoke(
        {"character_name": npc_name, "item_name": "Shortbow", "action": "add", "quantity": 1}, config=config
    )

    await manage_inventory.ainvoke(
        {"character_name": npc_name, "item_name": "Torch", "action": "remove", "quantity": 10}, config=config
    )
    await manage_inventory.ainvoke(
        {"character_name": char_name, "item_name": "Torch", "action": "add", "quantity": 10}, config=config
    )

    with open(os.path.join(vault_path, "server", "Journals", f"{char_name}.md"), "r", encoding="utf-8") as f:
        pc_yaml = yaml.safe_load(f.read().split("---")[1])
        assert "Torch (x10)" in pc_yaml["inventory"]
        assert "Shortbow" not in pc_yaml["inventory"]  # Successfully removed

    with open(os.path.join(vault_path, "server", "Journals", f"{npc_name}.md"), "r", encoding="utf-8") as f:
        npc_yaml = yaml.safe_load(f.read().split("---")[1])
        assert "Shortbow" in npc_yaml["inventory"]
        assert "Torch (x40)" in npc_yaml["inventory"]


@pytest.mark.asyncio
async def test_generate_random_loot(mock_inventory_entity):
    """Tests that the random loot generator successfully builds string outputs based on CR tables."""
    vault_path, _, _ = mock_inventory_entity
    config = {"configurable": {"thread_id": vault_path}}

    # 1. Test Low Level Individual
    with patch("random.randint", return_value=15):  # Roll 15 usually triggers CP
        res1 = await generate_random_loot.ainvoke({"challenge_rating": 2, "loot_type": "individual"}, config=config)
        assert "cp" in res1

    # 2. Test Mid Level Hoard
    with patch("random.randint", return_value=95):  # Roll 95 triggers magic items
        res2 = await generate_random_loot.ainvoke({"challenge_rating": 6, "loot_type": "hoard"}, config=config)
        assert "gp" in res2
        assert "Weapon" in res2 or "Cloak" in res2 or "Ring" in res2
