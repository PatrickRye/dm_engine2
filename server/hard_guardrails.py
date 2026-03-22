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
    # Gap 4 / Gap 5: SVO (Subject-Verb-Object) claim validation
    # ------------------------------------------------------------------
    # Verb patterns that imply KG relationship transfers or changes.
    # Maps natural-language verbs to the GraphPredicate that would need to exist.
    # None = no KG predicate (e.g., "kills"), but still a world-state claim.
    _TRANSFER_VERBS = {
        # Ownership / possession transfer
        "gives": GraphPredicate.POSSESSES,
        "gives you": GraphPredicate.POSSESSES,
        "hands": GraphPredicate.POSSESSES,
        "hands you": GraphPredicate.POSSESSES,
        "grants": GraphPredicate.POSSESSES,
        "offers": GraphPredicate.POSSESSES,
        "bequeaths": GraphPredicate.POSSESSES,
        "transfers": GraphPredicate.POSSESSES,
        "receives": GraphPredicate.POSSESSES,
        "loses": None,
        "takes": GraphPredicate.POSSESSES,
        "steals": GraphPredicate.POSSESSES,
        "pawns": GraphPredicate.POSSESSES,
        "awards": GraphPredicate.POSSESSES,
        "frees": GraphPredicate.POSSESSES,
        # Alliance / hostility transitions
        "betrays": GraphPredicate.HOSTILE_TOWARD,
        "turns on": GraphPredicate.HOSTILE_TOWARD,
        "renounces": GraphPredicate.HOSTILE_TOWARD,
        "joins": GraphPredicate.MEMBER_OF,
        "leaves": GraphPredicate.MEMBER_OF,
        "becomes ally": GraphPredicate.ALLIED_WITH,
        "allies with": GraphPredicate.ALLIED_WITH,
        "pledges to": GraphPredicate.ALLIED_WITH,
        "vows to": GraphPredicate.ALLIED_WITH,
        "attacks": GraphPredicate.HOSTILE_TOWARD,
        "assaults": GraphPredicate.HOSTILE_TOWARD,
        "murders": None,
        "kills": None,
        "slays": None,
        "destroys": None,
        "exiles": GraphPredicate.HOSTILE_TOWARD,
        "banishes": GraphPredicate.HOSTILE_TOWARD,
        "imprisons": GraphPredicate.HOSTILE_TOWARD,
    }

    def _extract_entity_names_from_text(self, text: str) -> List[str]:
        """
        Extract potential entity names from freeform prose (no Wikilinks needed).

        Strategy: capitalized multi-word sequences AND single capitalized words
        that correspond to KG node names. We scan the text for Title-Case
        sequences and filter out common non-entity words.
        """
        # Multi-word Title-Case: "King Aldric", "Queen Kaya", "Lord Varyk"
        multi_re = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")
        # Single capitalized word (min 3 chars): "Lyra", "Excalibur", "Aldric"
        single_re = re.compile(r"\b([A-Z][a-z]{2,})\b")

        candidates: List[Tuple[int, int, str]] = []
        for m in multi_re.finditer(text):
            candidates.append((m.start(), m.end(), m.group(1).strip()))
        # Add single words not already captured by multi-word regex
        for m in single_re.finditer(text):
            if not any(s == m.start() for s, _, _ in candidates):
                candidates.append((m.start(), m.end(), m.group(1).strip()))

        # Filter out common non-entity patterns (title-case titles, pronouns, etc.)
        filtered: List[Tuple[int, int, str]] = []
        for start, end, name in candidates:
            if name.lower() in (
                "the party", "the group", "the player", "the dm", "the dungeon master",
                "the narrator", "the dm", "you", "your", "i", "he", "she", "they",
                "it", "we", "us", "them", "his", "her", "their", "a", "an",
                "chapter", "scene", "session", "episode", "turn", "round",
                "the", "and", "but", "or", "so", "yet", "for", "nor",
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "king", "queen", "lord", "sir", "lady", "saint", "dragon",
            ):
                continue
            if len(name) < 2:
                continue
            filtered.append((start, end, name))

        # Cross-reference with KG: only keep candidates that match a KG node name
        matched: List[str] = []
        for start, end, name in filtered:
            if self.kg.get_node_by_name(name):
                matched.append(name)
            else:
                # Try case-insensitive match
                found = self.kg.get_node_by_name(name.lower())
                if found:
                    matched.append(found.name)  # Use KG's canonical casing

        return matched

    def _extract_svo_triples(self, text: str) -> List[Tuple[str, str, str]]:
        """
        Extract (subject, verb, object) triples from narrative text.

        Gap 5 improvement: works with both [[Wikilinks]] AND freeform entity names
        that match KG nodes. This means the SVO backstop catches prose like:
          "King Aldric gives Excalibur to the party"
        even without Wikilinks.

        Approach:
        1. Collect Wikilinks AND KG-matching freeform names from the text
        2. For each known transfer verb, find all occurrences
        3. For each occurrence, find the sentence bounds
        4. Pair entities before verb as subject, entities after as object
        """
        triples = []

        # Collect Wikilinks
        wikilinks: List[Tuple[int, int, str]] = []
        for m in re.finditer(r"\[\[([^\]]+)\]\]", text):
            wikilinks.append((m.start(), m.end(), m.group(1).strip()))

        # Collect KG-matching freeform entity names
        kg_names: List[Tuple[int, int, str]] = []
        for name in self._extract_entity_names_from_text(text):
            node = self.kg.get_node_by_name(name)
            if not node:
                continue
            # Find all occurrences of this name in text
            for m in re.finditer(rf"\b{re.escape(name)}\b", text):
                kg_names.append((m.start(), m.end(), name))

        if not wikilinks and not kg_names:
            return []

        # Merge and deduplicate (prefer Wikilinks for canonical casing)
        wikilink_names = {n for _, _, n in wikilinks}
        all_entities: List[Tuple[int, int, str]] = []
        for t in wikilinks:
            all_entities.append(t)
        for t in kg_names:
            # Don't duplicate if a Wikilink with the same canonical name exists
            if t[2] not in wikilink_names:
                all_entities.append(t)

        all_entities.sort(key=lambda x: x[0])

        for verb_phrase in self._TRANSFER_VERBS:
            for m in re.finditer(rf"\b{re.escape(verb_phrase)}\b", text, re.IGNORECASE):
                verb_start, verb_end = m.start(), m.end()

                # Find sentence boundaries
                sent_start = 0
                for i in range(verb_start - 1, max(verb_start - 200, -1), -1):
                    if text[i] in '.!?':
                        sent_start = i + 1
                        break
                sent_end = len(text)
                for i in range(verb_end, min(verb_end + 200, len(text))):
                    if text[i] in '.!?':
                        sent_end = i + 1
                        break

                # Filter entities to this sentence
                in_sent = [(s, e, n) for s, e, n in all_entities
                            if s >= sent_start and e <= sent_end]

                # Classify by position relative to verb
                before = [n for s, e, n in in_sent if e <= verb_start]
                after = [n for s, e, n in in_sent if s >= verb_end]

                if after:
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

        For most verbs: mutation must mention both subject AND object.
        For transfer verbs (POSSESSES, ALLIED_WITH, HOSTILE_TOWARD): mutation's
        node_name must match the transferred entity (SVO object) and predicate
        must match — the old owner (SVO subject) is implicitly removed from
        possession and needs no separate mutation.
        """
        subject, verb, obj = svo
        if not subject or not obj:
            return False

        transfer_predicate = self._TRANSFER_VERBS.get(verb)

        for mutation in proposed_mutations:
            mut_node = (mutation.node_name or "").lower()
            mut_target = (mutation.target_name or "").lower()
            mut_attr = (mutation.attribute or "").lower()

            # For transfer verbs (POSSESSES, ALLIED_WITH, HOSTILE_TOWARD):
            # - SVO subject = old owner/actor (implicit, no mutation needed)
            # - SVO object = transferred entity (must be in node_name)
            # - predicate must match the transfer type
            if transfer_predicate is not None:
                pred = (mutation.predicate or "").lower()
                pred_matches = pred == transfer_predicate.value.lower()

                if transfer_predicate == GraphPredicate.POSSESSES:
                    # Transfer: node_name = transferred entity (SVO object)
                    if obj.lower() in mut_node and pred_matches:
                        return True
                elif transfer_predicate == GraphPredicate.MEMBER_OF:
                    # Membership: node_name = joiner (SVO subject)
                    if subject.lower() in mut_node and pred_matches:
                        return True
                elif transfer_predicate in (
                    GraphPredicate.ALLIED_WITH,
                    GraphPredicate.HOSTILE_TOWARD,
                ):
                    # Alliance/hostility: node_name = actor (SVO subject),
                    # target_name = target (SVO object)
                    if (subject.lower() in mut_node and obj.lower() in mut_target
                            and pred_matches):
                        return True
                continue

            # For non-transfer verbs (None predicate like "kills"): both subject
            # and object must appear in mutation fields
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
        Gap 4 / Gap 5 fix: Detect narrative-implied world state changes and verify
        that corresponding mutations have been emitted.

        For example:
          "King Aldric gives [[Excalibur]] to the party"
          → No mutation touching both King Aldric and Excalibur = REJECTED.

        Also handles freeform prose (no Wikilinks required) as long as entity
        names match nodes in the KG.

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
            subj, verb, obj = svo
            if not subj or not obj:
                continue
            # Verbs with None predicate (kills, destroys) don't map to KG edges
            # but still imply a world-state claim — flag them as needing a mutation
            # if any proposed mutation touches the same entities
            if self._TRANSFER_VERBS.get(verb) is None:
                # No KG predicate, but verify at least one mutation mentions both entities
                if not self._mutation_covers_svo(svo, proposed_mutations):
                    uncovered.append(svo)
                continue
            if not self._mutation_covers_svo(svo, proposed_mutations):
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
        Run all hard guardrail checks and aggregate ALL failures into a single result.

        This replaces the previous fail-fast approach (return on first rejection).
        Returning all violations at once lets the LLM fix everything in one revision
        instead of cycling through one issue per QA loop.
        """
        all_reasons: List[str] = []
        all_revisions: List[str] = []

        # 1. Narrative claim check (Wikilinks only — entity existence)
        claim_result = self.validate_narrative_claim(narrative_text, ctx)
        if not claim_result.allowed:
            all_reasons.append(claim_result.reason)
            all_revisions.extend(claim_result.required_revisions)

        # 2. Gap 4 / Gap 5: SVO claim validation — cross-reference implied world changes vs mutations
        svo_result = self.validate_svo_claims(narrative_text, proposed_mutations, ctx)
        if not svo_result.allowed:
            all_reasons.append(svo_result.reason)
            all_revisions.extend(svo_result.required_revisions)

        # 3. Validate each mutation (collect all failures)
        for mutation in proposed_mutations:
            node_uuid = mutation._resolve_node_uuid(self.kg)

            # Immutable check
            if node_uuid:
                imm_result = self.validate_immutable_violation(node_uuid, mutation)
                if not imm_result.allowed:
                    all_reasons.append(imm_result.reason)
                    all_revisions.extend(imm_result.required_revisions)

            # Consistency check
            cons_result = self.validate_graph_consistency(mutation)
            if not cons_result.allowed:
                all_reasons.append(cons_result.reason)
                all_revisions.extend(cons_result.required_revisions)

        # 4. NPC disposition and faction standing consistency
        disp_result = self.validate_disposition_consistency(narrative_text, ctx)
        if not disp_result.allowed:
            all_reasons.append(disp_result.reason)
            all_revisions.extend(disp_result.required_revisions)

        if all_reasons:
            return GuardrailResult(
                allowed=False,
                reason="; ".join(all_reasons),
                required_revisions=all_revisions,
            )

        return GuardrailResult(allowed=True)

    # ------------------------------------------------------------------
    # Disposition / Faction Reputation consistency checks
    # ------------------------------------------------------------------
    # Descriptors that imply hostile disposition (NPC with disp > 30 shouldn't have these)
    _HOSTILE_DESCRIPTORS = {
        "snarls", "attacks", "draws weapon", "reaches for weapon",
        "refuses to speak", "spits at", "glares with hatred", "denounces",
        "curses", "draws a blade", "moves to strike", "raises arms to strike",
        "refuses your request", "turns you away", "denies you entry",
    }
    # Descriptors that imply friendly disposition (NPC with disp < 70 shouldn't have these)
    _FRIENDLY_DESCRIPTORS = {
        "smiles warmly", "greets you", "eagerly helps", "gladly offers",
        "waves you in", "embraces", "offers a drink", "extends hand",
        "welcomes you", "invites you", "thanks you profusely",
    }

    def validate_disposition_consistency(
        self, narrative_text: str, ctx: Dict[str, Any]
    ) -> GuardrailResult:
        """
        Check that narrative tone is consistent with NPC disposition and faction standing.

        An NPC with disposition_toward_party < 30 should not be described warmly.
        An NPC with disposition_toward_party > 70 should not be described hostilely.
        A faction with faction_standing["party"] < 30 should not be described as friendly.
        """
        if not narrative_text:
            return GuardrailResult(allowed=True)

        text_lower = narrative_text.lower()
        violations: List[str] = []

        # Check each NPC mentioned in prose
        wikilinks = re.findall(r"\[\[([^\]]+)\]\]", narrative_text)
        for link in wikilinks:
            name = link.strip()
            node = self.kg.get_node_by_name(name)
            if not node:
                continue

            # NPC disposition check
            if node.node_type.value == "npc":
                disp = node.attributes.get("disposition_toward_party", 50)
                if disp < 30:
                    # Hostile NPC: should not be described with hostile actions
                    # (A villain attacking is consistent; a villain welcoming players is also fine.
                    # The guardrail catches: hostile NPC described with hostile behavior
                    # that's inconsistent with their revealed disposition.)
                    for desc in self._HOSTILE_DESCRIPTORS:
                        if desc in text_lower:
                            violations.append(
                                f"[[{name}]] has disposition {disp} (HOSTILE) but narrative "
                                f"describes them with hostile action ('{desc}')."
                            )
                            break
                elif disp > 70:
                    # Friendly NPC: should not be described with friendly actions
                    # (A friend attacking is jarring. The guardrail catches: friendly NPC
                    # described with friendly behavior that's inconsistent.)
                    for desc in self._FRIENDLY_DESCRIPTORS:
                        if desc in text_lower:
                            violations.append(
                                f"[[{name}]] has disposition {disp} (FRIENDLY) but narrative "
                                f"describes them with friendly action ('{desc}')."
                            )
                            break

            # Faction standing check
            if node.node_type.value == "faction":
                standing_map = node.attributes.get("faction_standing", {})
                party_key = ctx.get("active_character", "party")
                standing = standing_map.get(party_key, standing_map.get("party", 50))
                if standing < 30:
                    # Hostile faction: reject hostile action descriptions
                    for desc in self._HOSTILE_DESCRIPTORS:
                        if desc in text_lower:
                            violations.append(
                                f"Faction [[{name}]] has standing {standing} (HOSTILE) but narrative "
                                f"describes them with hostile action ('{desc}')."
                            )
                            break
                elif standing > 70:
                    # Allied faction: reject friendly action descriptions
                    for desc in self._FRIENDLY_DESCRIPTORS:
                        if desc in text_lower:
                            violations.append(
                                f"Faction [[{name}]] has standing {standing} (ALLIED) but narrative "
                                f"describes them with friendly action ('{desc}')."
                            )
                            break

        if violations:
            revisions = [
                f"Rewrite to describe {name} with a tone consistent with their "
                f"current disposition/standing."
                for violation in violations
                for name in [violation.split("[[")[1].split("]]")[0]]
            ]
            return GuardrailResult(
                allowed=False,
                reason="; ".join(violations),
                required_revisions=revisions,
            )
        return GuardrailResult(allowed=True)

    def check_storylet_integrity(
        self, storylet: Any, ctx: Dict[str, Any]
    ) -> GuardrailResult:
        """
        Validate that a storylet's content references entities that actually exist.
        Used when a storylet is being activated to prevent pre-existing invalid state.
        """
        return self.validate_narrative_claim(storylet.content, ctx)
