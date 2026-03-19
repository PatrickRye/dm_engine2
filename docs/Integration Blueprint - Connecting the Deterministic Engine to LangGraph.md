# Integration Blueprint: Connecting the Deterministic Engine to LangGraph

To fully realize the hybrid Neuro-Symbolic architecture, we must bridge your existing `LangGraph` setup in `main.py` with the new deterministic `dnd_rules_engine.py`. The goal is to demote the primary LLM from "Rules Arbiter" to "Intent Planner" and "Narrator."

## Step 1: Initialize the Game State (in `vault_io.py`)

Currently, `vault_io.py` reads Markdown text. We need to parse the YAML frontmatter into our new Pydantic objects (`Creature`, `Weapon`) and cache them in the Engine's memory on startup.

**Updates required in `vault_io.py`:**

1. Create a `load_campaign_state()` function that runs when `main.py` starts up.
    
2. It should parse all character and monster markdown files in the Obsidian vault.
    
3. Instantiate them using `Creature(**yaml_data)` which automatically registers their UUIDs in `BaseGameEntity._registry`.
    

## Step 2: Create Deterministic Tools (in `tools.py`)

The LLM (Planner Agent) will no longer write narrative combat text directly. Instead, it will emit structured tool calls to the engine.

**Updates required in `tools.py`:**

Add Langchain tools that act as wrappers for the `EventBus`.

```
from langchain_core.tools import tool
from dnd_rules_engine import GameEvent, EventBus, BaseGameEntity

@tool
def execute_melee_attack(attacker_name: str, target_name: str) -> str:
    """Use this tool when a character attempts to attack a target with a melee weapon."""
    
    # 1. Lookup UUIDs by name (you'll need a helper function for this)
    attacker_uuid = get_uuid_by_name(attacker_name) 
    target_uuid = get_uuid_by_name(target_name)

    # 2. Fire the Event into the Deterministic Engine
    event = GameEvent(
        event_type="MeleeAttack",
        source_uuid=attacker_uuid,
        target_uuid=target_uuid
    )
    result = EventBus.dispatch(event)
    
    # 3. Return the absolute mechanical truth to the LLM
    # E.g., "HIT! Kaelen deals 8 slashing damage. Lyra has 6 HP remaining."
    if result.payload.get("hit"):
        return f"HIT! Dealt {result.payload['damage']} damage."
    return "MISS! The attack failed to beat the target's AC."
```

## Step 3: Restructure LangGraph (in `main.py`)

Your current `main.py` likely has a straightforward graph: `Agent -> Tools -> Agent -> QA -> End`.

We need to split the Agent into two distinct personas to enforce the separation of concerns.

**Updates required in `main.py`:**

Redesign your `StateGraph` into this flow:

1. **`planner_node`**: Reads user input (`DMState['messages']`). Its system prompt STRICTLY FORBIDS writing narrative. It is only allowed to output JSON tool calls to `execute_melee_attack`, `cast_spell`, or `move`.
    
2. **`tool_node`**: Executes the deterministic Python tools and appends the result to the state.
    
3. **`narrator_node`**: Reads the user input AND the deterministic tool results. Its system prompt dictates: _"You are the Narrator. Read the mechanical outcome provided by the system. Describe the action vividly. DO NOT alter the math, damage, or hit/miss outcome."_
    
4. **`qa_node`**: Receives the `narrator_node` output. Because the engine guaranteed the math, this node only checks if the Narrator hallucinated a lore fact or controlled the player character unfairly.
    

## Step 4: Serialize the Aftermath (in `vault_io.py`)

After the `narrator_node` finishes and the `qa_node` approves the draft, the current state of the objects in Python memory must be synced back to the local Markdown vault to prevent data loss.

1. Iterate over `BaseGameEntity._registry.values()`.
    
2. Use `model_dump(exclude={'_registry'})` to convert the `Creature` objects back to dicts.
    
3. Use the YAML parsing techniques already present in `vault_io.py` to overwrite the frontmatter of `Kaelen.md`, `Goblin.md`, updating their current `hp.base_value` and any active `Modifiers`.
    

By adopting this structure, the LLM will act purely as the graphical interface and storyteller, while the invisible Python engine enforces the D&D 5e mechanics flawlessly.