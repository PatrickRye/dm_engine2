import threading
import uuid
import math
from collections import OrderedDict
from typing import Any, List, Tuple, Dict, Optional, Protocol, ClassVar
from pydantic import BaseModel, Field, PrivateAttr


class SpatialObject(Protocol):
    entity_uuid: uuid.UUID
    x: float
    y: float
    z: float
    size: float
    height: float


try:
    from shapely.geometry import Point, LineString, Polygon, box
    from shapely.ops import unary_union
    from rtree import index

    HAS_GIS = True
except ImportError:
    HAS_GIS = False
    print("WARNING: shapely and rtree are required for the spatial engine. Run: pip install shapely rtree")


class TrapDefinition(BaseModel):
    hazard_name: str
    requires_attack_roll: bool = False
    attack_bonus: int = 5
    save_required: str = ""
    save_dc: int = 15
    damage_dice: str = ""
    damage_type: str = ""
    half_damage_on_save: bool = True
    condition_applied: str = ""
    trigger_on_interact_fail: bool = False
    trigger_on_move: bool = False
    trigger_on_turn_start: bool = False
    is_persistent: bool = False
    is_disarmable: bool = True
    radius: float = 0.0
    is_active: bool = True
    known_by_players: bool = False


class Wall(BaseModel):
    wall_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    label: str = "wall"
    start: Tuple[float, float]
    end: Tuple[float, float]
    z: float = 0.0
    height: float = 10.0
    is_solid: bool = True
    is_visible: bool = True
    is_locked: bool = False
    interact_dc: Optional[int] = None
    trap: Optional[TrapDefinition] = None

    # REQ-ILL: Illusion fields
    is_illusion: bool = False      # True = illusion wall (blocks LOS/perception, not attacks)
    is_phantasm: bool = False      # True = mental only (physical pass-through doesn't auto-reveal)
    illusion_spell_dc: int = 0     # DC for Investigation check to disbelieve
    revealed_for: List[str] = Field(default_factory=list)  # entity UUID strings that have seen through it

    hp: Optional[int] = None
    max_hp: Optional[int] = None
    ac: int = 10
    damage_threshold: int = 0
    immunities: List[str] = Field(default_factory=list)
    resistances: List[str] = Field(default_factory=list)
    vulnerabilities: List[str] = Field(default_factory=list)

    @property
    def line(self):
        if not HAS_GIS:
            return None
        return LineString([self.start, self.end])


class TerrainZone(BaseModel):
    zone_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    label: str = "terrain"
    points: List[Tuple[float, float]]
    z: float = 0.0
    height: float = 0.0
    is_difficult: bool = True
    trap: Optional[TrapDefinition] = None
    tags: List[str] = Field(default_factory=list)

    source_name: str = ""
    source_uuid: Optional[uuid.UUID] = None
    duration_seconds: int = -1
    applied_initiative: int = 0

    # Cached Shapely geometry — points are immutable after creation
    _cached_polygon: Any = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        if HAS_GIS and len(self.points) >= 3:
            self._cached_polygon = Polygon(self.points)

    @property
    def polygon(self):
        if not HAS_GIS or len(self.points) < 3:
            return None
        return self._cached_polygon


class LightSource(BaseModel):
    source_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    label: str = "light source"
    x: float
    y: float
    z: float = 5.0
    bright_radius: float
    dim_radius: float
    attached_to_entity_uuid: Optional[uuid.UUID] = None


class MapData(BaseModel):
    map_name: str = "Active Map"
    dm_map_image_path: Optional[str] = None
    player_map_image_path: Optional[str] = None
    explored_areas: List[Tuple[float, float, float]] = Field(default_factory=list)  # x, y, radius
    fow_disabled_for: List[str] = Field(default_factory=list)

    original_walls: List[Wall] = Field(default_factory=list)
    walls: List[Wall] = Field(default_factory=list)
    temporary_walls: List[Wall] = Field(default_factory=list)

    original_terrain: List[TerrainZone] = Field(default_factory=list)
    terrain: List[TerrainZone] = Field(default_factory=list)
    temporary_terrain: List[TerrainZone] = Field(default_factory=list)

    original_lights: List[LightSource] = Field(default_factory=list)
    lights: List[LightSource] = Field(default_factory=list)
    temporary_lights: List[LightSource] = Field(default_factory=list)

    grid_scale: float = 5.0  # 1 square = 5ft
    distance_metric: str = "chebyshev"  # "chebyshev" or "euclidean"
    pixels_per_foot: float = 1.0

    @property
    def active_walls(self) -> List[Wall]:
        return self.walls + self.temporary_walls

    @property
    def active_terrain(self) -> List[TerrainZone]:
        return self.terrain + self.temporary_terrain

    @property
    def active_lights(self) -> List[LightSource]:
        return self.lights + self.temporary_lights


