"""
Drama Manager for the DM Engine's Graph-Grounded Storylet Orchestrator.

Provides:
- TensionArc: tracks session dramatic pacing (low/medium/high tension, consecutive beats)
- DramaManager: selects optimal storylet based on tension arc; injects storylet content into narrative

Integration: acts as a pre-hook in planner_node and as a post-action router.
No LLM calls — pure Python deterministic selection logic.
"""

import re
import uuid
from typing import Dict, Any, Optional, List

from storylet import Storylet, TensionLevel
from storylet_registry import StoryletRegistry


class TensionArc:
    """
    Tracks the session's dramatic pacing.

    Rules:
    - After each narrative beat, call advance_turn(outcome_tension)
    - 3 consecutive LOW → auto-escalate to MEDIUM target
    - 2 consecutive HIGH → auto-deescalate to LOW target
    """

    def __init__(
        self,
        current_tension: TensionLevel = TensionLevel.MEDIUM,
        target_tension: TensionLevel = TensionLevel.MEDIUM,
        consecutive_low: int = 0,
        consecutive_high: int = 0,
        session_turns: int = 0,
    ) -> None:
        self.current_tension = current_tension
        self.target_tension = target_tension
        self.consecutive_low = consecutive_low
        self.consecutive_high = consecutive_high
        self.session_turns = session_turns

    def advance_turn(self, outcome_tension: TensionLevel) -> None:
        """Called after each narrative beat. Updates the tension arc."""
        self.session_turns += 1
        self.current_tension = outcome_tension

        if outcome_tension == TensionLevel.LOW:
            self.consecutive_low += 1
            self.consecutive_high = 0
            if self.consecutive_low >= 3:
                self.target_tension = TensionLevel.MEDIUM
        elif outcome_tension == TensionLevel.HIGH:
            self.consecutive_high += 1
            self.consecutive_low = 0
            if self.consecutive_high >= 2:
                self.target_tension = TensionLevel.LOW
        else:  # MEDIUM
            self.consecutive_low = 0
            self.consecutive_high = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_tension": self.current_tension.value,
            "target_tension": self.target_tension.value,
            "consecutive_low": self.consecutive_low,
            "consecutive_high": self.consecutive_high,
            "session_turns": self.session_turns,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TensionArc":
        return cls(
            current_tension=TensionLevel(d.get("current_tension", "medium")),
            target_tension=TensionLevel(d.get("target_tension", "medium")),
            consecutive_low=int(d.get("consecutive_low", 0)),
            consecutive_high=int(d.get("consecutive_high", 0)),
            session_turns=int(d.get("session_turns", 0)),
        )


