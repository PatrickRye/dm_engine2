import uuid
from typing import Dict, Any, Optional

_ACTIVE_ENTITIES: Dict[uuid.UUID, Any] = {}

def register_entity(entity: Any) -> None:
    """Registers an entity into the global state."""
    if hasattr(entity, 'entity_uuid'):
        _ACTIVE_ENTITIES[entity.entity_uuid] = entity

def get_entity(uid: uuid.UUID) -> Optional[Any]:
    """Retrieves an entity by its UUID."""
    return _ACTIVE_ENTITIES.get(uid)

def remove_entity(uid: uuid.UUID) -> None:
    """Removes an entity from the global state by its UUID."""
    if uid in _ACTIVE_ENTITIES:
        del _ACTIVE_ENTITIES[uid]

def get_all_entities() -> Dict[uuid.UUID, Any]:
    """Returns a dictionary of all active entities."""
    return _ACTIVE_ENTITIES

def clear_registry() -> None:
    """Clears all entities from the global state."""
    _ACTIVE_ENTITIES.clear()