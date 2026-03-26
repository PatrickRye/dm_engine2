# Compendium Hydrator Coordinator

**Role:** Orchestrates parallel hydration of raw D&D compendium material (creatures, maps, locations, factions, NPCs, campaign narratives) into the DM Engine's Knowledge Graph, Storylet Registry, and Compendium Manager.

**Invoked by:** LLM-DM (planner node) via `hydrate_compendium` tool. Closes when all sub-hydrators complete.

---

## Architecture

```
LLM-DM planner
    │
    │ "hydrate my Compendium: [material]"  (tool call)
    ▼
┌─────────────────────────────────────────────────────────┐
│  compendium_hydration_node (coordinator)                │
│                                                          │
│  1. Parse incoming materials, classify by type          │
│  2. Dispatch N sub-hydrators in parallel                 │
│  3. Await all results                                    │
│  4. Aggregate into KG updates + vault writes            │
│  5. Return CompendiumHydrationReport                     │
└─────────────────────────────────────────────────────────┘
    │
    │ parallel
    ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│ Creature │ │ Location │ │ Faction  │ │   NPC    │ │   Map    │
│ Hydrator │ │ Hydrator │ │ Hydrator │ │ Hydrator │ │ Hydrator │
└──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
    │              │           │            │           │
    └──────────────┴───────────┴────────────┴───────────┘
                            │
                     ┌──────┴──────┐
                     │  Narrative  │
                     │  Hydrator   │
                     │ (Campaign   │
                     │HydrationPipe│
                     └─────────────┘
```

---

## Sub-Hydrators

### 1. Creature Hydrator
- **Reads:** `docs/Creature Hydrator.md`
- **Input:** Raw creature stat block text (Markdown or plain)
- **Output:** KG nodes (CREATURE type) + relationships to locations/factions + tactical metadata (role, engagement, morale, targeting)
- **Vault:** Writes to `server/Journals/{CreatureName}.md` via vault_io
- **KG predicate contributions:** MEMBER_OF, LOCATED_IN, HOSTILE_TOWARD, ALLIED_WITH

### 2. Location Hydrator
- **Reads:** `docs/Location Hydrator.md`
- **Input:** Location description text (place names, geography, landmarks, demographics)
- **Output:** KG LOCATION nodes + CONNECTED_TO edges between locations
- **Vault:** Writes to `server/Journals/LOCATIONS/{LocationName}.md`
- **KG predicate contributions:** CONNECTED_TO, LOCATED_IN

### 3. Faction Hydrator
- **Reads:** `docs/Faction Hydrator.md`
- **Input:** Faction lore text (goals, assets, key NPCs, hierarchy)
- **Output:** KG FACTION nodes + LEADS/CONTROLS/MEMBER_OF edges
- **Vault:** Writes to `server/Journals/FACTIONS/{FactionName}.md`
- **KG predicate contributions:** CONTROLS, LEADS, MEMBER_OF, ALLIED_WITH, HOSTILE_TOWARD

### 4. NPC Hydrator
- **Reads:** `docs/NPC Hydrator.md`
- **Input:** NPC biography, personality, stat block, relationships
- **Output:** KG NPC nodes + disposition edges + `NPCDetails` in state
- **Vault:** Writes to `server/Journals/{NPCName}.md`
- **KG predicate contributions:** MEMBER_OF (faction), ALLIED_WITH, HOSTILE_TOWARD, SERVES, RIVAL_OF
- **Note:** NPC is distinct from CREATURE — NPCs have social attributes (goals, connections, attitude dials)

### 5. Map Hydrator
- **Reads:** `docs/Map Hydrator.md`
- **Input:** Map image file (with optional description text)
- **Output:** KG LOCATION node with map_data (grid, tokens, areas) + battlemaps stored in `server/Journals/MAPS/`
- **Process:** Vision AI to analyze map image, extract JSON battlemaps via `vault_io`'s existing `auto_ingest_battlemaps`

### 6. Narrative/Storylet Hydrator
- **Reads:** `docs/Narrative Hydrator.md`
- **Input:** Campaign narrative prose, session summaries, session prep notes
- **Output:** Storylet Registry entries with prerequisites and GraphMutations
- **Engine:** Delegates to `CampaignHydrationPipeline` (existing) for storylet extraction and `EffectAnnotationPipeline` for resolution annotation
- **KG predicate contributions:** Creates QUEST nodes, KNOWS_ABOUT edges

