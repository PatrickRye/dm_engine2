import pytest
from unittest.mock import patch

from dnd_rules_engine import Creature, ModifiableValue, GameEvent, EventBus
from spatial_engine import spatial_service, Wall, TerrainZone
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup_engine():
    """Clears the object registries and R-tree spatial indexes before each test."""
    clear_registry()
    spatial_service.clear()
    yield


def create_test_creature(name: str, x: float, y: float) -> Creature:
    """Helper to instantiate properly formatted engine entities."""
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
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


def test_fireball_inner_corner():
    """
    Test a fireball thrown into the inner corner of a large square room
    with infinitely strong walls. The explosion should only be a 90-degree wedge.
    [Mapped: REQ-SPL-008]
    """
    # Create an L-shaped corner at (0,0) opening into the top-right quadrant (+x, +y)
    wall1 = Wall(label="North Wall", start=(0, 0), end=(50, 0), is_solid=True, hp=9999)
    wall2 = Wall(label="East Wall", start=(0, 0), end=(0, 50), is_solid=True, hp=9999)
    spatial_service.add_wall(wall1)
    spatial_service.add_wall(wall2)

    c_inside = create_test_creature("Inside Room", 10, 10)
    c_out_x = create_test_creature("Behind North Wall", 10, -10)
    c_out_y = create_test_creature("Behind East Wall", -10, 10)
    c_out_diag = create_test_creature("Diagonal Outside", -10, -10)

    # Fireball aimed just inside the corner
    hits, hit_walls, _ = spatial_service.get_aoe_targets("sphere", 20.0, 0.1, 0.1)

    assert c_inside.entity_uuid in hits, "Target inside the wedge should be hit."
    assert c_out_x.entity_uuid not in hits, "Wall should block blast along the X axis."
    assert c_out_y.entity_uuid not in hits, "Wall should block blast along the Y axis."
    assert c_out_diag.entity_uuid not in hits, "The thick corner should block the blast completely."


def test_fireball_convex_corner():
    """
    Test a fireball thrown at the convex corner of a square building made of
    infinitely strong walls. The area should be a 3/4 circle (270 degrees).
    [Mapped: REQ-SPL-008, REQ-SPL-007]
    """
    # Building occupies the bottom-left quadrant (-X, -Y). The corner is at (0,0).
    wall1 = Wall(label="South Face", start=(0, 0), end=(-50, 0), is_solid=True, hp=9999)
    wall2 = Wall(label="West Face", start=(0, 0), end=(0, -50), is_solid=True, hp=9999)
    spatial_service.add_wall(wall1)
    spatial_service.add_wall(wall2)

    c_inside = create_test_creature("Inside Building", -10, -10)
    c_exposed1 = create_test_creature("Top-Right Exposed", 10, 10)
    c_exposed2 = create_test_creature("Top-Left Exposed", -10, 10)
    c_exposed3 = create_test_creature("Bottom-Right Exposed", 10, -10)

    # Fireball aimed just outside the corner
    hits, hit_walls, _ = spatial_service.get_aoe_targets("sphere", 20.0, 0.1, 0.1)

    assert c_exposed1.entity_uuid in hits
    assert c_exposed2.entity_uuid in hits
    assert c_exposed3.entity_uuid in hits
    assert c_inside.entity_uuid not in hits, "Target inside the convex building corner should be protected."


def test_cone_of_cold_column():
    """
    Test a cone of cold cast at an enemy hiding directly behind a column.
    The hiding enemy is safe, but an exposed enemy is hit.
    [Mapped: REQ-SPL-010]
    """
    # Small 4ft column at (0, 10) acting as a blockage in front of the target
    col = Wall(label="Stone Column", start=(-2, 10), end=(2, 10), is_solid=True, hp=9999)
    spatial_service.add_wall(col)

    c_hiding = create_test_creature("Hiding", 0, 15)  # Directly behind the column
    # Cone spans from ~63.4 deg to 116.5 deg. (5, 15) sits at 71.5 deg, inside the cone, bypassing the 4ft column.
    c_exposed = create_test_creature("Exposed", 5, 15)

    # 60ft Cone cast from origin (0,0) facing North towards (0, 20)
    hits, hit_walls, _ = spatial_service.get_aoe_targets("cone", 60.0, 0.0, 0.0, target_x=0.0, target_y=20.0)

    assert c_hiding.entity_uuid not in hits, "Enemy behind the column should not be hit."
    assert c_exposed.entity_uuid in hits, "Enemy beside the column should be hit."


