from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from decklist_search_service import (
    DECKLIST_STATUS_SCOPES,
    DEFAULT_DECKLIST_STATUS_SCOPE,
    matching_available_cards_in_batch,
    parse_decklist,
    parse_decklist_line,
    search_decklist_inventory,
)
from models import Base, Batch, InventoryCard


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'decklist_search.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_batch(session, code="B1", **overrides):
    values = {"batch_code": code}
    values.update(overrides)
    batch = Batch(**values)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, **overrides):
    values = {"batch_id": batch.id, "name": "Lightning Bolt", "status": "available"}
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


# --- parse_decklist_line ---------------------------------------------------

def test_parses_baseline_quantity_and_name():
    result = parse_decklist_line("4 Lightning Bolt")
    assert result == {
        "raw_line": "4 Lightning Bolt", "quantity": 4, "name": "Lightning Bolt",
        "set_code": None, "collector_number": None,
    }


def test_parses_x_suffix_on_quantity():
    result = parse_decklist_line("4x Lightning Bolt")
    assert result["quantity"] == 4
    assert result["name"] == "Lightning Bolt"


def test_parses_trailing_set_and_collector_number():
    result = parse_decklist_line("1 Sol Ring (LEA) 233")
    assert result == {
        "raw_line": "1 Sol Ring (LEA) 233", "quantity": 1, "name": "Sol Ring",
        "set_code": "LEA", "collector_number": "233",
    }


def test_set_code_is_uppercased_and_collector_number_preserved_case():
    result = parse_decklist_line("1 Sol Ring (lea) 233a")
    assert result["set_code"] == "LEA"
    assert result["collector_number"] == "233A"


def test_multiword_card_name_with_punctuation():
    result = parse_decklist_line("2 Jace, the Mind Sculptor")
    assert result["name"] == "Jace, the Mind Sculptor"
    assert result["quantity"] == 2


def test_blank_line_returns_none():
    assert parse_decklist_line("") is None
    assert parse_decklist_line("   ") is None


def test_line_with_no_quantity_returns_none():
    assert parse_decklist_line("Lightning Bolt") is None


def test_zero_quantity_returns_none():
    assert parse_decklist_line("0 Lightning Bolt") is None


def test_quantity_with_no_name_returns_none():
    assert parse_decklist_line("4") is None


# --- parse_decklist ----------------------------------------------------------

def test_parse_decklist_separates_parsed_and_unparsed():
    text = "4 Lightning Bolt\nnot a real line\n1 Sol Ring (LEA) 233\n\n"
    parsed, unparsed = parse_decklist(text)
    assert [p["name"] for p in parsed] == ["Lightning Bolt", "Sol Ring"]
    assert len(unparsed) == 1
    assert unparsed[0]["raw_line"] == "not a real line"
    assert "Could not parse" in unparsed[0]["reason"]


def test_parse_decklist_skips_blank_lines_silently():
    parsed, unparsed = parse_decklist("4 Lightning Bolt\n\n\n1 Sol Ring (LEA) 233")
    assert len(parsed) == 2
    assert unparsed == []


def test_parse_decklist_one_bad_line_does_not_block_the_rest():
    text = "\n".join([f"{n} Card {n}" for n in range(1, 4)] + ["garbage line here"])
    parsed, unparsed = parse_decklist(text)
    assert len(parsed) == 3
    assert len(unparsed) == 1


# --- search_decklist_inventory: name-only matching --------------------------

def test_name_only_match_counts_available_copies_across_batches(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt")
    add_card(session, b1, name="Lightning Bolt")
    add_card(session, b2, name="Lightning Bolt")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "2 Lightning Bolt", "quantity": 2, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )

    assert not_found == []
    assert len(found) == 1
    assert found[0]["on_hand"] == 3
    assert found[0]["requested_quantity"] == 2
    assert found[0]["fillable"] is True
    assert found[0]["match_mode"] == "name_only"


def test_name_only_match_is_case_insensitive(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 lightning bolt", "quantity": 1, "name": "lightning bolt",
                    "set_code": None, "collector_number": None}],
    )
    assert not_found == []
    assert found[0]["on_hand"] == 1


def test_name_only_match_ignores_unavailable_status(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt", status="available")
    add_card(session, b1, name="Lightning Bolt", status="sold")
    add_card(session, b1, name="Lightning Bolt", status="reserved")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )
    assert found[0]["on_hand"] == 1


def test_shortfall_still_reported_as_found_not_missing(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "4 Lightning Bolt", "quantity": 4, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )
    assert not_found == []
    assert found[0]["on_hand"] == 1
    assert found[0]["fillable"] is False


def test_no_matches_goes_to_not_found_not_a_zero_row(session):
    add_batch(session)

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Black Lotus", "quantity": 1, "name": "Black Lotus",
                    "set_code": None, "collector_number": None}],
    )
    assert found == []
    assert len(not_found) == 1
    assert not_found[0]["raw_line"] == "1 Black Lotus"


