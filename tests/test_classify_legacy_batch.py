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


def test_double_faced_battle_land_check_uses_top_level_type_line():
    """Invasion of Ixalan // Belligerent Regisaur: top-level type_line IS
    populated for double-faced cards ("Battle — Siege // Creature —
    Dinosaur"), so the land check is unaffected -- only colors needed the
    fallback."""
    card = {
        "type_line": "Battle — Siege // Creature — Dinosaur",
        "colors": None,
        "card_faces": [
            {"name": "Invasion of Ixalan", "colors": ["G"]},
            {"name": "Belligerent Regisaur", "colors": ["G"]},
        ],
    }
    assert classify_legacy_batch({"finish": "foil"}, card) == "leg_foil_g"
