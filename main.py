import os
import traceback
import json
import asyncio
import yaml
import aiofiles
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.exceptions import RequestValidationError

from pydantic import BaseModel, Field
import uvicorn
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt import tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.runnables import RunnableConfig

from shapely.geometry import LineString
from state import DMState, QAResult
from vault_io import (
    write_audit_log,
    initialize_engine_from_vault,
    sync_engine_to_vault,
    get_journals_dir,
    edit_markdown_entity,
    read_markdown_entity,
)
from tools import (
    upsert_journal_section,
    create_new_entity,
    flesh_out_entity,
    update_yaml_frontmatter,
    equip_item,
    use_expendable_resource,
    fetch_entity_context,
    level_up_character,
    update_character_status,
    manage_inventory,
    roll_generic_dice,
    perform_ability_check_or_save,
    search_vault_by_tag,
    advance_time,
    start_combat,
    update_combat_state,
    end_combat,
    execute_melee_attack,
    modify_health,
    query_bestiary,
    query_rulebook,
    query_campaign_module,
    use_ability_or_spell,
    encode_new_compendium_entry,
    drop_concentration,
    ready_action,
    clear_readied_action,
    move_entity,
    use_dash_action,
    manage_light_sources,
    toggle_condition,
    execute_grapple_or_shove,
    manage_map_geometry,
    trigger_environmental_hazard,
    interact_with_object,
    manage_map_trap,
    manage_skill_challenge,
    generate_random_loot,
    ingest_battlemap_json,
    manage_map_terrain,
    update_roll_automations,
    _get_config_tone,
    _get_entity_by_name,
    _calculate_reach,
)
import event_handlers  # noqa: F401
from spatial_engine import spatial_service
from registry import get_all_entities

# Define the master list of tools ONCE so it's consistent
MASTER_TOOLS_LIST = [
    upsert_journal_section,
    create_new_entity,
    flesh_out_entity,
    update_yaml_frontmatter,
    equip_item,
    use_expendable_resource,
    fetch_entity_context,
    level_up_character,
    update_character_status,
    manage_inventory,
    roll_generic_dice,
    perform_ability_check_or_save,
    search_vault_by_tag,
    advance_time,
    start_combat,
    update_combat_state,
    end_combat,
    execute_melee_attack,
    modify_health,
    query_bestiary,
    query_rulebook,
    query_campaign_module,
    use_ability_or_spell,
    encode_new_compendium_entry,
    drop_concentration,
    ready_action,
    clear_readied_action,
    move_entity,
    use_dash_action,
    manage_light_sources,
    toggle_condition,
    execute_grapple_or_shove,
    manage_map_geometry,
    trigger_environmental_hazard,
    interact_with_object,
    manage_map_trap,
    manage_skill_challenge,
    generate_random_loot,
    ingest_battlemap_json,
    manage_map_terrain,
]

# 1. INITIALIZE THE APP FIRST
# This is handled by the lifespan manager further down

# GLOBAL STATE
dm_engine_app = None
draft_llm = None
qa_llm = None

# MULTIPLAYER LOCKS: Maps character_name -> client_id
CHARACTER_LOCKS = {}
LAST_SEEN = {}  # client_id -> timestamp


class EventBroadcaster:
    def __init__(self):
        self.queues = []

    def add_queue(self, client_id: str, q: asyncio.Queue):
        self.queues.append((client_id, q))

    def remove_queue(self, client_id: str, q: asyncio.Queue):
        if (client_id, q) in self.queues:
            self.queues.remove((client_id, q))

    async def broadcast(self, sender_id: str, data: str):
        for cid, q in list(self.queues):
            if cid != sender_id:
                try:
                    await q.put(data)
                except Exception:
                    pass


broadcaster = EventBroadcaster()


# 5. CODE
# 5.1. THE PLANNER NODE

# --- REPLACE YOUR EXISTING GRAPH NODES AND BUILDER WITH THIS ---


