# server/rules_engine.py
"""Self-contained D&D 5e rules utilities: currency normalization, encounter building, carrying capacity."""
from __future__ import annotations

# =============================================================================
# REQ-ECO-001: Currency Normalization
# =============================================================================
# All currency stored as integer Copper Pieces (CP) internally to avoid float errors.
# Conversion rates: 1 SP = 10 CP, 1 EP = 50 CP, 1 GP = 100 CP, 1 PP = 1000 CP

CP = 1
SP = 10
EP = 50
GP = 100
PP = 1000


def gold_to_cp(gold: int) -> int:
    """REQ-ECO-001: Convert gold pieces to copper pieces."""
    return gold * GP


def silver_to_cp(silver: int) -> int:
    """Convert silver pieces to copper pieces."""
    return silver * SP


def electrum_to_cp(electrum: int) -> int:
    """Convert electrum pieces to copper pieces."""
    return electrum * EP


def pp_to_cp(platinum: int) -> int:
    """Convert platinum pieces to copper pieces."""
    return platinum * PP


def cp_to_gold(cp: int) -> tuple[int, int]:
    """Convert CP to gold and remaining CP. Returns (gold, remaining_cp)."""
    gold = cp // GP
    remaining = cp % GP
    return gold, remaining


def cp_to_silver(cp: int) -> tuple[int, int]:
    """Convert CP to silver and remaining CP. Returns (silver, remaining_cp)."""
    silver = cp // SP
    remaining = cp % SP
    return silver, remaining


def parse_coin_string(coin_str: str) -> int:
    """Parse a coin string like '5 gp' or '12gp' or '100 cp' into total CP.

    Supports: cp, sp, ep, gp, pp (case-insensitive, with or without space).
    Multiple entries can be comma-separated: '5 gp, 12 sp, 100 cp'.
    """
    total_cp = 0
    # Normalize: lowercase and replace comma separators
    coin_str = coin_str.lower().replace(",", " ").replace("  ", " ")
    parts = coin_str.strip().split()

    import re

    i = 0
    while i < len(parts):
        part = parts[i].strip()
        if not part:
            i += 1
            continue

        m = re.match(r"^(\d+)\s*([a-z]+)$", part)
        if m:
            # Direct match: "5gp" or "5 gp"
            amount = int(m.group(1))
            unit = m.group(2)
        elif re.match(r"^\d+$", part) and i + 1 < len(parts):
            # Standalone number followed by a unit: "5" "gp" → combine
            amount = int(part)
            i += 1
            unit = parts[i].strip() if i < len(parts) else ""
            if not re.match(r"^[a-z]+$", unit):
                # Next token isn't a unit — skip
                i -= 1
                amount = 0
                unit = ""
        else:
            amount = 0
            unit = ""

        if unit in ("cp", "c"):
            total_cp += amount * CP
        elif unit in ("sp", "s"):
            total_cp += amount * SP
        elif unit in ("ep", "e"):
            total_cp += amount * EP
        elif unit in ("gp", "g"):
            total_cp += amount * GP
        elif unit in ("pp", "p"):
            total_cp += amount * PP

        i += 1

    return total_cp


def format_cp(cp: int) -> str:
    """Format a CP value as a human-readable coin string: '12 gp, 5 sp, 3 cp'."""
    if cp == 0:
        return "0 cp"

    pp_val, remainder = divmod(cp, PP)
    gp_val, remainder = divmod(remainder, GP)
    sp_val, ep_val = divmod(remainder, SP)

    parts = []
    if pp_val:
        parts.append(f"{pp_val} pp")
    if gp_val:
        parts.append(f"{gp_val} gp")
    if sp_val:
        parts.append(f"{sp_val} sp")
    if ep_val:
        parts.append(f"{ep_val} ep")
    if not parts:
        parts.append("0 cp")
    return ", ".join(parts)


# =============================================================================
# REQ-BUI-001: CR to XP Conversion (2024 DMG)
# =============================================================================
# Per the 2024 DMG Ch. 4 Encounter Difficulty. XP values by Challenge Rating.
_CR_TO_XP = {
    0: 10,
    1 / 8: 25,
    1 / 4: 50,
    1 / 2: 100,
    1: 200,
    2: 450,
    3: 700,
    4: 1100,
    5: 1800,
    6: 2300,
    7: 2900,
    8: 3900,
    9: 5000,
    10: 5900,
    11: 7200,
    12: 8400,
    13: 10000,
    14: 11500,
    15: 13000,
    16: 15000,
    17: 18000,
    18: 20000,
    19: 22000,
    20: 25000,
    21: 33000,
    22: 41000,
    23: 50000,
    24: 62000,
    25: 75000,
    26: 90000,
    27: 105000,
    28: 120000,
    29: 135000,
    30: 155000,
}


