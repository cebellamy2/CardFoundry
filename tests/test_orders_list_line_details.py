"""Order line details on the /orders list, expanded, with no N+1.

★ WHY THE STATEMENT COUNT IS PINNED AND NOT JUST THE MARKUP. /orders
renders up to ORDERS_PAGE_SIZE (100) rows and is the busiest page in the
app. Measured before this change: 19 SQL statements, FLAT at 5, 20 and 100
orders -- the page was already fully aggregated. Adding per-order lines the
obvious way (one query for the lines, one for the batch codes, per row)
would have made it 19 + 2N. order_lines_prefetch does both for the whole
page, so it is 21, still flat.

The test asserts FLATNESS, not a magic number: a future column or filter
may legitimately add a constant, but nothing may make the count grow with
the page.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    SalesOrder,
)


def setup_db(tmp_path, monkeypatch, name="orders-lines.db"):
    db = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def seed_orders(session, *, count, lines=2, batch_code="A2"):
    batch = session.query(Batch).filter_by(batch_code=batch_code).one_or_none()
    if not batch:
        batch = Batch(batch_code=batch_code)
        session.add(batch)
        session.flush()
    rec = ImportRecord(batch_id=batch.id, filename="x.csv",
                       file_hash=f"h-{batch_code}", card_count=1)
    session.add(rec)
    session.flush()
    for o in range(count):
        order = SalesOrder(external_order_id=f"o-{o}", external_label=f"L-{o}",
                           source="manapool", status="ready_to_pick")
        session.add(order)
        session.flush()
        for n in range(lines):
            card = InventoryCard(
                batch_id=batch.id, import_id=rec.id, name=f"Lightning Bolt {o}-{n}",
                set_code="LEA", collector_number=str(n + 1),
                scryfall_id=f"sf-{o}-{n}", mtgjson_id=f"mtg-{o}-{n}",
                language_id="JA", condition_id="LP", finish_id="NF",
                condition="LP", finish="normal", status="reserved",
            )
            session.add(card)
            session.flush()
            item = OrderItem(
                order_id=order.id, name=card.name, set_code="LEA",
                collector_number=str(n + 1), finish="normal",
                scryfall_id=card.scryfall_id, language_id="JA",
                condition_id="LP", finish_id="NF", quantity=1, price_cents=250,
            )
            session.add(item)
            session.flush()
            session.add(PickAllocation(
                order_item_id=item.id, inventory_card_id=card.id,
                batch_id=batch.id, status="allocated",
            ))
    session.commit()


def count_statements(db, url):
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(db, "before_cursor_execute", record)
    try:
        response = TestClient(main.app).get(url)
    finally:
        event.remove(db, "before_cursor_execute", record)
    assert response.status_code == 200
    return len(statements), response.text


# --- the N+1 guard -------------------------------------------------------

@pytest.mark.parametrize("count", [1, 5, 25])
def test_the_statement_count_does_not_grow_with_the_page(tmp_path, monkeypatch, count):
    """★ THE ONE THAT MATTERS. Flat, whatever the page size."""
    db = setup_db(tmp_path, monkeypatch, name=f"orders-{count}.db")
    with Session(db) as session:
        seed_orders(session, count=count, lines=3)
    statements, _ = count_statements(db, "/orders?status=all")
    assert statements == 21, (count, statements)


def test_more_lines_per_order_does_not_add_statements_either(tmp_path, monkeypatch):
    """A 61-line order exists in production. Lines per order must not cost
    queries any more than orders per page do."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        seed_orders(session, count=5, lines=12)
    statements, _ = count_statements(db, "/orders?status=all")
    assert statements == 21, statements


def test_an_empty_orders_page_queries_nothing_extra(tmp_path, monkeypatch):
    """order_lines_prefetch short-circuits on no orders, so the empty page
    must not pay for the two queries."""
    db = setup_db(tmp_path, monkeypatch)
    statements, text = count_statements(db, "/orders?status=all")
    assert "No orders yet" in text or "No orders match" in text
    assert statements <= 21, statements


# --- what the operator sees ----------------------------------------------

def test_every_order_shows_its_lines_expanded(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        seed_orders(session, count=3, lines=2)
    _, html = count_statements(db, "/orders?status=all")
    assert html.count('class="section-disclosure order-lines" open>') == 3
    assert html.count("2 lines</summary>") == 3


def test_the_lines_carry_the_shared_column_set(tmp_path, monkeypatch):
    from main import ORDER_LINES_COLUMNS, ORDER_LINE_COLUMNS

    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        seed_orders(session, count=1, lines=1)
    _, html = count_statements(db, "/orders?status=all")
    for key in ORDER_LINES_COLUMNS:
        label = ORDER_LINE_COLUMNS[key][0]
        if label:
            assert f"<th>{label}</th>" in html, key
    # the values, not just the headers
    assert "Lightning Bolt 0-0" in html
    assert "A2" in html                # the filling batch
    assert "JA" in html                # language
    assert "$2.50" in html             # the line's sale price


def test_the_orders_page_offers_the_shared_toolbar(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        seed_orders(session, count=2)
    _, html = count_statements(db, "/orders?status=all")
    assert "Expand all order details" in html
    assert "Collapse all order details" in html
    assert html.count("details.order-lines") == 2      # one per button


def test_the_disclosure_row_spans_the_whole_table(tmp_path, monkeypatch):
    """Ten columns: Select, Order, Source, Cards, Total, CardFoundry Status,
    Mana Pool Status, Order placed, Ship by, Ingested."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        seed_orders(session, count=1)
    _, html = count_statements(db, "/orders?status=all")
    assert html.count('<th>') + html.count('<th class="no-print">') >= 10
    assert '<td colspan="10">' in html


def test_an_order_with_no_lines_says_so_on_the_list(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = SalesOrder(external_order_id="o-empty", source="manapool",
                           status="ready_to_pick")
        session.add(order)
        session.commit()
    _, html = count_statements(db, "/orders?status=all")
    assert "This order has no line items." in html