async def planner_node(state: DMState, config: RunnableConfig):
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
            "12. ENVIRONMENT: You can cast spells or cause effects that alter the environment. Use `manage_map_terrain`.\n\n"
            "MOVEMENT PARADIGMS:\n"
            "- TRAVEL / TOWN (Out of Combat): Use `move_entity(movement_type='travel')`.\n"
            "- DUNGEON CRAWL (Out of Combat): Use `move_entity(movement_type='walk')`.\n"
            "- COMBAT: Use `move_entity(movement_type='walk')`. Strict 5ft grid speeds and Opportunity Attacks apply.\n\n"
            "If you get a CACHE MISS on a spell or ability, use `query_rulebook` to find the rules, \n"
            "then `encode_new_compendium_entry` to permanently save it to the engine.\n\n"
            'Once all tool logic is complete and you have the "MECHANICAL TRUTH", \n'
            "output a brief summary of the events. DO NOT write dialogue or narrative prose.\n"
        )
    )
    llm_with_tools = draft_llm.bind_tools(MASTER_TOOLS_LIST)
    response = await llm_with_tools.ainvoke([sys_msg] + state["messages"], config=config)
    return {"messages": [response]}


async def narrator_node(state: DMState, config: RunnableConfig):
    """The Storyteller. Turns the mechanical truth into vivid prose."""

    # 1. Check if we are in a revision loop
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
            f"Do not violate player agency or do anything more than add color to an action dialogue. {feedback_context}\n\n"
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

    # Invoke WITHOUT tools so it is forced to write a string
    response = await draft_llm.ainvoke([sys_msg] + state["messages"], config=config)
    return {"draft_response": response.content}


def build_graph():
    workflow = StateGraph(DMState)

    workflow.add_node("planner", planner_node)

    # ADD YOUR OTHER TOOLS HERE
    workflow.add_node("action", ToolNode(MASTER_TOOLS_LIST))

    workflow.add_node("narrator", narrator_node)
    workflow.add_node("qa", qa_node)

    workflow.set_entry_point("planner")
    workflow.add_conditional_edges("planner", tools_condition, {"tools": "action", "__end__": "narrator"})
    workflow.add_edge("action", "planner")
    workflow.add_edge("narrator", "qa")
    workflow.add_conditional_edges("qa", qa_router)

    return workflow.compile(checkpointer=MemorySaver())


async def qa_node(state: DMState, config: RunnableConfig):
    vault, draft, revisions = state["vault_path"], state["draft_response"], state.get("revision_count", 0)

    # --- ESCAPE CLAUSE 1: Allow DM to ask questions without being audited ---
    if draft.strip().startswith("[OOC") or draft.strip().startswith("OOC:"):
        await write_audit_log(vault, "QA Agent", "Bypass", "OOC Clarification detected. Auto-approving.")
        return {"qa_feedback": "APPROVED", "messages": [AIMessage(content=draft)]}

    # --- ESCAPE CLAUSE 2: Max Revisions ---
    if revisions >= 3:
        await write_audit_log(vault, "QA Agent", "Force Approve", "Max revisions reached. Passing to prevent loop.")
        return {"qa_feedback": "APPROVED", "messages": [AIMessage(content=draft)]}

    # --- GATHER CONTEXT (Tools & Mechanical Truths) ---
    recent_tools = []
    mechanical_truths = []

    # Iterate backwards to find everything that happened THIS turn
    for msg in reversed(state["messages"]):
        # Stop searching when we hit the player's actual prompt
        if isinstance(msg, HumanMessage) and not msg.content.startswith("[SYSTEM OVERRIDE"):
            break

        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                recent_tools.append(f"{tc['name']}({tc.get('args', {})})")

        # NEW: Grab the math results from the deterministic engine!
        if isinstance(msg, ToolMessage) and "MECHANICAL TRUTH" in msg.content:
            mechanical_truths.append(msg.content)

    tools_used_str = " | ".join(recent_tools) if recent_tools else "None"
    truth_str = "\n".join(mechanical_truths) if mechanical_truths else "No combat math was executed this turn."

    await write_audit_log(vault, "QA Agent", "Reviewing Draft", f"Tools audited this turn: {tools_used_str}")

    # --- TONE CHECKING ---
    try:
        tone_rules = _get_config_tone(vault)
    except Exception:
        tone_rules = ""
    tone_check = (
        f"10. TONE & BOUNDARIES: Did the DM violate any of these boundaries: '{tone_rules}'? (If yes, REJECT).\n"
        if tone_rules
        else ""
    )

    # --- THE SUPER-PROMPT (Merged Old + New) ---
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
        + tone_check
        + "\nIf ANY rule is broken, set 'approved' to False and explain exactly what to rewrite. "
        "If the DM applied a mechanic incorrectly, do not just tell them it is wrong. "
        "You MUST provide the details of the game rules needed to fix it."
    )

    # We use structured output to enforce the QAResult schema
    qa_chain = qa_llm.with_structured_output(QAResult)

    try:
        result: QAResult = await qa_chain.ainvoke([HumanMessage(content=qa_prompt)], config=config)
    except Exception as e:
        print(f"[QA Agent] Error during structured output parsing: {e}. Auto-approving.")
        return {"qa_feedback": "APPROVED"}

    # --- QA INTERCEPT: QA takes over and asks the player directly ---
    if getattr(result, "requires_clarification", False):
        await write_audit_log(vault, "QA Agent", "Clarification Intercept", result.clarification_message)
        final_msg = f"**[OOC - Engine Supervisor]:** {result.clarification_message}"
        return {
            "draft_response": final_msg,
            "qa_feedback": "APPROVED",  # Approving it forces the graph to end and output this clarification
            "messages": [AIMessage(content=final_msg)],
        }

    elif result.approved:
        await write_audit_log(vault, "QA Agent", "Result", "APPROVED")
        return {"qa_feedback": "APPROVED", "messages": [AIMessage(content=draft)]}

    else:
        await write_audit_log(vault, "QA Agent", "Result", f"REJECTED. Feedback: {result.feedback}")

        return {"qa_feedback": result.feedback, "revision_count": revisions + 1}