def test_fireball_destructible_wall(mock_roll_dice):
    """
    Test a fireball hitting a 50ft wooden wall made of 10x 5ft sections (10hp each, fire vulnerability).
    The middle 40ft (8 sections) are destroyed, leaving 1 section on each side intact.
    [Mapped: REQ-OBJ-002]
    """
    wall_ids = []
    # Build a 50ft wall out of 10x 5-foot sections along the X axis from -25 to +25
    for i in range(10):
        start_x = -25 + (i * 5)
        end_x = start_x + 5
        w = Wall(
            label=f"Wooden Section {i}",
            start=(start_x, 0),
            end=(end_x, 0),
            is_solid=True,
            hp=10,
            max_hp=10,
            vulnerabilities=["fire"],
        )
        spatial_service.add_wall(w)
        wall_ids.append(w.wall_id)

    caster = create_test_creature("Mage", 0, 50)

    # We use radius 19.9 to cleanly avoid Shapely reporting intersecting edge boundaries (e.g. exactly at x=-20).
    # This accurately maps to a D&D grid: a 20ft radius hits 4 interior 5ft squares, but misses the 5th outer square.
    hits, hit_walls, _ = spatial_service.get_aoe_targets("sphere", 19.9, 0.0, 0.0)

    # We must fire a SpellCast event so the engine's resolve_spell_cast_handler natively applies the geometric damage
    mechanics = {"damage_dice": "8d6", "damage_type": "fire", "save_required": "dexterity"}

    with mock_roll_dice(default=28):  # 28 * 2 (vulnerability) = 56 fire damage!
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=caster.entity_uuid,
            payload={"ability_name": "Fireball", "mechanics": mechanics, "target_uuids": hits, "target_wall_ids": hit_walls},
        )
        EventBus.dispatch(event)

    # Validate the results against the active map layer
    active_walls = {w.wall_id: w for w in spatial_service.map_data.active_walls}

    # The left-most (-25 to -20) and right-most (20 to 25) sections survive.
    assert active_walls[wall_ids[0]].is_solid is True
    assert active_walls[wall_ids[9]].is_solid is True

    # The inner 8 sections (-20 to 20) are destroyed (dropped below 0 hp, became non-solid)
    for i in range(1, 9):
        assert active_walls[wall_ids[i]].is_solid is False
        assert active_walls[wall_ids[i]].hp <= 0
        assert active_walls[wall_ids[i]].max_hp == 10


def test_fireball_destructible_chest(mock_roll_dice):
    """
    Test an AoE completely shattering an item sitting on the ground.
    [Mapped: REQ-OBJ-002]
    """
    # Create a 5x5 box mimicking a chest
    chest = Wall(label="Wooden Chest", start=(0, 0), end=(5, 0), is_solid=True, hp=15, vulnerabilities=["fire"])
    spatial_service.add_wall(chest)
    caster = create_test_creature("Mage", 0, 50)

    hits, hit_walls, _ = spatial_service.get_aoe_targets("sphere", 20.0, 0.0, 0.0)
    assert chest.wall_id in hit_walls

    mechanics = {"damage_dice": "8d6", "damage_type": "fire", "save_required": "dexterity"}
    with mock_roll_dice(default=28):  # 28 * 2 = 56
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=caster.entity_uuid,
            payload={"ability_name": "Fireball", "mechanics": mechanics, "target_uuids": hits, "target_wall_ids": hit_walls},
        )
        EventBus.dispatch(event)

    active_walls = {w.wall_id: w for w in spatial_service.map_data.active_walls}
    assert active_walls[chest.wall_id].is_solid is False, "The chest should be blown wide open."
    assert active_walls[chest.wall_id].hp <= 0