def test_double_faced_card_matches_by_front_face_name(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Fable of the Mirror-Breaker // Reflection of Kiki-Jiki")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Fable of the Mirror-Breaker", "quantity": 1,
                    "name": "Fable of the Mirror-Breaker",
                    "set_code": None, "collector_number": None}],
    )
    assert not_found == []
    assert found[0]["on_hand"] == 1
    assert found[0]["matched_name"] == "Fable of the Mirror-Breaker // Reflection of Kiki-Jiki"


def test_front_face_match_does_not_loosely_match_unrelated_names(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Fable of the Mirror-Breakerish Other Card")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Fable of the Mirror-Breaker", "quantity": 1,
                    "name": "Fable of the Mirror-Breaker",
                    "set_code": None, "collector_number": None}],
    )
    assert found == []
    assert len(not_found) == 1


# --- search_decklist_inventory: exact-printing matching ----------------------

def test_exact_printing_match_ignores_name_and_uses_set_plus_collector(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Sol Ring", set_code="LEA", collector_number="233")
    add_card(session, b1, name="Sol Ring", set_code="7ED", collector_number="288")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Sol Ring (LEA) 233", "quantity": 1, "name": "Sol Ring",
                    "set_code": "LEA", "collector_number": "233"}],
    )
    assert not_found == []
    assert found[0]["on_hand"] == 1
    assert found[0]["match_mode"] == "exact_printing"


def test_exact_printing_match_is_case_insensitive_on_set_and_collector(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Sol Ring", set_code="lea", collector_number="233a")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Sol Ring (LEA) 233A", "quantity": 1, "name": "Sol Ring",
                    "set_code": "LEA", "collector_number": "233A"}],
    )
    assert not_found == []
    assert found[0]["on_hand"] == 1


def test_exact_printing_no_match_reports_set_and_collector_in_reason(session):
    add_batch(session)

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Sol Ring (LEA) 233", "quantity": 1, "name": "Sol Ring",
                    "set_code": "LEA", "collector_number": "233"}],
    )
    assert found == []
    assert "LEA #233" in not_found[0]["reason"]


def test_multiple_lines_processed_independently(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt")

    lines = [
        {"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
         "set_code": None, "collector_number": None},
        {"raw_line": "1 Black Lotus", "quantity": 1, "name": "Black Lotus",
         "set_code": None, "collector_number": None},
    ]
    found, not_found = search_decklist_inventory(session, lines)
    assert len(found) == 1
    assert found[0]["name"] == "Lightning Bolt"
    assert len(not_found) == 1
    assert not_found[0]["raw_line"] == "1 Black Lotus"


# --- first-batch-by-finish -----------------------------------------------

def test_result_includes_first_nonfoil_and_foil_batch(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt", finish_id="NF",
             imported_at=datetime(2026, 1, 1))
    add_card(session, b2, name="Lightning Bolt", finish_id="FO",
             imported_at=datetime(2026, 1, 2))

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )

    assert not_found == []
    assert found[0]["nonfoil_batch"] == {"id": b1.id, "batch_code": "B1"}
    assert found[0]["foil_batch"] == {"id": b2.id, "batch_code": "B2"}


def test_missing_finish_is_none_not_an_error(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt", finish_id="NF")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )

    assert found[0]["nonfoil_batch"] is not None
    assert found[0]["foil_batch"] is None


def test_etched_finish_groups_as_nonfoil(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt", finish_id="EF")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )

    assert found[0]["nonfoil_batch"] == {"id": b1.id, "batch_code": "B1"}
    assert found[0]["foil_batch"] is None
    # Still counted in the unchanged aggregate on-hand total.
    assert found[0]["on_hand"] == 1


def test_first_batch_uses_card_imported_at_not_batch_created_at(session):
    """Matches the real picking precedent (order_service.allocate_order
    orders by InventoryCard.imported_at) -- a batch created earlier can
    still receive a card later (e.g. via /inventory/add), so batch
    creation date alone would misreport where the oldest stock actually
    is."""
    old_batch = add_batch(session, "OLD", created_at=datetime(2020, 1, 1))
    new_batch = add_batch(session, "NEW", created_at=datetime(2026, 1, 1))
    # The "old" batch's copy was actually added far more recently than
    # the "new" batch's copy.
    add_card(session, old_batch, name="Lightning Bolt", finish_id="NF",
             imported_at=datetime(2026, 8, 1))
    add_card(session, new_batch, name="Lightning Bolt", finish_id="NF",
             imported_at=datetime(2026, 1, 5))

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )

    assert found[0]["nonfoil_batch"] == {"id": new_batch.id, "batch_code": "NEW"}


def test_first_batch_among_several_picks_the_oldest_imported_card(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    b3 = add_batch(session, "B3")
    add_card(session, b2, name="Lightning Bolt", finish_id="NF",
             imported_at=datetime(2026, 3, 1))
    add_card(session, b1, name="Lightning Bolt", finish_id="NF",
             imported_at=datetime(2026, 1, 1))
    add_card(session, b3, name="Lightning Bolt", finish_id="NF",
             imported_at=datetime(2026, 6, 1))

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "3 Lightning Bolt", "quantity": 3, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )

    assert found[0]["nonfoil_batch"] == {"id": b1.id, "batch_code": "B1"}
    assert found[0]["on_hand"] == 3


def test_first_batch_only_considers_available_copies(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt", finish_id="NF", status="sold",
             imported_at=datetime(2026, 1, 1))
    add_card(session, b2, name="Lightning Bolt", finish_id="NF", status="available",
             imported_at=datetime(2026, 6, 1))

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )

    assert found[0]["nonfoil_batch"] == {"id": b2.id, "batch_code": "B2"}


