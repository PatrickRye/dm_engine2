# test/server/test_subclass_remapping.py
"""Tests for REQ-ENT-004 — 2014 to 2024 subclass name remapping."""
import pytest

from vault_io import _remap_subclass_name, _SUBCLASS_2014_TO_2024


class TestSubclassRemapping:
    """REQ-ENT-004: Legacy 2014 subclass names must redirect to 2024 names."""

    def test_totem_warrior_remaps_to_wild_heart(self):
        """Barbarian "Path of the Totem Warrior" → "Path of the Wild Heart"."""
        result = _remap_subclass_name("Path of the Totem Warrior")
        assert result == "Path of the Wild Heart"

    def test_circle_of_the_moon_remaps_to_moon_druid(self):
        """Druid "Circle of the Moon" → "Moon Druid"."""
        result = _remap_subclass_name("Circle of the Moon")
        assert result == "Moon Druid"

    def test_circle_of_the_land_remaps_to_land_druid(self):
        result = _remap_subclass_name("Circle of the Land")
        assert result == "Land Druid"

    def test_oath_of_devotion_remaps_to_devotion(self):
        """Paladin "Oath of Devotion" → "Devotion"."""
        result = _remap_subclass_name("Oath of Devotion")
        assert result == "Devotion"

    def test_dragonic_bloodline_remaps_to_draconic(self):
        """Sorcerer "Draconic Bloodline" → "Draconic"."""
        result = _remap_subclass_name("Draconic Bloodline")
        assert result == "Draconic"

    def test_wild_magic_bloodline_remaps_to_wild_magic(self):
        result = _remap_subclass_name("Wild Magic Bloodline")
        assert result == "Wild Magic"

    def test_storm_sorcery_remaps_to_storm_sorcerer(self):
        result = _remap_subclass_name("Storm Sorcery")
        assert result == "Storm Sorcerer"

    def test_way_of_the_four_elements_remaps_to_elements(self):
        """Monk "Way of the Four Elements" → "Way of the Elements"."""
        result = _remap_subclass_name("Way of the Four Elements")
        assert result == "Way of the Elements"

    def test_way_of_the_open_hand_remaps_to_open_palm(self):
        """Monk "Way of the Open Hand" → "Way of the Open Palm"."""
        result = _remap_subclass_name("Way of the Open Hand")
        assert result == "Way of the Open Palm"

    def test_hexblade_unmodified(self):
        """"Hexblade" was not changed in 2024 (or already correct)."""
        result = _remap_subclass_name("Hexblade")
        assert result == "Hexblade"

    def test_unknown_subclass_returns_unchanged(self):
        """Unknown subclass names pass through without modification."""
        result = _remap_subclass_name("My Custom Subclass")
        assert result == "My Custom Subclass"

    def test_empty_string_returns_empty_string(self):
        """Empty string input returns empty string (handled gracefully)."""
        result = _remap_subclass_name("")
        assert result == ""

    def test_already_2024_name_returns_unchanged(self):
        """If a subclass name is already the 2024 version, it returns unchanged."""
        result = _remap_subclass_name("Path of the Wild Heart")
        assert result == "Path of the Wild Heart"

    def test_school_prefix_remapping(self):
        """Wizard schools with "School of" prefix map correctly."""
        result = _remap_subclass_name("School of Abjuration")
        assert result == "Abjuration"
        result = _remap_subclass_name("School of Evocation")
        assert result == "Evocation"
        result = _remap_subclass_name("School of Necromancy")
        assert result == "Necromancy"

    def test_remapping_table_has_all_major_classes(self):
        """Verify the remapping table covers the major class subclass groups."""
        # Barbarian
        assert "Path of the Totem Warrior" in _SUBCLASS_2014_TO_2024
        # Bard
        assert "College of Lore" in _SUBCLASS_2014_TO_2024
        # Cleric (domains)
        assert "Life Domain" in _SUBCLASS_2014_TO_2024
        assert "Light Domain" in _SUBCLASS_2014_TO_2024
        # Druid
        assert "Circle of the Moon" in _SUBCLASS_2014_TO_2024
        assert "Circle of the Land" in _SUBCLASS_2014_TO_2024
        # Fighter
        assert "Champion" in _SUBCLASS_2014_TO_2024
        assert "Battle Master" in _SUBCLASS_2014_TO_2024
        # Monk
        assert "Way of the Four Elements" in _SUBCLASS_2014_TO_2024
        # Paladin
        assert "Oath of Devotion" in _SUBCLASS_2014_TO_2024
        assert "Oath of the Ancients" in _SUBCLASS_2014_TO_2024
        # Ranger
        assert "Hunter" in _SUBCLASS_2014_TO_2024
        assert "Beast Master" in _SUBCLASS_2014_TO_2024
        # Rogue
        assert "Thief" in _SUBCLASS_2014_TO_2024
        assert "Arcane Trickster" in _SUBCLASS_2014_TO_2024
        # Sorcerer
        assert "Draconic Bloodline" in _SUBCLASS_2014_TO_2024
        assert "Wild Magic Bloodline" in _SUBCLASS_2014_TO_2024
        # Warlock
        assert "Archfey Patron" in _SUBCLASS_2014_TO_2024
        assert "Fiend Patron" in _SUBCLASS_2014_TO_2024
        assert "Great Old One Patron" in _SUBCLASS_2014_TO_2024
        # Wizard
        assert "School of Abjuration" in _SUBCLASS_2014_TO_2024
        assert "School of Illusion" in _SUBCLASS_2014_TO_2024
