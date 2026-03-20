# Project Design & Architecture

## 1. Purpose
This repository contains a fully autonomous, event-driven D&D AI Dungeon Master Engine. It blends deterministic Python math (the rules engine) with generative AI (LangGraph agents) to track state, validate rules, and narrate gameplay seamlessly.

## 2. Architectural Domains & Extensibility
When assigning or implementing tasks, you must respect the boundaries of these domains:
- **Rules Engine (`dnd_rules_engine.py`)**: Strict Object-Oriented Programming (OOP). Driven by an `EventBus`. This must be highly deterministic math. **NO LLM CALLS ALLOWED HERE.** Use the Decorator pattern for dynamic modifiers (like magic items).
- **LangChain Tools (`tools.py`)**: These are the bridge between the LLM and the rules engine. They must always return strings starting with `MECHANICAL TRUTH:` or `SYSTEM ERROR:` to guide the Narrator LLM.
- **State Management (`state.py`)**: Strictly enforced via Pydantic models.
- **Prompts (`prompts.py`)**: The system instructions for the LLM agents.
- **Spatial Engine (`spatial_engine.py`)**: Handles GIS, line-of-sight, and coordinates using `shapely` and `rtree`.

## 3. Testing Strategy
- Test-Driven Development (TDD) is highly encouraged.
- All new features, bug fixes, and mechanical additions MUST be accompanied by updates to the `pytest` suite in `test/server/`.
- Do not submit Pull Requests unless the local test suite passes perfectly.

## 4. AI Developer Workflow
- **Planner**: Analyzes the architecture and drafts specific `Implementer Instructions` for the coder.
- **Implementer**: A Senior Software Engineer focused on clean, DRY, and extensible code. Writes tests and implementation.
- **Reviewer**: Audits the PR to ensure the architecture wasn't violated.