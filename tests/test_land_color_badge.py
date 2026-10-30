"""Lands show L on screen, not C -- and the screen and the slip agree.

THE BUG (operator, 2026-10-28). `color` stores the empty string for a
land AND for a colourless non-land, and main.py's `_color_badge` took only
the colour, so every land on screen rendered a confident, wrong **C**.
Measured on production 2026-10-30: 674 of the 11,336 available cards are
lands, every one with `color = ''`, and all 674 showed C. The packing slip
has printed (L) correctly since v2.68.0 because it goes through
`card_color_marker.color_marker`, which also takes the type line.

So these tests assert the outcome the operator sees -- the rendered page --
and then pin the slip's marker against the screen's badge for the same
card, which is the assertion that stops the two drifting apart again.

The reversible double-faced case is here deliberately: Scryfall omits the
top-level `type_line` for a `reversible_card` and carries it per face, so
Steam Vents and Hallowed Fountain are the printings that went wrong in
v2.68.0. A stored type line can therefore be either the bare front-face
line or a joined "A // B" one, and both must reach L.
"""
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import card_color_badge
import card_color_marker
import inventory_sync_service
import main
from models import Base, Batch, InventoryCard

# (name, set_code, collector_number, color, type_line) -- the stored shape
LAND_PLAIN = ("Ancient Tomb", "TMP", "237", "", "Land")
LAND_SUBTYPED = ("Hallowed Fountain", "RNA", "251", "", "Land — Plains Island")
# A reversible double-faced land: v2.68.0's scryfall_card_type_line stores
# the FRONT face, so this is what lands in the column.
LAND_REVERSIBLE = ("Steam Vents", "ECL", "347", "", "Land — Island Mountain")
# A joined line, which is what a modal DFC stores -- front face is a land.
LAND_JOINED = ("Shelldock Isle", "LRW", "275", "", "Land // Land")
# Front face is a SPELL, so NOT a land however the back reads.
SPELL_WITH_LAND_BACK = ("Malevolent Hermit", "MID", "336", "U",
                        "Creature — Human Wizard // Land")
COLORLESS_NONLAND = ("Sol Ring", "LEA", "237", "", "Artifact")
MONO = ("Lightning Bolt", "LEA", "161", "R", "Instant")
MULTI = ("Baleful Strix", "C13", "190", "UB", "Artifact Creature — Bird")
LAND_WITH_A_COLOUR = ("Dryad Arbor", "FUT", "174", "G",
                      "Land Creature — Forest Dryad")


def setup_db(tmp_path, monkeypatch, name="badge.db"):
    db = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add(session, row, *, batch, status="available", price=1.00):
    name, set_code, number, color, type_line = row
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code=set_code,
        collector_number=number, color=color, type_line=type_line,
        language_id="EN", condition_id="NM", finish_id="NF",
        status=status, current_price=price,
    )
    session.add(card)
    session.flush()
    return card


def seed(session, rows, batch_code="A1"):
    batch = Batch(batch_code=batch_code, is_archived=False)
    session.add(batch)
    session.flush()
    return [add(session, row, batch=batch) for row in rows]


def badges_for(html, card_name):
    """Every pip letter rendered in the cells that mention this card.

    Scans EVERY matching cell rather than the first: the row's leading
    select cell carries an `aria-label="Select <name>"`, so "the first td
    containing the name" is the checkbox, not the name cell. Collecting
    across all of them keeps an empty result meaningful -- it means no pip
    was rendered anywhere for this card, which is what the no-type-line
    case must produce.
    """
    cells = [block for block in re.findall(r"<td[^>]*>.*?</td>", html, re.S)
             if card_name in block]
    assert cells, f"no cell rendered for {card_name}"
    pips = []
    for cell in cells:
        pips.extend(
            re.findall(r'class="color-pip color-pip-(\w)"[^>]*>(\w+)<', cell)
        )
    return pips


def distinct_badges_for(html, card_name):
    """The distinct badges rendered for this card anywhere on the page.

    An order page shows the same card in more than one place (the order
    line and its pick allocation), so a count assertion would be about
    the page's layout rather than about the badge. What matters is that
    every badge for a card agrees -- so this collapses them, and a page
    that disagreed with itself would show up as two entries.
    """
    return sorted(set(badges_for(html, card_name)))


# --- the rule, at the unit the slip and the screen share ------------------