def qa_router(state: DMState) -> str:
    # If approved, we are done!
    if state.get("qa_feedback") == "APPROVED":
        return END

    # Prevent infinite loops. If it fails 3 times, force it to END anyway.
    if state.get("revision_count", 0) >= 3:
        print("[QA Agent] - Max revisions reached. Force approving.")
        return END

    # If rejected and under the limit, send it back to the Narrator to rewrite
    return "narrator"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dm_engine_app, draft_llm, qa_llm

    try:
        # Upgraded to Pro for deeper reasoning, and Temp 0.6 to encourage "Jazz"
        draft_llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.6)
        qa_llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.1)

        # Build and compile the multi-agent graph ONCE when the server boots
        dm_engine_app = build_graph()

        print("DM Engine initialized successfully with ReAct architecture.")
        yield
    except Exception as e:
        print(f"Failed to initialize DM Engine: {e}")
        yield


# --- 4. YOUR FAST API APP ---
app = FastAPI(title="AI DM Engine", lifespan=lifespan)

# ADD CORS MIDDLEWARE (Crucial for Obsidian to connect!)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# EXCEPTION HANDLERS
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("\n" + "!" * 40)
    print("🚨 BAD PAYLOAD RECEIVED FROM FRONTEND 🚨")
    print(f"Details: {exc.errors()}")
    print("!" * 40 + "\n")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": exc.body},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print("\n" + "💥" * 20)
    print("💥 UNHANDLED SERVER EXCEPTION 💥")
    traceback.print_exc()
    print("💥" * 20 + "\n")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error. Check the backend console."},
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    print(f"\n---> Incoming Request: {request.method} {request.url}")

    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        print(f"<--- Response: {response.status_code} (Took {process_time:.2f}s)")
        return response
    except Exception as e:
        print(f"<--- Server Error: {str(e)}")
        raise


class ChatRequest(BaseModel):
    message: str
    character: str
    vault_path: str
    client_id: str
    roll_automations: dict = Field(default_factory=dict)


class ChatResponse(BaseModel):
    reply: str
    status: str


class SwitchRequest(BaseModel):
    old_character: str
    new_character: str
    client_id: str


class HeartbeatRequest(BaseModel):
    client_id: str
    character: str
    roll_automations: dict = Field(default_factory=dict)


class VaultRequest(BaseModel):
    vault_path: str


class OOCMoveRequest(BaseModel):
    entity_name: str
    x: float
    y: float
    vault_path: str


class ProposeMoveRequest(BaseModel):
    entity_name: str
    waypoints: list[tuple[float, float]]
    vault_path: str
    force_execute: bool = False


class ProposeMoveResponse(BaseModel):
    is_valid: bool
    opportunity_attacks: list[str] = Field(default_factory=list)
    traps_triggered: list[str] = Field(default_factory=list)
    alternative_path: list[tuple[float, float]] = Field(default_factory=list)
    movement_cost: float = Field(default=0.0)
    invalid_reason: str = ""
    executed: bool = False
    final_x: float = Field(default=0.0)
    final_y: float = Field(default=0.0)


class ToggleFoWRequest(BaseModel):
    vault_path: str
    disabled_for: list[str]


class ClearPathRequest(BaseModel):
    entity_name: str
    vault_path: str


class PingRequest(BaseModel):
    client_id: str
    character: str
    x: float
    y: float
    vault_path: str