def test_fireball_evasion_mechanics(mock_dice, mock_roll_dice):
    """
    Test dropping a fireball on multiple combatants (one with Evasion, one without)
    to ensure the rules engine correctly halves damage on a failed save, and zeros
    it on a successful save.
    [Mapped: REQ-SPL-018]
    """
    caster = create_test_creature("Mage", 0, 0)
    caster.spell_save_dc.base_value = 15

    fighter = create_test_creature("Fighter", 5, 0)
    fighter.hp.base_value = 30

    rogue = create_test_creature("Rogue", -5, 0)
    rogue.hp.base_value = 30
    rogue.tags.append("evasion")
    rogue.dexterity_mod.base_value = 5

    hits, _, _ = spatial_service.get_aoe_targets("sphere", 20.0, 0.0, 0.0)

    mechanics = {"damage_dice": "8d6", "damage_type": "fire", "save_required": "dexterity", "half_damage_on_save": True}

    # --- SCENARIO 1: Failed Saves ---
    # Base damage = 30. DC = 15.
    # randint returns 5.
    # Fighter save: 5 + 0 = 5 (Fail). Full damage (30).
    # Rogue save: 5 + 5 = 10 (Fail). Evasion halves damage on fail (15).
    with mock_roll_dice(default=30), mock_dice(default=5):
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=caster.entity_uuid,
            payload={"ability_name": "Fireball", "mechanics": mechanics, "target_uuids": hits, "target_wall_ids": []},
        )
        EventBus.dispatch(event)

    assert fighter.hp.base_value == 0, "Fighter failed save, should take full 30 damage."
    assert rogue.hp.base_value == 15, "Rogue failed save, but Evasion halves the 30 damage to 15."

    # --- SCENARIO 2: Successful Saves ---
    fighter.hp.base_value = 30
    rogue.hp.base_value = 30
    caster.spell_save_dc.base_value = 10  # Lower DC so they both succeed

    with mock_roll_dice(default=30), mock_dice(default=10):
        EventBus.dispatch(
            GameEvent(
                event_type="SpellCast",
                source_uuid=caster.entity_uuid,
                payload={"ability_name": "Fireball", "mechanics": mechanics, "target_uuids": hits, "target_wall_ids": []},
            )
        )

    assert fighter.hp.base_value == 15, "Fighter succeeded save, should take half damage (15)."
    assert rogue.hp.base_value == 30, "Rogue succeeded save, Evasion negates damage entirely (0)."


def test_aoe_z_axis_sphere():
    """
    Test that a sphere AoE correctly calculates 3D distance and hits/misses
    entities on the Z-axis (e.g. flying creatures).
    [Mapped: REQ-SPL-008]
    """
    c_ground = create_test_creature("Ground", 20, 0)
    c_ground.z = 0.0
    spatial_service.sync_entity(c_ground)

    c_flying_low = create_test_creature("Flying Low", 0, 0)
    c_flying_low.z = 15.0
    spatial_service.sync_entity(c_flying_low)

    c_flying_high = create_test_creature("Flying High", 0, 0)
    c_flying_high.z = 25.0
    spatial_service.sync_entity(c_flying_high)

    c_flying_diag = create_test_creature("Flying Diag", 15, 15)
    c_flying_diag.z = 15.0
    spatial_service.sync_entity(c_flying_diag)

    hits, _, _ = spatial_service.get_aoe_targets("sphere", 20.0, 0.0, 0.0, origin_z=0.0)

    assert c_ground.entity_uuid in hits
    assert c_flying_low.entity_uuid in hits
    assert c_flying_high.entity_uuid not in hits, "Flying creature out of sphere radius should be missed."
    assert c_flying_diag.entity_uuid in hits


def test_aoe_z_axis_cylinder():
    """
    Test that a cylinder AoE correctly applies its height on the Z-axis.
    A cylinder goes straight up from the origin to its specified height.
    [Mapped: REQ-SPL-012]
    """
    c_ground = create_test_creature("Ground", 15, 0)
    c_ground.z = 0.0
    spatial_service.sync_entity(c_ground)

    c_flying_mid = create_test_creature("Flying Mid", 0, 0)
    c_flying_mid.z = 30.0  # Bounding box: 30 to 35.
    spatial_service.sync_entity(c_flying_mid)

    c_flying_high = create_test_creature("Flying High", 0, 0)
    c_flying_high.z = 45.0  # Bounding box: 45 to 50.
    spatial_service.sync_entity(c_flying_high)

    hits, _, _ = spatial_service.get_aoe_targets("cylinder", 20.0, 0.0, 0.0, origin_z=0.0, aoe_height=40.0)

    assert c_ground.entity_uuid in hits
    assert c_flying_mid.entity_uuid in hits, "Target within cylinder height should be hit."
    assert c_flying_high.entity_uuid not in hits, "Target above cylinder height should be safe."