def test_the_badge_and_the_slip_marker_agree_for_every_shape():
    """The assertion that stops them drifting. Same two stored values in,
    and the badge must be exactly the slip's marker, rendered."""
    for row in (LAND_PLAIN, LAND_SUBTYPED, LAND_REVERSIBLE, LAND_JOINED,
                SPELL_WITH_LAND_BACK, COLORLESS_NONLAND, MONO, MULTI,
                LAND_WITH_A_COLOUR):
        name, _set, _num, color, type_line = row
        marker = card_color_marker.color_marker(color, type_line)
        html = card_color_badge.color_badge_html(color, type_line)
        letters = "".join(re.findall(r'title="[^"]*">(\w+)<', html))
        assert letters == marker, (
            f"{name}: slip prints {marker!r} but the screen renders "
            f"{letters!r} -- the two have drifted"
        )


def test_a_land_is_L_whatever_its_colour():
    assert card_color_badge.color_badge_html("", "Land") == (
        '<span class="color-pip color-pip-l" title="Land">L</span>'
    )
    # Dryad Arbor is a green card and still (L): the operator's rule.
    assert "L</span>" in card_color_badge.color_badge_html(
        "G", "Land Creature — Forest Dryad")
    assert "color-pip-g" not in card_color_badge.color_badge_html(
        "G", "Land Creature — Forest Dryad")


def test_a_colourless_non_land_is_still_C():
    assert card_color_badge.color_badge_html("", "Artifact") == (
        '<span class="color-pip color-pip-c" title="Colorless">C</span>'
    )


def test_no_type_line_renders_no_badge_never_a_wrong_C():
    assert card_color_badge.color_badge_html("", None) == ""


# --- Inventory Search, the real route ------------------------------------

