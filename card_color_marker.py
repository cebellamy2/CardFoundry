"""One rule for the marker printed next to a card name.

Packing slips are often printed in black and white, so the marker carries
information colour alone cannot. Before this, the marker was just the
colour string, which meant a colourless card and a land both stored `''`
and got NOTHING -- 2,450 of 11,303 order items on a real production
database, a mix of lands (Valakut, Rogue's Passage, Myriad Landscape) and
colourless artifacts (Skullclamp, Mind Stone, Ruby Medallion), all printed
identically.

THE RULE (operator decision, 2026-09-24/28):
    a LAND         -> (L)   whatever its colour. Dryad Arbor is (L), not (G).
    a COLOURLESS
      NON-land     -> (C)
    anything else  -> its colour, unchanged: (WU), (R), ...
    NOT KNOWN YET  -> nothing at all, NEVER (C)

★ THE LAST LINE IS THE ONE THAT MATTERS. "Colourless" and "we have not
looked it up yet" are different states, and the schema already tells them
apart: `color = ''` is a resolved colourless card, `color IS NULL` is
unresolved, and a missing `type_line` means the card type is unknown. A
card whose type we do not know is not evidence of anything, so it gets no
marker rather than a confident, wrong (C).

★ DOUBLE-FACED AND MODAL CARDS GO BY THE FRONT FACE. Scryfall's top-level
`type_line` JOINS the faces -- "Creature — Elf Druid // Land" -- so a
substring test for "Land" marks a front-face SPELL as a land. This splits
on " // " and reads the FRONT segment only, which is what the physical
card shows and what `legacy_import_service.scryfall_card_colors` already
does for colour, for the same stated reason: it is "what a physical card
actually shows at a glance in a binder or pick list".

(`legacy_import_service.classify_legacy_batch` still uses the naive
substring test. That is a real latent bug in a different flow -- it is
logged separately and deliberately not touched here.)

This module is deliberately dependency-free so both the PDF renderer
(packing_slip_service) and, later, main.py's HTML `_color_badge` can
import it. It cannot live in main.py: packing_slip_service would then
import main and create a cycle.
"""

FACE_SEPARATOR = " // "
LAND_MARKER = "L"
COLORLESS_MARKER = "C"


def front_face_type_line(type_line: str | None) -> str:
    """The front face's type line only.

    A single-faced card has no separator and comes back unchanged.
    """
    if not type_line:
        return ""
    return str(type_line).split(FACE_SEPARATOR)[0].strip()


def is_land(type_line: str | None) -> bool:
    """True when the FRONT face is a land.

    Matches the word, not the substring: "Land" appears in real non-land
    type lines -- "Landwalk" is not a type, but "Legendary Creature —
    Landfall" style wording and card names in type lines make a bare `in`
    test needlessly fragile, so this compares tokens.
    """
    front = front_face_type_line(type_line)
    if not front:
        return False
    # Type lines separate the supertype/type block from subtypes with an
    # em dash. Only the left side carries card TYPES.
    types_block = front.split("—")[0]
    return "land" in types_block.casefold().split()


def color_marker(color: str | None, type_line: str | None) -> str:
    """The marker letters for one card, or "" for no marker at all.

    Returns the bare letters; the caller decides how to present them --
    " (L)" in the PDF, a badge in HTML.
    """
    if is_land(type_line):
        return LAND_MARKER
    if color:
        return str(color)
    # Colourless, but only if we actually know the card's type. A missing
    # type line means unknown, not colourless.
    if type_line:
        return COLORLESS_MARKER
    return ""


# ---------------------------------------------------------------------
# Sort groups (operator decision, 2026-10-26)
# ---------------------------------------------------------------------
#
# The packing slip groups its lines by colour, in the order below, A-Z by
# card name within each group. The group is derived from `color_marker`'s
# OWN return value rather than from `color`/`type_line` again, which is
# what makes it structurally impossible for the printed marker and the
# group a line sorts into to disagree: if the marker says (L), the line is
# in the Lands group, because the marker is the input.
#
#   0  Multicolour   -- two or more colours, not a land
#   1-5  W U B R G   -- mono-coloured, not a land
#   6  Colourless    -- (C), a non-land we have resolved as colourless
#   7  Lands         -- (L), any colour, same rule as the marker
#   8  Unclassified  -- a marker we do not recognise (see below)
#   9  Unknown       -- no marker at all: colour not looked up yet
#
# ★ GROUP 8 IS THE "UNCLASSIFIED" BUCKET, not dead code. `color` is only
# ever WUBRG letters today, so a single letter outside WUBRG cannot occur
# -- but if one ever does, it must not fall into a real colour group
# silently, and it must not be mistaken for group 9 either, because group 9
# means "we never looked it up" and this means "we looked and got something
# we have no rule for". It sorts just before Unknown, where the operator
# will see it at the end of the slip rather than buried mid-list.
#
# ★ GROUP 9 IS LAST BY THE OPERATOR'S RULE: an unresolved line is never
# mixed into a colour group it might not belong to, and never dropped.

MONO_COLOR_ORDER = ("W", "U", "B", "R", "G")

GROUP_MULTICOLOUR = 0
GROUP_COLORLESS = 6
GROUP_LAND = 7
GROUP_UNCLASSIFIED = 8
GROUP_UNKNOWN = 9

_MONO_GROUPS = {
    letter: index for index, letter in enumerate(MONO_COLOR_ORDER, start=1)
}


def color_sort_group(color: str | None, type_line: str | None) -> int:
    """Which colour group one card sorts into. Lower sorts earlier.

    Takes the same two stored values as `color_marker` and goes through it,
    so the two can never drift apart.
    """
    marker = color_marker(color, type_line)
    if not marker:
        return GROUP_UNKNOWN
    if marker == LAND_MARKER:
        return GROUP_LAND
    if marker == COLORLESS_MARKER:
        return GROUP_COLORLESS
    if len(marker) > 1:
        return GROUP_MULTICOLOUR
    return _MONO_GROUPS.get(marker, GROUP_UNCLASSIFIED)
