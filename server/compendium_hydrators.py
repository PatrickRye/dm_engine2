"""
Compendium Hydration System — Parallel sub-hydrators coordinated by a single node.

Each sub-hydrator reads its system prompt from docs/*.md, processes raw D&D
compendium material (creatures, locations, factions, NPCs, maps, narratives, items),
writes outputs to the KG and vault, and returns a typed result.

The CompendiumHydrationCoordinator dispatches all sub-hydrators in parallel,
aggregates results, and handles partial failures.
"""

from __future__ import annotations

import os
import re
import uuid
import json
import asyncio
from typing import Any, Optional, List, Dict, TypedDict, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from knowledge_graph import KnowledgeGraphNode, KnowledgeGraphEdge
    from state import Storylet


# ---------------------------------------------------------------------------
# Input/Output Schemas
# ---------------------------------------------------------------------------


class CompendiumMaterials(TypedDict, total=False):
    """Top-level input for the compendium hydration coordinator."""

    creatures: str  # Raw creature stat blocks (multi-entry Markdown)
    locations: str  # Location descriptions
    factions: str  # Faction lore entries
    npcs: str  # NPC biographies + stat blocks
    maps: str  # Map descriptions (text fallback; images handled separately)
    campaign_narrative: str  # Campaign prose for storylets
    session_prep_notes: str  # Session prep for storylets
    storylet_resolutions: Dict[str, str]  # storylet_name -> resolution text
    items: str  # Spells, feats, items raw text


class CreatureHydrationResult(TypedDict):
    nodes_created: int
    edges_created: int
    entities_written: List[str]
    warnings: List[str]


class LocationHydrationResult(TypedDict):
    nodes_created: int
    edges_created: int
    warnings: List[str]


class FactionHydrationResult(TypedDict):
    nodes_created: int
    edges_created: int
    warnings: List[str]


class NPCHydrationResult(TypedDict):
    nodes_created: int
    edges_created: int
    entities_written: List[str]
    warnings: List[str]


class MapHydrationResult(TypedDict):
    nodes_created: int
    edges_created: int
    files_written: List[str]
    warnings: List[str]


class NarrativeHydrationResult(TypedDict):
    nodes_created: int
    edges_created: int
    storylets_created: int
    effects_annotated: int
    backup_storylets: int
    three_clue_violations: int
    warnings: List[str]


class CompendiumItemsResult(TypedDict):
    entries_saved: List[str]
    warnings: List[str]


class CompendiumHydrationReport(TypedDict):
    """Aggregated result from all sub-hydrators."""

    creatures: Optional[CreatureHydrationResult]
    locations: Optional[LocationHydrationResult]
    factions: Optional[FactionHydrationResult]
    npcs: Optional[NPCHydrationResult]
    maps: Optional[MapHydrationResult]
    narrative: Optional[NarrativeHydrationResult]
    items: Optional[CompendiumItemsResult]
    errors: List[str]
    partial_failures: List[str]


# ---------------------------------------------------------------------------
# Prompt loading utilities
# ---------------------------------------------------------------------------

_PROMPT_CACHE: Dict[str, str] = {}


def _load_prompt(prompt_name: str) -> str:
    """
    Load a hydrator's system prompt from docs/{PromptName}.md.
    Results are cached after first load.
    """
    if prompt_name in _PROMPT_CACHE:
        return _PROMPT_CACHE[prompt_name]

    # Navigate from server/ to project root
    server_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(server_dir)
    docs_dir = os.path.join(project_root, "docs")
    path = os.path.join(docs_dir, f"{prompt_name}.md")

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # Strip the # SYSTEM PROMPT header if present (prompt itself starts after it)
        lines = content.split("\n")
        # Find the first non-header line to use as prompt start
        prompt = content  # use full file as prompt
        _PROMPT_CACHE[prompt_name] = prompt
        return prompt

    _PROMPT_CACHE[prompt_name] = ""
    return ""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------


async def _call_llm_structured(
    llm: BaseChatModel,
    system_prompt: str,
    user_text: str,
    output_schema: type[BaseModel],
) -> Optional[BaseModel]:
    """Call LLM with a system prompt + user text and parse into output_schema."""
    from langchain_core.messages import HumanMessage, SystemMessage

    try:
        chain = llm.with_structured_output(output_schema)
        response = await chain.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
        )
        return response
    except Exception:
        return None