### 7. Compendium Items Hydrator
- **Reads:** `docs/Compendium Items Hydrator.md`
- **Input:** Spells, feats, features, items raw text
- **Output:** Compendium Manager JSON entries (via `compendium_manager.py`)
- **Handles:** `CompendiumEntry` schema with `MechanicEffect` for spells/feats

---

## Coordinator Protocol

```python
class CompendiumMaterials(TypedDict):
    """Top-level input schema for the coordinator."""
    creatures: str          # Raw creature stat blocks (multi-entry Markdown)
    locations: str          # Location descriptions
    factions: str           # Faction lore entries
    npcs: str               # NPC biographies + stat blocks
    maps: list[str]         # Map image paths or base64
    campaign_narrative: str # Campaign prose for storylets
    session_prep_notes: str  # Session prep for storylets
    storylet_resolutions: dict[str, str]  # name -> resolution text
    items: str              # Spells, feats, items raw text
```

```python
class CompendiumHydrationReport(TypedDict):
    """Aggregated result from all sub-hydrators."""
    creatures: CreatureHydrationReport
    locations: LocationHydrationReport
    factions: FactionHydrationReport
    npcs: NPCHydrationReport
    maps: MapHydrationReport
    narrative: HydrationReport  # from CampaignHydrationPipeline
    items: CompendiumItemsReport
    errors: list[str]
    partial_failures: list[str]
```

---

## Prompt File Structure

Each hydrator reads its system prompt from `docs/{HydratorName}.md`. The first section is the system prompt (role + objective + rules). Any `# EXAMPLES` or `# OUTPUT SCHEMA` sections are extracted by the hydrator for structured output guidance.

```
docs/
  Creature Hydrator.md       ← existing, full system prompt
  Location Hydrator.md        ← TO CREATE
  Faction Hydrator.md         ← TO CREATE
  NPC Hydrator.md             ← TO CREATE
  Map Hydrator.md             ← TO CREATE
  Narrative Hydrator.md       ← TO CREATE
  Compendium Items Hydrator.md ← TO CREATE
```

The coordinator reads ALL hydrator prompts at startup (cached). Each sub-hydrator is initialized with its specific prompt.

---

## Node Integration (graph.py)

The coordinator is a **single LangGraph node** (`compendium_hydration_node`) that:

1. Receives `CompendiumMaterials` as tool args from the planner
2. Classifies which sub-hydrators are needed based on non-empty fields
3. Spawns sub-hydrators as async tasks (parallel where possible)
4. Collects results and handles errors per-sub-hydrator
5. Writes KG updates and vault files
6. Returns a `CompendiumHydrationReport` as a `ToolMessage`
7. **Closes** when done — the node is not persistent

**Sub-hydrator lifecycle:**
- Each sub-hydrator is an **async function** (not a separate process/agent)
- It receives: raw material text + system prompt + vault_path + llm
- It returns: structured output + side effects (KG writes, vault writes)
- It **closes** (returns) when done — no ongoing state

**Invocation from LLM-DM:**
```
planner → "hydrate_compendium" tool call
  → routed to compendium_hydration_node
  → coordinator dispatches sub-hydrators
  → ToolMessage(CompendiumHydrationReport) returned
  → back to planner
```

---

## Error Handling

- **Partial failure:** If one sub-hydrator fails, others complete. Failed items listed in `report.errors`.
- **Retry:** Each sub-hydrator retries once on transient LLM errors.
- **Validation:** Output schemas validated with Pydantic before KG写入.
- **Atomic-ish:** KG writes happen after all sub-hydrators complete (no partial graph state on failure).

---

## Routing from Planner

The planner decides when to call `hydrate_compendium`. It should be triggered when:
- DM says "load my compendium" or "hydrate these creatures"
- DM uploads creature stat blocks or lore documents
- Campaign prep includes map/npc/faction/lore material

The `hydrate_compendium` tool aggregates ALL material types in one call so the coordinator can parallelize.