@app.post("/ooc_move_entity")
async def ooc_move_entity_endpoint(request: OOCMoveRequest):
    """OOC wrapper for the DM to drag and drop tokens without triggering combat rules."""

    entity = await _get_entity_by_name(request.entity_name, request.vault_path)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if request.entity_name in spatial_service.active_paths.get(request.vault_path, {}):
        del spatial_service.active_paths[request.vault_path][request.entity_name]

    entity.x, entity.y = round(request.x, 1), round(request.y, 1)
    spatial_service.sync_entity(entity)

    file_path = os.path.join(get_journals_dir(request.vault_path), f"{entity.name}.md")
    if os.path.exists(file_path):
        try:
            async with edit_markdown_entity(file_path) as state:
                state["yaml_data"]["x"], state["yaml_data"]["y"] = entity.x, entity.y
        except Exception:
            pass

    combat_file = os.path.join(get_journals_dir(request.vault_path), "ACTIVE_COMBAT.md")
    if os.path.exists(combat_file):
        try:
            async with edit_markdown_entity(combat_file) as state:
                for c in state["yaml_data"].get("combatants", []):
                    if c.get("name", "").lower() == entity.name.lower():
                        c["x"], c["y"] = entity.x, entity.y
        except Exception:
            pass
    return {"status": "success", "x": entity.x, "y": entity.y}


@app.post("/toggle_fow")
async def toggle_fow_endpoint(request: ToggleFoWRequest):
    """Overwrites the list of characters who bypass Fog of War."""
    spatial_service.get_map_data(request.vault_path).fow_disabled_for = request.disabled_for
    return {"status": "success", "fow_disabled_for": spatial_service.map_data.fow_disabled_for}


@app.post("/clear_path")
async def clear_path_endpoint(request: ClearPathRequest):
    """Clears a proposed or alternate path from the canvas."""
    if request.entity_name in spatial_service.active_paths.get(request.vault_path, {}):
        del spatial_service.active_paths[request.vault_path][request.entity_name]
    return {"status": "success"}


@app.post("/ping")
async def ping_endpoint(request: PingRequest):
    """Broadcasts a map ping to all connected clients."""
    payload = {
        "type": "ping",
        "character": request.character,
        "x": request.x,
        "y": request.y,
        "reply": f"**[{request.character}]** pinged the map at ({request.x}, {request.y}).\n\n",
        "status": "streaming",
    }
    await broadcaster.broadcast("", f"data: {json.dumps(payload)}\n\n")
    await broadcaster.broadcast("", f"data: {json.dumps({'status': 'done'})}\n\n")
    return {"status": "success"}


