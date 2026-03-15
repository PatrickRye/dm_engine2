import uuid
import math
from typing import List, Tuple, Dict, Optional, Protocol
from pydantic import BaseModel, Field

class SpatialObject(Protocol):
    entity_uuid: uuid.UUID
    x: float
    y: float
    z: float
    size: float
    height: float

try:
    from shapely.geometry import Point, LineString, Polygon, box
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
    radius: float = 0.0
    is_active: bool = True

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
    
    @property
    def line(self):
        if not HAS_GIS: return None
        return LineString([self.start, self.end])

class TerrainZone(BaseModel):
    zone_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    label: str = "terrain"
    points: List[Tuple[float, float]]
    z: float = 0.0
    height: float = 0.0
    is_difficult: bool = True
    trap: Optional[TrapDefinition] = None
    
    @property
    def polygon(self):
        if not HAS_GIS or len(self.points) < 3: return None
        return Polygon(self.points)

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
    walls: List[Wall] = Field(default_factory=list)
    terrain: List[TerrainZone] = Field(default_factory=list)
    lights: List[LightSource] = Field(default_factory=list)
    grid_scale: float = 5.0 # 1 square = 5ft
    distance_metric: str = "chebyshev" # "chebyshev" or "euclidean"

class SpatialQueryService:
    """
    A headless, deterministic spatial engine utilizing GIS libraries.
    Provides mathematical answers to mechanical game logic without rendering.
    """
    def __init__(self):
        self.map_data = MapData()
        if HAS_GIS:
            p = index.Property()
            self.entity_idx = index.Index(properties=p)
            self._entities: Dict[uuid.UUID, SpatialObject] = {}
            self._uuid_to_id: Dict[uuid.UUID, int] = {}
            self._id_to_uuid: Dict[int, uuid.UUID] = {}
            self._uuid_to_bbox: Dict[uuid.UUID, Tuple[float, float, float, float]] = {}
            self._next_id = 0
            self._raycast_cache: Dict[Tuple, any] = {}
            
    def clear(self):
        """Completely resets the spatial engine and Rtree index for testing."""
        self.map_data = MapData()
        if HAS_GIS:
            self.entity_idx = index.Index(properties=index.Property())
            self._entities.clear()
            self._uuid_to_id.clear()
            self._id_to_uuid.clear()
            self._uuid_to_bbox.clear()
            self._next_id = 0
            self._raycast_cache.clear()

    def invalidate_cache(self):
        """Clears the raycast cache when map geometry changes."""
        if HAS_GIS:
            self._raycast_cache.clear()

    def add_wall(self, wall: Wall, is_temporary: bool = False):
        """Dynamically adds a wall to the current or temporary map."""
        if is_temporary:
            self.map_data.temporary_walls.append(wall)
        else:
            self.map_data.walls.append(wall)
        self.invalidate_cache()
        
    def remove_wall(self, wall_id: uuid.UUID):
        """Removes a wall from the dynamic or temporary layers."""
        initial_count = len(self.map_data.walls) + len(self.map_data.temporary_walls)
        self.map_data.walls = [w for w in self.map_data.walls if w.wall_id != wall_id]
        self.map_data.temporary_walls = [w for w in self.map_data.temporary_walls if w.wall_id != wall_id]
        if len(self.map_data.walls) + len(self.map_data.temporary_walls) < initial_count:
            self.invalidate_cache()
            
    def modify_wall(self, wall_id: uuid.UUID, is_solid: Optional[bool] = None, is_visible: Optional[bool] = None, is_locked: Optional[bool] = None):
        """Dynamically updates a wall's state (e.g., opening a door or destroying a wall)."""
        for wall in self.map_data.active_walls:
            if wall.wall_id == wall_id:
                if is_solid is not None: wall.is_solid = is_solid
                if is_visible is not None: wall.is_visible = is_visible
                if is_locked is not None: wall.is_locked = is_locked
                self.invalidate_cache()
                return

    def reset_map_geometry(self):
        """Restores the current map to the original base map, wiping all damage/doors/temporary effects."""
        self.map_data.walls = [w.model_copy(deep=True) for w in self.map_data.original_walls]
        self.map_data.temporary_walls.clear()
        self.invalidate_cache()

    def load_map(self, map_data: MapData):
        # Preserve the original untouched geometry state
        if not map_data.original_walls and map_data.walls:
            map_data.original_walls = [w.model_copy(deep=True) for w in map_data.walls]
        if not map_data.original_terrain and map_data.terrain:
            map_data.original_terrain = [t.model_copy(deep=True) for t in map_data.terrain]
        if not map_data.original_lights and map_data.lights:
            map_data.original_lights = [l.model_copy(deep=True) for l in map_data.lights]
            
        self.map_data = map_data
        self.invalidate_cache()

    def sync_entity(self, entity: SpatialObject):
        """Adds or updates an entity's bounding box in the Rtree spatial index."""
        if not HAS_GIS: return
        
        self._entities[entity.entity_uuid] = entity
        new_bbox = self._get_bbox(entity)
        
        if entity.entity_uuid not in self._uuid_to_id:
            curr_id = self._next_id
            self._uuid_to_id[entity.entity_uuid] = curr_id
            self._id_to_uuid[curr_id] = entity.entity_uuid
            self._next_id += 1
        else:
            curr_id = self._uuid_to_id[entity.entity_uuid]
            old_bbox = self._uuid_to_bbox.get(entity.entity_uuid)
            if old_bbox:
                self.entity_idx.delete(curr_id, old_bbox)
            
        self.entity_idx.insert(curr_id, new_bbox)
        self._uuid_to_bbox[entity.entity_uuid] = new_bbox

    def remove_entity(self, entity_uuid: uuid.UUID):
        """Removes an entity from the spatial index."""
        if not HAS_GIS or entity_uuid not in self._uuid_to_id: return
        
        curr_id = self._uuid_to_id[entity_uuid]
        old_bbox = self._uuid_to_bbox.get(entity_uuid)
        if old_bbox:
            self.entity_idx.delete(curr_id, old_bbox)
        del self._id_to_uuid[curr_id]
        del self._uuid_to_id[entity_uuid]
        del self._uuid_to_bbox[entity_uuid]
        if entity_uuid in self._entities:
            del self._entities[entity_uuid]
        
    def calculate_distance(self, x1: float, y1: float, z1: float, x2: float, y2: float, z2: float) -> float:
        """Calculates 3D distance using the map's configured metric."""
        dx = abs(x1 - x2)
        dy = abs(y1 - y2)
        dz = abs(z1 - z2)
        if self.map_data.distance_metric == "chebyshev":
            return max(dx, dy, dz)
        else:
            return math.hypot(dx, dy, dz)

    def _get_bbox(self, entity: SpatialObject) -> Tuple[float, float, float, float]:
        half_size = entity.size / 2.0
        return (entity.x - half_size, entity.y - half_size, entity.x + half_size, entity.y + half_size)

    def _get_entity_bbox(self, entity: SpatialObject):
        if not HAS_GIS: return None
        return box(*self._get_bbox(entity))

    def _get_entity_spatial_hash(self, entity: SpatialObject) -> Tuple:
        """Quantizes an entity's spatial state to 1 decimal place (~1.2 inches) for cache keys."""
        return (round(entity.x, 1), round(entity.y, 1), round(entity.z, 1), round(entity.size, 1), round(getattr(entity, 'height', 5.0), 1))

    def _cache_set(self, key: Tuple, value: any):
        if len(self._raycast_cache) > 10000:
            self._raycast_cache.clear() # Fast clear if memory bounds are exceeded
        self._raycast_cache[key] = value

    def get_targets_in_radius(self, origin_x: float, origin_y: float, radius: float) -> List[uuid.UUID]:
        """Resolves circular Area of Effect (AoE) like Fireball perfectly."""
        if not HAS_GIS: return []
        
        if self.map_data.distance_metric == "chebyshev":
            search_area = box(origin_x - radius, origin_y - radius, origin_x + radius, origin_y + radius)
        else:
            search_area = Point(origin_x, origin_y).buffer(radius)
        
        # 1. Broad phase query (Rtree instantly filters out entities across the map)
        candidate_ids = list(self.entity_idx.intersection(search_area.bounds))
        
        # 2. Narrow phase query (Shapely precise intersection on the remaining candidates)
        hit_uuids = []
        origin_z = 0.0 # Default flat if not specified
        for cid in candidate_ids:
            ent_uuid = self._id_to_uuid[cid]
            entity = self._entities.get(ent_uuid)
            if entity:
                ent_poly = self._get_entity_bbox(entity)
                if search_area.intersects(ent_poly):
                    if self.calculate_distance(entity.x, entity.y, entity.z, origin_x, origin_y, origin_z) <= radius:
                        hit_uuids.append(ent_uuid)
                    
        return hit_uuids
        
    def get_targets_in_cone(self, origin_x: float, origin_y: float, target_x: float, target_y: float, length: float, angle_degrees: float = 60.0) -> List[uuid.UUID]:
        """Resolves cone Area of Effect (like Burning Hands)."""
        if not HAS_GIS: return []
        
        base_angle = math.degrees(math.atan2(target_y - origin_y, target_x - origin_x))
        start_angle = math.radians(base_angle - (angle_degrees / 2))
        end_angle = math.radians(base_angle + (angle_degrees / 2))
        
        points = [(origin_x, origin_y)]
        num_segments = max(4, int(angle_degrees / 15))
        for i in range(num_segments + 1):
            theta = start_angle + i * (end_angle - start_angle) / num_segments
            points.append((origin_x + length * math.cos(theta), origin_y + length * math.sin(theta)))
            
        cone_poly = Polygon(points)
        candidate_ids = list(self.entity_idx.intersection(cone_poly.bounds))
        
        hit_uuids = []
        for cid in candidate_ids:
            ent_uuid = self._id_to_uuid[cid]
            entity = self._entities.get(ent_uuid)
            if entity:
                ent_poly = self._get_entity_bbox(entity)
                if cone_poly.intersects(ent_poly):
                    hit_uuids.append(ent_uuid)
                    
        return hit_uuids

    def get_distance_and_cover(self, source_uuid: uuid.UUID, target_uuid: uuid.UUID) -> Tuple[float, str]:
        """Calculates distance and determines cover (None, Half, Three-Quarters, Total)."""
        if not HAS_GIS: return 0.0, "None"
        
        source = self._entities.get(source_uuid)
        target = self._entities.get(target_uuid)
        if not source or not target:
            # Fallback for purely mathematical combat tests that don't sync spatial data
            return 5.0, "None"
            
        # 3D Distance calculated via configured metric
        dist = self.calculate_distance(source.x, source.y, source.z, target.x, target.y, target.z)
        
        source_hash = self._get_entity_spatial_hash(source)
        target_hash = self._get_entity_spatial_hash(target)
        cache_key = ("cover", source_hash, target_hash)
        
        if cache_key in self._raycast_cache:
            return dist, self._raycast_cache[cache_key]
        
        source_poly = self._get_entity_bbox(source)
        target_poly = self._get_entity_bbox(target)
        
        source_corners = list(source_poly.exterior.coords)[:-1]
        target_corners = list(target_poly.exterior.coords)[:-1] 
        
        best_visible_corners = 0
        for s_corner in source_corners:
            visible_corners = 0
            for t_corner in target_corners:
                line = LineString([s_corner, t_corner])
                blocked = False
                for wall in self.map_data.active_walls:
                    if wall.is_solid and line.intersects(wall.line):
                        min_z = min(source.z, target.z)
                        max_z = max(source.z + getattr(source, 'height', 5.0), target.z + getattr(target, 'height', 5.0))
                        if not (max_z <= wall.z or min_z >= wall.z + wall.height):
                            blocked = True
                            break
                if not blocked:
                    visible_corners += 1
                    
            if visible_corners > best_visible_corners:
                best_visible_corners = visible_corners
            if best_visible_corners == 4:
                break # Cannot get better cover than no cover!
                
        if best_visible_corners == 4:
            cover = "None"
        elif best_visible_corners >= 2:
            cover = "Half"
        elif best_visible_corners == 1:
            cover = "Three-Quarters"
        else:
            cover = "Total"
            
        self._cache_set(cache_key, cover)
        return dist, cover

    def has_line_of_sight(self, source_uuid: uuid.UUID, target_uuid: uuid.UUID) -> bool:
        """Determines if the center point of the target is visible."""
        target = self._entities.get(target_uuid)
        if not target: return False
        # Check Line of Sight exactly to the center-mass of the target
        return self.has_line_of_sight_to_point(source_uuid, target.x, target.y, target.z + (getattr(target, 'height', 5.0) / 2.0))

    def calculate_path_terrain_costs(self, start_x: float, start_y: float, start_z: float, end_x: float, end_y: float, end_z: float) -> Tuple[float, float]:
        """Calculates how much of a path traverses normal terrain vs difficult terrain."""
        total_distance = self.calculate_distance(start_x, start_y, start_z, end_x, end_y, end_z)
        if not HAS_GIS or not self.map_data.active_terrain:
            return total_distance, 0.0
            
        path = LineString([(start_x, start_y), (end_x, end_y)])
        min_z = min(start_z, end_z)
        max_z = max(start_z, end_z)
        
        difficult_polys = []
        for zone in self.map_data.active_terrain:
            if zone.is_difficult and (max_z >= zone.z and min_z <= zone.z + max(zone.height, 0.1)):
                if zone.polygon: difficult_polys.append(zone.polygon)
                    
        if not difficult_polys: return total_distance, 0.0
            
        from shapely.ops import unary_union
        union_poly = unary_union(difficult_polys)
        intersection = path.intersection(union_poly)
        
        difficult_distance = 0.0
        if not intersection.is_empty:
            lines = []
            if intersection.geom_type == 'LineString': lines = [intersection]
            elif intersection.geom_type == 'MultiLineString': lines = list(intersection.geoms)
                
            total_2d = path.length
            if total_2d > 0:
                for line in lines:
                    coords = list(line.coords)
                    if len(coords) >= 2:
                        # Map 2D fractions back onto the 3D path to get exact 3D hypotenuses
                        z1 = start_z + (end_z - start_z) * (path.project(Point(coords[0])) / total_2d)
                        z2 = start_z + (end_z - start_z) * (path.project(Point(coords[-1])) / total_2d)
                        difficult_distance += self.calculate_distance(coords[0][0], coords[0][1], z1, coords[-1][0], coords[-1][1], z2)
                        
        difficult_distance = min(difficult_distance, total_distance)
        return total_distance - difficult_distance, difficult_distance

    def check_path_collision(self, start_x: float, start_y: float, start_z: float, end_x: float, end_y: float, end_z: float, entity_height: float = 5.0, check_vision: bool = False) -> Optional[Wall]:
        """Returns the Wall object if the straight 3D line between start and end intersects it."""
        if not HAS_GIS: return False
        
        cache_key = ("path", round(start_x, 1), round(start_y, 1), round(start_z, 1), round(end_x, 1), round(end_y, 1), round(end_z, 1), round(entity_height, 1), check_vision)
        if cache_key in self._raycast_cache:
            return self._raycast_cache[cache_key]
            
        path = LineString([(start_x, start_y), (end_x, end_y)])
        for wall in self.map_data.active_walls:
            blocks = (not wall.is_visible) if check_vision else wall.is_solid
            if blocks and path.intersects(wall.line):
                min_z = min(start_z, end_z)
                max_z = max(start_z, end_z) + entity_height
                if not (max_z <= wall.z or min_z >= wall.z + wall.height):
                    self._cache_set(cache_key, wall)
                    return wall
        self._cache_set(cache_key, False)
        return None

    def get_illumination(self, target_x: float, target_y: float, target_z: float) -> str:
        """Determines the highest level of illumination at a specific point, respecting Line of Sight."""
        if not HAS_GIS: return "bright" # Default gracefully if engine disabled
        
        highest_illum = "darkness"
        for light in self.map_data.active_lights:
            lx, ly, lz = light.x, light.y, light.z
            if light.attached_to_entity_uuid:
                ent = self._entities.get(light.attached_to_entity_uuid)
                if ent:
                    lx, ly, lz = ent.x, ent.y, ent.z
                    
            dist = self.calculate_distance(lx, ly, lz, target_x, target_y, target_z)
            if dist <= light.dim_radius:
                # Check Line of Sight from light source to the target point
                if not self.check_path_collision(lx, ly, lz, target_x, target_y, target_z, entity_height=0.1, check_vision=True):
                    if dist <= light.bright_radius:
                        return "bright" # Cannot get brighter than this, return early
                    highest_illum = "dim"
        return highest_illum

    def has_line_of_sight_to_point(self, source_uuid: uuid.UUID, target_x: float, target_y: float, target_z: float = 0.0) -> bool:
        """Checks if a source entity has unbroken line of sight to a specific coordinate from ANY of its corners."""
        if not HAS_GIS: return True
        
        source = self._entities.get(source_uuid)
        if not source: return False
        
        source_hash = self._get_entity_spatial_hash(source)
        cache_key = ("los_point", source_hash, round(target_x, 1), round(target_y, 1), round(target_z, 1))
        if cache_key in self._raycast_cache:
            return self._raycast_cache[cache_key]
            
        source_poly = self._get_entity_bbox(source)
        source_corners = list(source_poly.exterior.coords)[:-1]
        
        for s_corner in source_corners:
            path = LineString([s_corner, (target_x, target_y)])
            corner_blocked = False
            for wall in self.map_data.active_walls:
                if not wall.is_visible and path.intersects(wall.line):
                    if not (source.z + getattr(source, 'height', 5.0) <= wall.z or target_z >= wall.z + wall.height):
                        corner_blocked = True
                        break
            if not corner_blocked:
                self._cache_set(cache_key, True)
                return True # At least one corner has unbroken LoS!
        self._cache_set(cache_key, False)
        return False

    def render_ascii_map(self, width: int = 40, height: int = 20) -> str:
        """Generates a simple 2D ASCII graphical representation for the UI or debugging."""
        if not HAS_GIS: return "Map engine disabled."
        grid = [["." for _ in range(width)] for _ in range(height)]
        
        # Draw Walls (simplified rasterization)
        for wall in self.map_data.active_walls:
            sx, sy = int(wall.start[0] / self.map_data.grid_scale), int(wall.start[1] / self.map_data.grid_scale)
            ex, ey = int(wall.end[0] / self.map_data.grid_scale), int(wall.end[1] / self.map_data.grid_scale)
            if 0 <= sx < width and 0 <= sy < height: grid[sy][sx] = "#"
            if 0 <= ex < width and 0 <= ey < height: grid[ey][ex] = "#"
            
        # Draw Entities
        for uid, eid in self._uuid_to_id.items():
            entity = self._entities.get(uid)
            if entity:
                x = int(entity.x / self.map_data.grid_scale)
                y = int(entity.y / self.map_data.grid_scale)
                if 0 <= x < width and 0 <= y < height:
                    char = "P" if "pc" in getattr(entity, 'tags', []) else "E"
                    # Highlighting in markdown format
                    grid[y][x] = f"**{char}**" 
                    
        return "\n".join([" ".join(row) for row in grid])

# Singleton instance for the engine to use
spatial_service = SpatialQueryService()