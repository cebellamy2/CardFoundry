"""The (L)/(C) marker rule, and the packing slip that prints it.

The rule: a LAND is (L) whatever its colour, a colourless NON-land is (C),
a coloured card keeps its colour, and a card whose type we have not looked
up yet gets NOTHING -- never a confident, wrong (C).
"""
import pytest

from card_color_marker import color_marker, front_face_type_line, is_land
from packing_slip_service import _color_suffix


class _Item:
    def __init__(self, name, color, type_line):
        self.name = name
        self.color = color
        self.type_line = type_line


# ---------------------------------------------------------------------
# Lands
# ---------------------------------------------------------------------

@pytest.mark.parametrize("type_line", [
    "Land",
    "Basic Land — Forest",
    "Land — Desert",
    "Legendary Land",
    "Artifact Land",
    "Snow Land — Forest",
])
def test_a_land_is_L(type_line):
    assert color_marker("", type_line) == "L"


def test_a_COLOURED_land_is_still_L():
    """★ Dryad Arbor: a green creature-land. The operator's rule is that
    every land shows (L) whatever mana it makes or colour it is."""
    assert color_marker("G", "Land Creature — Forest Dryad") == "L"


def test_a_multicoloured_land_is_still_L():
    assert color_marker("WU", "Land Creature — Plains Island") == "L"


# ---------------------------------------------------------------------
# Colourless non-lands
# ---------------------------------------------------------------------

@pytest.mark.parametrize("type_line", [
    "Artifact",
    "Legendary Artifact — Equipment",
    "Artifact Creature — Construct",
    "Creature — Eldrazi",
    "Instant",
])
def test_a_colourless_non_land_is_C(type_line):
    assert color_marker("", type_line) == "C"


# ---------------------------------------------------------------------
# Coloured cards are untouched
# ---------------------------------------------------------------------

@pytest.mark.parametrize("color", ["R", "WU", "WUBRG", "BG"])
def test_a_coloured_card_keeps_its_colour(color):
    assert color_marker(color, "Creature — Human Wizard") == color


def test_a_coloured_card_with_no_type_line_still_shows_its_colour():
    """Only the LAND/COLOURLESS distinction needs the type line. A card we
    already know is red is still red."""
    assert color_marker("R", None) == "R"


# ---------------------------------------------------------------------
# ★ Missing data shows NOTHING, never (C)
# ---------------------------------------------------------------------

@pytest.mark.parametrize("color,type_line", [
    ("", None),      # resolved colourless, type not looked up yet
    (None, None),    # nothing resolved at all
    ("", ""),
    (None, ""),
])
def test_missing_type_data_shows_no_marker(color, type_line):
    assert color_marker(color, type_line) == ""


def test_a_missing_type_line_is_never_reported_as_colourless():
    """The whole point: '' means colourless, NULL means unknown, and the
    two must not print the same thing."""
    assert color_marker("", None) == ""
    assert color_marker("", "Artifact") == "C"


# ---------------------------------------------------------------------
# ★ Double-faced and modal cards go by the FRONT face
# ---------------------------------------------------------------------

def test_a_spell_front_with_a_land_back_is_NOT_a_land():
    """★ Scryfall's top-level type_line JOINS the faces, so a substring
    test for "Land" would mark this front-face creature as a land."""
    joined = "Creature — Elf Druid // Land"
    assert is_land(joined) is False
    assert color_marker("G", joined) == "G"


def test_a_land_front_with_a_spell_back_IS_a_land():
    joined = "Land // Creature — Elf Druid"
    assert is_land(joined) is True
    assert color_marker("G", joined) == "L"


def test_a_colourless_spell_front_with_a_land_back_is_C_not_L():
    assert color_marker("", "Artifact // Land") == "C"


def test_front_face_type_line_splits_on_the_separator():
    assert front_face_type_line("Creature — Elf // Land") == "Creature — Elf"
    assert front_face_type_line("Instant") == "Instant"
    assert front_face_type_line(None) == ""


def test_a_subtype_mentioning_land_is_not_a_land():
    """Only the TYPES block (left of the em dash) carries card types."""
    assert is_land("Creature — Landwalker") is False
    assert is_land("Enchantment — Aura") is False


# ---------------------------------------------------------------------
# The packing slip's own rendering
# ---------------------------------------------------------------------

def test_the_packing_slip_prints_the_marker_in_parentheses():
    assert _color_suffix("", "Basic Land — Forest") == " (L)"
    assert _color_suffix("", "Artifact") == " (C)"
    assert _color_suffix("WU", "Creature — Bird") == " (WU)"
    assert _color_suffix("", None) == ""


def test_the_packing_slip_is_unchanged_for_coloured_cards():
    """Pins the pre-existing behaviour: this build must not move a single
    coloured card's marker."""
    for color in ("R", "WU", "BG", "WUBRG"):
        assert _color_suffix(color, "Creature — Human") == f" ({color})"
        # ...and with no type line at all, exactly as before this build.
        assert _color_suffix(color) == f" ({color})"


def test_a_row_without_a_type_line_attribute_still_renders():
    """Defensive: _draw_item_row reads type_line via getattr, so an object
    that predates the column cannot break a slip."""
    class Old:
        name = "Lightning Bolt"
        color = "R"

    assert _color_suffix(Old.color, getattr(Old, "type_line", None)) == " (R)"
