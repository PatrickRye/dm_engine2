# test/server/test_spatial_rules.py
"""Tests for REQ-GEO-001/002/003/004/005 — Entity footprint by size."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from dnd_rules_engine import Creature
from spatial_tools import get_entity_space, _SIZE_TO_SPACE


class MockConfig:
    def __init__(self, vault_path):
        self.configurable = {"thread_id": vault_path}


@pytest.fixture
def mock_creature():
    def _make(size: float):
        ent = MagicMock(spec=Creature)
        ent.entity_uuid = "test-uuid"
        ent.name = "Test Creature"
        ent.size = size
        return ent
    return _make


class TestGetEntitySpace:
    """REQ-GEO-001/002/003/004/005"""

    @pytest.mark.asyncio
    async def test_tiny_size_space(self, mock_creature):
        """REQ-GEO-001: Tiny (≤3ft) creature occupies 2.5×2.5 ft."""
        entity = mock_creature(2.5)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        assert "2.5" in result
        assert "1 five-foot square" in result

    @pytest.mark.asyncio
    async def test_small_medium_size_space(self, mock_creature):
        """REQ-GEO-002: Small/Medium (4-6ft) creature occupies 5×5 ft."""
        entity = mock_creature(5.0)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        assert "5.0" in result
        assert "1 five-foot square" in result

    @pytest.mark.asyncio
    async def test_large_size_space(self, mock_creature):
        """REQ-GEO-003: Large (7-11ft) creature occupies 10×10 ft (4 squares)."""
        entity = mock_creature(10.0)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        assert "10.0" in result
        assert "4 five-foot squares" in result

    @pytest.mark.asyncio
    async def test_huge_size_space(self, mock_creature):
        """REQ-GEO-004: Huge (12-16ft) creature occupies 15×15 ft (9 squares)."""
        entity = mock_creature(15.0)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        assert "15.0" in result
        assert "9 five-foot squares" in result

    @pytest.mark.asyncio
    async def test_gargantuan_size_space(self, mock_creature):
        """REQ-GEO-005: Gargantuan (17ft+) creature occupies 20×20 ft (16 squares)."""
        entity = mock_creature(20.0)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        assert "20.0" in result
        assert "16 five-foot squares" in result

    @pytest.mark.asyncio
    async def test_entity_not_found(self):
        """Unknown entity returns error message."""
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            result = await get_entity_space.ainvoke({"entity_name": "Unknown", "config": config})

        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_size_boundary_tiny_vs_small(self, mock_creature):
        """Size exactly at 3.0ft boundary falls in Tiny range (0, 3.0] → 2.5×2.5."""
        entity = mock_creature(3.0)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        # size=3.0 falls in (0, 3.0] → Tiny → 2.5×2.5
        assert "2.5" in result
        assert "1 five-foot square" in result

    @pytest.mark.asyncio
    async def test_size_boundary_large_vs_huge(self, mock_creature):
        """Size exactly at 11.0ft boundary is Large."""
        entity = mock_creature(11.0)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        # size=11.0 falls in (6.0, 11.0] → Large → 10×10
        assert "10.0" in result

    @pytest.mark.asyncio
    async def test_size_just_over_boundary_huge(self, mock_creature):
        """Size just over Huge boundary (11.0+) is Huge."""
        entity = mock_creature(11.1)
        config = MockConfig("/tmp/vault")

        with patch("spatial_tools._get_entity_by_name", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = entity
            result = await get_entity_space.ainvoke({"entity_name": "Test Creature", "config": config})

        # size=11.1 falls in (11.0, 16.0] → Huge → 15×15
        assert "15.0" in result


class TestSizeToSpaceLookup:
    """Unit tests for the _SIZE_TO_SPACE lookup table."""

    def test_tiny_range(self):
        """0 < size <= 3.0 → Tiny 2.5×2.5"""
        dims = _SIZE_TO_SPACE[(0, 3.0)]
        assert dims == (2.5, 2.5)

    def test_small_medium_range(self):
        """3.0 < size <= 6.0 → Small/Medium 5×5"""
        dims = _SIZE_TO_SPACE[(3.0, 6.0)]
        assert dims == (5.0, 5.0)

    def test_large_range(self):
        """6.0 < size <= 11.0 → Large 10×10"""
        dims = _SIZE_TO_SPACE[(6.0, 11.0)]
        assert dims == (10.0, 10.0)

    def test_huge_range(self):
        """11.0 < size <= 16.0 → Huge 15×15"""
        dims = _SIZE_TO_SPACE[(11.0, 16.0)]
        assert dims == (15.0, 15.0)

    def test_gargantuan_range(self):
        """size > 16.0 → Gargantuan 20×20"""
        dims = _SIZE_TO_SPACE[(16.0, float("inf"))]
        assert dims == (20.0, 20.0)