async def _call_llm_json(
    llm: BaseChatModel,
    system_prompt: str,
    user_text: str,
) -> Optional[Dict[str, Any]]:
    """Call LLM expecting a raw JSON object (dict) in response."""
    from langchain_core.messages import HumanMessage, SystemMessage

    try:
        response = await llm.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
        )
        content = response.content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        return json.loads(content)
    except Exception:
        return None


async def _call_llm_list(
    llm: BaseChatModel,
    system_prompt: str,
    user_text: str,
    item_schema: type[BaseModel],
) -> Optional[List[BaseModel]]:
    """Call LLM expecting a JSON array of schema objects."""

    class ListWrapper(BaseModel):
        items: List[item_schema] = Field(alias="items")

        model_config = {"populate_by_name": True}

    try:
        chain = llm.with_structured_output(ListWrapper)
        response = await chain.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
        )
        return response.items
    except Exception:
        return None


# ---------------------------------------------------------------------------
# KG write helpers
# ---------------------------------------------------------------------------


def _get_kg(vault_path: str):
    """Get the KG for a vault, creating if needed."""
    from registry import get_knowledge_graph, set_knowledge_graph

    kg = get_knowledge_graph(vault_path)
    if kg is None:
        from knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        set_knowledge_graph(vault_path, kg)
    return kg


def _kg_node_to_yaml(node: Dict[str, Any], vault_path: str) -> str:
    """Serialize a KG node dict as a YAML frontmatter block."""
    import yaml

    safe_name = node.get("node_name", "Unknown")
    safe_name_lower = safe_name.lower().replace(" ", "_")
    node_type = node.get("node_type", "npc")
    tags = node.get("tags", [])
    attributes = node.get("attributes", {})

    frontmatter = {
        "id": str(uuid.uuid4()),
        "name": safe_name,
        "type": node_type,
        "tags": tags,
        "attributes": attributes,
    }

    body = f"\n# {safe_name}\n\n"
    if attributes.get("description"):
        body += f"{attributes['description']}\n"

    yaml_front = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
    return f"---\n{yaml_front}---\n{body}"


def _write_kg_entity_to_vault(
    node: Dict[str, Any], vault_path: str
) -> str:
    """Write a KG node to the vault as a markdown file."""
    from registry import get_knowledge_graph

    kg = _get_kg(vault_path)
    safe_name = node.get("node_name", "Unknown")
    safe_name_lower = safe_name.lower().replace(" ", "_")
    node_type = node.get("node_type", "npc")

    # Determine subdirectory
    if node_type == "faction":
        subdir = os.path.join("server", "Journals", "FACTIONS")
    elif node_type == "location":
        subdir = os.path.join("server", "Journals", "LOCATIONS")
    else:
        subdir = os.path.join("server", "Journals")

    os.makedirs(subdir, exist_ok=True)
    filepath = os.path.join(subdir, f"{safe_name_lower}.md")
    content = _kg_node_to_yaml(node, vault_path)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    # Also add to KG in memory
    from knowledge_graph import KnowledgeGraphNode, GraphNodeType

    try:
        gnode_type = GraphNodeType(node_type.upper())
    except ValueError:
        gnode_type = GraphNodeType.NPC

    kg_node = KnowledgeGraphNode(
        node_uuid=uuid.uuid4(),
        node_type=gnode_type,
        name=safe_name,
        attributes=node.get("attributes", {}),
        tags=set(node.get("tags", [])),
    )
    kg.add_node(kg_node)

    # Add edges
    from knowledge_graph import KnowledgeGraphEdge, GraphPredicate

    predicate_map = {
        "CONNECTED_TO": GraphPredicate.CONNECTED_TO,
        "LOCATED_IN": GraphPredicate.LOCATED_IN,
        "MEMBER_OF": GraphPredicate.MEMBER_OF,
        "ALLIED_WITH": GraphPredicate.ALLIED_WITH,
        "HOSTILE_TOWARD": GraphPredicate.HOSTILE_TOWARD,
        "CONTROLS": GraphPredicate.CONTROLS,
        "LEADS": GraphPredicate.LEADS,
        "SERVES": GraphPredicate.SERVES,
        "RIVAL_OF": GraphPredicate.RIVAL_OF,
        "KNOWS_ABOUT": GraphPredicate.KNOWS_ABOUT,
        "POSSESSES": GraphPredicate.POSSESSES,
        "OWNS": GraphPredicate.OWNED_BY,
    }

    for edge in node.get("edges", []):
        try:
            pred = predicate_map.get(edge.get("predicate", "").upper(), GraphPredicate.CONNECTED_TO)
        except Exception:
            pred = GraphPredicate.CONNECTED_TO

        # Resolve target by name
        target_name = edge.get("target_node", "")
        target_uuid = kg.get_node_by_name(target_name) if hasattr(kg, "get_node_by_name") else None
        if target_uuid is None:
            target_uuid = uuid.uuid4()  # placeholder

        kg_edge = KnowledgeGraphEdge(
            subject_uuid=kg_node.node_uuid,
            predicate=pred,
            object_uuid=target_uuid,
            weight=edge.get("weight", 1.0),
            secret=edge.get("attributes", {}).get("secret", False),
        )
        kg.add_edge(kg_edge)

    return filepath