# --- matching_available_cards_in_batch --------------------------------------

def test_matching_cards_scoped_to_batch_and_finish_oldest_first(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    older = add_card(session, b1, name="Lightning Bolt", finish_id="NF",
                      imported_at=datetime(2026, 1, 1))
    newer = add_card(session, b1, name="Lightning Bolt", finish_id="NF",
                      imported_at=datetime(2026, 6, 1))
    add_card(session, b1, name="Lightning Bolt", finish_id="FO",
             imported_at=datetime(2026, 1, 1))
    add_card(session, b2, name="Lightning Bolt", finish_id="NF",
             imported_at=datetime(2025, 1, 1))

    matches = matching_available_cards_in_batch(
        session, "Lightning Bolt", None, None, b1.id, foil=False,
    )

    assert [card.id for card in matches] == [older.id, newer.id]


def test_matching_cards_excludes_unavailable_and_other_batches(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt", finish_id="NF", status="sold")
    add_card(session, b2, name="Lightning Bolt", finish_id="NF", status="available")

    matches = matching_available_cards_in_batch(
        session, "Lightning Bolt", None, None, b1.id, foil=False,
    )

    assert matches == []


def test_matching_cards_by_exact_printing(session):
    b1 = add_batch(session, "B1")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161",
             finish_id="NF")
    add_card(session, b1, name="Lightning Bolt", set_code="M10", collector_number="146",
             finish_id="NF")

    matches = matching_available_cards_in_batch(
        session, "Lightning Bolt", "LEA", "161", b1.id, foil=False,
    )

    assert len(matches) == 1
    assert matches[0].set_code == "LEA"


def test_matching_cards_no_results_returns_empty_list(session):
    b1 = add_batch(session, "B1")
    matches = matching_available_cards_in_batch(
        session, "Nonexistent Card", None, None, b1.id, foil=False,
    )
    assert matches == []


# --- status_scope toggle (Phase 9) ------------------------------------------

def test_default_scope_is_available_only():
    assert DEFAULT_DECKLIST_STATUS_SCOPE == "available"
    assert DECKLIST_STATUS_SCOPES["available"] == ("available",)


def test_extended_scope_covers_reserved_and_unsellable_but_not_sold_or_removed():
    extended = set(DECKLIST_STATUS_SCOPES["extended"])
    assert extended == {"available", "reserved", "unsellable"}


def test_search_decklist_inventory_defaults_to_available_only(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt", status="reserved")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )
    assert found == []
    assert len(not_found) == 1


def test_search_decklist_inventory_extended_scope_surfaces_reserved_and_unsellable(session):
    b1 = add_batch(session)
    add_card(session, b1, name="Lightning Bolt", status="reserved")
    add_card(session, b1, name="Lightning Bolt", status="unsellable")
    add_card(session, b1, name="Lightning Bolt", status="sold")
    add_card(session, b1, name="Lightning Bolt", status="removed")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "2 Lightning Bolt", "quantity": 2, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
        DECKLIST_STATUS_SCOPES["extended"],
    )
    assert not_found == []
    # sold/removed stay excluded even at the widest scope.
    assert found[0]["on_hand"] == 2


def test_matching_cards_in_batch_defaults_to_available_only(session):
    b1 = add_batch(session, "B1")
    add_card(session, b1, name="Lightning Bolt", finish_id="NF", status="reserved")

    matches = matching_available_cards_in_batch(
        session, "Lightning Bolt", None, None, b1.id, foil=False,
    )
    assert matches == []


def test_matching_cards_in_batch_extended_scope_includes_reserved(session):
    b1 = add_batch(session, "B1")
    card = add_card(session, b1, name="Lightning Bolt", finish_id="NF", status="reserved")

    matches = matching_available_cards_in_batch(
        session, "Lightning Bolt", None, None, b1.id, foil=False,
        statuses=DECKLIST_STATUS_SCOPES["extended"],
    )
    assert [m.id for m in matches] == [card.id]


# --- printings breakdown (Phase 10: flag-and-nest display) ------------------

def test_single_printing_line_has_one_printings_entry_not_flagged(session):
    b1 = add_batch(session, "B1")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )
    assert not_found == []
    assert len(found[0]["printings"]) == 1
    printing = found[0]["printings"][0]
    assert printing["set_code"] == "LEA"
    assert printing["collector_number"] == "161"
    assert printing["on_hand"] == 1
    assert printing["is_exact_match"] is False  # nothing specific was requested


