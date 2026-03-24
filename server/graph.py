"""LangGraph multi-agent graph: Planner → Action → DramaManager → Narrator → QA.

Nodes are defined as closures inside build_graph() so they capture the LLM
instances and tools list without relying on module-level globals.

Storylet Orchestration integration:
- DramaManager pre-hook: If active_storylet is in state, injects its content into planner context
- action → drama_manager: After each tool execution, drama manager updates tension arc and selects next storylet
- drama_manager → narrator: If storylet selected, skip planner and go directly to narrator
- Hard Guardrails: Narrator output validated against KG before reaching QA
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from state import DMState, QAResult, TensionArc
from vault_io import write_audit_log
from system_logger import qa_logger
from tools import _get_config_tone
from mutation_manager import MutationManager

MAX_QA_REVISIONS = 3

# Mutation manager — extracted from pending_mutations flow across graph nodes
_mutation_manager = MutationManager()

# ---------------------------------------------------------------------
# TTL + LRU Cache: GraphRAG context (invalidated on KG writes)
# ---------------------------------------------------------------------
_GRAG_CACHE_TTL_SECONDS = 60.0
_GRAG_CACHE_MAX_SIZE = 32
# { (vault_path, active_character): (timestamp, result_str) }
_grag_cache: Dict[Tuple[str, str], Tuple[float, str]] = {}
_grag_cache_lock = threading.RLock()

# KG immutable constraints are vault-wide (don't depend on active_character)
# { vault_path: (timestamp, constraints_str, kg_id) }  — kg_id ensures different KG instances don't collide
_kg_constraints_cache: Dict[str, Tuple[float, str, int]] = {}
_kg_constraints_cache_lock = threading.RLock()
_KG_CONSTRAINTS_TTL_SECONDS = 120.0  # Longer TTL — KG changes are rare


def extract_npc_dials(biography_text: str) -> Dict[str, float]:
    """
    Extract NPC behavioral dials from biography text.

    Gap 9: Returns a dict of dial_name -> value (0.0 to 1.0).
    e.g. {"greed": 0.8, "loyalty": 0.9, "courage": 0.3, "cruelty": 0.7}

    This is a deterministic keyword heuristic. The LLM-powered version
    (Phase 2 ingestion pipeline) will use an LLM to extract these from
    raw NPC biography text, but this function provides the same interface
    for manual attribute entry.

    Supported dials:
    - greed, loyalty, courage, cruelty, cunning, piety, honor, patience
    """
    if not biography_text:
        return {}

    text_lower = biography_text.lower()
    dials: Dict[str, float] = {}

    # Keyword maps: positive indicators → high value, negative → low value
    POSITIVE_GREED = ["wealthy", "greedy", "greed", "miserly", "coin", "gold", "riches", "brib", "pay", "price"]
    NEGATIVE_GREED = ["generous", "charitable", "gives freely", "selfless"]

    POSITIVE_LOYALTY = ["loyal", "devoted", "faithful", "oath", "sworn", "pledged", "faithful servant"]
    NEGATIVE_LOYALTY = ["traitor", "betrayer", "disloyal", "renegade", "treacherous"]

    POSITIVE_COURAGE = ["brave", "courageous", "bold", "fearless", "valiant", "heroic", "stalwart"]
    NEGATIVE_COURAGE = ["coward", "timid", "fearful", "craven", "frightened"]

    POSITIVE_CRUELTY = ["cruel", "sadistic", "brutal", "merciless", "ruthless", "pitiless"]
    NEGATIVE_CRUELTY = ["merciful", "compassionate", "kind", "humane", "gentle"]

    POSITIVE_CUNNING = ["clever", "cunning", "shrewd", "wise", "astute", "deceptive", "sly"]
    NEGATIVE_CUNNING = ["naive", "simple", "honest", "straightforward", "guileless"]

    POSITIVE_PIETY = ["devout", "religious", "pious", "faithful to gods", "blessed", "holy"]
    NEGATIVE_PIETY = ["atheist", "godless", "blasphemous", "profane"]

    def score(positive_kw: list, negative_kw: list) -> float:
        pos_count = sum(1 for kw in positive_kw if kw in text_lower)
        neg_count = sum(1 for kw in negative_kw if kw in text_lower)
        total = pos_count + neg_count
        if total == 0:
            return 0.5  # Neutral baseline
        return pos_count / total

    dials["greed"] = score(POSITIVE_GREED, NEGATIVE_GREED)
    dials["loyalty"] = score(POSITIVE_LOYALTY, NEGATIVE_LOYALTY)
    dials["courage"] = score(POSITIVE_COURAGE, NEGATIVE_COURAGE)
    dials["cruelty"] = score(POSITIVE_CRUELTY, NEGATIVE_CRUELTY)
    dials["cunning"] = score(POSITIVE_CUNNING, NEGATIVE_CUNNING)
    dials["piety"] = score(POSITIVE_PIETY, NEGATIVE_PIETY)

    # Remove near-neutral dials (0.45 to 0.55)
    return {k: v for k, v in dials.items() if abs(v - 0.5) > 0.05}


def _get_grag_context(kg, vault_path: str = None, active_character: str = None, max_hops: int = 2) -> str:
    """
    GraphRAG context injection (Gap 8).

    Returns a formatted string of KG facts relevant to the active scene:
    - Active character location + nearby entities
    - NPCs and their relationship edges to active character
    - Active quests and their connected nodes

    Results are cached for 60 seconds per (vault_path, active_character) key.
    Cache is invalidated by _invalidate_grag_cache() after KG writes.
    Cache is bounded to _GRAG_CACHE_MAX_SIZE entries with LRU eviction.

    This is prepended to the narrator's system prompt to give the LLM
    dynamic world-state grounding rather than static rules.
    """
    # Opt E: TTL + LRU cache with max size
    if vault_path and active_character:
        cache_key = (vault_path, active_character)
        with _grag_cache_lock:
            now = time.monotonic()
            if cache_key in _grag_cache:
                ts, cached = _grag_cache[cache_key]
                if now - ts < _GRAG_CACHE_TTL_SECONDS:
                    # LRU: promote to fresh entry on hit
                    _grag_cache[cache_key] = (now, cached)
                    return cached

            # Evict oldest entry if at capacity
            if len(_grag_cache) >= _GRAG_CACHE_MAX_SIZE:
                oldest_key = min(_grag_cache, key=lambda k: _grag_cache[k][0])
                # TOCTOU guard: re-check after min() — key may have been removed by
                # _invalidate_grag_cache() on another thread since we acquired the lock
                if oldest_key in _grag_cache:
                    del _grag_cache[oldest_key]

            # Compute and cache
            result = _compute_grag_context(kg, vault_path, active_character, max_hops)
            _grag_cache[cache_key] = (now, result)
            return result

    return _compute_grag_context(kg, vault_path, active_character, max_hops)


def _compute_grag_context(kg, vault_path: str, active_character: str, max_hops: int) -> str:
    """
    Internal: computes GraphRAG context without caching.
    Extracted so the cache logic and TTL are centralized here.
    """
    from knowledge_graph import GraphNodeType, GraphPredicate

    if not kg or not kg.nodes:
        return ""

    lines = [
        "\n\n=== CAMPAIGN WORLD STATE (Dynamic KG Context) ===",
        "The following facts are established in this session. Use them to ground your narrative:",
    ]

    # Active location and nearby entities
    if active_character:
        char_uuid = kg.find_node_uuid(active_character)
        if char_uuid:
            ctx = kg.get_context_for_node(char_uuid, max_hops=max_hops, hide_secrets=True)
            if ctx:
                lines.append(f"\n[Active Character: {ctx.get('name', active_character)}]")
                neighbors = ctx.get("neighbors", {})
                if neighbors:
                    for pred, names in neighbors.items():
                        if pred.startswith("reverse_"):
                            lines.append(f"  ← {names} [{pred.replace('reverse_', '')}]")
                        else:
                            lines.append(f"  {pred.replace('_', ' ')}: {', '.join(names)}")

    # All NPC nodes with their edges
    # Opt A: skip the active character if they're in the NPC list (already rendered above)
    char_uuid = kg.find_node_uuid(active_character) if active_character else None
    npc_nodes = kg.query_nodes(node_type=GraphNodeType.NPC)
    for npc in npc_nodes[:5]:  # Limit to 5 NPCs to avoid prompt bloat
        # Deduplicate: if active character is an NPC, skip them (already rendered)
        if char_uuid and npc.node_uuid == char_uuid:
            continue
        ctx = kg.get_context_for_node(npc.node_uuid, max_hops=1, hide_secrets=True)
        if ctx:
            attrs = ctx.get("attributes", {})
            neighbors = ctx.get("neighbors", {})
            lines.append(f"\n[NPC: {npc.name}]")
            # NPC disposition toward party
            disp = attrs.get("disposition_toward_party", 50)
            if disp < 30:
                disp_label = "HOSTILE"
            elif disp > 70:
                disp_label = "FRIENDLY"
            else:
                disp_label = "NEUTRAL"
            lines.append(f"  disposition: {disp}/100 ({disp_label})")
            # Gap 9: Inject NPC behavioral dials
            if npc.npc_dials:
                dial_str = ", ".join(f"{k}={v:.1f}" for k, v in npc.npc_dials.items())
                lines.append(f"  Behavioral Dials: {dial_str}")
            if attrs:
                notable = {k: v for k, v in attrs.items()
                           if k not in ("description", "bio", "notes", "disposition_toward_party") and v}
                if notable:
                    for k, v in list(notable.items())[:3]:
                        lines.append(f"  {k}: {v}")
            if neighbors:
                edge_summary = []
                for pred, names in neighbors.items():
                    if not pred.startswith("reverse_"):
                        edge_summary.append(f"{pred.replace('_', ' ')}: {', '.join(names[:2])}")
                if edge_summary:
                    lines.append(f"  Relationships: {'; '.join(edge_summary[:3])}")

    # Faction nodes with their standing toward the party
    faction_nodes = kg.query_nodes(node_type=GraphNodeType.FACTION)
    for faction in faction_nodes[:3]:  # Limit to 3 factions
        standing_map = faction.attributes.get("faction_standing", {})
        party_key = active_character or "party"
        standing = standing_map.get(party_key, standing_map.get("party", 50))
        if standing < 30:
            standing_label = "HOSTILE"
        elif standing > 70:
            standing_label = "ALLIED"
        else:
            standing_label = "NEUTRAL"
        lines.append(f"\n[FACTION: {faction.name}]")
        lines.append(f"  standing: {standing}/100 ({standing_label} toward party)")
        # Controls (edges with CONTROLS predicate)
        controls_edges = kg.query_edges(
            subject_uuid=faction.node_uuid,
            predicate=GraphPredicate.CONTROLS,
        )
        if controls_edges:
            ctrl_names = []
            for edge in controls_edges[:3]:
                node = kg.get_node(edge.object_uuid)
                if node:
                    ctrl_names.append(f"[[{node.name}]]")
            if ctrl_names:
                lines.append(f"  Controls: {', '.join(ctrl_names)}")

    # PC (PLAYER) nodes — show what they've witnessed (scene provenance)
    pc_nodes = kg.query_nodes(node_type=GraphNodeType.PLAYER)
    for pc in pc_nodes[:5]:  # Limit to 5 PCs
        witnessed = pc.attributes.get("witnessed", set())
        if isinstance(witnessed, list):
            witnessed = set(witnessed)
        lines.append(f"\n[PC: {pc.name}]")
        if witnessed:
            lines.append(f"  witnessed: {', '.join(sorted(witnessed))}")
        else:
            lines.append(f"  witnessed: (none)")

    lines.append("\n(Only describe entities and relationships that are established in the KG above.)\n")
    return "\n".join(lines)


def _invalidate_grag_cache(vault_path: str = None) -> None:
    """
    Invalidate the GraphRAG context cache and KG constraints cache.

    Called after KG writes (mutations committed) so that the next
    narrator_node invocation re-fetches fresh KG context instead of
    returning stale cached data.
    """
    with _grag_cache_lock:
        if vault_path:
            # Invalidate only entries for this vault
            keys_to_delete = [k for k in _grag_cache if k[0] == vault_path]
            for k in keys_to_delete:
                del _grag_cache[k]
        else:
            _grag_cache.clear()
    with _kg_constraints_cache_lock:
        if vault_path:
            _kg_constraints_cache.pop(vault_path, None)
        else:
            _kg_constraints_cache.clear()


def _build_kg_constraints_prompt(kg, vault_path: str = "default") -> str:
    """
    Query the Knowledge Graph for immutable nodes and format them as
    forbidden claims in the narrator's system prompt.

    Gap 2 fix: Inject KG state constraints BEFORE the LLM generates prose,
    making the fishbowl a pre-constraint rather than a post-filter.

    Cached per vault (120s TTL) — invalidated by _invalidate_grag_cache when KG is mutated.
    """
    # Check cache — use hex(id(kg)) so different KG instances don't collide
    # hex() gives stable string representation vs raw int id which can be reused
    now = time.monotonic()
    kg_id = hex(id(kg))
    with _kg_constraints_cache_lock:
        if vault_path in _kg_constraints_cache:
            ts, cached, cached_kg_id = _kg_constraints_cache[vault_path]
            if kg_id == cached_kg_id and now - ts < _KG_CONSTRAINTS_TTL_SECONDS:
                return cached

    if not kg or not kg.nodes:
        return ""

    lines = [
        "\n\n=== KNOWLEDGE GRAPH CONSTRAINTS (Immutable World Facts) ===",
        "The following facts are TRUE in this world. Your narrative MUST NOT contradict them:",
    ]

    for node in kg.nodes.values():
        if not node.is_immutable:
            continue

        # Describe the node's key facts
        facts = [f"[[{node.name}]]"]
        if node.node_type.value:
            facts.append(f"is a {node.node_type.value}")
        if node.attributes:
            for k, v in node.attributes.items():
                if k not in ("description", "bio", "notes"):
                    facts.append(f"has {k}={v}")

        # Add outgoing edges
        for predicate, obj_uuids in kg.adjacency.get(node.node_uuid, {}).items():
            for obj_uuid in obj_uuids:
                obj_node = kg.get_node(obj_uuid)
                if obj_node:
                    facts.append(f"{predicate.value.replace('_', ' ')} [[{obj_node.name}]]")

        lines.append("- " + ". ".join(facts) + ".")

    if len(lines) == 2:
        return ""  # No immutable nodes

    lines.append("(These facts are immutable. Do not describe them as changed, destroyed, or transferred.)\n")
    result = "\n".join(lines)
    with _kg_constraints_cache_lock:
        _kg_constraints_cache[vault_path] = (now, result, kg_id)
    return result

# Names of tools that require Hard Guardrail validation before committing mutations.
# These are routed through action_logic (separate ToolNode) to enforce privilege segregation:
# the Creative LLM (planner/narrator) can PROPOSE mutations only via these tools,
# but must route through Hard Guardrails in action_logic to commit them.
LOGIC_TOOL_NAMES = frozenset({
    "request_graph_mutations",
    "mark_entity_immutable",
    "create_storylet",
})


def _get_tool_name_from_message(msg) -> str | None:
    """Extract the tool name from an AIMessage's tool_calls, or None if no tool was called."""
    if not hasattr(msg, "tool_calls") or not msg.tool_calls:
        return None
    for tc in msg.tool_calls:
        if isinstance(tc, dict):
            name = tc.get("name")
        else:
            name = getattr(tc, "name", None)
        if name:
            return name
    return None


