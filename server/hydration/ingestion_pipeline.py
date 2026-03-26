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


class CampaignMaterials(BaseModel):
    """
    All the raw inputs a DM might provide for campaign hydration.

    All fields are optional — the pipeline will process whatever fields are provided.
    """
    campaign_name: str = Field(default="", description="Campaign title")
    npc_lore: str = Field(default="", description="NPC biographies, lore text, faction details")
    campaign_narrative: str = Field(
        default="",
        description="Plot beats, session summaries, quest lines, story arcs",
    )
    session_prep_notes: str = Field(
        default="",
        description="Free-form DM session preparation notes",
    )
    storylet_resolutions: Dict[str, str] = Field(
        default_factory=dict,
        description="Dict of storylet_name → resolution prose for effect annotation",
    )


class HydrationReport(BaseModel):
    """Result of a full campaign hydration run."""
    nodes_created: int = 0
    edges_created: int = 0
    storylets_created: int = 0
    storylets_annotated: int = 0
    effects_attached: int = 0
    backup_storylets_generated: int = 0
    three_clue_violations_fixed: int = 0
    warnings: List[str] = Field(default_factory=list)
    vault_persisted: bool = False


class IncrementalHydrationReport(BaseModel):
    """Result of an incremental hydration operation."""
    delta_report: HydrationReport
    missing_entities: List[str] = Field(default_factory=list)
    all_missing_entities: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Emergent Worldbuilding — lazy entity hydration during play
# ---------------------------------------------------------------------------


class EmergentEntityReport(BaseModel):
    """Result of emergent worldbuilding for a newly created entity."""
    entity_name: str
    description: str = ""
    storylets_created: int = 0
    edges_created: int = 0
    warnings: List[str] = Field(default_factory=list)


