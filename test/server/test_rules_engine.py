# test/server/test_rules_engine.py
"""Tests for REQ-ECO-001, REQ-BUI-001-010, REQ-INV-002 — rules_engine utilities."""
import pytest

from rules_engine import (
    CP, SP, EP, GP, PP,
    gold_to_cp, silver_to_cp, electrum_to_cp, pp_to_cp,
    cp_to_gold, cp_to_silver,
    parse_coin_string, format_cp,
    cr_to_xp, xp_to_cr,
    calc_encounter_xp, evaluate_encounter,
    calc_party_xp_budget, get_char_xp_threshold,
    distribute_xp, get_daily_xp_budget,
    max_push_drag_lift, max_carrying_capacity, carrying_status, carrying_speed_penalty,
    _CHAR_XP_THRESHOLDS,
)


class TestCurrencyConversion:
    """REQ-ECO-001: All currency stored as integer Copper Pieces (CP) internally."""

    def test_gold_to_cp(self):
        assert gold_to_cp(10) == 1000
        assert gold_to_cp(1) == 100
        assert gold_to_cp(0) == 0

    def test_silver_to_cp(self):
        assert silver_to_cp(5) == 50
        assert silver_to_cp(10) == 100
        assert silver_to_cp(0) == 0

    def test_electrum_to_cp(self):
        assert electrum_to_cp(2) == 100
        assert electrum_to_cp(1) == 50
        assert electrum_to_cp(0) == 0

    def test_pp_to_cp(self):
        assert pp_to_cp(1) == 1000
        assert pp_to_cp(5) == 5000
        assert pp_to_cp(0) == 0

    def test_cp_to_gold(self):
        assert cp_to_gold(250) == (2, 50)
        assert cp_to_gold(100) == (1, 0)
        assert cp_to_gold(0) == (0, 0)

    def test_cp_to_silver(self):
        assert cp_to_silver(55) == (5, 5)
        assert cp_to_silver(10) == (1, 0)

    def test_parse_coin_string_gp(self):
        assert parse_coin_string("5 gp") == 500
        assert parse_coin_string("12gp") == 1200

    def test_parse_coin_string_cp(self):
        assert parse_coin_string("100 cp") == 100
        assert parse_coin_string("50cp") == 50

    def test_parse_coin_string_mixed(self):
        assert parse_coin_string("5 gp, 12 sp, 100 cp") == 500 + 120 + 100
        assert parse_coin_string("2 pp, 3 gp, 5 ep") == 2000 + 300 + 250

    def test_parse_coin_string_case_insensitive(self):
        assert parse_coin_string("10 GP") == 1000
        assert parse_coin_string("5 GP, 3 SP") == 500 + 30

    def test_parse_coin_string_short_units(self):
        assert parse_coin_string("5 g") == 500   # gp shorthand
        assert parse_coin_string("5 s") == 50    # sp shorthand
        assert parse_coin_string("5 p") == 5000   # pp shorthand

    def test_parse_coin_string_empty(self):
        assert parse_coin_string("") == 0
        assert parse_coin_string("   ") == 0

    def test_parse_coin_string_invalid(self):
        # Invalid entries are silently skipped
        assert parse_coin_string("abc") == 0
        assert parse_coin_string("5 gp, garbage, 3 sp") == 500 + 30

    def test_format_cp(self):
        assert "5 gp" in format_cp(500)
        assert "2 pp" in format_cp(2000)
        assert "1 gp" in format_cp(100)  # 100 cp = 1 gp

    def test_format_cp_zero(self):
        result = format_cp(0)
        assert "cp" in result

    def test_format_cp_rounds_correctly(self):
        # 1 pp = 1000 cp = 10 gp
        result = format_cp(1000)
        assert "1 pp" in result or "10 gp" in result


class TestCRToXP:
    """REQ-BUI-001: CR to XP conversion per 2024 DMG Ch. 4."""

    def test_cr_zero(self):
        assert cr_to_xp(0) == 10

    def test_cr_fractional(self):
        assert cr_to_xp(1 / 8) == 25
        assert cr_to_xp(1 / 4) == 50
        assert cr_to_xp(1 / 2) == 100

    def test_cr_integer(self):
        assert cr_to_xp(1) == 200
        assert cr_to_xp(5) == 1800
        assert cr_to_xp(10) == 5900
        assert cr_to_xp(20) == 25000

    def test_cr_30(self):
        assert cr_to_xp(30) == 155000

    def test_cr_above_30_returns_zero(self):
        assert cr_to_xp(31) == 0

    def test_xp_to_cr_roundtrip(self):
        for cr_val, xp_val in {
            1 / 8: 25, 1 / 4: 50, 1 / 2: 100,
            1: 200, 5: 1800, 10: 5900, 20: 25000, 30: 155000,
        }.items():
            assert xp_to_cr(xp_val) == cr_val

    def test_xp_to_cr_unknown_returns_none(self):
        assert xp_to_cr(99999) is None


class TestPartyXPBudget:
    """REQ-BUI-003: Party XP budget by character level."""

    def test_easy_threshold_level_1(self):
        assert get_char_xp_threshold(1, "easy") == 25

    def test_deadly_threshold_level_5(self):
        assert get_char_xp_threshold(5, "deadly") == 1100

    def test_deadly_threshold_level_20(self):
        assert get_char_xp_threshold(20, "deadly") == 12700

    def test_unknown_level_returns_zero(self):
        assert get_char_xp_threshold(99, "easy") == 0
        assert get_char_xp_threshold(0, "easy") == 0

    def test_unknown_difficulty_returns_zero(self):
        assert get_char_xp_threshold(5, "impossible") == 0

    def test_calc_party_xp_budget_four_level_5_chars(self):
        # 4 level-5 characters for a medium encounter
        budget = calc_party_xp_budget([5, 5, 5, 5], "medium")
        assert budget == 500 * 4  # medium for level 5 = 500

    def test_calc_party_xp_budget_mixed_levels(self):
        budget = calc_party_xp_budget([1, 3, 5], "hard")
        # Level 1 hard = 75, level 3 hard = 225, level 5 hard = 750
        assert budget == 75 + 225 + 750


