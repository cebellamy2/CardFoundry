"""Wave status, and what Mark Wave Picked tells the operator.

THREE THINGS, all driven through the real routes.

1. A CANCELLED ORDER MUST NOT HOLD A WAVE AT "PICKED". The operator's rule
   (2026-10-08) is that a wave is shipped once every order that CAN ship
   has shipped. The display mapping -- how every legacy "completed" wave
   is read -- was comparing against EVERY order, so one cancelled order
   held four finished waves at "Picked" on production.

2. SHIPPING A WAVE'S LAST ORDER FROM THE ORDER PAGE MUST CLOSE THE WAVE.
   mark_wave_shipped_if_complete had exactly one caller, the wave's own
   bulk ship, so that route left the wave open for good.

3. MARK WAVE PICKED LANDS BACK ON THE WAVE AND SAYS WHAT IT DID -- it
   used to interrupt with a confirm box and then return saying nothing.
   Mark Wave as Shipped KEEPS its results page: it reports Mana Pool push
   failures and there is nowhere else to put them.
"""
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wavestatus.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory", lambda **kw: [])
    # the processing/shipment pushes are remote; never call them in a test
    monkeypatch.setattr(main, "_push_processing_sync", lambda session, order: None)
    monkeypatch.setattr(main, "_push_shipment_sync", lambda session, order: None)
    return TestClient(main.app), engine


def build_wave(session, *, wave_status, orders):
    """orders: list of (order_status, allocation_status)."""
    wave = PickWave(label="W", status=wave_status, created_at=datetime.now())
    session.add(wave)
    session.flush()
    # batch_code is unique, and this helper is called more than once per
    # test to build several waves
    batch = Batch(batch_code=f"A{wave.id}", is_archived=False)
    session.add(batch)
    session.flush()
    made = []
    for index, (order_status, alloc_status) in enumerate(orders):
        order = SalesOrder(
            external_order_id=f"u-{wave.id}-{index}",
            external_label=f"70164{wave.id}{index:03d}", source="manapool",
            status=order_status, placed_at=datetime.now(),
            shipping_method="first_class", shipping_cents=0,
        )
        session.add(order)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        identity = {
            "name": f"Card {wave.id}-{index}", "set_code": "soc",
            "collector_number": str(index), "scryfall_id": f"sf{wave.id}-{index}",
            "mtgjson_id": f"mtg{wave.id}-{index}", "language_id": "EN",
            "condition_id": "NM", "finish_id": "NF",
        }
        item = OrderItem(order_id=order.id, quantity=1, price_cents=250, **identity)
        session.add(item)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, condition="Near Mint", finish="normal",
            status="sold" if order_status == "shipped" else "reserved",
            current_price=2.5, **identity,
        )
        session.add(card)
        session.flush()
        if alloc_status:
            session.add(PickAllocation(
                order_item_id=item.id, inventory_card_id=card.id,
                batch_id=batch.id, status=alloc_status,
            ))
        made.append(order.id)
    session.flush()
    return wave.id, made


def wave_badge(html):
    """The status word in the badge on the wave detail page. The badge
    renders a glyph before the word, so match the word itself."""
    start = html.index("Status:")
    text = re.sub(r"<[^>]+>", " ", html[start:start + 400]).lower()
    found = re.search(r"\b(shipped|picked|packed|active|cancelled)\b", text)
    assert found, text[:160]
    return found.group(1)


def listed_status(html, wave_id):
    row_at = html.index(f"/pick-waves/{wave_id}")
    row = html[html.rindex("<tr", 0, row_at):html.index("</tr>", row_at)]
    return re.sub(r"<[^>]+>", " ", row).lower()


# --- 1. a cancelled order does not hold a wave ---------------------------

def test_a_legacy_wave_with_a_cancelled_order_displays_shipped(client):
    """★ THE PRODUCTION CASE: waves 40, 42, 48 and 49."""
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="completed", orders=[
            ("shipped", "shipped"), ("shipped", "shipped"), ("cancelled", None),
        ])
        session.commit()

    assert wave_badge(http.get(f"/pick-waves/{wave_id}").text) == "shipped"
    assert "shipped" in listed_status(http.get("/pick-waves?status=all").text, wave_id)


