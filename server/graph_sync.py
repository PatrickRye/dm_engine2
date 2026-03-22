"""
Graph sync: bridges the flat entity registry to the Knowledge Graph.

Provides:
- sync_registry_to_graph: populates KG from existing registry entities
- sync_graph_to_registry: writes KG changes back to entity attributes

Called during initialize_engine_from_vault and after major story beats.
No LLM calls — pure deterministic sync logic.
"""

import uuid
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from knowledge_graph import KnowledgeGraph, GraphNodeType, GraphPredicate, KnowledgeGraphNode, KnowledgeGraphEdge
    from registry import StoryletRegistry


def sync_registry_to_graph(vault_path: str, kg: Any) -> str:
    """
    Bridge the flat entity registry into the Knowledge Graph.

    For each Creature/PC/NPC in registry, create a corresponding KG node.
    Infer edges from entity attributes (faction membership → MEMBER_OF,
    location → LOCATED_IN, etc.).

    Returns a status message.
    """
    from registry import get_all_entities
    from dnd_rules_engine import Creature
    from knowledge_graph import KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge

    nodes_added = 0
    edges_added = 0

    for uid, entity in get_all_entities(vault_path).items():
        if not hasattr(entity, "entity_uuid"):
            continue

        # Determine node type from entity tags
        tags = getattr(entity, "tags", [])
        if "npc" in tags or "monster" in tags:
            node_type = GraphNodeType.NPC
        elif "pc" in tags or "player" in tags:
            node_type = GraphNodeType.PLAYER
        elif "faction" in tags:
            node_type = GraphNodeType.FACTION
        elif "location" in tags:
            node_type = GraphNodeType.LOCATION
        elif "deity" in tags:
            node_type = GraphNodeType.DEITY
        else:
            node_type = GraphNodeType.NPC  # Default

        # Extract attributes
        attributes = {
            "hp": getattr(entity, "hp", None),
            "ac": getattr(entity, "ac", None),
        }
        if hasattr(entity, "cr") and entity.cr:
            attributes["cr"] = entity.cr

        # Build node
        node = KnowledgeGraphNode(
            node_type=node_type,
            name=getattr(entity, "name", str(uid)),
            attributes=attributes,
            tags=set(tags),
        )
        # Check if node already exists by name
        existing = kg.get_node_by_name(node.name)
        if existing:
            # Update tags and attributes
            existing.tags.update(node.tags)
            existing.attributes.update(attributes)
        else:
            kg.add_node(node)
            nodes_added += 1

        # Infer edges
        # Faction membership
        faction = getattr(entity, "faction", None) or getattr(entity, "faction_name", None)
        if faction:
            faction_uid = kg.find_node_uuid(faction)
            if faction_uid:
                if not kg.edge_exists(node.node_uuid, GraphPredicate.MEMBER_OF, faction_uid):
                    kg.add_edge(
                        KnowledgeGraphEdge(
                            subject_uuid=node.node_uuid,
                            predicate=GraphPredicate.MEMBER_OF,
                            object_uuid=faction_uid,
                        )
                    )
                    edges_added += 1

        # Location (from entity's x,y position if it has a map)
        current_map = getattr(entity, "current_map", None)
        if current_map and hasattr(entity, "x") and hasattr(entity, "y"):
            # Look for a location node with matching map name
            map_node = kg.get_node_by_name(current_map)
            if map_node:
                if not kg.edge_exists(node.node_uuid, GraphPredicate.LOCATED_IN, map_node.node_uuid):
                    kg.add_edge(
                        KnowledgeGraphEdge(
                            subject_uuid=node.node_uuid,
                            predicate=GraphPredicate.LOCATED_IN,
                            object_uuid=map_node.node_uuid,
                        )
                    )
                    edges_added += 1

    # Gap 2 fix: Invalidate GraphRAG cache since KG was modified
    if nodes_added > 0 or edges_added > 0:
        try:
            from graph import _invalidate_grag_cache
            _invalidate_grag_cache(vault_path)
        except Exception:
            pass  # Best-effort: cache invalidation failure shouldn't break sync

    return f"MECHANICAL TRUTH: Synced registry to Knowledge Graph. Added {nodes_added} nodes, {edges_added} edges."


def sync_graph_to_registry(vault_path: str, kg: Any) -> str:
    """
    Write Knowledge Graph changes back to entity attributes.

    E.g., if an NPC's disposition changed in the KG (via a storylet effect),
    update their registry attributes.

    Returns a status message.
    """
    from registry import get_all_entities
    from knowledge_graph import GraphPredicate

    updated = 0

    for uid, entity in get_all_entities(vault_path).items():
        if not hasattr(entity, "entity_uuid"):
            continue

        entity_node_uuid = kg.find_node_uuid(getattr(entity, "name", ""))
        if not entity_node_uuid:
            continue

        # Check for disposition/attitude edges
        disposition_edges = kg.query_edges(subject_uuid=entity_node_uuid, predicate=GraphPredicate.HOSTILE_TOWARD)
        if disposition_edges:
            # Could update entity's hostility flag here
            updated += 1

        # Apply attribute changes tracked in KG back to entity
        node = kg.get_node(entity_node_uuid)
        if node and node.attributes:
            # Sync HP if KG has it and entity has hp attribute
            if hasattr(entity, "hp") and "hp" in node.attributes:
                hp_val = node.attributes["hp"]
                if hp_val is not None and hasattr(entity.hp, "base_value"):
                    entity.hp.base_value = hp_val
                    updated += 1

    return f"MECHANICAL TRUTH: Synced Knowledge Graph to registry. Updated {updated} entities."
