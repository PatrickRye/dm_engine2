"""LangGraph multi-agent graph: Planner → Action → Narrator → QA.

Nodes are defined as closures inside build_graph() so they capture the LLM
instances and tools list without relying on module-level globals.
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from state import DMState, QAResult
from vault_io import write_audit_log
from system_logger import qa_logger
from tools import _get_config_tone

MAX_QA_REVISIONS = 3


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
            )
        )
        llm_with_tools = draft_llm.bind_tools(master_tools_list)
        response = await llm_with_tools.ainvoke([sys_msg] + state["messages"], config=config)
        return {"messages": [response]}

    # ------------------------------------------------------------------
    # NODE: Narrator — turns mechanical truth into vivid prose
    # ------------------------------------------------------------------
    async def narrator_node(state: DMState, config: RunnableConfig):
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

        response = await draft_llm.ainvoke([sys_msg] + state["messages"], config=config)
        return {"draft_response": response.content}

    # ------------------------------------------------------------------
    # NODE: QA — validates the draft against the 12-point checklist
    # ------------------------------------------------------------------
    async def qa_node(state: DMState, config: RunnableConfig):
        vault, draft, revisions = state["vault_path"], state["draft_response"], state.get("revision_count", 0)

        # ESCAPE CLAUSE 1: OOC messages bypass audit
        if draft.strip().startswith("[OOC") or draft.strip().startswith("OOC:"):
            await write_audit_log(vault, "QA Agent", "Bypass", "OOC Clarification detected. Auto-approving.")
            return {"qa_feedback": "APPROVED", "messages": [AIMessage(content=draft)]}

        # ESCAPE CLAUSE 2: Max revisions reached
        if revisions >= MAX_QA_REVISIONS:
            await write_audit_log(vault, "QA Agent", "Force Approve", "Max revisions reached. Passing to prevent loop.")
            return {"qa_feedback": "APPROVED", "messages": [AIMessage(content=draft)]}

        # Gather tools used and mechanical truths from this turn
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
            await write_audit_log(vault, "QA Agent", "Result", "APPROVED")
            qa_logger.info(
                "Draft approved.",
                extra={
                    "agent_id": "QA_Agent",
                    "context": {
                        "character": state.get("active_character"),
                        "vault_path": vault,
                        "revisions_used": revisions,
                    },
                },
            )
            return {"qa_feedback": "APPROVED", "messages": [AIMessage(content=draft)]}

        else:
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
                    },
                },
            )
            return {"qa_feedback": result.feedback, "revision_count": revisions + 1}

    # ------------------------------------------------------------------
    # ROUTER: qa_router — decides whether to approve or loop back
    # ------------------------------------------------------------------
    def qa_router(state: DMState) -> str:
        if state.get("qa_feedback") == "APPROVED":
            return END
        if state.get("revision_count", 0) >= MAX_QA_REVISIONS:
            print("[QA Agent] - Max revisions reached. Force approving.")
            return END
        return "narrator"

    # ------------------------------------------------------------------
    # GRAPH ASSEMBLY
    # ------------------------------------------------------------------
    workflow = StateGraph(DMState)
    workflow.add_node("planner", planner_node)
    workflow.add_node("action", ToolNode(master_tools_list))
    workflow.add_node("narrator", narrator_node)
    workflow.add_node("qa", qa_node)

    workflow.set_entry_point("planner")
    workflow.add_conditional_edges("planner", tools_condition, {"tools": "action", "__end__": "narrator"})
    workflow.add_edge("action", "planner")
    workflow.add_edge("narrator", "qa")
    workflow.add_conditional_edges("qa", qa_router)

    return workflow.compile(checkpointer=checkpointer)
