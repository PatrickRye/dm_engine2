import os
import traceback
import json
import asyncio
import yaml
import aiofiles
import time
import socket
import psutil
import subprocess
import math
from typing import List, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from filelock import FileLock

from dotenv import dotenv_values

# Load non-default env variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(env_path):
    defaults = {"your_gemini_api_key_here", "github_pat_your_token_here", "owner/repo_name"}
    for k, v in dotenv_values(env_path).items():
        if v and v not in defaults:
            os.environ[k] = v

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_LOCK_FILE = os.path.join(BASE_DIR, "repo_operation.lock")

from pydantic import BaseModel, Field
import uvicorn
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

try:
    import sqlite3 as _sqlite3
    import aiosqlite as _aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver as _AsyncSqliteSaver

    _SQLITE_AVAILABLE = True
    _ASYNC_SQLITE_AVAILABLE = True
except ImportError:
    try:
        import sqlite3 as _sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver
        _SQLITE_AVAILABLE = True
        _ASYNC_SQLITE_AVAILABLE = False
    except ImportError:
        _SQLITE_AVAILABLE = False
        _ASYNC_SQLITE_AVAILABLE = False
from shapely.geometry import LineString
from dnd_rules_engine import Creature
from state import DMState
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
    manage_mount,
    ingest_battlemap_json,
    evaluate_extreme_weather,
    manage_map_terrain,
    update_roll_automations,
    use_font_of_magic,
    spawn_summon,
    refresh_vault_data,
    report_rule_challenge,
    create_storylet,
    list_active_storylets,
    mark_entity_immutable,
    request_graph_mutations,
    sync_knowledge_graph,
    run_ingestion_pipeline_tool,
    hydrate_campaign,
    hydrate_delta,
    detect_missing_entities,
    propose_entity_creation,
    generate_side_quests_for_entity,
    reveal_secret,
    get_scene_provenance,
    set_storylet_deadline,
    propose_backstory_claim,
    review_backstory_claims,
    approve_backstory_claim,
    _get_config_tone,
    _get_entity_by_name,
    _calculate_reach,
)
from system_logger import logger, qa_logger
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
    manage_mount,
    use_font_of_magic,
    ingest_battlemap_json,
    evaluate_extreme_weather,
    manage_map_terrain,
    spawn_summon,
    refresh_vault_data,
    report_rule_challenge,
    create_storylet,
    list_active_storylets,
    mark_entity_immutable,
    request_graph_mutations,
    sync_knowledge_graph,
    run_ingestion_pipeline_tool,
    hydrate_campaign,
    hydrate_delta,
    detect_missing_entities,
    propose_entity_creation,
    generate_side_quests_for_entity,
    reveal_secret,
    get_scene_provenance,
    set_storylet_deadline,
    propose_backstory_claim,
    review_backstory_claims,
    approve_backstory_claim,
]

# 1. INITIALIZE THE APP FIRST
# This is handled by the lifespan manager further down

# GLOBAL STATE
dm_engine_app = None
draft_llm = None
qa_llm = None

# CONCURRENCY LOCKS: Maps vault_path -> asyncio.Lock
VAULT_LOCKS = {}

# MULTIPLAYER LOCKS: Maps character_name -> client_id
CHARACTER_LOCKS = {}
LAST_SEEN = {}  # client_id -> timestamp
LAST_MESSAGE_TIME = {}  # character_name -> timestamp
ACTIVE_TYPERS = {}  # character_name -> timestamp (for timeout-based expiry)
CHARACTER_LOCK_MUTEX = asyncio.Lock()  # Serializes mutations to CHARACTER_LOCKS / LAST_SEEN

# Server discovery state
ACTIVE_VAULT_PATH = ""  # Set when DM loads a vault
ACTIVE_CAMPAIGN_NAME = "No Campaign Loaded"  # Display name for clients
SERVER_NAME = "DM Engine"  # Configurable server display name

# State change notification system for real-time client updates
STATE_CHANGES: List[Dict[str, Any]] = []
STATE_CHANGES_LOCK = asyncio.Lock()


def push_state_change(change_type: str, entity_name: str, changes: Dict[str, Any]):
    """Push a state change notification for clients. Thread-safe."""
    global STATE_CHANGES
    STATE_CHANGES.append({
        "type": change_type,
        "entity": entity_name,
        "changes": changes,
        "timestamp": time.time(),
    })
    # Keep only recent changes (last 100)
    if len(STATE_CHANGES) > 100:
        STATE_CHANGES = STATE_CHANGES[-100:]


async def get_and_clear_state_changes() -> List[Dict[str, Any]]:
    """Get all pending state changes and clear the queue. For use by heartbeat."""
    global STATE_CHANGES
    async with STATE_CHANGES_LOCK:
        changes = list(STATE_CHANGES)
        STATE_CHANGES = []
    return changes


class EventBroadcaster:
    def __init__(self):
        self.queues = []

    def add_queue(self, client_id: str, q: asyncio.Queue):
        self.queues.append((client_id, q))

    def remove_queue(self, client_id: str, q: asyncio.Queue):
        if (client_id, q) in self.queues:
            self.queues.remove((client_id, q))

    async def broadcast(self, sender_id: str, data: str, target_client_ids: list[str] = None):
        for cid, q in list(self.queues):
            if cid != sender_id:
                if target_client_ids is None or cid in target_client_ids:
                    try:
                        await q.put(data)
                    except Exception:
                        pass