def test_aoe_z_axis_cube():
    """
    Test that a cube AoE properly applies half its size on the Z-axis relative to the origin.
    A 20ft cube from origin_z=10 spans from Z=0 to Z=20.
    [Mapped: REQ-SPL-009]
    """
    c_ground = create_test_creature("Ground", 0, 0)
    c_ground.z = 0.0  # BB: 0 to 5. Hits cube [0, 20]
    spatial_service.sync_entity(c_ground)

    c_below = create_test_creature("Below", 0, 0)
    c_below.z = -10.0  # BB: -10 to -5. Misses cube [0, 20]
    spatial_service.sync_entity(c_below)

    c_above = create_test_creature("Above", 0, 0)
    c_above.z = 25.0  # BB: 25 to 30. Misses cube [0, 20]
    spatial_service.sync_entity(c_above)

    hits, _, _ = spatial_service.get_aoe_targets("cube", 20.0, 0.0, 0.0, origin_z=10.0)

    assert c_ground.entity_uuid in hits, "Ground target intersects bottom of cube."
    assert c_below.entity_uuid not in hits, "Target below cube should be safe."
    assert c_above.entity_uuid not in hits, "Target above cube should be safe."


def test_aoe_z_axis_line_lightning_bolt():
    """
    Test that a line AoE correctly calculates 3D distance and hits/misses
    entities on the Z-axis (e.g. Lightning Bolt firing up at a flying target).
    [Mapped: REQ-SPL-011]
    """
    c_ground = create_test_creature("Ground Enemy", 0, 20)
    c_ground.z = 0.0
    spatial_service.sync_entity(c_ground)

    c_flying_path = create_test_creature("Flying In Path", 0, 20)
    c_flying_path.z = 20.0
    spatial_service.sync_entity(c_flying_path)

    c_flying_above = create_test_creature("Flying Above Path", 0, 20)
    c_flying_above.z = 40.0
    spatial_service.sync_entity(c_flying_above)

    c_flying_far = create_test_creature("Flying Far", 0, 50)
    c_flying_far.z = 50.0
    spatial_service.sync_entity(c_flying_far)

    c_flying_out_of_range = create_test_creature("Flying Out Of Range", 0, 110)
    c_flying_out_of_range.z = 110.0
    spatial_service.sync_entity(c_flying_out_of_range)

    # 100ft line from (0,0,0) aimed at (0,30,30)
    hits, _, _ = spatial_service.get_aoe_targets(
        "line", 100.0, 0.0, 0.0, target_x=0.0, target_y=30.0, origin_z=0.0, target_z=30.0
    )

    assert c_ground.entity_uuid not in hits, "Ground enemy under the beam should be safe."
    assert c_flying_path.entity_uuid in hits, "Flying enemy directly in the beam should be hit."
    assert c_flying_above.entity_uuid not in hits, "Flying enemy above the beam should be safe."
    assert c_flying_far.entity_uuid in hits, "Flying enemy further along the beam should be hit."
    assert c_flying_out_of_range.entity_uuid not in hits, "Enemy past the 100ft range should be safe."


def test_aoe_z_axis_cone_upward():
    """
    Test that a cone AoE aimed diagonally upward hits a flying creature
    in the path but misses ground targets beneath it.
    [Mapped: REQ-SPL-010]
    """
    # Caster at (0,0,0) aiming at (0, 30, 30)
    c_ground = create_test_creature("Ground Enemy", 0, 20)
    c_ground.z = 0.0
    spatial_service.sync_entity(c_ground)

    c_flying_path = create_test_creature("Flying In Path", 0, 20)
    c_flying_path.z = 20.0
    spatial_service.sync_entity(c_flying_path)

    c_flying_above = create_test_creature("Flying Above", 0, 0)
    c_flying_above.z = 20.0
    spatial_service.sync_entity(c_flying_above)

    # D&D Chebyshev distance treats diagonals identically to straight lines.
    c_flying_far = create_test_creature("Flying Far", 0, 70)
    c_flying_far.z = 70.0
    spatial_service.sync_entity(c_flying_far)

    # 60ft cone aimed diagonally upward
    hits, _, _ = spatial_service.get_aoe_targets(
        "cone", 60.0, 0.0, 0.0, target_x=0.0, target_y=30.0, origin_z=0.0, target_z=30.0
    )

    assert c_ground.entity_uuid not in hits, "Ground enemy under the cone should be safe."
    assert c_flying_path.entity_uuid in hits, "Flying enemy inside the cone path should be hit."
    assert (
        c_flying_above.entity_uuid not in hits
    ), "Flying enemy directly above the caster should be outside the 53 deg cone angle."
    assert c_flying_far.entity_uuid not in hits, "Flying enemy outside 60ft radius should be safe."


