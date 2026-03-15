import uuid
import math
from typing import List, Tuple, Dict, Optional
from pydantic import BaseModel, Field

# Ensure dnd_rules_engine classes are available
from dnd_rules_engine import BaseGameEntity

try:
    from shapely.geometry import Point, LineString, Polygon, box
    from rtree import index
    HAS_GIS = True
except ImportError:
    HAS_GIS = False
    print("WARNING: shapely and rtree are required for the spatial engine. Run: pip install shapely rtree")

class Wall(BaseModel):
    wall_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    label: str = "wall"
    start: Tuple[float, float]
    end: Tuple[float, float]
    z: float = 0.0
    height: float = 10.0
    is_solid: bool = True
    is_visible: bool = True
    
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
            self._uuid_to_id: Dict[uuid.UUID, int] = {}
            self._id_to_uuid: Dict[int, uuid.UUID] = {}
            self._uuid_to_bbox: Dict[uuid.UUID, Tuple[float, float, float, float]] = {}
            self._next_id = 0

    def load_map(self, map_data: MapData):
        self.map_data = map_data

    def sync_entity(self, entity: BaseGameEntity):
        """Adds or updates an entity's bounding box in the Rtree spatial index."""
        if not HAS_GIS: return
        
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
        
    def calculate_distance(self, x1: float, y1: float, z1: float, x2: float, y2: float, z2: float) -> float:
        """Calculates 3D distance using the map's configured metric."""
        dx = abs(x1 - x2)
        dy = abs(y1 - y2)
        dz = abs(z1 - z2)
        if self.map_data.distance_metric == "chebyshev":
            return max(dx, dy, dz)
        else:
            return math.hypot(dx, dy, dz)

    def _get_bbox(self, entity: BaseGameEntity) -> Tuple[float, float, float, float]:
        half_size = entity.size / 2.0
        return (entity.x - half_size, entity.y - half_size, entity.x + half_size, entity.y + half_size)

    def _get_entity_bbox(self, entity: BaseGameEntity):
        if not HAS_GIS: return None
        return box(*self._get_bbox(entity))

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
            entity = BaseGameEntity.get(ent_uuid)
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
            entity = BaseGameEntity.get(ent_uuid)
            if entity:
                ent_poly = self._get_entity_bbox(entity)
                if cone_poly.intersects(ent_poly):
                    hit_uuids.append(ent_uuid)
                    
        return hit_uuids

    def get_distance_and_cover(self, source_uuid: uuid.UUID, target_uuid: uuid.UUID) -> Tuple[float, str]:
        """Calculates distance and determines cover (None, Half, Three-Quarters, Total)."""
        if not HAS_GIS: return 0.0, "None"
        
        source = BaseGameEntity.get(source_uuid)
        target = BaseGameEntity.get(target_uuid)
        if not source or not target:
            return 0.0, "Total"
            
        # 3D Distance calculated via configured metric
        dist = self.calculate_distance(source.x, source.y, source.z, target.x, target.y, target.z)
        
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
                for wall in self.map_data.walls:
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
            
        return dist, cover

    def has_line_of_sight(self, source_uuid: uuid.UUID, target_uuid: uuid.UUID) -> bool:
        """Determines if the center point of the target is visible."""
        _, cover = self.get_distance_and_cover(source_uuid, target_uuid)
        return cover != "Total"

    def calculate_path_terrain_costs(self, start_x: float, start_y: float, start_z: float, end_x: float, end_y: float, end_z: float) -> Tuple[float, float]:
        """Calculates how much of a path traverses normal terrain vs difficult terrain."""
        total_distance = self.calculate_distance(start_x, start_y, start_z, end_x, end_y, end_z)
        if not HAS_GIS or not self.map_data.terrain:
            return total_distance, 0.0
            
        path = LineString([(start_x, start_y), (end_x, end_y)])
        min_z = min(start_z, end_z)
        max_z = max(start_z, end_z)
        
        difficult_polys = []
        for zone in self.map_data.terrain:
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

    def check_path_collision(self, start_x: float, start_y: float, start_z: float, end_x: float, end_y: float, end_z: float, entity_height: float = 5.0) -> bool:
        """Returns True if the straight 3D line between start and end intersects a solid wall."""
        if not HAS_GIS: return False
        path = LineString([(start_x, start_y), (end_x, end_y)])
        for wall in self.map_data.walls:
            if wall.is_solid and path.intersects(wall.line):
                min_z = min(start_z, end_z)
                max_z = max(start_z, end_z) + entity_height
                if not (max_z <= wall.z or min_z >= wall.z + wall.height):
                    return True
        return False

    def has_line_of_sight_to_point(self, source_uuid: uuid.UUID, target_x: float, target_y: float, target_z: float = 0.0) -> bool:
        """Checks if a source entity has unbroken line of sight to a specific coordinate from ANY of its corners."""
        if not HAS_GIS: return True
        source = BaseGameEntity.get(source_uuid)
        if not source: return False
        
        source_poly = self._get_entity_bbox(source)
        source_corners = list(source_poly.exterior.coords)[:-1]
        
        for s_corner in source_corners:
            path = LineString([s_corner, (target_x, target_y)])
            corner_blocked = False
            for wall in self.map_data.walls:
                if wall.is_solid and path.intersects(wall.line):
                    if not (source.z + getattr(source, 'height', 5.0) <= wall.z or target_z >= wall.z + wall.height):
                        corner_blocked = True
                        break
            if not corner_blocked:
                return True # At least one corner has unbroken LoS!
        return False

    def render_ascii_map(self, width: int = 40, height: int = 20) -> str:
        """Generates a simple 2D ASCII graphical representation for the UI or debugging."""
        if not HAS_GIS: return "Map engine disabled."
        grid = [["." for _ in range(width)] for _ in range(height)]
        
        # Draw Walls (simplified rasterization)
        for wall in self.map_data.walls:
            sx, sy = int(wall.start[0] / self.map_data.grid_scale), int(wall.start[1] / self.map_data.grid_scale)
            ex, ey = int(wall.end[0] / self.map_data.grid_scale), int(wall.end[1] / self.map_data.grid_scale)
            if 0 <= sx < width and 0 <= sy < height: grid[sy][sx] = "#"
            if 0 <= ex < width and 0 <= ey < height: grid[ey][ex] = "#"
            
        # Draw Entities
        for uid, eid in self._uuid_to_id.items():
            entity = BaseGameEntity.get(uid)
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