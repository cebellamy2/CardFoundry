"""The on-screen colour badge, from the SAME rule the packing slip prints.

WHY THIS MODULE EXISTS. The badge used to be `main.py`'s
`_color_badge(color)`, which took only the colour. `color` stores '' for a
colourless card AND for a land, so every land on screen showed a
confident, wrong **C** -- measured on production 2026-10-30: 674 of the
11,336 available cards are lands, every one of them with `color = ''`, and
all 674 were rendering C. The packing slip has printed (L) correctly since
v2.68.0 because it goes through `card_color_marker.color_marker`, which
also takes the type line.

So the fix is not a second land rule, it is giving the screen the rule the
slip already has. `card_color_marker` stays the single owner of "what
letters does this card get"; this module owns only "how do those letters
look in HTML". That split is what makes it structurally impossible for the
slip and the screen to disagree: they share the input and the decision,
and differ only in presentation.

It cannot live in `card_color_marker` itself, which is deliberately
dependency-free and imported by the PDF renderer -- HTML has no business
there. It cannot live in `main.py` either, because the tests that pin
"slip marker == screen badge" would then have to import the whole web app
to check a string.

★ A LAND IS (L) WHATEVER ITS COLOUR, including Dryad Arbor, and a
reversible double-faced land goes by its FRONT face -- both rules belong
to `card_color_marker`, not here. The only thing this file knows about
lands is that "L" needs a pip colour.

★ NOTHING IS LOOKED UP HERE. The caller passes the two values already on
the row it is rendering (`InventoryCard.type_line` / `OrderItem.type_line`
-- both stored columns since v2.68.0), so a list page costs no extra
query. Measured on production 2026-10-30: 0 of 11,336 available cards and
0 of 12,939 order items have a NULL type_line, so the column is there to
be read.
"""

from card_color_marker import COLORLESS_MARKER, LAND_MARKER, color_marker

# Display names for the title attribute, so a pip is readable to a screen
# reader and on hover. L and C are spelled out for the same reason.
PIP_TITLES = {
    "W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green",
    COLORLESS_MARKER: "Colorless",
    LAND_MARKER: "Land",
}


def _pip(letter: str) -> str:
    return (
        f'<span class="color-pip color-pip-{letter.lower()}" '
        f'title="{PIP_TITLES.get(letter, letter)}">{letter}</span>'
    )


def color_badge_html(color: str | None, type_line: str | None = None) -> str:
    """The badge for one card, as HTML, or "" for no badge at all.

    `type_line` defaults to None so a caller that genuinely has no type
    line still renders the colour it does know -- but note that None also
    means `color_marker` will not emit C for a colourless card, because
    "colourless" and "not looked up yet" are different states and only the
    type line tells them apart. That is the slip's rule, unchanged.

    A multi-letter marker ("WU") becomes one pip per letter, exactly as
    before. "L" and "C" are single pips.
    """
    marker = color_marker(color, type_line)
    if not marker:
        return ""
    return "".join(_pip(letter) for letter in marker)
