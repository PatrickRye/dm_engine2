import uuid
from typing import Dict, Any, Optional

_ACTIVE_ENTITIES: Dict[str, Dict[uuid.UUID, Any]] = {}
_NAME_INDEX: Dict[str, Dict[str, uuid.UUID]] = {}

# Per-vault Knowledge Graph and Storylet Registry instances
_KNOWLEDGE_GRAPHS: Dict[str, Any] = {}  # vault_path -> KnowledgeGraph
_STORYLET_REGISTRIES: Dict[str, Any] = {}  # vault_path -> StoryletRegistry


def register_entity(entity: Any, vault_path: str = "default") -> None:
    """Registers an entity into the global state."""
    vp = getattr(entity, "vault_path", vault_path)
    if not vp:
        vp = "default"

    if vp not in _ACTIVE_ENTITIES:
        _ACTIVE_ENTITIES[vp] = {}
        _NAME_INDEX[vp] = {}

    if hasattr(entity, "entity_uuid"):
        _ACTIVE_ENTITIES[vp][entity.entity_uuid] = entity
        if hasattr(entity, "name"):
            _NAME_INDEX[vp][entity.name.lower()] = entity.entity_uuid


def get_entity(uid: uuid.UUID, vault_path: str = None) -> Optional[Any]:
    """Retrieves an entity by its UUID."""
    if vault_path:
        return _ACTIVE_ENTITIES.get(vault_path, {}).get(uid)

    for vp, vault in _ACTIVE_ENTITIES.items():
        if uid in vault:
            return vault[uid]
    return None


def remove_entity(uid: uuid.UUID) -> None:
    """Removes an entity from the global state by its UUID."""
    for vp, vault in _ACTIVE_ENTITIES.items():
        if uid in vault:
            ent = vault[uid]
            if hasattr(ent, "name") and ent.name.lower() in _NAME_INDEX.get(vp, {}):
                del _NAME_INDEX[vp][ent.name.lower()]
            del vault[uid]
            break


def get_all_entities(vault_path: str = "default") -> Dict[uuid.UUID, Any]:
    """Returns a dictionary of all active entities."""
    return _ACTIVE_ENTITIES.get(vault_path, {})


def clear_registry(vault_path: str = None) -> None:
    """Clears all entities from the global state."""
    if vault_path:
        if vault_path in _ACTIVE_ENTITIES:
            _ACTIVE_ENTITIES[vault_path].clear()
        if vault_path in _NAME_INDEX:
            _NAME_INDEX[vault_path].clear()
    else:
        _ACTIVE_ENTITIES.clear()
        _NAME_INDEX.clear()


# ---------------------------------------------------------------------
# Knowledge Graph helpers
# ---------------------------------------------------------------------
def get_knowledge_graph(vault_path: str = "default"):
    """Returns the KnowledgeGraph for a vault, creating it if absent."""
    if vault_path not in _KNOWLEDGE_GRAPHS:
        from knowledge_graph import KnowledgeGraph
        _KNOWLEDGE_GRAPHS[vault_path] = KnowledgeGraph()
    return _KNOWLEDGE_GRAPHS[vault_path]


def set_knowledge_graph(vault_path: str, kg: Any) -> None:
    """Replaces the KnowledgeGraph for a vault."""
    _KNOWLEDGE_GRAPHS[vault_path] = kg


# ---------------------------------------------------------------------
# Storylet Registry helpers
# ---------------------------------------------------------------------
def get_storylet_registry(vault_path: str = "default"):
    """Returns the StoryletRegistry for a vault, creating it if absent."""
    if vault_path not in _STORYLET_REGISTRIES:
        from storylet_registry import StoryletRegistry
        _STORYLET_REGISTRIES[vault_path] = StoryletRegistry()
    return _STORYLET_REGISTRIES[vault_path]


def clear_storylet_registry(vault_path: str = None) -> None:
    """Clears the storylet registry for a vault, or all if vault_path is None."""
    if vault_path:
        _STORYLET_REGISTRIES.pop(vault_path, None)
    else:
        _STORYLET_REGISTRIES.clear()
