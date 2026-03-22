import os
import pytest
from dnd_rules_engine import Creature, ModifiableValue
from spatial_engine import spatial_service
from registry import clear_registry, register_entity


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _make_sorcerer(name, vault_path, sp_current, sp_max, slot_level, slot_current, slot_max):
    c = Creature(
        name=name,
        vault_path=vault_path,
        hp=ModifiableValue(base_value=20),
        ac=ModifiableValue(base_value=10),
        strength_mod=ModifiableValue(base_value=0),
        dexterity_mod=ModifiableValue(base_value=0),
    )
    c.resources = {"Sorcery Points": f"{sp_current}/{sp_max}", f"Level {slot_level} Spell Slots": f"{slot_current}/{slot_max}"}
    register_entity(c)
    spatial_service.sync_entity(c)
    return c


@pytest.mark.asyncio
async def test_req_cls_009_font_of_magic_creation(setup):
    """Trace: REQ-CLS-009 - Validates that a Sorcerer can convert Sorcery Points into a Spell Slot."""
    from tools import use_font_of_magic
    from vault_io import get_journals_dir

    vp = setup
    sorc = _make_sorcerer("Sorcerer", vp, sp_current=5, sp_max=5, slot_level=1, slot_current=0, slot_max=4)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Sorcerer.md"), "w") as f:
        f.write("---\nname: Sorcerer\nresources:\n  Sorcery Points: 5/5\n  Level 1 Spell Slots: 0/4\n---\n")

    config = {"configurable": {"thread_id": vp}}
    # Creating a 1st level slot costs 2 SP
    res = await use_font_of_magic.ainvoke(
        {"character_name": "Sorcerer", "action": "create_slot", "slot_level": 1}, config=config
    )

    assert "MECHANICAL TRUTH" in res
    assert "Level 1" in res
    assert sorc.resources["Sorcery Points"] == "3/5"
    assert sorc.resources["Level 1 Spell Slots"] == "1/4"


@pytest.mark.asyncio
async def test_req_cls_010_font_of_magic_conversion(setup):
    """Trace: REQ-CLS-010 - Validates that a Sorcerer can convert an existing Spell Slot into Sorcery Points."""
    from tools import use_font_of_magic
    from vault_io import get_journals_dir

    vp = setup
    sorc = _make_sorcerer("Sorcerer", vp, sp_current=0, sp_max=5, slot_level=2, slot_current=1, slot_max=3)

    j_dir = get_journals_dir(vp)
    with open(os.path.join(j_dir, "Sorcerer.md"), "w") as f:
        f.write("---\nname: Sorcerer\nresources:\n  Sorcery Points: 0/5\n  Level 2 Spell Slots: 1/3\n---\n")

    config = {"configurable": {"thread_id": vp}}
    # Converting a 2nd level slot yields 2 SP
    res = await use_font_of_magic.ainvoke(
        {"character_name": "Sorcerer", "action": "convert_slot", "slot_level": 2}, config=config
    )

    assert "MECHANICAL TRUTH" in res
    assert sorc.resources["Sorcery Points"] == "2/5"
    assert sorc.resources["Level 2 Spell Slots"] == "0/3"
