from card_color_marker import color_marker
from legacy_import_service import scryfall_card_colors, scryfall_card_type_line


# The real Scryfall response shape for Steam Vents in Lorwyn Eclipsed
# (ECL 348), fetched 2026-10-26. `layout` is "reversible_card", and the
# top-level `type_line` and `colors` keys are ABSENT ENTIRELY -- not null,
# not empty -- which is what made this fail silently: card.get("type_line")
# returns None, and every writer then declined to store it.
REVERSIBLE_STEAM_VENTS = {
    "id": "eb96c335-9ed3-4f7d-b07a-185ff4044976",
    "name": "Steam Vents // Steam Vents",
    "layout": "reversible_card",
    "set": "ecl",
    "collector_number": "348",
    "card_faces": [
        {"name": "Steam Vents", "type_line": "Land — Island Mountain", "colors": []},
        {"name": "Steam Vents", "type_line": "Land — Island Mountain", "colors": []},
    ],
}

# Same layout, but the two faces are genuinely different cards and the
# front face is a creature, not a land (TDM 379). Its colour DID resolve
# before this fix, through scryfall_card_colors' front-face fallback, so
# it printed (B) and looked fine -- it was only ever one reprint away from
# being the loud version of the same bug.
REVERSIBLE_SCAVENGER_REGENT = {
    "id": "9cf54062-7b5b-4e46-ae1e-fab7e419a9fa",
    "name": "Scavenger Regent // Exude Toxin // Scavenger Regent",
    "layout": "reversible_card",
    "card_faces": [
        {"name": "Scavenger Regent", "type_line": "Creature — Dragon", "colors": ["B"]},
        {"name": "Exude Toxin", "type_line": "Sorcery — Omen", "colors": ["B"]},
    ],
}


def test_normal_card_reads_top_level_type_line():
    assert scryfall_card_type_line({"type_line": "Instant"}) == "Instant"


def test_modal_dfc_keeps_its_joined_top_level_line():
    """A modal DFC DOES carry a joined top-level type line, so no fallback
    should fire -- card_color_marker splits " // " itself and reads the
    front face. Confirmed against the live API for Agadeem's Awakening."""
    card = {
        "layout": "modal_dfc",
        "type_line": "Sorcery // Land",
        "card_faces": [
            {"name": "Agadeem's Awakening", "type_line": "Sorcery"},
            {"name": "Agadeem, the Undercrypt", "type_line": "Land"},
        ],
    }
    assert scryfall_card_type_line(card) == "Sorcery // Land"


def test_reversible_card_falls_back_to_the_front_face():
    assert scryfall_card_type_line(REVERSIBLE_STEAM_VENTS) == "Land — Island Mountain"


def test_reversible_card_with_differing_faces_takes_the_front_one():
    assert scryfall_card_type_line(REVERSIBLE_SCAVENGER_REGENT) == "Creature — Dragon"


def test_card_with_no_faces_and_no_type_line_is_unknown():
    """None, not "" -- "unknown" has to stay distinguishable from
    "resolved as something empty", because card_color_marker prints no
    marker for the first and (C) for a known colourless card."""
    assert scryfall_card_type_line({}) is None


def test_empty_top_level_line_still_falls_back():
    card = {"type_line": "", "card_faces": [{"type_line": "Land"}]}
    assert scryfall_card_type_line(card) == "Land"


def test_the_marker_this_exists_for_now_comes_out_as_land():
    """The whole point, end to end: a reversible double-sided land earns
    (L). Before the fallback its type line was None, so color_marker saw
    colour '' and type unknown and printed NOTHING -- which was honest,
    and useless, on a black-and-white slip."""
    colors = scryfall_card_colors(REVERSIBLE_STEAM_VENTS)
    type_line = scryfall_card_type_line(REVERSIBLE_STEAM_VENTS)
    assert colors == []
    assert color_marker("", type_line) == "L"
    # And the regression this replaces:
    assert color_marker("", None) == ""


def test_reversible_creature_front_still_marks_by_colour():
    type_line = scryfall_card_type_line(REVERSIBLE_SCAVENGER_REGENT)
    assert color_marker("B", type_line) == "B"


# --- the ingest-time writer, the other half of the fix --------------------

def test_order_ingest_stores_the_reversible_type_line():
    """`_enrichment_by_scryfall_id` is where a NEW order gets its marker.

    Fixing only the hourly backfill would have left every future
    reversible purchase printing blank until the next tick, so both
    writers go through the same helper. Driven with the real Mana Pool
    order-detail shape rather than calling the helper directly.
    """
    import order_service

    detail = {
        "items": [
            {"product": {"single": {"scryfall_id": REVERSIBLE_STEAM_VENTS["id"]}}},
        ],
    }
    enrichment = order_service._enrichment_by_scryfall_id(
        detail, lambda ids: {REVERSIBLE_STEAM_VENTS["id"]: REVERSIBLE_STEAM_VENTS},
    )

    values = enrichment[REVERSIBLE_STEAM_VENTS["id"]]
    assert values["type_line"] == "Land — Island Mountain"
    assert values["color"] == ""          # a land is colourless, correctly
    assert color_marker(values["color"], values["type_line"]) == "L"


def test_order_ingest_still_degrades_to_no_marker_on_a_lookup_failure():
    """The best-effort contract is unchanged: a Scryfall outage must leave
    the colour and type line unset rather than fail the sync, and an
    unknown type must print NOTHING rather than a confident (C)."""
    import order_service

    def exploding_lookup(ids):
        raise RuntimeError("scryfall is down")

    detail = {"items": [{"product": {"single": {"scryfall_id": "sf-anything"}}}]}
    assert order_service._enrichment_by_scryfall_id(detail, exploding_lookup) == {}
    assert color_marker(None, None) == ""