def test_aoe_line_beside_beam():
    """
    Test that a line AoE (like Lightning Bolt) correctly bypasses creatures
    standing directly beside or above the 5ft wide beam.
    [Mapped: REQ-SPL-011]
    """
    c_ground = create_test_creature("Ground Enemy", 0, 20)
    c_ground.z = 0.0
    spatial_service.sync_entity(c_ground)

    c_beside = create_test_creature("Beside Beam", 10, 20)
    c_beside.z = 0.0
    spatial_service.sync_entity(c_beside)

    c_above = create_test_creature("Above Beam", 0, 20)
    c_above.z = 15.0
    spatial_service.sync_entity(c_above)

    # 100ft line from (0,0,0) aimed flatly North at (0,30,0)
    hits, _, _ = spatial_service.get_aoe_targets(
        "line", 100.0, 0.0, 0.0, target_x=0.0, target_y=30.0, origin_z=0.0, target_z=0.0
    )

    assert c_ground.entity_uuid in hits, "Enemy directly in the beam should be hit."
    assert c_beside.entity_uuid not in hits, "Enemy 10ft beside the 5ft wide beam should be safe."
    assert c_above.entity_uuid not in hits, "Enemy 15ft above the 5ft high beam should be safe."


def test_aoe_ignore_walls_darkness():
    """
    Test that spells like Darkness and Silence (with ignore_walls=True)
    pass through solid indestructible walls.
    [Mapped: REQ-EDG-006]
    """
    wall = Wall(label="Solid Stone Wall", start=(0, -20), end=(0, 20), is_solid=True, hp=9999)
    spatial_service.add_wall(wall)

    c_behind = create_test_creature("Hiding Target", 10, 0)

    # Standard sphere is blocked
    hits_blocked, _, _ = spatial_service.get_aoe_targets("sphere", 20.0, -5.0, 0.0)
    assert c_behind.entity_uuid not in hits_blocked

    # Darkness/Silence ignores walls for its AoE spread
    hits_penetrate, _, _ = spatial_service.get_aoe_targets("sphere", 20.0, -5.0, 0.0, ignore_walls=True)
    assert c_behind.entity_uuid in hits_penetrate


def test_aoe_lightning_bolt_destructible_door(mock_dice, mock_roll_dice):
    """
    Test that a line spell like Lightning Bolt (with penetrates_destructible=True)
    hits the destructible door and the entity behind it, whereas an indestructible wall blocks it.
    [Mapped: REQ-OBJ-002]
    """
    door = Wall(label="Wooden Door", start=(0, -5), end=(0, 5), is_solid=True, hp=10, max_hp=10)
    spatial_service.add_wall(door)

    iron_wall = Wall(label="Iron Wall", start=(0, 15), end=(0, 25), is_solid=True, hp=9999)
    spatial_service.add_wall(iron_wall)

    c_behind_door = create_test_creature("Behind Door", 10, 0)
    c_behind_iron = create_test_creature("Behind Iron Wall", 10, 20)

    hits_door, hit_walls_door, _ = spatial_service.get_aoe_targets(
        "line", 100.0, -10.0, 0.0, target_x=20.0, target_y=0.0, penetrates_destructible=True
    )
    assert door.wall_id in hit_walls_door
    assert c_behind_door.entity_uuid in hits_door, "Target behind destructible door should be hit."

    hits_iron, hit_walls_iron, _ = spatial_service.get_aoe_targets(
        "line", 100.0, -10.0, 20.0, target_x=20.0, target_y=20.0, penetrates_destructible=True
    )
    assert iron_wall.wall_id in hit_walls_iron
    assert c_behind_iron.entity_uuid not in hits_iron, "Target behind indestructible wall should be safe."

    mechanics = {"damage_dice": "8d6", "damage_type": "lightning", "save_required": "dexterity"}
    caster = create_test_creature("Mage", -10, 0)

    with mock_roll_dice(default=30), mock_dice(default=5):
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=caster.entity_uuid,
            payload={
                "ability_name": "Lightning Bolt",
                "mechanics": mechanics,
                "target_uuids": hits_door,
                "target_wall_ids": hit_walls_door,
            },
        )
        EventBus.dispatch(event)

    active_walls = {w.wall_id: w for w in spatial_service.map_data.active_walls}
    assert active_walls[door.wall_id].is_solid is False
    assert active_walls[door.wall_id].hp <= 0
    assert c_behind_door.hp.base_value < 10