# ---------------------------------------------------------------------------
# Sub-hydrators
# ---------------------------------------------------------------------------


async def _hydrate_creatures(
    raw_text: str, vault_path: str, llm: BaseChatModel
) -> CreatureHydrationResult:
    """
    Parse creature stat blocks and write KG nodes + entity files.

    Reads: docs/Creature Hydrator.md
    Outputs: KG CREATURE nodes + server/Journals/{Name}.md entity files
    """
    if not raw_text.strip():
        return CreatureHydrationResult(nodes_created=0, edges_created=0, entities_written=[], warnings=[])

    from langchain_core.language_models import BaseChatModel

    prompt_text = _load_prompt("Creature Hydrator")
    if not prompt_text:
        return CreatureHydrationResult(
            nodes_created=0, edges_created=0, entities_written=[],
            warnings=["Creature Hydrator.md prompt not found"]
        )

    # Split input into individual stat blocks
    blocks = _split_creature_blocks(raw_text)
    nodes_created = 0
    edges_created = 0
    entities_written = []
    warnings = []

    class CreatureSpec(BaseModel):
        node_name: str = Field(description="Canonical creature name")
        node_type: str = Field(default="creature")
        attributes: Dict[str, Any] = Field(default_factory=dict)
        tags: List[str] = Field(default_factory=list)
        edges: List[Dict[str, Any]] = Field(default_factory=list)

    for block in blocks:
        try:
            spec = await _call_llm_structured(llm, prompt_text, block, CreatureSpec)
            if spec is None:
                warnings.append(f"Failed to parse creature block: {block[:50]}...")
                continue

            # Write to vault
            node_dict = {
                "node_name": spec.node_name,
                "node_type": "creature",
                "attributes": spec.attributes,
                "tags": spec.tags,
                "edges": spec.edges,
            }
            filepath = _write_kg_entity_to_vault(node_dict, vault_path)
            entities_written.append(filepath)
            nodes_created += 1
            edges_created += len(spec.edges)

        except Exception as e:
            warnings.append(f"Error processing creature block: {e}")

    return CreatureHydrationResult(
        nodes_created=nodes_created,
        edges_created=edges_created,
        entities_written=entities_written,
        warnings=warnings,
    )


