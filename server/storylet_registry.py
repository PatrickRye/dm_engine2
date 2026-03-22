"""
Storylet registry with polling engine and Obsidian vault persistence.

Provides StoryletRegistry:
- In-memory store for all storylets, keyed by UUID
- Inverted indexes by tag and tension level for fast polling
- poll() — returns all storylets with currently met prerequisites
- load_from_vault() / save_to_vault() — Obsidian markdown persistence

Persistence format:
  server/Journals/STORYLETS/{storylet_name}.md  — one file per storylet, YAML frontmatter
"""

import os
import uuid
from typing import Dict, List, Optional, Set, Any
import asyncio
import aiofiles

from storylet import Storylet, StoryletEffect, StoryletPrerequisites, GraphQuery, GraphMutation, TensionLevel

# ---------------------------------------------------------------------
# Vault persistence helpers (sync — called from async context via asyncio.to_thread)
# ---------------------------------------------------------------------


def _storylet_to_yaml_dict(storylet: Storylet) -> dict:
    return {
        "id": str(storylet.id),
        "name": storylet.name,
        "description": storylet.description,
        "tension_level": storylet.tension_level.value,
        "tags": list(storylet.tags),
        "max_occurrences": storylet.max_occurrences,
        "current_occurrences": storylet.current_occurrences,
        "is_active": storylet.is_active,
        "priority_override": storylet.priority_override,
        "prerequisites": {
            "all_of": [_query_to_dict(q) for q in storylet.prerequisites.all_of],
            "any_of": [_query_to_dict(q) for q in storylet.prerequisites.any_of],
            "none_of": [_query_to_dict(q) for q in storylet.prerequisites.none_of],
        },
        "effects": [
            {
                "id": str(e.id),
                "graph_mutations": [_mutation_to_dict(m) for m in e.graph_mutations],
                "flag_changes": e.flag_changes,
                "attribute_mods": e.attribute_mods,
            }
            for e in storylet.effects
        ],
    }


def _dict_to_storylet(data: dict) -> Storylet:
    prerequisites_data = data.get("prerequisites", {})
    effects_data = data.get("effects", [])

    def _restore_query(qdata: dict) -> GraphQuery:
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

    def _restore_mutation(mdata: dict) -> GraphMutation:
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
            secret=mdata.get("secret", False),
        )

    def _restore_effect(edata: dict) -> StoryletEffect:
        return StoryletEffect(
            id=uuid.UUID(edata["id"]) if edata.get("id") else uuid.uuid4(),
            graph_mutations=[_restore_mutation(m) for m in edata.get("graph_mutations", [])],
            flag_changes=edata.get("flag_changes", {}),
            attribute_mods=edata.get("attribute_mods", {}),
        )

    try:
        tension = TensionLevel(data.get("tension_level", "medium"))
    except ValueError:
        tension = TensionLevel.MEDIUM

    return Storylet(
        id=uuid.UUID(data["id"]) if data.get("id") else uuid.uuid4(),
        name=data.get("name", "Unnamed Storylet"),
        description=data.get("description", ""),
        tension_level=tension,
        tags=set(data.get("tags", [])),
        max_occurrences=int(data.get("max_occurrences", 1)),
        current_occurrences=int(data.get("current_occurrences", 0)),
        is_active=bool(data.get("is_active", True)),
        priority_override=int(data["priority_override"]) if data.get("priority_override") else None,
        prerequisites=StoryletPrerequisites(
            all_of=[_restore_query(q) for q in prerequisites_data.get("all_of", [])],
            any_of=[_restore_query(q) for q in prerequisites_data.get("any_of", [])],
            none_of=[_restore_query(q) for q in prerequisites_data.get("none_of", [])],
        ),
        effects=[_restore_effect(e) for e in effects_data],
        content=data.get("content", ""),
    )


def _query_to_dict(q: GraphQuery) -> dict:
    return {
        "query_type": q.query_type,
        "node_uuid": str(q.node_uuid) if q.node_uuid else None,
        "node_name": q.node_name,
        "node_type": q.node_type,
        "predicate": q.predicate,
        "target_uuid": str(q.target_uuid) if q.target_uuid else None,
        "target_name": q.target_name,
        "attribute": q.attribute,
        "op": q.op.value if hasattr(q.op, "value") else q.op,
        "value": q.value,
    }


def _mutation_to_dict(m: GraphMutation) -> dict:
    return {
        "mutation_type": m.mutation_type,
        "node_uuid": str(m.node_uuid) if m.node_uuid else None,
        "node_name": m.node_name,
        "node_type": m.node_type,
        "predicate": m.predicate,
        "target_uuid": str(m.target_uuid) if m.target_uuid else None,
        "target_name": m.target_name,
        "attribute": m.attribute,
        "value": m.value,
        "tags": m.tags,
        "secret": m.secret,
    }


