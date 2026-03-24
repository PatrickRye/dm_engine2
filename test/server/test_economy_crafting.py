# test/server/test_economy_crafting.py
"""Tests for REQ-ECO-002/003/004 and REQ-CRF-001/002/003/004/005."""
import pytest
from unittest.mock import patch, AsyncMock

from world_tools import (
    sell_item,
    deduct_lifestyle_expense,
    check_craft_prerequisites,
    calculate_crafting_time,
    record_crafting_progress,
)


class MockConfig(dict):
    """Dict-like config compatible with LangChain's ensure_config()."""

    def __init__(self, vault_path):
        super().__init__()
        self["configurable"] = {"thread_id": vault_path}


# =============================================================================
# REQ-ECO-002: Selling Mundane Equipment (50% of base cost)
# =============================================================================
class TestSellItem:
    """REQ-ECO-002: Undamaged mundane equipment sells for exactly 50% of base cost."""

    @pytest.mark.asyncio
    async def test_undamaged_mundane_sells_at_half(self):
        """REQ-ECO-002: 1000 cp base → 500 cp sale (50%)."""
        result = await sell_item.ainvoke(
            {
                "item_name": "Longsword",
                "item_type": "weapon",
                "base_cost_cp": 1000,  # 10 gp → 500 cp sale
                "is_damaged": False,
                "is_trade_good": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "500 cp" in result
        assert "50%" in result
        assert "undamaged mundane" in result.lower()

    @pytest.mark.asyncio
    async def test_damaged_mundane_also_half(self):
        """REQ-ECO-002: Damaged items also sell at 50% (no bonus for undamaged only)."""
        result = await sell_item.ainvoke(
            {
                "item_name": "Chain Mail",
                "item_type": "armor",
                "base_cost_cp": 7500,  # 75 gp
                "is_damaged": True,
                "is_trade_good": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "3750 cp" in result
        assert "damaged" in result.lower()

    @pytest.mark.asyncio
    async def test_trade_good_sells_at_full(self):
        """REQ-ECO-003: Trade goods sell for 100% of base cost."""
        result = await sell_item.ainvoke(
            {
                "item_name": "Gold Bar",
                "item_type": "trade_good",
                "base_cost_cp": 5000,  # 50 gp gold bar
                "is_damaged": False,
                "is_trade_good": True,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "5000 cp" in result
        assert "100%" in result
        assert "trade good" in result.lower()

    @pytest.mark.asyncio
    async def test_wheat_trade_good_full_price(self):
        """REQ-ECO-003: Wheat as trade good sells at full price."""
        result = await sell_item.ainvoke(
            {
                "item_name": "Buskel of Wheat",
                "item_type": "trade_good",
                "base_cost_cp": 100,
                "is_trade_good": True,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "100 cp" in result

    @pytest.mark.asyncio
    async def test_sell_item_shows_breakdown(self):
        """Result includes base cost and sale price with formula explanation."""
        result = await sell_item.ainvoke(
            {
                "item_name": "Hunting Trap",
                "item_type": "adventuring gear",
                "base_cost_cp": 500,  # 5 gp
                "is_damaged": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "Base cost:" in result
        assert "Sale price:" in result
        assert "5 gp" in result  # base cost


# =============================================================================
# REQ-ECO-004: Lifestyle Expenses
# =============================================================================
class TestLifestyleExpenses:
    """REQ-ECO-004: Lifestyle expenses deducted per downtime day."""

    @pytest.mark.asyncio
    async def test_modest_lifestyle_one_day(self):
        """Modest lifestyle = 10 GP/day. 1 day from 500 gp wallet."""
        result = await deduct_lifestyle_expense.ainvoke(
            {
                "character_name": "Aldric",
                "lifestyle": "modest",
                "days": 1,
                "wallet_cp": 5000,  # 50 gp
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "40 gp" in result or "4000 cp" in result
        assert "remaining" in result.lower()
        assert "Aldric" in result

    @pytest.mark.asyncio
    async def test_modest_lifestyle_multiple_days(self):
        """5 days modest = 50 gp. From 100 gp wallet = 50 gp remaining."""
        result = await deduct_lifestyle_expense.ainvoke(
            {
                "character_name": "Aldric",
                "lifestyle": "modest",
                "days": 5,
                "wallet_cp": 10000,  # 100 gp
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "50 gp" in result  # 5 days × 10 gp
        assert "remaining" in result.lower()

    @pytest.mark.asyncio
    async def test_aristocratic_is_expensive(self):
        """Aristocratic lifestyle = 100 GP/day."""
        result = await deduct_lifestyle_expense.ainvoke(
            {
                "character_name": "Baroness",
                "lifestyle": "Aristocratic",
                "days": 1,
                "wallet_cp": 20000,  # 200 gp
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "100 gp" in result

    @pytest.mark.asyncio
    async def test_insufficient_funds(self):
        """Wallet too thin for lifestyle shows shortfall."""
        result = await deduct_lifestyle_expense.ainvoke(
            {
                "character_name": "Pauper",
                "lifestyle": "modest",
                "days": 10,
                "wallet_cp": 500,  # 5 gp — not enough for 10 days modest (100 gp)
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "insufficient" in result.lower() or "shortfall" in result.lower()

    @pytest.mark.asyncio
    async def test_invalid_lifestyle(self):
        """Unknown lifestyle name returns error."""
        result = await deduct_lifestyle_expense.ainvoke(
            {
                "character_name": "Wanderer",
                "lifestyle": "extravagant",
                "days": 1,
                "wallet_cp": 10000,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "SYSTEM ERROR" in result

    @pytest.mark.asyncio
    async def test_squalid_is_cheapest(self):
        """Squalid lifestyle = 1 GP/day."""
        result = await deduct_lifestyle_expense.ainvoke(
            {
                "character_name": "Beggar",
                "lifestyle": "squalid",
                "days": 7,
                "wallet_cp": 1000,  # 10 gp
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "7" in result  # 7 gp for 7 days squalid


# =============================================================================
# REQ-CRF-001: Crafting Prerequisites
# =============================================================================
class TestCraftingPrerequisites:
    """REQ-CRF-001: Proficiency + materials required to craft."""

    @pytest.mark.asyncio
    async def test_has_proficiency_and_materials(self):
        """Character with tool proficiency and enough wallet can craft."""
        result = await check_craft_prerequisites.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 1000,  # 10 gp
                "required_tool": "smith's tools",
                "character_has_tool_proficiency": True,
                "character_wallet_cp": 1000,  # exactly 50% of 10 gp = 5 gp materials
                "has_crafter_feat": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "CAN BEGIN CRAFTING" in result
        assert "✅" in result

    @pytest.mark.asyncio
    async def test_lacks_proficiency(self):
        """Without tool proficiency, cannot craft even with funds."""
        result = await check_craft_prerequisites.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 1000,
                "required_tool": "smith's tools",
                "character_has_tool_proficiency": False,
                "character_wallet_cp": 50000,
                "has_crafter_feat": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "CANNOT CRAFT" in result
        assert "lacks proficiency" in result.lower()

    @pytest.mark.asyncio
    async def test_insufficient_funds(self):
        """Without enough for materials, cannot craft."""
        result = await check_craft_prerequisites.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 1000,  # materials = 500 cp
                "required_tool": "smith's tools",
                "character_has_tool_proficiency": True,
                "character_wallet_cp": 100,  # only 1 gp — not enough
                "has_crafter_feat": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "CANNOT CRAFT" in result
        assert "insufficient" in result.lower()

    @pytest.mark.asyncio
    async def test_crafter_feat_reduces_cost(self):
        """REQ-CRF-003: Crafter feat reduces material cost by 20%."""
        # Base cost 1000 cp → materials = 500 cp. Crafter feat → 400 cp.
        # Wallet 400 cp: sufficient. Without feat would need 500 cp.
        result = await check_craft_prerequisites.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 1000,
                "required_tool": "smith's tools",
                "character_has_tool_proficiency": True,
                "character_wallet_cp": 400,
                "has_crafter_feat": True,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "CAN BEGIN CRAFTING" in result
        assert "crafter feat" in result.lower()


# =============================================================================
# REQ-CRF-002/003: Crafting Time
# =============================================================================
class TestCraftingTime:
    """REQ-CRF-002: 50 GP progress per 8-hour day per crafter."""

    @pytest.mark.asyncio
    async def test_standard_crafting_time(self):
        """100 gp item / 50 gp/day = 2 days for 1 crafter."""
        result = await calculate_crafting_time.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 10000,  # 100 gp
                "num_crafters": 1,
                "has_crafter_feat": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "2" in result
        assert "day" in result.lower()

    @pytest.mark.asyncio
    async def test_multiple_crafters_faster(self):
        """2 crafters × 50 gp/day = 100 gp/day. 100 gp item = 1 day."""
        result = await calculate_crafting_time.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 10000,
                "num_crafters": 2,
                "has_crafter_feat": False,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "1" in result
        assert "day" in result.lower()

    @pytest.mark.asyncio
    async def test_crafter_feat_faster(self):
        """REQ-CRF-003: Crafter feat = 20% faster = 62.5 gp/day effective progress."""
        # 100 gp / (50 × 1.25) = 1.6 → ceil = 2 days (same as baseline for small items)
        # Let's try a larger item: 1000 gp → 1000/62.5 = 16 → ceil = 16 days vs 20 without feat
        result = await calculate_crafting_time.ainvoke(
            {
                "item_name": "Plate Armor",
                "item_base_cost_cp": 80000,  # 800 gp
                "num_crafters": 1,
                "has_crafter_feat": True,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "Crafter feat" in result
        assert "day" in result.lower()

    @pytest.mark.asyncio
    async def test_potion_healing_time(self):
        """REQ-CRF-004: Common potion = 1 day, Rare = 7 days."""
        result = await calculate_crafting_time.ainvoke(
            {
                "item_name": "Potion of Healing",
                "item_base_cost_cp": 2500,
                "num_crafters": 1,
                "has_crafter_feat": False,
                "is_herbalism_kit_potion": True,
                "potion_rarity": "common",
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "1 day" in result
        assert "Herbalism Kit" in result

    @pytest.mark.asyncio
    async def test_potion_uncommon_days(self):
        """REQ-CRF-004: Uncommon potion = 3 days."""
        result = await calculate_crafting_time.ainvoke(
            {
                "item_name": "Potion of Greater Healing",
                "item_base_cost_cp": 2500,
                "num_crafters": 1,
                "is_herbalism_kit_potion": True,
                "potion_rarity": "uncommon",
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "3" in result

    @pytest.mark.asyncio
    async def test_spell_scroll_time(self):
        """REQ-CRF-005: Spell scrolls take longer at higher levels."""
        result = await calculate_crafting_time.ainvoke(
            {
                "item_name": "Scroll of Fireball",
                "item_base_cost_cp": 25000,  # 250 gp
                "num_crafters": 1,
                "has_crafter_feat": False,
                "is_herbalism_kit_potion": False,
                "spell_level": 3,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "Scroll" in result
        assert "day" in result.lower()


# =============================================================================
# REQ-CRF-002: Crafting Progress Tracking
# =============================================================================
class TestCraftingProgress:
    """REQ-CRF-002: Crafting progress accumulates and completes when >= base cost."""

    @pytest.mark.asyncio
    async def test_progress_completes(self):
        """10000 cp item, 5000 cp/day × 2 days = 10000 cp → complete."""
        result = await record_crafting_progress.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 10000,  # 100 gp
                "days_worked": 2,
                "num_crafters": 1,
                "has_crafter_feat": False,
                "prior_progress_cp": 0,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "COMPLETE" in result
        assert "✅" in result

    @pytest.mark.asyncio
    async def test_progress_incomplete(self):
        """10000 cp item, only 5000 cp progress so far (50%)."""
        result = await record_crafting_progress.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 10000,  # 100 gp
                "days_worked": 1,
                "num_crafters": 1,
                "has_crafter_feat": False,
                "prior_progress_cp": 0,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "IN PROGRESS" in result
        assert "50" in result  # 50% progress

    @pytest.mark.asyncio
    async def test_progress_accumulates(self):
        """Progress from prior work + new work is summed correctly."""
        result = await record_crafting_progress.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 10000,  # 100 gp
                "days_worked": 1,
                "num_crafters": 1,
                "has_crafter_feat": False,
                "prior_progress_cp": 2500,  # 25 gp already done
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "75" in result  # 75% progress (7500/10000)
        assert "IN PROGRESS" in result

    @pytest.mark.asyncio
    async def test_excess_progress_discarded(self):
        """Progress beyond item base cost is noted but not wasted."""
        result = await record_crafting_progress.ainvoke(
            {
                "item_name": "Steel Shield",
                "item_base_cost_cp": 5000,  # 50 gp
                "days_worked": 3,  # 3 × 5000 = 15000 cp progress
                "num_crafters": 1,
                "has_crafter_feat": False,
                "prior_progress_cp": 0,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "COMPLETE" in result
        assert "excess" in result.lower()

    @pytest.mark.asyncio
    async def test_multiple_crafting_days_total(self):
        """3 crafters × 5000 cp/day × 5 days = 75000 cp progress."""
        result = await record_crafting_progress.ainvoke(
            {
                "item_name": "Plate Armor",
                "item_base_cost_cp": 50000,  # 500 gp
                "days_worked": 5,
                "num_crafters": 3,
                "has_crafter_feat": False,
                "prior_progress_cp": 0,
            },
            config=MockConfig("/tmp/vault"),
        )
        assert "COMPLETE" in result  # 75000 cp progress > 50000 cp cost
        assert "✅" in result
