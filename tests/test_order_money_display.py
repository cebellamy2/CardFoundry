"""Dollar amounts on Orders: list column, detail summary, line amounts.

One rule underpins all of it: an unpriced line makes a total UNKNOWN, not
zero. Five real order lines have no stored price, and the packing slip
used to print a confident "$0.00" for them and silently count them as
nothing -- a wrong number that looked exact. Every surface now renders an
em dash and excludes the line, and says so.

The figures are AS ORDERED. price_cents is not part of the line signature
used to detect changes on re-sync, so it is never refreshed; the pages
label it rather than implying a live price.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, OrderItem, SalesOrder
from packing_slip_service import _line_total_cents, _money
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'money.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def order_with(session, *, prices, shipping=135, quantities=None, label="mp-money",
               remote_status=None):
    """prices: list of price_cents per line (None = unpriced)."""
    order, item, card, allocation = seed(session)
    order.external_order_id = label
    order.external_label = label
    order.shipping_cents = shipping
    order.remote_fulfillment_status = remote_status
    quantities = quantities or [1] * len(prices)
    item.price_cents = prices[0]
    item.quantity = quantities[0]
    for price, qty in zip(prices[1:], quantities[1:]):
        session.add(OrderItem(
            order_id=order.id, name="Extra", set_code="SET", collector_number="2",
            scryfall_id="sf2", mtgjson_id="mtg2", language_id="EN",
            condition_id="LP", finish_id="NF", quantity=qty, price_cents=price,
        ))
    session.commit()
    return order


# --- the shared helper -------------------------------------------------

def test_an_unpriced_line_makes_the_total_unknown_not_smaller(db):
    with Session(db) as session:
        order = order_with(session, prices=[500, None])
        items = session.query(OrderItem).filter_by(order_id=order.id).all()
        money = main._order_money(items, order.shipping_cents)
    assert money["subtotal_cents"] == 500      # the priced line still counts
    assert money["unpriced_lines"] == 1
    assert money["total_cents"] is None        # but the TOTAL is unknowable


def test_a_fully_priced_order_totals_subtotal_plus_shipping(db):
    with Session(db) as session:
        order = order_with(session, prices=[500, 250], shipping=135)
        items = session.query(OrderItem).filter_by(order_id=order.id).all()
        money = main._order_money(items, order.shipping_cents)
    assert money["subtotal_cents"] == 750
    assert money["total_cents"] == 885


def test_an_unknown_shipping_cost_also_makes_the_total_unknown(db):
    with Session(db) as session:
        order = order_with(session, prices=[500], shipping=None)
        items = session.query(OrderItem).filter_by(order_id=order.id).all()
        money = main._order_money(items, order.shipping_cents)
    assert money["subtotal_cents"] == 500
    assert money["total_cents"] is None


def test_line_total_multiplies_by_quantity(db):
    with Session(db) as session:
        order = order_with(session, prices=[250], quantities=[4])
        item = session.query(OrderItem).filter_by(order_id=order.id).one()
        assert main._line_total_cents(item) == 1000


# --- Orders list --------------------------------------------------------

def test_orders_list_shows_the_buyer_paid_total(db):
    with Session(db) as session:
        order_with(session, prices=[500, 250], shipping=135, label="mp-list")

    text = TestClient(main.app).get("/orders?status=all").text
    assert "<th>Cards</th>" in text
    assert "$8.85" in text


def test_orders_list_shows_an_em_dash_when_a_line_has_no_price(db):
    with Session(db) as session:
        order_with(session, prices=[500, None], label="mp-gap")

    text = TestClient(main.app).get("/orders?status=all").text
    assert "mp-gap" in text
    assert "$6.35" not in text     # would be the understated figure


def test_orders_list_labels_the_figure_as_ordered(db):
    text = TestClient(main.app).get("/orders?status=all").text
    assert main.AS_ORDERED_NOTE in text


def test_a_refunded_order_keeps_its_original_total_with_a_note(db):
    """Mana Pool exposes no per-line refund data, so any adjusted figure
    would be invented. The original amount plus a note is the honest one."""
    with Session(db) as session:
        order_with(session, prices=[500], shipping=135,
                   label="mp-refunded", remote_status="refunded")

    text = TestClient(main.app).get("/orders?status=all").text
    assert "$6.35" in text          # the ORIGINAL total, not recomputed
    assert "refunded" in text


# --- Order Detail -------------------------------------------------------

def test_order_detail_summary_shows_subtotal_shipping_and_total(db):
    with Session(db) as session:
        order = order_with(session, prices=[500, 250], shipping=135)
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "$7.50" in text      # subtotal
    assert "$1.35" in text      # shipping
    assert "$8.85" in text      # total


def test_order_detail_names_the_unpriced_lines_instead_of_a_wrong_total(db):
    with Session(db) as session:
        order = order_with(session, prices=[500, None])
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "without a stored price" in text


def test_a_line_shows_its_total_and_only_shows_unit_price_when_qty_over_one(db):
    with Session(db) as session:
        order = order_with(session, prices=[250], quantities=[4])
        single = order_with(session, prices=[700], quantities=[1], label="mp-single")
        order_id, single_id = order.id, single.id

    multi_text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "$10.00" in multi_text          # line total
    assert "4 &times; $2.50" in multi_text  # unit price alongside

    single_text = TestClient(main.app).get(f"/orders/{single_id}").text
    assert "$7.00" in single_text
    assert "&times;" not in single_text.split("Order Lines")[-1]


# --- packing slip -------------------------------------------------------

def test_packing_slip_line_total_is_none_not_zero_when_unpriced():
    class Item:
        price_cents = None
        quantity = 2
    assert _line_total_cents(Item()) is None


def test_packing_slip_money_renders_unknown_as_an_em_dash():
    assert _money(None) == "—"
    assert _money(1234) == "$12.34"


def test_packing_slip_excludes_an_unpriced_line_from_the_subtotal(db):
    """The regression that motivated decision 5: it used to print $0.00
    and count the line as nothing."""
    from packing_slip_service import generate_packing_slip_pdf
    with Session(db) as session:
        order = order_with(session, prices=[500, None], shipping=135)
        items = session.query(OrderItem).filter_by(order_id=order.id).all()
        pdf = generate_packing_slip_pdf(order, items)
    assert pdf[:4] == b"%PDF"       # it still renders


# --- one source of truth ------------------------------------------------

def test_the_list_the_detail_page_and_the_slip_agree_on_one_order(db):
    """All three read the same helpers, so they cannot quote different
    numbers for the same order."""
    with Session(db) as session:
        order = order_with(session, prices=[1066, 1207], shipping=135, label="mp-agree")
        order_id = order.id
        items = session.query(OrderItem).filter_by(order_id=order_id).all()
        money = main._order_money(items, order.shipping_cents)

    expected_total = main._money_from_cents(money["total_cents"])
    assert expected_total == "$24.08"

    client = TestClient(main.app)
    assert expected_total in client.get("/orders?status=all").text
    assert expected_total in client.get(f"/orders/{order_id}").text