class DramaManager:
    """
    Drama Manager selects the optimal active storylet based on:
    1. Prerequisites must be met (polled from Knowledge Graph)
    2. Filtered by target tension level
    3. Sorted by priority_override then current_occurrences (prefer less-used)
    4. [Gap 7] Relationship-weighted: storylets involving NPCs with strong
       KG edges to the active character are boosted.
    """

    def __init__(self, registry: StoryletRegistry, kg: Any) -> None:
        self.registry = registry
        self.kg = kg
        self.arc = TensionArc()

    def _extract_storylet_npcs(self, storylet: Storylet) -> List[str]:
        """
        Extract NPC names referenced by a storylet.

        Looks at: storylet name, description, content, and tags.
        Tags formatted as 'npc:Name' are preferred; falls back to
        extracting [[Wikilink]]-style names from content.
        """
        names: List[str] = []

        # From tags: 'npc:King Aldric' format
        for tag in storylet.tags:
            if tag.startswith("npc:"):
                names.append(tag[4:].strip())

        # From content: [[Wikilink]] patterns
        for match in re.finditer(r"\[\[([^\]]+)\]\]", storylet.content):
            names.append(match.group(1).strip())

        # From name
        if storylet.name:
            names.append(storylet.name)

        return list(set(names))

    def _relationship_weight(self, storylet: Storylet, active_character: str) -> float:
        """
        Calculate relationship weight boost for a storylet.

        Gap 7: Queries KG for HOSTILE_TOWARD and ALLIED_WITH edges between
        storylet's NPCs and the active character. Positive relationships get
        a small boost; hostile relationships get a larger boost (drama).
        """
        if not active_character or active_character == "Unknown":
            return 0.0

        active_uuid = self.kg.find_node_uuid(active_character)
        if not active_uuid:
            return 0.0

        npc_names = self._extract_storylet_npcs(storylet)
        if not npc_names:
            return 0.0

        weight = 0.0
        for npc_name in npc_names:
            npc_uuid = self.kg.find_node_uuid(npc_name)
            if not npc_uuid:
                continue

            # Check edges from NPC to active character
            from knowledge_graph import GraphPredicate

            if self.kg.edge_exists(npc_uuid, GraphPredicate.HOSTILE_TOWARD, active_uuid):
                weight += 2.0  # Hostile NPCs nearby → more dramatic
            elif self.kg.edge_exists(npc_uuid, GraphPredicate.ALLIED_WITH, active_uuid):
                weight += 0.5  # Allied NPCs nearby → small positive boost
            elif self.kg.edge_exists(npc_uuid, GraphPredicate.SERVES, active_uuid):
                weight += 1.0  # Loyal followers → strong positive

        return weight

    def select_next(self, ctx: Dict[str, Any]) -> Optional[Storylet]:
        """
        Select the optimal storylet given current tension arc and available candidates.

        Selection criteria (in order):
        1. Prerequisites met (from registry.poll)
        2. Target tension level match
        3. Relationship weight boost (Gap 7)
        4. Priority override
        5. Prefer less-used storylets

        Returns None if no storylets are available.
        """
        candidates = self.registry.poll(self.kg, ctx)
        if not candidates:
            return None

        active_character = ctx.get("active_character", "Unknown")

        # Filter by target tension
        tension_bucket = [
            c for c in candidates if c.tension_level == self.arc.target_tension
        ]
        if not tension_bucket:
            tension_bucket = candidates  # Fall back to any valid storylet

        # Sort: priority_override desc, current_occurrences asc, relationship_weight desc
        tension_bucket.sort(
            key=lambda s: (
                -(s.priority_override or 0),
                s.current_occurrences,
                -self._relationship_weight(s, active_character),
            )
        )

        return tension_bucket[0]

    def inject_storylet(self, storylet: Storylet, ctx: Dict[str, Any]) -> str:
        """
        Render the storylet content for injection into the narrative.

        Simple template substitution: {variable} → ctx["variable"].
        No LLM calls.
        """
        content = storylet.content
        for key, val in ctx.items():
            placeholder = "{" + key + "}"
            if placeholder in content:
                content = content.replace(placeholder, str(val))
        return content

    def apply_effects(self, storylet: Storylet) -> List[Any]:
        """
        Execute all graph mutations from storylet effects.

        Returns list of executed GraphMutation objects.
        """
        executed = []
        for effect in storylet.effects:
            for mutation in effect.graph_mutations:
                mutation.execute(self.kg)
                executed.append(mutation)
            # Increment occurrence counter
            storylet.current_occurrences += 1
        return executed

    def storylet_injection_prompt(self, storylet: Storylet, ctx: Dict[str, Any]) -> str:
        """
        Builds a system prompt fragment that injects the active storylet's
        narrative into the planner/narrator context.
        """
        content = self.inject_storylet(storylet, ctx)
        return (
            f"[STORYLET DRIVE — '{storylet.name}']:\n"
            f"The following narrative beat is active and MUST be incorporated into your response:\n\n"
            f"{content}\n\n"
            f"Tags: {', '.join(sorted(storylet.tags))} | Tension: {storylet.tension_level.value}\n"
            f"Inject this faithfully. Do not contradict established campaign facts."
        )