def test_a_legacy_wave_with_an_unshipped_order_still_reads_picked(client):
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="completed", orders=[
            ("shipped", "shipped"), ("packed", "packed"), ("cancelled", None),
        ])
        session.commit()

    assert wave_badge(http.get(f"/pick-waves/{wave_id}").text) == "picked"


def test_a_legacy_wave_of_only_cancelled_orders_is_not_called_shipped(client):
    """Vacuous truth is how a status gets invented."""
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="completed", orders=[
            ("cancelled", None), ("cancelled", None),
        ])
        session.commit()

    assert wave_badge(http.get(f"/pick-waves/{wave_id}").text) == "picked"


def test_the_list_and_the_detail_page_agree(client):
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="completed", orders=[
            ("shipped", "shipped"), ("cancelled", None),
        ])
        session.commit()
    detail = wave_badge(http.get(f"/pick-waves/{wave_id}").text)
    listing = listed_status(http.get("/pick-waves?status=all").text, wave_id)
    assert detail == "shipped" and "shipped" in listing


@pytest.mark.parametrize("stored", ["active", "packed", "shipped", "cancelled"])
def test_a_non_legacy_wave_is_shown_as_stored(client, stored):
    """The mapping only ever refines the legacy word."""
    assert main._wave_display_status(stored, open_orders=3, shipped_orders=3) == stored


def count_membership_queries(engine, http):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "pick_wave_orders" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert http.get("/pick-waves?status=all").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def test_the_wave_list_does_not_query_per_wave(client):
    """★ PINNED AS THE REAL PROPERTY: the number of queries must not grow
    with the number of waves. An absolute ceiling would just be a number
    someone bumps; tripling the waves and seeing the same count is the
    thing that actually rules out an N+1.
    """
    http, engine = client
    with Session(engine) as session:
        for _ in range(4):
            build_wave(session, wave_status="completed", orders=[
                ("shipped", "shipped"), ("cancelled", None)])
        session.commit()
    with_four = count_membership_queries(engine, http)

    with Session(engine) as session:
        for _ in range(8):
            build_wave(session, wave_status="completed", orders=[
                ("shipped", "shipped"), ("cancelled", None)])
        session.commit()
    with_twelve = count_membership_queries(engine, http)

    assert with_four == with_twelve, (
        f"{with_four} queries for 4 waves but {with_twelve} for 12 -- that is an N+1"
    )
    assert with_twelve < 12, "and it must not be one per wave"


# --- 2. the order page closes the wave -----------------------------------

def test_shipping_the_last_order_from_the_order_page_closes_the_wave(client):
    """★ THE LATENT BUG. mark_wave_shipped_if_complete had one caller."""
    http, engine = client
    with Session(engine) as session:
        wave_id, orders = build_wave(session, wave_status="packed", orders=[
            ("shipped", "shipped"), ("packed", "packed"),
        ])
        session.commit()
        last = orders[1]

    response = http.post(f"/orders/{last}/shipped", data={"tracking_number": ""},
                         follow_redirects=False)
    assert response.status_code == 303, response.text[:300]

    with Session(engine) as session:
        assert session.get(PickWave, wave_id).status == "shipped"
        assert session.get(PickWave, wave_id).shipped_at is not None


def test_shipping_a_non_final_order_leaves_the_wave_open(client):
    http, engine = client
    with Session(engine) as session:
        wave_id, orders = build_wave(session, wave_status="packed", orders=[
            ("packed", "packed"), ("packed", "packed"),
        ])
        session.commit()

    http.post(f"/orders/{orders[0]}/shipped", data={"tracking_number": ""},
              follow_redirects=False)
    with Session(engine) as session:
        assert session.get(PickWave, wave_id).status == "packed"


def test_a_cancelled_order_does_not_keep_the_wave_open_either(client):
    http, engine = client
    with Session(engine) as session:
        wave_id, orders = build_wave(session, wave_status="packed", orders=[
            ("packed", "packed"), ("cancelled", None),
        ])
        session.commit()

    http.post(f"/orders/{orders[0]}/shipped", data={"tracking_number": ""},
              follow_redirects=False)
    with Session(engine) as session:
        assert session.get(PickWave, wave_id).status == "shipped"