async def _hydrate_locations(
    raw_text: str, vault_path: str, llm: BaseChatModel
) -> LocationHydrationResult:
    """
    Parse location descriptions into KG LOCATION nodes.

    Reads: docs/Location Hydrator.md
    Outputs: KG LOCATION nodes + edges
    """
    if not raw_text.strip():
        return LocationHydrationResult(nodes_created=0, edges_created=0, warnings=[])

    prompt_text = _load_prompt("Location Hydrator")
    if not prompt_text:
        return LocationHydrationResult(
            nodes_created=0, edges_created=0,
            warnings=["Location Hydrator.md prompt not found"]
        )

    class LocationSpec(BaseModel):
        locations: List[Dict[str, Any]] = Field(default_factory=list)
        edges: List[Dict[str, Any]] = Field(default_factory=list)
        warnings: List[str] = Field(default_factory=list)

    try:
        spec = await _call_llm_structured(llm, prompt_text, raw_text, LocationSpec)
        if spec is None:
            return LocationHydrationResult(nodes_created=0, edges_created=0, warnings=["LLM returned no result"])

        kg = _get_kg(vault_path)
        nodes_created = 0
        edges_created = 0

        for loc in spec.locations:
            node_dict = {
                "node_name": loc.get("node_name", "Unknown"),
                "node_type": "location",
                "attributes": loc.get("attributes", {}),
                "tags": loc.get("tags", []),
                "edges": [],
            }
            filepath = _write_kg_entity_to_vault(node_dict, vault_path)
            nodes_created += 1

        for edge in spec.edges:
            try:
                from knowledge_graph import KnowledgeGraphEdge, GraphPredicate

                pred_str = edge.get("predicate", "CONNECTED_TO").upper()
                pred = getattr(GraphPredicate, pred_str, GraphPredicate.CONNECTED_TO)

                subject_name = edge.get("subject_name") or edge.get("subject_node", "")
                object_name = edge.get("object_name") or edge.get("target_node", "")

                # Resolve UUIDs by name
                from registry import get_knowledge_graph
                kg_ref = get_knowledge_graph(vault_path)

                subject_uuid: uuid.UUID
                object_uuid: uuid.UUID

                if hasattr(kg_ref, "name_index") and subject_name:
                    subject_uuid = kg_ref.name_index.get(subject_name.lower(), uuid.uuid4())
                else:
                    subject_uuid = uuid.uuid4()

                if hasattr(kg_ref, "name_index") and object_name:
                    object_uuid = kg_ref.name_index.get(object_name.lower(), uuid.uuid4())
                else:
                    object_uuid = uuid.uuid4()

                kg_edge = KnowledgeGraphEdge(
                    subject_uuid=subject_uuid,
                    predicate=pred,
                    object_uuid=object_uuid,
                    weight=edge.get("weight", 1.0),
                )
                kg.add_edge(kg_edge)
                edges_created += 1
            except Exception as e:
                warnings.append(f"Edge error: {e}")

        all_warnings = list(spec.warnings) if spec.warnings else []
        return LocationHydrationResult(
            nodes_created=nodes_created,
            edges_created=edges_created,
            warnings=all_warnings,
        )

    except Exception as e:
        return LocationHydrationResult(nodes_created=0, edges_created=0, warnings=[f"Fatal: {e}"])


async def _hydrate_factions(
    raw_text: str, vault_path: str, llm: BaseChatModel
) -> FactionHydrationResult:
    """
    Parse faction lore into KG FACTION nodes.

    Reads: docs/Faction Hydrator.md
    Outputs: KG FACTION nodes + social edges
    """
    if not raw_text.strip():
        return FactionHydrationResult(nodes_created=0, edges_created=0, warnings=[])

    prompt_text = _load_prompt("Faction Hydrator")
    if not prompt_text:
        return FactionHydrationResult(
            nodes_created=0, edges_created=0,
            warnings=["Faction Hydrator.md prompt not found"]
        )

    class FactionSpec(BaseModel):
        factions: List[Dict[str, Any]] = Field(default_factory=list)
        edges: List[Dict[str, Any]] = Field(default_factory=list)
        warnings: List[str] = Field(default_factory=list)

    try:
        spec = await _call_llm_structured(llm, prompt_text, raw_text, FactionSpec)
        if spec is None:
            return FactionHydrationResult(nodes_created=0, edges_created=0, warnings=["LLM returned no result"])

        nodes_created = 0
        edges_created = 0
        kg = _get_kg(vault_path)

        for faction in spec.factions:
            node_dict = {
                "node_name": faction.get("node_name", "Unknown"),
                "node_type": "faction",
                "attributes": faction.get("attributes", {}),
                "tags": faction.get("tags", []),
                "edges": [],
            }
            _write_kg_entity_to_vault(node_dict, vault_path)
            nodes_created += 1

        for edge in spec.edges:
            try:
                from knowledge_graph import KnowledgeGraphEdge, GraphPredicate

                pred_str = edge.get("predicate", "ALLIED_WITH").upper()
                pred = getattr(GraphPredicate, pred_str, GraphPredicate.ALLIED_WITH)

                subject_name = edge.get("subject_name", "")
                object_name = edge.get("object_name", "")

                kg_ref = get_knowledge_graph(vault_path)
                subject_uuid = getattr(kg_ref.name_index, "get", lambda x: uuid.uuid4())(subject_name.lower()) if subject_name else uuid.uuid4()
                object_uuid = getattr(kg_ref.name_index, "get", lambda x: uuid.uuid4())(object_name.lower()) if object_name else uuid.uuid4()

                kg_edge = KnowledgeGraphEdge(
                    subject_uuid=subject_uuid if isinstance(subject_uuid, uuid.UUID) else uuid.uuid4(),
                    predicate=pred,
                    object_uuid=object_uuid if isinstance(object_uuid, uuid.UUID) else uuid.uuid4(),
                    weight=edge.get("weight", 1.0),
                )
                kg.add_edge(kg_edge)
                edges_created += 1
            except Exception:
                pass

        all_warnings = list(spec.warnings) if spec.warnings else []
        return FactionHydrationResult(
            nodes_created=nodes_created,
            edges_created=edges_created,
            warnings=all_warnings,
        )

    except Exception as e:
        return FactionHydrationResult(nodes_created=0, edges_created=0, warnings=[f"Fatal: {e}"])


