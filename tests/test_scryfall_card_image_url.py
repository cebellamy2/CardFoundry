from legacy_import_service import scryfall_card_image_url


def test_normal_card_reads_top_level_image_uris():
    card = {"name": "Lightning Bolt", "image_uris": {"normal": "https://example.com/normal.jpg", "small": "https://example.com/small.jpg"}}
    assert scryfall_card_image_url(card) == "https://example.com/normal.jpg"


def test_size_parameter_selects_a_different_resolution():
    card = {"image_uris": {"normal": "https://example.com/normal.jpg", "small": "https://example.com/small.jpg"}}
    assert scryfall_card_image_url(card, size="small") == "https://example.com/small.jpg"


def test_transform_card_falls_back_to_front_face():
    """Same shape class as scryfall_card_colors (v1.39.2) and
    scryfall_card_flavor_name -- image_uris is absent at the top level
    for double-faced/transform/modal cards, only present per face."""
    card = {
        "layout": "transform",
        "card_faces": [
            {"name": "Front Face", "image_uris": {"normal": "https://example.com/front.jpg"}},
            {"name": "Back Face", "image_uris": {"normal": "https://example.com/back.jpg"}},
        ],
    }
    assert scryfall_card_image_url(card) == "https://example.com/front.jpg"


def test_transform_card_front_face_with_no_image_uris_is_none():
    card = {
        "layout": "transform",
        "card_faces": [{"name": "Front Face"}, {"name": "Back Face", "image_uris": {"normal": "https://example.com/back.jpg"}}],
    }
    assert scryfall_card_image_url(card) is None


def test_card_with_no_faces_and_no_image_uris_key_is_none():
    assert scryfall_card_image_url({}) is None


def test_top_level_image_uris_takes_priority_over_faces():
    card = {
        "image_uris": {"normal": "https://example.com/top.jpg"},
        "card_faces": [{"name": "Front", "image_uris": {"normal": "https://example.com/face.jpg"}}],
    }
    assert scryfall_card_image_url(card) == "https://example.com/top.jpg"


def test_missing_requested_size_returns_none():
    card = {"image_uris": {"small": "https://example.com/small.jpg"}}
    assert scryfall_card_image_url(card, size="normal") is None