def test_name_only_line_spanning_multiple_printings_breaks_them_down(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161")
    add_card(session, b2, name="Lightning Bolt", set_code="M10", collector_number="146")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "3 Lightning Bolt", "quantity": 3, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )
    assert not_found == []
    printings = found[0]["printings"]
    assert len(printings) == 2
    by_set = {p["set_code"]: p["on_hand"] for p in printings}
    assert by_set == {"LEA": 2, "M10": 1}
    assert all(p["is_exact_match"] is False for p in printings)
    # Total across printings must equal the line's own on_hand aggregate.
    assert sum(p["on_hand"] for p in printings) == found[0]["on_hand"]


def test_exact_printing_line_flags_requested_printing_and_still_lists_others(session):
    """The core Phase 10 requirement: an exact-printing line's search stays
    scoped to that printing for on_hand/fillable (unchanged matching logic),
    but the printings breakdown now ALSO surfaces other printings of the
    same name that the exact-printing query alone would never have
    fetched."""
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161")
    add_card(session, b2, name="Lightning Bolt", set_code="M10", collector_number="146")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt (LEA) 161", "quantity": 1,
                    "name": "Lightning Bolt", "set_code": "LEA", "collector_number": "161"}],
    )
    assert not_found == []
    row = found[0]
    # on_hand/fillable stay scoped to the exact printing -- unchanged.
    assert row["on_hand"] == 1
    assert row["fillable"] is True

    printings = row["printings"]
    assert len(printings) == 2
    exact = next(p for p in printings if p["is_exact_match"])
    other = next(p for p in printings if not p["is_exact_match"])
    assert exact["set_code"] == "LEA" and exact["collector_number"] == "161"
    assert other["set_code"] == "M10" and other["collector_number"] == "146"
    # Exact match sorts first.
    assert printings[0]["is_exact_match"] is True


def test_exact_printing_line_with_no_other_printings_has_single_entry(session):
    b1 = add_batch(session, "B1")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt (LEA) 161", "quantity": 1,
                    "name": "Lightning Bolt", "set_code": "LEA", "collector_number": "161"}],
    )
    assert not_found == []
    printings = found[0]["printings"]
    assert len(printings) == 1
    assert printings[0]["is_exact_match"] is True


def test_printings_breakdown_reports_per_printing_batch_by_finish(session):
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161",
             finish_id="NF")
    add_card(session, b2, name="Lightning Bolt", set_code="LEA", collector_number="161",
             finish_id="FO")
    add_card(session, b1, name="Lightning Bolt", set_code="M10", collector_number="146",
             finish_id="NF")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )
    printings = {p["set_code"]: p for p in found[0]["printings"]}
    assert printings["LEA"]["nonfoil_batch"] == {"id": b1.id, "batch_code": "B1"}
    assert printings["LEA"]["foil_batch"] == {"id": b2.id, "batch_code": "B2"}
    assert printings["M10"]["nonfoil_batch"] == {"id": b1.id, "batch_code": "B1"}
    assert printings["M10"]["foil_batch"] is None


def test_printings_breakdown_only_surfaces_matched_status_scope(session):
    """Widening the printings query must respect the same status scope as
    the rest of the search -- a reserved-only printing shouldn't leak into
    the breakdown under the default available-only scope."""
    b1 = add_batch(session, "B1")
    b2 = add_batch(session, "B2")
    add_card(session, b1, name="Lightning Bolt", set_code="LEA", collector_number="161",
             status="available")
    add_card(session, b2, name="Lightning Bolt", set_code="M10", collector_number="146",
             status="reserved")

    found, not_found = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
    )
    printings = found[0]["printings"]
    assert len(printings) == 1
    assert printings[0]["set_code"] == "LEA"

    found_extended, _ = search_decklist_inventory(
        session, [{"raw_line": "1 Lightning Bolt", "quantity": 1, "name": "Lightning Bolt",
                    "set_code": None, "collector_number": None}],
        DECKLIST_STATUS_SCOPES["extended"],
    )
    printings_extended = found_extended[0]["printings"]
    assert {p["set_code"] for p in printings_extended} == {"LEA", "M10"}
