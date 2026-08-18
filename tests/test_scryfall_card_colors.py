from legacy_import_service import scryfall_card_colors, wubrg_color_string


def test_normal_card_reads_top_level_colors():
    assert scryfall_card_colors({"colors": ["U", "W"]}) == ["U", "W"]


def test_colorless_normal_card_reads_empty_top_level_colors():
    assert scryfall_card_colors({"colors": []}) == []


def test_transform_card_falls_back_to_front_face():
    """Aang, Swift Savior // Aang and La, Ocean's Fury: mana_cost/colors are
    null at the top level for transform cards -- only the front face
    ({1}{W}{U}, colors ['U','W']) carries them."""
    card = {
        "layout": "transform",
        "colors": None,
        "card_faces": [
            {"name": "Aang, Swift Savior", "mana_cost": "{1}{W}{U}", "colors": ["U", "W"]},
            {"name": "Aang and La, Ocean's Fury", "mana_cost": "", "colors": []},
        ],
    }
    assert scryfall_card_colors(card) == ["U", "W"]


def test_modal_dfc_falls_back_to_front_face():
    card = {
        "layout": "modal_dfc",
        "colors": None,
        "card_faces": [
            {"name": "Agadeem's Awakening", "mana_cost": "{X}{B}{B}{B}", "colors": ["B"]},
            {"name": "Agadeem, the Undercrypt", "mana_cost": "", "colors": []},
        ],
    }
    assert scryfall_card_colors(card) == ["B"]


def test_split_card_uses_top_level_colors_not_faces():
    """Split cards (e.g. Fire // Ice) DO populate top-level colors as the
    union of both halves -- no fallback needed, and the fallback must not
    kick in and produce a different (front-face-only) result."""
    card = {
        "layout": "split",
        "colors": ["R", "U"],
        "card_faces": [
            {"name": "Fire", "mana_cost": "{1}{R}", "colors": None},
            {"name": "Ice", "mana_cost": "{1}{U}", "colors": None},
        ],
    }
    assert scryfall_card_colors(card) == ["R", "U"]


def test_card_with_no_faces_and_no_colors_key_is_colorless():
    assert scryfall_card_colors({}) == []


def test_wubrg_string_reorders_scryfalls_alphabetical_arrays():
    """Scryfall returns colors alphabetically (B,G,R,U,W), not WUBRG order --
    confirmed against real API responses: Orzhov Signet's colors is
    ['B','W'] (alphabetical), which should display as "WB", not "BW"."""
    assert wubrg_color_string(["B", "W"]) == "WB"
    assert wubrg_color_string(["R", "W"]) == "WR"
    assert wubrg_color_string(["B", "U"]) == "UB"
    assert wubrg_color_string(["B", "G", "R", "U", "W"]) == "WUBRG"


def test_wubrg_string_handles_single_and_no_colors():
    assert wubrg_color_string(["G"]) == "G"
    assert wubrg_color_string([]) == ""
