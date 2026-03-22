# Integration Blueprint: Connecting the Deterministic Engine to LangGraph

> **Status**: Partially implemented. Core architecture is in place; 9 significant gaps remain.
> Last reviewed: 2026-03-22

## Architecture Overview (What Was Built)

The hybrid Neuro-Symbolic architecture is realized as a 5-node LangGraph:

```
planner_node → action/action_logic → drama_manager → narrator → qa → END
                     ↑                       ↑
                  ToolNode              ToolNode
```

### Current Node Responsibilities

| Node | Role |
|------|------|
| `planner_node` | Translates player intent into deterministic tool calls. System prompt forbids narrative. |
| `action` (ToolNode) | Executes all tools via `EventBus.dispatch()`. |
| `action_logic` (ToolNode) | Executes mutation/storylet tools with Hard Guardrail enforcement. |
| `drama_manager` | Updates tension arc, selects active storylet, routes to narrator if storylet active. |
| `narrator_node` | Converts mechanical truth to vivid prose. Runs Hard Guardrails validation before QA. |
| `qa_node` | 13-point validation checklist. Max 3 revision loops before force-approve. |

### New Files

| File | Purpose |
|------|---------|
| `knowledge_graph.py` | In-memory directed labeled KG with CRUD, path-finding, adjacency index. |
| `hard_guardrails.py` | Deterministic validation: immutable nodes, graph consistency, Wikilink existence. |
| `storylet.py` | Pydantic schema: GraphQuery, GraphMutation, StoryletPrerequisites, Storylet, TensionLevel. |
| `storylet_registry.py` | Storylet polling engine + Obsidian vault persistence at `server/Journals/STORYLETS/`. |
| `drama_manager.py` | TensionArc + DramaManager: selects storylets by tension, applies effects. |
| `graph_sync.py` | Bidirectional sync between flat entity registry and Knowledge Graph. |
| `registry.py` | Per-vault KG and StoryletRegistry singletons. |

---

## Gaps and Required Fixes

Sorted by severity (highest first).

---

### [CRITICAL] Gap 6: Mutations Validated But Never Executed

**What the design requires**: After Hard Guardrails approve proposed mutations, they must be committed to the Knowledge Graph.

**Current state** (FIXED 2026-03-22): `request_graph_mutations` was executing mutations immediately (before narration). The `pending_mutations` field in DMState was dead code.

**Fix implemented**:
- Added `commit: bool = True` parameter to `request_graph_mutations` — when `False`, validates and returns mutation data without executing
- Custom `action_logic_node` intercepts mutation tool calls, captures them into `pending_mutations` WITHOUT executing
- `narrator_node` executes deferred mutations ONLY AFTER guardrails approve the narrative, then clears `pending_mutations`

```python
# In narrator_node, after guard_result.allowed == True:
pending = list(state.get("pending_mutations", []))
for mdict in pending:
    mutation = GraphMutation(**mdict)
    mutation.execute(kg)  # Commits after QA approval
return {"draft_response": draft, "pending_mutations": []}
```

---

### [HIGH] Gap 2: Narrative Guardrails Run AFTER Narrator, Not Before

**What the design requires**: Hard guardrails should intercept *before* the LLM generates prose that violates world state, not after. The fishbowl should be a pre-constraint, not a post-filter.

**Current state**: `narrator_node` calls LLM → validates output → rejects if bad → loops. Force-approve at `MAX_QA_REVISIONS` means rejected content can still slip through.

**Fix**: Inject KG state constraints directly into the narrator's system prompt before LLM invocation. The validation then becomes a sanity check rather than the primary enforcement mechanism.

1. Before invoking LLM in `narrator_node`, query the KG for immutable facts relevant to the current scene
2. Pre-format them as forbidden claims in the system prompt
3. Keep the post-hoc validation as defense-in-depth

---

### [HIGH] Gap 7: Storylet Prerequisites Can't Query Engine State

**What the design requires**: Prerequisites should check *"the global world state"* which includes both the Knowledge Graph AND the entity engine state (HP, conditions, resources, position).