def test_shipping_an_order_in_no_wave_still_works(client):
    http, engine = client
    with Session(engine) as session:
        batch = Batch(batch_code="B", is_archived=False)
        session.add(batch)
        session.flush()
        order = SalesOrder(external_order_id="solo", external_label="999999-9999999",
                           source="manapool", status="packed", placed_at=datetime.now(),
                           shipping_method="first_class", shipping_cents=0)
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, quantity=1, name="C", price_cents=100)
        session.add(item)
        session.flush()
        card = InventoryCard(batch_id=batch.id, name="C", condition="Near Mint",
                             finish="normal", status="reserved", current_price=1.0)
        session.add(card)
        session.flush()
        session.add(PickAllocation(order_item_id=item.id, inventory_card_id=card.id,
                                   batch_id=batch.id, status="packed"))
        session.commit()
        order_id = order.id

    assert http.post(f"/orders/{order_id}/shipped", data={"tracking_number": ""},
                     follow_redirects=False).status_code == 303
    with Session(engine) as session:
        assert session.get(SalesOrder, order_id).status == "shipped"


# --- 3. Mark Wave Picked ------------------------------------------------

def test_mark_picked_lands_back_on_the_wave_with_a_note(client):
    http, engine = client
    with Session(engine) as session:
        wave_id, orders = build_wave(session, wave_status="active", orders=[
            ("in_pick_wave", "allocated"), ("in_pick_wave", "allocated"),
        ])
        session.commit()

    response = http.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/pick-waves/{wave_id}?picked=2&held=0"

    page = http.get(response.headers["location"]).text
    assert "2 orders marked picked." in page
    with Session(engine) as session:
        assert [session.get(SalesOrder, o).status for o in orders] == ["picked", "picked"]


def test_the_note_says_one_order_in_the_singular(client):
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="active", orders=[
            ("in_pick_wave", "allocated"),
        ])
        session.commit()
    response = http.post(f"/pick-waves/{wave_id}/complete", follow_redirects=False)
    assert "1 order marked picked." in http.get(response.headers["location"]).text


def test_there_is_no_confirmation_step_any_more(client):
    """The action used to interrupt with a browser confirm box."""
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="active", orders=[
            ("in_pick_wave", "allocated"),
        ])
        session.commit()
    page = http.get(f"/pick-waves/{wave_id}").text
    form_at = page.index(f'action="/pick-waves/{wave_id}/complete"')
    form = page[page.rindex("<form", 0, form_at):page.index("</form>", form_at)]
    assert "confirm(" not in form, "Mark Wave Picked should not interrupt"
    assert "Mark Wave Picked" in form


def test_cancel_pick_wave_keeps_its_confirmation(client):
    """That one throws the picking away, so it still asks."""
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="active", orders=[
            ("in_pick_wave", "allocated"),
        ])
        session.commit()
    page = http.get(f"/pick-waves/{wave_id}").text
    form_at = page.index(f'action="/pick-waves/{wave_id}/cancel"')
    form = page[page.rindex("<form", 0, form_at):page.index("</form>", form_at)]
    assert "confirm(" in form


def test_an_ordinary_visit_shows_no_note(client):
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="active", orders=[
            ("in_pick_wave", "allocated"),
        ])
        session.commit()
    page = http.get(f"/pick-waves/{wave_id}").text
    assert "marked picked." not in page


def test_mark_wave_as_shipped_still_has_its_results_page(client):
    """★ UNCHANGED ON PURPOSE: it reports Mana Pool push failures."""
    http, engine = client
    with Session(engine) as session:
        wave_id, orders = build_wave(session, wave_status="packed", orders=[
            ("packed", "packed"),
        ])
        session.commit()

    response = http.post(f"/pick-waves/{wave_id}/ship",
                         data={"ship_order_ids": orders, "tracking_numbers": [""]},
                         follow_redirects=False)
    assert response.status_code == 200, "a results page, not a redirect"
    assert "Bulk Ship Results" in response.text


def test_the_note_survives_a_nonsense_query_string(client):
    """A hand-edited URL is not worth a 500 and not worth a wrong number."""
    http, engine = client
    with Session(engine) as session:
        wave_id, _ = build_wave(session, wave_status="active", orders=[
            ("in_pick_wave", "allocated"),
        ])
        session.commit()
    assert http.get(f"/pick-waves/{wave_id}?picked=0&held=0").status_code == 200
    assert main._wave_picked_note("x", "y") == ""
    assert main._wave_picked_note(None, None) == ""
