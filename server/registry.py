import threading
import uuid
from typing import Dict, Any, Optional

_ENTITY_LOCK = threading.RLock()

_ACTIVE_ENTITIES: Dict[str, Dict[uuid.UUID, Any]] = {}
_NAME_INDEX: Dict[str, Dict[str, uuid.UUID]] = {}
# Prefix index: vault_path -> first-3-chars (lowercase) -> set of entity UUIDs
_PREFIX_INDEX: Dict[str, Dict[str, set]] = {}

# Per-vault Knowledge Graph and Storylet Registry instances
_KNOWLEDGE_GRAPHS: Dict[str, Any] = {}  # vault_path -> KnowledgeGraph
_STORYLET_REGISTRIES: Dict[str, Any] = {}  # vault_path -> StoryletRegistry
_KG_LOCK = threading.RLock()
_STORYLET_LOCK = threading.RLock()


def register_entity(entity: Any, vault_path: str = "default") -> None:
    """Registers an entity into the global state. Thread-safe."""
    vp = getattr(entity, "vault_path", vault_path)
    if not vp:
        vp = "default"

    with _ENTITY_LOCK:
        if vp not in _ACTIVE_ENTITIES:
            _ACTIVE_ENTITIES[vp] = {}
            _NAME_INDEX[vp] = {}
            _PREFIX_INDEX[vp] = {}
        else:
            # Ensure PREFIX_INDEX exists even if vault was created manually (e.g. test setup)
            if vp not in _PREFIX_INDEX:
                _PREFIX_INDEX[vp] = {}

        if hasattr(entity, "entity_uuid"):
            _ACTIVE_ENTITIES[vp][entity.entity_uuid] = entity
            if hasattr(entity, "name"):
                name_lower = entity.name.lower()
                _NAME_INDEX[vp][name_lower] = entity.entity_uuid
                # Populate prefix index (first 3 chars)
                if len(name_lower) >= 3:
                    prefix = name_lower[:3]
                    if prefix not in _PREFIX_INDEX[vp]:
                        _PREFIX_INDEX[vp][prefix] = set()
                    _PREFIX_INDEX[vp][prefix].add(entity.entity_uuid)


def get_entity(uid: uuid.UUID, vault_path: str = None) -> Optional[Any]:
    """Retrieves an entity by its UUID. Thread-safe."""
    with _ENTITY_LOCK:
        if vault_path:
            return _ACTIVE_ENTITIES.get(vault_path, {}).get(uid)

        for vp, vault in _ACTIVE_ENTITIES.items():
            if uid in vault:
                return vault[uid]
        return None


def remove_entity(uid: uuid.UUID) -> None:
    """Removes an entity from the global state by its UUID. Thread-safe."""
    with _ENTITY_LOCK:
        for vp, vault in _ACTIVE_ENTITIES.items():
            if uid in vault:
                ent = vault[uid]
                if hasattr(ent, "name"):
                    name_lower = ent.name.lower()
                    if name_lower in _NAME_INDEX.get(vp, {}):
                        del _NAME_INDEX[vp][name_lower]
                    if len(name_lower) >= 3:
                        prefix = name_lower[:3]
                        _PREFIX_INDEX.get(vp, {}).get(prefix, set()).discard(uid)
                del vault[uid]
                break


def get_all_entities(vault_path: str = "default") -> Dict[uuid.UUID, Any]:
    """Returns a dictionary of all active entities. Thread-safe."""
    with _ENTITY_LOCK:
        return _ACTIVE_ENTITIES.get(vault_path, {})


def get_candidate_uuids_by_prefix(name_lower: str, vault_path: str) -> set:
    """
    Return entity UUIDs whose names share the first 3 characters with name_lower.
    Used to narrow entity search before full substring comparison.
    Returns an empty set if name_lower has fewer than 3 chars.
    Thread-safe.
    """
    with _ENTITY_LOCK:
        if len(name_lower) < 3 or vault_path not in _PREFIX_INDEX:
            return set()
        prefix = name_lower[:3]
        return set(_PREFIX_INDEX.get(vault_path, {}).get(prefix, set()))


def clear_registry(vault_path: str = None) -> None:
    """Clears all entities from the global state. Thread-safe."""
    with _ENTITY_LOCK:
        if vault_path:
            if vault_path in _ACTIVE_ENTITIES:
                _ACTIVE_ENTITIES[vault_path].clear()
            if vault_path in _NAME_INDEX:
                _NAME_INDEX[vault_path].clear()
            if vault_path in _PREFIX_INDEX:
                _PREFIX_INDEX[vault_path].clear()
        else:
            _ACTIVE_ENTITIES.clear()
            _NAME_INDEX.clear()
            _PREFIX_INDEX.clear()


# ---------------------------------------------------------------------
# Knowledge Graph helpers
# ---------------------------------------------------------------------
def get_knowledge_graph(vault_path: str = "default"):
    """Returns the KnowledgeGraph for a vault, creating it if absent. Thread-safe."""
    with _KG_LOCK:
        if vault_path not in _KNOWLEDGE_GRAPHS:
            from knowledge_graph import KnowledgeGraph
            _KNOWLEDGE_GRAPHS[vault_path] = KnowledgeGraph()
        return _KNOWLEDGE_GRAPHS[vault_path]


def set_knowledge_graph(vault_path: str, kg: Any) -> None:
    """Replaces the KnowledgeGraph for a vault. Thread-safe."""
    with _KG_LOCK:
        _KNOWLEDGE_GRAPHS[vault_path] = kg


# ---------------------------------------------------------------------
# Storylet Registry helpers
# ---------------------------------------------------------------------
def get_storylet_registry(vault_path: str = "default"):
    """Returns the StoryletRegistry for a vault, creating it if absent. Thread-safe."""
    with _STORYLET_LOCK:
        if vault_path not in _STORYLET_REGISTRIES:
            from storylet_registry import StoryletRegistry
            _STORYLET_REGISTRIES[vault_path] = StoryletRegistry()
        return _STORYLET_REGISTRIES[vault_path]


def clear_storylet_registry(vault_path: str = None) -> None:
    """Clears the storylet registry for a vault, or all if vault_path is None. Thread-safe."""
    with _STORYLET_LOCK:
        if vault_path:
            _STORYLET_REGISTRIES.pop(vault_path, None)
        else:
            _STORYLET_REGISTRIES.clear()