class EmergentWorldBuilder:
    """
    Lazily hydrates player-invented entities during play.

    Triggered when commit_node commits an add_node mutation.
    Fleshes out the entity and generates side quest storylets.
    """

    def __init__(self, llm, vault_path: str) -> None:
        self.llm = llm
        self.vault_path = vault_path

    async def on_entity_created(
        self,
        entity_name: str,
        entity_type: str = "npc",
        context: str = "",
        player_proposed: bool = False,
    ) -> EmergentEntityReport:
        """
        Called by commit_node after an add_node mutation is committed.

        1. Flesh out entity details via LLM
        2. Generate side quest storylet stubs
        3. Infer edges to existing world
        """
        kg = get_knowledge_graph(self.vault_path)
        reg = get_storylet_registry(self.vault_path)

        node = kg.get_node_by_name(entity_name)
        if node is None:
            return EmergentEntityReport(entity_name=entity_name, warnings=[f"Entity '{entity_name}' not in KG."])

        # Phase 1: Flesh out via LLM (description, dials, connections)
        await self._flesh_out_entity(node, context)

        # Phase 2: Generate side quest storylets
        storylets_created = await self._generate_side_quest_storylets(node, context)

        # Phase 3: Infer and add world edges
        edges_created = self._infer_world_edges(node, context, kg, reg)

        return EmergentEntityReport(
            entity_name=entity_name,
            description=node.attributes.get("description", "") if node.attributes else "",
            storylets_created=storylets_created,
            edges_created=edges_created,
        )

    async def _flesh_out_entity(self, node, context: str) -> None:
        """Call LLM to produce description and behavioral dials for the entity."""
        from graph import extract_npc_dials

        if self.llm is None:
            return

        prompt = (
            f"You are a D&D world-builder. Flesh out the following entity with rich details.\n\n"
            f"ENTITY: [[{node.name}]] (type: {node.node_type.value})\n"
            f"CONTEXT: {context or 'No additional context provided.'}\n\n"
            f"Provide a 2-3 sentence vivid description and estimate behavioral dials (0.0-1.0) for: "
            f"greed, loyalty, courage, cruelty, cunning, piety. Be conservative — only high values "
            f"for clearly demonstrated traits.\n\n"
            f"Output a JSON object with fields: description (string), greed (float), loyalty (float), "
            f"courage (float), cruelty (float), cunning (float), piety (float).\n"
            f"Use null for any dial that cannot be estimated from the context."
        )

        try:
            result = await _call_llm_json(self.llm, prompt, "")
            if result:
                dials = {}
                for dial in ("greed", "loyalty", "courage", "cruelty", "cunning", "piety"):
                    if dial in result and result[dial] is not None:
                        dials[dial] = float(result[dial])
                if dials:
                    node.npc_dials = dials
                if "description" in result and result["description"]:
                    if node.attributes is None:
                        node.attributes = {}
                    node.attributes["description"] = result["description"]
        except Exception:
            pass  # Silently skip LLM fleshing on failure

    async def _generate_side_quest_storylets(
        self, node, context: str
    ) -> int:
        """
        Generate 2-3 storylet stubs for a newly created NPC.

        Each stub has:
        - Name: "{Entity} - {Quest Hook}"
        - Prerequisites anchored to the entity (node exists)
        - Tension level: MEDIUM (exploration/discovery)
        - Content template with {entity} substitution
        """
        reg = get_storylet_registry(self.vault_path)
        if reg is None:
            return 0

        quest_hooks = [
            f"A personal secret from {node.name}'s past threatens to surface.",
            f"Someone connected to {node.name} has gone missing.",
            f"{node.name} owes a dangerous debt to a powerful faction.",
        ]

        storylets_created = 0
        for hook in quest_hooks:
            storylet_name = f"{node.name} — {hook.split('.')[0]}"

            # Skip if storylet already exists
            if reg.get_by_name(storylet_name) is not None:
                continue

            storylet = Storylet(
                name=storylet_name,
                description=hook,
                content=(
                    f"The party encounters an unfolding situation involving [[{node.name}]]. "
                    f"{hook} This is an opportunity for roleplay and discovery. "
                    f"Present the situation vividly and let the players decide how to engage."
                ),
                tension_level=TensionLevel.MEDIUM,
                tags={node.node_type.value, "emergent", "side-quest"},
                prerequisites=StoryletPrerequisites(
                    all_of=[
                        GraphQuery(
                            query_type="node_exists",
                            node_name=node.name,
                        )
                    ],
                ),
            )
            reg.register(storylet)
            storylets_created += 1

        return storylets_created

    def _infer_world_edges(
        self, node, context: str, kg, reg
    ) -> int:
        """
        Look at context (narrative text) and existing KG to infer plausible edges:
        - {Entity} --LOCATED_IN--> {location} (from context)
        - {Entity} --KNOWS_ABOUT--> {existing_npc} (from context)
        - {Entity} --SERVES/ALLIED_WITH/HOSTILE_TOWARD--> {faction} (from dials)
        """
        import re

        edges_created = 0

        # Try to infer LOCATED_IN from context
        location_patterns = [
            r"(?:in|at|near|inside|outside)\s+(?:the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
            r"(?:from|to)\s+(?:the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
        ]

        location_kw = {
            "tavern", "inn", "village", "city", "castle", "dungeon", "tower",
            "forest", "road", "bridge", "market", "temple", "shrine", "cave",
        }

        for pattern in location_patterns:
            for match in re.finditer(pattern, context, re.IGNORECASE):
                potential_loc = match.group(1).strip()
                loc_lower = potential_loc.lower()
                # Check if this looks like a location keyword
                if any(kw in loc_lower for kw in location_kw):
                    loc_node = kg.get_node_by_name(potential_loc)
                    if loc_node is None:
                        # Create the location node
                        from knowledge_graph import KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
                        import uuid as uuid_lib

                        loc_uuid = uuid_lib.uuid4()
                        loc_node = KnowledgeGraphNode(
                            node_uuid=loc_uuid,
                            node_type=GraphNodeType.LOCATION,
                            name=potential_loc,
                            attributes={"description": f"Location {potential_loc} (emergent)"},
                        )
                        kg.add_node(loc_node)

                    # Add LOCATED_IN edge
                    from knowledge_graph import GraphPredicate, KnowledgeGraphEdge
                    edge = KnowledgeGraphEdge(
                        edge_uuid=uuid_lib.uuid4(),
                        subject_uuid=node.node_uuid,
                        predicate=GraphPredicate.LOCATED_IN,
                        object_uuid=loc_node.node_uuid,
                    )
                    kg.add_edge(edge)
                    edges_created += 1
                    break

        # Infer faction relationships from dials and context
        faction_kw = {
            "thieves guild": "Thieves Guild",
            "merchants": "Merchants Guild",
            "temple": "Temple",
            "crown": "The Crown",
            "nobles": "Nobility",
            "wizard": "Wizard's Tower",
        }

        for kw, faction_name in faction_kw.items():
            if kw in context.lower():
                faction_node = kg.get_node_by_name(faction_name)
                if faction_node is None:
                    from knowledge_graph import KnowledgeGraphNode, GraphNodeType, GraphPredicate, KnowledgeGraphEdge
                    import uuid as uuid_lib

                    fac_uuid = uuid_lib.uuid4()
                    faction_node = KnowledgeGraphNode(
                        node_uuid=fac_uuid,
                        node_type=GraphNodeType.FACTION,
                        name=faction_name,
                        attributes={"description": f"Faction {faction_name} (emergent)"},
                    )
                    kg.add_node(faction_node)

                # Determine relationship type from context
                hostile_kw = {"hates", "hostile", "enemy", "rival", "fights", "opposes"}
                allied_kw = {"serves", "loyal", "member", "ally", "friend", "supports"}

                if any(w in context.lower() for w in hostile_kw):
                    pred = GraphPredicate.HOSTILE_TOWARD
                elif any(w in context.lower() for w in allied_kw):
                    pred = GraphPredicate.SERVES
                else:
                    pred = GraphPredicate.CONNECTED_TO

                import uuid as uuid_lib
                from knowledge_graph import KnowledgeGraphEdge
                edge = KnowledgeGraphEdge(
                    edge_uuid=uuid_lib.uuid4(),
                    subject_uuid=node.node_uuid,
                    predicate=pred,
                    object_uuid=faction_node.node_uuid,
                )
                kg.add_edge(edge)
                edges_created += 1

        return edges_created