broadcaster = EventBroadcaster()


# Graph nodes and build_graph() live in graph.py — imported below.
from graph import build_graph  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dm_engine_app, draft_llm, qa_llm
    heartbeat_task = None
    qa_process = None
    _checkpoint_conn = None

    def is_qa_running():
        for p in psutil.process_iter(["cmdline"]):
            try:
                cmdline = p.info.get("cmdline") or []
                if any("bug_reporters.py" in cmd for cmd in cmdline):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return False

    if not is_qa_running():
        qa_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qa", "bug_reporters.py")
        print(f"Starting QA Reporters at {qa_path}...")
        qa_process = subprocess.Popen(["python", qa_path])

    # Get model from env or default to Gemini-2.5-flash (higher rate limits than Pro)
    _model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    try:
        # Using Flash for better rate limits — Pro exhausts quickly
        draft_llm = ChatGoogleGenerativeAI(model=_model, temperature=0.6)
        qa_llm = ChatGoogleGenerativeAI(model=_model, temperature=0.1)

        # Build checkpointer: prefer AsyncSqliteSaver (persistent across restarts) over in-memory
        if _ASYNC_SQLITE_AVAILABLE:
            db_path = os.path.join(BASE_DIR, "checkpoints.db")
            try:
                _checkpoint_conn = await asyncio.wait_for(_aiosqlite.connect(db_path), timeout=5.0)
                checkpointer = _AsyncSqliteSaver(_checkpoint_conn)
                print(f"Using AsyncSqliteSaver checkpoint store at {db_path}")
            except asyncio.TimeoutError:
                print(f"AsyncSqliteSaver connection timed out, falling back to MemorySaver")
                checkpointer = MemorySaver()
        elif _SQLITE_AVAILABLE:
            db_path = os.path.join(BASE_DIR, "checkpoints.db")
            _checkpoint_conn = _sqlite3.connect(db_path, check_same_thread=False)
            checkpointer = _SqliteSaver(_checkpoint_conn)
            print(f"Using SqliteSaver checkpoint store at {db_path}")
        else:
            checkpointer = MemorySaver()
            print(
                "WARNING: langgraph-checkpoint-sqlite not installed; using in-memory MemorySaver. "
                "Install with: pip install langgraph-checkpoint-sqlite"
            )

        # Build and compile the multi-agent graph ONCE when the server boots
        dm_engine_app = build_graph(draft_llm, qa_llm, MASTER_TOOLS_LIST, checkpointer=checkpointer)

        async def heartbeat_emitter():
            # UDP socket for local monitoring (127.0.0.1:9999)
            local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            local_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # UDP socket for LAN broadcast (255.255.255.255:9998)
            broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            p = psutil.Process(os.getpid())
            while True:
                try:
                    # Local monitoring heartbeat
                    local_payload = json.dumps(
                        {
                            "pid": p.pid,
                            "cpu_percent": p.cpu_percent(),
                            "mem_mb": p.memory_info().rss / (1024 * 1024),
                            "timestamp": time.time(),
                        }
                    ).encode("utf-8")
                    local_sock.sendto(local_payload, ("127.0.0.1", 9999))
                except Exception:
                    pass
                try:
                    # LAN broadcast for server discovery
                    global ACTIVE_VAULT_PATH, ACTIVE_CAMPAIGN_NAME, SERVER_NAME
                    broadcast_payload = json.dumps(
                        {
                            "server_name": SERVER_NAME,
                            "campaign": ACTIVE_CAMPAIGN_NAME,
                            "vault_path": ACTIVE_VAULT_PATH,
                            "port": 8000,
                            "timestamp": time.time(),
                        }
                    ).encode("utf-8")
                    broadcast_sock.sendto(broadcast_payload, ("255.255.255.255", 9998))
                except Exception:
                    pass
                await asyncio.sleep(2)

        heartbeat_task = asyncio.create_task(heartbeat_emitter())
        print("DM Engine initialized successfully with ReAct architecture.")
        yield
    except Exception as e:
        print(f"Failed to initialize DM Engine: {e}")
        yield
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
        if qa_process:
            qa_process.terminate()
        if _checkpoint_conn is not None:
            _checkpoint_conn.close()


# --- 4. YOUR FAST API APP ---
app = FastAPI(title="AI DM Engine", lifespan=lifespan)

# ADD CORS MIDDLEWARE (Crucial for Obsidian to connect!)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# EXCEPTION HANDLERS
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(
        "Bad payload received from frontend.",
        extra={"agent_id": "SYSTEM_API", "context": {"errors": exc.errors(), "body": exc.body, "url": str(request.url)}},
    )
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": exc.body},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled server exception.", extra={"agent_id": "SYSTEM_API", "context": {"url": str(request.url)}})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error. Check the backend console."},
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    logger.debug(
        f"Incoming Request: {request.method} {request.url}",
        extra={"agent_id": "SYSTEM_API", "context": {"method": request.method, "url": str(request.url)}},
    )

    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        logger.debug(
            f"Response: {response.status_code}",
            extra={
                "agent_id": "SYSTEM_API",
                "context": {"status_code": response.status_code, "process_time_s": round(process_time, 2)},
            },
        )
        return response
    except Exception as e:
        print(f"<--- Server Error: {str(e)}")
        raise


class ChatRequest(BaseModel):
    message: str
    character: str
    vault_path: str = ""
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