class TestEncounterEvaluation:
    """REQ-BUI-002/004: Encounter evaluation and difficulty."""

    def test_trivial_encounter(self):
        # 4 level-1 chars vs 1 CR 0 monster (XP=10)
        result = evaluate_encounter([0], [1, 1, 1, 1])
        assert result["difficulty"] == "Trivial"
        assert result["total_xp"] == 10

    def test_easy_encounter(self):
        # 4 level-5 chars: easy budget=1000, medium=2000
        # CR4 = 1100 XP — exceeds easy budget → Easy
        result = evaluate_encounter([4], [5, 5, 5, 5])
        assert result["total_xp"] == 1100
        assert result["difficulty"] == "Easy"

    def test_medium_encounter(self):
        # 4 level-5 chars: medium budget=2000, hard=3000
        # CR6 = 2300 XP — exceeds medium (2000) but not hard (3000) → Medium
        result = evaluate_encounter([6], [5, 5, 5, 5])
        assert result["difficulty"] == "Medium"

    def test_hard_encounter(self):
        # 4 level-5 chars: deadly budget=4400
        # Two CR5 = 3600 XP — exceeds hard (3000) but not deadly (4400) → Hard
        result = evaluate_encounter([5, 5], [5, 5, 5, 5])
        assert result["difficulty"] == "Hard"

    def test_deadly_encounter(self):
        # 4 level-5 chars, deadly budget = 4*1100=4400
        result = evaluate_encounter([10, 10], [5, 5, 5, 5])
        # CR10 * 2 = 5900*2 = 11800 > 4400 → Deadly
        assert result["difficulty"] == "Deadly"

    def test_lethality_warning(self):
        """REQ-BUI-006: Warn when monster CR is 3+ above average party level."""
        # 4 level-3 chars (avg=3) vs CR9 monster (CR 9 >= 3+3=6 → warning)
        result = evaluate_encounter([9], [3, 3, 3, 3])
        assert len(result["warnings"]) == 1
        assert "LETHALITY WARNING" in result["warnings"][0]

    def test_no_lethality_warning_normal_encounter(self):
        # CR3 vs level 3 party — no warning
        result = evaluate_encounter([3], [3, 3, 3, 3])
        assert result["warnings"] == []

    def test_empty_party_returns_deadly(self):
        """Empty party (0 XP budget) with monsters returns 'Deadly' since XP exceeds all budgets."""
        result = evaluate_encounter([1], [])  # 200 XP vs 0 budget = Deadly
        assert result["difficulty"] == "Deadly"

    def test_empty_monsters_returns_trivial(self):
        result = evaluate_encounter([], [5, 5, 5, 5])
        assert result["difficulty"] == "Trivial"
        assert result["total_xp"] == 0


class TestXPDistribution:
    """REQ-BUI-010: XP award distribution."""

    def test_distribute_xp_evenly(self):
        awards = distribute_xp(1000, 4)
        assert awards == [250, 250, 250, 250]

    def test_distribute_xp_remainder_discarded(self):
        # 1000 / 4 = 250 exactly, no remainder
        awards = distribute_xp(1001, 4)
        assert awards == [250, 250, 250, 250]  # floor division, remainder discarded

    def test_distribute_xp_zero_members(self):
        assert distribute_xp(1000, 0) == []

    def test_distribute_xp_one_member(self):
        assert distribute_xp(500, 1) == [500]


class TestDailyXPBudget:
    """REQ-BUI-005: Daily XP budget."""

    def test_daily_budget_level_5(self):
        assert get_daily_xp_budget([5, 5, 5, 5]) == 3500 * 4

    def test_daily_budget_level_20(self):
        assert get_daily_xp_budget([20]) == 40000

    def test_daily_budget_unknown_level_returns_zero(self):
        # Level 0 or unknown levels return 0
        assert get_daily_xp_budget([99]) == 0


class TestCarryingCapacity:
    """REQ-INV-002: Push/drag/lift and carrying capacity."""

    def test_max_push_drag_lift(self):
        assert max_push_drag_lift(10) == 300
        assert max_push_drag_lift(20) == 600
        assert max_push_drag_lift(0) == 0

    def test_max_carrying_capacity(self):
        assert max_carrying_capacity(10) == 150
        assert max_carrying_capacity(20) == 300
        assert max_carrying_capacity(0) == 0

    def test_carrying_status_normal(self):
        # Below Str×15 = normal
        assert carrying_status(100, 10) == "normal"
        assert carrying_status(150, 10) == "normal"

    def test_carrying_status_encumbered(self):
        # Between Str×15 and Str×30 = encumbered (speed -20)
        assert carrying_status(151, 10) == "encumbered"
        assert carrying_status(300, 10) == "encumbered"

    def test_carrying_status_heavily_encumbered(self):
        # Above Str×30 = heavily encumbered (speed -40)
        assert carrying_status(301, 10) == "heavily_encumbered"
        assert carrying_status(600, 10) == "heavily_encumbered"

    def test_carrying_speed_penalty(self):
        assert carrying_speed_penalty("normal") == 0
        assert carrying_speed_penalty("encumbered") == 20
        assert carrying_speed_penalty("heavily_encumbered") == 40
        assert carrying_speed_penalty("unknown") == 0  # safe default
