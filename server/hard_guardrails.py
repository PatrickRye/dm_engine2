"""
Hard Guardrails for the DM Engine's Graph-Grounded Storylet Orchestrator.

Provides deterministic validation (NO LLM calls) that intercepts narrative
outputs and proposed mutations before they reach the player:

- GuardrailResult: allowed/rejected with reason and required revisions
- HardGuardrails:
  - validate_immutable_violation: rejects mutations targeting immutable nodes
  - validate_graph_consistency: rejects mutations referencing non-existent nodes
  - validate_narrative_claim: verifies [[Wikilinks]] in prose correspond to actual KG entities
  - validate_svo_claims: verifies narrative-implied world state changes have corresponding mutations (Gap 4)
  - validate_full_pipeline: runs all checks, aggregates results

This is the "fishbowl" — the deterministic walls that prevent the LLM from
violating established world facts, thematic boundaries, or storylet contracts.
"""

import re
import uuid
from typing import Dict, Any, List, Optional, Tuple

from knowledge_graph import GraphPredicate
from storylet import GraphMutation


class GuardrailResult:
    """Result of a hard guardrail validation check."""

    def __init__(
        self,
        allowed: bool,
        reason: str = "",
        required_revisions: Optional[List[str]] = None,
    ) -> None:
        self.allowed = allowed
        self.reason = reason
        self.required_revisions = required_revisions or []

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:
        if self.allowed:
            return "GuardrailResult(allowed=True)"
        return f"GuardrailResult(allowed=False, reason='{self.reason}', revisions={self.required_revisions})"


