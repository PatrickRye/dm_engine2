import unittest
import uuid

from dnd_rules_engine import BaseGameEntity, Creature, ModifiableValue
from spatial_engine import spatial_service, MapData, Wall, HAS_GIS

@unittest.skipIf(not HAS_GIS, "Shapely and Rtree are required (pip install shapely rtree)")
class TestSpatialEngine(unittest.TestCase):
    def setUp(self):
        BaseGameEntity._registry.clear()
        if HAS_GIS:
            spatial_service.map_data = MapData()
            spatial_service._uuid_to_id.clear()
            spatial_service._id_to_uuid.clear()
            
    def test_line_of_sight_and_cover(self):
        archer = Creature(
            name="Archer", x=0.0, y=0.0, size=5.0,
            hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0)
        )
        goblin = Creature(
            name="Goblin", x=20.0, y=0.0, size=5.0,
            hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0)
        )
        
        spatial_service.sync_entity(archer)
        spatial_service.sync_entity(goblin)
        
        # Test clear LOS
        self.assertTrue(spatial_service.has_line_of_sight(archer.entity_uuid, goblin.entity_uuid))
        dist, cover = spatial_service.get_distance_and_cover(archer.entity_uuid, goblin.entity_uuid)
        self.assertEqual(dist, 20.0)
        self.assertEqual(cover, "None")
        
        # Add a solid wall directly between them
        wall = Wall(start=(10.0, -10.0), end=(10.0, 10.0))
        spatial_service.map_data.walls.append(wall)
        
        self.assertFalse(spatial_service.has_line_of_sight(archer.entity_uuid, goblin.entity_uuid))
        dist, cover = spatial_service.get_distance_and_cover(archer.entity_uuid, goblin.entity_uuid)
        self.assertEqual(cover, "Total")
        
        # Move wall so it only blocks the bottom half of the Goblin's bounding box
        spatial_service.map_data.walls[0] = Wall(start=(10.0, 0.0), end=(10.0, 10.0))
        dist, cover = spatial_service.get_distance_and_cover(archer.entity_uuid, goblin.entity_uuid)
        self.assertIn(cover, ["Half", "Three-Quarters"])
        
    def test_aoe_fireball(self):
        # Setup two goblins
        goblin1 = Creature(name="Goblin 1", x=10.0, y=10.0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
        goblin2 = Creature(name="Goblin 2", x=100.0, y=100.0, size=5.0, hp=ModifiableValue(base_value=10), ac=ModifiableValue(base_value=10), strength_mod=ModifiableValue(base_value=0), dexterity_mod=ModifiableValue(base_value=0))
        
        spatial_service.sync_entity(goblin1)
        spatial_service.sync_entity(goblin2)
        
        # Fireball centered at (0, 0) with a radius of 20 feet
        hits = spatial_service.get_targets_in_radius(0.0, 0.0, 20.0)
        
        self.assertIn(goblin1.entity_uuid, hits) # Goblin 1 is 14ft away (hit!)
        self.assertNotIn(goblin2.entity_uuid, hits) # Goblin 2 is 141ft away (safe!)

if __name__ == '__main__':
    unittest.main()