def cr_to_xp(cr: float) -> int:
    """REQ-BUI-001: Convert a monster's Challenge Rating to XP value.

    Supports integer CR (0-30) and fractional CR (1/8, 1/4, 1/2).
    Returns 0 for CR values above 30.
    """
    return _CR_TO_XP.get(cr, 0)


def xp_to_cr(xp: int) -> float | None:
    """Inverse lookup: given XP, return the CR that has that XP value, or None."""
    for cr_val, xp_val in _CR_TO_XP.items():
        if xp_val == xp:
            return cr_val
    return None


# =============================================================================
# REQ-BUI-003: Party XP Budget by Character Level (2024 DMG)
# =============================================================================
# Thresholds for individual character XP to meet a difficulty, by level.
# Format: {level: {difficulty: xp_threshold}}
# Difficulty: "easy", "medium", "hard", "deadly"

_CHAR_XP_THRESHOLDS: dict[int, dict[str, int]] = {
    1:  {"easy": 25, "medium": 50, "hard": 75, "deadly": 100},
    2:  {"easy": 50, "medium": 100, "hard": 150, "deadly": 200},
    3:  {"easy": 75, "medium": 150, "hard": 225, "deadly": 400},
    4:  {"easy": 125, "medium": 250, "hard": 375, "deadly": 500},
    5:  {"easy": 250, "medium": 500, "hard": 750, "deadly": 1100},
    6:  {"easy": 300, "medium": 600, "hard": 900, "deadly": 1400},
    7:  {"easy": 350, "medium": 750, "hard": 1100, "deadly": 1700},
    8:  {"easy": 450, "medium": 900, "hard": 1400, "deadly": 2100},
    9:  {"easy": 550, "medium": 1100, "hard": 1600, "deadly": 2400},
    10: {"easy": 600, "medium": 1200, "hard": 1900, "deadly": 2800},
    11: {"easy": 800, "medium": 1600, "hard": 2400, "deadly": 3600},
    12: {"easy": 1000, "medium": 2000, "hard": 3000, "deadly": 4500},
    13: {"easy": 1100, "medium": 2200, "hard": 3400, "deadly": 5100},
    14: {"easy": 1250, "medium": 2500, "hard": 3800, "deadly": 5700},
    15: {"easy": 1400, "medium": 2800, "hard": 4300, "deadly": 6400},
    16: {"easy": 1600, "medium": 3200, "hard": 4800, "deadly": 7200},
    17: {"easy": 2000, "medium": 3900, "hard": 5900, "deadly": 8800},
    18: {"easy": 2100, "medium": 4200, "hard": 6300, "deadly": 9500},
    19: {"easy": 2400, "medium": 4900, "hard": 7300, "deadly": 10900},
    20: {"easy": 2800, "medium": 5700, "hard": 8500, "deadly": 12700},
}


def get_char_xp_threshold(level: int, difficulty: str) -> int:
    """REQ-BUI-003: Get the XP threshold for one character of `level` to meet `difficulty`.

    difficulty: "easy" | "medium" | "hard" | "deadly"
    Returns 0 for unknown levels or difficulties.
    """
    if level not in _CHAR_XP_THRESHOLDS:
        return 0
    return _CHAR_XP_THRESHOLDS[level].get(difficulty.lower(), 0)


def calc_party_xp_budget(party_levels: list[int], difficulty: str) -> int:
    """REQ-BUI-003: Sum individual character XP thresholds to get the party's total budget.

    Args:
        party_levels: List of character levels (e.g., [5, 5, 4, 4])
        difficulty: "easy" | "medium" | "hard" | "deadly"
    """
    return sum(get_char_xp_threshold(lvl, difficulty) for lvl in party_levels)


# =============================================================================
# REQ-BUI-002/004: Encounter Evaluation
# =============================================================================
def calc_encounter_xp(monster_crs: list[float]) -> int:
    """REQ-BUI-002: Sum monster XP values. No gang-up/swarm multipliers in 2024."""
    return sum(cr_to_xp(cr) for cr in monster_crs)