def test_inventory_search_shows_L_for_lands_and_colours_for_the_rest(
        tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        seed(session, [LAND_PLAIN, LAND_REVERSIBLE, COLORLESS_NONLAND,
                       MONO, MULTI, SPELL_WITH_LAND_BACK])
        session.commit()

    resp = TestClient(main.app).get("/inventory?status_submitted=1")
    assert resp.status_code == 200
    html = resp.text

    assert badges_for(html, "Ancient Tomb") == [("l", "L")]
    # The reversible double-faced land -- the v2.68.0 victim.
    assert badges_for(html, "Steam Vents") == [("l", "L")]
    # A colourless NON-land keeps C.
    assert badges_for(html, "Sol Ring") == [("c", "C")]
    assert badges_for(html, "Lightning Bolt") == [("r", "R")]
    assert badges_for(html, "Baleful Strix") == [("u", "U"), ("b", "B")]
    # Front face is a creature, so it is NOT a land.
    assert badges_for(html, "Malevolent Hermit") == [("u", "U")]


def test_inventory_search_no_longer_calls_a_land_colourless(
        tmp_path, monkeypatch):
    """The bug, stated as the operator reported it."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        seed(session, [LAND_PLAIN])
        session.commit()
    html = TestClient(main.app).get("/inventory?status_submitted=1").text
    assert badges_for(html, "Ancient Tomb") == [("l", "L")]
    assert ("c", "C") not in badges_for(html, "Ancient Tomb")


# --- the orders pick list, the real route --------------------------------

def _order_with_cards(session, rows):
    """Build the order through the REAL ingest, including the real
    Scryfall enrichment that puts `type_line` on an OrderItem.

    ★ The only seam is `scryfall_lookup`, which ingest_manapool_orders
    already takes as an argument -- so the fake hands back RAW
    Scryfall-shaped card dicts and the production code does the rest.
    That matters here more than convenience: it means the reversible
    double-faced case runs through the real
    `legacy_import_service.scryfall_card_type_line`, which is the function
    v2.68.0 added because Scryfall omits the top-level `type_line` for a
    `reversible_card` and carries it per face. A hand-built OrderItem
    would have skipped the very code path this bug is about.
    """
    from order_service import ingest_manapool_orders

    batch = Batch(batch_code="A1", is_archived=False)
    session.add(batch)
    session.flush()
    cards = []
    for index, row in enumerate(rows, start=1):
        card = add(session, row, batch=batch)
        card.scryfall_id = f"sf-{index}"
        card.mtgjson_id = f"mtg-{index}"
        cards.append(card)
    session.commit()

    items = []
    scryfall_cards = {}
    for index, (row, card) in enumerate(zip(rows, cards), start=1):
        name, set_code, number, color, type_line = row
        items.append({
            "id": f"item-{index}", "quantity": 1, "price_cents": 100,
            "product": {"single": {
                "name": name, "set": set_code, "number": number,
                "scryfall_id": card.scryfall_id, "mtgjson_id": card.mtgjson_id,
                "language_id": "EN", "condition_id": "NM", "finish_id": "NF",
            }},
        })
        if row is LAND_REVERSIBLE:
            # Exactly Scryfall's reversible_card shape: NO top-level
            # type_line or colors, both carried per face only.
            scryfall_cards[card.scryfall_id] = {
                "id": card.scryfall_id, "name": name,
                "card_faces": [
                    {"type_line": type_line, "colors": []},
                    {"type_line": "Land — Island Mountain", "colors": []},
                ],
            }
        else:
            scryfall_cards[card.scryfall_id] = {
                "id": card.scryfall_id, "name": name,
                "type_line": type_line, "colors": list(color),
            }

    orders = [{
        "id": "order-badge-1", "order_number": "999999-1",
        "status": "paid", "created_at": "2026-10-30T00:00:00Z",
        "total_cents": 100,
    }]
    ingest_manapool_orders(
        session, orders,
        lambda order_id: {"order": {**orders[0], "items": items}},
        scryfall_lookup=lambda ids: {
            scryfall_id: scryfall_cards[scryfall_id]
            for scryfall_id in ids if scryfall_id in scryfall_cards
        },
        max_orders=5,
    )
    session.commit()
    return cards


def test_the_order_pick_list_shows_L_for_a_land(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _order_with_cards(session, [LAND_PLAIN, LAND_REVERSIBLE,
                                    COLORLESS_NONLAND, MONO])
        from models import SalesOrder
        order_id = session.query(SalesOrder).one().id

    resp = TestClient(main.app).get(f"/orders/{order_id}")
    assert resp.status_code == 200
    html = resp.text
    assert distinct_badges_for(html, "Ancient Tomb") == [("l", "L")]
    assert distinct_badges_for(html, "Steam Vents") == [("l", "L")]
    assert distinct_badges_for(html, "Sol Ring") == [("c", "C")]
    assert distinct_badges_for(html, "Lightning Bolt") == [("r", "R")]


def test_the_slip_and_the_order_page_agree_on_the_same_card(
        tmp_path, monkeypatch):
    """One card, both surfaces, from the same stored row."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _order_with_cards(session, [LAND_REVERSIBLE])
        from models import OrderItem, SalesOrder
        order_id = session.query(SalesOrder).one().id
        item = session.query(OrderItem).one()
        slip_marker = card_color_marker.color_marker(
            item.color, item.type_line)

    html = TestClient(main.app).get(f"/orders/{order_id}").text
    screen = "".join(
        letter for _cls, letter in distinct_badges_for(html, "Steam Vents")
    )
    assert slip_marker == "L"
    assert screen == slip_marker


# --- no new queries -------------------------------------------------------

def test_the_badge_adds_no_query_to_inventory_search(tmp_path, monkeypatch):
    """type_line is a column on the row already being selected, so the
    land rule must cost nothing. Instrumented, not reasoned about: any
    per-card SELECT would scale with the 30 cards below."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch)
        session.flush()
        # 20, not 30: INVENTORY_SEARCH_DEFAULT_PAGE_SIZE is 25, and a
        # page that silently truncated would weaken the assertion below.
        for index in range(20):
            add(session, ("Land %d" % index, "TMP", str(index), "", "Land"),
                batch=batch)
        session.commit()

    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db, "before_cursor_execute", record)
    try:
        resp = TestClient(main.app).get("/inventory?status_submitted=1")
    finally:
        event.remove(db, "before_cursor_execute", record)

    assert resp.status_code == 200
    # +1 for the .color-pip-l rule in the inline stylesheet.
    assert resp.text.count('color-pip-l') == 21, "all 20 lands must show L"
    per_card = [
        q for q in queries
        if "inventory_cards" in q and "type_line" in q and " WHERE " in q
        and "inventory_cards.id = ?" in q
    ]
    assert per_card == [], (
        f"the badge must read type_line off the row already selected; "
        f"got {len(per_card)} per-card lookup(s)"
    )
