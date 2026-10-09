"""Line details on the attention page's short-order table, and only there.

★ THE DECISION, PER SUB-TABLE (2026-10-09). /orders/shipment-sync-issues
has five order-ish sub-tables. Exactly one earns the line details:

  Short / unallocatable orders      YES -- a short order is short OF
      SPECIFIC CARDS. Which lines are on it, and which batch each was
      filled from, IS the diagnosis.
  Mana Pool sync                    no -- an order-level push failure;
      which cards are on the order is irrelevant to retrying a status push.
  Fulfillment exceptions            no -- already names the exact card per
      row, so the order's other lines would be noise around it.
  Cancelled to match Mana Pool      no -- already carries a per-line
      SETTLEMENT column, which says more here than card details would.
  Everything waiting on you         no -- not per-order at all; its items
      are pricing, webhooks and listings as well as orders.

Lines in the other four would be bulk, not information, which is the test
the operator set.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    SalesOrder,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'attention-lines.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def short_order(session, *, lines=2, allocate=True, status="short"):
    batch = session.query(Batch).filter_by(batch_code="A2").one_or_none()
    if not batch:
        batch = Batch(batch_code="A2")
        session.add(batch)
        session.flush()
    rec = ImportRecord(batch_id=batch.id, filename="x.csv",
                       file_hash=f"h-{session.query(ImportRecord).count()}",
                       card_count=1)
    session.add(rec)
    session.flush()
    order = SalesOrder(external_order_id="o-short", external_label="L-short",
                       source="manapool", status=status,
                       review_detail="Only 1 of 2 lines could be allocated.")
    session.add(order)
    session.flush()
    for n in range(lines):
        card = InventoryCard(
            batch_id=batch.id, import_id=rec.id, name=f"Brainstorm {n}",
            set_code="ICE", collector_number=str(n + 1),
            scryfall_id=f"sf-{n}", mtgjson_id=f"mtg-{n}", language_id="EN",
            condition_id="LP", finish_id="NF", condition="LP",
            finish="normal", status="reserved",
        )
        session.add(card)
        session.flush()
        item = OrderItem(
            order_id=order.id, name=card.name, set_code="ICE",
            collector_number=str(n + 1), finish="normal",
            scryfall_id=card.scryfall_id, language_id="EN",
            condition_id="LP", finish_id="NF", quantity=1, price_cents=175,
        )
        session.add(item)
        session.flush()
        if allocate and n == 0:
            session.add(PickAllocation(
                order_item_id=item.id, inventory_card_id=card.id,
                batch_id=batch.id, status="allocated",
            ))
    session.commit()
    return order


def test_a_short_order_shows_its_lines_expanded(db):
    with Session(db) as session:
        short_order(session, lines=2)
    html = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "Short / unallocatable orders" in html
    assert 'class="section-disclosure order-lines" open>' in html
    assert "2 lines</summary>" in html
    assert "Brainstorm 0" in html
    assert "$1.75" in html


def test_the_filled_line_names_its_batch_and_the_unfilled_one_an_em_dash(db):
    """This is why the lines belong here: the operator can see at a glance
    which line is the one that could not be filled."""
    with Session(db) as session:
        short_order(session, lines=2, allocate=True)
    html = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "A2" in html          # the line that WAS filled
    assert "&mdash;" in html     # the line that was not


def test_the_short_table_offers_the_shared_toolbar(db):
    with Session(db) as session:
        short_order(session)
    html = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "Expand all order details" in html
    assert "Collapse all order details" in html


def test_no_toolbar_when_there_are_no_short_orders(db):
    """An empty section must not grow a toolbar controlling nothing."""
    html = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "No orders are currently short" in html
    assert "Expand all order details" not in html


def test_the_disclosure_row_spans_the_short_table(db):
    with Session(db) as session:
        short_order(session)
    html = TestClient(main.app).get("/orders/shipment-sync-issues").text
    # Status, Order, Created, Why, action
    assert '<td colspan="5">' in html


def test_only_short_orders_grow_line_details(db):
    """★ THE DELIBERATE OMISSION. The number of disclosures equals the
    number of SHORT orders exactly -- not the number of order-ish rows on
    the page. A fulfillment exception on another order puts that order on
    the page and names its card, and still adds no disclosure."""
    from datetime import datetime

    from fulfillment_exception_service import mark_fulfillment_exception

    with Session(db) as session:
        first = short_order(session, lines=2)
        first.external_order_id = "o-short-1"
        first.external_label = "L-short-1"

        second = short_order(session, lines=1)
        second.external_order_id = "o-short-2"
        second.external_label = "L-short-2"
        session.commit()

        # A third order, on the page via a fulfillment exception.
        batch = session.query(Batch).filter_by(batch_code="A2").one()
        rec = ImportRecord(batch_id=batch.id, filename="x.csv",
                           file_hash="h-exc", card_count=1)
        session.add(rec)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, import_id=rec.id, name="Counterspell",
            set_code="ICE", collector_number="9", scryfall_id="sf-exc",
            mtgjson_id="mtg-exc", language_id="EN", condition_id="LP",
            finish_id="NF", condition="LP", finish="normal", status="reserved",
        )
        session.add(card)
        session.flush()
        third = SalesOrder(external_order_id="o-exc", external_label="L-exc",
                           source="manapool", status="in_pick_wave")
        session.add(third)
        session.flush()
        item = OrderItem(
            order_id=third.id, name=card.name, set_code="ICE",
            collector_number="9", scryfall_id=card.scryfall_id,
            mtgjson_id=card.mtgjson_id, language_id="EN", condition_id="LP",
            finish_id="NF", quantity=1, price_cents=100,
        )
        session.add(item)
        session.flush()
        allocation = PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=batch.id, status="allocated",
        )
        session.add(allocation)
        session.commit()
        mark_fulfillment_exception(session, allocation.id, "missing")
        session.commit()

    html = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "L-exc" in html                 # the exception row is there
    assert "Counterspell" in html          # and it names its own card
    # ...but only the two SHORT orders carry a disclosure.
    assert html.count('class="section-disclosure order-lines"') == 2
    assert "L-short-1" in html and "L-short-2" in html
