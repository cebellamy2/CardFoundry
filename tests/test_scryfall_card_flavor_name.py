from legacy_import_service import scryfall_card_flavor_name


def test_card_with_flavor_name_reads_top_level_field():
    """Roaming Throne // Doom Variant (MAR 099) -- a real Universes Beyond
    crossover printing."""
    assert scryfall_card_flavor_name({"name": "Roaming Throne", "flavor_name": "Doom Variant"}) == "Doom Variant"


def test_normal_card_has_no_flavor_name_key_at_all():
    """The key is ABSENT on a normal card, not null and not "" -- card.get()
    already returns None for a missing key."""
    assert scryfall_card_flavor_name({"name": "Lightning Bolt"}) is None


def test_flavor_name_key_present_but_empty_string_is_treated_as_absent():
    assert scryfall_card_flavor_name({"name": "Lightning Bolt", "flavor_name": ""}) is None


def test_transform_card_falls_back_to_front_face():
    """Same shape class that left top-level `colors` null on transform
    layouts (v1.39.2) -- untested against any real inventory example, but
    handled defensively rather than waiting to find out live."""
    card = {
        "layout": "transform",
        "card_faces": [
            {"name": "Front Face", "flavor_name": "Front Alt Name"},
            {"name": "Back Face", "flavor_name": "Back Alt Name"},
        ],
    }
    assert scryfall_card_flavor_name(card) == "Front Alt Name"


def test_transform_card_front_face_with_no_flavor_name_is_none():
    card = {
        "layout": "transform",
        "card_faces": [
            {"name": "Front Face"},
            {"name": "Back Face", "flavor_name": "Back Alt Name"},
        ],
    }
    assert scryfall_card_flavor_name(card) is None


def test_card_with_no_faces_and_no_flavor_name_key_is_none():
    assert scryfall_card_flavor_name({}) is None


def test_top_level_flavor_name_takes_priority_over_faces():
    card = {
        "flavor_name": "Top Level Name",
        "card_faces": [{"name": "Front", "flavor_name": "Face Name"}],
    }
    assert scryfall_card_flavor_name(card) == "Top Level Name"