**Current state**: `GraphQuery.evaluate(kg, ctx)` only queries the KG. It cannot check `"Kaelen's HP < max_hp"` or `"Lyra is concentrating on Haste"` because those live in `Creature` objects, not the KG.

**Fix**: Extend `StoryletPrerequisites.is_met()` to also accept the entity registry. Add `query_type` values like `engine_state_check` that read from `get_all_entities()`:

```python
if query_type == "engine_state_check":
    # Check entity.hp.base_value, entity.active_conditions, etc.
```

---

### [HIGH] Gap 1: State-Space Validation Only Checks Wikilinks, Not Claims

**What the design requires** (Task 4.3): *"If the AI generates text stating that a king has granted the participants a legendary magical sword, the validation layer executes a graph query to confirm the king actually possesses the sword."*

**Current state**: `validate_narrative_claim` only checks that `[[Wikilinks]]` correspond to existing KG nodes. It does not verify relationships or attribute claims.

**Fix**: Implement SVO (subject-verb-object) extraction from prose:

1. Parse sentences into (subject, verb, object) triples
2. For each triple, determine if it implies a KG relationship change
3. Cross-reference against KG edges: `"king gives sword"` → verify `(King) --[POSSESSES]--> (Sword)` exists
4. Reject if the implied relationship doesn't exist in KG

---

### [HIGH] Gap 3: Three Clue Rule and Ingestion Pipeline Are Absent

**What the design requires**:
- Phase 2 (World Builder): NLP pipeline to extract entities, infer edges, assign NPC behavioral dials.
- Phase 3 (Campaign Builder): Sequence-to-storylet conversion with Three Clue Rule redundancy.
- Inverse Three Clue Redundancy: *"for any essential conclusion, the architecture must provide at least three distinct vectors of discovery."*

**Current state**: Three Clue Rule analyzer (`storylet_analyzer.py`) and NLP Ingestion Pipeline (`ingestion_pipeline.py`) are implemented:
- `IngestionPipeline` with Phase 2 (a): NPC lore → KG entities/edges/dials via LLM
- Phase 2 (b): Campaign narrative → Storylets with prereq annotations
- Phase 2 (c): Storylet resolution prose → GraphMutation effects (Effect Annotation, Task 3.3)
- `ThreeClueAnalyzer` for chokepoint detection and backup storylet generation

**Fix** (incremental):
1. Build `IngestionPipeline` class with Phase 1 entity extraction using LLM
2. Build `three_clue_analyzer` that traverses storylet dependency graph, identifies bottlenecks, uses LLM to generate N-2 additional storylets per bottleneck
3. Task 3.3 (Effect Annotation) is also absent — parse storylet resolution text and encode as Graph Mutations

---

### [HIGH] Gap 5: Privilege Segregation Incomplete

**What the design requires** (Task 4.2): *"The Creative Agent can read from the Knowledge Graph but cannot execute write commands."*

**Current state**: `action_logic` handles mutation tools, but the narrator can write prose that implies world state changes without emitting any mutation. The KG is never updated to reflect narrative claims.

**Fix**: QA node must cross-check: if narrative implies a world state change (item transferred, NPC attitude shifted, relationship altered), a corresponding mutation must have been committed. If the prose claims a change without a mutation, reject it.

---

### [MEDIUM] Gap 8: Drama Manager Ignores Relationship Web

**What the design requires**: *"When an event occurs at one node in the web, the tension reverberates through the interconnected threads."* Storylet selection should weight based on PC-NPC relationship edges.

**Current state**: `DramaManager.select_next()` only considers tension level and priority. It never examines KG edges between PCs and NPCs.

**Fix**: In `select_next`, calculate a `relationship_weight` boost for storylets involving NPCs with strong relationship edges to the active character. Query KG for `HOSTILE_TOWARD` / `ALLIED_WITH` edges between NPC and active character.

---

### [MEDIUM] Gap 9: No GraphRAG Context Injection

