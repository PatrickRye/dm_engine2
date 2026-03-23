"""
MutationManager — extracted from graph.py pending_mutations flow.

Responsibilities (all deferred-mutation operations across the LangGraph nodes):
1. Accumulate mutations from tool-call interception  (action_logic_node)
2. Accumulate mutations from storylet effects         (drama_manager_node)
3. Validate pending mutations against KG + draft     (narrator_node)
4. Detect mutation leaks before QA                  (qa_node)
5. Execute mutations after QA approval               (commit_node)
6. Detect stale mutations on new HumanMessage        (clear_mutations_node)
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from knowledge_graph import KnowledgeGraph, GraphPredicate


class MutationManager:
    """
    Stateless convenience layer over the pending_mutations list in DMState.

    Each method corresponds to one location in the graph where mutations
    are touched, replacing inline logic with a named operation.
    """

    def accumulate_from_tool_calls(
        self, existing: List[dict], mutation_data: List[dict]
    ) -> List[dict]:
        """Merge new mutations from a tool call into the existing pending list."""
        if not mutation_data:
            return existing
        return existing + mutation_data

    def accumulate_from_storylet_effects(
        self, existing: List[dict], selected: Any, vault_path: str
    ) -> List[dict]:
        """
        Collect witness-events from a selected storylet's effects and convert
        them to pending mutations.
        """
        pending: List[dict] = []
        for effect in selected.effects:
            for event_id in effect.witness_events:
                # Import here to avoid circular imports at module level
                from registry import get_all_entities

                entities = get_all_entities(vault_path)
                for ent in entities.values():
                    if getattr(ent, "name", None):
                        pending.append({
                            "mutation_type": "add_witness",
                            "node_name": ent.name,
                            "value": event_id,
                        })
        if not pending:
            return existing
        return existing + pending

    def validate_pending(
        self,
        pending: List[dict],
        draft: str,
        kg: KnowledgeGraph,
        ctx: Dict[str, Any],
    ) -> Tuple["HardGuardrails", "GuardrailResult"]:
        """
        Run full guardrail validation on pending mutations + draft.
        Returns (guardrails_instance, result) so callers can inspect details.
        """
        from storylet import GraphMutation
        from hard_guardrails import HardGuardrails

        guardrails = HardGuardrails(kg)
        mutations = [GraphMutation(**m) for m in pending] if pending else []
        result = guardrails.validate_full_pipeline(draft, mutations, ctx)
        return guardrails, result

    def detect_leak(
        self, pending: List[dict], revision_count: int
    ) -> Optional[str]:
        """
        Check whether mutations reached QA without being executed.
        Returns an error message if a leak is detected, else None.
        """
        MAX_QA_REVISIONS = 7  # matches graph.py constant
        if pending and (revision_count + 1) < MAX_QA_REVISIONS:
            return (
                f"[MUTATION LEAK DETECTED]: {len(pending)} deferred mutations "
                "were not executed before reaching QA. This is an engine error. "
                "The narrator must execute pending mutations before QA review."
            )
        return None

    async def execute_pending(
        self,
        pending: List[dict],
        kg: KnowledgeGraph,
        vault_path: str,
        draft_llm: Any,
        narrative_context: str,
    ) -> Tuple[List[str], List[str]]:
        """
        Execute all deferred mutations after QA approval.

        Returns (mutation_errors, newly_created_entity_names).
        Performs attitude-edge auto-update when disposition/faction_standing changes.
        Calls EmergentWorldBuilder for newly created entities.
        """
        from storylet import GraphMutation
        from ingestion_pipeline import EmergentWorldBuilder

        mutation_errors: List[str] = []
        newly_created_entities: List[str] = []
        committed = 0

        # Snapshot party UUID for attitude-edge auto-update
        party_uuid = kg.find_node_uuid("The Party") or kg.find_node_uuid("Party")

        for mdict in pending:
            try:
                mutation = GraphMutation(**mdict)
                mutation.execute(kg)
                committed += 1

                if mutation.mutation_type == "add_node" and mutation.node_name:
                    newly_created_entities.append(mutation.node_name)

                # Auto-update attitude edges on disposition/standing changes
                if mutation.mutation_type == "set_attribute" and mutation.attribute in (
                    "disposition_toward_party",
                    "faction_standing",
                ):
                    node_uuid = mutation._resolve_node_uuid(kg)
                    node = kg.get_node(node_uuid) if node_uuid else None
                    if not node:
                        continue

                    if mutation.attribute == "disposition_toward_party":
                        new_val = mutation.value
                        old_val = node.attributes.get("disposition_toward_party", 50)
                        self._update_attitude_edge(kg, node, old_val, new_val, party_uuid)
                    elif mutation.attribute == "faction_standing":
                        new_standing = mutation.value or {}
                        old_standing = node.attributes.get("faction_standing", {})
                        old_val = old_standing.get("party", 50)
                        new_val = new_standing.get("party", old_val)
                        self._update_attitude_edge(kg, node, old_val, new_val, party_uuid)

            except Exception as e:
                err_msg = (
                    f"Mutation execution failed: {mdict.get('mutation_type')} "
                    f"on {mdict.get('node_name')}: {e}"
                )
                mutation_errors.append(err_msg)
                await self._audit(
                    vault_path, "CommitNode", "Mutation Execution Error", err_msg
                )

        if committed:
            await self._audit(
                vault_path,
                "CommitNode",
                "Mutations Committed",
                f"{committed}/{len(pending)} deferred mutations executed after QA approval.",
            )

        # Invalidate GraphRAG cache since KG was modified
        from graph import _invalidate_grag_cache

        _invalidate_grag_cache(vault_path)

        # Emergent Worldbuilding: flesh out newly created entities
        for entity_name in newly_created_entities:
            try:
                builder = EmergentWorldBuilder(llm=draft_llm, vault_path=vault_path)
                report = await builder.on_entity_created(
                    entity_name=entity_name,
                    context=narrative_context,
                )
                await self._audit(
                    vault_path,
                    "EmergentWorldBuilder",
                    "Entity Hydrated",
                    f"Entity '{entity_name}' fleshed out: "
                    f"{report.storylets_created} storylets, "
                    f"{report.edges_created} edges created."
                    + (f" Warnings: {report.warnings}" if report.warnings else ""),
                )
            except Exception as e:
                await self._audit(
                    vault_path,
                    "EmergentWorldBuilder",
                    "Hydration Skipped",
                    f"Could not hydrate '{entity_name}': {e}",
                )

        return mutation_errors, newly_created_entities

    def should_clear_on_human_message(self, state: Dict[str, Any]) -> bool:
        """
        Detect a new player turn: pending_mutations from a QA-rejected previous
        turn are now stale and should be cleared.
        """
        last_msg = state.get("messages", [{}])[-1]
        # HumanMessage is from langchain_core.messages; check by name to avoid import cycle
        return (
            type(last_msg).__name__ == "HumanMessage"
            and state.get("pending_mutations")
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_attitude_edge(
        self,
        kg: KnowledgeGraph,
        node: Any,
        old_val: float,
        new_val: float,
        party_uuid: Any,
    ) -> None:
        """Auto-create/update HOSTILE_TOWARD or ALLIED_WITH edge on disposition change."""
        import uuid

        if party_uuid is None:
            return

        THRESHOLD = 30.0  # below this = hostile, above = friendly/neutral
        def _set_attitude(pred: GraphPredicate) -> None:
            # Remove both attitudes first
            for p in (GraphPredicate.HOSTILE_TOWARD, GraphPredicate.ALLIED_WITH):
                if kg.edge_exists(node.node_uuid, p, party_uuid):
                    kg.remove_edge(node.node_uuid, p, party_uuid)
            kg.add_edge(
                KnowledgeGraphEdge(
                    subject_uuid=node.node_uuid,
                    predicate=pred,
                    object_uuid=party_uuid,
                )
            )

        if new_val < THRESHOLD:
            _set_attitude(GraphPredicate.HOSTILE_TOWARD)
        elif new_val > 70.0:
            _set_attitude(GraphPredicate.ALLIED_WITH)
        else:
            # Back to neutral — remove attitude edge
            for p in (GraphPredicate.HOSTILE_TOWARD, GraphPredicate.ALLIED_WITH):
                if kg.edge_exists(node.node_uuid, p, party_uuid):
                    kg.remove_edge(node.node_uuid, p, party_uuid)

    async def _audit(
        self, vault_path: str, source: str, event: str, detail: str
    ) -> None:
        """Write an audit log entry."""
        from graph import write_audit_log

        await write_audit_log(vault_path, source, event, detail)