class ToggleLivePatchRequest(BaseModel):
    client_id: str
    character: str
    enabled: bool


class TypingRequest(BaseModel):
    client_id: str
    character: str
    is_typing: bool


class PartyStatusRequest(BaseModel):
    vault_path: str


@app.post("/toggle_live_patch")
async def toggle_live_patch_endpoint(request: ToggleLivePatchRequest):
    if request.character != "Human DM":
        raise HTTPException(status_code=403, detail="Only the DM can toggle Live Patch Mode.")

    mode_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".live_patch_mode")
    if request.enabled:
        with open(mode_file, "w") as f:
            f.write("true")
    else:
        if os.path.exists(mode_file):
            os.remove(mode_file)
    return {"status": "success", "live_patch_mode": request.enabled}


@app.get("/check_updates")
async def check_updates_endpoint():
    """Silently checks if the GitHub repository has updates the local machine lacks."""
    try:
        subprocess.run(["git", "fetch", "origin", "main"], cwd=BASE_DIR, capture_output=True, timeout=10)
        local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE_DIR, capture_output=True, text=True).stdout.strip()
        remote = subprocess.run(
            ["git", "rev-parse", "origin/main"], cwd=BASE_DIR, capture_output=True, text=True
        ).stdout.strip()
        return {"update_available": local != remote and bool(remote)}
    except Exception as e:
        return {"update_available": False, "error": str(e)}