def build_graph(draft_llm, qa_llm, master_tools_list, checkpointer=None):
    """Compile and return the DM Engine LangGraph.

    Args:
        draft_llm: LLM instance used by the Planner and Narrator nodes.
        qa_llm: LLM instance used by the QA node (lower temperature).
        master_tools_list: Complete list of LangChain tools available to the graph.
        checkpointer: LangGraph checkpointer (SqliteSaver or MemorySaver).
    """
    if checkpointer is None:
        checkpointer = MemorySaver()

    # ------------------------------------------------------------------
    # NODE: Planner — translates player intent into tool calls
    # ------------------------------------------------------------------
    async def planner_node(state: DMState, config: RunnableConfig):
        # STORYLET INJECTION: If a storylet is active, inject its content into planner context
        storylet_injection = ""
        if state.get("active_storylet_id"):
            from registry import get_storylet_registry, get_knowledge_graph
            vault_path = state.get("vault_path", "default")
            reg = get_storylet_registry(vault_path)
            storylet = reg.get(state["active_storylet_id"])
            if storylet:
                from drama_manager import DramaManager
                kg = get_knowledge_graph(vault_path)
                dm = DramaManager(reg, kg)
                ctx = {"vault_path": vault_path, "active_character": state.get("active_character", "Unknown")}
                storylet_injection = "\n\n" + dm.storylet_injection_prompt(storylet, ctx)

        sys_msg = SystemMessage(
            content=(
                "You are the D&D Tactical Planner. You are invisible to the player.\n"
                "Your ONLY job is to translate the player's intent into Tool Calls.\n\n"
                "TOOL ROUTING GUIDE:\n"
                "1. MELEE WEAPONS: Always use `execute_melee_attack`. Never roll manually.\n"
                "2. SPELLS & CLASS FEATURES: Always use `use_ability_or_spell`. Never roll manually. If the player "
                "specifies an Area of Effect (AoE) with coordinates, pass `target_x`, `target_y`, `aoe_shape`, and "
                "`aoe_size` to let the engine automatically resolve line-of-sight, calculate exact hits, and damage walls.\n"
                "3. SKILL CHECKS & SAVES: Use `perform_ability_check_or_save` for jumping, sneaking, perception, etc.\n"
                "4. DAMAGE/HEALING: Use `modify_health` for guaranteed/direct damage or healing (e.g., falling, potions).\n"
                "5. TRAPS & HAZARDS: Always use `trigger_environmental_hazard` for AoE effects, traps, or weather that "
                "require saving throws or attack rolls.\n"
                "6. MAP & GEOMETRY: Use `manage_map_geometry` when players interact with physical obstacles.\n"
                "7. OBJECT INTERACTION: Use `interact_with_object` to natively resolve lockpicking or disarming traps.\n"
                "8. TRAPPING GEOMETRY: Use `manage_map_trap` to attach a trap to an existing door, wall, or terrain.\n"
                "9. SKILL CHALLENGES: Use `manage_skill_challenge` to track multi-stage progress clocks.\n"
                "10. RANDOM LOOT: Use `generate_random_loot` ONLY when improvising homebrew encounters.\n"
                "11. MAP INGESTION: Use `ingest_battlemap_json` to bulk-load a complete battlemap JSON.\n"
                "12. EXTREME WEATHER: Use `evaluate_extreme_weather` for resolving exposure to "
                "extreme heat (>= 100F) or cold (<= 0F).\n"
                "13. ENVIRONMENT: You can cast spells or cause effects that alter the environment. Use `manage_map_terrain`.\n"
                "14. SUMMONS: Use `spawn_summon` to spawn creatures or familiars. "
                "Use `use_ability_or_spell` with `proxy_caster_name` for familiar touch spells.\n\n"
                "15. RULE DISPUTES: If the player challenges or disputes a mechanical ruling, immediately use `report_rule_challenge` to route it to the offline QA log.\n\n"
                "MOVEMENT PARADIGMS:\n"
                "- TRAVEL / TOWN (Out of Combat): Use `move_entity(movement_type='travel')`.\n"
                "- DUNGEON CRAWL (Out of Combat): Use `move_entity(movement_type='walk')`.\n"
                "- COMBAT: Use `move_entity(movement_type='walk')`. Strict 5ft grid speeds and Opportunity Attacks apply.\n\n"
                "If you get a CACHE MISS on a spell or ability, use `query_rulebook` to find the rules, \n"
                "then `encode_new_compendium_entry` to permanently save it to the engine.\n\n"
                'Once all tool logic is complete and you have the "MECHANICAL TRUTH", \n'
                "output a brief summary of the events. DO NOT write dialogue or narrative prose.\n"
                + storylet_injection
            )
        )
        llm_with_tools = draft_llm.bind_tools(master_tools_list)
        response = await llm_with_tools.ainvoke([sys_msg] + state["messages"], config=config)
        return {"messages": [response]}

    # ------------------------------------------------------------------
    # NODE: Narrator — turns mechanical truth into vivid prose
    # ------------------------------------------------------------------
    async def narrator_node(state: DMState, config: RunnableConfig):
        from registry import get_knowledge_graph

        vault_path = state.get("vault_path", "default")
        kg = get_knowledge_graph(vault_path)

        # Gap 2 fix: Pre-inject KG immutable facts as forbidden claims BEFORE LLM invocation.
        # This makes the fishbowl a pre-constraint rather than a post-filter.
        kg_constraints = _build_kg_constraints_prompt(kg, vault_path)

        # Gap 8 fix: Pre-inject GraphRAG context (NPCs, locations, relationships)
        # before LLM invocation so narrator has dynamic world-state grounding.
        grag_context = _get_grag_context(kg, vault_path=vault_path, active_character=state.get("active_character"))

        feedback_context = ""
        if state.get("qa_feedback") and state["qa_feedback"] != "APPROVED":
            feedback_context = (
                f"\n\n[QA REJECTION FEEDBACK]: Your previous draft was rejected for the following reason:\n"
                f"{state['qa_feedback']}\n\nYour rejected draft was:\n\"{state.get('draft_response')}\"\n\n"
                f"Fix this in your new draft."
            )

        sys_msg = SystemMessage(
            content=(
                "You are the Dungeon Master. Read the history of the current interaction.\n"
                "Look at the 'MECHANICAL TRUTH' outputs generated by the system tools.\n"
                "Narrate these exact events vividly to the player. \n"
                "DO NOT change the numbers, damage, or hit/miss outcomes. Do not roll dice.\n"
                "Output only the narrative response.\n"
                f"Do not violate player agency or do anything more than add color to an action dialogue. {feedback_context}"
                + kg_constraints
                + grag_context + "\n\n"
                "CRITICAL MULTIPLAYER RULE (PERSPECTIVE): \n"
                "If characters are in different rooms, or if perception mechanics mean characters observe entirely "
                "different things, you MUST divide your narrative using HTML tags.\n\n"
                "Wrap narrative meant for EVERYONE in:\n"
                '<div class="perspective" data-target="ALL">...</div>\n\n'
                "Wrap secret or distinct observations meant for a SPECIFIC character in:\n"
                '<div class="perspective" data-target="CharacterName">...</div>\n\n'
                'Always default to "ALL" unless a split perspective is mechanically required by the engine\'s truths.\n'
            )
        )

        response = await draft_llm.ainvoke([sys_msg] + state["messages"], config=config)
        draft = response.content

        # HARD GUARDRAILS: Validate draft against Knowledge Graph before QA
        ctx = {"vault_path": vault_path, "active_character": state.get("active_character", "Unknown")}
        guardrails, guard_result = _mutation_manager.validate(state, draft, kg, ctx)

        if not guard_result.allowed:
            await write_audit_log(
                vault_path,
                "HardGuardrails",
                "Narrative Rejected",
                f"Reason: {guard_result.reason}",
            )
            revisions_list = (
                "; ".join(f"{i+1}. {r}" for i, r in enumerate(guard_result.required_revisions))
                if guard_result.required_revisions
                else "Rewrite the narrative to address the violations above."
            )
            _mutation_manager.clear(state)
            return {
                "qa_feedback": (
                    f"[HARD GUARDRAIL REJECTED]: {guard_result.reason}\n\n"
                    f"Required revisions:\n{revisions_list}\n\n"
                    f"Your draft: {draft}"
                ),
                "revision_count": state.get("revision_count", 0) + 1,
            }

        # Snapshot KG state before QA — if QA rejects, we restore from this snapshot.
        _mutation_manager.snapshot(state, kg)

        # Preserve qa_feedback="COMMIT" if already set (commit_node will execute mutations).
        return_dict: Dict[str, Any] = {
            "draft_response": draft,
        }
        if state.get("qa_feedback") == "COMMIT":
            return_dict["qa_feedback"] = "COMMIT"
        return return_dict

    # ------------------------------------------------------------------
    # NODE: QA — validates the draft against the 13-point checklist + mutation cross-check
    # ------------------------------------------------------------------
    async def qa_node(state: DMState, config: RunnableConfig):
        from registry import get_knowledge_graph

        vault, draft, revisions = state["vault_path"], state["draft_response"], state.get("revision_count", 0)

        # ESCAPE CLAUSE 1: OOC messages bypass audit
        if draft.strip().startswith("[OOC") or draft.strip().startswith("OOC:"):
            await write_audit_log(vault, "QA Agent", "Bypass", "OOC Clarification detected. Auto-approving.")
            return {"qa_feedback": "APPROVED", "messages": [AIMessage(content=draft)]}

        # ESCAPE CLAUSE 2: qa_feedback already set to COMMIT — skip QA and route to commit.
        # This handles the case where the graph is seeded with COMMIT in the initial state
        # (e.g., from a prior approved turn). The narrator_node has already merged its
        # output which may not include qa_feedback, so we detect COMMIT here and return
        # early to allow qa_router to route to commit_node.
        if state.get("qa_feedback") == "COMMIT":
            return {"qa_feedback": "COMMIT", "messages": [AIMessage(content=draft)]}

        # ESCAPE CLAUSE 3: Max revisions reached — force approve via commit_node
        if revisions >= MAX_QA_REVISIONS:
            await write_audit_log(vault, "QA Agent", "Force Approve", "Max revisions reached. Routing to commit_node.")
            return {"qa_feedback": "COMMIT", "messages": [AIMessage(content=draft)]}

        # ------------------------------------------------------------------
        # Gap 6 (table) fix: Deterministic cross-check #1 — SVO claim validation
        # This is an independent backstop to the SVO check already done in narrator_node.
        # Run it again here so QA rejects prose that implies world-state changes
        # without mutations (in case narrator's guard check was bypassed or missed).
        # ------------------------------------------------------------------
        kg = get_knowledge_graph(vault)
        mutation_errors = list(state.get("mutation_errors", []))

        # Cross-check 1: If there are pending_mutations (shouldn't happen but guard),
        # or if the draft implies world changes without mutations.
        # Skip this check when the next rejection would hit MAX_QA_REVISIONS (force-commit
        # will execute mutations anyway, so let them through to commit_node).
        leak_msg = _mutation_manager.detect_leak(state, revisions)
        if leak_msg:
            pending_count = len(state.get("pending_mutations", []))
            await write_audit_log(
                vault, "QA Agent", "Mutation Leak Detected",
                f"{pending_count} mutations leaked into QA without execution. Rejecting."
            )
            # Signal rejection — rollback happens in the single reject path below
            return {
                "qa_feedback": leak_msg,
                "revision_count": revisions + 1,
            }

        # Cross-check 2: mutation_errors — if mutations failed during narrator execution,
        # QA must reject because the KG may be in an inconsistent state
        errors = state.get("mutation_errors", [])
        if errors:
            await write_audit_log(
                vault, "QA Agent", "Mutation Errors Detected",
                f"Errors during mutation execution: {'; '.join(errors)}. Rejecting."
            )
            # Signal rejection — rollback happens in the single reject path below
            return {
                "qa_feedback": (
                    f"[MUTATION EXECUTION ERROR]: The following errors occurred while "
                    "committing world-state changes. The narrative cannot be approved until "
                    "the engine state is consistent:\n"
                    + "\n".join(f"  - {e}" for e in errors)
                ),
                "revision_count": revisions + 1,
            }

        # Cross-check 3: Re-validate SVO claims in QA (independent backstop)
        pending = state.get("pending_mutations", [])
        if draft and pending:
            from storylet import GraphMutation

            guardrails, _ = _mutation_manager.validate(
                state, draft, kg,
                {"vault_path": vault, "active_character": state.get("active_character", "Unknown")}
            )
            mutations = [GraphMutation(**m) for m in pending]
            svo_result = guardrails.validate_svo_claims(draft, mutations, {})
            if not svo_result.allowed:
                await write_audit_log(vault, "QA Agent", "SVO Claim Rejected (QA backstop)", svo_result.reason)
                # Signal rejection — rollback happens in the single reject path below
                return {
                    "qa_feedback": (
                        f"[QA SVO BACKSTOP REJECTED]: {svo_result.reason}\n\n"
                        f"Your draft: {draft}"
                    ),
                    "revision_count": revisions + 1,
                }

        # ------------------------------------------------------------------
        # Gather tools used and mechanical truths from this turn
        # ------------------------------------------------------------------
        recent_tools = []
        mechanical_truths = []
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage) and not msg.content.startswith("[SYSTEM OVERRIDE"):
                break
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    recent_tools.append(f"{tc['name']}({tc.get('args', {})})")
            if isinstance(msg, ToolMessage) and "MECHANICAL TRUTH" in msg.content:
                mechanical_truths.append(msg.content)

        tools_used_str = " | ".join(recent_tools) if recent_tools else "None"
        truth_str = "\n".join(mechanical_truths) if mechanical_truths else "No combat math was executed this turn."

        await write_audit_log(vault, "QA Agent", "Reviewing Draft", f"Tools audited this turn: {tools_used_str}")

        try:
            tone_rules = _get_config_tone(vault)
        except Exception:
            tone_rules = ""
        tone_check = (
            f"10. TONE & BOUNDARIES: Did the DM violate any of these boundaries: '{tone_rules}'? (If yes, REJECT).\n"
            if tone_rules
            else ""
        )

        qa_prompt = (
            "You are the strict QA Auditor for a D&D game. Review this DM Draft:\n"
            f"DRAFT: '{draft}'\n"
            f"TOOLS USED THIS TURN: {tools_used_str}\n"
            f"ENGINE MECHANICAL TRUTHS:\n{truth_str}\n\n"
            "RULES COMPLIANCE CHECKLIST:\n"
            "1. MECHANICAL SYNC: Did the draft contradict the ENGINE MECHANICAL TRUTHS? (If yes, REJECT).\n"
            "2. PLAYER AGENCY: Did the DM dictate what the player's character thinks or their actions? (If yes, REJECT).\n"
            "3. DESCRIBE TO ME: Did the DM state factual NPC motives instead of physical sensory details? (If yes, REJECT).\n"
            "4. META-GAMING: Did the DM leak mechanical stats like exact AC or HP numbers in the narrative? (If yes, REJECT).\n"
            "5. FAIL FORWARD: If an action failed, did the DM introduce a dead end? (If yes, REJECT).\n"
            "6. DICE MATH AUDIT: Did the DM hallucinate any dice rolls or damage? (If yes, REJECT).\n"
            "7. FEATS & MECHANICS: Did the DM ignore a character's active feats or spells? (If yes, REJECT).\n"
            "8. MAGIC ITEMS & ATTUNEMENT: Did the DM grant/use an item without tracking it? (If yes, REJECT).\n"
            "9. OBSIDIAN FORMATTING: Did the DM fail to use [[Wikilinks]] for proper nouns? (If yes, REJECT).\n"
            "10. COMPENDIUM AUDIT: If a feat was unsupported, did the DM notify the human player OOC? (If no, REJECT).\n"
            "11. PARADIGMS: Did the DM force movement limits out of combat? (If yes, REJECT).\n"
            "12. CRITICAL FAILURES: If a knowledge check resulted in a [NATURAL 1 - CRITICAL FAILURE],"
            " did the DM accurately tell the player the truth? (If yes, REJECT. They MUST confidently narrate dangerously wrong facts).\n"
            "13. STORYLET CONSISTENCY: If a storylet was active this turn, did the DM faithfully "
            "incorporate its narrative content without contradicting the established Knowledge Graph facts? "
            "(If the draft ignores or contradicts an active storylet's content, REJECT).\n"
            + tone_check
            + "\nIf ANY rule is broken, set 'approved' to False and explain exactly what to rewrite. "
            "If the DM applied a mechanic incorrectly, do not just tell them it is wrong. "
            "You MUST provide the details of the game rules needed to fix it."
        )

        qa_chain = qa_llm.with_structured_output(QAResult)
        try:
            result: QAResult = await qa_chain.ainvoke([HumanMessage(content=qa_prompt)], config=config)
        except Exception as e:
            print(f"[QA Agent] Error during structured output parsing: {e}. Auto-approving.")
            return {"qa_feedback": "APPROVED"}

        if getattr(result, "requires_clarification", False):
            await write_audit_log(vault, "QA Agent", "Clarification Intercept", result.clarification_message)
            qa_logger.info(
                "Clarification required from player.",
                extra={
                    "agent_id": "QA_Agent",
                    "context": {
                        "character": state.get("active_character"),
                        "vault_path": vault,
                        "clarification_message": result.clarification_message,
                    },
                },
            )
            final_msg = f"**[OOC - Engine Supervisor]:** {result.clarification_message}"
            return {
                "draft_response": final_msg,
                "qa_feedback": "APPROVED",
                "messages": [AIMessage(content=final_msg)],
            }

        elif result.approved:
            await write_audit_log(vault, "QA Agent", "Result", "COMMIT")
            qa_logger.info(
                "Draft approved. Routing to commit_node for mutation execution.",
                extra={
                    "agent_id": "QA_Agent",
                    "context": {
                        "character": state.get("active_character"),
                        "vault_path": vault,
                        "revisions_used": revisions,
                    },
                },
            )
            return {"qa_feedback": "COMMIT", "messages": [AIMessage(content=draft)]}

        else:
            # ------------------------------------------------------------------
            # Task #3: KG Rollback on Rejected Narrative
            # If QA rejects, restore the KG from the snapshot to undo any
            # mutations that were speculatively committed after narrator_node.
            # ------------------------------------------------------------------
            kg_snapshot = state.get("kg_snapshot")
            rollback_performed = False
            if kg_snapshot:
                try:
                    from registry import set_knowledge_graph
                    from knowledge_graph import KnowledgeGraph
                    restored_kg = KnowledgeGraph.model_validate(kg_snapshot)
                    set_knowledge_graph(vault, restored_kg)
                    rollback_performed = True
                    # Invalidate cache since KG was restored to pre-mutation state
                    _invalidate_grag_cache(vault)
                    await write_audit_log(
                        vault, "QA Agent", "KG Rollback",
                        "Restored KG from pre-QA snapshot. No mutations were committed before approval."
                    )
                except Exception as rb_err:
                    await write_audit_log(
                        vault, "QA Agent", "KG Rollback FAILED",
                        f"Snapshot restore failed: {rb_err}. KG may be inconsistent."
                    )

            rejection_msg = result.feedback
            if rollback_performed:
                pending_count = len(state.get("pending_mutations", []))
                rejection_msg = (
                    f"[KG ROLLED BACK — {pending_count} speculative mutations discarded]\n\n"
                    + rejection_msg
                )

            await write_audit_log(vault, "QA Agent", "Result", f"REJECTED. Feedback: {result.feedback}")
            qa_logger.warning(
                "Rule inconsistency detected. Draft rejected.",
                extra={
                    "agent_id": "QA_Agent",
                    "context": {
                        "character": state.get("active_character"),
                        "vault_path": vault,
                        "feedback": result.feedback,
                        "revision_count": revisions + 1,
                        "kg_rollback": rollback_performed,
                    },
                },
            )
            # Clear pending_mutations so they cannot reach commit_node on the
            # force-commit cycle. This also breaks the "mutation leak" self-loop:
            # leaked mutations are discarded on first rejection, preventing them
            # from surviving into the force-commit path.
            return {
                "qa_feedback": rejection_msg,
                "revision_count": revisions + 1,
                "pending_mutations": [],
            }

    # ------------------------------------------------------------------
    # NODE: DramaManager — updates tension arc and selects the next active storylet
    # ------------------------------------------------------------------
    async def drama_manager_node(state: DMState, config: RunnableConfig):
        """
        After each action turn, the Drama Manager:
        1. Updates the tension arc based on the last tool outcome (inferred from messages)
        2. Polls available storylets and selects the optimal one
        3. If a storylet is selected, activates it and routes to narrator (bypass planner)
        4. Otherwise routes back to planner for normal flow

        Also clears stale pending_mutations from a rejected previous turn when a
        new HumanMessage arrives, so old mutations don't contaminate a fresh turn.
        """
        from registry import get_storylet_registry, get_knowledge_graph
        from drama_manager import DramaManager, TensionArc
        from storylet import TensionLevel

        vault_path = state.get("vault_path", "default")
        reg = get_storylet_registry(vault_path)
        kg = get_knowledge_graph(vault_path)
        dm = DramaManager(reg, kg)

        # Restore tension arc from state
        arc_dict = state.get("tension_arc", {})
        dm.arc = TensionArc.from_dict(arc_dict) if arc_dict else TensionArc()

        # Infer outcome tension from last tool message
        outcome_tension = TensionLevel.MEDIUM
        for msg in reversed(state.get("messages", [])):
            if isinstance(msg, ToolMessage):
                content = msg.content.upper()
                if "MECHANICAL TRUTH: HIT" in content or "MECHANICAL TRUTH: DAMAGE" in content:
                    outcome_tension = TensionLevel.HIGH
                    break
                elif "MECHANICAL TRUTH:" in content:
                    outcome_tension = TensionLevel.MEDIUM
                    break
        dm.arc.advance_turn(outcome_tension)

        # Decrement storylet deadlines each session turn
        deactivated = await dm.registry.decrement_deadlines()
        if deactivated > 0:
            await write_audit_log(
                vault_path,
                "DramaManager",
                "Storylet Deadlines Expired",
                f"{deactivated} storylet(s) auto-deactivated (deadline reached).",
            )

        # Build runtime context
        ctx = {
            "vault_path": vault_path,
            "active_character": state.get("active_character", "Unknown"),
        }

        # Select next storylet
        selected = await dm.select_next(ctx)

        if selected:
            # Apply storylet effects
            dm.apply_effects(selected)
            # Check integrity
            from hard_guardrails import HardGuardrails
            guardrails = HardGuardrails(kg)
            integrity = guardrails.check_storylet_integrity(selected, ctx)
            if not integrity.allowed:
                await write_audit_log(
                    vault_path,
                    "DramaManager",
                    "Storylet Rejected (Guardrails)",
                    f"Storylet '{selected.name}' failed guardrails: {integrity.reason}",
                )
                result = {
                    "active_storylet_id": None,
                    "tension_arc": dm.arc.to_dict(),
                    "pending_mutations": [],
                }
                return result

            await write_audit_log(
                vault_path, "DramaManager", "Storylet Activated", selected.name
            )

            has_mutations = _mutation_manager.accumulate_from_storylet_effects(
                state, selected, vault_path
            )

            result: Dict[str, Any] = {
                "active_storylet_id": str(selected.id),
                "tension_arc": dm.arc.to_dict(),
            }
            if has_mutations:
                result["pending_mutations"] = state["pending_mutations"]
            return result

        return {
            "active_storylet_id": None,
            "tension_arc": dm.arc.to_dict(),
        }

    def drama_manager_router(state: DMState) -> str:
        """Route to narrator if a storylet is active, otherwise back to planner."""
        if state.get("active_storylet_id"):
            return "narrator"
        return "planner"

    # ------------------------------------------------------------------
    # NODE: action_logic — captures mutations for deferred execution (Gap 6 fix)
    #
    # DESIGN CHANGE: Mutations are NO LONGER executed immediately.
    # Instead, mutation tool calls are intercepted, validated, and stored in
    # pending_mutations. They are only EXECUTED after the narrator's QA
    # approval in narrator_node.
    #
    # This enforces the privilege segregation: the Creative LLM proposes
    # mutations, but execution is deferred until the deterministic engine
    # has approved the accompanying narrative.
    # ------------------------------------------------------------------
    async def action_logic_node(state: DMState, config: RunnableConfig):
        """
        Custom node that intercepts mutation tool calls and captures them
        into pending_mutations WITHOUT executing them.

        Execution is deferred to narrator_node after QA approval.
        """
        import json
        from registry import get_knowledge_graph
        from storylet import GraphMutation
        from hard_guardrails import HardGuardrails

        last_msg = state["messages"][-1]
        tool_name = _get_tool_name_from_message(last_msg)
        if not tool_name or tool_name not in LOGIC_TOOL_NAMES:
            # No mutation tool was called; this is a no-op passthrough
            return {}

        # Build a name -> tool lookup from master_tools_list
        tool_map = {getattr(t, "name", None): t for t in master_tools_list}
        tool = tool_map.get(tool_name)
        if not tool:
            return {}

        vault_path = state.get("vault_path", "default")
        kg = get_knowledge_graph(vault_path)
        guardrails = HardGuardrails(kg)

        # Get the tool call arguments
        tc = last_msg.tool_calls[0]
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})

        # For request_graph_mutations, inject commit=False to defer execution
        mutation_data = None
        result_content = ""

        if tool_name == "request_graph_mutations":
            # Parse the mutations from the tool args
            try:
                mut_json = args.get("mutations", "[]")
                mut_list = json.loads(mut_json) if mut_json else []
                narrative_ctx = args.get("narrative_context", "")
                parsed = [GraphMutation(**m) for m in mut_list]
            except Exception:
                parsed = []

            # Validate (but don't execute) using HardGuardrails
            ctx = {"vault_path": vault_path}
            guard_result = guardrails.validate_full_pipeline(narrative_ctx, parsed, ctx)

            if not guard_result.allowed:
                # Tool should return an error; let it execute normally via ToolNode
                # Fall through to normal execution
                tool_node = ToolNode([tool])
                return {"messages": await tool_node.ainvoke(state, config)}

            # Capture the mutation data for deferred execution
            mutation_data = [
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
                for m in parsed
            ]
            result_content = (
                f"MECHANICAL TRUTH: {len(parsed)} graph mutations validated and captured "
                f"(deferred execution pending QA approval). Mutations: {json.dumps(mutation_data)}"
            )
        elif tool_name == "reveal_secret":
            # reveal_secret: pre-validate edge exists and is secret, then defer mutation
            from knowledge_graph import GraphPredicate

            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            subject_name = args.get("subject_name", "")
            predicate_str = args.get("predicate", "")
            object_name = args.get("object_name", "")

            # Resolve edge
            subj_uuid = kg.find_node_uuid(subject_name)
            obj_uuid = kg.find_node_uuid(object_name)
            if not subj_uuid or not obj_uuid:
                result_content = f"SYSTEM ERROR: Could not find node(s): {subject_name} or {object_name}"
            else:
                try:
                    pred = GraphPredicate(predicate_str)
                except ValueError:
                    result_content = f"SYSTEM ERROR: Unknown predicate '{predicate_str}'"
                    pred = None

                if pred is not None:
                    target_edge = None
                    for edge in kg.edges:
                        if (edge.subject_uuid == subj_uuid and
                            edge.object_uuid == obj_uuid and
                            edge.predicate == pred):
                            target_edge = edge
                            break

                    if target_edge is None:
                        result_content = f"SYSTEM ERROR: Edge not found: [[{subject_name}]] --{predicate_str}--> [[{object_name}]]"
                    elif not target_edge.secret:
                        result_content = f"MECHANICAL TRUTH: Edge is already public. No secret to reveal."
                    else:
                        mutation_dict = {
                            "mutation_type": "set_edge_attribute",
                            "node_name": subject_name,
                            "predicate": predicate_str,
                            "target_name": object_name,
                            "attribute": "secret",
                            "value": False,
                        }
                        mutation_data = [mutation_dict]
                        result_content = (
                            f"MECHANICAL TRUTH: Secret-reveal mutation validated and captured "
                            f"(deferred execution pending QA approval). Mutation: {json.dumps(mutation_dict)}"
                        )

            if not mutation_data:
                mutation_data = None
        else:
            # For create_storylet and mark_entity_immutable, execute immediately
            # (they don't have the commit=False pattern yet)
            tool_node = ToolNode([tool])
            return {"messages": await tool_node.ainvoke(state, config)}

        # Build a ToolMessage to add to state
        from langchain_core.messages import ToolMessage as LCToolMessage

        tool_msg = LCToolMessage(
            content=result_content,
            tool_call_id=tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "unknown"),
            name=tool_name,
        )

        # Return: add ToolMessage to messages, plus pending_mutations
        updates = {
            "messages": [tool_msg],
        }
        if mutation_data:
            _mutation_manager.accumulate_from_tool_calls(state, mutation_data)
            updates["pending_mutations"] = state["pending_mutations"]

        return updates

    # ------------------------------------------------------------------
    # NODE: clear_mutations_node — clears stale pending_mutations from a QA-rejected
    # previous turn when a new HumanMessage arrives, BEFORE routing to tool execution.
    #
    # This must run after planner_node (which produces tool calls from the new input)
    # but before action/action_logic (which would execute with stale mutation state).
    #
    # Routing mirrors planner_tool_router: routes based on the last message's tool calls.
    # ------------------------------------------------------------------
    async def clear_mutations_node(state: DMState, config: RunnableConfig):
        """
        Check if the last message is a new HumanMessage with stale pending_mutations
        from a QA-rejected previous turn. If so, clear them.

        Routing:
        - AIMessage with run_ingestion_pipeline_tool → ingestion (Phase 2 pipeline)
        - AIMessage with mutation tool call → action_logic
        - AIMessage with other tool call → action
        - AIMessage without tool calls → narrator (direct prose)
        """
        last_msg = state["messages"][-1]
        updates = {}

        # Detect new player turn: HumanMessage means a fresh turn started.
        # Clear any stale pending_mutations from a QA-rejected previous turn.
        if _mutation_manager.should_clear_on_human_message(state):
            _mutation_manager.clear(state)
            updates["pending_mutations"] = []
            await write_audit_log(
                state.get("vault_path", "default"),
                "ClearMutations",
                "Stale Mutations Cleared",
                "New player turn detected. Cleared pending_mutations from rejected turn.",
            )

        # Route based on what planner returned (the AIMessage before this node)
        tool_name = _get_tool_name_from_message(last_msg)
        if tool_name == "run_ingestion_pipeline_tool":
            goto = "ingestion"
        elif tool_name in LOGIC_TOOL_NAMES:
            goto = "action_logic"
        elif tool_name and tool_name not in ("__end__",):
            goto = "action"
        else:
            goto = "narrator"

        # Return state updates AND routing destination
        from langgraph.types import Command
        return Command(goto=goto, update=updates if updates else None)

    # ------------------------------------------------------------------
    # ROUTER: planner_tool_router — routes to clear_mutations_node (which handles routing)
    # ------------------------------------------------------------------
    def planner_tool_router(state: DMState) -> str:
        """Route: ingestion → ingestion_node (direct, no stale-check needed), all others → clear_mutations."""
        last_msg = state["messages"][-1]
        tool_name = _get_tool_name_from_message(last_msg)
        if tool_name == "run_ingestion_pipeline_tool":
            return "ingestion"
        return "clear_mutations"

    # ------------------------------------------------------------------
    # NODE: ingestion_node — runs the NLP ingestion pipeline with direct LLM access
    #
    # Unlike other tools (invoked via ToolNode), this node calls the ingestion
    # pipeline directly so it has access to the session LLM (draft_llm from closure).
    # ------------------------------------------------------------------
    async def ingestion_node(state: DMState, config: RunnableConfig):
        """
        Phase 2: Parse raw NPC lore and campaign narrative into KG entities and Storylets.

        Triggered when the planner calls run_ingestion_pipeline_tool.
        Uses the session LLM (draft_llm from build_graph closure) directly.
        """
        from langchain_core.messages import ToolMessage as LCToolMessage

        last_msg = state["messages"][-1]
        tool_name = _get_tool_name_from_message(last_msg)
        if tool_name != "run_ingestion_pipeline_tool":
            return {}  # Not an ingestion call — shouldn't happen

        # Extract tool args from the planner's AIMessage
        tc = last_msg.tool_calls[0]
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})

        vault_path = state.get("vault_path", "default")
        npc_lore = args.get("npc_lore_text", "") or ""
        campaign = args.get("campaign_narrative_text", "") or ""
        try:
            import json
            resolutions_raw = args.get("storylet_resolutions_json", "{}")
            resolutions = json.loads(resolutions_raw) if resolutions_raw else {}
        except Exception:
            resolutions = {}

        try:
            from ingestion_pipeline import run_ingestion_pipeline
            result = await run_ingestion_pipeline(
                vault_path=vault_path,
                npc_lore_text=npc_lore or None,
                campaign_narrative_text=campaign or None,
                storylet_resolutions=resolutions or None,
                llm=draft_llm,
            )
            summary = (
                f"MECHANICAL TRUTH: Ingestion pipeline complete.\n"
                f"  KG nodes added: {result['nodes_added']}\n"
                f"  KG edges added: {result['edges_added']}\n"
                f"  Storylets created: {result['storylets_created']}\n"
                f"  Effects annotated: {result['effects_annotated']}"
            )
        except Exception as e:
            summary = f"SYSTEM ERROR: Ingestion pipeline failed: {e}"

        tool_msg = LCToolMessage(
            content=summary,
            tool_call_id=tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "unknown"),
            name="run_ingestion_pipeline_tool",
        )
        return {"messages": [tool_msg]}

    # ------------------------------------------------------------------
    # ROUTER: qa_router — decides whether to approve or loop back
    # ------------------------------------------------------------------
    def qa_router(state: DMState) -> str:
        # APPROVED: route to commit_node to execute deferred mutations before ending
        if state.get("qa_feedback") == "COMMIT":
            return "commit"
        # Force approve at max revisions — still route to commit_node to persist mutations
        if state.get("revision_count", 0) >= MAX_QA_REVISIONS:
            print("[QA Agent] - Max revisions reached. Routing to commit_node.")
            return "commit"
        # Rejection: route back to narrator for revision
        return "narrator"

    # ------------------------------------------------------------------
    # NODE: commit_node — executes deferred mutations after QA approval
    #
    # DESIGN: Mutations are NOT committed in narrator_node (which checkpoints
    # before QA runs). Instead they stay in pending_mutations through the
    # QA cycle. Only after QA approves do we commit.
    #
    # This ensures the checkpointer never saves a state where mutations are
    # committed but the narrative is rejected — fixing the rollback/checkpointer
    # issue that required speculative execution.
    async def commit_node(state: DMState, config: RunnableConfig):
        from registry import get_knowledge_graph

        vault_path = state.get("vault_path", "default")
        kg = get_knowledge_graph(vault_path)
        narrative_context = state.get("draft_response", "")

        errors = await _mutation_manager.commit(state, kg, vault_path, draft_llm)

        await write_audit_log(
            vault_path, "CommitNode", "Turn Complete",
            f"Draft approved. Errors: {len(errors)}"
        )

        return {
            "qa_feedback": "APPROVED",
            # Explicitly return cleared lists so LangGraph state merge picks them up
            "pending_mutations": [],
            "mutation_errors": errors,
        }

    # ------------------------------------------------------------------
    # GRAPH ASSEMBLY
    # ------------------------------------------------------------------
    workflow = StateGraph(DMState)
    workflow.add_node("planner", planner_node)
    workflow.add_node("action", ToolNode(master_tools_list))
    workflow.add_node("action_logic", action_logic_node)  # Mutation tools with Hard Guardrail enforcement
    workflow.add_node("drama_manager", drama_manager_node)
    workflow.add_node("narrator", narrator_node)
    workflow.add_node("qa", qa_node)
    workflow.add_node("commit", commit_node)
    workflow.add_node("clear_mutations", clear_mutations_node)
    workflow.add_node("ingestion", ingestion_node)

    workflow.set_entry_point("planner")
    # Planner routes: ingestion → ingestion_node, everything else → clear_mutations (stale check)
    workflow.add_conditional_edges(
        "planner",
        planner_tool_router,
        {"clear_mutations": "clear_mutations", "ingestion": "ingestion"},
    )
    # clear_mutations_node uses Command(goto=...) to route to action/action_logic/narrator
    # After action (creative or logic), always route to drama_manager to update tension arc
    workflow.add_edge("action", "drama_manager")
    workflow.add_edge("action_logic", "drama_manager")
    # Ingestion goes to drama_manager to continue the session flow
    workflow.add_edge("ingestion", "drama_manager")
    # Drama manager conditionally routes: storylet active → narrator, else → planner
    workflow.add_conditional_edges("drama_manager", drama_manager_router, {"narrator": "narrator", "planner": "planner"})
    workflow.add_edge("narrator", "qa")
    # QA routes: approved → commit_node (executes mutations), rejected → narrator (retry)
    workflow.add_conditional_edges("qa", qa_router, {"commit": "commit", "narrator": "narrator"})
    # commit_node always ends the turn (QA has already approved)
    workflow.add_edge("commit", END)

    return workflow.compile(checkpointer=checkpointer)
