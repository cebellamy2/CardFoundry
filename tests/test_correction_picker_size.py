"""The correction picker scales to its list, and only scrolls past ten.

THE OPERATOR: "the box where you select the correct option should scale
to meet the list up to 10, only scroll after eleven plus options."

So a three-option result shows three rows and no scrollbar, a ten-option
result shows ten, and an eleven-option result shows ten and scrolls.

★ THE SCAN PICKER IS A DIFFERENT COMPONENT and is deliberately NOT
touched: the by-name printing picker used by scan-to-inventory was moved
off a fixed-size select on purpose (a select clipped unreadably at 390px
and Sol Ring has 130 printings), and is filterable rows instead.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, InventoryCard


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'picker.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Lightning Bolt", status="available",
            condition="Near Mint", finish="normal", set_code="lea",
            collector_number="161", language_id="EN", condition_id="NM",
            finish_id="NF", scryfall_id="sf-1", mtgjson_id="mtg-1",
        )
        session.add(card)
        session.commit()
        card_id = card.id
    return engine, card_id


def printings(count):
    return [
        {"id": f"sf-{i}", "set_name": f"Set {i}", "set": f"s{i}",
         "collector_number": str(i), "lang": "en", "finishes": ["nonfoil"],
         "released_at": "2020-01-01"}
        for i in range(count)
    ]


def rendered_size(html):
    match = re.search(r'<select name="replacement_scryfall_id" size="(\d+)"', html)
    assert match, "the picker should render with a size"
    return int(match.group(1))


def option_count(html):
    start = html.index('name="replacement_scryfall_id"')
    block = html[start:html.index("</select>", start)]
    return block.count("<option ")


# --- the rule the operator asked for ------------------------------------

@pytest.mark.parametrize("options,expected", [(3, 3), (10, 10), (11, 10), (40, 10)])
def test_the_picker_scales_to_the_list_and_stops_at_ten(db, monkeypatch, options, expected):
    engine, card_id = db
    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: printings(options))
    html = TestClient(main.app).get(f"/inventory/{card_id}/printing-correction/options").text
    assert option_count(html) == options
    assert rendered_size(html) == expected


def test_three_options_show_all_three_with_no_scrolling(db, monkeypatch):
    """The operator's actual complaint: a short list in a tall box."""
    engine, card_id = db
    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: printings(3))
    html = TestClient(main.app).get(f"/inventory/{card_id}/printing-correction/options").text
    assert rendered_size(html) == option_count(html) == 3


def test_eleven_options_scroll_at_ten(db, monkeypatch):
    engine, card_id = db
    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: printings(11))
    html = TestClient(main.app).get(f"/inventory/{card_id}/printing-correction/options").text
    assert option_count(html) == 11
    assert rendered_size(html) == 10, "eleven options is where scrolling starts"


def test_it_is_never_a_dropdown(db, monkeypatch):
    """size=1 is a dropdown, not a short list box -- the control must not
    change shape when a search returns a single printing."""
    engine, card_id = db
    for count in (0, 1):
        monkeypatch.setattr(main, "search_scryfall_printings", lambda name, c=count: printings(c))
        html = TestClient(main.app).get(f"/inventory/{card_id}/printing-correction/options").text
        assert rendered_size(html) >= 2, f"{count} options should still be a list box"


def test_the_sizing_rule_itself(db):
    assert main._printing_picker_size(0) == 2
    assert main._printing_picker_size(1) == 2
    assert main._printing_picker_size(2) == 2
    assert main._printing_picker_size(3) == 3
    assert main._printing_picker_size(10) == 10
    assert main._printing_picker_size(11) == 10
    assert main._printing_picker_size(130) == 10


def test_no_picker_is_left_at_a_hardcoded_size(db):
    """Both correction pickers share the rule; neither keeps a fixed size."""
    source = open(main.__file__).read()
    assert 'name="replacement_scryfall_id" size="15"' not in source
    assert 'name="replacement_scryfall_id" size="10"' not in source
    assert source.count('name="replacement_scryfall_id" size="{picker_size}"') == 2


def test_the_scan_by_name_picker_is_untouched(db):
    """A different component on purpose -- filterable ROWS, not a select
    -- so the sizing rule must not have been applied to it. Asserted on
    what it actually renders, not on the source: the source still
    mentions the old select in a docstring recording why it was replaced.
    """
    html = main._printing_picker_html(
        printings(12), card_name="Sol Ring", set_filter="", page=1,
        target_batch_id=None,
    )
    assert "<select" not in html, (
        "the by-name picker is rows, and must not become a select again"
    )
    assert 'size="' not in html