async def _hydrate_npcs(
    raw_text: str, vault_path: str, llm: BaseChatModel
) -> NPCHydrationResult:
    """
    Parse NPC biographies into KG NPC nodes.

    Reads: docs/NPC Hydrator.md
    Outputs: KG NPC nodes + edges
    """
    if not raw_text.strip():
        return NPCHydrationResult(nodes_created=0, edges_created=0, entities_written=[], warnings=[])

    prompt_text = _load_prompt("NPC Hydrator")
    if not prompt_text:
        return NPCHydrationResult(
            nodes_created=0, edges_created=0, entities_written=[],
            warnings=["NPC Hydrator.md prompt not found"]
        )

    class NPCHydratorSpec(BaseModel):
        npcs: List[Dict[str, Any]] = Field(default_factory=list)
        edges: List[Dict[str, Any]] = Field(default_factory=list)
        warnings: List[str] = Field(default_factory=list)

    try:
        spec = await _call_llm_structured(llm, prompt_text, raw_text, NPCHydratorSpec)
        if spec is None:
            return NPCHydrationResult(nodes_created=0, edges_created=0, entities_written=[], warnings=["LLM returned no result"])

        nodes_created = 0
        edges_created = 0
        entities_written = []
        kg = _get_kg(vault_path)

        for npc in spec.npcs:
            node_dict = {
                "node_name": npc.get("node_name", "Unknown"),
                "node_type": "npc",
                "attributes": npc.get("attributes", {}),
                "tags": npc.get("tags", []),
                "edges": npc.get("edges", []),
            }
            filepath = _write_kg_entity_to_vault(node_dict, vault_path)
            entities_written.append(filepath)
            nodes_created += 1
            edges_created += len(npc.get("edges", []))

        for edge in spec.edges:
            try:
                from knowledge_graph import KnowledgeGraphEdge, GraphPredicate

                pred_str = edge.get("predicate", "ALLIED_WITH").upper()
                pred = getattr(GraphPredicate, pred_str, GraphPredicate.ALLIED_WITH)

                subject_name = edge.get("subject_name", "")
                object_name = edge.get("object_name", "")

                kg_ref = get_knowledge_graph(vault_path)
                subject_uuid = getattr(kg_ref.name_index, "get", lambda x: uuid.uuid4())(subject_name.lower()) if subject_name else uuid.uuid4()
                object_uuid = getattr(kg_ref.name_index, "get", lambda x: uuid.uuid4())(object_name.lower()) if object_name else uuid.uuid4()

                kg_edge = KnowledgeGraphEdge(
                    subject_uuid=subject_uuid if isinstance(subject_uuid, uuid.UUID) else uuid.uuid4(),
                    predicate=pred,
                    object_uuid=object_uuid if isinstance(object_uuid, uuid.UUID) else uuid.uuid4(),
                    weight=edge.get("weight", 1.0),
                )
                kg.add_edge(kg_edge)
                edges_created += 1
            except Exception:
                pass

        all_warnings = list(spec.warnings) if spec.warnings else []
        return NPCHydrationResult(
            nodes_created=nodes_created,
            edges_created=edges_created,
            entities_written=entities_written,
            warnings=all_warnings,
        )

    except Exception as e:
        return NPCHydrationResult(nodes_created=0, edges_created=0, entities_written=[], warnings=[f"Fatal: {e}"])