# ---------------------------------------------------------------------
# StoryletRegistry
# ---------------------------------------------------------------------
class StoryletRegistry:
    def __init__(self) -> None:
        self._storylets: Dict[uuid.UUID, Storylet] = {}
        self._by_tag: Dict[str, Set[uuid.UUID]] = {}
        self._by_tension: Dict[TensionLevel, Set[uuid.UUID]] = {}

    # ------------------------------------------------------------------
    # Core registry operations
    # ------------------------------------------------------------------
    def register(self, storylet: Storylet) -> None:
        self._storylets[storylet.id] = storylet
        self._reindex(storylet)

    def unregister(self, storylet_id: uuid.UUID) -> None:
        storylet = self._storylets.pop(storylet_id, None)
        if storylet:
            self._deindex(storylet)

    def get(self, storylet_id: uuid.UUID) -> Optional[Storylet]:
        return self._storylets.get(storylet_id)

    def get_by_name(self, name: str) -> Optional[Storylet]:
        """Find a storylet by exact name (case-insensitive)."""
        name_lower = name.lower()
        for s in self._storylets.values():
            if s.name.lower() == name_lower:
                return s
        return None

    def get_all(self) -> List[Storylet]:
        return list(self._storylets.values())

    def _reindex(self, storylet: Storylet) -> None:
        # By tag
        for tag in storylet.tags:
            if tag not in self._by_tag:
                self._by_tag[tag] = set()
            self._by_tag[tag].add(storylet.id)
        # By tension
        if storylet.tension_level not in self._by_tension:
            self._by_tension[storylet.tension_level] = set()
        self._by_tension[storylet.tension_level].add(storylet.id)

    def _deindex(self, storylet: Storylet) -> None:
        for tag in storylet.tags:
            self._by_tag.get(tag, set()).discard(storylet.id)
        self._by_tension.get(storylet.tension_level, set()).discard(storylet.id)

    # ------------------------------------------------------------------
    # Polling engine
    # ------------------------------------------------------------------
    def poll(
        self,
        kg,  # KnowledgeGraph — type hints deferred to avoid circular import
        ctx: Dict[str, Any],
        tension: Optional[TensionLevel] = None,
        required_tags: Optional[Set[str]] = None,
    ) -> List[Storylet]:
        """
        Return all storylets whose prerequisites are currently met.

        Args:
            kg: KnowledgeGraph instance to evaluate prerequisites against
            ctx: Runtime context dict (e.g., {"party_name": "Heroes", "session_level": 3})
            tension: If provided, only return storylets matching this tension level
            required_tags: If provided, only return storylets sharing at least one tag
        """
        candidates: List[Storylet] = []
        for storylet in self._storylets.values():
            if not storylet.can_fire(kg, ctx):
                continue
            if tension is not None and storylet.tension_level != tension:
                continue
            if required_tags and not required_tags.intersection(storylet.tags):
                continue
            candidates.append(storylet)
        return candidates

    def get_active_count(self) -> int:
        return sum(1 for s in self._storylets.values() if s.is_active)

    # ------------------------------------------------------------------
    # Vault persistence (async — caller should use asyncio.to_thread or await)
    # ------------------------------------------------------------------
    def _get_storylets_dir(self, vault_path: str) -> str:
        return os.path.join(vault_path, "server", "Journals", "STORYLETS")

    async def load_from_vault(self, vault_path: str) -> int:
        """Load all storylets from vault. Returns count of loaded storylets."""
        import yaml

        storylets_dir = self._get_storylets_dir(vault_path)
        if not os.path.exists(storylets_dir):
            return 0

        count = 0
        for filename in os.listdir(storylets_dir):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(storylets_dir, filename)
            try:
                async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
                    content = await f.read()

                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        yaml_data = yaml.safe_load(parts[1]) or {}
                        storylet = _dict_to_storylet(yaml_data)
                        self.register(storylet)
                        count += 1
            except Exception as e:
                print(f"[StoryletRegistry] Failed to load {filename}: {e}")
        return count

    async def save_to_vault(self, vault_path: str) -> int:
        """Persist all registered storylets to vault. Returns count of saved storylets."""
        import yaml

        storylets_dir = self._get_storylets_dir(vault_path)
        os.makedirs(storylets_dir, exist_ok=True)

        count = 0
        for storylet in self._storylets.values():
            try:
                safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in storylet.name)
                filepath = os.path.join(storylets_dir, f"{safe_name}.md")

                yaml_data = _storylet_to_yaml_dict(storylet)
                body = f"\n# {storylet.name}\n\n{storylet.content}\n"
                yaml_str = yaml.dump(yaml_data, sort_keys=False, default_flow_style=False)
                file_content = f"---\n{yaml_str}---\n{body}"

                async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                    await f.write(file_content)
                count += 1
            except Exception as e:
                print(f"[StoryletRegistry] Failed to save {storylet.name}: {e}")
        return count
