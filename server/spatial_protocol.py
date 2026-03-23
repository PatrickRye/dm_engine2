"""
Structural protocols (interfaces) for the Spatial Engine.
These allow tools.py and other modules to depend on clean interfaces
rather than concrete implementations.

Usage:
    from spatial_protocol import SpatialEngineProtocol
    def my_function(spatial: SpatialEngineProtocol): ...
"""

import uuid
from typing import Any, List, Tuple, Dict, Optional, Protocol


class SpatialEngineProtocol(Protocol):
    """
    The SpatialQueryService singleton exposes this interface.
    All spatial queries, entity management, and map operations
    flow through these methods.
    """

    # ── Map / Geometry ───────────────────────────────────────────────────────

    def get_map_data(self, vault_path: str = "default") -> Any: ...
    def reset_map_geometry(self, vault_path: str = "default") -> None: ...
    def load_map(self, vault_path: str, map_name: str, grid_scale: float = 5.0) -> None: ...

    def add_wall(self, wall: Any, vault_path: str = "default") -> None: ...
    def remove_wall(self, wall_id: uuid.UUID, vault_path: str = "default") -> None: ...
    def modify_wall(
        self,
        wall_id: uuid.UUID,
        label: Optional[str] = None,
        start: Optional[Tuple[float, float]] = None,
        end: Optional[Tuple[float, float]] = None,
        z: Optional[float] = None,
        height: Optional[float] = None,
        is_solid: Optional[bool] = None,
        is_visible: Optional[bool] = None,
        is_locked: Optional[bool] = None,
        vault_path: str = "default",
    ) -> None: ...
    def add_terrain(self, terrain: Any, vault_path: str = "default") -> None: ...
    def remove_terrain(self, terrain_id: uuid.UUID, vault_path: str = "default") -> None: ...

    # ── Entity Sync ──────────────────────────────────────────────────────────

    def sync_entity(
        self,
        entity: Any,
        vault_path: str = "default",
        current_map: str = "",
    ) -> None: ...
    def remove_entity(self, entity_uuid: uuid.UUID, vault_path: str = "default") -> None: ...
    def get_entity_terrain_zones(self, entity_uuid: uuid.UUID, vault_path: str = "default") -> List[Any]: ...

    # ── Spatial Queries ──────────────────────────────────────────────────────

    def calculate_distance(
        self, x1: float, y1: float, z1: float, x2: float, y2: float, z2: float, vault_path: str = "default"
    ) -> float: ...
    def get_entities_at_position(
        self, x: float, y: float, size: float, vault_path: str = "default", exclude_uuid: uuid.UUID = None
    ) -> List[uuid.UUID]: ...
    def get_entities_on_path(
        self, entity_uuid: uuid.UUID, speed: float, vault_path: str = "default"
    ) -> List[Tuple[float, float, float, float, float, str]]: ...
    def get_targets_in_radius(
        self, x: float, y: float, z: float, radius: float, vault_path: str = "default"
    ) -> List[uuid.UUID]: ...
    def get_targets_in_cone(
        self,
        origin_x: float,
        origin_y: float,
        target_x: float,
        target_y: float,
        length: float,
        angle_degrees: float = 60.0,
        vault_path: str = "default",
    ) -> List[uuid.UUID]: ...
    def get_aoe_targets(
        self,
        shape: str,
        size: float,
        origin_x: float,
        origin_y: float,
        target_x: float = None,
        target_y: float = None,
        origin_z: float = 0.0,
        target_z: float = None,
        aoe_height: float = None,
        ignore_walls: bool = False,
        penetrates_destructible: bool = False,
        vault_path: str = "default",
    ) -> Tuple[List[uuid.UUID], List[uuid.UUID], List[uuid.UUID]]: ...
    def get_distance_and_cover(
        self, source_uuid: uuid.UUID, target_uuid: uuid.UUID, vault_path: str = "default"
    ) -> Tuple[float, str]: ...
    def get_intervening_creatures(
        self, source_uuid: uuid.UUID, target_uuid: uuid.UUID, vault_path: str = "default"
    ) -> List[uuid.UUID]: ...
    def has_line_of_sight(
        self, source_uuid: uuid.UUID, target_uuid: uuid.UUID, vault_path: str = "default"
    ) -> bool: ...
    def has_line_of_sight_to_point(
        self,
        source_uuid: uuid.UUID,
        target_x: float,
        target_y: float,
        target_z: float = 0.0,
        vault_path: str = "default",
    ) -> bool: ...
    def check_path_collision(
        self,
        start_x: float,
        start_y: float,
        start_z: float,
        end_x: float,
        end_y: float,
        end_z: float,
        entity_height: float = 5.0,
        check_vision: bool = False,
        viewer_uuid: uuid.UUID = None,
        vault_path: str = "default",
    ) -> Optional[Any]: ...
    def get_illumination(
        self, target_x: float, target_y: float, target_z: float, vault_path: str = "default"
    ) -> str: ...
    def calculate_path_terrain_costs(
        self,
        start_x: float,
        start_y: float,
        start_z: float,
        end_x: float,
        end_y: float,
        end_z: float,
        vault_path: str = "default",
    ) -> Tuple[float, float]: ...
    def get_shape_points(
        self,
        shape: str,
        size: float,
        origin_x: float,
        origin_y: float,
        target_x: float = None,
        target_y: float = None,
    ) -> List[Tuple[float, float]]: ...

    # ── Fog of War / Rendering ──────────────────────────────────────────────

    def reveal_fog_of_war(self, x: float, y: float, radius: float, vault_path: str = "default") -> None: ...
    def render_ascii_map(self, vault_path: str = "default", width: int = 40, height: int = 20) -> str: ...
    def get_illusion_walls(self, vault_path: str = "default") -> List[Any]: ...