async def _hydrate_maps(
    raw_text: str, vault_path: str, llm: BaseChatModel
) -> MapHydrationResult:
    """
    Parse map descriptions into KG LOCATION nodes with map_data.

    Reads: docs/Map Hydrator.md
    Outputs: KG LOCATION nodes with map_data attributes + JSON battlemaps
    """
    if not raw_text.strip():
        return MapHydrationResult(nodes_created=0, edges_created=0, files_written=[], warnings=[])

    prompt_text = _load_prompt("Map Hydrator")
    if not prompt_text:
        return MapHydrationResult(
            nodes_created=0, edges_created=0, files_written=[],
            warnings=["Map Hydrator.md prompt not found"]
        )

    class MapSpec(BaseModel):
        maps: List[Dict[str, Any]] = Field(default_factory=list)
        kg_nodes: List[Dict[str, Any]] = Field(default_factory=list)
        kg_edges: List[Dict[str, Any]] = Field(default_factory=list)
        warnings: List[str] = Field(default_factory=list)

    try:
        spec = await _call_llm_structured(llm, prompt_text, raw_text, MapSpec)
        if spec is None:
            return MapHydrationResult(nodes_created=0, edges_created=0, files_written=[], warnings=["LLM returned no result"])

        nodes_created = 0
        edges_created = 0
        files_written = []

        for mp in spec.maps:
            # Write battlemap JSON
            map_name = mp.get("map_name", "UnknownMap")
            safe_name = map_name.lower().replace(" ", "_")
            maps_dir = os.path.join("server", "Journals", "MAPS")
            os.makedirs(maps_dir, exist_ok=True)
            filepath = os.path.join(maps_dir, f"{safe_name}.json")

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(mp, f, indent=2)
            files_written.append(filepath)

            # Write KG node for map
            node_dict = {
                "node_name": map_name,
                "node_type": "location",
                "attributes": {
                    "map_type": mp.get("map_type", "battlemap"),
                    "map_data_ref": filepath,
                    "grid_width": mp.get("grid_width", 0),
                    "grid_height": mp.get("grid_height", 0),
                    "danger_rating": mp.get("danger_rating", "unknown"),
                    "tactical_notes": mp.get("tactical_notes", ""),
                },
                "tags": mp.get("tags", []),
                "edges": [],
            }
            _write_kg_entity_to_vault(node_dict, vault_path)
            nodes_created += 1

        for edge in spec.kg_edges:
            try:
                from knowledge_graph import KnowledgeGraphEdge, GraphPredicate

                pred_str = edge.get("predicate", "CONNECTED_TO").upper()
                pred = getattr(GraphPredicate, pred_str, GraphPredicate.CONNECTED_TO)
                kg = _get_kg(vault_path)
                kg_edge = KnowledgeGraphEdge(
                    subject_uuid=uuid.uuid4(),
                    predicate=pred,
                    object_uuid=uuid.uuid4(),
                    weight=edge.get("weight", 1.0),
                )
                kg.add_edge(kg_edge)
                edges_created += 1
            except Exception:
                pass

        all_warnings = list(spec.warnings) if spec.warnings else []
        return MapHydrationResult(
            nodes_created=nodes_created,
            edges_created=edges_created,
            files_written=files_written,
            warnings=all_warnings,
        )

    except Exception as e:
        return MapHydrationResult(nodes_created=0, edges_created=0, files_written=[], warnings=[f"Fatal: {e}"])


