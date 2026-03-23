"""
In-memory directed labeled Knowledge Graph for the DM Engine.

Provides:
- KnowledgeGraphNode: typed entities (Location, NPC, Faction, Item, Deity, Player, Quest)
- KnowledgeGraphEdge: directed labeled relationships with weights
- KnowledgeGraph: CRUD + graph queries (node_exists, edge_exists, path finding, subgraph)

Persistence: WORLD_GRAPH.md YAML frontmatter via vault_io.py
No LLM calls — pure Python deterministic data structure.
"""

import uuid
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict
from pydantic import BaseModel, Field


class GraphNodeType(str, Enum):
    LOCATION = "location"
    NPC = "npc"
    FACTION = "faction"
    ITEM = "item"
    DEITY = "deity"
    PLAYER = "player"
    QUEST = "quest"


class GraphPredicate(str, Enum):
    # Spatial
    CONNECTED_TO = "connected_to"
    LOCATED_IN = "located_in"
    # Social / Faction
    MEMBER_OF = "member_of"
    ALLIED_WITH = "allied_with"
    HOSTILE_TOWARD = "hostile_toward"
    CONTROLS = "controls"
    LEADS = "leads"
    SERVES = "serves"
    RIVAL_OF = "rival_of"
    OWNED_BY = "owned_by"
    # Ownership / Desire
    POSSESSES = "possesses"
    WANTS = "wants"
    # Knowledge
    KNOWS_ABOUT = "knows_about"
    RULES = "rules"


class KnowledgeGraphNode(BaseModel):
    node_uuid: uuid.UUID = Field(default_factory=uuid.uuid4)
    node_type: GraphNodeType
    name: str
    attributes: Dict[str, Any] = Field(default_factory=dict)
    tags: Set[str] = Field(default_factory=set)
    is_immutable: bool = False
    # Gap 9: NPC behavioral dials extracted from biography text
    # e.g. {"greed": 0.8, "loyalty": 0.9, "courage": 0.4}
    npc_dials: Dict[str, float] = Field(default_factory=dict)

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def get_attribute(self, key: str, default: Any = None) -> Any:
        return self.attributes.get(key, default)


class KnowledgeGraphEdge(BaseModel):
    edge_uuid: uuid.UUID = Field(default_factory=uuid.uuid4)
    subject_uuid: uuid.UUID
    predicate: GraphPredicate
    object_uuid: uuid.UUID
    weight: float = 1.0
    # Secret edges are hidden from the narrator's GraphRAG context
    # but remain visible to storylets (DM-level knowledge).
    secret: bool = False