class HardGuardrails:
    """
    Deterministic validation layer. No LLM involved.

    All checks are pure Python operating on the Knowledge Graph and
    the narrative text (parsed with regex for Wikilinks).
    """

    def __init__(self, kg: Any) -> None:
        self.kg = kg

    # ------------------------------------------------------------------
    # Core checks
    # ------------------------------------------------------------------
    def validate_immutable_violation(
        self, node_uuid: Optional[uuid.UUID], mutation: GraphMutation
    ) -> GuardrailResult:
        """Reject if a mutation targets an immutable node."""
        if node_uuid is None:
            return GuardrailResult(allowed=True)  # Can't check unknown node

        node = self.kg.get_node(node_uuid)
        if node is None:
            return GuardrailResult(allowed=True)  # Unknown node; other checks will catch

        if node.is_immutable:
            return GuardrailResult(
                allowed=False,
                reason=f"Node '{node.name}' is immutable and cannot be modified.",
                required_revisions=[f"Do not attempt to modify, remove, or alter '{node.name}'."],
            )
        return GuardrailResult(allowed=True)

    def validate_graph_consistency(self, mutation: GraphMutation) -> GuardrailResult:
        """
        Ensure the mutation references valid nodes.
        add_node: node_name must not already exist (unless overwrite is intended)
        add_edge: both subject and object must exist
        Other mutations: subject must exist
        """
        # Resolve subject node
        subj_uuid = mutation._resolve_node_uuid(self.kg)
        obj_uuid = mutation._resolve_target_uuid(self.kg)

        if mutation.mutation_type == "add_node":
            if mutation.node_name:
                existing = self.kg.get_node_by_name(mutation.node_name)
                if existing and existing.node_type.value != mutation.node_type:
                    return GuardrailResult(
                        allowed=False,
                        reason=f"A node named '{mutation.node_name}' already exists with type '{existing.node_type.value}'. "
                        f"Cannot create a duplicate with type '{mutation.node_type}'.",
                        required_revisions=[
                            f"Use a different name for the new {mutation.node_type} or update the existing node."
                        ],
                    )
            return GuardrailResult(allowed=True)

        if mutation.mutation_type in ("add_edge", "remove_edge"):
            if subj_uuid is None:
                return GuardrailResult(
                    allowed=False,
                    reason=f"Subject node '{mutation.node_name or str(subj_uuid)}' does not exist in the Knowledge Graph.",
                    required_revisions=["Ensure the subject entity exists before creating this relationship."],
                )
            if obj_uuid is None:
                return GuardrailResult(
                    allowed=False,
                    reason=f"Object node '{mutation.target_name or str(obj_uuid)}' does not exist in the Knowledge Graph.",
                    required_revisions=["Ensure the target entity exists before creating this relationship."],
                )
            return GuardrailResult(allowed=True)

        # For remove_node, set_attribute, add_tag, etc. — subject must exist
        if mutation.mutation_type in (
            "remove_node",
            "set_attribute",
            "add_tag",
            "remove_tag",
            "set_immutable",
        ):
            if subj_uuid is None:
                return GuardrailResult(
                    allowed=False,
                    reason=f"Node '{mutation.node_name or str(subj_uuid)}' does not exist.",
                    required_revisions=[f"Cannot modify non-existent node '{mutation.node_name}'."],
                )

        return GuardrailResult(allowed=True)

    def validate_narrative_claim(
        self, narrative_text: str, ctx: Dict[str, Any]
    ) -> GuardrailResult:
        """
        Parse [[Wikilinks]] from narrative text and verify claimed relationships.

        This is a conservative check — it only flags PROVEN violations:
        1. Extract all [[Wikilinks]] from narrative
        2. For each, verify it corresponds to an actual node in KG
        3. (Phase 2) Simple heuristic: does the entity name at least exist?

        More sophisticated SVO (subject-verb-object) parsing can be added in Phase 3+.
        """
        if not narrative_text:
            return GuardrailResult(allowed=True)

        # Extract [[Wikilinks]]
        wikilinks = re.findall(r"\[\[([^\]]+)\]\]", narrative_text)
        if not wikilinks:
            return GuardrailResult(allowed=True)

        unknown_entities = []
        for link in wikilinks:
            name = link.strip()
            if not self.kg.get_node_by_name(name):
                unknown_entities.append(name)

        if unknown_entities:
            return GuardrailResult(
                allowed=False,
                reason=f"Narrative references entities not in the Knowledge Graph: {', '.join(unknown_entities)}.",
                required_revisions=[
                    f"Remove or replace references to {', '.join(unknown_entities)}. "
                    "Use only entities established in the campaign."
                ],
            )

        return GuardrailResult(allowed=True)

    # ------------------------------------------------------------------
    # Gap 4: SVO (Subject-Verb-Object) claim validation
    # ------------------------------------------------------------------
    # Verb patterns that imply KG relationship transfers or changes.
    # Maps natural-language verbs to the GraphPredicate that would need to exist.
    _TRANSFER_VERBS = {
        "gives": GraphPredicate.POSSESSES,
        "gives you": GraphPredicate.POSSESSES,
        "hands": GraphPredicate.POSSESSES,
        "hands you": GraphPredicate.POSSESSES,
        "presses": GraphPredicate.POSSESSES,
        "grants": GraphPredicate.POSSESSES,
        "offers": GraphPredicate.POSSESSES,
        "bequeaths": GraphPredicate.POSSESSES,
        "transfers": GraphPredicate.POSSESSES,
        "takes": GraphPredicate.POSSESSES,
        "steals": GraphPredicate.POSSESSES,
        "betrays": GraphPredicate.HOSTILE_TOWARD,
        "turns on": GraphPredicate.HOSTILE_TOWARD,
        "joins": GraphPredicate.MEMBER_OF,
        "leaves": GraphPredicate.MEMBER_OF,
        "becomes ally": GraphPredicate.ALLIED_WITH,
        "allies with": GraphPredicate.ALLIED_WITH,
        "attacks": GraphPredicate.HOSTILE_TOWARD,
        "kills": None,
        "slays": None,
        "destroys": None,
    }

    def _extract_svo_triples(self, text: str) -> List[Tuple[str, str, str]]:
        """
        Extract (subject, verb, object) triples from narrative text.

        Approach:
        1. For each known transfer verb, find all occurrences
        2. For each occurrence, find the sentence bounds (between punctuation)
        3. Collect Wikilinks in that sentence
        4. Pair Wikilinks before verb as subject, Wikilinks after as object
           If only one Wikilink exists, record it as the object (subject="")
        """
        triples = []

        wikilinks: List[Tuple[int, int, str]] = []
        for m in re.finditer(r"\[\[([^\]]+)\]\]", text):
            wikilinks.append((m.start(), m.end(), m.group(1).strip()))

        if not wikilinks:
            return []

        wl_starts = {s for s, e, n in wikilinks}
        wl_ends = {e for s, e, n in wikilinks}

        for verb_phrase in self._TRANSFER_VERBS:
            for m in re.finditer(rf"\b{re.escape(verb_phrase)}\b", text, re.IGNORECASE):
                verb_start, verb_end = m.start(), m.end()

                # Find sentence boundaries: before the verb, after the verb
                # Look backward for [.!?] from verb_start
                sent_start = 0
                for i in range(verb_start - 1, max(verb_start - 200, -1), -1):
                    if text[i] in '.!?':
                        sent_start = i + 1
                        break
                # Look forward for [.!?] from verb_end
                sent_end = len(text)
                for i in range(verb_end, min(verb_end + 200, len(text))):
                    if text[i] in '.!?':
                        sent_end = i + 1
                        break

                # Filter wikilinks to this sentence
                in_sent = [(s, e, n) for s, e, n in wikilinks
                            if s >= sent_start and e <= sent_end]

                # Classify by position relative to verb (using original text positions)
                before = [n for s, e, n in in_sent if e <= verb_start]
                after = [n for s, e, n in in_sent if s >= verb_end]

                if after:
                    # Object Wikilink found (appears during or after the verb)
                    obj = after[0]
                    subj = before[-1] if before else ""
                    triples.append((subj, verb_phrase, obj))

        return triples

    def _mutation_covers_svo(
        self,
        svo: Tuple[str, str, str],
        proposed_mutations: List[GraphMutation],
    ) -> bool:
        """
        Check if any proposed mutation accounts for the implied SVO relationship.
        A mutation 'covers' the triple if it mentions both the subject and the object.
        """
        subject, verb, obj = svo
        if not subject or not obj:
            return False

        for mutation in proposed_mutations:
            mut_node = (mutation.node_name or "").lower()
            mut_target = (mutation.target_name or "").lower()
            mut_attr = (mutation.attribute or "").lower()

            # Check if both subject and object appear in mutation fields
            subject_present = (
                subject.lower() in mut_node
                or subject.lower() in mut_target
                or subject.lower() in mut_attr
            )
            obj_present = (
                obj.lower() in mut_node
                or obj.lower() in mut_target
                or obj.lower() in mut_attr
            )

            if subject_present and obj_present:
                return True

        return False

    def validate_svo_claims(
        self,
        narrative_text: str,
        proposed_mutations: List[GraphMutation],
        ctx: Dict[str, Any],
    ) -> GuardrailResult:
        """
        Gap 4 fix: Detect narrative-implied world state changes and verify
        that corresponding mutations have been emitted.

        For example:
          "King Aldric gives [[Excalibur]] to the party"
          → No mutation touching both King Aldric and Excalibur = REJECTED.

        Returns GuardrailResult.disallowed if any SVO claim lacks a covering mutation.
        """
        if not narrative_text:
            return GuardrailResult(allowed=True)

        triples = self._extract_svo_triples(narrative_text)
        if not triples:
            return GuardrailResult(allowed=True)

        if not proposed_mutations:
            return GuardrailResult(
                allowed=False,
                reason=(
                    f"Narrative describes a world state change but no graph mutations were proposed. "
                    f"Implied changes: {', '.join(f'{s} {v} {o}' for s, v, o in triples)}."
                ),
                required_revisions=[
                    "Emit the appropriate graph mutations (POSSESSES transfer, HOSTILE_TOWARD edge, etc.) "
                    "before describing the outcome in prose."
                ],
            )

        uncovered = []
        for svo in triples:
            if svo[0] and svo[2] and not self._mutation_covers_svo(svo, proposed_mutations):
                uncovered.append(svo)

        if uncovered:
            return GuardrailResult(
                allowed=False,
                reason=(
                    f"Narrative claims world state changes without corresponding mutations: "
                    f"{', '.join(f'{s} {v} {o}' for s, v, o in uncovered)}."
                ),
                required_revisions=[
                    f"Before writing '{uncovered[0][0]} {uncovered[0][1]} {uncovered[0][2]}', "
                    "emit the appropriate graph mutation."
                ],
            )

        return GuardrailResult(allowed=True)

    def validate_full_pipeline(
        self,
        narrative_text: str,
        proposed_mutations: List[GraphMutation],
        ctx: Dict[str, Any],
    ) -> GuardrailResult:
        """
        Run all hard guardrail checks.

        Returns the first rejection found (fail-fast), or allows if all pass.
        """
        # 1. Narrative claim check (Wikilinks only — entity existence)
        claim_result = self.validate_narrative_claim(narrative_text, ctx)
        if not claim_result.allowed:
            return claim_result

        # 2. Gap 4: SVO claim validation — cross-reference implied world changes vs mutations
        svo_result = self.validate_svo_claims(narrative_text, proposed_mutations, ctx)
        if not svo_result.allowed:
            return svo_result

        # 3. Validate each mutation
        for mutation in proposed_mutations:
            node_uuid = mutation._resolve_node_uuid(self.kg)

            # Immutable check
            if node_uuid:
                imm_result = self.validate_immutable_violation(node_uuid, mutation)
                if not imm_result.allowed:
                    return imm_result

            # Consistency check
            cons_result = self.validate_graph_consistency(mutation)
            if not cons_result.allowed:
                return cons_result

        return GuardrailResult(allowed=True)

    def check_storylet_integrity(
        self, storylet: Any, ctx: Dict[str, Any]
    ) -> GuardrailResult:
        """
        Validate that a storylet's content references entities that actually exist.
        Used when a storylet is being activated to prevent pre-existing invalid state.
        """
        return self.validate_narrative_claim(storylet.content, ctx)
