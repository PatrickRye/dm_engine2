"""
MutationManager — extracted from graph.py pending_mutations flow.

Provides a stateful interface to the mutation lifecycle, decoupling it from DMState.
MutationManager takes a DMState dict and mutates it in-place for all mutation-related
fields. Graph nodes delegate to MutationManager methods rather than directly
accessing state["pending_mutations"], state["kg_snapshot"], state["mutation_errors"].

Lifecycle owned here:
  - snapshot():     capture kg_snapshot before QA
  - rollback():    restore KG from kg_snapshot on QA reject
  - commit():      execute mutations after QA approval
  - accumulate():  merge new mutations from tool calls / storylet effects
  - validate():    run guardrail validation and return (guardrails, result)
  - detect_leak(): check for mutation leaks before QA
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from knowledge_graph import KnowledgeGraph, GraphPredicate, KnowledgeGraphEdge


class MutationManager:
    """
    Stateful interface for the mutation lifecycle.

    Holds no state itself — all mutation lifecycle state lives in DMState.
    This class mutates DMState in-place for all mutation-related fields.

    Graph nodes call manager methods rather than directly manipulating:
      state["pending_mutations"], state["kg_snapshot"], state["mutation_errors"]
    """

    # ------------------------------------------------------------------
    # Lifecycle: snapshot / rollback / commit
    # ------------------------------------------------------------------

    def snapshot(self, state: Dict[str, Any], kg: KnowledgeGraph) -> None:
        """
        Capture a point-in-time snapshot of the KG into DMState.

        Called by narrator_node before QA. On QA reject, rollback() restores it.
        """
        state["kg_snapshot"] = kg.model_dump()
        state["mutation_errors"] = []

    def rollback(self, state: Dict[str, Any], kg: KnowledgeGraph) -> None:
        """
        Restore KG from DMState's kg_snapshot and clear pending mutations.

        Called by QA node when rejecting a draft.
        """
        snapshot = state.get("kg_snapshot")
        if snapshot:
            # Restore KG from snapshot fields
            for key, value in snapshot.items():
                if hasattr(kg, key):
                    setattr(kg, key, value)
        state["pending_mutations"] = []
        state["mutation_errors"] = []
        state["kg_snapshot"] = None

    async def commit(self, state: Dict[str, Any], kg: KnowledgeGraph, vault_path: str, draft_llm: Any) -> List[str]:
        """
        Execute all pending mutations after QA approval.

        Performs attitude-edge auto-update when disposition/faction_standing changes.
        Calls EmergentWorldBuilder for newly created entities.
        Clears pending_mutations and mutation_errors on success.
        Returns a list of error messages (empty = success).
        """
        from storylet import GraphMutation
        from ingestion_pipeline import EmergentWorldBuilder

        pending = state.get("pending_mutations", [])
        errors: List[str] = []
        newly_created: List[str] = []
        party_uuid = kg.find_node_uuid("The Party") or kg.find_node_uuid("Party")

        for mdict in list(pending):
            try:
                mutation = GraphMutation(**mdict)
                mutation.execute(kg)

                if mutation.mutation_type == "add_node" and mutation.node_name:
                    newly_created.append(mutation.node_name)

                if mutation.mutation_type == "set_attribute" and mutation.attribute in (
                    "disposition_toward_party",
                    "faction_standing",
                ):
                    node_uuid = mutation._resolve_node_uuid(kg)
                    node = kg.get_node(node_uuid) if node_uuid else None
                    if node:
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
                err_msg = f"Mutation execution failed: {mdict.get('mutation_type')} on {mdict.get('node_name')}: {e}"
                errors.append(err_msg)
                await self._audit(vault_path, "CommitNode", "Mutation Execution Error", err_msg)

        state["pending_mutations"] = []
        state["mutation_errors"] = errors
        state["kg_snapshot"] = None

        if pending:
            await self._audit(
                vault_path,
                "CommitNode",
                "Mutations Committed",
                f"{len(pending)} deferred mutations executed.",
            )

        # Invalidate GraphRAG cache since KG was modified
        from graph import _invalidate_grag_cache
        _invalidate_grag_cache(vault_path)

        # Emergent Worldbuilding
        for entity_name in newly_created:
            try:
                builder = EmergentWorldBuilder(llm=draft_llm, vault_path=vault_path)
                report = await builder.on_entity_created(entity_name=entity_name, context="")
                await self._audit(
                    vault_path,
                    "EmergentWorldBuilder",
                    "Entity Hydrated",
                    f"Entity '{entity_name}' fleshed out: "
                    f"{report.storylets_created} storylets, {report.edges_created} edges created."
                    + (f" Warnings: {report.warnings}" if report.warnings else ""),
                )
            except Exception as e:
                await self._audit(
                    vault_path,
                    "EmergentWorldBuilder",
                    "Hydration Skipped",
                    f"Could not hydrate '{entity_name}': {e}",
                )

        return errors

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def accumulate_from_tool_calls(self, state: Dict[str, Any], mutation_data: List[dict]) -> None:
        """Merge new mutations from a tool call into pending_mutations."""
        if mutation_data:
            existing = state.get("pending_mutations", [])
            state["pending_mutations"] = existing + mutation_data

    def accumulate_from_storylet_effects(self, state: Dict[str, Any], selected: Any, vault_path: str) -> bool:
        """
        Collect witness-events from a selected storylet's effects and convert them
        to pending mutations. Returns True if any mutations were added.
        """
        from registry import get_all_entities

        pending: List[dict] = []
        for effect in selected.effects:
            for event_id in effect.witness_events:
                entities = get_all_entities(vault_path)
                for ent in entities.values():
                    if getattr(ent, "name", None):
                        pending.append({
                            "mutation_type": "add_witness",
                            "node_name": ent.name,
                            "value": event_id,
                        })
        if pending:
            existing = state.get("pending_mutations", [])
            state["pending_mutations"] = existing + pending
            return True
        return False

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(
        self,
        state: Dict[str, Any],
        draft: str,
        kg: KnowledgeGraph,
        ctx: Dict[str, Any],
    ) -> Tuple["HardGuardrails", "GuardrailResult"]:
        """
        Run full guardrail validation on pending mutations + draft.

        Returns (guardrails_instance, result). The caller uses guard_result.reason
        and guard_result.required_revisions for feedback text.
        """
        from storylet import GraphMutation
        from hard_guardrails import HardGuardrails

        guardrails = HardGuardrails(kg)
        pending = state.get("pending_mutations", [])
        mutations = [GraphMutation(**m) for m in pending] if pending else []
        result = guardrails.validate_full_pipeline(draft, mutations, ctx)
        return guardrails, result

    def detect_leak(self, state: Dict[str, Any], revision_count: int) -> Optional[str]:
        """
        Check whether pending_mutations reached QA without being executed.
        Returns an error message if a leak is detected, else None.
        """
        MAX_QA_REVISIONS = 3
        pending = state.get("pending_mutations", [])
        if pending and (revision_count + 1) < MAX_QA_REVISIONS:
            return (
                f"[MUTATION LEAK DETECTED]: {len(pending)} deferred mutations "
                "were not executed before reaching QA. This is an engine error. "
                "The narrator must execute pending mutations before QA review."
            )
        return None

    def should_clear_on_human_message(self, state: Dict[str, Any]) -> bool:
        """
        Detect a new player turn: pending_mutations from a QA-rejected previous
        turn are now stale and should be cleared.
        """
        last_msg = state.get("messages", [{}])[-1]
        return (
            type(last_msg).__name__ == "HumanMessage"
            and state.get("pending_mutations")
        )

    def clear(self, state: Dict[str, Any]) -> None:
        """Clear all mutation state from DMState."""
        state["pending_mutations"] = []
        state["mutation_errors"] = []
        state["kg_snapshot"] = None

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
        if party_uuid is None:
            return

        THRESHOLD = 30.0
        old_state = "hostile" if old_val < THRESHOLD else ("friendly" if old_val > 70.0 else None)
        new_state = "hostile" if new_val < THRESHOLD else ("friendly" if new_val > 70.0 else None)
        if old_state == new_state:
            return

        def _set_attitude(pred: GraphPredicate) -> None:
            for p in (GraphPredicate.HOSTILE_TOWARD, GraphPredicate.ALLIED_WITH):
                if kg.edge_exists(node.node_uuid, p, party_uuid):
                    kg.remove_edge(node.node_uuid, p, party_uuid)
            kg.add_edge(
                KnowledgeGraphEdge(subject_uuid=node.node_uuid, predicate=pred, object_uuid=party_uuid)
            )

        if new_state is None:
            _set_attitude(GraphPredicate.HOSTILE_TOWARD)
        elif new_state == "hostile":
            _set_attitude(GraphPredicate.HOSTILE_TOWARD)
        else:
            _set_attitude(GraphPredicate.ALLIED_WITH)

    async def _audit(self, vault_path: str, source: str, event: str, detail: str) -> None:
        """Write an audit log entry."""
        from graph import write_audit_log
        await write_audit_log(vault_path, source, event, detail)
