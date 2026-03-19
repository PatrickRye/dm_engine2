import unittest

from dnd_rules_engine import Creature, ModifiableValue
from spatial_engine import spatial_service, Wall, HAS_GIS
from registry import clear_registry


@unittest.skipIf(not HAS_GIS, "Shapely and Rtree are required (pip install shapely rtree)")
class TestSpatialEngine(unittest.TestCase):
    def setUp(self):
        clear_registry()
        spatial_service.clear()

    def create_combatant(self, name: str, x: float, y: float) -> Creature:
        c = Creature(
            name=name,
            x=x,
            y=y,
            size=5.0,
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
        )
        spatial_service.sync_entity(c)
        return c

    def test_line_of_sight_and_cover(self):
        archer = Creature(
            name="Archer",
            x=0.0,
            y=0.0,
            size=5.0,
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
        )
        goblin = Creature(
            name="Goblin",
            x=20.0,
            y=0.0,
            size=5.0,
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
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
        spatial_service.invalidate_cache()

        self.assertFalse(spatial_service.has_line_of_sight(archer.entity_uuid, goblin.entity_uuid))
        dist, cover = spatial_service.get_distance_and_cover(archer.entity_uuid, goblin.entity_uuid)
        self.assertEqual(cover, "Total")

        # Move wall so it only blocks the bottom half of the Goblin's bounding box
        spatial_service.map_data.walls[0] = Wall(start=(10.0, 0.0), end=(10.0, 10.0))
        spatial_service.invalidate_cache()
        dist, cover = spatial_service.get_distance_and_cover(archer.entity_uuid, goblin.entity_uuid)
        self.assertIn(cover, ["Half", "Three-Quarters"])

    def test_aoe_fireball(self):
        # Setup two goblins
        goblin1 = Creature(
            name="Goblin 1",
            x=10.0,
            y=10.0,
            size=5.0,
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
        )
        goblin2 = Creature(
            name="Goblin 2",
            x=100.0,
            y=100.0,
            size=5.0,
            hp=ModifiableValue(base_value=10),
            ac=ModifiableValue(base_value=10),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
        )

        spatial_service.sync_entity(goblin1)
        spatial_service.sync_entity(goblin2)

        # Fireball centered at (0, 0) with a radius of 20 feet
        hits = spatial_service.get_targets_in_radius(0.0, 0.0, 20.0)

        self.assertIn(goblin1.entity_uuid, hits)  # Goblin 1 is 14ft away (hit!)
        self.assertNotIn(goblin2.entity_uuid, hits)  # Goblin 2 is 141ft away (safe!)

    def test_glass_window_physics(self):
        """Test is_solid=True, is_visible=True: Blocks physical movement and provides cover, but allows Line of Sight."""
        p1 = self.create_combatant("P1", 0.0, 0.0)
        p2 = self.create_combatant("P2", 10.0, 0.0)

        window = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=True, is_visible=False, label="Glass Window")
        spatial_service.add_wall(window)

        # Can see right through it
        self.assertTrue(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))

        # Cannot move through it
        self.assertTrue(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))

        # Still provides physical cover
        _, cover = spatial_service.get_distance_and_cover(p1.entity_uuid, p2.entity_uuid)
        self.assertEqual(cover, "Total")

    def test_illusion_wall_physics(self):
        """Test is_solid=False, is_visible=False: Blocks vision/LoS, but allows physical movement and projectiles."""
        p1 = self.create_combatant("P1", 0.0, 0.0)
        p2 = self.create_combatant("P2", 10.0, 0.0)

        fog = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=False, is_visible=True, label="Fog Cloud")
        spatial_service.add_wall(fog)

        # Cannot see through it
        self.assertFalse(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))

        # Can walk right through it
        self.assertFalse(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))

        # Provides zero physical cover to projectiles
        _, cover = spatial_service.get_distance_and_cover(p1.entity_uuid, p2.entity_uuid)
        self.assertEqual(cover, "None")

    def test_dynamic_door_opening(self):
        """Test opening a door dynamically updates the pathing and LoS cache."""
        p1 = self.create_combatant("P1", 0.0, 0.0)
        p2 = self.create_combatant("P2", 10.0, 0.0)

        door = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=True, is_visible=True, label="Heavy Oak Door")
        spatial_service.add_wall(door)

        # Closed door
        self.assertFalse(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))
        self.assertTrue(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))

        # Open the door
        spatial_service.modify_wall(door.wall_id, is_solid=False, is_visible=False)

        # Opened door
        self.assertTrue(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))
        self.assertFalse(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))

    def test_temporary_walls(self):
        """Test that temporary walls block paths but are correctly wiped on map reset."""
        spatial_service.map_data.original_walls = []

        # Cast Wall of Stone (temporary)
        temp_wall = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=True, is_visible=True)
        spatial_service.add_wall(temp_wall, is_temporary=True)

        self.assertTrue(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))
        self.assertEqual(len(spatial_service.map_data.active_walls), 1)

        # Duration expires, reset map geometry
        spatial_service.reset_map_geometry()

        self.assertFalse(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))
        self.assertEqual(len(spatial_service.map_data.active_walls), 0)

    def test_rounding_corners_line_of_sight(self):
        """Test that entities can peer around a corner if their bounding box extends past the wall."""
        p1 = self.create_combatant("P1", 0.0, 0.0)  # Bounding box covers y: -2.5 to 2.5
        p2 = self.create_combatant("P2", 10.0, 0.0)

        # Wall goes from (5,0) exactly at the center-line, extending north to (5,10)
        # The direct center-to-center path from (0,0) to (10,0) hits the corner exactly at (5,0).
        # However, P1's southern corners (-2.5, -2.5) and (2.5, -2.5) have a clear unbroken line to P2.
        corner_wall = Wall(start=(5.0, 0.0), end=(5.0, 10.0), is_solid=True, is_visible=True)
        spatial_service.add_wall(corner_wall)

        # P1 can see P2 by looking "around" the bottom of the corner
        self.assertTrue(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))

        # If we extend the wall south to cover P1's entire bounding box, LoS is finally broken.
        spatial_service.remove_wall(corner_wall.wall_id)
        extended_wall = Wall(start=(5.0, -5.0), end=(5.0, 10.0), is_solid=True, is_visible=True)
        spatial_service.add_wall(extended_wall)

        self.assertFalse(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))

    def test_breaking_walls(self):
        """Test physically breaking an obstacle removes it from the spatial map."""
        fragile_wall = Wall(start=(5.0, -5.0), end=(5.0, 5.0), is_solid=True, is_visible=True, label="Ice Wall")
        spatial_service.add_wall(fragile_wall)

        self.assertTrue(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))

        # Barbarian smashes the Ice Wall
        spatial_service.remove_wall(fragile_wall.wall_id)

        self.assertFalse(spatial_service.check_path_collision(0, 0, 0, 10, 0, 0))

    def test_3d_raycast_over_wall(self):
        """Test that Line of Sight and Cover natively calculate the 3D ray Z-intersection."""
        p1 = self.create_combatant("Archer", 0.0, 0.0)
        p1.z = 0.0  # Height is 5. Eye level = 5

        p2 = self.create_combatant("Target", 20.0, 0.0)
        p2.z = 0.0  # Center is 2.5

        # Ray goes from Z=5 to Z=2.5. Distance is 20ft.
        # Wall is exactly in the middle at X=10.
        # Ray crosses wall at fraction 0.5. Z = 5 + 0.5 * (2.5 - 5) = 3.75.

        # If wall is Z=0, height 3 -> ray Z (3.75) is > 3. OVER the wall! Cover = None
        short_wall = Wall(start=(10.0, -10.0), end=(10.0, 10.0), z=0.0, height=3.0)
        spatial_service.add_wall(short_wall)

        self.assertTrue(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))
        _, cover = spatial_service.get_distance_and_cover(p1.entity_uuid, p2.entity_uuid)
        self.assertEqual(cover, "None")

        # If wall is Z=0, height 5 -> ray Z (3.75) is < 5. HITS the wall! Cover = Total
        spatial_service.remove_wall(short_wall.wall_id)
        tall_wall = Wall(start=(10.0, -10.0), end=(10.0, 10.0), z=0.0, height=5.0)
        spatial_service.add_wall(tall_wall)

        self.assertFalse(spatial_service.has_line_of_sight(p1.entity_uuid, p2.entity_uuid))
        _, cover2 = spatial_service.get_distance_and_cover(p1.entity_uuid, p2.entity_uuid)
        self.assertEqual(cover2, "Total")


if __name__ == "__main__":
    unittest.main()
