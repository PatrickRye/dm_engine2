"""
Tests for the Compendium Hydration System (compendium_hydrators.py).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from compendium_hydrators import (
    CompendiumMaterials,
    CompendiumHydrationReport,
    CreatureHydrationResult,
    LocationHydrationResult,
    FactionHydrationResult,
    NPCHydrationResult,
    MapHydrationResult,
    NarrativeHydrationResult,
    CompendiumItemsResult,
    _split_creature_blocks,
    _load_prompt,
    _hydrate_creatures,
    _hydrate_locations,
    _hydrate_factions,
    _hydrate_npcs,
    _hydrate_maps,
    _hydrate_narrative,
    _hydrate_items,
    run_compendium_hydration,
)


# ---------------------------------------------------------------------------
# TypedDict construction tests
# ---------------------------------------------------------------------------


class TestTypedDictSchemas:
    def test_compendium_materials_empty(self):
        mat = CompendiumMaterials()
        assert mat == {}

    def test_compendium_materials_partial(self):
        mat = CompendiumMaterials(creatures="## Goblin\nMedium humanoid...")
        assert mat["creatures"] == "## Goblin\nMedium humanoid..."
        assert "locations" not in mat

    def test_compendium_materials_all_fields(self):
        mat = CompendiumMaterials(
            creatures="## Goblin\nMedium humanoid...",
            locations="Saltmere is a coastal city...",
            factions="The Crimson Shield...",
            npcs="## Zara\nHalf-elf female...",
            maps="20x15 dungeon...",
            campaign_narrative="The party arrives...",
            session_prep_notes="The warlock will betray...",
            storylet_resolutions={"The Betrayal": "Zara reveals..."},
            items="### Fireball\n3rd-level evocation...",
        )
        assert len(mat) == 9

    def test_creature_hydration_result(self):
        r = CreatureHydrationResult(nodes_created=3, edges_created=5, entities_written=["a.md"], warnings=["x"])
        assert r["nodes_created"] == 3
        assert r["warnings"] == ["x"]

    def test_location_hydration_result(self):
        loc = LocationHydrationResult(nodes_created=1, edges_created=2, warnings=[])
        assert loc["nodes_created"] == 1

    def test_faction_hydration_result(self):
        fac = FactionHydrationResult(nodes_created=2, edges_created=4, warnings=[])
        assert fac["nodes_created"] == 2

    def test_npc_hydration_result(self):
        npc = NPCHydrationResult(nodes_created=1, edges_created=0, entities_written=[], warnings=[])
        assert npc["nodes_created"] == 1

    def test_map_hydration_result(self):
        mp = MapHydrationResult(nodes_created=0, edges_created=0, files_written=["map.json"], warnings=[])
        assert mp["files_written"] == ["map.json"]

    def test_narrative_hydration_result(self):
        nar = NarrativeHydrationResult(
            nodes_created=5, edges_created=3, storylets_created=2,
            effects_annotated=1, backup_storylets=0, three_clue_violations=0, warnings=[]
        )
        assert nar["storylets_created"] == 2

    def test_items_result(self):
        items = CompendiumItemsResult(entries_saved=["fireball.json"], warnings=[])
        assert items["entries_saved"] == ["fireball.json"]


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------


class TestSplitCreatureBlocks:
    def test_single_block(self):
        text = "## Goblin\nMedium humanoid (goblinoid), Neutral Evil\n**Armor Class** 15"
        blocks = _split_creature_blocks(text)
        assert len(blocks) == 1
        assert "Goblin" in blocks[0]

    def test_markdown_header_splits(self):
        text = "## Goblin\nMedium humanoid\n\n## Dragon\nHuge dragon\n"
        blocks = _split_creature_blocks(text)
        assert len(blocks) == 2
        assert "Goblin" in blocks[0]
        assert "Dragon" in blocks[1]

    def test_empty_input(self):
        assert _split_creature_blocks("") == []
        assert _split_creature_blocks("   \n\n  ") == []

    def test_double_blank_line_split(self):
        text = "## Goblin\n\nMedium\n\n\n## Dragon\n\nHuge"
        blocks = _split_creature_blocks(text)
        assert len(blocks) == 2


class TestLoadPrompt:
    def test_nonexistent_prompt_returns_empty(self):
        # Clear cache first if present
        _PROMPT_CACHE = {}
        result = _load_prompt("Non-Existent Prompt XYZ 123")
        assert result == ""


# ---------------------------------------------------------------------------
# Sub-hydrator empty-input tests (async — must be awaited)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSubHydratorsEmptyInput:
    async def test_hydrate_creatures_empty(self):
        mock_llm = MagicMock()
        result = await _hydrate_creatures("", "test_vault", mock_llm)
        assert result["nodes_created"] == 0
        assert result["entities_written"] == []

    async def test_hydrate_locations_empty(self):
        mock_llm = MagicMock()
        result = await _hydrate_locations("", "test_vault", mock_llm)
        assert result["nodes_created"] == 0

    async def test_hydrate_factions_empty(self):
        mock_llm = MagicMock()
        result = await _hydrate_factions("", "test_vault", mock_llm)
        assert result["nodes_created"] == 0

    async def test_hydrate_npcs_empty(self):
        mock_llm = MagicMock()
        result = await _hydrate_npcs("", "test_vault", mock_llm)
        assert result["nodes_created"] == 0

    async def test_hydrate_maps_empty(self):
        mock_llm = MagicMock()
        result = await _hydrate_maps("", "test_vault", mock_llm)
        assert result["nodes_created"] == 0

    async def test_hydrate_narrative_empty(self):
        mock_llm = MagicMock()
        result = await _hydrate_narrative("", "", {}, "test_vault", mock_llm)
        assert result["nodes_created"] == 0
        assert result["storylets_created"] == 0

    async def test_hydrate_items_empty(self):
        mock_llm = MagicMock()
        result = await _hydrate_items("", "test_vault", mock_llm)
        assert result["entries_saved"] == []


# ---------------------------------------------------------------------------
# Coordinator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCoordinator:
    async def test_empty_materials_runs_nothing(self):
        """When no materials are provided, no sub-hydrators run."""
        mat = CompendiumMaterials()
        mock_llm = MagicMock()
        report = await run_compendium_hydration(mat, "test_vault", mock_llm)
        assert report["creatures"] is None
        assert report["locations"] is None
        assert report["errors"] == []

    async def test_only_creatures(self):
        """Only the creatures field triggers the creatures sub-hydrator."""
        with patch("compendium_hydrators._hydrate_creatures", new_callable=AsyncMock) as mock_creatures:
            mock_creatures.return_value = CreatureHydrationResult(
                nodes_created=1, edges_created=0, entities_written=["goblin.md"], warnings=[]
            )
            mat = CompendiumMaterials(creatures="## Goblin\nMedium humanoid...")
            mock_llm = MagicMock()
            report = await run_compendium_hydration(mat, "test_vault", mock_llm)

            assert report["creatures"] is not None
            assert report["creatures"]["nodes_created"] == 1
            assert report["locations"] is None
            assert report["narrative"] is None

    async def test_multiple_sub_hydrators_parallel(self):
        """Multiple non-empty fields run in parallel via asyncio.gather."""
        with patch("compendium_hydrators._hydrate_creatures", new_callable=AsyncMock) as mc, \
             patch("compendium_hydrators._hydrate_locations", new_callable=AsyncMock) as ml, \
             patch("compendium_hydrators._hydrate_npcs", new_callable=AsyncMock) as mn:

            mc.return_value = CreatureHydrationResult(nodes_created=1, edges_created=0, entities_written=[], warnings=[])
            ml.return_value = LocationHydrationResult(nodes_created=2, edges_created=1, warnings=[])
            mn.return_value = NPCHydrationResult(nodes_created=3, edges_created=2, entities_written=[], warnings=[])

            mat = CompendiumMaterials(
                creatures="## Goblin",
                locations="Saltmere is a city.",
                npcs="## Zara the Broker",
            )
            mock_llm = MagicMock()
            report = await run_compendium_hydration(mat, "test_vault", mock_llm)

            assert report["creatures"]["nodes_created"] == 1
            assert report["locations"]["nodes_created"] == 2
            assert report["npcs"]["nodes_created"] == 3
            assert report["factions"] is None

    async def test_partial_failure_collected(self):
        """Errors in one sub-hydrator are collected but don't stop others."""
        with patch("compendium_hydrators._hydrate_creatures", new_callable=AsyncMock) as mc, \
             patch("compendium_hydrators._hydrate_locations", new_callable=AsyncMock) as ml:

            mc.return_value = CreatureHydrationResult(
                nodes_created=0, edges_created=0, entities_written=[], warnings=["parse error"]
            )
            ml.side_effect = RuntimeError("LLM failed")

            mat = CompendiumMaterials(creatures="## Goblin", locations="Saltmere")
            mock_llm = MagicMock()
            report = await run_compendium_hydration(mat, "test_vault", mock_llm)

            # Creatures completed with warning
            assert report["creatures"] is not None
            assert any("parse error" in str(w) for w in report["creatures"]["warnings"])
            # Locations raised exception
            assert any("LLM failed" in str(e) for e in report["errors"])

    async def test_narrative_runs_with_campaign_fields(self):
        """Narrative sub-hydrator triggers when campaign_narrative is set."""
        with patch("compendium_hydrators._hydrate_narrative", new_callable=AsyncMock) as mn:
            mn.return_value = NarrativeHydrationResult(
                nodes_created=5, edges_created=3, storylets_created=2,
                effects_annotated=1, backup_storylets=0, three_clue_violations=0, warnings=[]
            )

            mat = CompendiumMaterials(
                campaign_narrative="The party arrives at Saltmere...",
                session_prep_notes="The warlock will betray...",
            )
            mock_llm = MagicMock()
            report = await run_compendium_hydration(mat, "test_vault", mock_llm)

            assert report["narrative"] is not None
            assert report["narrative"]["storylets_created"] == 2

    async def test_items_runs(self):
        """Items sub-hydrator triggers when items field is set."""
        with patch("compendium_hydrators._hydrate_items", new_callable=AsyncMock) as mi:
            mi.return_value = CompendiumItemsResult(entries_saved=["fireball.json"], warnings=[])

            mat = CompendiumMaterials(items="### Fireball\n3rd-level evocation...")
            mock_llm = MagicMock()
            report = await run_compendium_hydration(mat, "test_vault", mock_llm)

            assert report["items"] is not None
            assert "fireball.json" in report["items"]["entries_saved"]

    async def test_all_seven_sub_hydrators(self):
        """All 7 sub-hydrators can be triggered at once."""
        with patch("compendium_hydrators._hydrate_creatures", new_callable=AsyncMock) as mc, \
             patch("compendium_hydrators._hydrate_locations", new_callable=AsyncMock) as ml, \
             patch("compendium_hydrators._hydrate_factions", new_callable=AsyncMock) as mf, \
             patch("compendium_hydrators._hydrate_npcs", new_callable=AsyncMock) as mn, \
             patch("compendium_hydrators._hydrate_maps", new_callable=AsyncMock) as mmp, \
             patch("compendium_hydrators._hydrate_narrative", new_callable=AsyncMock) as mnar, \
             patch("compendium_hydrators._hydrate_items", new_callable=AsyncMock) as mi:

            mc.return_value = CreatureHydrationResult(nodes_created=1, edges_created=0, entities_written=[], warnings=[])
            ml.return_value = LocationHydrationResult(nodes_created=1, edges_created=0, warnings=[])
            mf.return_value = FactionHydrationResult(nodes_created=1, edges_created=0, warnings=[])
            mn.return_value = NPCHydrationResult(nodes_created=1, edges_created=0, entities_written=[], warnings=[])
            mmp.return_value = MapHydrationResult(nodes_created=0, edges_created=0, files_written=[], warnings=[])
            mnar.return_value = NarrativeHydrationResult(
                nodes_created=0, edges_created=0, storylets_created=0,
                effects_annotated=0, backup_storylets=0, three_clue_violations=0, warnings=[]
            )
            mi.return_value = CompendiumItemsResult(entries_saved=[], warnings=[])

            mat = CompendiumMaterials(
                creatures="## Goblin",
                locations="Saltmere",
                factions="The Crimson Shield",
                npcs="## Zara",
                maps="20x15 dungeon",
                campaign_narrative="The party arrives",
                items="### Fireball",
            )
            mock_llm = MagicMock()
            report = await run_compendium_hydration(mat, "test_vault", mock_llm)

            assert report["creatures"] is not None
            assert report["locations"] is not None
            assert report["factions"] is not None
            assert report["npcs"] is not None
            assert report["maps"] is not None
            assert report["narrative"] is not None
            assert report["items"] is not None
            assert report["errors"] == []
