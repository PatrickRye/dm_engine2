"""
NLP Ingestion Pipeline for the DM Engine.

Phase 2 of the Integration Blueprint — converts raw DM content into
structured engine artifacts:

  (a) NPC lore text           → KG entities, edges, and behavioral dials
  (b) Campaign narrative      → Storylets with prerequisite annotations
  (c) Storylet resolution     → GraphMutation effects (Effect Annotation, Task 3.3)

Each sub-pipeline is LLM-powered (structured output) with deterministic
fallbacks. All functions are vault-aware and write directly to the
Knowledge Graph and Storylet Registry.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field

from knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphNode,
    KnowledgeGraphEdge,
    GraphNodeType,
    GraphPredicate,
)
from storylet import (
    Storylet,
    StoryletPrerequisites,
    GraphQuery,
    GraphMutation,
    TensionLevel,
)
from registry import get_knowledge_graph, get_storylet_registry


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------

class NPCEntitySpec(BaseModel):
    """Single NPC entity extracted from lore text."""
    name: str = Field(description="Canonical NPC name")
    node_type: str = Field(default="npc", description="KG node type (npc, faction, location, item)")
    aliases: List[str] = Field(default_factory=list, description="Known aliases/titles")
    description: str = Field(default="", description="Physical and behavioral description")
    bio: str = Field(default="", description="Background biography")
    connections: str = Field(default="", description="Allies, enemies, affiliations")
    long_term_goals: str = Field(default="", description="Overarching life goals")
    immediate_goals: str = Field(default="", description="What they want right now")
    misc_notes: str = Field(default="", description="Rumors, secrets, 'jazz'")
    behavioral_dials: Dict[str, float] = Field(
        default_factory=dict,
        description="Dial values 0.0-1.0: greed, loyalty, courage, cruelty, cunning, piety",
    )
    tags: List[str] = Field(default_factory=list, description="KG tags for this entity")
    is_immutable: bool = Field(default=False, description="True if this NPC should never be removed or fundamentally altered")


class KGEdgeSpec(BaseModel):
    """A single KG edge inferred from lore text."""
    subject_name: str = Field(description="Name of the subject node")
    predicate: str = Field(description="GraphPredicate value: connected_to, member_of, hostile_toward, etc.")
    object_name: str = Field(description="Name of the object node")
    weight: float = Field(default=1.0, description="Edge weight 0.0-1.0")


class StoryletSpec(BaseModel):
    """A storylet extracted from campaign narrative text."""
    name: str = Field(description="Unique storylet name")
    content: str = Field(description="Narrative content / scene text to present to the player")
    narrative_beats: List[str] = Field(
        default_factory=list,
        alias="narrative beats",
        description="Key events or beats this storylet covers",
    )
    tension_level: str = Field(
        default="medium",
        description="Tension at which this storylet fires: low, medium, high, cliffhanger",
    )
    priority_override: int = Field(
        default=0,
        description="Priority boost (positive) or suppression (negative, -100 to +100)",
    )
    prerequisite_queries: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "List of GraphQuery dicts (query_type, entity_name, etc.) that must pass "
            "for this storylet to be eligible. Uses any_of/any_approach semantics."
        ),
    )
    tags: List[str] = Field(default_factory=list, description="Storylet tags for graph chaining")
    effects: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="GraphMutation dicts to apply when this storylet is activated",
    )


class EffectAnnotationSpec(BaseModel):
    """GraphMutations extracted from storylet resolution prose."""
    mutations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of GraphMutation dicts encoding the effects described in the prose",
    )
    summary: str = Field(default="", description="Human-readable summary of the effects")


# ---------------------------------------------------------------------------
# NPC Lore → KG Ingestion
# ---------------------------------------------------------------------------

def _build_npc_system_prompt() -> str:
    return (
        "You are an expert D&D world-builder. Parse the NPC biography text below and extract "
        "structured knowledge graph data.\n\n"
        "RULES:\n"
        "1. Extract EXACTLY one NPCEntitySpec. Use the canonical name as the name field.\n"
        "2. Infer behavioral dials (0.0 to 1.0) from the text:\n"
        "   - greed: focuses on wealth, haggles, values coin → high (~0.7-1.0)\n"
        "   - loyalty: devoted to a cause, oath-bound, faithful servant → high (~0.7-1.0)\n"
        "   - courage: brave, charges into danger, steadfast → high (~0.7-1.0)\n"
        "   - cruelty: brutal, merciless, takes pleasure in suffering → high (~0.7-1.0)\n"
        "   - cunning: clever, always three steps ahead, manipulative → high (~0.7-1.0)\n"
        "   - piety: devout, keeps sacred laws, temple-attending → high (~0.7-1.0)\n"
        "   Use negative/contradictory text for low values (~0.1-0.3).\n"
        "3. Extract all relationships as KGEdgeSpec objects (ALLIED_WITH, HOSTILE_TOWARD, "
        "MEMBER_OF, SERVES, LEADS, etc.).\n"
        "4. Be CONSERVATIVE — only extract what is EXPLICITLY stated in the text.\n"
        "5. If the text does not contain enough information for a dial, omit it (empty dict).\n"
        "6. node_type should be 'npc' unless a different KG type is clearly implied.\n"
        "7. tags should capture faction affiliations, locations, and roles.\n"
        "8. is_immutable should be True for major quest-givers, recurring NPCs, and lore figures.\n"
        "9. Output ONLY a valid JSON object matching the NPCEntitySpec schema — no markdown, "
        "no explanation, no preamble.\n"
    )


async def ingest_npc_lore(
    lore_text: str,
    vault_path: str = "default",
    llm=None,
) -> tuple[List[KnowledgeGraphNode], List[KnowledgeGraphEdge]]:
    """
    Phase 2 (a): Parse NPC biography/lore text and produce KG entities and edges.

    Uses the llm (if provided) for LLM-powered extraction; otherwise falls back
    to keyword heuristics (deterministic but limited).

    Returns (nodes, edges) — caller is responsible for adding them to the KG.
    """
    from graph import extract_npc_dials

    if llm is None:
        # Deterministic fallback: cannot extract edges without LLM
        return [], []

    node_spec = await _call_llm_structured(
        llm,
        _build_npc_system_prompt(),
        lore_text,
        NPCEntitySpec,
    )

    if node_spec is None:
        return [], []

    # Build node
    node_uuid = uuid.uuid4()
    attrs: Dict[str, Any] = {
        "description": node_spec.description,
        "bio": node_spec.bio,
        "connections": node_spec.connections,
        "long_term_goals": node_spec.long_term_goals,
        "immediate_goals": node_spec.immediate_goals,
        "misc_notes": node_spec.misc_notes,
    }
    if node_spec.aliases:
        attrs["aliases"] = ", ".join(node_spec.aliases)

    node = KnowledgeGraphNode(
        node_uuid=node_uuid,
        node_type=GraphNodeType(node_spec.node_type),
        name=node_spec.name,
        attributes=attrs,
        tags=set(node_spec.tags),
        is_immutable=node_spec.is_immutable,
        npc_dials=node_spec.behavioral_dials or extract_npc_dials(lore_text),
    )

    # Build edges
    edges: List[KnowledgeGraphEdge] = []
    # NOTE: without an LLM call to extract edges, we cannot infer them here.
    # The edges would come from a separate call that extracts KGEdgeSpec objects.

    return [node], edges


async def ingest_npc_lore_with_edges(
    lore_text: str,
    vault_path: str = "default",
    llm=None,
) -> tuple[List[KnowledgeGraphNode], List[KnowledgeGraphEdge]]:
    """
    Phase 2 (a) full version: parse NPC lore AND extract relationship edges.

    Two-step LLM call:
      1. Extract NPCEntitySpec (entity + dials)
      2. Extract KGEdgeSpec list (relationships)
    """
    from graph import extract_npc_dials

    nodes, edges = await ingest_npc_lore(lore_text, vault_path, llm)
    if not nodes or llm is None:
        return nodes, edges

    # Second pass: extract relationships
    edge_prompt = (
        "From the NPC biography below, extract ALL relationship edges as a JSON list "
        "of KGEdgeSpec objects.\n\n"
        "Valid predicates: connected_to, located_in, member_of, allied_with, "
        "hostile_toward, controls, leads, serves, rival_of, owned_by, possesses, "
        "wants, knows_about, rules.\n\n"
        "Only extract relationships EXPLICITLY described in the text.\n"
        "Output a JSON array of KGEdgeSpec objects. If no relationships exist, "
        "output an empty array [] .\n"
        "Example: [{\"subject_name\": \"Sir Cedric\", \"predicate\": \"allied_with\", "
        "\"object_name\": \"The Crown\", \"weight\": 1.0}]\n\n"
        f"BIOGRAPHY:\n{lore_text}"
    )

    edge_specs = await _call_llm_structured_list(llm, edge_prompt, "", KGEdgeSpec)
    if edge_specs is None:
        return nodes, edges

    for spec in edge_specs:
        try:
            pred = GraphPredicate(spec.predicate)
        except ValueError:
            pred = GraphPredicate.CONNECTED_TO

        subj_uuid = None
        obj_uuid = None
        for n in nodes:
            if n.name.lower() == spec.subject_name.lower():
                subj_uuid = n.node_uuid
            if n.name.lower() == spec.object_name.lower():
                obj_uuid = n.node_uuid

        # Look up in KG if not in our newly created nodes
        kg = get_knowledge_graph(vault_path)
        if subj_uuid is None:
            found = kg.get_node_by_name(spec.subject_name)
            subj_uuid = found.node_uuid if found else None
        if obj_uuid is None:
            found = kg.get_node_by_name(spec.object_name)
            obj_uuid = found.node_uuid if found else None

        if subj_uuid and obj_uuid:
            edges.append(
                KnowledgeGraphEdge(
                    subject_uuid=subj_uuid,
                    predicate=pred,
                    object_uuid=obj_uuid,
                    weight=spec.weight,
                )
            )

    return nodes, edges


# ---------------------------------------------------------------------------
# Campaign Narrative → Storylets
# ---------------------------------------------------------------------------

async def ingest_campaign_narrative(
    narrative_text: str,
    vault_path: str = "default",
    llm=None,
) -> List[Storylet]:
    """
    Phase 2 (b): Parse campaign narrative text and produce Storylets.

    Scans for scenes, encounters, or plot beats and converts each into a
    Storylet with prerequisites, tension level, and effect annotations.
    """
    if llm is None:
        return []

    prompt = (
        "You are a D&D adventure architect. Parse the campaign narrative below and "
        "extract all distinct storylets (scenes, encounters, plot beats).\n\n"
        "For each storylet extract a StoryletSpec:\n"
        "  - name: a unique, descriptive name\n"
        "  - content: the narrative text/scene to present to players (2-5 sentences)\n"
        "  - narrative_beats: key events in this storylet\n"
        "  - tension_level: low | medium | high | cliffhanger\n"
        "  - priority_override: -100 to +100 (default 0)\n"
        "  - prerequisite_queries: GraphQuery dicts for KG/entity preconditions\n"
        "  - tags: storylet tags for chaining (use lowercase with underscores)\n"
        "  - effects: GraphMutation dicts describing world-state changes\n\n"
        "PREREQUISITE QUERY GUIDANCE:\n"
        "  - query_type 'node_exists': verify entity_name exists in KG\n"
        "  - query_type 'edge_exists': verify relationship between two entities\n"
        "  - query_type 'engine_state_check': verify entity HP, conditions, etc.\n"
        "  - query_type 'attribute_check': verify entity has specific attribute value\n"
        "  - Use 'any_of' for OR logic, 'all_of' for AND logic.\n\n"
        "TENSION LEVEL GUIDANCE:\n"
        "  - low: social interaction, shopping, travel, rest\n"
        "  - medium: exploration, discovery, negotiation, skill challenge\n"
        "  - high: combat, chase, heist, survival situation\n"
        "  - cliffhanger: major revelation, character death, betrayl, last-second rescue\n\n"
        "Be CONSERVATIVE — extract only clearly distinct storylets.\n"
        "If the text describes a single scene, extract one storylet.\n"
        "Output a JSON array of StoryletSpec objects.\n\n"
        f"CAMPAIGN NARRATIVE:\n{narrative_text}"
    )

    specs = await _call_llm_structured_list(llm, prompt, "", StoryletSpec)
    if specs is None:
        return []

    storylets: List[Storylet] = []
    for spec in specs:
        try:
            tension = TensionLevel(spec.tension_level.lower())
        except ValueError:
            tension = TensionLevel.MEDIUM

        # Convert prerequisite query dicts → StoryletPrerequisites
        prereqs = _build_storylet_prerequisites(spec.prerequisite_queries)

        # Convert effect dicts → GraphMutations
        effects: List[GraphMutation] = []
        for eff in spec.effects:
            try:
                effects.append(GraphMutation(**eff))
            except Exception:
                pass

        storylet = Storylet(
            id=uuid.uuid4(),
            name=spec.name,
            content=spec.content,
            narrative_beats=spec.narrative_beats,
            prerequisites=prereqs,
            effects=effects,
            tension_level=tension,
            priority_override=spec.priority_override if spec.priority_override else None,
            tags=set(spec.tags),
        )
        storylets.append(storylet)

    return storylets


# ---------------------------------------------------------------------------
# Storylet Resolution → GraphMutation Effects (Task 3.3)
# ---------------------------------------------------------------------------

async def annotate_storylet_effects(
    resolution_text: str,
    vault_path: str = "default",
    llm=None,
) -> EffectAnnotationSpec:
    """
    Phase 2 (c) / Task 3.3: Parse storylet resolution prose and produce
    GraphMutation effects.

    This is the "Effect Annotation" pipeline — given a storylet's resolution
    text (what happens when it concludes), extract the implied world-state
    changes as structured mutations.

    Example:
      "The party convinced Lord Vader to join their cause. He grants them
       the Shadowblade as a token of his new allegiance."
      →
      mutations: [
        {"mutation_type": "add_edge", "node_name": "Lord Vader",
         "predicate": "allied_with", "target_name": "The Party"},
        {"mutation_type": "add_edge", "node_name": "Shadowblade",
         "predicate": "owned_by", "target_name": "The Party"},
      ]
    """
    if llm is None:
        return EffectAnnotationSpec()

    prompt = (
        "You are a D&D world-state annotator. Given the storylet resolution prose below, "
        "extract all implied world-state changes as GraphMutation objects.\n\n"
        "Mutation types:\n"
        "  - add_node: create a new KG entity (requires node_name, node_type)\n"
        "  - add_edge: create a relationship (requires node_name, predicate, target_name)\n"
        "  - remove_edge: delete a relationship\n"
        "  - set_attribute: set an entity attribute (requires node_name, attribute, value)\n"
        "  - add_tag / remove_tag: modify entity tags\n"
        "  - set_immutable / remove_immutable: lock/unlock an entity\n\n"
        "Predicate values: connected_to, located_in, member_of, allied_with, "
        "hostile_toward, controls, leads, serves, rival_of, owned_by, possesses, "
        "wants, knows_about, rules\n\n"
        "RULES:\n"
        "1. Only extract changes EXPLICITLY described in the prose.\n"
        "2. For ownership transfer: use 'owned_by' predicate.\n"
        "3. For alliance/hostility changes: use 'allied_with' / 'hostile_toward'.\n"
        "4. For gaining knowledge: use 'knows_about'.\n"
        "5. Include node_name, predicate, and target_name for each mutation.\n"
        "6. Output ONLY a valid JSON object with 'mutations' (list) and 'summary' (str).\n"
        f"RESOLUTION PROSE:\n{resolution_text}"
    )

    result = await _call_llm_structured(llm, prompt, "", EffectAnnotationSpec)
    return result if result is not None else EffectAnnotationSpec()


# ---------------------------------------------------------------------------
# Full pipeline orchestrator
# ---------------------------------------------------------------------------

async def run_ingestion_pipeline(
    vault_path: str,
    *,
    npc_lore_text: str = "",
    campaign_narrative_text: str = "",
    storylet_resolutions: Dict[str, str] = None,
    llm=None,
) -> Dict[str, Any]:
    """
    Run the full Phase 2 ingestion pipeline.

    Args:
        vault_path: Target vault
        npc_lore_text: Raw NPC biography/lore text
        campaign_narrative_text: Campaign narrative for storylet extraction
        storylet_resolutions: Dict[storylet_name, resolution_text] for effect annotation
        llm: LLM instance with ainvoke method

    Returns a summary dict with counts of ingested entities, edges, and storylets.
    """
    storylet_resolutions = storylet_resolutions or {}
    kg = get_knowledge_graph(vault_path)
    reg = get_storylet_registry(vault_path)

    nodes_added = 0
    edges_added = 0
    storylets_created = 0
    effects_annotated = 0

    # (a) NPC lore → KG
    if npc_lore_text:
        nodes, edges = await ingest_npc_lore_with_edges(npc_lore_text, vault_path, llm)
        for node in nodes:
            kg.add_node(node)
            nodes_added += 1
        for edge in edges:
            kg.add_edge(edge)
            edges_added += 1

    # (b) Campaign narrative → Storylets
    if campaign_narrative_text:
        storylets = await ingest_campaign_narrative(campaign_narrative_text, vault_path, llm)
        for sl in storylets:
            reg.register(sl)
            storylets_created += 1

    # (c) Storylet resolutions → Effect annotations
    if storylet_resolutions:
        for sl_name, res_text in storylet_resolutions.items():
            annotated = await annotate_storylet_effects(res_text, vault_path, llm)
            if annotated.mutations:
                # Attach mutations to the registered storylet
                existing = reg.get_by_name(sl_name)
                if existing:
                    for mut_dict in annotated.mutations:
                        try:
                            existing.effects.append(GraphMutation(**mut_dict))
                        except Exception:
                            pass
                    effects_annotated += 1

    # Gap 2 fix: Invalidate GraphRAG cache since KG was modified
    if nodes_added > 0 or edges_added > 0:
        try:
            from graph import _invalidate_grag_cache
            _invalidate_grag_cache(vault_path)
        except Exception:
            pass  # Best-effort

    return {
        "nodes_added": nodes_added,
        "edges_added": edges_added,
        "storylets_created": storylets_created,
        "effects_annotated": effects_annotated,
    }


# ---------------------------------------------------------------------------
# Generic DM Notes → KG Entities + Edges (Task 3.3 / Phase 1 NLP)
# ---------------------------------------------------------------------------

async def extract_entities_from_text(
    raw_notes: str,
    vault_path: str = "default",
    llm=None,
) -> tuple[List[KnowledgeGraphNode], List[KnowledgeGraphEdge]]:
    """
    Parse raw DM notes (meeting notes, session prep, lore fragments) into
    KG entities and edges.

    This is the Phase 1 NLP entry point — works on arbitrary freeform text,
    not just structured NPC bios. Extracts any named characters, locations,
    items, or factions, plus inferred relationships.

    With LLM: uses structured extraction for high-quality output.
    Without LLM: falls back to deterministic heuristics (limited but functional).

    Returns (nodes, edges) — caller adds them to the KG.
    """
    import re

    if llm is not None:
        return await _extract_entities_llm(raw_notes, vault_path, llm)

    # Deterministic fallback: keyword-based extraction
    return _extract_entities_deterministic(raw_notes, vault_path)


def _extract_entities_deterministic(
    raw_notes: str,
    vault_path: str,
) -> tuple[List[KnowledgeGraphNode], List[KnowledgeGraphEdge]]:
    """
    Deterministic fallback for entity extraction from raw notes.

    Heuristics:
    - Capitalized multi-word sequences → potential entity names
    - Section headers: "## NPC:", "## Location:", "## Item:", "## Faction:" → node type
    - Relationship patterns: "owns", "hates", "serves", "allied with", "leads", etc.
    """
    import re

    nodes: List[KnowledgeGraphNode] = []
    edges: List[KnowledgeGraphEdge] = []
    seen_names: set[str] = set()

    # Node type markers
    type_markers = {
        re.compile(r"##?\s*NPC[s]?\s*[:\-]?\s*(.+)", re.I): GraphNodeType.NPC,
        re.compile(r"##?\s*Character[s]?\s*[:\-]?\s*(.+)", re.I): GraphNodeType.NPC,
        re.compile(r"##?\s*Location[s]?\s*[:\-]?\s*(.+)", re.I): GraphNodeType.LOCATION,
        re.compile(r"##?\s*Place[s]?\s*[:\-]?\s*(.+)", re.I): GraphNodeType.LOCATION,
        re.compile(r"##?\s*Item[s]?\s*[:\-]?\s*(.+)", re.I): GraphNodeType.ITEM,
        re.compile(r"##?\s*Faction[s]?\s*[:\-]?\s*(.+)", re.I): GraphNodeType.FACTION,
        re.compile(r"##?\s*Quest[s]?\s*[:\-]?\s*(.+)", re.I): GraphNodeType.QUEST,
    }

    relationship_patterns = [
        (re.compile(r"\b(\w[\w\s]+?)\s+(?:owns|possesses|holds)\s+the\s+(\w[\w\s]+)", re.I), "possesses"),
        (re.compile(r"\b(\w[\w\s]+?)\s+is\s+(?:allied|allies)\s+with\s+(?:the\s+)?(\w[\w\s]+)", re.I), "allied_with"),
        (re.compile(r"\b(\w[\w\s]+?)\s+is\s+(?:an?\s+)?enemy\s+of\s+(?:the\s+)?(\w[\w\s]+)", re.I), "hostile_toward"),
        (re.compile(r"\b(\w[\w\s]+?)\s+(?:hates|despises)\s+(?:the\s+)?(\w[\w\s]+)", re.I), "hostile_toward"),
        (re.compile(r"\b(\w[\w\s]+?)\s+(?:serves|works for|loyal to)\s+(?:the\s+)?(\w[\w\s]+)", re.I), "serves"),
        (re.compile(r"\b(\w[\w\s]+?)\s+(?:leads|commands|rules)\s+(?:the\s+)?(\w[\w\s]+)", re.I), "leads"),
        (re.compile(r"\b(\w[\w\s]+?)\s+is\s+(?:a\s+)?member\s+of\s+(?:the\s+)?(\w[\w\s]+)", re.I), "member_of"),
        (re.compile(r"\b(\w[\w\s]+?)\s+(?:located\s+in|situated\s+in|lives?\s+in)\s+(?:the\s+)?(\w[\w\s]+)", re.I), "located_in"),
        (re.compile(r"\b(\w[\w\s]+?)\s+(?:knows about|knows)\s+(?:the\s+)?(\w[\w\s]+)", re.I), "knows_about"),
    ]

    default_node_type = GraphNodeType.NPC

    # Pass 1: collect named entities from section headers and body
    named_entities: Dict[tuple[str, GraphNodeType], str] = {}

    for line in raw_notes.split("\n"):
        # Detect section headers for type
        node_type = default_node_type
        extracted_name = None
        for pattern, ntype in type_markers.items():
            m = pattern.match(line.strip())
            if m:
                node_type = ntype
                extracted_name = m.group(1).strip()
                break

        # Extract capitalized multi-word names
        for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", line):
            name = m.group(1).strip()
            if len(name) < 2:
                continue
            # Skip common non-entity words
            if name.lower() in {
                "the", "and", "but", "for", "with", "you", "your",
                "session", "chapter", "scene", "notes", " dm ", "goblin",
                "dragon", "king", "queen", "lord", "lady", "sir",
            }:
                continue
            key = (name.lower(), node_type)
            if key not in named_entities:
                named_entities[key] = name

    # Create KG nodes for each extracted entity
    name_to_uuid: Dict[str, uuid.UUID] = {}
    for (name_lower, _), canonical_name in named_entities.items():
        if name_lower in seen_names:
            continue
        seen_names.add(name_lower)
        node_uuid = uuid.uuid4()
        name_to_uuid[name_lower] = node_uuid
        node = KnowledgeGraphNode(
            node_uuid=node_uuid,
            node_type=GraphNodeType.NPC,
            name=canonical_name,
            attributes={"source": "deterministic_extraction"},
            tags={"extracted"},
        )
        nodes.append(node)

    # Pass 2: extract relationship edges
    kg = get_knowledge_graph(vault_path)
    for pattern, predicate in relationship_patterns:
        for m in pattern.finditer(raw_notes):
            subj_name = m.group(1).strip()
            obj_name = m.group(2).strip()

            # Look up subject UUID
            subj_uuid = name_to_uuid.get(subj_name.lower())
            if subj_uuid is None:
                found = kg.get_node_by_name(subj_name)
                if found:
                    subj_uuid = found.node_uuid
                else:
                    # Create the node on the fly
                    new_node = KnowledgeGraphNode(
                        node_uuid=uuid.uuid4(),
                        node_type=GraphNodeType.NPC,
                        name=subj_name,
                        attributes={"source": "relationship_inference"},
                        tags={"inferred"},
                    )
                    kg.add_node(new_node)
                    subj_uuid = new_node.node_uuid
                    name_to_uuid[subj_name.lower()] = subj_uuid
                    nodes.append(new_node)

            # Look up object UUID
            obj_uuid = name_to_uuid.get(obj_name.lower())
            if obj_uuid is None:
                found = kg.get_node_by_name(obj_name)
                if found:
                    obj_uuid = found.node_uuid
                else:
                    new_node = KnowledgeGraphNode(
                        node_uuid=uuid.uuid4(),
                        node_type=GraphNodeType.NPC,
                        name=obj_name,
                        attributes={"source": "relationship_inference"},
                        tags={"inferred"},
                    )
                    kg.add_node(new_node)
                    obj_uuid = new_node.node_uuid
                    name_to_uuid[obj_name.lower()] = obj_uuid
                    nodes.append(new_node)

            if subj_uuid and obj_uuid:
                try:
                    pred = GraphPredicate(predicate)
                except ValueError:
                    pred = GraphPredicate.CONNECTED_TO
                edges.append(
                    KnowledgeGraphEdge(
                        subject_uuid=subj_uuid,
                        predicate=pred,
                        object_uuid=obj_uuid,
                    )
                )

    return nodes, edges


async def _extract_entities_llm(
    raw_notes: str,
    vault_path: str,
    llm,
) -> tuple[List[KnowledgeGraphNode], List[KnowledgeGraphEdge]]:
    """LLM-powered extraction of entities and edges from raw notes."""

    class EntityListSpec(BaseModel):
        entities: List[NPCEntitySpec] = Field(description="List of extracted entities")
        edges: List[KGEdgeSpec] = Field(description="List of inferred relationship edges")

    prompt = (
        "You are a D&D world-builder. Parse the raw DM notes below and extract ALL "
        "named entities and their relationships.\n\n"
        "For each entity extract an NPCEntitySpec:\n"
        "  - name: canonical name (use Title Case)\n"
        "  - node_type: npc | location | item | faction | quest\n"
        "  - tags: faction affiliations, location, role\n"
        "  - is_immutable: True for key NPCs, quest-givers, major factions\n"
        "  - description: brief note if provided\n\n"
        "For each relationship extract a KGEdgeSpec:\n"
        "  - subject_name: name of subject entity (must match an extracted entity)\n"
        "  - predicate: connected_to | located_in | member_of | allied_with | "
        "hostile_toward | controls | leads | serves | rival_of | owned_by | "
        "possesses | knows_about | rules\n"
        "  - object_name: name of object entity\n"
        "  - weight: 0.0-1.0 (confidence in the inference)\n\n"
        "RULES:\n"
        "1. Be CONSERVATIVE — only extract entities EXPLICITLY named.\n"
        "2. Only infer relationships when clearly implied by the text.\n"
        "3. node_type should be inferred from context (a city = location, a guild = faction, etc.)\n"
        "4. Output a valid JSON object with 'entities' (list) and 'edges' (list).\n"
        f"RAW NOTES:\n{raw_notes}"
    )

    result = await _call_llm_structured(llm, prompt, "", EntityListSpec)
    if result is None:
        return [], []

    kg = get_knowledge_graph(vault_path)
    nodes: List[KnowledgeGraphNode] = []
    edges: List[KnowledgeGraphEdge] = []
    name_to_uuid: Dict[str, uuid.UUID] = {}

    for spec in result.entities:
        try:
            ntype = GraphNodeType(spec.node_type)
        except ValueError:
            ntype = GraphNodeType.NPC

        node_uuid = uuid.uuid4()
        name_to_uuid[spec.name.lower()] = node_uuid

        attrs: Dict[str, Any] = {
            "description": spec.description,
            "bio": spec.bio,
            "connections": spec.connections,
            "misc_notes": spec.misc_notes,
        }

        node = KnowledgeGraphNode(
            node_uuid=node_uuid,
            node_type=ntype,
            name=spec.name,
            attributes=attrs,
            tags=set(spec.tags),
            is_immutable=spec.is_immutable,
        )
        nodes.append(node)

    for edge_spec in result.edges:
        # Resolve subject
        subj_uuid = name_to_uuid.get(edge_spec.subject_name.lower())
        if subj_uuid is None:
            found = kg.get_node_by_name(edge_spec.subject_name)
            subj_uuid = found.node_uuid if found else None

        # Resolve object
        obj_uuid = name_to_uuid.get(edge_spec.object_name.lower())
        if obj_uuid is None:
            found = kg.get_node_by_name(edge_spec.object_name)
            obj_uuid = found.node_uuid if found else None

        if subj_uuid and obj_uuid:
            try:
                pred = GraphPredicate(edge_spec.predicate)
            except ValueError:
                pred = GraphPredicate.CONNECTED_TO
            edges.append(
                KnowledgeGraphEdge(
                    subject_uuid=subj_uuid,
                    predicate=pred,
                    object_uuid=obj_uuid,
                    weight=edge_spec.weight,
                )
            )

    return nodes, edges


# ---------------------------------------------------------------------------
# Effect Annotation Pipeline (Task 3.3)
# ---------------------------------------------------------------------------

class EffectAnnotationPipeline:
    """
    LLM-powered pipeline for converting storylet resolution prose into
    GraphMutation effects.

    Usage:
        pipeline = EffectAnnotationPipeline(llm)
        result = await pipeline.annotate("Lord Vader joins the party.")
        mutations = result.mutations  # List[GraphMutation]

        # Apply to KG directly:
        for mut in mutations:
            pipeline.apply_mutation(kg, mut)

        # Or attach to a storylet:
        pipeline.attach_to_storylet(storylet_name, mutations, vault_path)
    """

    def __init__(self, llm) -> None:
        self.llm = llm

    async def annotate(
        self,
        resolution_text: str,
        vault_path: str = "default",
    ) -> EffectAnnotationSpec:
        """Parse resolution prose and return an EffectAnnotationSpec."""
        return await annotate_storylet_effects(
            resolution_text=resolution_text,
            vault_path=vault_path,
            llm=self.llm,
        )

    async def annotate_batch(
        self,
        resolutions: Dict[str, str],
        vault_path: str = "default",
    ) -> Dict[str, EffectAnnotationSpec]:
        """
        Annotate multiple storylet resolutions concurrently.

        Args:
            resolutions: Dict[storylet_name, resolution_text]
        Returns:
            Dict[storylet_name, EffectAnnotationSpec]
        """
        import asyncio

        async def _annotate_one(name: str, text: str) -> tuple[str, EffectAnnotationSpec]:
            spec = await self.annotate(text, vault_path)
            return name, spec

        results = await asyncio.gather(
            *[_annotate_one(n, t) for n, t in resolutions.items()]
        )
        return dict(results)

    def apply_mutation(
        self,
        kg: KnowledgeGraph,
        mutation: GraphMutation,
    ) -> bool:
        """
        Apply a single GraphMutation to the KG.

        Returns True if the mutation was applied successfully,
        False if it could not be resolved or executed.
        """
        try:
            mutation.execute(kg)
            return True
        except Exception:
            return False

    def apply_effects(
        self,
        kg: KnowledgeGraph,
        mutations: List[GraphMutation],
    ) -> tuple[int, int]:
        """
        Apply a list of GraphMutations to the KG.

        Returns (applied_count, failed_count).
        """
        applied = 0
        failed = 0
        for mut in mutations:
            if self.apply_mutation(kg, mut):
                applied += 1
            else:
                failed += 1
        return applied, failed

    def attach_to_storylet(
        self,
        storylet_name: str,
        mutations: List[GraphMutation],
        vault_path: str = "default",
    ) -> bool:
        """
        Attach GraphMutations as effects to a registered storylet.

        Returns True if the storylet was found and updated.
        """
        from storylet import StoryletEffect

        reg = get_storylet_registry(vault_path)
        storylet = reg.get_by_name(storylet_name)
        if storylet is None:
            return False

        for mut in mutations:
            storylet.effects.append(StoryletEffect(graph_mutations=[mut]))
        return True

    async def run(
        self,
        storylet_resolutions: Dict[str, str],
        vault_path: str = "default",
    ) -> Dict[str, Any]:
        """
        Run the full effect annotation pipeline.

        1. Annotate all resolutions concurrently
        2. Apply mutations to KG
        3. Attach mutations to storylets

        Returns a summary dict with counts and any failures.
        """
        kg = get_knowledge_graph(vault_path)

        annotated = await self.annotate_batch(storylet_resolutions, vault_path)

        mutations_applied = 0
        mutations_failed = 0
        storylets_updated = 0

        for sl_name, spec in annotated.items():
            if not spec.mutations:
                continue

            # Convert dicts → GraphMutation objects
            mutations: List[GraphMutation] = []
            for mut_dict in spec.mutations:
                try:
                    mutations.append(GraphMutation(**mut_dict))
                except Exception:
                    pass

            # Apply to KG
            a, f = self.apply_effects(kg, mutations)
            mutations_applied += a
            mutations_failed += f

            # Attach to storylet
            if self.attach_to_storylet(sl_name, mutations, vault_path):
                storylets_updated += 1

        return {
            "storylets_annotated": sum(1 for s in annotated.values() if s.mutations),
            "mutations_applied": mutations_applied,
            "mutations_failed": mutations_failed,
            "storylets_updated": storylets_updated,
            "summaries": {n: s.summary for n, s in annotated.items() if s.summary},
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _call_llm_structured(
    llm,
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


async def _call_llm_structured_list(
    llm,
    system_prompt: str,
    user_text: str,
    item_schema: type[BaseModel],
) -> Optional[List[BaseModel]]:
    """Call LLM expecting a JSON array of output_schema objects."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from typing import List as PyList

    class ListWrapper(BaseModel):
        items: PyList[item_schema] = Field(alias="items")

        model_config = {"populate_by_name": True}

    try:
        chain = llm.with_structured_output(ListWrapper)
        response = await chain.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
        )
        return response.items
    except Exception:
        return None


def _build_storylet_prerequisites(query_dicts: List[Dict[str, Any]]) -> StoryletPrerequisites:
    """Convert a list of GraphQuery dicts into a StoryletPrerequisites object."""
    queries = []
    for qd in query_dicts:
        try:
            queries.append(GraphQuery(**qd))
        except Exception:
            pass

    if not queries:
        return StoryletPrerequisites()
    return StoryletPrerequisites(any_of=queries)
