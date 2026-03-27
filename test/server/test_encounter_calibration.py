"""
Tests for generate_or_calibrate_encounter (DEGA encounter generation).
"""
import pytest
import math
from unittest.mock import patch, MagicMock
import uuid

from world_tools import (
    _select_archetype,
    _distribute_xp_to_roles,
    _best_cr_for_xp,
    _apply_template,
    _build_env_requirements,
    _build_tactical_demeanor,
    _compute_party_levels_from_kg,
    _guess_damage_type_for_role,
    _DAMAGE_TYPE_ATMOSPHERE,
    generate_or_calibrate_encounter,
)
from knowledge_graph import KnowledgeGraph, GraphNodeType, KnowledgeGraphNode
from state import ClassLevel
from dnd_rules_engine import Creature


class TestDEGAHelpers:
    """Unit tests for DEGA helper functions (no I/O, fully deterministic)."""

    def test_archetype_selection_e1_apex_heavy(self):
        """DEGA §2: E=1 should select Apex ~70% of the time."""
        from collections import Counter
        trials = 10_000
        archs = [_select_archetype(1) for _ in range(trials)]
        dist = {k: v / trials for k, v in Counter(archs).items()}
        assert dist["apex"] == pytest.approx(0.70, abs=0.03)
        assert dist["phalanx"] < 0.20

    def test_archetype_selection_e2_ambush_phalanx(self):
        """DEGA §2: E=2 should select Ambush ~40% and Phalanx ~30%."""
        from collections import Counter
        trials = 10_000
        archs = [_select_archetype(2) for _ in range(trials)]
        dist = {k: v / trials for k, v in Counter(archs).items()}
        assert dist["ambush"] == pytest.approx(0.40, abs=0.03)
        assert dist["phalanx"] == pytest.approx(0.30, abs=0.03)

    def test_archetype_selection_e3_swarm_heavy(self):
        """DEGA §2: E≥3 should select Swarm ~40%."""
        from collections import Counter
        trials = 10_000
        archs = [_select_archetype(3) for _ in range(trials)]
        dist = {k: v / trials for k, v in Counter(archs).items()}
        assert dist["swarm"] == pytest.approx(0.40, abs=0.03)

    def test_distribute_xp_to_roles_phalanx(self):
        """DEGA §2: Phalanx splits 40/40/20 across tank/artillerist/minion."""
        role_xp = _distribute_xp_to_roles("phalanx", 1000)
        assert role_xp["tank"] == 400
        assert role_xp["artillerist"] == 400
        assert role_xp["minion"] == 200

    def test_distribute_xp_to_roles_apex(self):
        """DEGA §2: Apex splits 70/15/15 across elite/minion/controller."""
        role_xp = _distribute_xp_to_roles("apex", 1000)
        assert role_xp["elite"] == 700
        assert role_xp["minion"] == 150
        assert role_xp["controller"] == 150

    def test_distribute_xp_to_roles_swarm(self):
        """DEGA §2: Swarm splits 30/60/10 across brute/minion/support."""
        role_xp = _distribute_xp_to_roles("swarm", 1000)
        assert role_xp["brute"] == 300
        assert role_xp["minion"] == 600
        assert role_xp["support"] == 100

    def test_distribute_xp_to_roles_ambush(self):
        """DEGA §2: Ambush splits 50/30/20 across lurker/controller/skirmisher."""
        role_xp = _distribute_xp_to_roles("ambush", 1000)
        assert role_xp["lurker"] == 500
        assert role_xp["controller"] == 300
        assert role_xp["skirmisher"] == 200

    def test_best_cr_for_xp_exact_match(self):
        """_best_cr_for_xp returns the highest CR whose XP ≤ xp_target."""
        assert _best_cr_for_xp(1100, 10) == 4.0
        assert _best_cr_for_xp(1500, 10) == 4.0
        assert _best_cr_for_xp(1800, 10) == 5.0

    def test_best_cr_for_xp_respects_cr_max(self):
        """CR must not exceed cr_max even if XP budget allows more."""
        assert _best_cr_for_xp(10000, 5) == 5.0
        assert _best_cr_for_xp(10000, 8) == 8.0

    def test_best_cr_for_xp_returns_zero_when_nothing_fits(self):
        """Returns 0.0 when no CR fits the budget."""
        assert _best_cr_for_xp(5, 1) == 0.0

    def test_apply_template_elite_cr_plus_one(self):
        """Elite template: CR +1."""
        new_cr, label, desc = _apply_template(2.0, "elite", 15)
        assert new_cr == 3.0
        assert "Advanced" in label

    def test_apply_template_brute_cr_plus_one(self):
        """Brute template: CR +1."""
        new_cr, label, desc = _apply_template(2.0, "brute", 15)
        assert new_cr == 3.0
        assert "Brute" in label

    def test_apply_template_artillerist_cr_unchanged(self):
        """Artillerist template: CR unchanged."""
        new_cr, label, desc = _apply_template(2.0, "artillerist", 15)
        assert new_cr == 2.0
        assert "Artillerist" in label

    def test_apply_template_controller_cr_unchanged(self):
        """Controller template: CR unchanged."""
        new_cr, label, desc = _apply_template(2.0, "controller", 15)
        assert new_cr == 2.0
        assert "Controller" in label

    def test_apply_template_lurker_cr_unchanged(self):
        """Lurker template: CR unchanged."""
        new_cr, label, desc = _apply_template(2.0, "lurker", 15)
        assert new_cr == 2.0
        assert "Lurker" in label

    def test_apply_template_support_cr_unchanged(self):
        """Support template: CR unchanged."""
        new_cr, label, desc = _apply_template(2.0, "support", 15)
        assert new_cr == 2.0
        assert "Support" in label

    def test_apply_template_tank_cr_plus_one(self):
        """Tank template: CR +1."""
        new_cr, label, desc = _apply_template(2.0, "tank", 15)
        assert new_cr == 3.0
        assert "Tank" in label

    def test_apply_template_respects_cr_max(self):
        """Template application respects CR ceiling."""
        new_cr, label, desc = _apply_template(29.0, "elite", 30)
        assert new_cr == 30.0

    def test_build_env_requirements_artillerist_requires_cover(self):
        """Artillerist presence → cover_elements and LOS in output."""
        reqs = _build_env_requirements({"artillerist": 2})
        assert "cover_elements" in reqs
        assert reqs["cover_elements"].count("half_cover") == 3
        assert "total_cover" in reqs["cover_elements"]
        assert "line_of_sight" in reqs

    def test_build_env_requirements_lurker_requires_obscurment(self):
        """Lurker presence → heavy obscurement and verticality."""
        reqs = _build_env_requirements({"lurker": 1})
        assert "obscurement" in reqs
        assert "verticality" in reqs

    def test_build_env_requirements_skirmisher_requires_arena(self):
        """Skirmisher presence → arena_size and difficult_terrain."""
        reqs = _build_env_requirements({"skirmisher": 2})
        assert reqs.get("arena_size") == "minimum 60×60 ft"
        assert "difficult_terrain" in reqs

    def test_build_env_requirements_controller_requires_chokepoints(self):
        """Controller presence → choke_points."""
        reqs = _build_env_requirements({"controller": 1})
        assert reqs.get("choke_points") == "required (narrow corridors, doorways, rope bridges)"

    def test_build_env_requirements_brute_requires_open_ground(self):
        """Brute presence → open_ground."""
        reqs = _build_env_requirements({"brute": 1})
        assert reqs.get("open_ground") == "open lanes for charge approach"

    def test_build_env_requirements_empty_when_no_roles(self):
        """No roles → empty requirements dict."""
        reqs = _build_env_requirements({})
        assert reqs == {}

    def test_build_env_requirements_multiple_roles(self):
        """Multiple roles → all relevant requirements merged."""
        reqs = _build_env_requirements({"artillerist": 1, "lurker": 1, "controller": 1})
        assert "cover_elements" in reqs
        assert "obscurement" in reqs
        assert "choke_points" in reqs

    def test_damage_type_atmosphere_all_keys_defined(self):
        """Every damage type in _DAMAGE_TYPE_ATMOSPHERE has 4-tuple with label/atm/combat/interaction."""
        for key, val in _DAMAGE_TYPE_ATMOSPHERE.items():
            assert len(val) == 4, f"{key} atmosphere tuple must have 4 elements: {val}"
            label, atmosphere, combat_voice, interaction_tone = val
            assert label and atmosphere and combat_voice and interaction_tone

    def test_guess_damage_type_for_role_returns_list(self):
        """_guess_damage_type_for_role returns a non-empty list of damage type keys."""
        for role in ["elite", "brute", "artillerist", "controller", "lurker",
                     "skirmisher", "support", "tank", "minion", "solo"]:
            dts = _guess_damage_type_for_role(role, 5.0)
            assert isinstance(dts, list)
            assert len(dts) >= 1
            assert all(dt in _DAMAGE_TYPE_ATMOSPHERE for dt in dts)

    def test_build_tactical_demeanor_emits_all_sections(self):
        """TACTICAL DEMEANOR block includes atmospheric, combat voice, and interaction tone."""
        monster_list = [
            {"name": "Chuul", "cr": 5.0, "role": "lurker",
             "damage_types": ["piercing", "poison"]},
        ]
        block = _build_tactical_demeanor(monster_list)
        assert "TACTICAL DEMEANOR" in block
        assert "Chuul" in block
        assert "ATMOSPHERE:" in block
        assert "COMBAT VOICE:" in block
        assert "INTERACTION TONE:" in block
        assert "PIERCING" in block
        assert "POISON" in block

    def test_build_tactical_demeanor_unknown_damage_types_graceful(self):
        """Unknown damage type keys do not crash _build_tactical_demeanor."""
        monster_list = [
            {"name": "Weird Blob", "cr": 1.0, "role": "standard",
             "damage_types": ["radiant", "force"]},
        ]
        block = _build_tactical_demeanor(monster_list)
        assert "TACTICAL DEMEANOR" in block
        assert "Weird Blob" in block
        assert "FORCE" in block