def evaluate_encounter(
    monster_crs: list[float],
    party_levels: list[int],
) -> dict:
    """REQ-BUI-002/004: Evaluate encounter difficulty.

    Compares total encounter XP against party XP budgets for each difficulty tier.

    Args:
        monster_crs: List of monster Challenge Ratings in the encounter.
        party_levels: List of player character levels.

    Returns a dict with:
        - total_xp: int
        - difficulty: "Trivial" | "Easy" | "Medium" | "Hard" | "Deadly"
        - budgets: {difficulty: threshold}
        - warnings: list[str]
    """
    total_xp = calc_encounter_xp(monster_crs)
    budgets = {
        "easy": calc_party_xp_budget(party_levels, "easy"),
        "medium": calc_party_xp_budget(party_levels, "medium"),
        "hard": calc_party_xp_budget(party_levels, "hard"),
        "deadly": calc_party_xp_budget(party_levels, "deadly"),
    }

    # Determine difficulty
    if total_xp <= budgets["easy"]:
        difficulty = "Trivial"
    elif total_xp <= budgets["medium"]:
        difficulty = "Easy"
    elif total_xp <= budgets["hard"]:
        difficulty = "Medium"
    elif total_xp <= budgets["deadly"]:
        difficulty = "Hard"
    else:
        difficulty = "Deadly"

    # REQ-BUI-006: Warn if any single monster's CR is significantly above party level
    avg_party_level = sum(party_levels) / len(party_levels) if party_levels else 0
    max_cr = max(monster_crs) if monster_crs else 0
    warnings = []
    if max_cr >= avg_party_level + 3:
        warnings.append(
            f"LETHALITY WARNING: A monster with CR {max_cr} is 3+ levels above "
            f"the party's average level ({avg_party_level:.1f}). This may cause a TPK."
        )

    return {
        "total_xp": total_xp,
        "difficulty": difficulty,
        "budgets": budgets,
        "warnings": warnings,
    }


# =============================================================================
# REQ-BUI-010: XP Award Distribution
# =============================================================================
def distribute_xp(total_encounter_xp: int, num_party_members: int) -> list[int]:
    """REQ-BUI-010: Divide XP equally among surviving party members.

    Returns a list of XP awards (one per member). Uses floor division;
    remainder CP is discarded.
    """
    if num_party_members <= 0:
        return []
    return [total_encounter_xp // num_party_members] * num_party_members


# =============================================================================
# REQ-BUI-005: Daily XP Budget
# =============================================================================
_DAILY_XP_BUDGET: dict[int, int] = {
    1: 300, 2: 600, 3: 1200, 4: 1700, 5: 3500,
    6: 4000, 7: 5000, 8: 6000, 9: 7500, 10: 9000,
    11: 10500, 12: 11500, 13: 13500, 14: 15000, 15: 18000,
    16: 20000, 17: 25000, 18: 27000, 19: 30000, 20: 40000,
}


def get_daily_xp_budget(party_levels: list[int]) -> int:
    """REQ-BUI-005: Sum of individual daily XP budgets for all party members."""
    return sum(_DAILY_XP_BUDGET.get(lvl, 0) for lvl in party_levels)


# =============================================================================
# REQ-INV-002: Push / Drag / Lift
# =============================================================================
def max_push_drag_lift(str_score: int) -> int:
    """REQ-INV-002: Maximum weight (in lbs) that can be pushed, dragged, or lifted.

    = Strength Score × 30
    """
    return str_score * 30


def max_carrying_capacity(str_score: int) -> int:
    """Maximum weight (lbs) that can be carried without penalty = Str × 15."""
    return str_score * 15


def carrying_status(current_load_lbs: int, str_score: int) -> str:
    """Determine carrying status and speed penalty based on current load.

    Returns: "normal" | "encumbered" (speed -20ft) | "heavily_encumbered" (speed -40ft)
    Carrying capacity (no penalty): up to Str×15 lbs
    Encumbered: Str×15 to Str×30 lbs
    Heavily encumbered: Str×30 to Str×60 lbs (push/drag/lift max)
    """
    max_carry = max_carrying_capacity(str_score)
    max_push = max_push_drag_lift(str_score)

    if current_load_lbs <= max_carry:
        return "normal"
    elif current_load_lbs <= max_push:
        return "encumbered"  # Speed reduced by 20 ft
    else:
        return "heavily_encumbered"  # Speed reduced by 40 ft, can't run


def carrying_speed_penalty(status: str) -> int:
    """REQ-INV-002: Speed penalty based on carrying status."""
    return {"normal": 0, "encumbered": 20, "heavily_encumbered": 40}.get(status, 0)