async def _hydrate_narrative(
    campaign_narrative: str,
    session_prep_notes: str,
    storylet_resolutions: Dict[str, str],
    vault_path: str,
    llm: BaseChatModel,
) -> NarrativeHydrationResult:
    """
    Delegate to existing CampaignHydrationPipeline for storylet extraction
    and effect annotation.

    Reads: docs/Narrative Hydrator.md (for prompt)
    Uses: ingestion_pipeline.CampaignHydrationPipeline
    Outputs: Storylets + KG edges
    """
    if not campaign_narrative.strip() and not session_prep_notes.strip():
        return NarrativeHydrationResult(
            nodes_created=0, edges_created=0, storylets_created=0,
            effects_annotated=0, backup_storylets=0,
            three_clue_violations=0, warnings=[]
        )

    from ingestion_pipeline import (
        CampaignHydrationPipeline,
        CampaignMaterials,
        HydrationReport,
    )

    try:
        materials = CampaignMaterials(
            campaign_narrative=campaign_narrative or "",
            session_prep_notes=session_prep_notes or "",
            storylet_resolutions=storylet_resolutions or {},
        )
        pipeline = CampaignHydrationPipeline(llm, vault_path=vault_path)
        report: HydrationReport = await pipeline.run(materials)

        return NarrativeHydrationResult(
            nodes_created=report.nodes_created,
            edges_created=report.edges_created,
            storylets_created=report.storylets_created,
            effects_annotated=report.effects_attached,
            backup_storylets=report.backup_storylets_generated,
            three_clue_violations=report.three_clue_violations_fixed,
            warnings=report.warnings,
        )

    except Exception as e:
        return NarrativeHydrationResult(
            nodes_created=0, edges_created=0, storylets_created=0,
            effects_annotated=0, backup_storylets=0,
            three_clue_violations=0, warnings=[f"Narrative hydration failed: {e}"]
        )


async def _hydrate_items(
    raw_text: str, vault_path: str, llm: BaseChatModel
) -> CompendiumItemsResult:
    """
    Parse spells, feats, items into CompendiumManager JSON entries.

    Reads: docs/Compendium Items Hydrator.md
    Outputs: CompendiumManager entries via compendium_manager.save_entry
    """
    if not raw_text.strip():
        return CompendiumItemsResult(entries_saved=[], warnings=[])

    prompt_text = _load_prompt("Compendium Items Hydrator")
    if not prompt_text:
        return CompendiumItemsResult(
            entries_saved=[],
            warnings=["Compendium Items Hydrator.md prompt not found"]
        )

    class CompendiumItemsSpec(BaseModel):
        entries: List[Dict[str, Any]] = Field(default_factory=list)
        warnings: List[str] = Field(default_factory=list)

    try:
        spec = await _call_llm_structured(llm, prompt_text, raw_text, CompendiumItemsSpec)
        if spec is None:
            return CompendiumItemsResult(entries_saved=[], warnings=["LLM returned no result"])

        from compendium_manager import CompendiumEntry, MechanicEffect, CompendiumManager

        entries_saved = []
        for entry_dict in spec.entries:
            try:
                # Build MechanicEffect
                mech_dict = entry_dict.pop("mechanic_effect", {})
                mechanic = MechanicEffect(**mech_dict)

                # Build CompendiumEntry
                entry = CompendiumEntry(
                    name=entry_dict.get("entry_name", "Unknown"),
                    category=entry_dict.get("entry_type", "item"),
                    action_type=entry_dict.get("action_type", "Action"),
                    description=entry_dict.get("description", ""),
                    mitigation_notes=entry_dict.get("mitigation_notes", ""),
                    mechanics=mechanic,
                )

                filepath = await CompendiumManager.save_entry(vault_path, entry)
                entries_saved.append(filepath)

            except Exception as e:
                spec.warnings.append(f"Failed to save entry {entry_dict.get('entry_name', '?')}: {e}")

        all_warnings = list(spec.warnings) if spec.warnings else []
        return CompendiumItemsResult(entries_saved=entries_saved, warnings=all_warnings)

    except Exception as e:
        return CompendiumItemsResult(entries_saved=[], warnings=[f"Fatal: {e}"])


# ---------------------------------------------------------------------------
# Utility: split multi-entry creature input into individual blocks
# ---------------------------------------------------------------------------


def _split_creature_blocks(text: str) -> List[str]:
    """
    Split a multi-entry creature document into individual stat blocks.

    Splits on:
    1. Markdown headers (## Name)
    2. D&D 5e stat block separators (--- --- block pattern)
    3. Blank-line-separated blocks
    """
    blocks = []
    text = text.strip()
    if not text:
        return blocks

    # Try markdown header splitting first
    header_pattern = re.compile(r"(?m)^##\s+.+$", re.MULTILINE)
    matches = list(header_pattern.finditer(text))

    if len(matches) > 1:
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[start:end].strip()
            if block:
                blocks.append(block)
        return blocks

    # Try ---  --- separator (D&D Beyond / 5e stat block style)
    sep_pattern = re.compile(r"(?m)^---$", re.MULTILINE)
    sep_matches = list(sep_pattern.finditer(text))
    if len(sep_matches) >= 2:
        # Find pairs of --- delimiters
        i = 0
        while i < len(sep_matches) - 1:
            # Find next sep after this one
            start = sep_matches[i].start()
            j = i + 1
            while j < len(sep_matches) and sep_matches[j].start() - sep_matches[j - 1].start() < 10:
                j += 1
            end = sep_matches[j - 1].start() + 4
            block = text[start:end].strip()
            if block:
                blocks.append(block)
            i = j
        return blocks

    # Fallback: split on double blank lines
    parts = re.split(r"\n\n\n+", text)
    for part in parts:
        part = part.strip()
        if part:
            blocks.append(part)

    return blocks if blocks else [text]


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