**What the design requires** (Task 1.1): *"a basic GraphRAG query function capable of returning a multi-hop context window for a given node."* When the narrator describes a scene, it should receive the NPC's attributes, edges, and nearby nodes.

**Current state**: `planner_node` and `narrator_node` have no GraphRAG retrieval step. System prompts contain static rules but no dynamically retrieved world state.

**Fix**: Before each LLM invocation, query the KG for:
1. Active location node + all entities connected to it
2. Active NPC nodes + their behavioral dials and relationship edges
3. Any quest-relevant edges involving the active character

Inject this as contextual grounding in the system prompt.

---

### [MEDIUM] Gap 4: NPC Dial-Based Behavioral Parameters Absent

**What the design requires** (Phase 2, NPC Parameterization): *"The pipeline scans NPC biographical text and outputs 2-3 core continuous variables (e.g., `greed: 0.8`, `loyalty: 0.9`) stored as node attributes."*

**Current state**: `KnowledgeGraphNode.attributes` is a `Dict[str, Any]` — structurally capable, but nothing creates or uses NPC dials.

**Fix**:
1. Add `npc_dials: Dict[str, float]` field to `KnowledgeGraphNode` (or use namespaced attributes)
2. Build LLM-powered `extract_npc_dials(biography_text) -> Dict[str, float]` function
3. Inject dials into narrator context when active scene involves an NPC

---

## Implementation Order

| # | Priority | Fix | Status | Files | Test File |
|---|----------|-----|--------|-------|-----------|
| 1 | ~~CRITICAL~~ | Mutations executed after guardrail approval | **DONE** ✅ | `graph.py`, `tools.py` | `test_mutation_capture.py` |
| 2 | HIGH | Pre-inject KG constraints into narrator prompt | **DONE** ✅ | `graph.py` | `test_narrator_kg_context.py` |
| 3 | HIGH | Engine state queries in storylet prereqs | **DONE** ✅ | `storylet.py` | `test_storylet_engine_prereqs.py` |
| 4 | HIGH | SVO claim validation | **DONE** ✅ | `hard_guardrails.py` | `test_svo_validation.py` |
| 5 | HIGH | Three Clue redundancy analyzer | **DONE** ✅ | `storylet_analyzer.py` (new) | `test_three_clue_analyzer.py` |
| 6 | HIGH | QA cross-check: narrative claims vs mutations | **DONE** ✅ | `graph.py` | Integration test in `test_qa_mutation_crosscheck.py` |
| 7 | MEDIUM | Relationship-weighted storylet selection | **DONE** ✅ | `drama_manager.py` | `test_drama_manager_relationships.py` |
| 8 | MEDIUM | GraphRAG context injection | **DONE** ✅ | `graph.py` | `test_grag_context_injection.py` |
| 9 | MEDIUM | NPC dial extraction and injection | **DONE** ✅ | `knowledge_graph.py`, `graph.py` | `test_npc_dials.py` |

---

## Step 1: Initialize the Game State (COMPLETED ✓)

`initialize_engine_from_vault()` in `vault_io.py` parses character/monster YAML and hydrates the engine registry. Modified files are synced back via `sync_engine_to_vault()`.

**Key enhancement added 2026-03-22**: `ModifiableValue` modifiers (Rage, Bless, Geas, etc.) are now persisted to/from YAML using `_{field}_modifiers` keys, ensuring active effects survive across server restarts.

---

## Step 2: Deterministic Tools (COMPLETED ✓)

All tools in `tools.py` use `EventBus.dispatch()` directly. The tools are the Langchain `@tool` wrappers around the deterministic engine. No changes needed.

---

## Step 3: LangGraph Restructuring (COMPLETED ✓)

`graph.py` implements the 5-node architecture as designed. The `action_logic` ToolNode enforces privilege segregation for mutation tools.

---

## Step 4: Serialize Aftermath (COMPLETED ✓)

`sync_engine_to_vault()` syncs entity state back to Obsidian files. KG syncs to `WORLD_GRAPH.md`. Storylet Registry syncs to `server/Journals/STORYLETS/`.
