# test/server/test_attunement_rules.py
"""Tests for REQ-LOT-005 — Attunement during a short rest blocks HP recovery from hit dice."""
import pytest
from unittest.mock import patch

from dnd_rules_engine import Creature, GameEvent, EventBus, ModifiableValue
from registry import clear_registry, register_entity
from spatial_engine import spatial_service


@pytest.fixture(autouse=True)
def setup(mock_obsidian_vault):
    clear_registry()
    spatial_service.clear()
    yield mock_obsidian_vault


def _make_creature(
    vault_path,
    name="Guts",
    current_hp=10,
    max_hp=30,
    con_mod=3,
    hd_current=3,
    hd_max=5,
    hd_size=10,
):
    """Create a creature with Hit Dice resource."""
    c = Creature(
        name=name,
        vault_path=vault_path,
        x=0, y=0,
        hp=ModifiableValue(base_value=current_hp),
        ac=ModifiableValue(base_value=15),
        strength_mod=ModifiableValue(base_value=1),
        dexterity_mod=ModifiableValue(base_value=1),
        constitution_mod=ModifiableValue(base_value=con_mod),
    )
    c.max_hp = max_hp
    c.resources[f"Hit Dice (d{hd_size})"] = f"{hd_current}/{hd_max}"
    register_entity(c)
    return c


def _dispatch_short_rest(
    creature,
    vault_path,
    dice_to_spend=0,
    rest_start_day=1,
    rest_start_hour=18,
):
    """Dispatch a short rest GameEvent for the given creature."""
    event = GameEvent(
        event_type="Rest",
        source_uuid=creature.entity_uuid,
        vault_path=vault_path,
        payload={
            "rest_type": "short",
            "target_uuids": [creature.entity_uuid],
            "hit_dice_to_spend": dice_to_spend,
            "rest_start_day": rest_start_day,
            "rest_start_hour": rest_start_hour,
        },
    )
    EventBus.dispatch(event)


def _dispatch_long_rest(creature, vault_path):
    """Dispatch a long rest GameEvent for the given creature."""
    event = GameEvent(
        event_type="Rest",
        source_uuid=creature.entity_uuid,
        vault_path=vault_path,
        payload={
            "rest_type": "long",
            "target_uuids": [creature.entity_uuid],
            "hit_dice_to_spend": 0,
        },
    )
    EventBus.dispatch(event)


class TestAttunementBlocksHP:
    """REQ-LOT-005: Attuning during a short rest blocks HP recovery via hit dice."""

    def test_attuned_this_short_rest_blocks_hit_dice_healing(self, setup):
        """REQ-LOT-005: HP must NOT be recovered when attuned_this_short_rest is True."""
        vp = setup
        creature = _make_creature(vp, current_hp=10, max_hp=30, con_mod=3, hd_current=3, hd_max=5)
        creature.attuned_this_short_rest = True

        _dispatch_short_rest(creature, vp, dice_to_spend=2)

        # HP must remain at 10 — attunement blocked hit dice healing
        assert creature.hp.base_value == 10, (
            f"Expected HP to stay at 10 (attunement blocked healing), got {creature.hp.base_value}. "
            "REQ-LOT-005: Attuning during a short rest must block HP recovery via hit dice."
        )

    def test_attuned_flag_cleared_after_rest_event(self, setup):
        """REQ-LOT-005: attuned_this_short_rest must be reset after rest completes."""
        vp = setup
        creature = _make_creature(vp, current_hp=10, max_hp=30, con_mod=3, hd_current=3, hd_max=5)
        creature.attuned_this_short_rest = True

        _dispatch_short_rest(creature, vp, dice_to_spend=0)

        assert creature.attuned_this_short_rest is False, (
            "attuned_this_short_rest must be reset to False after rest event completes."
        )

    def test_no_attunement_allows_hit_dice_healing(self, setup):
        """Without attunement, short rest normally restores HP via hit dice."""
        vp = setup
        creature = _make_creature(vp, current_hp=10, max_hp=30, con_mod=3, hd_current=3, hd_max=5)
        creature.attuned_this_short_rest = False

        # Roll 10 on each die (1d10+3 = 13 each), 2 dice = 26 total, capped at max HP
        with patch("dnd_rules_engine.random.randint", return_value=10):
            _dispatch_short_rest(creature, vp, dice_to_spend=2)

        # HP should increase from 10
        assert creature.hp.base_value > 10, (
            f"Expected HP to increase from hit dice healing, got {creature.hp.base_value}. "
            "Without attunement, short rest should allow HP recovery via hit dice."
        )

    def test_long_rest_not_blocked_by_attuned_flag(self, setup):
        """REQ-LOT-005: Long rest HP restore is not affected by attuned_this_short_rest."""
        vp = setup
        creature = _make_creature(vp, current_hp=10, max_hp=30, con_mod=3, hd_current=0, hd_max=5)
        creature.attuned_this_short_rest = True  # set from a prior short rest attunement

        _dispatch_long_rest(creature, vp)

        # Long rest fully restores HP regardless of attuned_this_short_rest
        assert creature.hp.base_value == 30, (
            f"Expected HP to be fully restored to 30 on long rest, got {creature.hp.base_value}. "
            "Long rest HP restore must not be blocked by attuned_this_short_rest."
        )

    def test_hit_dice_reduced_after_spending(self, setup):
        """Hit dice count must decrease after being spent on a short rest."""
        vp = setup
        creature = _make_creature(vp, current_hp=10, max_hp=30, con_mod=3, hd_current=3, hd_max=5)
        creature.attuned_this_short_rest = False

        with patch("dnd_rules_engine.random.randint", return_value=10):
            _dispatch_short_rest(creature, vp, dice_to_spend=2)

        # Hit dice remaining should be 3-2=1
        assert creature.resources["Hit Dice (d10)"] == "1/5", (
            f"Expected 1/5 hit dice remaining, got {creature.resources['Hit Dice (d10)']}. "
            "Hit dice must be spent during short rest HP recovery."
        )

    def test_attunement_with_no_hit_dice_remaining(self, setup):
        """If no hit dice remain, attunement flag being set has no HP effect to block."""
        vp = setup
        creature = _make_creature(vp, current_hp=10, max_hp=30, con_mod=3, hd_current=0, hd_max=5)
        creature.attuned_this_short_rest = True

        _dispatch_short_rest(creature, vp, dice_to_spend=2)

        # HP stays at 10 (no dice to spend anyway)
        assert creature.hp.base_value == 10
        # Flag should still be cleared
        assert creature.attuned_this_short_rest is False
