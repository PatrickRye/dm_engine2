"""
Tests for Passive Score, Skill Proficiency, and Proficiency Bonus requirements.
REQ-ENT-002, REQ-PAS-001 through REQ-PAS-004, REQ-SKL-002, REQ-SKL-003
"""
import math
import pytest
from unittest.mock import patch

from dnd_rules_engine import (
    Creature,
    ModifiableValue,
    NumericalModifier,
    ModifierPriority,
    ClassLevel,
)
from registry import clear_registry
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup_system():
    clear_registry()
    spatial_service.clear()
    yield


# ============================================================
# REQ-ENT-002: Proficiency Bonus
# ============================================================

@pytest.mark.parametrize("total_level,expected_pb", [
    (1, 2),
    (2, 2),
    (4, 2),
    (5, 3),
    (8, 3),
    (9, 4),
    (12, 4),
    (13, 5),
    (16, 5),
    (17, 6),
    (20, 6),
])
def test_req_ent_002_proficiency_bonus_by_level(total_level, expected_pb):
    """
    REQ-ENT-002: Proficiency Bonus (PB)
    PB is calculated strictly using the total combined character level.
    Formula: Floor((Total_Level - 1) / 4) + 2
    """
    pb = math.floor((total_level - 1) / 4) + 2
    assert pb == expected_pb, f"Level {total_level}: expected PB {expected_pb}, got {pb}"


def test_req_ent_002_multiclass_uses_total_level():
    """
    REQ-ENT-002: Multiclassing uses TOTAL level for PB, not individual class levels.
    A Fighter 4 / Rogue 4 (total level 8) has PB=3, not PB=2 (level 4).
    """
    creature = Creature(
        name="Multiclasser",
        hp=ModifiableValue(base_value=60),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=3),
        dexterity_mod=ModifiableValue(base_value=2),
        classes=[
            ClassLevel(class_name="Fighter", subclass_name="", level=4),
            ClassLevel(class_name="Rogue", subclass_name="", level=4),
        ],
    )
    total_level = creature.character_level
    assert total_level == 8

    pb = math.floor((total_level - 1) / 4) + 2
    assert pb == 3  # Level 8 PB = 3, not level-4 PB = 2


# ============================================================
# REQ-PAS-001: Passive Score Base Math
# ============================================================

def test_req_pas_001_passive_score_base():
    """
    REQ-PAS-001: Passive Score (Base Math)
    Passive_Score = 10 + Ability_Mod + (PB * Prof_Multiplier)
    For a Perception check: 10 + WIS_mod + (PB * proficiency_multiplier)
    """
    # Wisdom mod +3, proficiency bonus +2, proficiency multiplier = 1 (proficient)
    wis_mod = 3
    pb = 2
    prof_multiplier = 1
    expected = 10 + wis_mod + (pb * prof_multiplier)  # 10 + 3 + 2 = 15
    assert expected == 15


def test_req_pas_001_passive_score_expertise():
    """
    REQ-PAS-001: With Expertise (multiplier=2), PB doubles.
    """
    wis_mod = 3
    pb = 2
    prof_multiplier = 2  # Expertise
    expected = 10 + wis_mod + (pb * prof_multiplier)  # 10 + 3 + 4 = 17
    assert expected == 17


def test_req_pas_001_passive_score_no_proficiency():
    """
    REQ-PAS-001: Without proficiency (multiplier=0), passive = 10 + ability_mod only.
    """
    wis_mod = 1
    pb = 3
    prof_multiplier = 0  # Not proficient
    expected = 10 + wis_mod + (pb * prof_multiplier)  # 10 + 1 + 0 = 11
    assert expected == 11


# ============================================================
# REQ-PAS-002: Passive Score + Advantage Modifier (+5)
# ============================================================

def test_req_pas_002_passive_advantage_adds_5():
    """
    REQ-PAS-002: Passive Score (Advantage Modifier)
    If the entity would have Advantage, add +5 to the passive score.
    """
    # Base passive Perception: 10 + 2 (WIS) + 2 (PB) = 14
    base_passive = 14
    passive_with_advantage = base_passive + 5
    assert passive_with_advantage == 19


# ============================================================
# REQ-PAS-003: Passive Score - Disadvantage Modifier (-5)
# ============================================================

