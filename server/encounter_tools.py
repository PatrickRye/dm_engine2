"""
Encounter generation and evaluation tools.

Canonical location for DEGA (Dynamic Encounter Generation Algorithm):
- evaluate_encounter_difficulty: REQ-BUI-001/002/003/004/006/010
- build_encounter: REQ-BUI-003/004
- distribute_encounter_xp: REQ-BUI-010
- generate_or_calibrate_encounter: REQ-BUI-007/008/009 (DEGA)
"""
from __future__ import annotations

import math
import random
from typing import Any

from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from pydantic import Field
from typing import Annotated

from knowledge_graph import KnowledgeGraph, GraphNodeType, GraphPredicate, KnowledgeGraphNode
from registry import get_all_entities, get_knowledge_graph
from dnd_rules_engine import Creature
import rules_engine as _rules_engine


# === Encounter Difficulty Evaluation (REQ-BUI-001/002/003/004/006/010) ===

from rules_engine import (
    cr_to_xp,
    xp_to_cr,
    calc_encounter_xp,
    evaluate_encounter as _evaluate_encounter,
    calc_party_xp_budget,
    get_char_xp_threshold,
    distribute_xp,
    get_daily_xp_budget,
)


@tool
async def evaluate_encounter_difficulty(
    monster_crs: Annotated[list[float], Field(description="List of monster Challenge Ratings (CRs) in the encounter.")],
    party_levels: Annotated[list[int], Field(description="List of player character levels in the party.")],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-BUI-001/002/003/004/006/010: Evaluate encounter difficulty for a party.

    Compares total monster XP against party XP budgets for each difficulty tier.
    Returns the difficulty rating (Trivial/Easy/Medium/Hard/Deadly) and a
    detailed breakdown including budgets per tier, total XP, and any
    lethality warnings (REQ-BUI-006).

    monster_crs example: [2, 3, 1] for three monsters of CR 2, 3, and 1.
    party_levels example: [5, 5, 4, 4] for a four-member party.
    """
    result = _evaluate_encounter(monster_crs, party_levels)

    lines = [
        f"Encounter Evaluation (REQ-BUI-002/004):",
        f"  Monster CRs: {monster_crs}",
        f"  Total XP: {result['total_xp']}",
        f"  Difficulty: **{result['difficulty']}**",
        "",
        f"  Party XP Budgets (REQ-BUI-003):",
        f"    Easy:    {result['budgets']['easy']} XP",
        f"    Medium:  {result['budgets']['medium']} XP",
        f"    Hard:   {result['budgets']['hard']} XP",
        f"    Deadly: {result['budgets']['deadly']} XP",
    ]

    if result["warnings"]:
        lines.append("")
        lines.append("  WARNINGS:")
        for w in result["warnings"]:
            lines.append(f"    ⚠ {w}")

    return "\n".join(lines)


@tool
async def build_encounter(
    target_difficulty: Annotated[str, Field(description="Desired difficulty: trivial, easy, medium, hard, or deadly.")],
    party_levels: Annotated[list[int], Field(description="List of player character levels.")],
    monster_pool: Annotated[list[dict] | None, Field(default=None, description="Optional list of available monsters as dicts with 'name' and 'cr' keys. If not provided, only XP math is returned.")],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-BUI-003/004: Given a target difficulty and party levels, return the XP budget.

    This tool calculates the XP budget for the requested difficulty and
    optionally suggests monster composition if a monster_pool is provided.

    Note: Actual monster selection from a pool requires the DM to choose
    appropriate CRs from the available monsters.
    """
    diff = target_difficulty.lower()
    if diff not in ("trivial", "easy", "medium", "hard", "deadly"):
        return f"SYSTEM ERROR: Unknown difficulty '{target_difficulty}'. Use: trivial, easy, medium, hard, deadly."

    budget = calc_party_xp_budget(party_levels, diff)
    daily_budget = get_daily_xp_budget(party_levels)

    lines = [
        f"Encounter Planning (REQ-BUI-003/004):",
        f"  Target Difficulty: {target_difficulty.capitalize()}",
        f"  Party Levels: {party_levels} (avg {sum(party_levels)/len(party_levels):.1f})",
        f"  XP Budget: {budget}",
        f"  Daily XP Budget (REQ-BUI-005): {daily_budget}",
    ]

    if monster_pool:
        total_xp = 0
        selected = []
        remaining = list(monster_pool)
        while remaining:
            best = None
            best_cr = 0
            for m in remaining:
                cr = float(m["cr"])
                if total_xp + cr_to_xp(cr) <= budget:
                    if cr > best_cr:
                        best_cr = cr
                    best = m
            if best is None:
                break
            selected.append(best)
            remaining.remove(best)
            total_xp += cr_to_xp(float(best["cr"]))

        lines.append(f"  Suggested monsters (XP total: {total_xp}):")
        for m in selected:
            lines.append(f"    - {m['name']} (CR {m['cr']}, XP {cr_to_xp(float(m['cr']))})")
        if remaining and selected:
            lines.append(f"  Note: {len(remaining)} monster(s) in pool could not be added without exceeding budget.")

    return "\n".join(lines)


@tool
async def distribute_encounter_xp(
    total_encounter_xp: Annotated[int, Field(description="Total XP from the defeated encounter.")],
    num_party_members: Annotated[int, Field(description="Number of surviving, participating party members.")],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """REQ-BUI-010: Divide XP equally among surviving party members."""
    if num_party_members <= 0:
        return "ERROR: num_party_members must be positive."

    awards = distribute_xp(total_encounter_xp, num_party_members)
    return (
        f"XP Distribution (REQ-BUI-010): {total_encounter_xp} XP split among "
        f"{num_party_members} party members = {awards[0]} XP each. Total distributed: {awards[0] * num_party_members} XP "
        f"({total_encounter_xp - awards[0] * num_party_members} XP discarded as remainder)."
    )


# === DEGA Encounter Generation (REQ-BUI-007/008/009) ===

# ---- Damage-type psychological atmosphere (Ammann's Metaphorical Mechanics) ----
# Maps damage type → (label, short_atmospheric_blurb, combat_voice_hint, interaction_tone)
# Used to give the LLM-DM explicit flavor guidance per creature's damage profile.
_DAMAGE_TYPE_ATMOSPHERE: dict[str, tuple[str, str, str, str]] = {
    "acid": (
        "The Corroding Malice",
        "Spiteful erosion of willpower and foundation. The creature's attacks represent toxic, "
        "lingering hatred that breaks down both body and resolve. Targets feel their confidence "
        "dissolving, their certainty in their own strength melting away.",
        "Speaks in hissing, dripping contempt. Comments mock the target's weakening defenses. "
        "Mouths phrases like 'I can already hear your armor pitting' or 'your resolve is already "
        "eating itself.'",
        "Cold hatred. The creature holds grudges and references past failures. A target who "
        "escapes may find their equipment corroding for days afterward.",
    ),
    "cold": (
        "The Apathetic Ruthlessness",
        "Unfeeling, surgical detachment. The creature treats targets as statistics, problems to "
        "be solved with the minimum necessary effort. Cold damage represents the numb realization "
        "that no mercy, guilt, or hesitation will be shown.",
        "Speaks in flat, clipped tones. No passion, no taunts. Only precise statements of intent "
        "and outcome. 'You will stop moving. I will wait.' Does not dignify attacks with words.",
        "Merciless but not cruel — there is no pleasure in cruelty, only efficiency. Interaction "
        "reveals a creature that has simply stopped seeing others as worth emotional investment.",
    ),
    "fire": (
        "The Blind Wrath",
        "Overwhelming, chaotic destruction driven by hatred or zealous fury. Fire damage represents "
        "consumption — of the self, of others, of everything in range. Targets feel the heat of "
        "uncontrolled emotion consuming their safety.",
        "Roars, screams, or preaches. Speech is absolute and inflammatory. References burning away "
        "impurity, purging the weak, or the glorious blaze that will consume all. Often quotes "
        "apocalyptic mantras.",
        "Zealotry or grief weaponized as violence. The creature's grudges burn bright; its hatred "
        "is all-consuming. Players who engage in dialogue will find only a monologue of consumed reason.",
    ),
    "psychic": (
        "The Existential Terror",
        "Weaponized trauma and identity unraveling. Psychic damage represents the forced "
        "confrontation with incomprehensible realities — the violation of the mind itself. Targets "
        "lose track of who they are mid-combat.",
        "Whispers contradictory truths, recites the target's own memories back incorrectly, or "
        "describes visions of their death from perspectives they have never occupied. "
        "'You already know how this ends. You've always known.'",
        "The creature seems to know things it should not. Dialogue shifts between prophetic "
        "foreknowledge and complete dissociation. Targets describe feeling 'peeled back' and 'seen "
        "in ways I can't describe.'",
    ),
    "necrotic": (
        "The Despairing Entropy",
        "The inevitability of decay and the draining of hope. Necrotic damage represents the "
        "ultimate futility — not death, but the slow slide toward it, the certainty that resistance "
        "changes nothing. Targets feel life itself becoming heavier.",
        "Speaks in tones of exhausted certainty. References the futility of fighting, the "
        "inevitability of the end. Often delivers monologues about how everything falls eventually, "
        "how the target was already dead the moment they entered this space.",
        "Somber, patient, almost pitying. The creature does not hate — it has simply accepted what "
        "awaits. Dialogue reveals a deep weariness that manifests as methodical cruelty or "
        "strangely gentle dismissal.",
    ),
    "force": (
        "The Unstoppable Conviction",
        "Pure, undiluted will made manifest as motion. Force damage is the imposition of the "
        "creature's intent onto reality without negotiation. Targets are not attacked — they are "
        "simply moved to where the creature intends them to be.",
        "Speaks with absolute authority. No threats, no emotion — only declarations. 'You will "
        "be moved.' 'This is not a choice.' Commands and spatial directives dominate its speech.",
        "The creature is always in control of the environment. It repositions targets as easily as "
        "speaking. Negotiation is possible but framed entirely on its terms.",
    ),
    "lightning": (
        "The Sudden Fury",
        "Instant, overwhelming voltage — the chaos of a storm made personal. Lightning damage "
        "represents the unpredictability of nature weaponized against a single target. Attacks "
        "are not planned; they simply happen, faster than thought.",
        "Speaks in crackling asides, fragmented shouts, or electric silence between strikes. "
        "Laughs at the speed of its own attacks. 'Did you see it coming? No. You won't next time either.'",
        "Erratic and hyperactive. The creature cannot sit still mentally or physically. It zips "
        "between topics, between positions, between targets. Dialogue is a dazzling, disorienting sprint.",
    ),
    "thunder": (
        "The Overwhelming Presence",
        "Raw kinetic force delivered with earth-shaking authority. Thunder damage is the "
        "declaration that the creature occupies this space and the target does not. It is not "
        "about killing — it is about announcing arrival.",
        "Speaks in declarations that physically resonate. Every word carries weight, literally. "
        "Shouts the creature's name or title as it strikes. Roars challenges that make the "
        "ground tremble.",
        "The creature's presence fills the room before combat begins. It speaks to claim space, "
        "literally and metaphorically. Negotiation is shouting over the creature's presence — "
        "it may not even lower its voice to listen.",
    ),
    "poison": (
        "The Patient Malice",
        "Slow, creeping ruination that starts invisible. Poison damage represents a grudge "
        "that has been cultivated for years, applied drop by drop, designed to watch the "
        "target deteriorate from something that seemed harmless.",
        "Speaks softly, with careful enunciation. References 'what's coming' in measured terms. "
        "Often describes symptoms with clinical detachment. 'You'll feel it in an hour. By morning, "
        "you'll understand.'",
        "The creature seems helpful until it isn't. It offers what appears to be genuine assistance "
        "or information, then reveals the poison beneath. Targets feel paranoid about every subsequent "
        "interaction, even with allies.",
    ),
    "slashing": (
        "The Territorial Claim",
        "Tearing flesh to mark territory and establish dominance. Slashing damage is personal — "
        "the creature is not just defeating the target, it is proving its superiority through "
        "visible, visceral evidence.",
        "Growls, snarls, or delivers territorial challenges. Threatens to mark those who flee. "
        "'Running just means I get to chase you first.' Proves dominance with every swing.",
        "The creature establishes dominance immediately. Physical posturing dominates all "
        "interactions. Treats negotiation as a test of status rather than an exchange of information.",
    ),
    "piercing": (
        "The Clinical Precision",
        "Surgical penetration to find and exploit weakness. Piercing damage represents a "
        "mind that sees through armor, deflection, and deception to the single point that "
        "matters. It is the attack of a hunter, not a warrior.",
        "Speaks in hunting terminology. References finding 'the right angle' on a target. "
        "Comments on weak points discovered. 'There's the one.' Rarely speaks during combat "
        "but delivers verdict-like statements before each strike.",
        "Analytical and patient. The creature evaluates everything as potential prey or "
        "terrain. It may know more about the party than they know about themselves, assembled "
        "through patient observation.",
    ),
    "bludgeoning": (
        "The Absolute Stop",
        "Unstoppable, grinding force that ends motion and argument alike. Bludgeoning damage "
        "is the rejection of evasion — the creature does not care about armor or agility, "
        "only that the target stops.",
        "Speaks in terms of finality and stopping. 'Stop.' 'Be still.' 'You are done.' "
        "Short, crushing declarations. The creature does not taunt — it delivers verdicts.",
        "The creature treats all resistance as already concluded. It speaks from the assumption "
        "that the target's fate is already decided, and it is merely informing them of details.",
    ),
}


def _guess_damage_type_for_role(role: str, cr: float) -> list[str]:
    """Assign thematic damage type(s) to a DEGA tactical role.

    Returns a list of 1-3 damage type keys from _DAMAGE_TYPE_ATMOSPHERE,
    reflecting the archetype's psychological profile.
    """
    # High-CR elites and solos are thematically richer
    high_cr = cr >= 5.0

    mapping: dict[str, list[str]] = {
        "elite":     ["force", "thunder"] if high_cr else ["piercing", "slashing"],
        "brute":     ["bludgeoning", "thunder"],
        "artillerist": ["fire", "lightning"] if high_cr else ["fire"],
        "controller": ["psychic", "cold"] if high_cr else ["psychic"],
        "lurker":    ["piercing", "poison"],
        "skirmisher": ["lightning", "piercing"],
        "support":   ["cold", "necrotic"],  # healing twisted to siphon rather than restore
        "tank":      ["bludgeoning", "cold"],
        "minion":    ["slashing", "piercing"],
        "solo":      ["force", "psychic", "necrotic"],
    }
    return mapping.get(role, ["slashing"])


def _build_tactical_demeanor(monster_list: list[dict]) -> str:
    """Build the TACTICAL DEMEANOR block for MECHANICAL TRUTH output.

    For each monster, emits damage-type psychological flavor so the LLM-DM
    has concrete atmospheric guidance for combat voice, interaction tone, and observable behavior.
    """
    lines = ["  TACTICAL DEMEANOR:"]
    for m in monster_list:
        name = m.get("name", "Unknown")
        role = m.get("role", "standard").lower()
        cr = m.get("cr", 0)

        dmg_types = m.get("damage_types", _guess_damage_type_for_role(role, cr))

        role_lines = []
        for dt_key in dmg_types[:3]:  # Cap at 3 damage types for brevity
            if dt_key not in _DAMAGE_TYPE_ATMOSPHERE:
                continue
            label, atmosphere, combat_voice, interaction_tone = _DAMAGE_TYPE_ATMOSPHERE[dt_key]
            role_lines.append(f"    [{dt_key.upper()}] {label}")
            role_lines.append(f"      ATMOSPHERE: {atmosphere}")
            role_lines.append(f"      COMBAT VOICE: {combat_voice}")
            role_lines.append(f"      INTERACTION TONE: {interaction_tone}")

        if not role_lines:
            role_lines = [f"    Standard combatant — no special damage-type flavor."]
        else:
            # Join with continuation indent
            role_lines = ["    Damage-type psychological profile:"] + role_lines

        lines.append(f"  {name} (role: {role}, CR {cr:.1f}):")
        lines.extend(role_lines)

    return "\n".join(lines)


# ---- DEGA archetype definitions ----
_ARCHETYPE_WEIGHTS_BY_PACING = {
    # E = encounters remaining today (including this one)
    1: {"apex": 0.70, "phalanx": 0.15, "ambush": 0.10, "swarm": 0.05},
    2: {"ambush": 0.40, "phalanx": 0.30, "swarm": 0.20, "apex": 0.10},
    3: {"swarm": 0.40, "phalanx": 0.30, "ambush": 0.20, "apex": 0.10},
}

_ARCHETYPE_ROLE_XP = {
    # Archetype -> list of (role, xp_fraction_of_encounter_budget)
    "phalanx": [("tank", 0.40), ("artillerist", 0.40), ("minion", 0.20)],
    "ambush":  [("lurker", 0.50), ("controller", 0.30), ("skirmisher", 0.20)],
    "swarm":   [("brute", 0.30), ("minion", 0.60), ("support", 0.10)],
    "apex":    [("elite", 0.70), ("minion", 0.15), ("controller", 0.15)],
}

# DEGA tactical-role template modifiers.
# Each entry: (cr_delta, ac_delta, description).
# AC delta is added to base AC (capped at max 22 for Tank).
_ROLE_TEMPLATES = {
    "elite":       {"cr": +1, "ac": +4, "desc": "Advanced (+2 all d20 rolls, +4 AC, +2 HP/hit die)"},
    "artillerist": {"cr":  0, "ac": -2, "desc": "Ranged attack (R≥30 ft), -2 AC"},
    "brute":       {"cr": +1, "ac": -2, "desc": "+2 to hit, +1 damage die, -2 AC"},
    "controller":  {"cr":  0, "ac":  0, "desc": "+Spellcasting (Save DC 13, web/command/bane)"},
    "lurker":      {"cr":  0, "ac":  0, "desc": "+10 ft speed, auto-grapple, Stealth expertise"},
    "skirmisher":  {"cr":  0, "ac": -2, "desc": "Double speed or Fly speed, reach +5 ft"},
    "support":     {"cr":  0, "ac":  0, "desc": "+Healing spellcasting (cure wounds 3/day, bless)"},
    "tank":        {"cr": +1, "ac": +4, "desc": "+4 AC (max 22), Parry reaction (+prof to AC vs melee)"},
    "minion":      {"cr":  0, "ac": -2, "desc": "1 HP, auto-succeeds spell saves (Flee Mortals! variant)"},
}


def _compute_party_levels_from_kg(
    kg: KnowledgeGraph,
    current_map_id: str | None,
    vault_path: str,
) -> list[int]:
    """DEGA §1: Query KG for PLAYER/NPC nodes on current map; return their levels.

    Filters out entities whose KG node has is_remote=True or a mismatched map_id,
    which covers NPCs on other maps or too distant to join the encounter.
    """
    levels: list[int] = []
    entities = get_all_entities(vault_path)
    for entity in entities.values():
        if not isinstance(entity, Creature) or not entity.classes:
            continue
        # Exclude summoned companions unless their master is present (handled elsewhere)
        if entity.summoned_by_uuid:
            continue
        level = sum(c.level for c in entity.classes)
        if level <= 0:
            continue

        # KG-based map filtering: cross-reference entity position with KG node
        node: KnowledgeGraphNode | None = kg.get_node_by_name(entity.name)
        if node:
            if node.get_attribute("is_remote") is True:
                continue
            node_map = node.get_attribute("map_id")
            if current_map_id and node_map and node_map != current_map_id:
                continue
        levels.append(level)
    return levels


def _select_archetype(encounters_remaining: int) -> str:
    """DEGA §2: Select encounter archetype weighted by session pacing."""
    weights = _ARCHETYPE_WEIGHTS_BY_PACING.get(
        encounters_remaining, _ARCHETYPE_WEIGHTS_BY_PACING[3]
    )
    archs = list(weights.keys())
    probs = list(weights.values())
    return random.choices(archs, weights=probs, k=1)[0]


def _distribute_xp_to_roles(
    archetype: str,
    xp_enc: int,
) -> dict[str, int]:
    """DEGA §2: Split XP_enc across tactical roles per archetype ratios."""
    role_xp: dict[str, int] = {}
    for role, fraction in _ARCHETYPE_ROLE_XP[archetype]:
        role_xp[role] = int(xp_enc * fraction)
    return role_xp


def _best_cr_for_xp(xp_target: int, cr_max: float) -> float:
    """Return the highest CR whose XP does not exceed xp_target."""
    best_cr: float = 0.0
    for cr_val in sorted(_rules_engine._CR_TO_XP.keys(), reverse=True):
        if cr_val <= cr_max and _rules_engine._CR_TO_XP[cr_val] <= xp_target:
            best_cr = cr_val
            break
    return best_cr


def _build_env_requirements(
    role_counts: dict[str, int],
) -> dict[str, Any]:
    """DEGA §4: Derive environmental constraints from assigned tactical roles."""
    reqs: dict[str, Any] = {}
    if role_counts.get("artillerist", 0) > 0:
        reqs["cover_elements"] = ["half_cover"] * 3 + ["total_cover"]
        reqs["line_of_sight"] = "clear center corridor required"
    if role_counts.get("lurker", 0) > 0:
        reqs["obscurement"] = "heavy (40% of battlefield, e.g. fog, darkness, deep water)"
        reqs["verticality"] = "required (climb or fly surfaces)"
    if role_counts.get("skirmisher", 0) > 0:
        reqs["arena_size"] = "minimum 60×60 ft"
        reqs["difficult_terrain"] = "central patches to penalize player movement"
    if role_counts.get("controller", 0) > 0:
        reqs["choke_points"] = "required (narrow corridors, doorways, rope bridges)"
    if role_counts.get("brute", 0) > 0:
        reqs["open_ground"] = "open lanes for charge approach"
    return reqs


def _apply_template(
    base_cr: float,
    role: str,
    cr_max: float,
) -> tuple[float, str, str]:
    """Apply DEGA role template to a base CR; returns (new_cr, role_label, template_desc)."""
    tmpl = _ROLE_TEMPLATES.get(role, {"cr": 0, "ac": 0, "desc": ""})
    new_cr = min(base_cr + tmpl["cr"], cr_max)
    label = f"{role.capitalize()} ({tmpl['desc']})"
    return new_cr, label, tmpl["desc"]


@tool
async def generate_or_calibrate_encounter(
    party_levels: Annotated[
        list[int] | None,
        Field(default=None, description=(
            "List of PC levels. If omitted, the tool queries the Knowledge Graph "
            "for all PLAYER/NPC entities on the current map and extracts levels "
            "from the engine registry, excluding remote/distant entities."
        )),
    ],
    mode: Annotated[
        str,
        Field(description='"generate" (random) or "calibrate" (tune pre-planned)'),
    ],
    preplanned_monsters: Annotated[
        list[dict] | None,
        Field(
            default=None,
            description='Calibrate mode: list of {name, cr, role_hint?} for each intended enemy',
        ),
    ],
    location_tags: Annotated[
        list[str] | None,
        Field(default=None, description="KG location tags to scope entity queries (e.g. [forest, underground])"),
    ],
    encounters_today: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Encounters already completed since the last long rest. "
                "Used to derive session pacing E = encounters_today + 1."
            ),
        ),
    ],
    target_difficulty: Annotated[
        str,
        Field(default="medium", description="trivial / easy / medium / hard / deadly"),
    ],
    current_map_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "KG map_id of the current location. Used to exclude party members "
                "on other maps / too distant to join the encounter."
            ),
        ),
    ],
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """DEGA Encounter Generator — REQ-BUI-007/008/009.

    Implements all four DEGA phases:

    **Phase I — Budget**: Queries KG for active party (excluding remote/distant NPCs),
    derives N, APL, and session pacing E. Computes XP_enc and CR_max.

    **Phase II — Archetype Selection**: In generate mode, selects a DEGA archetype
    weighted by E (Apex for climactic single encounters, Swarm/Phalanx for attrition).

    **Phase III — Composition / Calibration**: Distributes XP_enc across tactical roles.
    In generate mode, selects base creatures from the KG entity pool and applies
    DEGA role templates (Brute, Artillerist, Controller, Elite, Lurker, Minion,
    Skirmisher, Solo, Support, Tank) to fit the budget. In calibrate mode, evaluates
    pre-planned monsters against XP_enc and applies Elite/Brute upscaling or Minion
    downscaling to close the gap.

    **Phase IV — Spatial Requirements**: Derives environmental constraints
    (cover for Artillerists, obscurement for Lurkers, choke points for Controllers,
    etc.) and verifies or generates them via the Spatial Engine.

    Output format: ``MECHANICAL TRUTH:`` string captured by the LangGraph
    action_logic_node for deferred mutation execution.

    Args:
        mode: "generate" builds a random encounter; "calibrate" tunes pre-planned enemies.
        preplanned_monsters: Required when mode="calibrate". Each dict: {name, cr, role_hint?}.
        party_levels: Optional override for party composition. If None, derived from KG.
        encounters_today: Encounters completed since last long rest.
        target_difficulty: Base difficulty (escalates automatically when E is low).
        current_map_id: KG map_id for filtering co-located party members.
        location_tags: KG tags for scoping candidate creature pool.
    """
    # ---- Derive vault_path from config ----
    vault_path: str = config.get("configurable", {}).get("vault_path", "default") if config else "default"

    # ---- Phase I: Party composition ----
    if party_levels is None:
        kg = get_knowledge_graph(vault_path)
        party_levels = _compute_party_levels_from_kg(kg, current_map_id, vault_path)

    if not party_levels:
        return (
            "SYSTEM ERROR: No party members found. Provide party_levels explicitly, "
            "or ensure the KG contains PLAYER/NPC nodes co-located on the current map."
        )

    N = len(party_levels)
    L = sum(party_levels) // N  # Average Party Level (integer floor)
    E = encounters_today + 1      # This encounter = encounters_today + 1

    # Escalate difficulty when pacing forces a single big battle
    effective_difficulty = target_difficulty
    if E <= 2:
        effective_difficulty = "deadly"

    XP_total   = get_daily_xp_budget(party_levels)
    XP_enc     = XP_total // E
    CR_max     = min(L + math.ceil(L / 2), 30)

    # ---- Generate or Calibrate? ----
    if mode == "calibrate":
        if not preplanned_monsters:
            return "SYSTEM ERROR: calibrate mode requires preplanned_monsters list."
        current_xp = sum(cr_to_xp(float(m["cr"])) for m in preplanned_monsters)
        xp_gap = XP_enc - current_xp

        lines = [
            "MECHANICAL TRUTH:",
            f"  Party: {N} members, APL {L}, encounters_today={encounters_today}",
            f"  XP_enc={XP_enc} (daily={XP_total}, E={E}), CR_max={CR_max}",
            f"  Effective difficulty: {effective_difficulty}",
            f"  Pre-planned XP={current_xp}, gap={xp_gap:+d}",
            f"  Monsters:",
        ]
        calibrated: list[dict] = []
        role_assignments: dict[str, str] = {}

        for m in preplanned_monsters:
            base_cr = float(m["cr"])
            role_hint = m.get("role_hint", "") or "standard"
            # Decide whether to upscale, leave as-is, or strip
            if xp_gap > 0 and base_cr < CR_max:
                # Gap is positive (underbudget) — upgrade strongest pre-planned
                new_cr, label, _ = _apply_template(base_cr, "elite", CR_max)
                xp_gain = cr_to_xp(new_cr) - cr_to_xp(base_cr)
                xp_gap -= xp_gain
                # Preserve original role_hint for narrative coherence
                effective_role = role_hint if role_hint else label
                role_assignments[m["name"]] = effective_role
                lines.append(f"    - {m['name']} CR {base_cr} → upgraded to {label} (CR {new_cr:.1f}) [{effective_role}]")
                calibrated.append({
                    **m, "cr": new_cr, "role": effective_role,
                    "damage_types": _guess_damage_type_for_role(effective_role, new_cr),
                })
            else:
                role_assignments[m["name"]] = role_hint
                lines.append(f"    - {m['name']} (CR {base_cr}, {role_hint})")
                calibrated.append({
                    **m, "role": role_hint,
                    "damage_types": _guess_damage_type_for_role(role_hint, base_cr),
                })

        # If still under budget, add minions to fill
        if xp_gap > 0:
            minion_cr = _best_cr_for_xp(xp_gap, CR_max)
            if minion_cr > 0:
                lines.append(f"    - Minion (CR {minion_cr:.1f}) — added to fill XP gap")
                calibrated.append({
                    "name": "Minion", "cr": minion_cr, "role": "minion",
                    "damage_types": _guess_damage_type_for_role("minion", minion_cr),
                })

        # Build role counts for env requirements
        role_counts: dict[str, int] = {}
        for m in calibrated:
            role = m.get("role", "standard").lower()
            role_counts[role] = role_counts.get(role, 0) + 1
        reqs = _build_env_requirements(role_counts)
        lines.append(f"  Environmental requirements: {reqs}")
        lines.append(_build_tactical_demeanor(calibrated))
        return "\n".join(lines)

    # ---- Generate mode ----
    archetype = _select_archetype(E)
    role_xp = _distribute_xp_to_roles(archetype, XP_enc)

    # Collect candidate creatures from KG by location_tags
    kg = get_knowledge_graph(vault_path)
    candidates: list[tuple[str, float]] = []  # (name, cr)
    if kg and kg.nodes:
        for node in kg.nodes.values():
            if location_tags:
                if not any(tag in node.tags for tag in location_tags):
                    continue
            if node.node_type in (GraphNodeType.NPC, GraphNodeType.PLAYER):
                cr = node.get_attribute("challenge_rating") or node.get_attribute("cr")
                if cr is not None:
                    try:
                        candidates.append((node.name, float(cr)))
                    except (TypeError, ValueError):
                        pass

    # Fallback pool if KG has no matching creatures
    if not candidates:
        candidates = [
            ("Bandit", 0.125), ("Cultist", 0.125), ("Wolf", 0.25),
            ("Orc", 0.5), ("Goblin", 0.25), ("Skeleton", 0.25),
            ("Zombie", 0.25), ("Ogre", 2.0), ("Owlbear", 5.0),
        ]

    lines = [
        "MECHANICAL TRUTH:",
        f"  Party: {N} members, APL {L}, encounters_today={encounters_today}",
        f"  XP_enc={XP_enc} (daily={XP_total}, E={E}), CR_max={CR_max}",
        f"  Effective difficulty: {effective_difficulty}",
        f"  Archetype: {archetype} (DEGA §2)",
        f"  Monsters:",
    ]

    role_counts: dict[str, int] = {}
    monster_list: list[dict] = []  # for tactical demeanor
    for role, role_budget_xp in role_xp.items():
        best_cr = _best_cr_for_xp(role_budget_xp, CR_max)
        if best_cr <= 0:
            continue
        # Pick a random candidate close to best_cr
        candidates_sorted = sorted(candidates, key=lambda c: abs(c[1] - best_cr))
        name = candidates_sorted[0][0] if candidates_sorted else role.capitalize()
        new_cr, label, _ = _apply_template(best_cr, role, CR_max)
        role_counts[role] = role_counts.get(role, 0) + 1
        dmg_types = _guess_damage_type_for_role(role, new_cr)
        lines.append(f"    - {name} (CR {best_cr:.2g}, {label})")
        monster_list.append({"name": name, "cr": best_cr, "role": role, "damage_types": dmg_types})

    reqs = _build_env_requirements(role_counts)
    lines.append(f"  Environmental requirements: {reqs}")
    lines.append(_build_tactical_demeanor(monster_list))
    return "\n".join(lines)