@app.post("/apply_update")
async def apply_update_endpoint():
    """Acquires the repo lock, pulls changes, and relies on Uvicorn to hot-reload."""
    lock = FileLock(REPO_LOCK_FILE, timeout=60)
    try:
        with lock:
            res = subprocess.run(["git", "pull", "origin", "main"], cwd=BASE_DIR, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                return {"status": "success", "message": "Update pulled. Server is hot-reloading."}
            else:
                return {"status": "error", "message": res.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ooc_move_entity")
async def ooc_move_entity_endpoint(request: OOCMoveRequest):
    """OOC wrapper for the DM to drag and drop tokens without triggering combat rules."""
    vault_lock = VAULT_LOCKS.setdefault(request.vault_path, asyncio.Lock())
    async with vault_lock:
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


@app.post("/sync_vault")
async def sync_vault_endpoint(request: VaultRequest):
    """Explicit API endpoint for the front-end to trigger hot-reloading."""
    from vault_io import sync_engine_from_vault_updates

    res = await sync_engine_from_vault_updates(request.vault_path)
    return {"status": "success", "message": res}


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


@app.post("/typing")
async def typing_endpoint(request: TypingRequest):
    """Broadcasts a typing indicator to all connected clients."""
    global ACTIVE_TYPERS
    current_time = time.time()
    if request.is_typing:
        ACTIVE_TYPERS[request.character] = current_time
    elif request.character in ACTIVE_TYPERS:
        del ACTIVE_TYPERS[request.character]
    payload = {"type": "typing", "character": request.character, "is_typing": request.is_typing}
    await broadcaster.broadcast(request.client_id, f"data: {json.dumps(payload)}\n\n")
    return {"status": "success"}


@app.post("/party_status")
async def party_status_endpoint(request: PartyStatusRequest):
    """Returns the health, location, and presence status of all party members."""
    party_members = []
    current_time = time.time()

    entities = get_all_entities(request.vault_path)
    for uid, ent in entities.items():
        tags = [t.lower() for t in getattr(ent, "tags", [])]
        if any(t in tags for t in ["pc", "player", "party_npc"]):
            char_name = ent.name

            # Check online status (Heartbeat in last 15 seconds)
            client_id = CHARACTER_LOCKS.get(char_name)
            is_online = False
            if client_id and client_id in LAST_SEEN:
                is_online = (current_time - LAST_SEEN[client_id]) <= 15

            # Check active status (Message sent in last 5 mins / 300 seconds)
            last_msg_ts = LAST_MESSAGE_TIME.get(char_name, 0)
            is_active = (current_time - last_msg_ts) <= 300

            hp = ent.hp.base_value if hasattr(ent, "hp") and hasattr(ent.hp, "base_value") else 0

            party_members.append(
                {
                    "name": char_name,
                    "hp": hp,
                    "max_hp": getattr(ent, "max_hp", 0),
                    "current_map": getattr(ent, "current_map", "Unknown Location"),
                    "is_online": is_online,
                    "is_active": is_active,
                }
            )

    return {"party": party_members}


@app.post("/propose_move", response_model=ProposeMoveResponse)
async def propose_move_endpoint(request: ProposeMoveRequest):  # noqa: C901
    """Analyzes a proposed movement path for collisions, opportunity attacks, and traps."""

    vault_lock = VAULT_LOCKS.setdefault(request.vault_path, asyncio.Lock())

    async with vault_lock:
        entity = await _get_entity_by_name(request.entity_name, request.vault_path)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        from shapely.geometry import box

        pixels_per_foot = getattr(spatial_service.get_map_data(request.vault_path), "pixels_per_foot", 1.0)
        foot_waypoints = [(p[0] / pixels_per_foot, p[1] / pixels_per_foot) for p in request.waypoints]

        # REQ-GEO-006: Check if final waypoint is occupied
        final_wp = foot_waypoints[-1]
        final_poly = box(
            final_wp[0] - entity.size / 2,
            final_wp[1] - entity.size / 2,
            final_wp[0] + entity.size / 2,
            final_wp[1] + entity.size / 2,
        )
        for other_entity in get_all_entities(request.vault_path).values():
            if other_entity.entity_uuid == entity.entity_uuid:
                continue
            if not hasattr(other_entity, "hp") or getattr(other_entity.hp, "base_value", 0) <= 0:
                continue
            o_poly = box(
                other_entity.x - other_entity.size / 2,
                other_entity.y - other_entity.size / 2,
                other_entity.x + other_entity.size / 2,
                other_entity.y + other_entity.size / 2,
            )
            if final_poly.buffer(-0.1).intersects(o_poly.buffer(-0.1)):
                return ProposeMoveResponse(
                    is_valid=False,
                    invalid_reason=f"Cannot end movement in a space occupied by {other_entity.name}.",
                    executed=False,
                )

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

        # Discretize path into <= 5ft chunks for precise trigger detection
        detailed_path = [foot_waypoints[0]]
        for i in range(1, len(foot_waypoints)):
            start = detailed_path[-1]
            end = foot_waypoints[i]
            dist = spatial_service.calculate_distance(
                start[0], start[1], entity.z, end[0], end[1], entity.z, request.vault_path
            )
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

            # REQ-GEO-007, REQ-GEO-008: Moving through creatures
            # Use rtree spatial index to narrow candidates before detailed checks
            path_candidates = spatial_service.get_entities_on_path(
                start[0],
                start[1],
                end[0],
                end[1],
                vault_path=request.vault_path,
                exclude_uuid=entity.entity_uuid,
            )
            for other_entity, overlap_len in path_candidates:
                if not hasattr(other_entity, "hp") or getattr(other_entity.hp, "base_value", 0) <= 0:
                    continue
                is_entity_pc = any(t in entity.tags for t in ["pc", "player", "party_npc"])
                is_other_pc = any(t in other_entity.tags for t in ["pc", "player", "party_npc"])

                is_other_incapacitated = any(
                    c.name.lower() in ["incapacitated", "unconscious", "stunned", "paralyzed", "petrified", "dead"]
                    for c in getattr(other_entity, "active_conditions", [])
                )
                is_other_tiny = "tiny" in [t.lower() for t in getattr(other_entity, "tags", [])] or other_entity.size <= 2.5

                def size_cat(size: float, tags: list):
                    tags_lower = [t.lower() for t in tags]
                    if "tiny" in tags_lower:
                        return 1
                    if "small" in tags_lower:
                        return 2
                    if "large" in tags_lower:
                        return 4
                    if "huge" in tags_lower:
                        return 5
                    if "gargantuan" in tags_lower:
                        return 6
                    if size <= 2.5:
                        return 1
                    if size <= 5.0:
                        return 3  # Medium
                    if size <= 10.0:
                        return 4  # Large
                    if size <= 15.0:
                        return 5  # Huge
                    return 6

                cat_e = size_cat(entity.size, getattr(entity, "tags", []))
                cat_o = size_cat(other_entity.size, getattr(other_entity, "tags", []))

                # REQ-MOV-012: Tiny/Incapacitated allow passage. Otherwise check size diff.
                if is_entity_pc != is_other_pc and abs(cat_e - cat_o) < 2 and not is_other_incapacitated and not is_other_tiny:
                    is_valid = False
                    invalid_reason = f"Cannot move through hostile creature {other_entity.name} (Size difference too small)."
                    break
                else:
                    segment_cost += segment_dist * (overlap_len / path_line.length) if path_line.length > 0 else 0

            if not is_valid and not request.force_execute:
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

                    if any(
                        c.name.lower() in ["incapacitated", "unconscious", "stunned", "paralyzed", "petrified", "dead"]
                        for c in getattr(other_entity, "active_conditions", [])
                    ):
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

        # Build the execution plan inside the lock; run the actual tool calls outside it.
        points_to_execute = []
        exec_config = None
        if is_valid and len(executed_waypoints) > 1:
            if request.force_execute or (not opportunity_attacks and not traps_triggered):
                exec_config = {"configurable": {"thread_id": request.vault_path}}
                for fw in foot_waypoints[1:]:
                    if fw in executed_waypoints:
                        points_to_execute.append(fw)
                if executed_waypoints[-1] not in points_to_execute:
                    points_to_execute.append(executed_waypoints[-1])

    # --- vault_lock released here ---
    # Execute move_entity outside the lock so the lock is not held during LLM/tool I/O.
    executed = False
    if points_to_execute and exec_config:
        for point in points_to_execute:
            await move_entity.ainvoke(
                {
                    "entity_name": request.entity_name,
                    "target_x": round(point[0], 2),
                    "target_y": round(point[1], 2),
                    "movement_type": "walk",
                },
                config=exec_config,
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
    # Use provided vault_path, or fall back to server's active vault
    vault_path = request.vault_path or ACTIVE_VAULT_PATH
    if not vault_path:
        return {"characters": ["Human DM"], "error": "No vault loaded on server"}
    j_dir = get_journals_dir(vault_path)
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
                                if any(t.lower() in ["pc", "player", "party_npc"] for t in tags):
                                    chars.append(yaml_data.get("name", filename.replace(".md", "")))
                except Exception:
                    pass
    return {"characters": list(set(chars))}


class CharSheetRequest(BaseModel):
    vault_path: str
    character: str


@app.post("/character_sheet")
async def character_sheet_endpoint(request: CharSheetRequest):
    """Fetches the parsed YAML data for the active character, merged with live engine state."""
    if request.character == "Human DM":
        return {
            "sheet": {
                "name": "Human DM",
                "role": "Dungeon Master",
                "hp": "—",
                "max_hp": "—",
                "ac": "—",
                "conditions": [],
                "speed": "—",
                "abilities": {},
            }
        }

    file_path = os.path.join(get_journals_dir(request.vault_path), f"{request.character}.md")
    if not os.path.exists(file_path):
        return {"error": f"Character file {request.character}.md not found."}

    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()
            sheet = {}
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    sheet = await asyncio.to_thread(yaml.safe_load, parts[1]) or {}
    except Exception as e:
        return {"error": str(e)}

    # Merge live engine state (HP, conditions, etc.) into sheet data
    vault_path = request.vault_path or ACTIVE_VAULT_PATH
    if vault_path:
        entity = await _get_entity_by_name(request.character, vault_path)
        if entity:
            # Override static YAML data with live engine state
            sheet["hp"] = entity.hp.base_value if hasattr(entity, "hp") else sheet.get("hp", 0)
            sheet["max_hp"] = entity.max_hp if hasattr(entity, "max_hp") else sheet.get("max_hp", 0)
            sheet["ac"] = getattr(entity, "ac", sheet.get("ac", 10))
            sheet["speed"] = getattr(entity, "speed", sheet.get("speed", "30 ft"))

            # Merge conditions
            engine_conds = [c.name for c in getattr(entity, "active_conditions", [])]
            yaml_conds = sheet.get("conditions", [])
            if isinstance(yaml_conds, str):
                yaml_conds = [c.strip() for c in yaml_conds.split(",")]
            # Combine, remove duplicates, prefer engine state
            all_conds = list(dict.fromkeys(yaml_conds + engine_conds))
            sheet["conditions"] = all_conds

            # Ability scores from engine if available
            if hasattr(entity, "ability_scores"):
                sheet["abilities"] = {
                    "str": getattr(entity.ability_scores, "strength", 10),
                    "dex": getattr(entity.ability_scores, "dexterity", 10),
                    "con": getattr(entity.ability_scores, "constitution", 10),
                    "int": getattr(entity.ability_scores, "intelligence", 10),
                    "wis": getattr(entity.ability_scores, "wisdom", 10),
                    "cha": getattr(entity.ability_scores, "charisma", 10),
                }

    return {"sheet": sheet}


@app.get("/vault_media")
async def vault_media_endpoint(filepath: str, vault_path: str = ""):
    """Securely serves local image files from the Obsidian vault to the web client."""
    # Resolve to absolute path and reject traversal attempts
    resolved = os.path.realpath(os.path.normpath(filepath))
    if vault_path:
        vault_root = os.path.realpath(vault_path)
        if not resolved.startswith(vault_root + os.sep) and resolved != vault_root:
            raise HTTPException(status_code=403, detail="Access denied: path outside vault")
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved)


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

            is_invisible = "invisible" in getattr(ent, "tags", []) or any(
                c.name.lower() == "invisible" for c in getattr(ent, "active_conditions", [])
            )
            is_hidden = any(c.name.lower() == "hidden" for c in getattr(ent, "active_conditions", []))

            entities.append(
                {
                    "name": ent.name,
                    "x": ent.x,
                    "y": ent.y,
                    "size": ent.size,
                    "is_pc": is_pc,
                    "hp": ent.hp.base_value if hasattr(ent, "hp") else 0,
                    "icon_url": icon_path,
                    "is_invisible": is_invisible,
                    "is_hidden": is_hidden,
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


class VisibilityRequest(BaseModel):
    character: str  # The character whose perspective we're checking
    target_x: float
    target_y: float
    vault_path: str = ""


@app.post("/visibility")
async def visibility_endpoint(request: VisibilityRequest):
    """Checks if a point is visible to a character, considering walls and fog of war."""
    vault_path = request.vault_path or ACTIVE_VAULT_PATH
    if not vault_path:
        return {"visible": False, "reason": "no_vault"}

    # Get the character's entity
    entity = await _get_entity_by_name(request.character, vault_path)
    if not entity:
        return {"visible": False, "reason": "character_not_found"}

    # Check line of sight through walls
    has_los = spatial_service.has_line_of_sight_to_point(
        entity.entity_uuid, request.target_x, request.target_y, 0.0, vault_path
    )

    # Check if in an explored area
    md = spatial_service.get_map_data(vault_path)
    in_explored = False
    for area in md.explored_areas:
        ax, ay, radius = area
        dist = math.hypot(request.target_x - ax, request.target_y - ay)
        if dist <= radius:
            in_explored = True
            break

    # For players (not DM), both LOS and explored area are required
    is_dm = request.character.lower() == "human dm"
    visible = has_los and (is_dm or in_explored)

    return {
        "visible": visible,
        "has_line_of_sight": has_los,
        "in_explored_area": in_explored,
        "is_dm": is_dm,
    }


@app.post("/heartbeat")
async def heartbeat_endpoint(request: HeartbeatRequest):
    async with CHARACTER_LOCK_MUTEX:
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

    # Build party info for connecting clients
    party_info = []
    current_time = time.time()

    # Purge stale typers (typing indicator expires after 10 seconds)
    stale_typers = [c for c, ts in ACTIVE_TYPERS.items() if current_time - ts > 10]
    for c in stale_typers:
        del ACTIVE_TYPERS[c]

    if ACTIVE_VAULT_PATH:
        entities = get_all_entities(ACTIVE_VAULT_PATH)
        for uid, ent in entities.items():
            tags = [t.lower() for t in getattr(ent, "tags", [])]
            if any(t in tags for t in ["pc", "player", "party_npc"]):
                char_name = ent.name
                client_id = CHARACTER_LOCKS.get(char_name)
                is_online = client_id and client_id in LAST_SEEN and (current_time - LAST_SEEN[client_id]) <= 15
                last_msg_ts = LAST_MESSAGE_TIME.get(char_name, 0)
                is_active = (current_time - last_msg_ts) <= 300
                is_typing = char_name in ACTIVE_TYPERS

                party_info.append({
                    "name": char_name,
                    "hp": getattr(ent, "hp", 0) if hasattr(ent, "hp") else 0,
                    "max_hp": getattr(ent, "max_hp", 0),
                    "current_map": getattr(ent, "current_map", "Unknown Location"),
                    "is_online": is_online,
                    "is_active": is_active,
                    "is_typing": is_typing,
                    "is_locked": char_name in CHARACTER_LOCKS and CHARACTER_LOCKS[char_name] != request.client_id,
                    "locked_by_other": char_name in locked_by_others,
                })

    # Get pending state changes (HP updates, condition changes, etc.)
    state_changes = await get_and_clear_state_changes()

    return {
        "locked_characters": locked_by_others,
        "server_name": SERVER_NAME,
        "campaign": ACTIVE_CAMPAIGN_NAME,
        "party": party_info,
        "state_changes": state_changes,
    }


@app.post("/switch_character")
async def switch_character_endpoint(request: SwitchRequest):
    async with CHARACTER_LOCK_MUTEX:
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

    # Use server's active vault if player didn't specify one
    if not request.vault_path:
        request.vault_path = ACTIVE_VAULT_PATH
    if not request.vault_path:
        raise HTTPException(status_code=400, detail="No vault loaded. DM must load a vault first.")

    # MULTIPLAYER LOCK ENFORCEMENT
    async with CHARACTER_LOCK_MUTEX:
        if request.character in CHARACTER_LOCKS and CHARACTER_LOCKS[request.character] != request.client_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Agency Error: '{request.character}' is currently " "being controlled by another player's connection."
                ),
            )
        CHARACTER_LOCKS[request.character] = request.client_id

    if request.character != "Human DM":
        update_roll_automations(request.character, request.roll_automations)

    if request.message.strip().startswith(">") and request.character.lower() != "human dm":
        raise HTTPException(status_code=403, detail="Only the Human DM may use OOC commands (>).")

    # Track activity for the "Active" indicator
    LAST_MESSAGE_TIME[request.character] = time.time()

    clean_msg = request.message[1:].strip() if request.message.startswith(">") else request.message
    if request.character.lower() == "human dm":
        prefix = "[OOC Override from Human DM]:"
    else:
        prefix = f"[{request.character} acts/speaks]:"
    formatted_prompt = f"{prefix} {clean_msg}"

    # WHISPER INTERCEPT
    is_whisper = False
    target_ent = None
    target_name = ""
    whisper_text = ""

    if clean_msg.lower().startswith("/w ") or clean_msg.lower().startswith("/whisper "):
        is_whisper = True
        cmd_len = 3 if clean_msg.lower().startswith("/w ") else 9
        content_after = clean_msg[cmd_len:].strip()

        all_ents = get_all_entities(request.vault_path)
        # Find PC targets sorted by length to match the longest full names first
        possible_names = sorted(
            [
                e.name
                for e in all_ents.values()
                if any(t.lower() in ["pc", "player", "party_npc"] for t in getattr(e, "tags", []))
            ],
            key=len,
            reverse=True,
        )
        for name in possible_names:
            if content_after.lower().startswith(name.lower()):
                # Ensure clean name break (handles cases like John matching Johnathon incorrectly)
                if len(content_after) == len(name) or content_after[len(name)] == " ":
                    target_ent = await _get_entity_by_name(name, request.vault_path)
                    whisper_text = content_after[len(name) :].strip()
                    break
        if content_after.lower().startswith("dm ") or content_after.lower() == "dm":
            target_name = "DM"
            whisper_text = content_after[3:].strip() if content_after.lower().startswith("dm ") else ""
        else:
            all_ents = get_all_entities(request.vault_path)
            # Find PC targets sorted by length to match the longest full names first
            possible_names = sorted(
                [
                    e.name
                    for e in all_ents.values()
                    if any(t.lower() in ["pc", "player", "party_npc"] for t in getattr(e, "tags", []))
                ],
                key=len,
                reverse=True,
            )
            for name in possible_names:
                if content_after.lower().startswith(name.lower()):
                    # Ensure clean name break (handles cases like John matching Johnathon incorrectly)
                    if len(content_after) == len(name) or content_after[len(name)] == " ":
                        target_ent = await _get_entity_by_name(name, request.vault_path)
                        target_name = target_ent.name if target_ent else name
                        whisper_text = content_after[len(name) :].strip()
                        break

    if is_whisper:

        async def whisper_generator():
            if not target_ent or not target_name:
                yield f"data: {json.dumps({'reply': '\\n\\n**System Error:** Could not find a valid player target for whisper.', 'status': 'error'})}\n\n"
                return

            if not whisper_text:
                yield f"data: {json.dumps({'reply': '\\n\\n**System Error:** Whisper message cannot be empty.', 'status': 'error'})}\n\n"
                return

            source_ent = await _get_entity_by_name(request.character, request.vault_path)
            if not source_ent and request.character != "Human DM":
                yield f"data: {json.dumps({'reply': '\\n\\n**System Error:** Source character not found.', 'status': 'error'})}\n\n"
                return

            if request.character != "Human DM":
                if getattr(source_ent, "current_map", "") != getattr(target_ent, "current_map", ""):
                    yield f"data: {json.dumps({'reply': f'\\n\\n**System Error:** {target_ent.name} is not in the same location.', 'status': 'error'})}\n\n"
            if target_name != "DM":
                source_ent = await _get_entity_by_name(request.character, request.vault_path)
                if not source_ent and request.character != "Human DM":
                    yield f"data: {json.dumps({'reply': '\\n\\n**System Error:** Source character not found.', 'status': 'error'})}\n\n"
                    return

                # Enforce standard 120ft telepathy/message range
                dist = spatial_service.calculate_distance(
                    getattr(source_ent, "x", 0.0),
                    getattr(source_ent, "y", 0.0),
                    getattr(source_ent, "z", 0.0),
                    getattr(target_ent, "x", 0.0),
                    getattr(target_ent, "y", 0.0),
                    getattr(target_ent, "z", 0.0),
                    request.vault_path,
                )
                if dist > 120.0:
                    yield f"data: {json.dumps({'reply': f'\\n\\n**System Error:** {target_ent.name} is too far away to whisper ({dist:.1f}ft > 120ft).', 'status': 'error'})}\n\n"
                    return
                if request.character != "Human DM":
                    if getattr(source_ent, "current_map", "") != getattr(target_ent, "current_map", ""):
                        yield f"data: {json.dumps({'reply': f'\\n\\n**System Error:** {target_name} is not in the same location.', 'status': 'error'})}\n\n"
                        return

                    # Enforce standard 120ft telepathy/message range
                    dist = spatial_service.calculate_distance(
                        getattr(source_ent, "x", 0.0),
                        getattr(source_ent, "y", 0.0),
                        getattr(source_ent, "z", 0.0),
                        getattr(target_ent, "x", 0.0),
                        getattr(target_ent, "y", 0.0),
                        getattr(target_ent, "z", 0.0),
                        request.vault_path,
                    )
                    if dist > 120.0:
                        yield f"data: {json.dumps({'reply': f'\\n\\n**System Error:** {target_name} is too far away to whisper ({dist:.1f}ft > 120ft).', 'status': 'error'})}\n\n"
                        return

            target_client_ids = []
            assigned_cids = set(CHARACTER_LOCKS.values())
            unassigned_cids = [cid for cid, q in broadcaster.queues if cid not in assigned_cids]
            target_client_ids.extend(unassigned_cids)
            if "Human DM" in CHARACTER_LOCKS:
                target_client_ids.append(CHARACTER_LOCKS["Human DM"])

            if target_ent.name in CHARACTER_LOCKS:
                target_client_ids.append(CHARACTER_LOCKS[target_ent.name])
            if target_name != "DM" and target_name in CHARACTER_LOCKS:
                target_client_ids.append(CHARACTER_LOCKS[target_name])

            target_client_ids = list(set(target_client_ids))

            await write_audit_log(request.vault_path, request.character, f"Whispered to {target_ent.name}", whisper_text)
            await write_audit_log(request.vault_path, request.character, f"Whispered to {target_name}", whisper_text)

            # Feedback to the sender (Formatted beautifully for the DM, plain text for players due to CSS constraints)
            if request.character == "Human DM":
                sender_feedback = f'<div class="perspective" data-target="{target_ent.name}">**[You whisper to {target_ent.name}]**: {whisper_text}</div>\\n\\n'
                sender_feedback = f'<div class="perspective" data-target="{target_name}">**[You whisper to {target_name}]**: {whisper_text}</div>\\n\\n'
            else:
                sender_feedback = f"*(Message secretly sent to {target_ent.name})*\\n\\n"
                sender_feedback = f"*(Message secretly sent to {target_name})*\\n\\n"
            yield f"data: {json.dumps({'reply': sender_feedback, 'status': 'done'})}\n\n"

            # Broadcast secretly to the Target and the DM using the Perspective HTML wrappers
            broadcast_targets = [cid for cid in target_client_ids if cid != request.client_id]
            if broadcast_targets:
                await broadcaster.broadcast(
                    request.client_id,
                    f"data: {json.dumps({'reply': f'<div class=\"perspective\" data-target=\"{target_ent.name}\">**[{request.character} whispers]**: {whisper_text}</div>\\n\\n', 'status': 'streaming'})}\n\n",
                    f"data: {json.dumps({'reply': f'<div class=\"perspective\" data-target=\"{target_name}\">**[{request.character} whispers]**: {whisper_text}</div>\\n\\n', 'status': 'streaming'})}\n\n",
                    target_client_ids=broadcast_targets,
                )
                await broadcaster.broadcast(
                    request.client_id, f"data: {json.dumps({'status': 'done'})}\n\n", target_client_ids=broadcast_targets
                )

        return StreamingResponse(whisper_generator(), media_type="text/event-stream")

    print(f"\nInitiating Supervisor Loop for: {formatted_prompt}")
    await write_audit_log(request.vault_path, request.character, "Input Received", clean_msg)

    # Identify who should receive the chat echo and the resulting LLM response stream
    target_client_ids = None  # None defaults to broadcasting to everyone (for Human DM)
    if request.character != "Human DM":
        target_client_ids = []

        # 1. DMs and Unassigned clients (Observers/QA) are omniscient and always get the broadcast
        assigned_cids = set(CHARACTER_LOCKS.values())
        unassigned_cids = [cid for cid, q in broadcaster.queues if cid not in assigned_cids]
        target_client_ids.extend(unassigned_cids)
        if "Human DM" in CHARACTER_LOCKS:
            target_client_ids.append(CHARACTER_LOCKS["Human DM"])

        # 2. Find nearby players who can actually perceive the sender
        source_entity = await _get_entity_by_name(request.character, request.vault_path)
        if source_entity:
            nearby_uuids = spatial_service.get_perceivers(
                source_entity.entity_uuid, radius=60.0, require_los=False, vault_path=request.vault_path
            )
            all_ents = get_all_entities(request.vault_path)
            for uid in nearby_uuids:
                ent = all_ents.get(uid)
                if ent and ent.name in CHARACTER_LOCKS:
                    target_client_ids.append(CHARACTER_LOCKS[ent.name])

        target_client_ids = list(set(target_client_ids))  # Deduplicate

    # Broadcast the user's action to listeners
    await broadcaster.broadcast(
        request.client_id,
        f"data: {json.dumps({'reply': f'**[{request.character}]**: {clean_msg}\\n\\n', 'status': 'streaming'})}\n\n",
        target_client_ids=target_client_ids,
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

    # Retrieve or create a concurrency lock for this specific campaign vault
    vault_lock = VAULT_LOCKS.setdefault(request.vault_path, asyncio.Lock())

    async def stream_generator():
        # Lock acquired: Protects the Read -> Execute -> Write cycle
        async with vault_lock:
            try:
                # 1. Initialize Deterministic Engine
                await initialize_engine_from_vault(request.vault_path)

                # Update server discovery info
                global ACTIVE_VAULT_PATH, ACTIVE_CAMPAIGN_NAME
                ACTIVE_VAULT_PATH = request.vault_path
                ACTIVE_CAMPAIGN_NAME = os.path.basename(os.path.abspath(request.vault_path.rstrip("/\\"))) or "DM Engine"

                # Yield an initial status
                yield f"data: {json.dumps({'reply': '*(Thinking...)*\\n\\n', 'status': 'streaming'})}\n\n"

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
                            await broadcaster.broadcast(request.client_id, payload, target_client_ids=target_client_ids)

                    # B. Expose Engine Tools so players see the math happening
                    elif kind == "on_tool_start":
                        tool_name = event.get("name", "tool")
                        if tool_name not in ["ChatGoogleGenerativeAI", "planner_node"]:
                            msg = f"\n> *(Engine: Executing {tool_name}...)*\n"
                            payload = f"data: {json.dumps({'reply': msg, 'status': 'streaming'})}\n\n"
                            yield payload
                            await broadcaster.broadcast(request.client_id, payload, target_client_ids=target_client_ids)

                    # C. Intercept QA Rejections
                    elif kind == "on_chain_end" and node_name == "qa":
                        qa_out = event["data"].get("output", {})
                        if isinstance(qa_out, dict):
                            feedback = qa_out.get("qa_feedback", "")
                            # If QA asked a clarifying question, stream it out
                            if "draft_response" in qa_out:
                                intercept_msg = qa_out.get("draft_response")
                                payload = f"data: {json.dumps({'reply': f'\\n\\n{intercept_msg}', 'status': 'streaming'})}\n\n"
                                yield payload
                                await broadcaster.broadcast(request.client_id, payload, target_client_ids=target_client_ids)
                            # If QA rejected and forced a rewrite
                            elif feedback and feedback != "APPROVED":
                                msg = "\n\n> *(QA Intercept: Correcting mechanical discrepancy. Rewriting...)*\n\n"
                                payload = f"data: {json.dumps({'reply': msg, 'status': 'streaming'})}\n\n"
                                yield payload
                                await broadcaster.broadcast(request.client_id, payload, target_client_ids=target_client_ids)

                # 3. Save combat math back to Obsidian files
                await sync_engine_to_vault(request.vault_path)
                payload = f"data: {json.dumps({'reply': '', 'status': 'done'})}\n\n"
                yield payload
                await broadcaster.broadcast(request.client_id, payload, target_client_ids=target_client_ids)

            except Exception as e:
                logger.exception(
                    "Fatal error during supervisor execution.",
                    extra={
                        "agent_id": "SUPERVISOR",
                        "context": {
                            "client_id": request.client_id,
                            "character": request.character,
                            "vault_path": request.vault_path,
                        },
                    },
                )
                payload = f"data: {json.dumps({'reply': f'\\n\\n**System Error:** {str(e)}', 'status': 'error'})}\n\n"
                yield payload
                await broadcaster.broadcast(request.client_id, payload, target_client_ids=target_client_ids)

    # Return the SSE stream bypassing the standard Pydantic model response
    return StreamingResponse(stream_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    # Passing the app as an import string with reload=True enables Uvicorn's native hot-patching.
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, app_dir=os.path.dirname(__file__))