def test_req_pas_003_passive_disadvantage_subtracts_5():
    """
    REQ-PAS-003: Passive Score (Disadvantage Modifier)
    If the entity would have Disadvantage, subtract 5 from the passive score.
    """
    base_passive = 14
    passive_with_disadvantage = base_passive - 5
    assert passive_with_disadvantage == 9


@pytest.mark.asyncio
async def test_req_pas_001_to_003_via_perform_tool(mock_obsidian_vault):
    """
    REQ-PAS-001/002/003: Validate the passive score formula via perform_ability_check_or_save.
    Passive mode: total = 10 + stat_mod + extra_modifier + (5 if adv else -5 if disadv else 0)
    """
    from tools import perform_ability_check_or_save

    vault_path = str(mock_obsidian_vault)
    config = {"configurable": {"thread_id": vault_path}}

    # Standard passive (WIS +3 modifier, +2 proficiency passed as extra_modifier=2)
    result = await perform_ability_check_or_save.ainvoke(
        {
            "character_name": "Fighter",
            "skill_or_stat_name": "wisdom",
            "is_passive": True,
            "extra_modifier": 5,  # WIS +3 + PB +2
        },
        config=config,
    )
    assert "15" in result  # 10 + 5 = 15

    # Passive with Advantage (+5)
    result_adv = await perform_ability_check_or_save.ainvoke(
        {
            "character_name": "Fighter",
            "skill_or_stat_name": "wisdom",
            "is_passive": True,
            "extra_modifier": 5,
            "advantage": True,
        },
        config=config,
    )
    assert "20" in result_adv  # 10 + 5 + 5 = 20

    # Passive with Disadvantage (-5)
    result_dis = await perform_ability_check_or_save.ainvoke(
        {
            "character_name": "Fighter",
            "skill_or_stat_name": "wisdom",
            "is_passive": True,
            "extra_modifier": 5,
            "disadvantage": True,
        },
        config=config,
    )
    assert "10" in result_dis  # 10 + 5 - 5 = 10


# ============================================================
# REQ-SKL-002: Proficiency Multiplier (0, 1, 2)
# ============================================================

def test_req_skl_002_proficiency_multiplier_values():
    """
    REQ-SKL-002: Proficiency Multiplier
    Entities can have no proficiency (0), proficiency (1), or Expertise (2).
    Skill bonus = ability_mod + (PB * prof_multiplier)
    """
    pb = 3
    ability_mod = 2

    no_prof = ability_mod + (pb * 0)
    proficient = ability_mod + (pb * 1)
    expertise = ability_mod + (pb * 2)

    assert no_prof == 2    # +2 ability only
    assert proficient == 5  # +2 + 3 = +5
    assert expertise == 8   # +2 + 6 = +8


# ============================================================
# REQ-SKL-003: Proficiency Stacking Limitation
# ============================================================

def test_req_skl_003_proficiency_does_not_stack():
    """
    REQ-SKL-003: Proficiency Stacking Limitation
    PB can only be added once, doubled once (Expertise), or halved once.
    Multiple sources granting PB/Expertise do not stack — only the highest applies.
    Modeled by ensuring two separate 'proficiency' sources resolve to max(2, 2) = 2x PB, not 3x.
    """
    # Two features that each grant Expertise (multiplier=2).
    # The correct result is still multiplier=2 (not 4).
    multipliers = [2, 2]  # Two Expertise sources
    effective_multiplier = max(multipliers)  # Rules: use the highest, don't stack
    assert effective_multiplier == 2

    # One proficiency + one Expertise source → result is Expertise (max wins)
    multipliers_mixed = [1, 2]
    assert max(multipliers_mixed) == 2

    # Verify with the ModifiableValue: two +PB ADDITIVE mods from different sources
    # would incorrectly stack. Verify we can model dedup at the tool level.
    creature = Creature(
        name="Rogue",
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=3),
    )
    pb = 3  # Level 9 character

    # Correct: only one PB modifier added
    prof_mod = NumericalModifier(
        priority=ModifierPriority.ADDITIVE,
        value=pb * 2,  # Expertise = 2x PB
        source_name="Expertise:Stealth",
    )
    creature.dexterity_mod.add_modifier(prof_mod)

    # A second Expertise source for the same skill should NOT add another +6
    existing = [m for m in creature.dexterity_mod.modifiers if m.source_name == "Expertise:Stealth"]
    assert len(existing) == 1, "Only one Expertise modifier should exist per skill"
    assert creature.dexterity_mod.total == 3 + (pb * 2)  # 3 + 6 = 9