class TestComputePartyLevelsFromKG:
    """Tests for KG-based party level computation with map/distance filtering."""

    def _make_kg_node(self, name: str, map_id: str | None = None, is_remote: bool = False) -> KnowledgeGraphNode:
        attrs = {}
        if map_id is not None:
            attrs["map_id"] = map_id
        if is_remote:
            attrs["is_remote"] = True
        return KnowledgeGraphNode(
            node_uuid=uuid.uuid4(),
            node_type=GraphNodeType.NPC,
            name=name,
            attributes=attrs,
        )

    def _fake_creature(
        self,
        name: str,
        levels: list[int],
        *,
        summoned_by_uuid: uuid.UUID | None = None,
        vault_path: str = "default",
    ) -> Creature:
        """Create a real Creature via model_construct (bypasses validator, no registry hit)."""
        from dnd_rules_engine import ModifiableValue
        fields = {
            "entity_uuid": uuid.uuid4(),
            "vault_path": vault_path,
            "name": name,
            "classes": [ClassLevel(class_name=f"Class{i}", level=l) for i, l in enumerate(levels)],
            "summoned_by_uuid": summoned_by_uuid,
            "hp": ModifiableValue(base_value=10),
            "ac": ModifiableValue(base_value=10),
        }
        return Creature.model_construct(**fields)

    def test_excludes_summoned_entities(self):
        """REQ-BUI-007: Summoned creatures (companions, summoned) are excluded."""
        kg = KnowledgeGraph()
        spirit = self._fake_creature("Spirit", [3], summoned_by_uuid=uuid.uuid4())
        with patch("world_tools.get_all_entities", return_value={spirit.entity_uuid: spirit}):
            levels = _compute_party_levels_from_kg(kg, None, "default")
            assert levels == []

    def test_excludes_remote_entities_via_kg_attribute(self):
        """REQ-BUI-007: Entities with is_remote=True on KG node are excluded."""
        kg = KnowledgeGraph()
        kg.add_node(self._make_kg_node("Spirit", is_remote=True))
        spirit = self._fake_creature("Spirit", [3])
        with patch("world_tools.get_all_entities", return_value={spirit.entity_uuid: spirit}):
            levels = _compute_party_levels_from_kg(kg, None, "default")
            assert levels == []

    def test_excludes_mismatched_map_id(self):
        """REQ-BUI-007: Entities on different maps (map_id mismatch) are excluded."""
        kg = KnowledgeGraph()
        kg.add_node(self._make_kg_node("Mercenary", map_id="map_A"))
        mercenary = self._fake_creature("Mercenary", [5])
        with patch("world_tools.get_all_entities", return_value={mercenary.entity_uuid: mercenary}):
            # Mercenary is on map_A, query for map_B → excluded
            levels = _compute_party_levels_from_kg(kg, "map_B", "default")
            assert levels == []
            # Mercenary is on map_A, query for map_A → included
            levels = _compute_party_levels_from_kg(kg, "map_A", "default")
            assert levels == [5]

    def test_includes_colocated_entities(self):
        """REQ-BUI-007: Entities on same map are included."""
        kg = KnowledgeGraph()
        kg.add_node(self._make_kg_node("Ally NPC", map_id="map_A"))
        ally = self._fake_creature("Ally NPC", [4])
        with patch("world_tools.get_all_entities", return_value={ally.entity_uuid: ally}):
            levels = _compute_party_levels_from_kg(kg, "map_A", "default")
            assert levels == [4]

    def test_sums_multi_class_levels(self):
        """Character level = sum of all class levels."""
        kg = KnowledgeGraph()
        kg.add_node(self._make_kg_node("Multiclass Hero"))
        hero = self._fake_creature("Multiclass Hero", [4, 3])
        with patch("world_tools.get_all_entities", return_value={hero.entity_uuid: hero}):
            levels = _compute_party_levels_from_kg(kg, None, "default")
            assert levels == [7]


