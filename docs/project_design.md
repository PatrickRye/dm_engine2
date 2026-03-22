# Project Design & Architecture

## 1. Purpose
This repository contains a fully autonomous, event-driven D&D AI Dungeon Master Engine, exposed via a FastAPI Python backend. It blends deterministic Python math (the rules engine) with generative AI (LangGraph agents powered by Google's Gemini models) to track state, validate D&D 5e/5.5e rules, and narrate gameplay seamlessly. It uses local Markdown files (an Obsidian Vault) as its ultimate source of truth, enabling a fully local, version-controlled, and human-readable database for campaign states.

The engine embodies the **Graph-Grounded Storylet Orchestrator** paradigm: a hybrid architecture that combines the relational depth of a Knowledge Graph with the narrative pacing of a Storylet engine, overseen by a Drama Manager agent that emulates the intuition of an expert human facilitator.

---

## 2. Client Capabilities (Player vs. DM)
The frontend is an Obsidian plugin that connects to the FastAPI backend via WebSockets (SSE) and REST endpoints. The system strictly enforces agency and capabilities based on the active client's role:
- **Players**: Can chat in-character, view their dynamically updated character sheets, and view the VTT map canvas. Players are bound by strict Fog of War (they cannot see enemies outside their line of sight or light radius). Their movement paths on the canvas are treated as "proposals" (`propose_move`), which natively calculate difficult terrain budgets, prompt for triggered traps, and alert for opportunity attacks. Players can also toggle automation for their dice rolls (opting to roll physical dice instead of letting the AI handle it).
- **Human DM**: Has elevated "God-mode" privileges. The DM can see all perspectives, ignore Fog of War, drag and drop entities anywhere on the map bypassing movement budgets (`ooc_move_entity`), issue out-of-character (OOC) override commands, manage the party dashboard, and execute hot-patch updates to the server without restarting.

---

## 3. The Deterministic Rules Engine
To prevent LLM hallucinations, D&D combat and mechanics are strictly hardcoded in Python (`dnd_rules_engine.py` and `event_handlers.py`). The AI is **not allowed to calculate damage, hit/miss probabilities, or determine saving throw outcomes**.
- **EventBus Architecture**: The engine uses a publisher/subscriber EventBus. Actions (like `MeleeAttack` or `SpellCast`) are dispatched as events that pass through distinct lifecycle phases (`PRE_EVENT`, `EXECUTION`, `POST_EVENT`, `RESOLVED`).
- **Modifiers & Conditions**: Traits, magical items, and buffs are handled via numerical modifiers and priority systems (e.g., ADDITIVE vs OVERRIDE). Active conditions natively alter state (e.g., "Prone" costs half movement to stand, "Stunned" auto-fails Dex saves).
- **ModifiableValue Persistence**: Active modifiers (Rage, Bless, Geas, etc.) are serialized to/from YAML using `_{field}_modifiers` keys, ensuring effects survive across server restarts.

---

## 4. Maps & Spatial Engine
The engine includes a headless, deterministic spatial calculator (`spatial_engine.py`) built on GIS libraries (`shapely` and `rtree`). It translates narrative intent into exact geometry:
- **Line of Sight & Cover**: Casts 3D rays to calculate if a target has Half, Three-Quarters, or Total cover based on intersecting walls or intervening creatures.
- **Area of Effect (AoE)**: Calculates exact pixel-to-grid hits for spheres, cones, lines, and cubes across 3D planes (X, Y, Z).
- **Dynamic Geometry & Lighting**: Destructible walls, openable doors, and light sources (bright/dim radii) directly influence combat math (e.g., shooting into darkness applies Disadvantage natively).

---

## 5. Knowledge Graph — World State Ontology
The world guide (lands, factions, NPCs, items, deities, historical events) is ingested and maintained in a **Knowledge Graph (KG)** (`knowledge_graph.py`). The KG serves as the absolute, objective reality of the game world — the "fishbowl" that no LLM prose may contradict.

**Node Types**: `NPC`, `Location`, `Faction`, `Item`, `Deity`, `Quest`, `Condition`

**Edge Predicates**: `connected_to`, `located_in`, `member_of`, `allied_with`, `hostile_toward`, `controls`, `leads`, `serves`, `rival_of`, `owned_by`, `possesses`, `wants`, `knows_about`, `rules`

**Key KG Features**:
- **Immutable Nodes**: Nodes tagged `is_immutable=True` cannot be modified or deleted by storylet effects — a hard guardrail for plot-critical NPCs, artifacts, and locations.
- **Behavioral Dials**: NPC nodes carry `npc_dials: Dict[str, float]` (e.g., `{"greed": 0.8, "loyalty": 0.9}`) extracted from biography text. These inform LLM roleplay generation.
- **GraphRAG Context**: `_get_grag_context()` returns a formatted multi-hop neighborhood for a given character, cached for 60s with LRU eviction. Injected into the narrator's system prompt for dynamic world-state grounding.
- **Path Finding**: `find_shortest_path()` enables multi-hop logical deductions (e.g., "if the players assassinate NPC X, which factions are impacted?").
- **Wikilink Existence**: All `[[Wikilinks]]` in narrative prose are validated against the KG before the prose reaches QA.

**Vault Persistence**: The KG syncs bidirectionally with `WORLD_GRAPH.md` in the Obsidian vault via `vault_io.py`.

---

## 6. Storylet Architecture — Narrative Vector Space
Campaign plots, quests, and villain schemes are stored as **Storylets** (`storylet.py`) — self-contained narrative units decoupled from chronological sequencing.

**Storylet Schema**:
1. **Prerequisites** (`StoryletPrerequisites`): Complex `GraphQuery` checks against the KG and engine state. A storylet only fires when all `all_of`, any of `any_of`, and none of `none_of` conditions are satisfied.
2. **Content**: The narrative text, dialogue, or encounter data presented to participants.
3. **Effects** (`StoryletEffect`): GraphMutations applied when the storylet resolves (e.g., `Faction_Reputation -= 10`, `Has_Met_Villain = True`). Effects are applied atomically by the Drama Manager.
4. **Tension Level**: `low`, `medium`, `high`, or `cliffhanger` — used by the Drama Manager to modulate pacing.

**StoryletRegistry** (`storylet_registry.py`): In-memory store with inverted indexes by tag and tension level. `poll(kg, ctx, tension, required_tags)` returns all eligible storylets. Persists to `server/Journals/STORYLETS/{name}.md` as YAML frontmatter + markdown body.

**Three Clue Rule**: The `ThreeClueAnalyzer` (`storylet_analyzer.py`) traverses the storylet dependency graph to detect bottleneck storylets (只有一个入站边). For each bottleneck, it uses an LLM to generate 2 additional logically consistent backup storylets, ensuring players always have redundant vectors to advance the plot.

---

## 7. Drama Manager — Tension Arc & Storylet Selection
The `DramaManager` (`drama_manager.py`) selects the optimal storylet to present based on narrative pacing, preventing the "narrative drift" that comes from purely reactive storytelling.

**TensionArc**: Tracks the session's dramatic rhythm (`low → medium → high → cliffhanger → resolution`). Advances based on the outcome tension of tool results (e.g., a hit/damage = HIGH tension; a miss = MEDIUM).

**Relationship-Weighted Selection**: Storylet selection weights storylets by the strength of PC-NPC relationship edges in the KG (`HOSTILE_TOWARD` and `ALLIED_WITH` edges between the NPC and active character), ensuring emotionally relevant drama surfaces organically.

**Privilege Segregation**: The Drama Manager applies storylet effects (GraphMutations) only after guardrail validation, never allowing the creative LLM direct write access to the KG.

---

## 8. The 5-Node LangGraph Orchestration
The core orchestration is a LangGraph state machine with 5 node types (`graph.py`):

```
planner_node → clear_mutations → action/action_logic → drama_manager → narrator → qa → commit → END
                                      ↑                       ↑
                                  ToolNode              Hard Guardrails
```

| Node | Responsibility |
|------|---------------|
| `planner_node` | Translates player intent into deterministic tool calls. System prompt forbids narrative prose. Receives GraphRAG context and active storylet injection. |
| `clear_mutations_node` | Clears stale `pending_mutations` from a QA-rejected previous turn when a new HumanMessage arrives. Routes to `action`, `action_logic`, or `narrator`. |
| `action` (ToolNode) | Executes all deterministic tools via `EventBus.dispatch()` — combat math, movement, skill checks, etc. |
| `action_logic` (custom ToolNode) | Intercepts mutation/storylet tools, validates via Hard Guardrails, and **captures mutations for deferred execution** without executing them. |
| `drama_manager` | Updates tension arc, polls storylets via `DramaManager.select_next()`, activates the selected storylet, applies its effects, validates integrity. Routes to `narrator` if a storylet is active, else back to `planner`. |
| `narrator_node` | Converts mechanical truth into vivid prose. **Pre-injects KG immutable constraints** and GraphRAG context before LLM invocation. Runs Hard Guardrails SVO validation before QA. Executes deferred `pending_mutations` after guardrails approval. |
| `qa_node` | 13-point validation checklist. Max 3 revision loops before force-approve. Handles OOC bypass, COMMIT escape clause, KG rollback on rejection. |
| `commit_node` | Executes `pending_mutations` after QA approval. Triggers **EmergentWorldBuilder** for any newly created entities. Invalidates GraphRAG cache. |
| `ingestion_node` | Runs the NLP ingestion pipeline with direct LLM access (Phase 2 campaign hydration). |

**Deferred Mutation Pattern (Gap 6 fix)**: Mutations are NOT executed in `action_logic`. Instead, `action_logic` validates and captures them into `state.pending_mutations`. `narrator_node` re-validates and commits them after Hard Guardrails approve the narrative. This prevents the checkpointer from saving a state where mutations are committed but the narrative is rejected.

**KG Rollback**: If QA rejects, the KG is restored from the `kg_snapshot` taken before narration, discarding any speculatively executed mutations.

---

## 9. Hard Guardrails — Deterministic Thematic Enforcement
Hard guardrails are deterministic, algorithmic constraints executed entirely outside the LLM's neural network. They function as the "fishbowl" of the improv table — the physical laws of the narrative world.

**State-Space Validation** (`hard_guardrails.py`):
- **Wikilink Existence**: All `[[Wikilinks]]` in prose must correspond to existing KG nodes.
- **Immutable Node Protection**: Mutations targeting `is_immutable=True` nodes are rejected.
- **SVO Claim Validation**: Extracts (subject, verb, object) triples from prose via regex + KG entity matching. For transfer verbs (`gives`, `takes`, `hands`), the transferred entity (SVO object) must appear in a corresponding mutation. For membership verbs (`joins`), the joiner (SVO subject) must appear in the mutation. For alliance/hostility, both subject and object must be covered. If prose claims a world-state change without a mutation, it is rejected.
- **Full-Pipeline Aggregation**: All guardrail failures are aggregated (not fail-fast) so the LLM can fix everything in one revision cycle.

**Privilege Segregation**: The creative LLM (narrator) cannot write directly to the KG. All KG modifications go through `request_graph_mutations`, which must pass Hard Guardrail validation before execution.

---

## 10. Emergent Worldbuilding — Lazy Entity Hydration
Players are secondary storytellers. When they walk into a tavern, invent an NPC, or reference an unnamed contact, the KG must lazily grow to accommodate this — **without** pre-inventing the world.

**EmergentWorldBuilder** (`ingestion_pipeline.py`): Triggered by `commit_node` after an `add_node` mutation is committed. Three phases:

1. **Flesh Out Entity**: Calls LLM to produce description and behavioral dials for the new entity.
2. **Generate Side Quest Storylets**: Creates 3 storylet stubs anchored to the entity via `node_exists` prerequisites (e.g., "Mary's Secret Past", "The Missing Contact", "The Dangerous Debt").
3. **Infer World Edges**: Regex-scans narrative context for location keywords and faction mentions, auto-creating KG nodes and edges (e.g., `Mary --LOCATED_IN--> The Prancing Pony`, `Mary --SERVES--> Thieves Guild`).

**Player-Created Entities**: Default to `is_immutable=False`, allowing refinement. The DM approves all emergent entities before permanent commitment.

**Tools**:
- `propose_entity_creation`: DM-facing tool to explicitly propose a new entity.
- `generate_side_quests_for_entity`: Explicitly generate side quests for an existing entity.

---

## 11. NLP Ingestion Pipeline — From Raw Text to Structured Data
Two ingestion pipelines convert raw DM materials into engine artifacts (`ingestion_pipeline.py`):

**CampaignHydrationPipeline** (one-shot):
1. Extract KG entities + edges from all raw text (LLM-powered with deterministic fallback)
2. Parse campaign narrative → Storylets with prerequisite annotations
3. Annotate storylet resolutions → GraphMutations (EffectAnnotationPipeline)
4. Three Clue Rule analysis + backup storylet generation
5. Register all artifacts + persist to vault

**IncrementalHydrationPipeline** (delta updates):
- `_filter_new_entities_from_text()`: Strips known entity names before LLM processing to avoid re-generating existing content.
- `delta_hydrate(new_materials)`: Processes only genuinely new content.
- `hydrate_missing_entity(entity_name, context)`: Hydrates a single entity referenced in narrative but missing from KG.
- `detect_missing_entities(narrative_text)`: Proactively scans for `[[Wikilinks]]` and capitalized names not in the KG.
- `suggest_hydration(missing_entities)`: Generates a DM prompt for manual hydration.

**EffectAnnotationPipeline**: Parses storylet resolution prose (e.g., "Lord Vader turns on the party and joins the Cult") into GraphMutations. Uses LLM with structured output; falls back to deterministic keyword matching.

**Deterministic Fallback**: When no LLM is available, section headers (`## NPC:`, `## Location:`), capitalized proper nouns, and relationship patterns (`owns`, `allied with`, `hates`, `serves`, `leads`, `member of`, `located in`, `knows about`) are extracted via regex.

---

## 12. Vault Persistence Strategy
The Obsidian vault is the ultimate source of truth. All engine state is serializable to and hydratable from Markdown/YAML files.

| Artifact | Vault Location | Format |
|----------|---------------|--------|
| Character sheets | `Characters/{name}.md` | YAML frontmatter + markdown body |
| Monster/compendium | `Compendium/{name}.md` | YAML frontmatter + markdown body |
| Knowledge Graph | `WORLD_GRAPH.md` | YAML frontmatter + adjacency list |
| Storylets | `server/Journals/STORYLETS/{name}.md` | YAML frontmatter + markdown body |
| Audit log | `server/Journals/AUDIT_LOG.md` | Markdown entries per turn |
| KG snapshot | `WORLD_GRAPH.md` (backup) | Auto-synced on mutations |

`initialize_engine_from_vault()` parses all YAML at startup. `sync_engine_to_vault()` writes back after each session turn.

---

## 13. QA Agent & Narrative Guardrails
A dedicated QA Agent node (`qa_node`) acts as a strict internal auditor before any text reaches the players.
- **Drafting Loop**: The Narrator AI generates a draft. The QA Agent reviews this draft against the deterministic "MECHANICAL TRUTH" logs generated by the math tools.
- **Guardrails**: The QA Agent enforces a strict checklist: No dictating player actions (preserving player agency), no meta-gaming (leaking exact AC/HP numbers), enforcing "Fail Forward" narrative momentum, preventing hallucinated dice rolls, and ensuring formatting rules (Obsidian Wikilinks) are maintained.
- **Rejection & Clarification**: If the draft hallucinates a rule or violates the checklist, the QA Agent rejects it and forces the Narrator to rewrite. If a player's intent is physically impossible or mechanically ambiguous, the QA Agent intercepts the flow and asks an Out-Of-Character (OOC) clarifying question directly to the user.
- **SVO Backstop**: Independently re-validates SVO claims in QA as a backstop to the narrator's guard check.
- **KG Rollback**: On rejection, restores KG from pre-narration snapshot, discarding any speculative mutations.

---

## 14. Architectural Domains & Extensibility
When assigning or implementing tasks, you must respect the boundaries of these domains:
- **Rules Engine (`dnd_rules_engine.py`)**: Strict Object-Oriented Programming (OOP). Driven by an `EventBus`. This must be highly deterministic math. **NO LLM CALLS ALLOWED HERE.** Use the Decorator pattern for dynamic modifiers (like magic items).
- **LangChain Tools (`tools.py`)**: These are the bridge between the LLM and the rules engine. They must always return strings starting with `MECHANICAL TRUTH:` or `SYSTEM ERROR:` to guide the Narrator LLM.
- **State Management (`state.py`)**: Strictly enforced via Pydantic models.
- **Prompts (`prompts.py`)**: The system instructions for the LLM agents.
- **Spatial Engine (`spatial_engine.py`)**: Handles GIS, line-of-sight, and coordinates using `shapely` and `rtree`.
- **Knowledge Graph (`knowledge_graph.py`)**: In-memory directed labeled graph. No LLM calls — pure data structure.
- **Storylet (`storylet.py`)**: Pydantic schemas only. No business logic.
- **Ingestion Pipeline (`ingestion_pipeline.py`)**: LLM-powered. Wraps `_call_llm_structured`, `_call_llm_json`, and deterministic fallbacks.

---

## 15. Testing Strategy
- Test-Driven Development (TDD) is highly encouraged.
- All new features, bug fixes, and mechanical additions MUST be accompanied by updates to the `pytest` suite in `test/server/`.
- Do not submit Pull Requests unless the local test suite passes perfectly.
- **Current test count**: 557 passing tests across unit and integration suites.

---

## 16. AI Developer Workflow
- **Planner**: Analyzes the architecture and drafts specific `Implementer Instructions` for the coder.
- **Implementer**: A Senior Software Engineer focused on clean, DRY, and extensible code. Writes tests and implementation.
- **Reviewer**: Audits the PR to ensure the architecture wasn't violated.

---

## 17. Key Files Reference

| File | Purpose |
|------|---------|
| `server/graph.py` | 5-node LangGraph: planner → action → drama_manager → narrator → qa → commit |
| `server/knowledge_graph.py` | KG with CRUD, path-finding, adjacency index, GraphRAG |
| `server/hard_guardrails.py` | SVO validation, immutable nodes, Wikilink checks |
| `server/storylet.py` | GraphQuery, GraphMutation, StoryletPrerequisites, Storylet, TensionLevel |
| `server/storylet_registry.py` | Storylet polling engine + Obsidian vault persistence |
| `server/drama_manager.py` | TensionArc, DramaManager with relationship-weighted selection |
| `server/storylet_analyzer.py` | ThreeClueAnalyzer for chokepoint detection and backup storylet generation |
| `server/ingestion_pipeline.py` | CampaignHydrationPipeline, IncrementalHydrationPipeline, EmergentWorldBuilder, EffectAnnotationPipeline |
| `server/registry.py` | Per-vault KG and StoryletRegistry singletons |
| `server/graph_sync.py` | Bidirectional sync between flat entity registry and Knowledge Graph |
| `server/vault_io.py` | YAML/JSON persistence for characters, monsters, KG, storylets |
| `server/tools.py` | All LangChain tools (+40 tools) |
| `server/main.py` | FastAPI app, LLM initialization, LangGraph compilation |
| `test/server/test_*.py` | 557 passing tests |