def test_aoe_elemental_terrain_lightning_water(mock_roll_dice):
    """
    Test that lightning extends through a wet terrain zone, hitting targets outside the direct ray.
    [Mapped: REQ-ENV-010]
    """
    caster = create_test_creature("Mage", 0, 0)
    t_direct = create_test_creature("Direct Target", 0, 20)
    t_water = create_test_creature("Water Target", 15, 20)  # 15ft away from the beam!

    puddle = TerrainZone(label="Giant Puddle", points=[(-5, 10), (20, 10), (20, 30), (-5, 30)], tags=["wet"])
    spatial_service.add_terrain(puddle)

    # Lightning bolt straight up the Y axis (5ft wide)
    hits, walls, terrains = spatial_service.get_aoe_targets("line", 100.0, 0.0, 0.0, target_x=0.0, target_y=30.0)

    assert t_direct.entity_uuid in hits
    assert t_water.entity_uuid not in hits  # Ensure math correctly bypassed the water target directly
    assert puddle.zone_id in terrains

    mechanics = {"damage_dice": "8d6", "damage_type": "lightning"}

    with mock_roll_dice(default=30):
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=caster.entity_uuid,
            payload={
                "ability_name": "Lightning",
                "mechanics": mechanics,
                "target_uuids": hits,
                "target_terrain_ids": terrains,
            },
        )
        EventBus.dispatch(event)

    # Because of the elemental extension, t_water should take the 30 damage anyway!
    assert t_direct.hp.base_value == -20
    assert t_water.hp.base_value == -20
    assert any("electrified" in res for res in event.payload["results"])


def test_aoe_elemental_terrain_fire_thorns(mock_roll_dice):
    """
    Test that fire ignites flammable difficult terrain, dealing immediate damage and creating a trap.
    [Mapped: REQ-ENV-010]
    """
    caster = create_test_creature("Mage", 0, 0)
    target_in_web = create_test_creature("Goblin", 10, 20)

    thorns = TerrainZone(
        label="Spike Growth", points=[(-5, 10), (20, 10), (20, 30), (-5, 30)], is_difficult=True, tags=["flammable", "thorns"]
    )
    spatial_service.add_terrain(thorns)

    hits, walls, terrains = spatial_service.get_aoe_targets("sphere", 20.0, 5.0, 15.0)
    assert thorns.zone_id in terrains

    mechanics = {"damage_dice": "8d6", "damage_type": "fire"}

    # Mock the 2d4 burn damage to be exactly 5, and the Fireball to be exactly 5 (Total 10 damage)
    with mock_roll_dice(default=5):
        event = GameEvent(
            event_type="SpellCast",
            source_uuid=caster.entity_uuid,
            payload={"ability_name": "Fireball", "mechanics": mechanics, "target_uuids": hits, "target_terrain_ids": terrains},
        )
        EventBus.dispatch(event)

    assert "flammable" not in thorns.tags
    assert "burning" in thorns.tags
    assert thorns.is_difficult is False
    assert "Burning" in thorns.label
    assert thorns.duration_seconds == 6
    assert thorns.trap is not None
    assert thorns.trap.damage_type == "fire"

    # Goblin took 5 fire damage from the immediate Web burning + 5 damage from the fireball
    assert target_in_web.hp.base_value == 0


