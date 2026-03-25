"""
Tests for the spatial engine's get_perceivers function.
Validates spatial radius boundaries, line-of-sight visual blocking,
and native conditions (Deafened, Silenced) for auditory perception.
"""

import pytest
from spatial_engine import spatial_service, Wall, MapData, HAS_GIS
from dnd_rules_engine import Creature, ModifiableValue, ActiveCondition
from registry import clear_registry

# Skip all spatial tests if the shapely/rtree GIS libraries are not installed
pytestmark = pytest.mark.skipif(not HAS_GIS, reason="GIS libraries (shapely, rtree) are required for spatial tests.")


@pytest.fixture(autouse=True)
def setup_spatial_env():
    clear_registry()
    spatial_service.clear()
    spatial_service.load_map(MapData())
    yield
    spatial_service.clear()
    clear_registry()


def create_dummy_creature(name: str, x: float, y: float, active_conditions=None) -> Creature:
    c = Creature(
        name=name,
        hp=ModifiableValue(base_value=10),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
        x=x,
        y=y,
        active_conditions=active_conditions or [],
    )
    spatial_service.sync_entity(c)
    return c


def test_get_perceivers_basic_proximity():
    source = create_dummy_creature("Source", 0, 0)
    close_target = create_dummy_creature("CloseTarget", 10, 0)
    far_target = create_dummy_creature("FarTarget", 100, 0)

    perceivers = spatial_service.get_perceivers(source.entity_uuid, radius=60.0, require_los=False)

    assert close_target.entity_uuid in perceivers
    assert far_target.entity_uuid not in perceivers
    assert source.entity_uuid not in perceivers  # Should not echo back to the sender


def test_get_perceivers_line_of_sight():
    source = create_dummy_creature("Source", 0, 0)
    visible_target = create_dummy_creature("VisibleTarget", 10, 0)
    blocked_target = create_dummy_creature("BlockedTarget", -10, 0)

    # Place an opaque wall between Source (0,0) and BlockedTarget (-10,0)
    wall = Wall(start=(-5.0, -5.0), end=(-5.0, 5.0), is_solid=True, is_visible=True)
    spatial_service.add_wall(wall)

    perceivers = spatial_service.get_perceivers(source.entity_uuid, radius=60.0, require_los=True)

    assert visible_target.entity_uuid in perceivers
    assert blocked_target.entity_uuid not in perceivers


def test_get_perceivers_auditory_deafened():
    source = create_dummy_creature("Source", 0, 0)
    deafened_cond = ActiveCondition(name="Deafened")
    deaf_target = create_dummy_creature("DeafTarget", 10, 0, active_conditions=[deafened_cond])
    normal_target = create_dummy_creature("NormalTarget", 10, 10)

    perceivers = spatial_service.get_perceivers(source.entity_uuid, radius=60.0, require_los=False)
    assert normal_target.entity_uuid in perceivers
    assert deaf_target.entity_uuid not in perceivers


def test_get_perceivers_auditory_silenced():
    silenced_cond = ActiveCondition(name="Silenced")
    source = create_dummy_creature("Source", 0, 0, active_conditions=[silenced_cond])
    target = create_dummy_creature("Target", 10, 0)

    perceivers = spatial_service.get_perceivers(source.entity_uuid, radius=60.0, require_los=False)
    assert len(perceivers) == 0
