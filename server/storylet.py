"""
Storylet data schema for the DM Engine's Graph-Grounded Storylet Orchestrator.

Provides:
- TensionLevel: LOW / MEDIUM / HIGH (dramatic pacing tiers)
- ComparisonOp: EQ / NE / GT / GTE / LT / LTE / IN / NOT_IN / HAS_TAG / NOT_HAS_TAG
- GraphQuery: deterministic prerequisite check against the Knowledge Graph
- GraphMutation: deterministic state change to the Knowledge Graph
- StoryletPrerequisites: AND/OR/NOT logical组合 of GraphQueries
- StoryletEffect: list of GraphMutations + flag/attribute changes
- Storylet: the core narrative unit

No LLM calls — pure Python deterministic models.
"""

import uuid
from enum import Enum
from typing import List, Dict, Any, Optional, Union, Set
from pydantic import BaseModel, Field

from knowledge_graph import KnowledgeGraph, KnowledgeGraphNode, GraphPredicate, GraphNodeType


class TensionLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ComparisonOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    HAS_TAG = "has_tag"
    NOT_HAS_TAG = "not_has_tag"


# ---------------------------------------------------------------------
# GraphQuery — deterministic prerequisite checks
# ---------------------------------------------------------------------
class GraphQuery(BaseModel):
    """
    A deterministic check evaluated against the Knowledge Graph + runtime context.

    query_type determines which evaluation path is used.
    One of node_uuid/name or node_name+node_type must be provided to identify the subject node.
    """

    query_type: str = Field(
        description=(
            "One of: 'node_exists', 'edge_exists', 'attribute_check', "
            "'path_exists', 'count_check', 'tag_check', 'engine_state_check'"
        )
    )
    # Node identification
    node_uuid: Optional[uuid.UUID] = None
    node_name: Optional[str] = None
    node_type: Optional[str] = None  # GraphNodeType value string
    # For edge_exists / path_exists
    predicate: Optional[str] = None
    target_uuid: Optional[uuid.UUID] = None
    target_name: Optional[str] = None
    # For attribute_check / count_check
    attribute: Optional[str] = None
    op: ComparisonOp = ComparisonOp.EQ
    value: Any = None

    def _resolve_node_uuid(self, kg: KnowledgeGraph) -> Optional[uuid.UUID]:
        if self.node_uuid:
            return self.node_uuid
        if self.node_name:
            # Try exact match first
            uid = kg.find_node_uuid(self.node_name)
            if uid:
                return uid
            # Fall back to search by name + type
            if self.node_type:
                try:
                    nt = GraphNodeType(self.node_type)
                    candidates = kg.query_nodes(node_type=nt)
                    for c in candidates:
                        if c.name.lower() == self.node_name.lower():
                            return c.node_uuid
                except ValueError:
                    pass
            return uid
        return None

    def _resolve_target_uuid(self, kg: KnowledgeGraph) -> Optional[uuid.UUID]:
        if self.target_uuid:
            return self.target_uuid
        if self.target_name:
            return kg.find_node_uuid(self.target_name)
        return None

    def evaluate(self, kg: KnowledgeGraph, ctx: Dict[str, Any]) -> bool:
        """Pure Python evaluation. Returns True/False."""
        try:
            if self.query_type == "node_exists":
                node_uuid = self._resolve_node_uuid(kg)
                if node_uuid is None:
                    return False
                node = kg.get_node(node_uuid)
                if node is None:
                    return False
                if self.node_type:
                    try:
                        if node.node_type != GraphNodeType(self.node_type):
                            return False
                    except ValueError:
                        return False
                return True

            if self.query_type == "edge_exists":
                subj_uuid = self._resolve_node_uuid(kg)
                obj_uuid = self._resolve_target_uuid(kg)
                if subj_uuid is None or obj_uuid is None:
                    return False
                if self.predicate:
                    pred = GraphPredicate(self.predicate)
                    return kg.edge_exists(subj_uuid, pred, obj_uuid)
                # Any edge between the two nodes
                return obj_uuid in kg.get_neighbors(subj_uuid)

            if self.query_type == "attribute_check":
                node_uuid = self._resolve_node_uuid(kg)
                if node_uuid is None:
                    return False
                node = kg.get_node(node_uuid)
                if node is None:
                    return False
                actual = node.attributes.get(self.attribute)
                return self._compare(actual, self.op, self.value)

            if self.query_type == "tag_check":
                node_uuid = self._resolve_node_uuid(kg)
                if node_uuid is None:
                    return False
                node = kg.get_node(node_uuid)
                if node is None:
                    return False
                if self.op == ComparisonOp.HAS_TAG:
                    return self.value in node.tags
                if self.op == ComparisonOp.NOT_HAS_TAG:
                    return self.value not in node.tags
                return False

            if self.query_type == "count_check":
                # Count edges or nodes matching criteria
                if self.predicate:
                    pred = GraphPredicate(self.predicate)
                    node_uuid = self._resolve_node_uuid(kg)
                    if node_uuid is None:
                        return False
                    count = len(kg.query_edges(subject_uuid=node_uuid, predicate=pred))
                    return self._compare(count, self.op, self.value)
                if self.node_type:
                    count = len(kg.query_nodes(node_type=GraphNodeType(self.node_type)))
                    return self._compare(count, self.op, self.value)
                return False

            if self.query_type == "path_exists":
                start_uuid = self._resolve_node_uuid(kg)
                end_uuid = self._resolve_target_uuid(kg)
                if start_uuid is None or end_uuid is None:
                    return False
                max_hops = int(self.value) if self.value else 3
                return kg.find_path(start_uuid, end_uuid, max_hops=max_hops) is not None

            if self.query_type == "engine_state_check":
                # Read runtime entity state (HP, conditions, resources) from the registry.
                # ctx["vault_path"] is required to look up entities.
                # node_name identifies the entity; attribute names the field to check.
                # Supported attributes: hp, max_hp, temp_hp, active_conditions, AC, etc.
                from registry import get_all_entities

                vault_path = ctx.get("vault_path", "default")
                entities = get_all_entities(vault_path)

                # Find the target entity by name
                target_name_lower = (self.node_name or "").lower()
                entity = None
                for ent in entities.values():
                    if getattr(ent, "name", "") and ent.name.lower() == target_name_lower:
                        entity = ent
                        break

                if entity is None:
                    return False

                attr = (self.attribute or "").lower()
                if attr == "hp":
                    actual = getattr(entity, "hp", None)
                    if hasattr(actual, "base_value"):
                        actual = actual.base_value
                    return self._compare(actual, self.op, self.value)
                if attr == "max_hp":
                    return self._compare(getattr(entity, "max_hp", 0), self.op, self.value)
                if attr == "temp_hp":
                    return self._compare(getattr(entity, "temp_hp", 0), self.op, self.value)
                if attr == "ac" or attr == "armor_class":
                    return self._compare(getattr(entity, "ac", 0), self.op, self.value)
                if attr == "active_conditions":
                    actual_conditions = [
                        c.name.lower() if hasattr(c, "name") else str(c).lower()
                        for c in getattr(entity, "active_conditions", [])
                    ]
                    if self.op == ComparisonOp.HAS_TAG:
                        return self.value.lower() in actual_conditions
                    if self.op == ComparisonOp.NOT_HAS_TAG:
                        return self.value.lower() not in actual_conditions
                    if self.op == ComparisonOp.IN:
                        return any(
                            cond in [v.lower() for v in (self.value or [])]
                            for cond in actual_conditions
                        )
                    if self.op == ComparisonOp.NOT_IN:
                        return all(
                            cond not in [v.lower() for v in (self.value or [])]
                            for cond in actual_conditions
                        )
                    return False
                # Fallback: try direct attribute access
                return self._compare(getattr(entity, attr, None), self.op, self.value)

            # --- NPC Disposition: disposition_toward_party attribute check ---
            if self.query_type == "disposition_check":
                node_uuid = self._resolve_node_uuid(kg)
                if node_uuid is None:
                    return False
                node = kg.get_node(node_uuid)
                if node is None:
                    return False
                disp = node.attributes.get("disposition_toward_party", 50)
                return self._compare(disp, self.op, self.value)

            # --- Faction Reputation: faction_standing["party"] attribute check ---
            if self.query_type == "faction_standing_check":
                node_uuid = self._resolve_node_uuid(kg)
                if node_uuid is None:
                    return False
                node = kg.get_node(node_uuid)
                if node is None or node.node_type != GraphNodeType.FACTION:
                    return False
                standing_map = node.attributes.get("faction_standing", {})
                # Support per-character tracking via ctx["active_character"]
                party_key = ctx.get("active_character", "party")
                standing = standing_map.get(party_key, standing_map.get("party", 50))
                return self._compare(standing, self.op, self.value)

            # --- Edge attribute check: for secret revelation prerequisites ---
            if self.query_type == "edge_attribute_check":
                subj_uuid = self._resolve_node_uuid(kg)
                obj_uuid = self._resolve_target_uuid(kg)
                if subj_uuid is None or obj_uuid is None:
                    return False
                pred = GraphPredicate(self.predicate) if self.predicate else None
                for edge in kg.edges:
                    if edge.subject_uuid != subj_uuid:
                        continue
                    if edge.object_uuid != obj_uuid:
                        continue
                    if pred is not None and edge.predicate != pred:
                        continue
                    # Found the matching edge — check the requested attribute
                    edge_attr_val = getattr(edge, self.attribute, None)
                    if edge_attr_val is None:
                        return False
                    return self._compare(edge_attr_val, self.op, self.value)
                return False

            return False
        except Exception:
            return False

    @staticmethod
    def _compare(actual: Any, op: ComparisonOp, expected: Any) -> bool:
        if op == ComparisonOp.EQ:
            return actual == expected
        if op == ComparisonOp.NE:
            return actual != expected
        if op in (ComparisonOp.GT,):
            return actual > expected
        if op in (ComparisonOp.GTE,):
            return actual >= expected
        if op in (ComparisonOp.LT,):
            return actual < expected
        if op in (ComparisonOp.LTE,):
            return actual <= expected
        if op == ComparisonOp.IN:
            return actual in expected if expected else False
        if op == ComparisonOp.NOT_IN:
            return actual not in expected if expected else True
        return False


