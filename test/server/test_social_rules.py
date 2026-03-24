# test/server/test_social_rules.py
"""Tests for REQ-SOC-001/002/003/004 — NPC attitude-based social interaction."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from dnd_rules_engine import Creature
from world_tools import perform_social_interaction, _ATTITUDE_BASE_DC, _RISK_DC_MODIFIER


class MockConfig:
    """Mimics the RunnableConfig dict structure used by LangChain tools."""

    def __init__(self, vault_path):
        self.configurable = {"thread_id": vault_path}

    def __getitem__(self, key):
        return self.configurable

    def get(self, key, default=None):
        return self.configurable.get(key, default)


def _make_creature(name: str, cha_mod: int = 3):
    """Create a mock creature with a charisma modifier."""
    ent = MagicMock(spec=Creature)
    ent.entity_uuid = "test-uuid"
    ent.name = name
    ent.charisma_mod = MagicMock()
    ent.charisma_mod.total = cha_mod
    return ent


class TestAttitudeDCLookup:
    """Verify the attitude base DC and risk modifier tables are correct."""

    def test_attitude_base_dc_hostile(self):
        assert _ATTITUDE_BASE_DC["hostile"] == 14

    def test_attitude_base_dc_indifferent(self):
        assert _ATTITUDE_BASE_DC["indifferent"] == 10

    def test_attitude_base_dc_friendly(self):
        assert _ATTITUDE_BASE_DC["friendly"] == 8

    def test_attitude_base_dc_helpful(self):
        assert _ATTITUDE_BASE_DC["helpful"] == 0

    def test_risk_dc_negligible_minor(self):
        assert _RISK_DC_MODIFIER["negligible"] == 0
        assert _RISK_DC_MODIFIER["minor"] == 0

    def test_risk_dc_low(self):
        assert _RISK_DC_MODIFIER["low"] == 5

    def test_risk_dc_moderate(self):
        assert _RISK_DC_MODIFIER["moderate"] == 10

    def test_risk_dc_high(self):
        assert _RISK_DC_MODIFIER["high"] == 15

    def test_risk_dc_severe(self):
        assert _RISK_DC_MODIFIER["severe"] == 20


class TestPerformSocialInteraction:
    """REQ-SOC-001/002/003/004"""

    @pytest.mark.asyncio
    async def test_hostile_auto_fail_risky_request(self):
        """REQ-SOC-002: Hostile NPC auto-rejects risky requests without a roll."""
        result = await perform_social_interaction(
            character_name="Guts",
            target_npc_name="Orc Chief",
            request_description="Stand down and join us",
            npc_attitude="Hostile",
            request_risk="moderate",
            config=MockConfig("/tmp/vault"),
        )
        assert "AUTO-FAILURE" in result
        assert "REQ-SOC-002" in result
        assert "Orc Chief" in result
        assert "won't take risks" in result.lower()

    @pytest.mark.asyncio
    async def test_hostile_auto_fail_severe_risk(self):
        """REQ-SOC-002: Hostile NPC auto-fails severe risk requests."""
        result = await perform_social_interaction(
            character_name="Guts",
            target_npc_name="Orc Chief",
            request_description="Betray your warchief",
            npc_attitude="Hostile",
            request_risk="severe",
            config=MockConfig("/tmp/vault"),
        )
        assert "AUTO-FAILURE" in result
        assert "REQ-SOC-002" in result

    @pytest.mark.asyncio
    async def test_hostile_negligible_risk_rolls(self):
        """REQ-SOC-002: Hostile NPC will attempt negligible/minor requests (DC 14)."""
        influencer = _make_creature("Guts", cha_mod=3)
        config = MockConfig("/tmp/vault")

        with patch("world_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = influencer
            with patch("world_tools.roll_dice", return_value=14):
                result = await perform_social_interaction(
                    character_name="Guts",
                    target_npc_name="Orc Chief",
                    request_description="Share the time of day",
                    npc_attitude="Hostile",
                    request_risk="negligible",
                    config=config,
                )

        assert "AUTO-FAILURE" not in result
        assert "SUCCESS" in result  # 14+3=17 vs DC 14

    @pytest.mark.asyncio
    async def test_indifferent_auto_fail_moderate_risk(self):
        """REQ-SOC-003: Indifferent NPC auto-rejects moderate+ risk requests."""
        result = await perform_social_interaction(
            character_name="Guts",
            target_npc_name="Merchant",
            request_description="Invest in our venture",
            npc_attitude="Indifferent",
            request_risk="moderate",
            config=MockConfig("/tmp/vault"),
        )
        assert "AUTO-FAILURE" in result
        assert "REQ-SOC-003" in result
        assert "Merchant" in result

    @pytest.mark.asyncio
    async def test_indifferent_low_risk_rolls(self):
        """REQ-SOC-003: Indifferent NPC will attempt low-risk requests (DC 15 = 10+5)."""
        influencer = _make_creature("Guts", cha_mod=0)
        config = MockConfig("/tmp/vault")

        with patch("world_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = influencer
            with patch("world_tools.roll_dice", return_value=15):
                result = await perform_social_interaction(
                    character_name="Guts",
                    target_npc_name="Merchant",
                    request_description="Carry this package across town",
                    npc_attitude="Indifferent",
                    request_risk="low",
                    config=config,
                )

        assert "AUTO-FAILURE" not in result
        assert "**15** vs DC 15" in result
        assert "SUCCESS" in result

    @pytest.mark.asyncio
    async def test_indifferent_high_risk_auto_fail(self):
        """REQ-SOC-003: Indifferent NPC auto-fails high-risk requests."""
        result = await perform_social_interaction(
            character_name="Guts",
            target_npc_name="Merchant",
            request_description="Testify against the guild",
            npc_attitude="Indifferent",
            request_risk="high",
            config=MockConfig("/tmp/vault"),
        )
        assert "AUTO-FAILURE" in result
        assert "REQ-SOC-003" in result

    @pytest.mark.asyncio
    async def test_friendly_helpful_auto_success_minor(self):
        """REQ-SOC-004: Friendly (Eager to Help) auto-succeeds minor/negligible requests."""
        result = await perform_social_interaction(
            character_name="Guts",
            target_npc_name="Village Elder",
            request_description="Show us the path through the forest",
            npc_attitude="Friendly (Eager to Help)",
            request_risk="minor",
            config=MockConfig("/tmp/vault"),
        )
        assert "AUTO-SUCCESS" in result
        assert "REQ-SOC-004" in result
        assert "Village Elder" in result
        assert "readily accepts" in result.lower()

    @pytest.mark.asyncio
    async def test_friendly_helpful_risky_request_rolls(self):
        """REQ-SOC-004: Friendly (Eager to Help) with risky requests still requires a roll."""
        influencer = _make_creature("Guts", cha_mod=5)
        config = MockConfig("/tmp/vault")

        with patch("world_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = influencer
            with patch("world_tools.roll_dice", return_value=10):
                result = await perform_social_interaction(
                    character_name="Guts",
                    target_npc_name="Village Elder",
                    request_description="Lead the patrol into the dungeon",
                    npc_attitude="Friendly (Eager to Help)",
                    request_risk="high",
                    config=config,
                )

        assert "AUTO-" not in result  # No auto-fail/success
        assert "DC 15" in result  # 0 + 15
        assert "**15** vs DC 15" in result  # 10+5=15 vs DC 15
        assert "SUCCESS" in result

    @pytest.mark.asyncio
    async def test_req_soc_001_dc_calculation(self):
        """REQ-SOC-001: DC = Base_DC + Risk_Modifier formula."""
        influencer = _make_creature("Guts", cha_mod=0)
        config = MockConfig("/tmp/vault")

        with patch("world_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = influencer
            with patch("world_tools.roll_dice", return_value=1):
                result = await perform_social_interaction(
                    character_name="Guts",
                    target_npc_name="Guard",
                    request_description="Let us pass",
                    npc_attitude="Indifferent",
                    request_risk="low",
                    config=config,
                )

        # Indifferent DC 10 + Low +5 = DC 15. Roll 1+0=1 vs DC 15 → FAIL
        assert "DC 15" in result
        assert "**1** vs DC 15" in result
        assert "FAILURE" in result

    @pytest.mark.asyncio
    async def test_manual_roll_override(self):
        """manual_roll_total parameter overrides the dice roll."""
        influencer = _make_creature("Guts", cha_mod=3)
        config = MockConfig("/tmp/vault")

        with patch("world_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = influencer
            result = await perform_social_interaction(
                character_name="Guts",
                target_npc_name="Guard",
                request_description="Open the gate",
                npc_attitude="Indifferent",
                request_risk="low",
                manual_roll_total=17,  # Player pre-rolled a 17
                config=config,
            )

        assert "manual(17)" in result
        assert "**20** vs DC 15" in result  # 17+3=20
        assert "SUCCESS" in result

    @pytest.mark.asyncio
    async def test_unknown_attitude_returns_error(self):
        """Invalid npc_attitude returns a SYSTEM ERROR."""
        result = await perform_social_interaction(
            character_name="Guts",
            target_npc_name="Guard",
            request_description="Open the gate",
            npc_attitude="Neutral",  # Not a valid attitude
            request_risk="minor",
            config=MockConfig("/tmp/vault"),
        )
        assert "SYSTEM ERROR" in result
        assert "Unknown NPC attitude" in result

    @pytest.mark.asyncio
    async def test_unknown_risk_returns_error(self):
        """Invalid request_risk returns a SYSTEM ERROR."""
        result = await perform_social_interaction(
            character_name="Guts",
            target_npc_name="Guard",
            request_description="Open the gate",
            npc_attitude="Friendly",
            request_risk="dangerous",  # Not valid
            config=MockConfig("/tmp/vault"),
        )
        assert "SYSTEM ERROR" in result
        assert "Unknown risk level" in result

    @pytest.mark.asyncio
    async def test_friendly_attitude_rolls_dc_eight_plus_risk(self):
        """Friendly NPC: base DC 8 + risk modifier. Low risk = DC 13."""
        influencer = _make_creature("Guts", cha_mod=2)
        config = MockConfig("/tmp/vault")

        with patch("world_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = influencer
            with patch("world_tools.roll_dice", return_value=10):
                result = await perform_social_interaction(
                    character_name="Guts",
                    target_npc_name="Innkeeper",
                    request_description="Watch our horses while we sleep",
                    npc_attitude="Friendly",
                    request_risk="low",
                    config=config,
                )

        # Friendly DC 8 + Low +5 = DC 13. Roll 10+2=12 vs DC 13 → FAIL
        assert "DC 13" in result
        assert "**12** vs DC 13" in result
        assert "FAILURE" in result

    @pytest.mark.asyncio
    async def test_hostile_minor_risk_rolls(self):
        """REQ-SOC-002: Hostile + minor risk → rolls (DC 14)."""
        influencer = _make_creature("Guts", cha_mod=0)
        config = MockConfig("/tmp/vault")

        with patch("world_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = influencer
            with patch("world_tools.roll_dice", return_value=14):
                result = await perform_social_interaction(
                    character_name="Guts",
                    target_npc_name="Orc",
                    request_description="Stop sharpening your axe for a moment",
                    npc_attitude="Hostile",
                    request_risk="minor",
                    config=config,
                )

        assert "AUTO-FAILURE" not in result
        assert "DC 14" in result  # Hostile DC 14 + Minor 0
        assert "**14** vs DC 14" in result
        assert "SUCCESS" in result