@app.post("/propose_move", response_model=ProposeMoveResponse)
async def propose_move_endpoint(request: ProposeMoveRequest):  # noqa: C901
    """Analyzes a proposed movement path for collisions, opportunity attacks, and traps."""

    entity = await _get_entity_by_name(request.entity_name, request.vault_path)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if request.entity_name in spatial_service.active_paths.get(request.vault_path, {}):
        del spatial_service.active_paths[request.vault_path][request.entity_name]

    is_valid = True
    invalid_reason = ""
    opportunity_attacks = []
    traps_triggered = []
    alternative_path = []

    active_list = spatial_service.active_combatants.get(request.vault_path, [])
    is_in_combat = any(c.lower() == request.entity_name.lower() for c in active_list)
    # 0. Enforce Combat Turn Order
    combat_file = os.path.join(get_journals_dir(request.vault_path), "ACTIVE_COMBAT.md")
    if os.path.exists(combat_file):
        try:
            async with read_markdown_entity(combat_file) as (yaml_data, _):
                combatants = yaml_data.get("combatants", [])
                current_idx = yaml_data.get("current_turn_index", 0)
                if combatants:
                    active_combatant = combatants[current_idx]["name"] if 0 <= current_idx < len(combatants) else ""
                    if is_in_combat and active_combatant.lower() != request.entity_name.lower():
                        return ProposeMoveResponse(
                            is_valid=False,
                            invalid_reason=f"It is currently {active_combatant}'s turn. You cannot move out of turn.",
                            executed=False,
                        )
        except Exception:
            pass

    pixels_per_foot = getattr(spatial_service.get_map_data(request.vault_path), "pixels_per_foot", 1.0)
    foot_waypoints = [(p[0] / pixels_per_foot, p[1] / pixels_per_foot) for p in request.waypoints]

    # Discretize path into <= 5ft chunks for precise trigger detection
    detailed_path = [foot_waypoints[0]]
    for i in range(1, len(foot_waypoints)):
        start = detailed_path[-1]
        end = foot_waypoints[i]
        dist = spatial_service.calculate_distance(start[0], start[1], entity.z, end[0], end[1], entity.z, request.vault_path)
        if dist > 5.0:
            num_steps = int(dist // 5.0)
            for s in range(1, num_steps + 1):
                fraction = (s * 5.0) / dist
                midpoint = (start[0] + (end[0] - start[0]) * fraction, start[1] + (end[1] - start[1]) * fraction)
                if midpoint != detailed_path[-1]:
                    detailed_path.append(midpoint)
        if detailed_path[-1] != end:
            detailed_path.append(end)

    movement_cost = 0.0
    ignores_dt = any(t.lower() == "ignore_difficult_terrain" for t in getattr(entity, "tags", []))
    is_disengaging = any(c.name.lower() == "disengage" for c in getattr(entity, "active_conditions", []))
    all_entities = get_all_entities(request.vault_path)

    executed_waypoints = [detailed_path[0]]
    final_x, final_y = detailed_path[0][0], detailed_path[0][1]

    for i in range(1, len(detailed_path)):
        start = detailed_path[i - 1]
        end = detailed_path[i]

        segment_dist = spatial_service.calculate_distance(
            start[0], start[1], entity.z, end[0], end[1], entity.z, request.vault_path
        )
        if segment_dist == 0:
            continue

        segment_cost = segment_dist
        path_line = LineString([start, end])

        collision = spatial_service.check_path_collision(
            start[0], start[1], entity.z, end[0], end[1], entity.z, vault_path=request.vault_path
        )
        if collision:
            is_valid = False
            invalid_reason = f"Path blocked by {collision.label}."
            alternative_path = [
                (start[0] * pixels_per_foot, start[1] * pixels_per_foot),
                (
                    (collision.start[0] - 5) * pixels_per_foot,
                    (collision.start[1] - 5) * pixels_per_foot,
                ),
                (end[0] * pixels_per_foot, end[1] * pixels_per_foot),
            ]
            break

        if not ignores_dt:
            for terrain in spatial_service.get_map_data(request.vault_path).active_terrain:
                if getattr(terrain, "is_difficult", False):
                    from shapely.geometry import Polygon

                    try:
                        poly = Polygon(terrain.points)
                        if path_line.intersects(poly):
                            intersection = path_line.intersection(poly)
                            if path_line.length > 0:
                                segment_cost += segment_dist * (intersection.length / path_line.length)
                    except Exception:
                        pass

        if hasattr(entity, "movement_remaining") and is_in_combat:
            projected_cost = movement_cost + segment_cost
            truncated_cost = int(projected_cost * 100) / 100.0
            if truncated_cost > entity.movement_remaining:
                is_valid = False
                movement_cost += segment_cost
                invalid_reason = (
                    f"Movement cost ({movement_cost:.1f} ft) exceeds remaining speed ({entity.movement_remaining} ft)."
                )
                break

        # Check Known Traps
        trap_hit = None
        for wall in spatial_service.get_map_data(request.vault_path).active_walls:
            if getattr(wall, "trap", None) and getattr(wall.trap, "known_by_players", False):
                if path_line.intersects(wall.line):
                    trap_hit = wall.trap.hazard_name
                    break
        if not trap_hit:
            for terrain in spatial_service.get_map_data(request.vault_path).active_terrain:
                if getattr(terrain, "trap", None) and getattr(terrain.trap, "known_by_players", False):
                    if path_line.intersects(terrain.polygon):
                        trap_hit = terrain.trap.hazard_name
                        break
        if trap_hit:
            traps_triggered.append(trap_hit)
            if not request.force_execute:
                break

        # Check OAs
        oa_hit = False
        for other_entity in all_entities.values():
            if (
                other_entity.entity_uuid != entity.entity_uuid
                and hasattr(other_entity, "hp")
                and getattr(other_entity.hp, "base_value", 0) > 0
            ):
                if is_disengaging and "ignores_disengage" not in getattr(other_entity, "tags", []):
                    continue

                base_reach = _calculate_reach(other_entity, is_active_turn=False)
                eff_reach = base_reach + max(0, (other_entity.size - 5.0) / 2.0) + max(0, (entity.size - 5.0) / 2.0)

                dist_before = spatial_service.calculate_distance(
                    start[0], start[1], 0, other_entity.x, other_entity.y, 0, request.vault_path
                )
                dist_after = spatial_service.calculate_distance(
                    end[0], end[1], 0, other_entity.x, other_entity.y, 0, request.vault_path
                )
                if dist_before <= eff_reach and dist_after > eff_reach:
                    oa_hit = True
                    opportunity_attacks.append(other_entity.name)
        if oa_hit and not request.force_execute:
            break

        movement_cost += segment_cost
        executed_waypoints.append(end)
        final_x, final_y = end[0], end[1]

        # Break here to enforce resolving attacks/traps natively before continuing!
        if oa_hit or trap_hit:
            break

    executed = False
    if is_valid and len(executed_waypoints) > 1:
        if request.force_execute or (not opportunity_attacks and not traps_triggered):
            config = {"configurable": {"thread_id": request.vault_path}}

            points_to_execute = []
            for fw in foot_waypoints[1:]:
                if fw in executed_waypoints:
                    points_to_execute.append(fw)
            if executed_waypoints[-1] not in points_to_execute:
                points_to_execute.append(executed_waypoints[-1])

            for point in points_to_execute:
                await move_entity.ainvoke(
                    {
                        "entity_name": request.entity_name,
                        "target_x": round(point[0], 2),
                        "target_y": round(point[1], 2),
                        "movement_type": "walk",
                    },
                    config=config,
                )

            executed = True

    if not executed:
        spatial_service.active_paths.setdefault(request.vault_path, {})[request.entity_name] = {
            "entity_name": request.entity_name,
            "waypoints": request.waypoints,
            "alternative_path": alternative_path,
            "is_valid": is_valid,
        }

    return ProposeMoveResponse(
        is_valid=is_valid,
        opportunity_attacks=list(set(opportunity_attacks)),
        traps_triggered=list(set(traps_triggered)),
        alternative_path=alternative_path,
        movement_cost=movement_cost,
        invalid_reason=invalid_reason,
        executed=executed,
        final_x=final_x * pixels_per_foot,
        final_y=final_y * pixels_per_foot,
    )


@app.post("/characters")
async def list_characters_endpoint(request: VaultRequest):
    """Allows external Web UIs to fetch the list of active player characters in the vault."""
    j_dir = get_journals_dir(request.vault_path)
    chars = ["Human DM"]
    if os.path.exists(j_dir):
        for filename in os.listdir(j_dir):
            if filename.endswith(".md"):
                file_path = os.path.join(j_dir, filename)
                try:
                    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                        content = await f.read()
                        if content.startswith("---"):
                            parts = content.split("---", 2)
                            if len(parts) >= 3:
                                yaml_data = await asyncio.to_thread(yaml.safe_load, parts[1]) or {}
                                tags = yaml_data.get("tags", [])
                                if isinstance(tags, str):
                                    tags = [tags]
                                if any(t.lower() in ["pc", "player"] for t in tags):
                                    chars.append(yaml_data.get("name", filename.replace(".md", "")))
                except Exception:
                    pass
    return {"characters": list(set(chars))}


class CharSheetRequest(BaseModel):
    vault_path: str
    character: str


@app.post("/character_sheet")
async def character_sheet_endpoint(request: CharSheetRequest):
    """Fetches the parsed YAML data for the active character to populate the UI."""
    if request.character == "Human DM":
        return {"sheet": {"name": "Human DM", "role": "Dungeon Master", "hp": "∞", "ac": "∞"}}

    file_path = os.path.join(get_journals_dir(request.vault_path), f"{request.character}.md")
    if not os.path.exists(file_path):
        return {"error": f"Character file {request.character}.md not found."}

    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return {"sheet": await asyncio.to_thread(yaml.safe_load, parts[1]) or {}}
    except Exception as e:
        return {"error": str(e)}
    return {"error": "Invalid format."}


@app.get("/vault_media")
async def vault_media_endpoint(filepath: str):
    """Securely serves local image files from the Obsidian vault to the web client."""
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    # Basic security check to prevent directory traversal
    if ".." in filepath:
        raise HTTPException(status_code=403, detail="Invalid path")
    return FileResponse(filepath)


@app.get("/maps")
async def maps_endpoint():
    """Exports the live spatial engine map and any other relevant battlemaps."""
    ascii_map = spatial_service.render_ascii_map(width=40, height=20).replace(
        "**", ""
    )  # Strip markdown bolding for raw PRE block
    maps = {"Live Tactical Grid": ascii_map} if "disabled" not in ascii_map.lower() else {}
    return {"maps": maps}


@app.post("/map_state")
async def map_state_endpoint(request: VaultRequest):
    """Exports the rich JSON geometry, image paths, and FoW for the VTT Canvas."""
    md = spatial_service.get_map_data(request.vault_path)

    entities = []

    for uid, ent in get_all_entities(request.vault_path).items():
        if hasattr(ent, "x") and hasattr(ent, "y"):
            is_pc = any(t in getattr(ent, "tags", []) for t in ["pc", "player"])

            icon_path = getattr(ent, "icon_url", "")
            if icon_path and not os.path.isabs(icon_path):
                icon_path = os.path.join(request.vault_path, icon_path)

            entities.append(
                {
                    "name": ent.name,
                    "x": ent.x,
                    "y": ent.y,
                    "size": ent.size,
                    "is_pc": is_pc,
                    "hp": ent.hp.base_value if hasattr(ent, "hp") else 0,
                    "icon_url": icon_path,
                }
            )

    known_traps = []
    for wall in md.active_walls:
        if wall.trap and wall.trap.known_by_players:
            known_traps.append(
                {
                    "x": (wall.start[0] + wall.end[0]) / 2,
                    "y": (wall.start[1] + wall.end[1]) / 2,
                    "name": wall.trap.hazard_name,
                }
            )

    for terrain in md.active_terrain:
        if terrain.trap and terrain.trap.known_by_players:
            x = sum(p[0] for p in terrain.points) / len(terrain.points)
            y = sum(p[1] for p in terrain.points) / len(terrain.points)
            known_traps.append(
                {
                    "x": x,
                    "y": y,
                    "name": terrain.trap.hazard_name,
                }
            )

    return {
        "map_data": md.model_dump(),
        "entities": entities,
        "known_traps": known_traps,
        "active_paths": list(spatial_service.active_paths.get(request.vault_path, {}).values()),
    }


@app.post("/heartbeat")
async def heartbeat_endpoint(request: HeartbeatRequest):
    LAST_SEEN[request.client_id] = time.time()

    # Purge stale clients (no heartbeat for 15 seconds)
    current_time = time.time()
    stale_clients = [cid for cid, ts in LAST_SEEN.items() if current_time - ts > 15]

    for cid in stale_clients:
        del LAST_SEEN[cid]
        keys_to_del = [k for k, v in CHARACTER_LOCKS.items() if v == cid]
        for k in keys_to_del:
            del CHARACTER_LOCKS[k]

    # Reinforce the lock for the current character
    if request.character != "Human DM":
        update_roll_automations(request.character, request.roll_automations)
        if request.character not in CHARACTER_LOCKS or CHARACTER_LOCKS[request.character] == request.client_id:
            CHARACTER_LOCKS[request.character] = request.client_id

    locked_by_others = [k for k, v in CHARACTER_LOCKS.items() if v != request.client_id]
    return {"locked_characters": locked_by_others}


@app.post("/switch_character")
async def switch_character_endpoint(request: SwitchRequest):
    if request.old_character in CHARACTER_LOCKS and CHARACTER_LOCKS[request.old_character] == request.client_id:
        del CHARACTER_LOCKS[request.old_character]

    if request.new_character in CHARACTER_LOCKS and CHARACTER_LOCKS[request.new_character] != request.client_id:
        raise HTTPException(
            status_code=403, detail=f"Character '{request.new_character}' is currently controlled by another player."
        )

    CHARACTER_LOCKS[request.new_character] = request.client_id
    return {"status": "success"}


@app.get("/listen")
async def listen_endpoint(client_id: str, request: Request):
    q = asyncio.Queue()
    broadcaster.add_queue(client_id, q)

    async def event_generator():
        try:
            while True:
                getter = asyncio.create_task(q.get())
                disconnect = asyncio.create_task(request.is_disconnected())
                done, pending = await asyncio.wait([getter, disconnect], return_when=asyncio.FIRST_COMPLETED)

                if disconnect in done and disconnect.result():
                    getter.cancel()
                    break

                if getter in done:
                    disconnect.cancel()
                    yield getter.result()
        finally:
            broadcaster.remove_queue(client_id, q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/chat")
async def chat_endpoint(request: ChatRequest):  # noqa: C901
    if not dm_engine_app:
        raise HTTPException(status_code=500, detail="DM Engine not initialized.")

    # MULTIPLAYER LOCK ENFORCEMENT
    if request.character in CHARACTER_LOCKS and CHARACTER_LOCKS[request.character] != request.client_id:
        raise HTTPException(
            status_code=403,
            detail=(f"Agency Error: '{request.character}' is currently " "being controlled by another player's connection."),
        )
    CHARACTER_LOCKS[request.character] = request.client_id

    if request.character != "Human DM":
        update_roll_automations(request.character, request.roll_automations)

    if request.message.strip().startswith(">") and request.character.lower() != "human dm":
        raise HTTPException(status_code=403, detail="Only the Human DM may use OOC commands (>).")

    clean_msg = request.message[1:].strip() if request.message.startswith(">") else request.message
    if request.character.lower() == "human dm":
        prefix = "[OOC Override from Human DM]:"
    else:
        prefix = f"[{request.character} acts/speaks]:"
    formatted_prompt = f"{prefix} {clean_msg}"

    print(f"\nInitiating Supervisor Loop for: {formatted_prompt}")
    await write_audit_log(request.vault_path, request.character, "Input Received", clean_msg)

    # Broadcast the user's action to listeners
    await broadcaster.broadcast(
        request.client_id,
        f"data: {json.dumps({'reply': f'**[{request.character}]**: {clean_msg}\\n\\n', 'status': 'streaming'})}\n\n",
    )

    initial_state = {
        "messages": [HumanMessage(content=formatted_prompt)],
        "vault_path": request.vault_path,
        "active_character": request.character,
        "draft_response": "",
        "qa_feedback": "",
        "revision_count": 0,
    }

    config = {"configurable": {"thread_id": request.vault_path}}

    async def stream_generator():
        try:
            # 1. Initialize Deterministic Engine
            await initialize_engine_from_vault(request.vault_path)

            # Yield an initial status
            yield f"data: {json.dumps({'reply': '*(Thinking...)*\n\n', 'status': 'streaming'})}\n\n"

            # 2. Run the graph with astream_events to catch token-by-token streams
            async for event in dm_engine_app.astream_events(initial_state, config=config, version="v2"):
                kind = event["event"]
                node_name = event.get("metadata", {}).get("langgraph_node")

                # A. Stream Narrator Tokens Live
                if kind == "on_chat_model_stream" and node_name == "narrator":
                    chunk = event["data"]["chunk"].content
                    if chunk:
                        payload = f"data: {json.dumps({'reply': chunk, 'status': 'streaming'})}\n\n"
                        yield payload
                        await broadcaster.broadcast(request.client_id, payload)

                # B. Expose Engine Tools so players see the math happening
                elif kind == "on_tool_start":
                    tool_name = event.get("name", "tool")
                    if tool_name not in ["ChatGoogleGenerativeAI", "planner_node"]:
                        msg = f"\n> *(Engine: Executing {tool_name}...)*\n"
                        payload = f"data: {json.dumps({'reply': msg, 'status': 'streaming'})}\n\n"
                        yield payload
                        await broadcaster.broadcast(request.client_id, payload)

                # C. Intercept QA Rejections
                elif kind == "on_chain_end" and node_name == "qa":
                    qa_out = event["data"].get("output", {})
                    if isinstance(qa_out, dict):
                        feedback = qa_out.get("qa_feedback", "")
                        # If QA asked a clarifying question, stream it out
                        if "draft_response" in qa_out:
                            intercept_msg = qa_out.get("draft_response")
                            payload = f"data: {json.dumps({'reply': f'\n\n{intercept_msg}', 'status': 'streaming'})}\n\n"
                            yield payload
                            await broadcaster.broadcast(request.client_id, payload)
                        # If QA rejected and forced a rewrite
                        elif feedback and feedback != "APPROVED":
                            msg = "\n\n> *(QA Intercept: Correcting mechanical discrepancy. Rewriting...)*\n\n"
                            payload = f"data: {json.dumps({'reply': msg, 'status': 'streaming'})}\n\n"
                            yield payload
                            await broadcaster.broadcast(request.client_id, payload)

            # 3. Save combat math back to Obsidian files
            await sync_engine_to_vault(request.vault_path)
            payload = f"data: {json.dumps({'reply': '', 'status': 'done'})}\n\n"
            yield payload
            await broadcaster.broadcast(request.client_id, payload)

        except Exception as e:
            print("\n" + "=" * 40 + "\n💥 FATAL ERROR DURING SUPERVISOR EXECUTION 💥\n" + "=" * 40)
            traceback.print_exc()
            print("=" * 40 + "\n")
            payload = f"data: {json.dumps({'reply': f'\n\n**System Error:** {str(e)}', 'status': 'error'})}\n\n"
            yield payload
            await broadcaster.broadcast(request.client_id, payload)

    # Return the SSE stream bypassing the standard Pydantic model response
    return StreamingResponse(stream_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