class TestGenerateOrCalibrateEncounterTool:
    """Integration tests for the full tool (mocking I/O)."""

    @pytest.fixture
    def mock_config(self):
        from langchain_core.runnables import RunnableConfig
        return RunnableConfig(configurable={"vault_path": "test_encounter_vault"})

    @pytest.fixture
    def tool_fn(self):
        """Access the raw async function behind the @tool decorator."""
        return generate_or_calibrate_encounter.coroutine

    @pytest.mark.asyncio
    async def test_generate_mode_returns_mechanical_truth(self, mock_config, tool_fn):
        """Output must start with 'MECHANICAL TRUTH:'."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="generate",
            preplanned_monsters=None,
            location_tags=None,
            encounters_today=0,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        assert result.startswith("MECHANICAL TRUTH:")
        assert "Party:" in result
        assert "XP_enc=" in result
        assert "Archetype:" in result

    @pytest.mark.asyncio
    async def test_generate_mode_e1_escalates_to_deadly(self, mock_config, tool_fn):
        """REQ-BUI-008: E≤2 → effective difficulty = deadly regardless of target."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="generate",
            preplanned_monsters=None,
            location_tags=None,
            encounters_today=0,
            target_difficulty="easy",
            current_map_id=None,
            config=mock_config,
        )
        assert "Effective difficulty: deadly" in result

    @pytest.mark.asyncio
    async def test_calibrate_mode_returns_mechanical_truth(self, mock_config, tool_fn):
        """Calibrate mode output must also start with 'MECHANICAL TRUTH:'."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="calibrate",
            preplanned_monsters=[
                {"name": "Orc", "cr": 0.5, "role_hint": "brute"},
                {"name": "Orc", "cr": 0.5, "role_hint": "brute"},
            ],
            location_tags=None,
            encounters_today=2,
            target_difficulty="hard",
            current_map_id=None,
            config=mock_config,
        )
        assert result.startswith("MECHANICAL TRUTH:")
        assert "gap=" in result

    @pytest.mark.asyncio
    async def test_calibrate_mode_no_preplanned_returns_error(self, mock_config, tool_fn):
        """Missing preplanned_monsters in calibrate mode → SYSTEM ERROR."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="calibrate",
            preplanned_monsters=None,
            location_tags=None,
            encounters_today=0,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        assert result.startswith("SYSTEM ERROR: calibrate mode requires preplanned_monsters")

    @pytest.mark.asyncio
    async def test_empty_party_levels_returns_error(self, mock_config, tool_fn):
        """No party members found and no override → SYSTEM ERROR."""
        with patch("world_tools._compute_party_levels_from_kg", return_value=[]):
            result = await tool_fn(
                party_levels=None,
                mode="generate",
                preplanned_monsters=None,
                location_tags=None,
                encounters_today=0,
                target_difficulty="medium",
                current_map_id=None,
                config=mock_config,
            )
            assert result.startswith("SYSTEM ERROR: No party members found")

    @pytest.mark.asyncio
    async def test_xp_enc_is_daily_budget_for_single_encounter(self, mock_config, tool_fn):
        """REQ-BUI-007: E=1 → XP_enc = full daily XP budget (apex fight)."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="generate",
            preplanned_monsters=None,
            location_tags=None,
            encounters_today=0,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        import re
        m = re.search(r"XP_enc=(\d+)", result)
        assert m, f"XP_enc not found in output: {result}"
        xp_enc = int(m.group(1))
        daily_match = re.search(r"daily=(\d+)", result)
        assert daily_match and xp_enc == int(daily_match.group(1)), \
            f"E=1 encounter should use full daily budget; got XP_enc={xp_enc}"

    @pytest.mark.asyncio
    async def test_cr_max_derived_from_apl(self, mock_config, tool_fn):
        """REQ-BUI-007: CR_max = L + ceil(L/2), capped at 30."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="generate",
            preplanned_monsters=None,
            location_tags=None,
            encounters_today=0,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        import re
        m = re.search(r"CR_max=(\d+)", result)
        assert m, f"CR_max not found in output: {result}"
        cr_max = int(m.group(1))
        apl_match = re.search(r"APL (\d+)", result)
        assert apl_match, f"APL not found: {result}"
        apl = int(apl_match.group(1))
        assert cr_max == apl + math.ceil(apl / 2), \
            f"CR_max={cr_max} should equal APL({apl}) + ceil(APL/2)"

    @pytest.mark.asyncio
    async def test_cr_max_capped_at_30(self, mock_config, tool_fn):
        """CR_max must never exceed 30."""
        result = await tool_fn(
            party_levels=[20] * 4,
            mode="generate",
            preplanned_monsters=None,
            location_tags=None,
            encounters_today=0,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        import re
        m = re.search(r"CR_max=(\d+)", result)
        assert m, f"CR_max not found: {result}"
        assert int(m.group(1)) == 30

    @pytest.mark.asyncio
    async def test_calibrate_upgrades_when_underbudget(self, mock_config, tool_fn):
        """REQ-BUI-009: XP gap > 0 → Elite template applied to upgrade pre-planned."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="calibrate",
            preplanned_monsters=[{"name": "Bandit", "cr": 0.125}],
            location_tags=None,
            encounters_today=1,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        assert "upgraded to" in result or "upgraded" in result

    @pytest.mark.asyncio
    async def test_calibrate_env_requirements_included(self, mock_config, tool_fn):
        """DEGA §4: Output includes environmental requirements for assigned roles."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="calibrate",
            preplanned_monsters=[
                {"name": "Guard", "cr": 0.125, "role_hint": "artillerist"},
                {"name": "Guard", "cr": 0.125, "role_hint": "artillerist"},
                {"name": "Guard", "cr": 0.125, "role_hint": "artillerist"},
                {"name": "Guard", "cr": 0.125, "role_hint": "artillerist"},
            ],
            location_tags=None,
            encounters_today=2,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        assert "Environmental requirements:" in result
        assert "artillerist" in result.lower()

    @pytest.mark.asyncio
    async def test_generate_env_requirements_artillerist_present(self, mock_config, tool_fn):
        """DEGA §4: Artillerist role → cover_elements in generated encounter."""
        with patch("world_tools.get_knowledge_graph", return_value=KnowledgeGraph()), \
             patch("world_tools.get_all_entities", return_value={}):
            result = await tool_fn(
                party_levels=[5, 5, 4, 4],
                mode="generate",
                preplanned_monsters=None,
                location_tags=["dungeon"],
                encounters_today=2,
                target_difficulty="medium",
                current_map_id=None,
                config=mock_config,
            )
            assert "Environmental requirements:" in result

    @pytest.mark.asyncio
    async def test_tactical_demeanor_in_tool_output_generate(self, mock_config, tool_fn):
        """MECHANICAL TRUTH output in generate mode includes TACTICAL DEMEANOR block."""
        with patch("world_tools.get_knowledge_graph", return_value=KnowledgeGraph()), \
             patch("world_tools.get_all_entities", return_value={}):
            result = await tool_fn(
                party_levels=[5, 5, 4, 4],
                mode="generate",
                preplanned_monsters=None,
                location_tags=None,
                encounters_today=2,
                target_difficulty="medium",
                current_map_id=None,
                config=mock_config,
            )
        assert "TACTICAL DEMEANOR:" in result
        assert "ATMOSPHERE:" in result
        assert "COMBAT VOICE:" in result
        assert "INTERACTION TONE:" in result

    @pytest.mark.asyncio
    async def test_tactical_demeanor_in_tool_output_calibrate(self, mock_config, tool_fn):
        """MECHANICAL TRUTH output in calibrate mode includes TACTICAL DEMEANOR block."""
        result = await tool_fn(
            party_levels=[5, 5, 4, 4],
            mode="calibrate",
            preplanned_monsters=[{"name": "Orc Warchief", "cr": 4.0, "role_hint": "brute"}],
            location_tags=None,
            encounters_today=1,
            target_difficulty="medium",
            current_map_id=None,
            config=mock_config,
        )
        assert "TACTICAL DEMEANOR:" in result
        assert "ATMOSPHERE:" in result