def test_aoe_elemental_terrain_cold_water_freezes():
    """
    Test that cold damage freezes a wet puddle.
    [Mapped: REQ-ENV-010]
    """
    caster = create_test_creature("Mage", 0, 0)
    puddle = TerrainZone(label="Puddle", points=[(-5, 10), (20, 10), (20, 30), (-5, 30)], is_difficult=False, tags=["wet"])
    spatial_service.add_terrain(puddle)

    hits, walls, terrains = spatial_service.get_aoe_targets("sphere", 20.0, 5.0, 15.0)
    mechanics = {"damage_dice": "8d6", "damage_type": "cold"}
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={"ability_name": "Cone of Cold", "mechanics": mechanics, "target_uuids": hits, "target_terrain_ids": terrains},
    )
    EventBus.dispatch(event)

    assert "wet" not in puddle.tags
    assert "frozen" in puddle.tags
    assert puddle.is_difficult is True


def test_aoe_elemental_terrain_wind_cloud():
    """
    Test that a wind spell physically moves a gaseous terrain zone.
    [Mapped: REQ-ENV-010]
    """
    caster = create_test_creature("Mage", 0, 0)
    cloud = TerrainZone(label="Toxic Cloud", points=[(0.0, 10.0), (10.0, 10.0), (10.0, 20.0), (0.0, 20.0)], tags=["gaseous"])
    spatial_service.add_terrain(cloud)

    # Gust of Wind cast straight north (along +Y)
    hits, walls, terrains = spatial_service.get_aoe_targets("line", 60.0, 0.0, 0.0, target_x=0.0, target_y=10.0)

    mechanics = {"damage_dice": "", "damage_type": "wind"}
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={
            "ability_name": "Gust of Wind",
            "mechanics": mechanics,
            "target_uuids": hits,
            "target_terrain_ids": [cloud.zone_id],  # Mocking the intersection hit
            "origin_x": 0.0,
            "origin_y": 0.0,
            "target_x": 0.0,
            "target_y": 10.0,
        },
    )
    EventBus.dispatch(event)

    # Vector is (0, 1) * 20ft distance
    assert cloud.points[0] == (0.0, 30.0)
    assert cloud.points[1] == (10.0, 30.0)
    assert cloud.points[2] == (10.0, 40.0)
    assert cloud.points[3] == (0.0, 40.0)


def test_terrain_duration_and_concentration():
    """
    Test that dropping concentration completely removes tied terrain effects.
    [Mapped: REQ-CND-017]
    """
    caster = create_test_creature("Mage", 0, 0)

    mechanics = {
        "requires_concentration": True,
        "terrain_effect": {"label": "Web", "duration": "1 hour", "is_difficult": True, "tags": ["flammable"]},
    }
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={
            "ability_name": "Web",
            "mechanics": mechanics,
            "origin_x": 10.0,
            "origin_y": 10.0,
            "target_x": 10.0,
            "target_y": 10.0,
            "aoe_shape": "cube",
            "aoe_size": 20.0,
        },
    )
    EventBus.dispatch(event)

    assert caster.concentrating_on == "Web"
    assert len(spatial_service.map_data.temporary_terrain) == 1

    EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid))
    assert caster.concentrating_on == ""
    assert len(spatial_service.map_data.temporary_terrain) == 0


def test_terrain_duration_expiration():
    """
    Test that advancing time naturally expires temporary terrain.
    [Mapped: REQ-ACT-001]
    """
    caster = create_test_creature("Mage", 0, 0)
    mechanics = {"terrain_effect": {"label": "Grease", "duration": "1 minute", "is_difficult": True}}
    event = GameEvent(
        event_type="SpellCast",
        source_uuid=caster.entity_uuid,
        payload={
            "ability_name": "Grease",
            "mechanics": mechanics,
            "origin_x": 10.0,
            "origin_y": 10.0,
            "target_x": 10.0,
            "target_y": 10.0,
            "aoe_shape": "cube",
            "aoe_size": 10.0,
        },
    )
    EventBus.dispatch(event)

    assert len(spatial_service.map_data.temporary_terrain) == 1

    # Advance 30 seconds (half duration) -> Still alive
    EventBus.dispatch(GameEvent(event_type="AdvanceTime", source_uuid=caster.entity_uuid, payload={"seconds_advanced": 30}))
    assert len(spatial_service.map_data.temporary_terrain) == 1

    # Advance remaining 30 seconds -> Dispels correctly
    EventBus.dispatch(GameEvent(event_type="AdvanceTime", source_uuid=caster.entity_uuid, payload={"seconds_advanced": 30}))
    assert len(spatial_service.map_data.temporary_terrain) == 0
