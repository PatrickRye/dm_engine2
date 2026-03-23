# flake8: noqa: W293, E203
"""
narrative_tools - Story, graph mutations, and backstory tools
"""
import asyncio
import os
import re
import threading
import yaml
import random
import math
import aiofiles
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from pydantic import Field
from typing import Optional, Annotated, Union, Dict
import uuid

# === DETERMINISTIC ENGINE INTEGRATION ===
from dnd_rules_engine import (
    EventBus,
    GameEvent,
    EventStatus,
    BaseGameEntity,
    Creature,
    MeleeWeapon,
    ActiveCondition,
    ModifiableValue,
    NumericalModifier,
    ModifierPriority,
    WeaponProperty,
)
from state import ClassLevel, PCDetails, NPCDetails, LocationDetails, FactionDetails
from vault_io import (
    get_journals_dir,
    write_audit_log,
    upsert_journal_section,
    read_markdown_entity,
    edit_markdown_entity,
)
from compendium_manager import CompendiumManager, CompendiumEntry, MechanicEffect
from spatial_engine import spatial_service, LightSource, Wall, HAS_GIS
from spell_system import SpellDefinition, SpellMechanics, SpellCompendium
from item_system import WeaponItem, ArmorItem, WondrousItem, ItemCompendium

from registry import get_all_entities, register_entity, get_entity, get_candidate_uuids_by_prefix

# Import helpers from roll_utils
from roll_utils import (
    VaultCache,
    _VAULT_CACHE,
    update_roll_automations,
    get_roll_automations,
    _calculate_reach,
    _build_npc_template,
    _build_location_template,
    _build_faction_template,
    _build_pc_template,
    _build_party_tracker,
    _get_config_tone,
    _get_config_settings,
    _get_config_dirs,
    _search_markdown_for_keywords,
    _get_entity_by_name,
    _get_current_combat_initiative,
)




