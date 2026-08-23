import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from decklist_search_service import (
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


def add_batch(session, code="B1"):
    batch = Batch(batch_code=code)
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