async def run_compendium_hydration(
    materials: CompendiumMaterials,
    vault_path: str,
    llm: BaseChatModel,
) -> CompendiumHydrationReport:
    """
    Run all applicable sub-hydrators in parallel and aggregate results.

    Only sub-hydrators whose corresponding material field is non-empty are run.
    Failures in one sub-hydrator do not affect others.

    Args:
        materials: CompendiumMaterials with raw input text per category
        vault_path: vault identifier for KG and vault I/O
        llm: LLM instance for structured extraction

    Returns:
        CompendiumHydrationReport with per-category results and aggregated errors
    """
    errors: List[str] = []
    partial_failures: List[str] = []

    # Build tasks for all applicable sub-hydrators
    tasks: Dict[str, asyncio.Task] = {}

    if materials.get("creatures"):
        tasks["creatures"] = asyncio.create_task(
            _hydrate_creatures(materials["creatures"], vault_path, llm)
        )

    if materials.get("locations"):
        tasks["locations"] = asyncio.create_task(
            _hydrate_locations(materials["locations"], vault_path, llm)
        )

    if materials.get("factions"):
        tasks["factions"] = asyncio.create_task(
            _hydrate_factions(materials["factions"], vault_path, llm)
        )

    if materials.get("npcs"):
        tasks["npcs"] = asyncio.create_task(
            _hydrate_npcs(materials["npcs"], vault_path, llm)
        )

    if materials.get("maps"):
        tasks["maps"] = asyncio.create_task(
            _hydrate_maps(materials["maps"], vault_path, llm)
        )

    if materials.get("items"):
        tasks["items"] = asyncio.create_task(
            _hydrate_items(materials["items"], vault_path, llm)
        )

    # Narrative is special: it needs multiple fields
    if materials.get("campaign_narrative") or materials.get("session_prep_notes"):
        tasks["narrative"] = asyncio.create_task(
            _hydrate_narrative(
                materials.get("campaign_narrative", ""),
                materials.get("session_prep_notes", ""),
                materials.get("storylet_resolutions", {}),
                vault_path,
                llm,
            )
        )

    # Await all tasks
    if tasks:
        results_dict = await asyncio.gather(*tasks.values(), return_exceptions=True)
    else:
        results_dict = {}

    # Map results back to keys
    task_keys = list(tasks.keys())
    results: Dict[str, Any] = {}
    for key, result in zip(task_keys, results_dict):
        if isinstance(result, Exception):
            errors.append(f"{key} hydrator raised exception: {result}")
            results[key] = None
        else:
            results[key] = result
            # Collect warnings from sub-hydrator
            if result and hasattr(result, "warnings"):
                for w in result.warnings:
                    partial_failures.append(f"[{key}] {w}")

    # Build final report
    report = CompendiumHydrationReport(
        creatures=results.get("creatures"),
        locations=results.get("locations"),
        factions=results.get("factions"),
        npcs=results.get("npcs"),
        maps=results.get("maps"),
        narrative=results.get("narrative"),
        items=results.get("items"),
        errors=errors,
        partial_failures=partial_failures,
    )

    return report


# ---------------------------------------------------------------------------
# Sync KG to vault after all sub-hydrators complete
# ---------------------------------------------------------------------------


def sync_compendium_to_vault(vault_path: str) -> None:
    """
    Write all in-memory KG state to vault after compendium hydration.

    Call this after run_compendium_hydration completes successfully.
    """
    try:
        from vault_io import sync_engine_to_vault

        sync_engine_to_vault(vault_path)
    except Exception as e:
        print(f"[CompendiumHydration] Warning: vault sync failed: {e}")
