from legacy_import_service import classify_legacy_batch


def test_colorless_card_normal_finish():
    card = {"type_line": "Artifact", "colors": []}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_c"


def test_single_color_card():
    card = {"type_line": "Instant", "colors": ["R"]}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_red"


def test_multicolor_card():
    card = {"type_line": "Creature", "colors": ["W", "U"]}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_multi"


def test_land_goes_to_land_category_regardless_of_colors_produced():
    card = {"type_line": "Basic Land — Forest", "colors": []}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_land"


def test_non_normal_finish_uses_the_foil_prefix():
    card = {"type_line": "Instant", "colors": ["R"]}
    assert classify_legacy_batch({"finish": "foil"}, card) == "leg_foil_red"


def test_double_faced_card_uses_front_face_color_not_colorless():
    """Aang, Swift Savior // Aang and La, Ocean's Fury: colors is null at
    the top level (transform layout), only the front face carries it.
    Before the scryfall_card_colors() fix this fell through to leg_c."""
    card = {
        "type_line": "Legendary Creature — Avatar // Legendary Creature — Avatar Spirit",
        "colors": None,
        "card_faces": [
            {"name": "Aang, Swift Savior", "colors": ["U", "W"]},
            {"name": "Aang and La, Ocean's Fury", "colors": []},
        ],
    }
    assert classify_legacy_batch({"finish": "foil"}, card) == "leg_foil_multi"


def test_double_faced_battle_with_no_land_face_is_categorised_by_colour():
    """Invasion of Ixalan // Belligerent Regisaur: top-level type_line IS
    populated for double-faced cards ("Battle — Siege // Creature —
    Dinosaur").

    ★ DOCSTRING CORRECTED 2026-10-09. It used to claim the land check was
    "unaffected -- only colors needed the fallback". That was true of THIS
    card, which has no land face, and false in general: the joined type
    line is exactly what sent a spell-front/land-back modal card to the
    land bin. See the tests below."""
    card = {
        "type_line": "Battle — Siege // Creature — Dinosaur",
        "colors": None,
        "card_faces": [
            {"name": "Invasion of Ixalan", "colors": ["G"]},
            {"name": "Belligerent Regisaur", "colors": ["G"]},
        ],
    }
    assert classify_legacy_batch({"finish": "foil"}, card) == "leg_foil_g"


# --- the front face decides land vs non-land (2026-10-09) ----------------
#
# ★ THE BUG. classify_legacy_batch tested `"Land" in type_line` against
# Scryfall's JOINED type line, so a modal double-faced card with a spell
# front and a land back ("Sorcery // Land") went to the land bin -- the
# same shape as the 65-card reshelving incident, and the same shape as the
# colours bug in scryfall_card_colors, which was fixed in v1.39.2/v1.39.4
# while this half was left behind. card_color_marker.is_land already held
# the correct rule and its own docstring had named this call site as the
# remaining latent bug ever since.


def test_a_spell_front_land_back_modal_card_is_not_a_land():
    """★ THE REGRESSION. Under the old substring test this was leg_land."""
    card = {
        "type_line": "Sorcery // Land",
        "colors": None,
        "card_faces": [
            {"name": "Bala Ged Recovery", "colors": ["G"]},
            {"name": "Bala Ged Sanctuary", "colors": []},
        ],
    }
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_g"
    assert classify_legacy_batch({"finish": "foil"}, card) == "leg_foil_g"


def test_a_land_front_modal_card_is_still_a_land():
    """The other direction must not regress: what the physical card shows
    is a land, so it belongs in the land bin."""
    card = {
        "type_line": "Land // Creature — Elf Druid",
        "colors": None,
        "card_faces": [
            {"name": "Front", "colors": []},
            {"name": "Back", "colors": ["G"]},
        ],
    }
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_land"


def test_an_ordinary_land_is_unchanged():
    card = {"type_line": "Basic Land — Forest", "colors": []}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_land"
    assert classify_legacy_batch({"finish": "etched"}, card) == "leg_foil_land"


def test_an_ordinary_spell_is_unchanged():
    card = {"type_line": "Creature — Elf Druid", "colors": ["G"]}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_g"


def test_a_single_faced_card_with_no_card_faces_is_unchanged():
    """No separator, no card_faces: the front face IS the whole type line,
    and the colours fallback has nothing to fall back to."""
    for type_line, colors, expected in (
        ("Artifact", [], "leg_c"),
        ("Land", [], "leg_land"),
        ("Instant", ["U"], "leg_u"),
    ):
        card = {"type_line": type_line, "colors": colors}
        assert classify_legacy_batch({"finish": "normal"}, card) == expected, type_line


def test_a_land_in_the_SUBTYPE_block_is_not_a_land_type():
    """is_land reads the types block, left of the em dash, so a subtype
    that merely contains the word does not make the card a land. A plain
    substring test could not tell these apart."""
    card = {"type_line": "Enchantment — Land Aura", "colors": ["W"]}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_w"


def test_a_missing_type_line_is_not_treated_as_a_land():
    card = {"type_line": None, "colors": ["R"]}
    assert classify_legacy_batch({"finish": "normal"}, card) == "leg_red"


def test_classification_shares_one_rule_with_the_packing_slip_marker():
    """A card that prints (L) on a packing slip and a card that lands in a
    leg_land bin must be the same card -- so both ask card_color_marker."""
    from card_color_marker import is_land

    for type_line in ("Sorcery // Land", "Land // Creature — Elf",
                      "Basic Land — Forest", "Creature — Elf Druid",
                      "Enchantment — Land Aura", None):
        card = {"type_line": type_line, "colors": ["G"]}
        landed = classify_legacy_batch({"finish": "normal"}, card) == "leg_land"
        assert landed == is_land(type_line), type_line