class SpatialQueryService:
    """
    A headless, deterministic spatial engine utilizing GIS libraries.
    Provides mathematical answers to mechanical game logic without rendering.

    Thread-safe: all mutable state is protected by a class-level reentrant lock.
    The lock is acquired for the full duration of every public method that
    reads or writes shared state, preventing races when concurrent async
    dispatches (via asyncio.to_thread) touch the same vault from multiple threads.
    """

    # Shared reentrant lock — all instances share the same lock since this is a singleton.
    # RLock allows reentrant acquisition so dispatch -> handler -> dispatch cycles don't deadlock.
    _lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self):
        self._map_data: Dict[str, MapData] = {}
        self.active_paths: Dict[str, Dict[str, dict]] = {}  # Maps vault_path -> entity_name -> path_data
        self.active_combatants: Dict[str, List[str]] = {}  # Maps vault_path -> [combatant_names]
        if HAS_GIS:
            self.entity_idx: Dict[str, index.Index] = {}
            self.wall_idx: Dict[str, index.Index] = {}
            self.terrain_idx: Dict[str, index.Index] = {}
            self.light_idx: Dict[str, index.Index] = {}  # R-tree for static (non-attached) lights
            self._entities: Dict[str, Dict[uuid.UUID, SpatialObject]] = {}
            self._uuid_to_id: Dict[str, Dict[uuid.UUID, int]] = {}
            self._id_to_uuid: Dict[str, Dict[int, uuid.UUID]] = {}
            self._uuid_to_bbox: Dict[str, Dict[uuid.UUID, Tuple[float, float, float, float]]] = {}
            self._uuid_to_shape: Dict[str, Dict[uuid.UUID, Any]] = {}  # Cached Shapely boxes per entity
            self._wall_map: Dict[str, Dict[int, Wall]] = {}
            self._terrain_map: Dict[str, Dict[int, TerrainZone]] = {}
            self._light_map: Dict[str, Dict[int, LightSource]] = {}  # R-tree id -> LightSource
            self._next_id: Dict[str, int] = {}
            # OrderedDict preserves insertion order for FIFO eviction (evict oldest 20% at limit)
            self._raycast_cache: Dict[str, OrderedDict] = {}
            # Cached unary_union of ground-level difficult terrain for fast path-cost queries
            self._terrain_union_cache: Dict[str, Any] = {}
            # Per-entity list of terrain zones currently occupied (updated in sync_entity)
            self._entity_terrain_occupancy: Dict[str, Dict[uuid.UUID, List[TerrainZone]]] = {}

    @property
    def map_data(self) -> MapData:
        """Fallback helper for backward compatibility in test suites."""
        return self.get_map_data("default")

    @map_data.setter
    def map_data(self, value: MapData):
        self._map_data["default"] = value

    def get_map_data(self, vault_path: str = "default") -> MapData:
        if not vault_path:
            vault_path = "default"
        if vault_path not in self._map_data:
            self._map_data[vault_path] = MapData()
            self.active_paths[vault_path] = {}
            if HAS_GIS:
                p = index.Property()
                self.entity_idx[vault_path] = index.Index(properties=p)
                self.wall_idx[vault_path] = index.Index(properties=p)
                self.terrain_idx[vault_path] = index.Index(properties=p)
                self.light_idx[vault_path] = index.Index(properties=p)
                self._entities[vault_path] = {}
                self._uuid_to_id[vault_path] = {}
                self._id_to_uuid[vault_path] = {}
                self._uuid_to_bbox[vault_path] = {}
                self._uuid_to_shape[vault_path] = {}
                self._wall_map[vault_path] = {}
                self._terrain_map[vault_path] = {}
                self._light_map[vault_path] = {}
                self._next_id[vault_path] = 0
                self._raycast_cache[vault_path] = OrderedDict()
                self._terrain_union_cache[vault_path] = None
                self._entity_terrain_occupancy[vault_path] = {}
        return self._map_data[vault_path]

    def clear(self, vault_path: str = None):  # noqa: C901
        """Completely resets the spatial engine and Rtree index for testing."""
        if vault_path:
            if vault_path in self._map_data:
                del self._map_data[vault_path]
            if vault_path in self.active_paths:
                del self.active_paths[vault_path]
            if vault_path in self.active_combatants:
                del self.active_combatants[vault_path]
            if HAS_GIS:
                for d in (
                    self.entity_idx, self.wall_idx, self.terrain_idx, self.light_idx,
                    self._entities, self._uuid_to_id, self._id_to_uuid,
                    self._uuid_to_bbox, self._uuid_to_shape,
                    self._wall_map, self._terrain_map, self._light_map,
                    self._next_id, self._raycast_cache,
                    self._terrain_union_cache, self._entity_terrain_occupancy,
                ):
                    d.pop(vault_path, None)
        else:
            self._map_data.clear()
            self.active_paths.clear()
            self.active_combatants.clear()
            if HAS_GIS:
                self.entity_idx.clear()
                self.wall_idx.clear()
                self.terrain_idx.clear()
                self.light_idx.clear()
                self._entities.clear()
                self._uuid_to_id.clear()
                self._id_to_uuid.clear()
                self._uuid_to_bbox.clear()
                self._uuid_to_shape.clear()
                self._wall_map.clear()
                self._terrain_map.clear()
                self._light_map.clear()
                self._next_id.clear()
                self._raycast_cache.clear()
                self._terrain_union_cache.clear()
                self._entity_terrain_occupancy.clear()

    def get_wall_by_id(self, wall_id: uuid.UUID, vault_path: str = "default") -> Optional[Wall]:
        """Retrieves a specific wall object from the active layers."""
        for w in self.get_map_data(vault_path).active_walls:
            if w.wall_id == wall_id:
                return w
        return None

    def get_terrain_by_id(self, zone_id: uuid.UUID, vault_path: str = "default") -> Optional[TerrainZone]:
        for tz in self.get_map_data(vault_path).active_terrain:
            if tz.zone_id == zone_id:
                return tz
        return None

    def reveal_fog_of_war(self, x: float, y: float, radius: float, vault_path: str = "default"):
        """Adds a circular area to the explored regions of the current map."""
        with self._lock:
            qx, qy, qr = round(x, 1), round(y, 1), round(radius, 1)
            md = self.get_map_data(vault_path)
            for ex, ey, er in md.explored_areas:
                dist = math.hypot(qx - ex, qy - ey)
                if dist + qr <= er:
                    return

            md.explored_areas.append((qx, qy, qr))

    def _rebuild_indices(self, vault_path: str = "default"):
        """Rebuilds all R-tree indices and derived caches when map geometry changes."""
        if not HAS_GIS:
            return
        md = self.get_map_data(vault_path)
        p = index.Property()

        # --- Walls ---
        self.wall_idx[vault_path] = index.Index(properties=p)
        self._wall_map[vault_path].clear()
        for i, w in enumerate(md.active_walls):
            if w.line:
                self.wall_idx[vault_path].insert(i, w.line.bounds)
                self._wall_map[vault_path][i] = w

        # --- Terrain ---
        self.terrain_idx[vault_path] = index.Index(properties=p)
        self._terrain_map[vault_path].clear()
        ground_difficult_polys = []
        for i, t in enumerate(md.active_terrain):
            if t.polygon:
                self.terrain_idx[vault_path].insert(i, t.polygon.bounds)
                self._terrain_map[vault_path][i] = t
                # Collect ground-level difficult terrain for the union cache
                if t.is_difficult and t.z <= 0.1 and t.height <= 1.0:
                    ground_difficult_polys.append(t.polygon)

        # Pre-compute terrain union for flat (ground-level) path cost queries
        self._terrain_union_cache[vault_path] = (
            unary_union(ground_difficult_polys) if ground_difficult_polys else None
        )

        # --- Static Lights (non-entity-attached) ---
        self.light_idx[vault_path] = index.Index(properties=p)
        self._light_map[vault_path].clear()
        for i, light in enumerate(md.active_lights):
            if not light.attached_to_entity_uuid:
                r = light.dim_radius
                self.light_idx[vault_path].insert(i, (light.x - r, light.y - r, light.x + r, light.y + r))
                self._light_map[vault_path][i] = light

        # Invalidate caches that depend on geometry
        self._raycast_cache.setdefault(vault_path, OrderedDict()).clear()
        # Entity-terrain occupancy is now stale — will be refreshed on next sync_entity calls
        self._entity_terrain_occupancy.setdefault(vault_path, {}).clear()

    def invalidate_cache(self, vault_path: str = "default"):
        """Clears the raycast cache and rebuilds spatial geometry indices when map geometry changes."""
        if HAS_GIS:
            self.get_map_data(vault_path)
            self._rebuild_indices(vault_path)

    def add_wall(self, wall: Wall, is_temporary: bool = False, vault_path: str = "default"):
        """Dynamically adds a wall to the current or temporary map."""
        md = self.get_map_data(vault_path)
        if is_temporary:
            md.temporary_walls.append(wall)
        else:
            md.walls.append(wall)
        self.invalidate_cache(vault_path)

    def remove_wall(self, wall_id: uuid.UUID, vault_path: str = "default"):
        """Removes a wall from the dynamic or temporary layers."""
        md = self.get_map_data(vault_path)
        initial_count = len(md.walls) + len(md.temporary_walls)
        md.walls = [w for w in md.walls if w.wall_id != wall_id]
        md.temporary_walls = [w for w in md.temporary_walls if w.wall_id != wall_id]
        if len(md.walls) + len(md.temporary_walls) < initial_count:
            self.invalidate_cache(vault_path)

    def add_terrain(self, terrain: TerrainZone, is_temporary: bool = False, vault_path: str = "default"):
        md = self.get_map_data(vault_path)
        if is_temporary:
            md.temporary_terrain.append(terrain)
        else:
            md.terrain.append(terrain)
        self.invalidate_cache(vault_path)

    def remove_terrain(self, zone_id: uuid.UUID, vault_path: str = "default"):
        md = self.get_map_data(vault_path)
        initial_count = len(md.terrain) + len(md.temporary_terrain)
        md.terrain = [t for t in md.terrain if t.zone_id != zone_id]
        md.temporary_terrain = [t for t in md.temporary_terrain if t.zone_id != zone_id]
        if len(md.terrain) + len(md.temporary_terrain) < initial_count:
            self.invalidate_cache(vault_path)

    def modify_wall(
        self,
        wall_id: uuid.UUID,
        is_solid: Optional[bool] = None,
        is_visible: Optional[bool] = None,
        is_locked: Optional[bool] = None,
        vault_path: str = "default",
    ):
        """Dynamically updates a wall's state (e.g., opening a door or destroying a wall)."""
        for wall in self.get_map_data(vault_path).active_walls:
            if wall.wall_id == wall_id:
                if is_solid is not None:
                    wall.is_solid = is_solid
                if is_visible is not None:
                    wall.is_visible = is_visible
                if is_locked is not None:
                    wall.is_locked = is_locked
                self.invalidate_cache(vault_path)
                return

    def reset_map_geometry(self, vault_path: str = "default"):
        """Restores the current map to the original base map, wiping all damage/doors/temporary effects."""
        md = self.get_map_data(vault_path)
        md.walls = [w.model_copy(deep=True) for w in md.original_walls]
        md.temporary_walls.clear()
        self.invalidate_cache(vault_path)

    def _entity_on_active_map(self, entity: SpatialObject, vault_path: str) -> bool:
        """Returns True if the entity belongs on the vault's currently active map.

        Entities with current_map=="" are treated as being on whatever map is active
        (backward-compat for entities created before multi-map support).  Entities with
        a specific current_map are only present on that named map.
        """
        current_map = getattr(entity, "current_map", "")
        if not current_map:
            return True  # unassigned — appears on any/default active map
        active_map = self.get_map_data(vault_path).map_name or ""
        if not active_map:
            return True  # no named map loaded yet; don't filter anyone out
        return current_map == active_map

    def load_map(self, map_data: MapData, vault_path: str = "default"):
        if not map_data.original_walls and map_data.walls:
            map_data.original_walls = [wall.model_copy(deep=True) for wall in map_data.walls]
        if not map_data.original_terrain and map_data.terrain:
            map_data.original_terrain = [terrain.model_copy(deep=True) for terrain in map_data.terrain]
        if not map_data.original_lights and map_data.lights:
            map_data.original_lights = [light.model_copy(deep=True) for light in map_data.lights]

        self.get_map_data(vault_path)
        self._map_data[vault_path] = map_data
        self.invalidate_cache(vault_path)

        # Re-sync every registered entity so map-specific entities appear/disappear correctly.
        if vault_path in self._entities:
            for entity in list(self._entities[vault_path].values()):
                self.sync_entity(entity)

    def sync_entity(self, entity: SpatialObject):
        """Adds or updates an entity's bounding box in the Rtree spatial index.

        Entities whose current_map doesn't match the vault's active map are kept in
        _entities (so registry/name lookups still work) but removed from the spatial
        index so they don't affect movement, LOS, or collision queries.
        """
        if not HAS_GIS:
            return

        vp = getattr(entity, "vault_path", "default")
        if not vp:
            vp = "default"
        self.get_map_data(vp)

        # Always store in _entities so the registry can find the entity by name/UUID.
        self._entities[vp][entity.entity_uuid] = entity

        # If this entity is assigned to a different map, remove it from the spatial
        # index (if present) and return early — it won't affect spatial queries.
        if not self._entity_on_active_map(entity, vp):
            if entity.entity_uuid in self._uuid_to_id[vp]:
                curr_id = self._uuid_to_id[vp][entity.entity_uuid]
                old_bbox = self._uuid_to_bbox[vp].get(entity.entity_uuid)
                if old_bbox:
                    self.entity_idx[vp].delete(curr_id, old_bbox)
                del self._id_to_uuid[vp][curr_id]
                del self._uuid_to_id[vp][entity.entity_uuid]
                self._uuid_to_bbox[vp].pop(entity.entity_uuid, None)
                self._uuid_to_shape[vp].pop(entity.entity_uuid, None)
                self._entity_terrain_occupancy.get(vp, {}).pop(entity.entity_uuid, None)
            return

        new_bbox = self._get_bbox(entity)
        new_shape = box(*new_bbox)

        if entity.entity_uuid not in self._uuid_to_id[vp]:
            curr_id = self._next_id[vp]
            self._uuid_to_id[vp][entity.entity_uuid] = curr_id
            self._id_to_uuid[vp][curr_id] = entity.entity_uuid
            self._next_id[vp] += 1
        else:
            curr_id = self._uuid_to_id[vp][entity.entity_uuid]
            old_bbox = self._uuid_to_bbox[vp].get(entity.entity_uuid)
            if old_bbox:
                self.entity_idx[vp].delete(curr_id, old_bbox)

        self.entity_idx[vp].insert(curr_id, new_bbox)
        self._uuid_to_bbox[vp][entity.entity_uuid] = new_bbox
        self._uuid_to_shape[vp][entity.entity_uuid] = new_shape  # Cache Shapely box

        # Update terrain occupancy — which zones is this entity currently standing in?
        if vp in self.terrain_idx and self._terrain_map.get(vp):
            zone_candidates = list(self.terrain_idx[vp].intersection(new_bbox))
            occupying = []
            for cid in zone_candidates:
                zone = self._terrain_map[vp].get(cid)
                if zone and zone.polygon and new_shape.intersects(zone.polygon):
                    occupying.append(zone)
            self._entity_terrain_occupancy[vp][entity.entity_uuid] = occupying

    def remove_entity(self, entity_uuid: uuid.UUID, vault_path: str = "default"):
        """Removes an entity from the spatial index."""
        vp = vault_path
        if not HAS_GIS or vp not in self._uuid_to_id or entity_uuid not in self._uuid_to_id[vp]:
            return

        curr_id = self._uuid_to_id[vp][entity_uuid]
        old_bbox = self._uuid_to_bbox[vp].get(entity_uuid)
        if old_bbox:
            self.entity_idx[vp].delete(curr_id, old_bbox)
        del self._id_to_uuid[vp][curr_id]
        del self._uuid_to_id[vp][entity_uuid]
        del self._uuid_to_bbox[vp][entity_uuid]
        self._uuid_to_shape[vp].pop(entity_uuid, None)
        self._entity_terrain_occupancy.get(vp, {}).pop(entity_uuid, None)
        if entity_uuid in self._entities[vp]:
            del self._entities[vp][entity_uuid]

    def get_entity_terrain_zones(self, entity_uuid: uuid.UUID, vault_path: str = "default") -> List[TerrainZone]:
        """Returns the list of terrain zones the entity is currently occupying (O(1) lookup)."""
        return self._entity_terrain_occupancy.get(vault_path, {}).get(entity_uuid, [])

    def get_entities_at_position(
        self,
        x: float,
        y: float,
        size: float,
        vault_path: str = "default",
        exclude_uuid: Optional[uuid.UUID] = None,
    ) -> list:
        """REQ-GEO-006: Returns entities whose bounding box meaningfully overlaps the given position+size box.
        Edge-touching is NOT counted as occupation (area must be > 0). exclude_uuid skips that entity."""
        if not HAS_GIS:
            return []
        self.get_map_data(vault_path)
        if vault_path not in self.entity_idx:
            return []
        half = size / 2.0
        query_bbox = (x - half, y - half, x + half, y + half)
        query_shape = box(x - half, y - half, x + half, y + half)
        candidates = list(self.entity_idx[vault_path].intersection(query_bbox))
        result = []
        for cid in candidates:
            uid = self._id_to_uuid[vault_path].get(cid)
            if uid is None or uid == exclude_uuid:
                continue
            entity = self._entities[vault_path].get(uid)
            if entity is None:
                continue
            entity_shape = self._uuid_to_shape[vault_path].get(uid)
            if entity_shape is None:
                entity_shape = box(*self._get_bbox(entity))
            # Use area > 0: edge-touching is legal (adjacent), actual overlap is not (REQ-GEO-006)
            if query_shape.intersection(entity_shape).area > 0:
                result.append(entity)
        return result

    def get_illusion_walls(self, vault_path: str = "default") -> List[Wall]:
        """Returns all illusion walls currently in the map (REQ-ILL)."""
        return [w for w in self.get_map_data(vault_path).active_walls if w.is_illusion]

    def get_entities_on_path(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        vault_path: str = "default",
        exclude_uuid: Optional[uuid.UUID] = None,
    ) -> list:
        """REQ-GEO-007/008: Returns (entity, overlap_length) pairs for living entities whose bounding box
        the path line passes through with non-zero intersection length."""
        if not HAS_GIS:
            return []
        self.get_map_data(vault_path)
        if vault_path not in self.entity_idx:
            return []
        path = LineString([(start_x, start_y), (end_x, end_y)])
        if path.length == 0:
            return []
        candidates = list(self.entity_idx[vault_path].intersection(path.bounds))
        result = []
        for cid in candidates:
            uid = self._id_to_uuid[vault_path].get(cid)
            if uid is None or uid == exclude_uuid:
                continue
            entity = self._entities[vault_path].get(uid)
            if entity is None:
                continue
            entity_shape = self._uuid_to_shape[vault_path].get(uid)
            if entity_shape is None:
                entity_shape = box(*self._get_bbox(entity))
            inter = path.intersection(entity_shape)
            if not inter.is_empty and inter.length > 0:
                result.append((entity, inter.length))
        return result

    def calculate_distance(
        self, x1: float, y1: float, z1: float, x2: float, y2: float, z2: float, vault_path: str = "default"
    ) -> float:
        """Calculates 3D distance using the map's configured metric."""
        dx = abs(x1 - x2)
        dy = abs(y1 - y2)
        dz = abs(z1 - z2)
        if self.get_map_data(vault_path).distance_metric == "chebyshev":
            return max(dx, dy, dz)
        else:
            return math.hypot(dx, dy, dz)

    def _get_bbox(self, entity: SpatialObject) -> Tuple[float, float, float, float]:
        half_size = entity.size / 2.0
        return (entity.x - half_size, entity.y - half_size, entity.x + half_size, entity.y + half_size)

    def _get_entity_bbox(self, entity: SpatialObject):
        """Returns a cached Shapely box for the entity, or constructs one on first access."""
        if not HAS_GIS:
            return None
        vp = getattr(entity, "vault_path", "default") or "default"
        cached = self._uuid_to_shape.get(vp, {}).get(entity.entity_uuid)
        if cached is not None:
            return cached
        # Fallback for entities not yet synced (e.g., during construction)
        return box(*self._get_bbox(entity))

    def _get_entity_spatial_hash(self, entity: SpatialObject) -> Tuple:
        """Quantizes an entity's spatial state to 1 decimal place (~1.2 inches) for cache keys."""
        return (
            round(entity.x, 1),
            round(entity.y, 1),
            round(entity.z, 1),
            round(entity.size, 1),
            round(getattr(entity, "height", 5.0), 1),
        )

    def _cache_set(self, vault_path: str, key: Tuple, value: Any):
        cache = self._raycast_cache.setdefault(vault_path, OrderedDict())
        if len(cache) >= 10000:
            # Evict oldest 20% to keep hot entries warm rather than clearing everything
            evict_count = 2000
            for _ in range(evict_count):
                try:
                    cache.popitem(last=False)
                except KeyError:
                    break
        cache[key] = value

    def get_targets_in_radius(
        self, origin_x: float, origin_y: float, radius: float, vault_path: str = "default"
    ) -> List[uuid.UUID]:
        """Resolves circular Area of Effect (AoE) like Fireball perfectly."""
        if not HAS_GIS:
            return []
        self.get_map_data(vault_path)

        if self.get_map_data(vault_path).distance_metric == "chebyshev":
            search_area = box(origin_x - radius, origin_y - radius, origin_x + radius, origin_y + radius)
        else:
            search_area = Point(origin_x, origin_y).buffer(radius)

        candidate_ids = list(self.entity_idx[vault_path].intersection(search_area.bounds))

        hit_uuids = []
        for cid in candidate_ids:
            ent_uuid = self._id_to_uuid[vault_path][cid]
            entity = self._entities[vault_path].get(ent_uuid)
            if entity:
                # search_area is already the exact shape (circle or square), so intersects() is sufficient
                if search_area.intersects(self._get_entity_bbox(entity)):
                    hit_uuids.append(ent_uuid)

        return hit_uuids

    def get_targets_in_cone(
        self,
        origin_x: float,
        origin_y: float,
        target_x: float,
        target_y: float,
        length: float,
        angle_degrees: float = 60.0,
        vault_path: str = "default",
    ) -> List[uuid.UUID]:
        """Resolves cone Area of Effect (like Burning Hands)."""
        if not HAS_GIS:
            return []
        self.get_map_data(vault_path)

        base_angle = math.degrees(math.atan2(target_y - origin_y, target_x - origin_x))
        start_angle = math.radians(base_angle - (angle_degrees / 2))
        end_angle = math.radians(base_angle + (angle_degrees / 2))

        points = [(origin_x, origin_y)]
        num_segments = max(4, int(angle_degrees / 15))
        for i in range(num_segments + 1):
            theta = start_angle + i * (end_angle - start_angle) / num_segments
            points.append((origin_x + length * math.cos(theta), origin_y + length * math.sin(theta)))

        cone_poly = Polygon(points)
        candidate_ids = list(self.entity_idx[vault_path].intersection(cone_poly.bounds))

        hit_uuids = []
        for cid in candidate_ids:
            ent_uuid = self._id_to_uuid[vault_path][cid]
            entity = self._entities[vault_path].get(ent_uuid)
            if entity:
                if cone_poly.intersects(self._get_entity_bbox(entity)):
                    hit_uuids.append(ent_uuid)

        return hit_uuids

    def get_aoe_targets(  # noqa: C901
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
    ) -> Tuple[List[uuid.UUID], List[uuid.UUID], List[uuid.UUID]]:
        """Returns all valid Entities, Walls, and Terrain hit by an AoE, enforcing Line of Effect."""
        if not HAS_GIS:
            return [], [], []
        if target_z is None:
            target_z = origin_z
        self.get_map_data(vault_path)

        hit_entities = []
        hit_walls = []
        hit_terrains = []
        shape = shape.lower()
        aoe_poly = None
        vx, vy, vz = 0.0, 0.0, 0.0

        if shape in ["circle", "sphere", "cylinder"]:
            aoe_poly = Point(origin_x, origin_y).buffer(size)
        elif shape == "cube":
            aoe_poly = box(origin_x - size / 2, origin_y - size / 2, origin_x + size / 2, origin_y + size / 2)
        elif shape in ["cone", "line"]:
            if target_x is None or target_y is None:
                return [], [], []

            dx = target_x - origin_x
            dy = target_y - origin_y
            dz = target_z - origin_z
            dist_3d = math.sqrt(dx**2 + dy**2 + dz**2)
            if dist_3d > 0:
                vx, vy, vz = dx / dist_3d, dy / dist_3d, dz / dist_3d
            else:
                vx, vy, vz = 1.0, 0.0, 0.0

            if shape == "cone":
                if vx == 0 and vy == 0:
                    aoe_poly = Point(origin_x, origin_y).buffer(size * 0.5)
                else:
                    angle_deg = 53.1
                    angle_xy = math.atan2(vy, vx)
                    start_angle = angle_xy - math.radians(angle_deg / 2)
                    end_angle = angle_xy + math.radians(angle_deg / 2)

                    points = [(origin_x, origin_y)]
                    num_segments = max(4, int(angle_deg / 15))
                    for i in range(num_segments + 1):
                        theta = start_angle + i * (end_angle - start_angle) / num_segments
                        points.append((origin_x + size * math.cos(theta), origin_y + size * math.sin(theta)))
                    aoe_poly = Polygon(points)
            else:  # Line
                ex = origin_x + vx * size
                ey = origin_y + vy * size
                if origin_x == ex and origin_y == ey:
                    aoe_poly = Point(origin_x, origin_y).buffer(2.5)
                else:
                    aoe_poly = LineString([(origin_x, origin_y), (ex, ey)]).buffer(2.5)

        if not aoe_poly:
            return [], [], []

        candidate_ids = list(self.entity_idx[vault_path].intersection(aoe_poly.bounds))
        for cid in candidate_ids:
            ent_uuid = self._id_to_uuid[vault_path][cid]
            entity = self._entities[vault_path].get(ent_uuid)
            if entity and aoe_poly.intersects(self._get_entity_bbox(entity)):
                # Hoist height to avoid repeated getattr calls
                ent_height = getattr(entity, "height", 5.0)
                ent_min_z = entity.z
                ent_max_z = entity.z + ent_height
                ent_center_z = entity.z + (ent_height / 2.0)

                is_hit = True
                if shape == "sphere":
                    dist = self.calculate_distance(origin_x, origin_y, origin_z, entity.x, entity.y, ent_center_z, vault_path)
                    if dist - (entity.size / 2.0) > size:
                        is_hit = False
                elif shape == "cube":
                    half_size = size / 2.0
                    min_cube_z = origin_z - half_size
                    max_cube_z = origin_z + half_size
                    if ent_max_z < min_cube_z or ent_min_z > max_cube_z:
                        is_hit = False
                elif shape == "cylinder":
                    cyl_height = aoe_height if aoe_height is not None else size
                    min_cyl_z = origin_z
                    max_cyl_z = origin_z + cyl_height
                    if ent_max_z < min_cyl_z or ent_min_z > max_cyl_z:
                        is_hit = False
                elif shape == "cone":
                    dist = self.calculate_distance(origin_x, origin_y, origin_z, entity.x, entity.y, ent_center_z, vault_path)
                    if dist - (entity.size / 2.0) > size:
                        is_hit = False
                    else:
                        ux = entity.x - origin_x
                        uy = entity.y - origin_y
                        uz = ent_center_z - origin_z
                        mag_u = math.sqrt(ux**2 + uy**2 + uz**2)
                        if mag_u > 0.1:
                            cos_theta = (ux * vx + uy * vy + uz * vz) / mag_u
                            if cos_theta < 0.85:
                                is_hit = False
                elif shape == "line":
                    ax, ay, az = origin_x, origin_y, origin_z
                    bx, by, bz = origin_x + vx * size, origin_y + vy * size, origin_z + vz * size
                    ex, ey, ez = entity.x, entity.y, ent_center_z

                    ab_x, ab_y, ab_z = bx - ax, by - ay, bz - az
                    ae_x, ae_y, ae_z = ex - ax, ey - ay, ez - az

                    ab_len_sq = ab_x**2 + ab_y**2 + ab_z**2
                    if ab_len_sq == 0:
                        dist = self.calculate_distance(ex, ey, ez, ax, ay, az, vault_path)
                    else:
                        t = max(0.0, min(1.0, (ae_x * ab_x + ae_y * ab_y + ae_z * ab_z) / ab_len_sq))
                        cx, cy, cz = ax + t * ab_x, ay + t * ab_y, az + t * ab_z
                        dist = self.calculate_distance(ex, ey, ez, cx, cy, cz, vault_path)

                    if dist - (entity.size / 2.0) > 2.5:
                        is_hit = False

                if is_hit:
                    if ignore_walls:
                        hit_entities.append(ent_uuid)
                    else:
                        blocking_wall = self.check_path_collision(
                            origin_x,
                            origin_y,
                            origin_z,
                            entity.x,
                            entity.y,
                            ent_center_z,
                            entity_height=0.1,
                            check_vision=False,
                            vault_path=vault_path,
                        )
                        if not blocking_wall:
                            hit_entities.append(ent_uuid)
                        elif (
                            penetrates_destructible
                            and getattr(blocking_wall, "hp", None) is not None
                            and blocking_wall.hp < 9999
                        ):
                            hit_entities.append(ent_uuid)

        wall_candidates = list(self.wall_idx[vault_path].intersection(aoe_poly.bounds))
        for cid in wall_candidates:
            wall = self._wall_map[vault_path][cid]
            if wall.line and aoe_poly.intersects(wall.line):
                hit_walls.append(wall.wall_id)

        terrain_candidates = list(self.terrain_idx[vault_path].intersection(aoe_poly.bounds))
        for cid in terrain_candidates:
            tz = self._terrain_map[vault_path][cid]
            if tz.polygon and aoe_poly.intersects(tz.polygon):
                hit_terrains.append(tz.zone_id)

        return hit_entities, hit_walls, hit_terrains

    def get_distance_and_cover(
        self, source_uuid: uuid.UUID, target_uuid: uuid.UUID, vault_path: str = "default"
    ) -> Tuple[float, str]:
        """Calculates distance and determines cover (None, Half, Three-Quarters, Total)."""
        if not HAS_GIS:
            return 0.0, "None"

        with self._lock:
            self.get_map_data(vault_path)
            source = self._entities.get(vault_path, {}).get(source_uuid)
            target = self._entities.get(vault_path, {}).get(target_uuid)
            if not source or not target:
                return 5.0, "None"

            dist = self.calculate_distance(source.x, source.y, source.z, target.x, target.y, target.z, vault_path)

            source_hash = self._get_entity_spatial_hash(source)
            target_hash = self._get_entity_spatial_hash(target)
            cache_key = ("cover", source_hash, target_hash)

            if cache_key in self._raycast_cache.get(vault_path, {}):
                return dist, self._raycast_cache[vault_path][cache_key]

            source_poly = self._get_entity_bbox(source)
            target_poly = self._get_entity_bbox(target)

            source_corners = list(source_poly.exterior.coords)[:-1]
            target_corners = list(target_poly.exterior.coords)[:-1]

            s_z = source.z + getattr(source, "height", 5.0)
            t_z = target.z + (getattr(target, "height", 5.0) / 2.0)

            best_visible_corners = 0
            for s_corner in source_corners:
                visible_corners = 0
                for t_corner in target_corners:
                    line = LineString([s_corner, t_corner])
                    blocked = False
                    wall_candidates = list(self.wall_idx[vault_path].intersection(line.bounds))
                    for cid in wall_candidates:
                        wall = self._wall_map[vault_path][cid]
                        if not wall.is_solid:
                            continue
                        # Use intersection() directly — avoids the redundant intersects() pre-check
                        inter = line.intersection(wall.line)
                        if inter.is_empty:
                            continue
                        if line.length > 0:
                            # math.hypot avoids allocating a Shapely Point just for distance
                            frac_dist = math.hypot(inter.x - s_corner[0], inter.y - s_corner[1])
                            fraction = frac_dist / line.length
                        else:
                            fraction = 0.0

                        ray_z = s_z + fraction * (t_z - s_z)
                        if wall.z <= ray_z <= wall.z + wall.height:
                            blocked = True
                            break
                    if not blocked:
                        visible_corners += 1

                if visible_corners > best_visible_corners:
                    best_visible_corners = visible_corners
                if best_visible_corners == 4:
                    break

            if best_visible_corners == 4:
                cover = "None"
            elif best_visible_corners >= 2:
                cover = "Half"
            elif best_visible_corners == 1:
                cover = "Three-Quarters"
            else:
                cover = "Total"

            self._cache_set(vault_path, cache_key, cover)
            return dist, cover

    def get_intervening_creatures(
        self, source_uuid: uuid.UUID, target_uuid: uuid.UUID, vault_path: str = "default"
    ) -> List[uuid.UUID]:
        """Returns a list of entity UUIDs that intersect the straight line between source and target."""
        if not HAS_GIS:
            return []
        with self._lock:
            source = self._entities.get(vault_path, {}).get(source_uuid)
            target = self._entities.get(vault_path, {}).get(target_uuid)
            if not source or not target:
                return []
            path = LineString([(source.x, source.y), (target.x, target.y)])
            candidates = list(self.entity_idx[vault_path].intersection(path.bounds))
            interveners = []
            for cid in candidates:
                e_uuid = self._id_to_uuid[vault_path][cid]
                if e_uuid in [source_uuid, target_uuid]:
                    continue
                e_obj = self._entities[vault_path][e_uuid]
                if getattr(e_obj, "hp", None) and e_obj.hp.base_value <= 0:
                    continue
                if path.intersects(self._get_entity_bbox(e_obj)):
                    interveners.append(e_uuid)
            return interveners

    def has_line_of_sight(self, source_uuid: uuid.UUID, target_uuid: uuid.UUID, vault_path: str = "default") -> bool:
        """Determines if the center point of the target is visible."""
        target = self._entities.get(vault_path, {}).get(target_uuid)
        with self._lock:
            if not target:
                return False
            return self.has_line_of_sight_to_point(
                source_uuid, target.x, target.y, target.z + (getattr(target, "height", 5.0) / 2.0), vault_path
            )

    def calculate_path_terrain_costs(
        self,
        start_x: float,
        start_y: float,
        start_z: float,
        end_x: float,
        end_y: float,
        end_z: float,
        vault_path: str = "default",
    ) -> Tuple[float, float]:
        """Calculates how much of a path traverses normal terrain vs difficult terrain."""
        with self._lock:
            total_distance = self.calculate_distance(start_x, start_y, start_z, end_x, end_y, end_z, vault_path)
            md = self.get_map_data(vault_path)
            if not HAS_GIS or not md.active_terrain:
                return total_distance, 0.0

            path = LineString([(start_x, start_y), (end_x, end_y)])
            min_z = min(start_z, end_z)
            max_z = max(start_z, end_z)

            # Fast path: use pre-built terrain union for flat ground-level movement (covers ~99% of calls)
            is_flat_path = min_z <= 0.1 and max_z <= 5.0
            cached_union = self._terrain_union_cache.get(vault_path)
            if is_flat_path and cached_union is not None:
                union_poly = cached_union
            else:
                # Elevated or non-standard path: build union from per-z-filtered candidates
                terrain_candidates = list(self.terrain_idx[vault_path].intersection(path.bounds))
                difficult_polys = []
                for cid in terrain_candidates:
                    zone = self._terrain_map[vault_path][cid]
                    if zone.is_difficult and (max_z >= zone.z and min_z <= zone.z + max(zone.height, 0.1)):
                        if zone.polygon:
                            difficult_polys.append(zone.polygon)
                if not difficult_polys:
                    return total_distance, 0.0
                union_poly = unary_union(difficult_polys)

            intersection = path.intersection(union_poly)

            difficult_distance = 0.0
            if not intersection.is_empty:
                lines = []
                if intersection.geom_type == "LineString":
                    lines = [intersection]
                elif intersection.geom_type == "MultiLineString":
                    lines = list(intersection.geoms)

                total_2d = path.length
                if total_2d > 0:
                    for line in lines:
                        coords = list(line.coords)
                        if len(coords) >= 2:
                            z1 = start_z + (end_z - start_z) * (path.project(Point(coords[0])) / total_2d)
                            z2 = start_z + (end_z - start_z) * (path.project(Point(coords[-1])) / total_2d)
                            difficult_distance += self.calculate_distance(
                                coords[0][0], coords[0][1], z1, coords[-1][0], coords[-1][1], z2, vault_path
                            )

            difficult_distance = min(difficult_distance, total_distance)
            return total_distance - difficult_distance, difficult_distance

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
        viewer_uuid: Optional[uuid.UUID] = None,
        vault_path: str = "default",
    ) -> Optional[Wall]:
        """Returns the Wall object if the straight 3D line between start and end intersects it.
        viewer_uuid: if set and check_vision=True, illusion walls revealed for this viewer are skipped (REQ-ILL)."""
        if not HAS_GIS:
            return False
        with self._lock:
            self.get_map_data(vault_path)

            viewer_str = str(viewer_uuid) if viewer_uuid else None
            cache_key = (
                "path",
                round(start_x, 1),
                round(start_y, 1),
                round(start_z, 1),
                round(end_x, 1),
                round(end_y, 1),
                round(end_z, 1),
                round(entity_height, 1),
                check_vision,
                viewer_str,
            )
            if cache_key in self._raycast_cache.get(vault_path, {}):
                return self._raycast_cache[vault_path][cache_key]

            path = LineString([(start_x, start_y), (end_x, end_y)])
            wall_candidates = list(self.wall_idx[vault_path].intersection(path.bounds))
            for cid in wall_candidates:
                wall = self._wall_map[vault_path][cid]
                blocks = wall.is_visible if check_vision else wall.is_solid
                # REQ-ILL: Skip illusion walls that have been revealed for this viewer
                if check_vision and wall.is_illusion and viewer_str and viewer_str in wall.revealed_for:
                    continue
                if blocks and path.intersects(wall.line):
                    inter = path.intersection(wall.line)
                    if inter.is_empty:
                        continue

                    if path.length > 0:
                        fraction = Point(start_x, start_y).distance(inter) / path.length
                    else:
                        fraction = 0.0

                    cross_z = start_z + fraction * (end_z - start_z)
                    if not (cross_z + entity_height <= wall.z or cross_z >= wall.z + wall.height):
                        self._cache_set(vault_path, cache_key, wall)
                        return wall
            self._cache_set(vault_path, cache_key, False)
            return None

    def get_illumination(self, target_x: float, target_y: float, target_z: float, vault_path: str = "default") -> str:
        """Determines the highest level of illumination at a specific point, respecting Line of Sight."""
        if not HAS_GIS:
            return "bright"
        with self._lock:
            md = self.get_map_data(vault_path)
            highest_illum = "darkness"

            for light in md.active_lights:
                # Resolve light position: entity-attached follows entity, static uses own coords
                if light.attached_to_entity_uuid:
                    ent = self._entities.get(vault_path, {}).get(light.attached_to_entity_uuid)
                    if not ent:
                        continue
                    lx, ly, lz = ent.x, ent.y, ent.z
                else:
                    lx, ly, lz = light.x, light.y, light.z
                dist = self.calculate_distance(lx, ly, lz, target_x, target_y, target_z, vault_path)
                if dist <= light.dim_radius:
                    if not self.check_path_collision(
                        lx, ly, lz, target_x, target_y, target_z,
                        entity_height=0.1, check_vision=True, vault_path=vault_path,
                    ):
                        if dist <= light.bright_radius:
                            return "bright"
                        highest_illum = "dim"

            return highest_illum

    def has_line_of_sight_to_point(
        self, source_uuid: uuid.UUID, target_x: float, target_y: float, target_z: float = 0.0, vault_path: str = "default"
    ) -> bool:
        """Checks if a source entity has unbroken line of sight to a specific coordinate from ANY of its corners."""
        if not HAS_GIS:
            return True
        with self._lock:
            self.get_map_data(vault_path)

            source = self._entities.get(vault_path, {}).get(source_uuid)
            if not source:
                return False

            source_hash = self._get_entity_spatial_hash(source)
            cache_key = ("los_point", source_hash, round(target_x, 1), round(target_y, 1), round(target_z, 1))
            if cache_key in self._raycast_cache.get(vault_path, {}):
                return self._raycast_cache[vault_path][cache_key]

            source_poly = self._get_entity_bbox(source)
            source_corners = list(source_poly.exterior.coords)[:-1]

            s_z = source.z + getattr(source, "height", 5.0)

            for s_corner in source_corners:
                path = LineString([s_corner, (target_x, target_y)])
                corner_blocked = False
                wall_candidates = list(self.wall_idx[vault_path].intersection(path.bounds))
                for cid in wall_candidates:
                    wall = self._wall_map[vault_path][cid]
                    if wall.is_visible and path.intersects(wall.line):
                        inter = path.intersection(wall.line)
                        if inter.is_empty:
                            continue
                        if path.length > 0:
                            fraction = Point(s_corner).distance(inter) / path.length
                        else:
                            fraction = 0.0

                        ray_z = s_z + fraction * (target_z - s_z)
                        if wall.z <= ray_z <= wall.z + wall.height:
                            corner_blocked = True
                            break
                if not corner_blocked:
                    self._cache_set(vault_path, cache_key, True)
                    return True
            self._cache_set(vault_path, cache_key, False)
            return False

    def get_shape_points(
        self, shape: str, size: float, origin_x: float, origin_y: float, target_x: float = None, target_y: float = None
    ) -> List[Tuple[float, float]]:
        """Returns the boundary coordinates of an AoE shape for precise TerrainZone generation."""
        if not HAS_GIS:
            return []
        with self._lock:
            shape = shape.lower()
            aoe_poly = None

            if shape in ["circle", "sphere", "cylinder"]:
                aoe_poly = Point(origin_x, origin_y).buffer(size)
            elif shape == "cube":
                aoe_poly = box(origin_x - size / 2, origin_y - size / 2, origin_x + size / 2, origin_y + size / 2)
            elif shape in ["cone", "line"]:
                if target_x is None or target_y is None:
                    return []
                dx, dy = target_x - origin_x, target_y - origin_y
                dist_2d = math.hypot(dx, dy)
                vx, vy = (dx / dist_2d, dy / dist_2d) if dist_2d > 0 else (1.0, 0.0)

                if shape == "cone":
                    if vx == 0 and vy == 0:
                        aoe_poly = Point(origin_x, origin_y).buffer(size * 0.5)
                    else:
                        angle_deg = 53.1
                        angle_xy = math.atan2(vy, vx)
                        start_angle = angle_xy - math.radians(angle_deg / 2)
                        end_angle = angle_xy + math.radians(angle_deg / 2)

                        points = [(origin_x, origin_y)]
                        num_segments = max(4, int(angle_deg / 15))
                        for i in range(num_segments + 1):
                            theta = start_angle + i * (end_angle - start_angle) / num_segments
                            points.append((origin_x + size * math.cos(theta), origin_y + size * math.sin(theta)))
                        aoe_poly = Polygon(points)
                else:  # Line
                    ex, ey = origin_x + vx * size, origin_y + vy * size
                    if origin_x == ex and origin_y == ey:
                        aoe_poly = Point(origin_x, origin_y).buffer(2.5)
                    else:
                        aoe_poly = LineString([(origin_x, origin_y), (ex, ey)]).buffer(2.5)

            if aoe_poly:
                if aoe_poly.geom_type == "Polygon":
                    return list(aoe_poly.exterior.coords)
                elif aoe_poly.geom_type == "MultiPolygon":
                    return list(aoe_poly.geoms[0].exterior.coords)
            return []

    def render_ascii_map(self, vault_path: str = "default", width: int = 40, height: int = 20) -> str:
        """Generates a simple 2D ASCII graphical representation for the UI or debugging."""
        if not HAS_GIS:
            return "Map engine disabled."
        with self._lock:
            md = self.get_map_data(vault_path)
            grid = [["." for _ in range(width)] for _ in range(height)]

            for wall in md.active_walls:
                sx, sy = int(wall.start[0] / md.grid_scale), int(wall.start[1] / md.grid_scale)
                ex, ey = int(wall.end[0] / md.grid_scale), int(wall.end[1] / md.grid_scale)
                if 0 <= sx < width and 0 <= sy < height:
                    grid[sy][sx] = "#"
                if 0 <= ex < width and 0 <= ey < height:
                    grid[ey][ex] = "#"

            for uid, eid in self._uuid_to_id.get(vault_path, {}).items():
                entity = self._entities[vault_path].get(uid)
                if entity:
                    x = int(entity.x / md.grid_scale)
                    y = int(entity.y / md.grid_scale)
                    if 0 <= x < width and 0 <= y < height:
                        char = "P" if "pc" in getattr(entity, "tags", []) else "E"
                        grid[y][x] = f"**{char}**"

            return "\n".join([" ".join(row) for row in grid])


# Singleton instance for the engine to use
spatial_service = SpatialQueryService()