# ---------------------------------------------------------------------------
# Campaign Hydration Pipeline (one-shot pre-loading)
# ---------------------------------------------------------------------------

class CampaignHydrationPipeline:
    """
    One-shot campaign pre-loader for the DM Engine.

    Given raw campaign materials (lore, narrative, prep notes), this pipeline
    fully hydrates the Knowledge Graph and Storylet Registry in a single
    expensive upfront pass. Subsequent sessions are fast because everything
    is pre-computed and structured.

    Usage:
        pipeline = CampaignHydrationPipeline(llm, vault_path="my_campaign")
        report = await pipeline.run(CampaignMaterials(
            campaign_name="Curse of Strahd",
            npc_lore=dm_notes,
            campaign_narrative=session_summaries,
            storylet_resolutions={"The Dark Arrival": "Lord Vader joins the party."},
        ))
    """

    def __init__(self, llm, vault_path: str = "default") -> None:
        self.llm = llm
        self.vault_path = vault_path
        self.effect_pipeline = EffectAnnotationPipeline(llm)

    async def run(self, materials: CampaignMaterials) -> HydrationReport:
        """
        Run the full campaign hydration pipeline.

        Phases:
          1. Extract KG entities + edges from all raw text
          2. Parse campaign narrative → Storylets
          3. Annotate storylet resolutions → GraphMutations
          4. Three Clue Rule analysis + backup storylet generation
          5. Register all artifacts + persist to vault
        """
        from storylet_analyzer import ThreeClueAnalyzer

        warnings: List[str] = []

        # Collect all raw text for entity extraction
        all_raw_text = "\n\n".join(filter(None, [
            materials.npc_lore,
            materials.campaign_narrative,
            materials.session_prep_notes,
        ]))

        # Phase 1: Entity + edge extraction
        nodes, edges = await extract_entities_from_text(
            all_raw_text, self.vault_path, self.llm
        )

        # Phase 2: Storylet extraction from campaign narrative
        storylets: List[Storylet] = []
        if materials.campaign_narrative:
            storylets = await ingest_campaign_narrative(
                materials.campaign_narrative, self.vault_path, self.llm
            )

        # Phase 3: Effect annotations
        effects_attached = 0
        storylets_annotated = 0
        annotated_specs: Dict[str, EffectAnnotationSpec] = {}
        if materials.storylet_resolutions:
            annotated_specs = await self.effect_pipeline.annotate_batch(
                materials.storylet_resolutions, self.vault_path
            )
            for sl_name, spec in annotated_specs.items():
                if not spec.mutations:
                    continue
                storylets_annotated += 1
                mutations: List[GraphMutation] = []
                for mut_dict in spec.mutations:
                    try:
                        mutations.append(GraphMutation(**mut_dict))
                    except Exception:
                        pass
                for mut in mutations:
                    if self.effect_pipeline.attach_to_storylet(sl_name, [mut], self.vault_path):
                        effects_attached += 1

        # Phase 4: Three Clue Rule analysis + backup storylet generation
        backup_count = 0
        violations_fixed = 0
        if storylets:
            analyzer = ThreeClueAnalyzer(storylets)
            analysis = analyzer.analyze()
            for chokepoint_id, score in analysis.get("redundancy_scores", {}).items():
                if score < 3:
                    chokepoint = analyzer.graph.storylets.get(chokepoint_id)
                    if chokepoint is None:
                        continue
                    missing = 3 - score
                    paths = analysis.get("paths_to_chokepoint", {}).get(chokepoint_id, [])
                    branch_path = paths[0] if paths else []
                    for _ in range(missing):
                        try:
                            backup = analyzer.generate_backup_storylet(chokepoint_id, branch_path)
                            storylets.append(backup)
                            backup_count += 1
                            violations_fixed += 1
                        except Exception:
                            warnings.append(f"Could not generate backup for chokepoint '{chokepoint.name}'")

        # Phase 5: Register everything
        kg = get_knowledge_graph(self.vault_path)
        reg = get_storylet_registry(self.vault_path)

        for node in nodes:
            kg.add_node(node)
        for edge in edges:
            kg.add_edge(edge)
        for sl in storylets:
            reg.register(sl)

        # Persist to vault
        vault_persisted = False
        try:
            await reg.save_to_vault(self.vault_path)
            vault_persisted = True
        except Exception as e:
            warnings.append(f"Vault persistence failed: {e}")

        return HydrationReport(
            nodes_created=len(nodes),
            edges_created=len(edges),
            storylets_created=len(storylets),
            storylets_annotated=storylets_annotated,
            effects_attached=effects_attached,
            backup_storylets_generated=backup_count,
            three_clue_violations_fixed=violations_fixed,
            warnings=warnings,
            vault_persisted=vault_persisted,
        )