# ---------------------------------------------------------------------
# GraphMutation — deterministic state changes
# ---------------------------------------------------------------------
class GraphMutation(BaseModel):
    """
    A deterministic state change to apply to the Knowledge Graph.
    One of node_uuid or node_name must be provided.
    """

    mutation_type: str = Field(
        description=(
            "One of: 'add_node', 'remove_node', 'add_edge', 'remove_edge', "
            "'set_attribute', 'add_tag', 'remove_tag', 'set_immutable', 'set_edge_attribute'"
        )
    )
    node_uuid: Optional[uuid.UUID] = None
    node_name: Optional[str] = None
    node_type: Optional[str] = None  # For add_node
    predicate: Optional[str] = None  # For add_edge/remove_edge
    target_uuid: Optional[uuid.UUID] = None
    target_name: Optional[str] = None  # For add_edge/remove_edge
    attribute: Optional[str] = None
    value: Any = None
    tags: Optional[List[str]] = None
    secret: bool = False  # For add_edge — sets edge.secret

    def _resolve_node_uuid(self, kg: KnowledgeGraph) -> Optional[uuid.UUID]:
        if self.node_uuid:
            return self.node_uuid
        if self.node_name:
            return kg.find_node_uuid(self.node_name)
        return None

    def _resolve_target_uuid(self, kg: KnowledgeGraph) -> Optional[uuid.UUID]:
        if self.target_uuid:
            return self.target_uuid
        if self.target_name:
            return kg.find_node_uuid(self.target_name)
        return None

    def execute(self, kg: KnowledgeGraph) -> None:
        """Pure Python execution. Raises on invalid mutation."""
        try:
            if self.mutation_type == "add_node":
                if not self.node_name:
                    raise ValueError("add_node requires node_name")
                node_type = GraphNodeType(self.node_type) if self.node_type else GraphNodeType.QUEST
                node = KnowledgeGraphNode(
                    node_type=node_type,
                    name=self.node_name,
                    attributes=self.value or {},
                    tags=set(self.tags or []),
                )
                kg.add_node(node)
                return

            if self.mutation_type == "remove_node":
                uid = self._resolve_node_uuid(kg)
                if uid:
                    kg.remove_node(uid)
                return

            if self.mutation_type == "add_edge":
                subj_uuid = self._resolve_node_uuid(kg)
                obj_uuid = self._resolve_target_uuid(kg)
                if not subj_uuid or not obj_uuid:
                    return
                pred = GraphPredicate(self.predicate) if self.predicate else GraphPredicate.CONNECTED_TO
                from knowledge_graph import KnowledgeGraphEdge
                edge = KnowledgeGraphEdge(
                    subject_uuid=subj_uuid,
                    predicate=pred,
                    object_uuid=obj_uuid,
                    secret=self.secret,
                )
                kg.add_edge(edge)
                return

            if self.mutation_type == "remove_edge":
                subj_uuid = self._resolve_node_uuid(kg)
                obj_uuid = self._resolve_target_uuid(kg)
                if not subj_uuid or not obj_uuid:
                    return
                pred = GraphPredicate(self.predicate) if self.predicate else None
                # Find and remove the matching edge
                for edge in list(kg.edges):
                    if (
                        edge.subject_uuid == subj_uuid
                        and (pred is None or edge.predicate == pred)
                        and edge.object_uuid == obj_uuid
                    ):
                        kg.remove_edge(edge.edge_uuid)
                        break
                return

            if self.mutation_type == "set_attribute":
                uid = self._resolve_node_uuid(kg)
                if not uid:
                    return
                node = kg.get_node(uid)
                if node:
                    node.attributes[self.attribute] = self.value
                return

            if self.mutation_type == "add_tag":
                uid = self._resolve_node_uuid(kg)
                if not uid:
                    return
                node = kg.get_node(uid)
                if node and self.value:
                    node.tags.add(self.value)
                return

            if self.mutation_type == "remove_tag":
                uid = self._resolve_node_uuid(kg)
                if not uid:
                    return
                node = kg.get_node(uid)
                if node and self.value:
                    node.tags.discard(self.value)
                return

            if self.mutation_type == "set_immutable":
                uid = self._resolve_node_uuid(kg)
                if not uid:
                    return
                node = kg.get_node(uid)
                if node:
                    node.is_immutable = bool(self.value)
                return

            if self.mutation_type == "set_edge_attribute":
                # Set an attribute on a specific edge (identified by subject + predicate + object).
                # Used to reveal secrets: set_edge_attribute(subject="Lord Vance", predicate="member_of",
                # object="The Cult", attribute="secret", value=False)
                subj_uuid = self._resolve_node_uuid(kg)
                obj_uuid = self._resolve_target_uuid(kg)
                if not subj_uuid or not obj_uuid:
                    return
                pred = GraphPredicate(self.predicate) if self.predicate else None
                for edge in kg.edges:
                    if edge.subject_uuid != subj_uuid:
                        continue
                    if edge.object_uuid != obj_uuid:
                        continue
                    if pred is not None and edge.predicate != pred:
                        continue
                    setattr(edge, self.attribute, self.value)
                    break
                return

        except Exception as e:
            # Mutations are deterministic — if they fail, we log and continue
            import sys
            print(f"[GraphMutation error] {self.mutation_type}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------
# StoryletPrerequisites — logical grouping
# ---------------------------------------------------------------------
class StoryletPrerequisites(BaseModel):
    all_of: List[GraphQuery] = Field(default_factory=list)
    any_of: List[GraphQuery] = Field(default_factory=list)
    none_of: List[GraphQuery] = Field(default_factory=list)

    def is_met(self, kg: KnowledgeGraph, ctx: Dict[str, Any]) -> bool:
        """
        True only if:
          - ALL of all_of are True
          - AND (ANY of any_of is True, OR any_of is empty)
          - AND NONE of none_of are True
        """
        for q in self.all_of:
            if not q.evaluate(kg, ctx):
                return False
        if self.any_of:
            if not any(q.evaluate(kg, ctx) for q in self.any_of):
                return False
        for q in self.none_of:
            if q.evaluate(kg, ctx):
                return False
        return True


# ---------------------------------------------------------------------
# StoryletEffect — outcome definitions
# ---------------------------------------------------------------------
class StoryletEffect(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    graph_mutations: List[GraphMutation] = Field(default_factory=list)
    flag_changes: Dict[str, bool] = Field(default_factory=dict)
    attribute_mods: Dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------
# Storylet — the core narrative unit
# ---------------------------------------------------------------------
class Storylet(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    description: str = ""
    tension_level: TensionLevel = TensionLevel.MEDIUM
    prerequisites: StoryletPrerequisites = Field(default_factory=StoryletPrerequisites)
    content: str = ""
    effects: List[StoryletEffect] = Field(default_factory=list)
    tags: Set[str] = Field(default_factory=set)
    max_occurrences: int = 1  # -1 = unlimited
    current_occurrences: int = 0
    is_active: bool = True
    priority_override: Optional[int] = None

    def can_fire(self, kg: KnowledgeGraph, ctx: Dict[str, Any]) -> bool:
        if not self.is_active:
            return False
        if self.max_occurrences > 0 and self.current_occurrences >= self.max_occurrences:
            return False
        return self.prerequisites.is_met(kg, ctx)