@tool
async def create_storylet(  # noqa: C901
    name: str,
    description: str,
    tension_level: str = "medium",
    prerequisites: str = "{}",
    content: str = "",
    effects: str = "[]",
    tags: str = "[]",
    max_occurrences: int = 1,
    priority_override: int = 0,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Creates a new storylet in the storylet registry. Storylets are narrative
    units anchored to the Knowledge Graph that drive campaign pacing.

    Args:
        name: Human-readable name for the storylet.
        description: Short description of the narrative beat.
        tension_level: 'low', 'medium', or 'high' — controls when the Drama Manager selects this.
        prerequisites: JSON-encoded StoryletPrerequisites (e.g., '{"all_of": [{"query_type": "node_exists", "node_name": "Goblin Cave"}]}').
        content: The narrative text or prompt template. Use {variable} for runtime substitution.
        effects: JSON-encoded list of StoryletEffects (graph mutations applied when storylet fires).
        tags: JSON-encoded list of string tags for categorization.
        max_occurrences: How many times this storylet can fire (-1 = unlimited).
        priority_override: Higher = selected first when multiple storylets are valid.
    """
    import json
    from registry import get_storylet_registry
    from storylet import (
        Storylet,
        StoryletPrerequisites,
        StoryletEffect,
        GraphQuery,
        GraphMutation,
        TensionLevel,
    )

    vault_path = config["configurable"].get("thread_id")

    try:
        prereq_data = json.loads(prerequisites) if prerequisites else {}
        effects_data = json.loads(effects) if effects else []
        tags_list = json.loads(tags) if tags else []
    except json.JSONDecodeError as e:
        return f"SYSTEM ERROR: Invalid JSON in prerequisites/effects/tags: {e}"

    # Reconstruct GraphQueries
    def restore_query(qdata: dict) -> GraphQuery:
        return GraphQuery(
            query_type=qdata.get("query_type", "node_exists"),
            node_uuid=uuid.UUID(qdata["node_uuid"]) if qdata.get("node_uuid") else None,
            node_name=qdata.get("node_name"),
            node_type=qdata.get("node_type"),
            predicate=qdata.get("predicate"),
            target_uuid=uuid.UUID(qdata["target_uuid"]) if qdata.get("target_uuid") else None,
            target_name=qdata.get("target_name"),
            attribute=qdata.get("attribute"),
            op=qdata.get("op", "eq"),
            value=qdata.get("value"),
        )

    def restore_mutation(mdata: dict) -> GraphMutation:
        return GraphMutation(
            mutation_type=mdata.get("mutation_type", "add_edge"),
            node_uuid=uuid.UUID(mdata["node_uuid"]) if mdata.get("node_uuid") else None,
            node_name=mdata.get("node_name"),
            node_type=mdata.get("node_type"),
            predicate=mdata.get("predicate"),
            target_uuid=uuid.UUID(mdata["target_uuid"]) if mdata.get("target_uuid") else None,
            target_name=mdata.get("target_name"),
            attribute=mdata.get("attribute"),
            value=mdata.get("value"),
            tags=mdata.get("tags"),
        )

    def restore_effect(edata: dict) -> StoryletEffect:
        return StoryletEffect(
            id=uuid.UUID(edata["id"]) if edata.get("id") else uuid.uuid4(),
            graph_mutations=[restore_mutation(m) for m in edata.get("graph_mutations", [])],
            flag_changes=edata.get("flag_changes", {}),
            attribute_mods=edata.get("attribute_mods", {}),
        )

    try:
        tension = TensionLevel(tension_level.lower())
    except ValueError:
        return f"SYSTEM ERROR: Invalid tension_level '{tension_level}'. Must be 'low', 'medium', or 'high'."

    prerequisites_obj = StoryletPrerequisites(
        all_of=[restore_query(q) for q in prereq_data.get("all_of", [])],
        any_of=[restore_query(q) for q in prereq_data.get("any_of", [])],
        none_of=[restore_query(q) for q in prereq_data.get("none_of", [])],
    )
    effects_list = [restore_effect(e) for e in effects_data]

    storylet = Storylet(
        name=name,
        description=description,
        tension_level=tension,
        prerequisites=prerequisites_obj,
        content=content,
        effects=effects_list,
        tags=set(tags_list),
        max_occurrences=max_occurrences,
        priority_override=priority_override if priority_override else None,
    )

    registry = get_storylet_registry(vault_path)
    registry.register(storylet)

    # Persist to vault — await directly since we're in an async tool
    try:
        await registry.save_to_vault(vault_path)
    except Exception:
        pass  # Best-effort persistence; failures logged by save_to_vault

    return (
        f"MECHANICAL TRUTH: Created storylet '{name}' (id={storylet.id}, tension={tension_level}). "
        f"Prerequisites: {len(prerequisites_obj.all_of)} required, {len(prerequisites_obj.any_of)} optional. "
        f"Effects: {len(effects_list)} mutations."
    )



@tool
async def list_active_storylets(
    tension_filter: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Returns all storylets whose prerequisites are currently met.

    Use this to discover what narrative beats are available given the current
    Knowledge Graph state.

    Args:
        tension_filter: Optional 'low', 'medium', or 'high' to filter by tension level.
    """
    from registry import get_storylet_registry, get_knowledge_graph
    from storylet import TensionLevel

    vault_path = config["configurable"].get("thread_id")
    registry = get_storylet_registry(vault_path)
    kg = get_knowledge_graph(vault_path)

    ctx = {"vault_path": vault_path}
    tension = None
    if tension_filter:
        try:
            tension = TensionLevel(tension_filter.lower())
        except ValueError:
            return f"SYSTEM ERROR: Invalid tension_filter '{tension_filter}'. Must be 'low', 'medium', or 'high'."

    candidates = await registry.poll(kg, ctx, tension=tension)

    if not candidates:
        return "No storylets currently have their prerequisites met."

    lines = [f"Active storylets ({len(candidates)}):"]
    for s in candidates:
        lines.append(
            f"  - [{s.tension_level.value}] {s.name} (id={s.id}, fires={s.current_occurrences}/{s.max_occurrences})"
        )
        if s.description:
            lines.append(f"      {s.description}")
    return "\n".join(lines)



@tool
async def request_graph_mutations(
    mutations: str,
    narrative_context: str = "",
    commit: bool = True,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Requests a set of GraphMutations to be validated and applied by Hard Guardrails.
    This is the ONLY pathway for storylet effects to modify the Knowledge Graph.

    All mutations are validated against the KG before being committed:
    - Immutable nodes cannot be modified
    - Nodes must exist for add_edge, remove_node, set_attribute
    - No LLM calls — pure deterministic validation

    Args:
        mutations: JSON-encoded list of GraphMutation dicts.
        narrative_context: The narrative text this mutation batch is associated with.
        commit: If True (default), execute mutations immediately after validation.
                If False, validate and return mutation data for deferred execution
                (the graph will commit them after narrator + QA approval).
    """
    import json
    from registry import get_knowledge_graph, get_storylet_registry
    from storylet import GraphMutation
    from hard_guardrails import HardGuardrails

    vault_path = config["configurable"].get("thread_id")
    kg = get_knowledge_graph(vault_path)
    guardrails = HardGuardrails(kg)

    try:
        mut_data = json.loads(mutations) if mutations else []
    except json.JSONDecodeError as e:
        return f"SYSTEM ERROR: Invalid JSON in mutations: {e}"

    parsed_mutations = []
    for m in mut_data:
        try:
            parsed_mutations.append(GraphMutation(**m))
        except Exception as e:
            return f"SYSTEM ERROR: Invalid mutation: {e}"

    ctx = {"vault_path": vault_path}
    result = guardrails.validate_full_pipeline(narrative_context, parsed_mutations, ctx)

    if not result.allowed:
        revisions = "; ".join(result.required_revisions) if result.required_revisions else "Rewrite to avoid invalid claims."
        return (
            f"SYSTEM ERROR: Guardrail rejection. {result.reason}\n"
            f"Required revisions: {revisions}"
        )

    if not commit:
        # Deferred execution mode: return validated mutation data for the graph to commit later.
        # Include the raw mutation dicts so the graph can re-validate and execute.
        mutation_summaries = [
            {
                "mutation_type": m.mutation_type,
                "node_name": m.node_name,
                "predicate": m.predicate,
                "target_name": m.target_name,
                "attribute": m.attribute,
                "value": m.value,
                "tags": m.tags,
                "node_uuid": str(m.node_uuid) if m.node_uuid else None,
                "target_uuid": str(m.target_uuid) if m.target_uuid else None,
                "node_type": m.node_type,
            }
            for m in parsed_mutations
        ]
        return (
            f"MECHANICAL TRUTH: {len(parsed_mutations)} graph mutations validated "
            f"(deferred execution). commit=False — pending narrator approval.\n"
            f"Mutations: {json.dumps(mutation_summaries)}"
        )

    # Execute validated mutations (commit=True, the default)
    for mutation in parsed_mutations:
        mutation.execute(kg)

    # Update active storylet occurrence counter if one is active
    active_storylet_id = config["configurable"].get("active_storylet_id")
    if active_storylet_id:
        registry = get_storylet_registry(vault_path)
        try:
            storylet = registry.get(uuid.UUID(active_storylet_id))
            if storylet:
                storylet.current_occurrences += 1
        except (ValueError, TypeError):
            pass

    return f"MECHANICAL TRUTH: {len(parsed_mutations)} graph mutations committed. Knowledge Graph updated."



@tool
async def sync_knowledge_graph(
    direction: str = "to_vault",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Syncs the Knowledge Graph with the Obsidian vault.

    Args:
        direction: 'to_vault' (registry → KG) or 'from_vault' (KG → registry).
    """
    from registry import get_knowledge_graph
    import asyncio

    vault_path = config["configurable"].get("thread_id")

    if direction == "to_vault":
        from vault_io import sync_knowledge_graph_to_vault
        kg = get_knowledge_graph(vault_path)
        try:
            await sync_knowledge_graph_to_vault(vault_path, kg)
        except Exception as e:
            return f"SYSTEM ERROR: Failed to sync KG to vault: {e}"
        return "MECHANICAL TRUTH: Knowledge graph synced to vault."

    elif direction == "from_vault":
        from vault_io import sync_knowledge_graph_from_vault
        kg = get_knowledge_graph(vault_path)
        try:
            await sync_knowledge_graph_from_vault(vault_path, kg)
        except Exception as e:
            return f"SYSTEM ERROR: Failed to sync KG from vault: {e}"
        return "MECHANICAL TRUTH: Knowledge graph synced from vault."

    return "SYSTEM ERROR: direction must be 'to_vault' or 'from_vault'."



@tool
async def run_ingestion_pipeline_tool(
    npc_lore_text: str = "",
    campaign_narrative_text: str = "",
    storylet_resolutions_json: str = "{}",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Phase 2 NLP Ingestion Pipeline: parse raw DM content into structured engine artifacts.

    This tool requires an active LLM session and may take several seconds to complete.
    Results are written directly to the Knowledge Graph and Storylet Registry.

    Args:
        npc_lore_text: Raw NPC biography or lore text to parse into KG entities.
        campaign_narrative_text: Campaign narrative text to parse into Storylets.
        storylet_resolutions_json: JSON string of {{"storylet_name": "resolution_text"}} pairs
            for effect annotation (Task 3.3).
    """
    import asyncio
    import json as _json

    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    # Get LLM from config context (set by the graph builder)
    llm = config["configurable"].get("_llm")
    if llm is None:
        return (
            "SYSTEM ERROR: No LLM available in session context. "
            "The ingestion pipeline requires an active LLM session. "
            "Ensure the session was initialized with an LLM instance."
        )

    try:
        storylet_resolutions = _json.loads(storylet_resolutions_json) if storylet_resolutions_json else {}
    except Exception:
        storylet_resolutions = {}

    try:
        from ingestion_pipeline import run_ingestion_pipeline
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If already in async context, create a task
            async def _run():
                return await run_ingestion_pipeline(
                    vault_path=vault_path,
                    npc_lore_text=npc_lore_text or None,
                    campaign_narrative_text=campaign_narrative_text or None,
                    storylet_resolutions=storylet_resolutions or None,
                    llm=llm,
                )
            task = loop.create_task(_run())
            # Wait for it in a blocking way (this tool is called from sync context)
            result = loop.run_until_complete(task)
        else:
            result = loop.run_until_complete(run_ingestion_pipeline(
                vault_path=vault_path,
                npc_lore_text=npc_lore_text or None,
                campaign_narrative_text=campaign_narrative_text or None,
                storylet_resolutions=storylet_resolutions or None,
                llm=llm,
            ))
    except Exception as e:
        return f"SYSTEM ERROR: Ingestion pipeline failed: {e}"

    return (
        f"MECHANICAL TRUTH: Ingestion pipeline complete.\n"
        f"  KG nodes added: {result['nodes_added']}\n"
        f"  KG edges added: {result['edges_added']}\n"
        f"  Storylets created: {result['storylets_created']}\n"
        f"  Effects annotated: {result['effects_annotated']}"
    )



@tool
async def hydrate_campaign(
    campaign_materials_json: str = "{}",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    One-shot campaign hydration: fully populate the Knowledge Graph and Storylet
    Registry from raw campaign materials provided by the DM.

    This is an expensive upfront operation (one-time LLM cost) that enables
    fast subsequent sessions. Call this when starting a new campaign or doing
    major session prep.

    Results are written directly to the Knowledge Graph and Storylet Registry.

    Args:
        campaign_materials_json: JSON string of CampaignMaterials:
            {{
              "campaign_name": "Curse of Strahd",
              "npc_lore": "Lord Vader is an ancient Sith lord...",
              "campaign_narrative": "The party arrives at Castle Ravenloft...",
              "session_prep_notes": "The warlock will betray the party...",
              "storylet_resolutions": {{
                "The Betrayal": "Lord Vader turns on the party and joins the Cult."
              }}
            }}
            All fields are optional — the pipeline processes whatever is provided.
    """
    import asyncio
    import json as _json

    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    llm = config["configurable"].get("_llm")
    if llm is None:
        return (
            "SYSTEM ERROR: No LLM available in session context. "
            "The hydration pipeline requires an active LLM session."
        )

    try:
        materials = _json.loads(campaign_materials_json)
    except Exception:
        return "SYSTEM ERROR: Invalid campaign_materials_json. Provide a valid JSON string."

    try:
        from ingestion_pipeline import CampaignHydrationPipeline, CampaignMaterials
        loop = asyncio.get_event_loop()

        async def _run():
            cm = CampaignMaterials(
                campaign_name=materials.get("campaign_name", ""),
                npc_lore=materials.get("npc_lore", ""),
                campaign_narrative=materials.get("campaign_narrative", ""),
                session_prep_notes=materials.get("session_prep_notes", ""),
                storylet_resolutions=materials.get("storylet_resolutions", {}),
            )
            pipeline = CampaignHydrationPipeline(llm, vault_path)
            return await pipeline.run(cm)

        if loop.is_running():
            task = loop.create_task(_run())
            result = loop.run_until_complete(task)
        else:
            result = loop.run_until_complete(_run())
    except Exception as e:
        return f"SYSTEM ERROR: Campaign hydration failed: {e}"

    lines = [
        "MECHANICAL TRUTH: Campaign hydration complete.",
        f"  KG nodes created: {result.nodes_created}",
        f"  KG edges created: {result.edges_created}",
        f"  Storylets created: {result.storylets_created}",
        f"  Storylets annotated: {result.storylets_annotated}",
        f"  Effects attached: {result.effects_attached}",
        f"  Backup storylets (Three Clue Rule): {result.backup_storylets_generated}",
        f"  Three Clue violations fixed: {result.three_clue_violations_fixed}",
        f"  Vault persisted: {result.vault_persisted}",
    ]
    if result.warnings:
        lines.append("  Warnings:")
        for w in result.warnings:
            lines.append(f"    - {w}")

    return "\n".join(lines)



@tool
async def hydrate_delta(
    new_materials_json: str = "{}",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Incrementally hydrate only NEW content from DM materials — skips entities
    and storylets that already exist in the Knowledge Graph or Storylet Registry.

    Use this for session prep updates, mid-campaign additions, or when the DM
    provides new materials. This is much cheaper than a full hydration because
    it only processes genuinely new content.

    To check what entities are missing before calling this tool, use
    detect_missing_entities first.

    Args:
        new_materials_json: JSON string of CampaignMaterials with only the new content:
            {{
              "npc_lore": "A new NPC named Zypyr appears...",
              "session_prep_notes": "The party will encounter the Archmage..."
            }}
    """
    import asyncio
    import json as _json

    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    llm = config["configurable"].get("_llm")
    if llm is None:
        return (
            "SYSTEM ERROR: No LLM available in session context. "
            "The hydration pipeline requires an active LLM session."
        )

    try:
        materials = _json.loads(new_materials_json)
    except Exception:
        return "SYSTEM ERROR: Invalid new_materials_json. Provide a valid JSON string."

    try:
        from ingestion_pipeline import IncrementalHydrationPipeline, CampaignMaterials
        loop = asyncio.get_event_loop()

        async def _run():
            cm = CampaignMaterials(
                npc_lore=materials.get("npc_lore", ""),
                campaign_narrative=materials.get("campaign_narrative", ""),
                session_prep_notes=materials.get("session_prep_notes", ""),
                storylet_resolutions=materials.get("storylet_resolutions", {}),
            )
            pipeline = IncrementalHydrationPipeline(llm, vault_path)
            return await pipeline.delta_hydrate(cm)

        if loop.is_running():
            task = loop.create_task(_run())
            result = loop.run_until_complete(task)
        else:
            result = loop.run_until_complete(_run())
    except Exception as e:
        return f"SYSTEM ERROR: Delta hydration failed: {e}"

    lines = [
        "MECHANICAL TRUTH: Delta hydration complete.",
        f"  KG nodes created: {result.nodes_created}",
        f"  KG edges created: {result.edges_created}",
        f"  Storylets created: {result.storylets_created}",
        f"  Warnings: {len(result.warnings)}",
    ]
    if result.warnings:
        for w in result.warnings:
            lines.append(f"    - {w}")

    return "\n".join(lines)



@tool
async def set_storylet_deadline(
    storylet_name: str,
    deadline_turns: int,
    urgency: str = "flexible",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Set or modify a storylet's deadline and urgency tier.

    Use this as a DM-facing tool to impose time pressure on storylets that
    have become urgent during play. The LLM-DM will receive urgency signals
    in the storylet injection prompt and add appropriate time-pressure language.

    Args:
        storylet_name: Name of the storylet to update
        deadline_turns: Number of session turns remaining (set to 0 to expire immediately)
        urgency: One of flexible, approaching, urgent, critical ( UrgencyLevel values)
    """
    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    from registry import get_storylet_registry
    from storylet import UrgencyLevel

    reg = get_storylet_registry(vault_path)
    storylet = reg.get_by_name(storylet_name)
    if not storylet:
        return f"SYSTEM ERROR: Storylet '{storylet_name}' not found."

    try:
        urgency_level = UrgencyLevel(urgency.lower())
    except ValueError:
        return f"SYSTEM ERROR: Invalid urgency '{urgency}'. Must be one of: flexible, approaching, urgent, critical."

    storylet.deadline_turns = max(0, deadline_turns)
    storylet.urgency = urgency_level
    if deadline_turns <= 0:
        storylet.is_active = False

    return (
        f"MECHANICAL TRUTH: Storylet '{storylet_name}' updated.\n"
        f"  urgency: {urgency_level.value}\n"
        f"  deadline_turns: {storylet.deadline_turns}\n"
        f"  is_active: {storylet.is_active}"
    )



@tool
async def get_scene_provenance(
    pc_name: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Return all events and clues a player character has witnessed.

    Use this tool to answer DM questions like "what does Aragorn know?" or
    "which PCs witnessed the betrayal scene?" This helps the DM track scene
    provenance and ensure the right information reaches the right players.

    Args:
        pc_name: The player character's name (e.g., "Aragorn")
    """
    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    from registry import get_knowledge_graph

    kg = get_knowledge_graph(vault_path)

    pc_uuid = kg.find_node_uuid(pc_name)
    if not pc_uuid:
        return f"SYSTEM ERROR: PC '{pc_name}' not found in KG."

    node = kg.get_node(pc_uuid)
    if not node:
        return f"SYSTEM ERROR: PC node for '{pc_name}' not found."

    witnessed = node.attributes.get("witnessed", set())
    if isinstance(witnessed, list):
        witnessed = set(witnessed)

    if not witnessed:
        return f"MECHANICAL TRUTH: [[{pc_name}]] has witnessed no events yet."

    lines = [f"[[{pc_name}]] has witnessed the following events:"]
    for event_id in sorted(witnessed):
        lines.append(f"  - {event_id}")
    return "\n".join(lines)



@tool
async def propose_backstory_claim(
    pc_name: str,
    claim_text: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Submit a player-authored backstory claim for DM review.

    Players use this to add personal history to their character. Claims are
    validated against the Knowledge Graph and held in a pending queue until
    the DM approves or rejects them.

    Two-phase workflow:
      1. Player calls propose_backstory_claim → claim is validated and queued
      2. DM calls review_backstory_claims to see pending claims
      3. DM calls approve_backstory_claim to commit approved claims to KG

    Valid claims (LIKELY_VALID) can also be auto-committed if the DM enables
    auto_approve_consistent_backstories in config.

    Args:
        pc_name: Name of the PC making the claim (e.g., "Aragorn")
        claim_text: Freeform backstory claim text (e.g., "Aragorn once served King Aldric before joining the Silver Order")
    """
    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    from registry import get_knowledge_graph
    from hard_guardrails import HardGuardrails

    kg = get_knowledge_graph(vault_path)
    guardrails = HardGuardrails(kg)

    # Find the PC node
    pc_node = kg.get_node_by_name(pc_name)
    if not pc_node:
        return f"SYSTEM ERROR: PC '{pc_name}' not found in Knowledge Graph."

    # Validate the claim
    validation = guardrails.validate_backstory_consistency(pc_node, claim_text)

    pending = _get_pending_claims(vault_path)
    if pc_name not in pending:
        pending[pc_name] = []

    claim_entry = {
        "claim_text": claim_text,
        "validation": validation,
    }
    pending[pc_name].append(claim_entry)

    if not validation.claims:
        return (
            f"MECHANICAL TRUTH: No parseable claims found in '{claim_text}'. "
            "Ensure your claim mentions relationships like 'served', 'was', 'ruled', 'possessed', etc."
        )

    lines = [f"MECHANICAL TRUTH: Backstory claim submitted for DM review. Claims detected: {len(validation.claims)}"]
    for i, claim in enumerate(validation.claims):
        lines.append(f"  [{i}] {claim}")
    if validation.contradictions:
        lines.append(f"  CONFLICTS ({len(validation.contradictions)}):")
        for c in validation.contradictions:
            lines.append(f"    - {c}")
    if validation.new_entities:
        lines.append(f"  NEW ENTITIES ({len(validation.new_entities)}):")
        for e in validation.new_entities:
            lines.append(f"    - {e}")
    lines.append("  Status: PENDING DM APPROVAL")
    return "\n".join(lines)



@tool
async def review_backstory_claims(
    pc_name: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Review all pending backstory claims for a player character (DM-facing).

    Returns all pending claims with their validation status:
      - LIKELY_VALID: Consistent with existing KG facts — DM can approve as-is
      - CONFLICT: Contradicts an existing KG edge — DM must resolve
      - NEW_ENTITY: References an entity not yet in KG — DM can approve with creation

    DM actions per claim:
      - APPROVE: Commit the claim as a KG edge (with source='player_backstory')
      - REJECT: Discard the claim
      - MODIFY: Edit the claim text and re-validate

    Args:
        pc_name: Name of the PC whose claims to review
    """
    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    pending = _get_pending_claims(vault_path)
    claims = pending.get(pc_name, [])

    if not claims:
        return f"MECHANICAL TRUTH: No pending backstory claims for '{pc_name}'."

    lines = [f"MECHANICAL TRUTH: Pending backstory claims for '{pc_name}' ({len(claims)}):\n"]
    for i, entry in enumerate(claims):
        validation = entry["validation"]
        lines.append(f"--- Claim [{i}] ---")
        lines.append(f"  Text: {entry['claim_text']}")
        for j, claim in enumerate(validation.claims):
            lines.append(f"  Triple {j}: {claim}")
        if validation.contradictions:
            lines.append(f"  CONFLICTS:")
            for c in validation.contradictions:
                lines.append(f"    - {c}")
        if validation.new_entities:
            lines.append(f"  NEW ENTITIES (not in KG):")
            for e in validation.new_entities:
                lines.append(f"    - {e}")
        lines.append("")
    return "\n".join(lines)



@tool
async def approve_backstory_claim(
    pc_name: str,
    claim_index: int,
    action: str = "approve",
    modified_text: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Approve, reject, or modify a pending backstory claim (DM-facing).

    APPROVE: Creates a KG edge with metadata:
      - source: 'player_backstory'
      - player_name: pc_name
      - approved_by_dm: True
      - original_claim: the claim text

    REJECT: Removes the claim from the pending queue without effect.

    MODIFY: Re-validates the modified_text against KG and replaces the claim.
    Use this when a claim has a CONFLICT but the DM wants to rephrase it.

    Args:
        pc_name: Name of the PC
        claim_index: Index from review_backstory_claims listing (0-based)
        action: 'approve', 'reject', or 'modify'
        modified_text: Required when action='modify' — the revised claim text
    """
    import json as _json

    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    pending = _get_pending_claims(vault_path)
    claims = pending.get(pc_name, [])

    if not claims:
        return f"SYSTEM ERROR: No pending claims for '{pc_name}'."
    if claim_index < 0 or claim_index >= len(claims):
        return f"SYSTEM ERROR: Invalid claim_index {claim_index}. Range: 0-{len(claims) - 1}."

    entry = claims[claim_index]
    original_text = entry["claim_text"]
    validation = entry["validation"]

    if action.lower() == "reject":
        claims.pop(claim_index)
        return f"MECHANICAL TRUTH: Claim rejected and removed from queue."

    if action.lower() == "modify":
        if not modified_text:
            return "SYSTEM ERROR: modified_text required when action='modify'."
        from registry import get_knowledge_graph
        from hard_guardrails import HardGuardrails

        kg = get_knowledge_graph(vault_path)
        guardrails = HardGuardrails(kg)
        pc_node = kg.get_node_by_name(pc_name)
        if not pc_node:
            return f"SYSTEM ERROR: PC '{pc_name}' not found in KG."

        new_validation = guardrails.validate_backstory_consistency(pc_node, modified_text)
        entry["claim_text"] = modified_text
        entry["validation"] = new_validation
        return (
            f"MECHANICAL TRUTH: Claim modified and re-validated.\n"
            f"  New text: {modified_text}\n"
            f"  New claims: {new_validation.claims}\n"
            f"  Conflicts: {new_validation.contradictions or 'None'}\n"
            f"  New entities: {new_validation.new_entities or 'None'}"
        )

    if action.lower() == "approve":
        # Commit each validated claim triple as a KG edge
        from registry import get_knowledge_graph
        from knowledge_graph import GraphPredicate
        import uuid

        kg = get_knowledge_graph(vault_path)
        pc_node = kg.get_node_by_name(pc_name)
        if not pc_node:
            return f"SYSTEM ERROR: PC '{pc_name}' not found in KG."

        # Map assertion verbs to GraphPredicates
        verb_to_pred = {
            "is": GraphPredicate.ALLIED_WITH,
            "was": GraphPredicate.ALLIED_WITH,
            "served": GraphPredicate.SERVES,
            "ruled": GraphPredicate.ALLIED_WITH,
            "led": GraphPredicate.ALLIED_WITH,
            "possessed": GraphPredicate.POSSESSES,
            "owned": GraphPredicate.POSSESSES,
            "betrayed": GraphPredicate.HOSTILE_TOWARD,
            "hated": GraphPredicate.HOSTILE_TOWARD,
            "loved": GraphPredicate.ALLIED_WITH,
            "feared": GraphPredicate.HOSTILE_TOWARD,
            "allied with": GraphPredicate.ALLIED_WITH,
            "joined": GraphPredicate.MEMBER_OF,
            "member of": GraphPredicate.MEMBER_OF,
            "led by": GraphPredicate.ALLIED_WITH,
        }

        results = []
        for claim in validation.claims:
            subj, verb, obj = claim.subject, claim.verb, claim.object

            # Determine which node is the player and which is the target
            if subj.lower() == pc_name.lower():
                actor_node = pc_node
                target_name = obj
            elif obj.lower() == pc_name.lower():
                actor_node = pc_node
                target_name = subj
            else:
                # Neither matches PC name — use PC as the actor (player's claim about self)
                actor_node = pc_node
                target_name = obj

            target_uuid = kg.find_node_uuid(target_name)

            # Map verb to predicate
            pred = verb_to_pred.get(verb.lower())
            if pred is None:
                # Fallback: treat as a generic relationship
                pred = GraphPredicate.ALLIED_WITH

            # Create edge attributes
            edge_attrs = {
                "source": "player_backstory",
                "player_name": pc_name,
                "approved_by_dm": True,
                "original_claim": original_text,
            }

            # If target doesn't exist, try to create it as an NPC
            if target_uuid is None and target_name:
                target_uuid_obj = uuid.uuid4()
                new_node = kg.add_node(
                    node_uuid=target_uuid_obj,
                    node_name=target_name,
                    node_type="npc",
                    attributes={"description": f"Created from player backstory claim by {pc_name}"},
                )
                target_uuid = new_node.uuid
                edge_attrs["newly_created"] = True

            if target_uuid:
                kg.add_edge(
                    subject_uuid=actor_node.uuid,
                    predicate=pred,
                    object_uuid=target_uuid,
                    attributes=edge_attrs,
                )
                results.append(f"  + [[{pc_name}]] --{pred.value}--> [[{target_name}]]")
            else:
                results.append(f"  ! Could not resolve target: {target_name}")

        # Remove approved claim from pending queue
        claims.pop(claim_index)

        if results:
            return (
                f"MECHANICAL TRUTH: Backstory claim APPROVED and committed to KG.\n"
                + "\n".join(results)
            )
        else:
            return "MECHANICAL TRUTH: Claim approved but no KG edges could be created."

    return f"SYSTEM ERROR: Unknown action '{action}'. Use 'approve', 'reject', or 'modify'."



@tool
async def reveal_secret(
    subject_name: str,
    predicate: str,
    object_name: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Reveal a secret edge in the Knowledge Graph — sets edge.secret=False.

    When a secret is discovered by the party (e.g., Lord Vance's membership in
    the Cult is revealed), call this tool to flip the edge from secret to public.
    After this call, the edge will appear in GraphRAG context for the narrator.

    This emits a 'set_edge_attribute' mutation: sets secret=False on the matching edge.
    The mutation is validated and committed through the standard deferred-mutation flow
    (Hard Guardrails validate, then commit_node executes after QA approval).

    Args:
        subject_name: The subject node name (e.g., "Lord Vance")
        predicate: The edge predicate (e.g., "member_of", "serves")
        object_name: The object node name (e.g., "The Cult")
    """
    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    from registry import get_knowledge_graph
    from knowledge_graph import GraphPredicate

    kg = get_knowledge_graph(vault_path)

    # Find the edge
    subj_uuid = kg.find_node_uuid(subject_name)
    obj_uuid = kg.find_node_uuid(object_name)
    if not subj_uuid or not obj_uuid:
        return f"SYSTEM ERROR: Could not find node(s): {subject_name} or {object_name}"

    try:
        pred = GraphPredicate(predicate)
    except ValueError:
        return f"SYSTEM ERROR: Unknown predicate '{predicate}'"

    # Find and check the edge
    target_edge = None
    for edge in kg.edges:
        if edge.subject_uuid == subj_uuid and edge.object_uuid == obj_uuid and edge.predicate == pred:
            target_edge = edge
            break

    if target_edge is None:
        return f"SYSTEM ERROR: Edge not found: [[{subject_name}]] --{predicate}--> [[{object_name}]]"

    if not target_edge.secret:
        return f"MECHANICAL TRUTH: Edge [[{subject_name}]] --{predicate}--> [[{object_name}]] is already public. No secret to reveal."

    # Build the mutation dict — action_logic_node will capture and defer this
    mutation_dict = {
        "mutation_type": "set_edge_attribute",
        "node_name": subject_name,
        "predicate": predicate,
        "target_name": object_name,
        "attribute": "secret",
        "value": False,
    }
    import json

    return (
        f"MECHANICAL TRUTH: Secret-reveal mutation for [[{subject_name}]] --{predicate}--> [[{object_name}]]. "
        f"Mutation: {json.dumps(mutation_dict)}"
    )


# ---------------------------------------------------------------------
# In-memory store for pending backstory claims (Feature #10)
# Keyed by vault_path; each value is a dict keyed by pc_name
# ---------------------------------------------------------------------
_PENDING_BACKSTORY_CLAIMS: Dict[str, Dict[str, list]] = {}
_PENDING_CLAIMS_LOCK = threading.RLock()


def _get_pending_claims(vault_path: str) -> Dict[str, list]:
    with _PENDING_CLAIMS_LOCK:
        if vault_path not in _PENDING_BACKSTORY_CLAIMS:
            _PENDING_BACKSTORY_CLAIMS[vault_path] = {}
        return _PENDING_BACKSTORY_CLAIMS[vault_path]



@tool
async def detect_missing_entities(
    narrative_text: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Scan narrative text for entity references (Wikilinks or capitalized names)
    that are NOT in the Knowledge Graph.

    Returns a list of missing entity names that can be hydrated by calling
    hydrate_delta or hydrate_missing_entity.

    This tool is useful:
      - Before a session: proactively identify gaps in the KG
      - After a guardrail rejection: the DM can call this to understand what
        entities the narrator referenced that aren't in the KG

    Args:
        narrative_text: The narrative prose or DM notes to scan.
    """
    import json as _json

    vault_path = config["configurable"].get("thread_id")
    if not vault_path:
        return "SYSTEM ERROR: No vault context found."

    try:
        from ingestion_pipeline import IncrementalHydrationPipeline
        pipeline = IncrementalHydrationPipeline(llm=None, vault_path=vault_path)
        missing = pipeline.detect_missing_entities(narrative_text)
    except Exception as e:
        return f"SYSTEM ERROR: Entity scan failed: {e}"

    if not missing:
        return (
            "MECHANICAL TRUTH: All referenced entities exist in the Knowledge Graph. "
            "No missing entities detected."
        )

    suggestion = pipeline.suggest_hydration(missing)
    return f"MECHANICAL TRUTH: Missing entities detected ({len(missing)}):\n" + "\n".join(f"  - {n}" for n in missing) + "\n\n" + suggestion



__all__ = [
    "create_storylet",
    "list_active_storylets",
    "request_graph_mutations",
    "sync_knowledge_graph",
    "run_ingestion_pipeline_tool",
    "hydrate_campaign",
    "hydrate_delta",
    "set_storylet_deadline",
    "get_scene_provenance",
    "propose_backstory_claim",
    "review_backstory_claims",
    "approve_backstory_claim",
    "reveal_secret",
    "detect_missing_entities",
]