class KnowledgeGraph(BaseModel):
    """
    In-memory directed labeled graph.

    Stores nodes and edges with:
    - adjacency index: O(1) outgoing edge UUID lookup by predicate
    - edge_index: O(1) edge object lookup by (subject, predicate, object)
    Callers must call _rebuild_adjacency() after manually modifying .edges.
    """
    nodes: Dict[uuid.UUID, KnowledgeGraphNode] = Field(default_factory=dict)
    edges: List[KnowledgeGraphEdge] = Field(default_factory=list)

    adjacency: Dict[uuid.UUID, Dict[GraphPredicate, Set[uuid.UUID]]] = Field(
        default_factory=dict, exclude=True
    )
    name_index: Dict[str, uuid.UUID] = Field(default_factory=dict, exclude=True)
    # O(1) edge object lookup: (subj_uuid, pred, obj_uuid) -> KnowledgeGraphEdge
    edge_index: Dict[Tuple[uuid.UUID, GraphPredicate, uuid.UUID], KnowledgeGraphEdge] = Field(
        default_factory=dict, exclude=True
    )

    def model_post_init(self, __context) -> None:
        self._rebuild_adjacency()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _rebuild_adjacency(self) -> None:
        self.adjacency.clear()
        self.name_index.clear()
        self.edge_index.clear()
        for node in self.nodes.values():
            self.name_index[node.name.lower()] = node.node_uuid
        for edge in self.edges:
            if edge.subject_uuid not in self.adjacency:
                self.adjacency[edge.subject_uuid] = {}
            if edge.predicate not in self.adjacency[edge.subject_uuid]:
                self.adjacency[edge.subject_uuid][edge.predicate] = set()
            self.adjacency[edge.subject_uuid][edge.predicate].add(edge.object_uuid)
            self.edge_index[(edge.subject_uuid, edge.predicate, edge.object_uuid)] = edge

    def _resolve_uuid(self, identifier: Optional[uuid.UUID | str], name: Optional[str]) -> Optional[uuid.UUID]:
        if identifier:
            return identifier if isinstance(identifier, uuid.UUID) else uuid.UUID(identifier)
        if name:
            return self.name_index.get(name.lower())
        return None

    # ------------------------------------------------------------------
    # Node CRUD
    # ------------------------------------------------------------------
    def add_node(self, node: KnowledgeGraphNode) -> None:
        self.nodes[node.node_uuid] = node
        self.name_index[node.name.lower()] = node.node_uuid
        # No edges to rebuild yet; adjacency entries for this node will be added when edges are added

    def remove_node(self, node_uuid: uuid.UUID) -> None:
        node = self.nodes.pop(node_uuid, None)
        if node:
            self.name_index.pop(node.name.lower(), None)
        # Remove all edges referencing this node
        self.edges = [
            e for e in self.edges if e.subject_uuid != node_uuid and e.object_uuid != node_uuid
        ]
        self.adjacency.pop(node_uuid, None)
        for pred_dict in self.adjacency.values():
            for obj_set in pred_dict.values():
                obj_set.discard(node_uuid)

    def get_node(self, node_uuid: uuid.UUID) -> Optional[KnowledgeGraphNode]:
        return self.nodes.get(node_uuid)

    def get_node_by_name(self, name: str) -> Optional[KnowledgeGraphNode]:
        uid = self.name_index.get(name.lower())
        return self.nodes.get(uid) if uid else None

    def find_node_uuid(self, name: str) -> Optional[uuid.UUID]:
        return self.name_index.get(name.lower())

    # ------------------------------------------------------------------
    # Edge CRUD
    # ------------------------------------------------------------------
    def add_edge(self, edge: KnowledgeGraphEdge) -> None:
        if edge.subject_uuid not in self.adjacency:
            self.adjacency[edge.subject_uuid] = {}
        if edge.predicate not in self.adjacency[edge.subject_uuid]:
            self.adjacency[edge.subject_uuid][edge.predicate] = set()
        self.adjacency[edge.subject_uuid][edge.predicate].add(edge.object_uuid)
        self.edge_index[(edge.subject_uuid, edge.predicate, edge.object_uuid)] = edge
        self.edges.append(edge)

    def remove_edge(self, edge_uuid: uuid.UUID) -> None:
        edge = next((e for e in self.edges if e.edge_uuid == edge_uuid), None)
        if edge:
            self.edges.remove(edge)
            self.edge_index.pop((edge.subject_uuid, edge.predicate, edge.object_uuid), None)
            if (
                edge.subject_uuid in self.adjacency
                and edge.predicate in self.adjacency[edge.subject_uuid]
            ):
                self.adjacency[edge.subject_uuid][edge.predicate].discard(edge.object_uuid)

    def get_neighbors(
        self, node_uuid: uuid.UUID, predicate: Optional[GraphPredicate] = None
    ) -> Set[uuid.UUID]:
        if node_uuid not in self.adjacency:
            return set()
        if predicate:
            return self.adjacency[node_uuid].get(predicate, set())
        # Return all neighbors across all predicates
        result: Set[uuid.UUID] = set()
        for obj_set in self.adjacency[node_uuid].values():
            result.update(obj_set)
        return result

    # ------------------------------------------------------------------
    # Graph queries
    # ------------------------------------------------------------------
    def query_nodes(
        self,
        node_type: Optional[GraphNodeType] = None,
        tags: Optional[Set[str]] = None,
        attributes_filter: Optional[Dict[str, Any]] = None,
    ) -> List[KnowledgeGraphNode]:
        results = []
        for node in self.nodes.values():
            if node_type is not None and node.node_type != node_type:
                continue
            if tags and not tags.intersection(node.tags):
                continue
            if attributes_filter:
                mismatch = any(
                    node.attributes.get(k) != v for k, v in attributes_filter.items()
                )
                if mismatch:
                    continue
            results.append(node)
        return results

    def query_edges(
        self,
        subject_uuid: Optional[uuid.UUID] = None,
        predicate: Optional[GraphPredicate] = None,
        object_uuid: Optional[uuid.UUID] = None,
    ) -> List[KnowledgeGraphEdge]:
        results = []
        for edge in self.edges:
            if subject_uuid is not None and edge.subject_uuid != subject_uuid:
                continue
            if predicate is not None and edge.predicate != predicate:
                continue
            if object_uuid is not None and edge.object_uuid != object_uuid:
                continue
            results.append(edge)
        return results

    def edge_exists(
        self, subject_uuid: uuid.UUID, predicate: GraphPredicate, object_uuid: uuid.UUID
    ) -> bool:
        return (
            subject_uuid in self.adjacency
            and predicate in self.adjacency[subject_uuid]
            and object_uuid in self.adjacency[subject_uuid][predicate]
        )

    def get_edge(
        self, subject_uuid: uuid.UUID, predicate: GraphPredicate, object_uuid: uuid.UUID
    ) -> Optional[KnowledgeGraphEdge]:
        """O(1) edge lookup using the edge index. Returns None if not found."""
        return self.edge_index.get((subject_uuid, predicate, object_uuid))

    def node_exists_by_name(self, name: str, node_type: Optional[GraphNodeType] = None) -> bool:
        node = self.get_node_by_name(name)
        if not node:
            return False
        if node_type is not None and node.node_type != node_type:
            return False
        return True

    def find_path(
        self, start_uuid: uuid.UUID, end_uuid: uuid.UUID, max_hops: int = 3
    ) -> Optional[List[uuid.UUID]]:
        """BFS path finding. Returns list of node UUIDs from start to end, or None."""
        if start_uuid == end_uuid:
            return [start_uuid]
        from collections import deque

        queue: deque[tuple[uuid.UUID, list[uuid.UUID]]] = deque()
        queue.append((start_uuid, [start_uuid]))
        visited: Set[uuid.UUID] = {start_uuid}

        while queue:
            current, path = queue.popleft()
            if len(path) > max_hops:
                continue
            for neighbor in self.get_neighbors(current):
                if neighbor == end_uuid:
                    return path + [neighbor]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return None

    def get_subgraph(self, center_uuid: uuid.UUID, radius: int = 2) -> "KnowledgeGraph":
        """
        Returns a new KnowledgeGraph containing all nodes within `radius` hops of center_uuid,
        and all edges between them.
        """
        from collections import deque

        visited: Set[uuid.UUID] = {center_uuid}
        queue: deque[tuple[uuid.UUID, int]] = deque()
        queue.append((center_uuid, 0))

        while queue:
            current, depth = queue.popleft()
            if depth >= radius:
                continue
            for neighbor in self.get_neighbors(current):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))

        # Build subgraph
        subgraph_nodes = {uid: self.nodes[uid] for uid in visited if uid in self.nodes}
        subgraph_edges = [
            e
            for e in self.edges
            if e.subject_uuid in visited and e.object_uuid in visited
        ]

        kg = KnowledgeGraph(nodes=subgraph_nodes, edges=subgraph_edges)
        kg._rebuild_adjacency()
        return kg

    def get_context_for_node(
        self, node_uuid: uuid.UUID, max_hops: int = 2, hide_secrets: bool = True
    ) -> Dict[str, Any]:
        """
        Returns a dict with the node's own data plus its immediate neighborhood.
        Used for GraphRAG-style context windows.

        Args:
            node_uuid: The node to get context for.
            max_hops: Unused but kept for API compatibility.
            hide_secrets: If True, secret edges are excluded from neighbors.
                          The narrator's GraphRAG context passes True (secrets hidden).
                          Storylet evaluation passes False (all edges visible).
        """
        node = self.get_node(node_uuid)
        if not node:
            return {}
        neighbors: Dict[str, List[str]] = defaultdict(list)

        # Outgoing edges: use adjacency index + get_edge for O(1) secret flag check
        for pred, obj_uuids in self.adjacency.get(node_uuid, {}).items():
            for obj_uuid in obj_uuids:
                edge_obj = self.get_edge(node_uuid, pred, obj_uuid)
                if edge_obj is None:
                    continue
                if hide_secrets and edge_obj.secret:
                    continue
                target = self.get_node(obj_uuid)
                if target:
                    neighbors[pred.value].append(target.name)

        # Incoming edges: scan adjacency entries for this node as object
        for subj_uuid, pred_dict in self.adjacency.items():
            for pred, obj_uuids in pred_dict.items():
                if node_uuid in obj_uuids:
                    edge_obj = self.get_edge(subj_uuid, pred, node_uuid)
                    if edge_obj is None:
                        continue
                    if hide_secrets and edge_obj.secret:
                        continue
                    subject = self.get_node(subj_uuid)
                    if subject:
                        neighbors[f"reverse_{pred.value}"].append(subject.name)

        return {
            "node_uuid": str(node_uuid),
            "name": node.name,
            "node_type": node.node_type.value,
            "attributes": node.attributes,
            "tags": list(node.tags),
            "is_immutable": node.is_immutable,
            "neighbors": dict(neighbors),
        }