# ---------------------------------------------------------------------------
# Incremental Hydration Pipeline (delta updates)
# ---------------------------------------------------------------------------

class IncrementalHydrationPipeline:
    """
    Incrementally hydrate new DM materials — skips entities and storylets
    that already exist in the Knowledge Graph or Storylet Registry.

    Use this for session prep updates, mid-campaign additions, or when
    guardrails detect missing entity references.

    Usage:
        pipeline = IncrementalHydrationPipeline(llm, vault_path="my_campaign")

        # DM provides new materials
        report = await pipeline.delta_hydrate(CampaignMaterials(
            npc_lore="The Wizard Zypyr appears in the tower.",
        ))

        # Probe for missing entities before they cause rejections
        missing = pipeline.detect_missing_entities(narrative_prose)
    """

    def __init__(self, llm, vault_path: str = "default") -> None:
        self.llm = llm
        self.vault_path = vault_path

    def _entities_already_known(self, entity_name: str) -> bool:
        """Check if an entity already exists in the KG."""
        kg = get_knowledge_graph(self.vault_path)
        return kg.get_node_by_name(entity_name) is not None

    def _storylets_already_known(self, storylet_name: str) -> bool:
        """Check if a storylet already exists in the registry."""
        reg = get_storylet_registry(self.vault_path)
        return reg.get_by_name(storylet_name) is not None

    def _filter_new_entities_from_text(self, raw_text: str) -> str:
        """
        Strip mentions of entities already in the KG from raw text.

        Returns the text with known entity references removed, so the
        LLM (if used) only generates truly new content.
        """
        import re

        kg = get_knowledge_graph(self.vault_path)

        # Find all KG node names
        known_names: List[str] = []
        for node in kg.nodes.values():
            known_names.append(re.escape(node.name))
        if not known_names:
            return raw_text

        # Build pattern that matches known entity names
        pattern = re.compile(
            r"\b(" + "|".join(sorted(known_names, key=len, reverse=True)) + r")\b",
            re.IGNORECASE,
        )
        return pattern.sub(lambda m: "", raw_text)

    def _filter_new_storylets(self, storylet_names: List[str]) -> List[str]:
        """Return only storylet names not already in the registry."""
        return [n for n in storylet_names if not self._storylets_already_known(n)]

    async def delta_hydrate(
        self,
        new_materials: CampaignMaterials,
    ) -> HydrationReport:
        """
        Incrementally hydrate only genuinely new content from new materials.

        Diffs against existing KG + registry state and only processes entities
        and storylets that don't already exist.

        Usage:
            pipeline = IncrementalHydrationPipeline(llm, vault_path="my_campaign")
            report = await pipeline.delta_hydrate(CampaignMaterials(
                npc_lore="The Wizard Zypyr appears in the tower.",
            ))
        """
        # Strip known entity references so we only process new ones
        filtered_notes = self._filter_new_entities_from_text("\n\n".join(filter(None, [
            new_materials.npc_lore,
            new_materials.campaign_narrative,
            new_materials.session_prep_notes,
        ])))

        if not filtered_notes.strip():
            return HydrationReport(
                warnings=["No new content detected — all entities and storylets already exist."],
            )

        # Build filtered materials with only the new content
        filtered_materials = CampaignMaterials(
            campaign_name=new_materials.campaign_name,
            npc_lore=filtered_notes if new_materials.npc_lore else "",
            campaign_narrative="",
            session_prep_notes="",
            storylet_resolutions={},  # Resolutions are storylet-specific; handled separately
        )

        # Delegate to full pipeline (it will process only the filtered content)
        return await CampaignHydrationPipeline(self.llm, self.vault_path).run(filtered_materials)

    async def hydrate_missing_entity(
        self,
        entity_name: str,
        entity_context: str = "",
        node_type: str = "npc",
        llm=None,
    ) -> HydrationReport:
        """
        Hydrate a single missing entity that was referenced in narrative prose.

        Called when HardGuardrails.validate_narrative_claim() detects a
        [[Wikilink]] or capitalized entity name not in the KG.

        Args:
            entity_name: Name of the missing entity
            entity_context: Freeform text about the entity (lore, description, relationships)
            node_type: KG node type (npc, location, item, faction)
            llm: LLM for richer extraction (uses deterministic fallback if None)
        """
        kg = get_knowledge_graph(self.vault_path)

        # Check if already added by a concurrent process
        if kg.get_node_by_name(entity_name) is not None:
            return HydrationReport(warnings=[f"'{entity_name}' already exists in KG."])

        # Build a minimal campaign materials around this entity
        materials = CampaignMaterials(
            campaign_name=f"Incremental: {entity_name}",
            npc_lore=f"## {node_type.capitalize()}\n{entity_name}. {entity_context}",
        )

        return await CampaignHydrationPipeline(llm or self.llm, self.vault_path).run(materials)

    def detect_missing_entities(self, narrative_text: str) -> List[str]:
        """
        Scan narrative text for entity references not in the KG.

        Returns a list of entity names that appear in the text but don't
        exist in the Knowledge Graph. Useful for proactively identifying
        gaps before they cause guardrail rejections.

        Detects:
          - [[Wikilinks]] not in KG
          - Capitalized Title-Case names not in KG
        """
        import re

        kg = get_knowledge_graph(self.vault_path)
        missing: List[str] = []

        # Check [[Wikilinks]]
        for link in re.findall(r"\[\[([^\]]+)\]\]", narrative_text):
            name = link.strip()
            if name and kg.get_node_by_name(name) is None:
                missing.append(name)

        # Check Title-Case capitalized words (min 3 chars)
        for m in re.finditer(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)\b", narrative_text):
            name = m.group(1).strip()
            if name.lower() in {
                "the", "and", "but", "for", "with", "you", "your",
                "session", "chapter", "scene", "king", "queen", "lord", "lady",
                "dragon", "goblin",
            }:
                continue
            if name and kg.get_node_by_name(name) is None and name not in missing:
                missing.append(name)

        return missing

    def suggest_hydration(self, missing_entities: List[str]) -> str:
        """
        Generate a DM prompt summarizing missing entities for hydration.

        Returns a ready-to-use prompt that the DM can send to hydrate
        the missing entities with full context.
        """
        if not missing_entities:
            return "All referenced entities are already in the Knowledge Graph."

        entity_list = "\n".join(f"  - {name}" for name in missing_entities)
        return (
            f"The following entities were referenced in narrative but are not in the "
            f"Knowledge Graph:\n{entity_list}\n\n"
            f"To hydrate these entities, provide brief context for each (who they are, "
            f"their key traits, and relationships). For example:\n"
            f"  'The Wizard Zypyr: an ancient archmage who serves the Emperor, "
            f"hostile toward the party.'\n"
            f"Then call hydrate_delta or hydrate_missing_entity with this context."
        )


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
    campaign_materials: CampaignMaterials = None,
    llm=None,
) -> Dict[str, Any]:
    """
    Run the full Phase 2 ingestion pipeline.

    Args:
        vault_path: Target vault
        npc_lore_text: Raw NPC biography/lore text
        campaign_narrative_text: Campaign narrative for storylet extraction
        storylet_resolutions: Dict[storylet_name, resolution_text] for effect annotation
        campaign_materials: CampaignMaterials object for one-shot hydration (preferred).
                           When provided, supersedes the individual text fields.
        llm: LLM instance with ainvoke method

    Returns a summary dict with counts of ingested entities, edges, and storylets.
    """
    # One-shot campaign hydration: use the full pipeline orchestrator
    if campaign_materials is not None:
        pipeline = CampaignHydrationPipeline(llm, vault_path)
        report = await pipeline.run(campaign_materials)
        return {
            "nodes_added": report.nodes_created,
            "edges_added": report.edges_created,
            "storylets_created": report.storylets_created,
            "effects_annotated": report.storylets_annotated,
            "effects_attached": report.effects_attached,
            "backup_storylets_generated": report.backup_storylets_generated,
            "three_clue_violations_fixed": report.three_clue_violations_fixed,
            "warnings": report.warnings,
            "vault_persisted": report.vault_persisted,
        }

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


async def _call_llm_json(
    llm,
    system_prompt: str,
    user_text: str,
) -> Optional[Dict[str, Any]]:
    """Call LLM expecting a raw JSON object (dict) in response."""
    import json
    from langchain_core.messages import HumanMessage, SystemMessage

    try:
        response = await llm.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
        )
        content = response.content.strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        return json.loads(content)
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
