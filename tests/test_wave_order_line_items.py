"""One shared renderer for an order's line items, used by two pages.

The order page built its line table inline; the pick wave's Order details
tab needs the same lines with the operator's own column set (card, set,
condition, language, quantity, the BATCH the card came from, and the line's
sale price). Two inline tables rendering the same concepts is how a card
name, a condition or a price starts being shown two different ways on two
screens -- the failure the shared _card_reference and dated_marker_cell
helpers exist to stop.

★ ONE COLUMN REGISTRY DRIVES BOTH THE HEADER AND THE CELLS, so a page
cannot list a column it does not render or render one it does not list.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from main import (
    ORDER_LINE_COLUMNS,
    ORDER_PAGE_LINE_COLUMNS,
    ORDER_LINES_COLUMNS,
    _order_line_batch_codes,
    _order_line_header,
    _order_line_rows,
)
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    PickWave, PickWaveOrder, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'wave-order-lines.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def seed_order(session, *, batch_codes=("A2",), quantity=1, allocate=True,
               price_cents=250, in_wave=True):
    order = SalesOrder(external_order_id="o-1", external_label="L-1",
                       source="manapool", status="picked")
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id, name="Lightning Bolt", set_code="LEA",
        collector_number="1", finish="normal", scryfall_id="sf-1",
        language_id="JA", condition_id="LP", finish_id="NF",
        quantity=quantity, price_cents=price_cents,
    )
    session.add(item)
    session.flush()
    for code in batch_codes:
        batch = session.query(Batch).filter_by(batch_code=code).one_or_none()
        if not batch:
            batch = Batch(batch_code=code)
            session.add(batch)
            session.flush()
        rec = ImportRecord(batch_id=batch.id, filename="x.csv",
                           file_hash=f"h-{code}", card_count=1)
        session.add(rec)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, import_id=rec.id, name=item.name,
            set_code="LEA", collector_number="1", scryfall_id="sf-1",
            mtgjson_id="mtg-1", language_id="JA", condition_id="LP",
            finish_id="NF", condition="LP", finish="normal", status="reserved",
        )
        session.add(card)
        session.flush()
        if allocate:
            session.add(PickAllocation(
                order_item_id=item.id, inventory_card_id=card.id,
                batch_id=batch.id, status="picked",
            ))
    wave = None
    if in_wave:
        wave = PickWave(label="Wave 1", status="picked")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
    session.commit()
    return order, item, wave


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'lines.db'}")
    Base.metadata.create_all(engine)
    return engine


# --- the registry cannot drift from the headers ---------------------------

def test_every_column_both_pages_ask_for_exists_in_the_registry():
    for key in ORDER_PAGE_LINE_COLUMNS + ORDER_LINES_COLUMNS:
        assert key in ORDER_LINE_COLUMNS, key


def test_the_header_is_derived_from_the_column_list_in_order():
    header = _order_line_header(ORDER_LINES_COLUMNS)
    labels = [ORDER_LINE_COLUMNS[k][0] for k in ORDER_LINES_COLUMNS]
    positions = [header.index(f"<th>{label}</th>") for label in labels]
    assert positions == sorted(positions)
    # and nothing else is in there
    assert header.count("<th>") == len(ORDER_LINES_COLUMNS)


def test_the_operator_asked_for_exactly_these_columns():
    """SAME columns on every order listing (2026-10-09), which is why there
    is one constant rather than one per page."""
    assert ORDER_LINES_COLUMNS == (
        "card", "set", "condition", "language", "quantity", "batch", "amount",
    )


# --- the batch cell, which is the new fact -------------------------------

def test_the_batch_cell_names_the_batch_filling_the_line(db):
    with Session(db) as session:
        order, item, _ = seed_order(session, batch_codes=("A2",))
        assert _order_line_batch_codes(session, [item.id]) == {item.id: "A2"}
        rows, _ = _order_line_rows(session, [item], ("batch",))
        assert "A2" in rows


def test_a_line_filled_from_two_batches_names_both_in_natural_order(db):
    """A line can legitimately be filled from more than one batch (quantity
    > 1, or a substitution pulled a copy from elsewhere), so this joins the
    codes rather than picking one and hiding the rest -- and A2 sorts before
    A10, not after it."""
    with Session(db) as session:
        order, item, _ = seed_order(
            session, batch_codes=("A10", "A2"), quantity=2,
        )
        assert _order_line_batch_codes(session, [item.id]) == {item.id: "A2, A10"}


def test_an_unfilled_line_shows_an_em_dash_not_a_blank(db):
    """An empty cell reads as a rendering bug; "nothing is filling this
    line yet" is a fact worth reading."""
    with Session(db) as session:
        order, item, _ = seed_order(session, allocate=False)
        rows, _ = _order_line_rows(session, [item], ("batch",))
        assert "&mdash;" in rows


def test_the_batch_lookup_is_one_query_for_the_whole_table(db):
    from sqlalchemy import event

    with Session(db) as session:
        order, item, _ = seed_order(session, batch_codes=("A2", "A10"), quantity=2)
        items = session.query(OrderItem).all()
        statements = []
        engine = session.get_bind()

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            _order_line_batch_codes(session, [i.id for i in items])
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert len(statements) == 1, statements


def test_no_items_means_no_query_and_no_rows(db):
    with Session(db) as session:
        assert _order_line_batch_codes(session, []) == {}


# --- the totals come from the renderer -----------------------------------

def test_the_renderer_returns_the_totals_its_own_cells_show(db):
    with Session(db) as session:
        order, item, _ = seed_order(session, quantity=3, batch_codes=("A2",))
        rows, totals = _order_line_rows(session, [item], ORDER_PAGE_LINE_COLUMNS)
        assert totals["requested"] == 3
        assert totals["allocated"] == 1
        assert "<td>\n                    3\n                </td>" in rows


# --- the two pages -------------------------------------------------------

def test_the_order_page_still_renders_its_own_columns(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        order, _, _ = seed_order(session, in_wave=False)
        order_id = order.id
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    for label in ("Card", "Set", "Collector #", "Finish", "Condition",
                  "Requested", "Allocated", "Missing"):
        assert f"<th>{label}</th>" in html, label
    # The order page does NOT grow the wave's columns.
    assert "<th>Batch</th>" not in html
    assert "<th>Qty</th>" not in html


def test_the_wave_order_details_tab_shows_each_orders_lines(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        order, item, wave = seed_order(session, batch_codes=("A2",))
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=orders").text
    assert "1 line</summary>" in html
    for label in ("Card", "Set", "Condition", "Language", "Qty", "Batch"):
        assert f"<th>{label}</th>" in html, label
    assert "A2" in html                      # the batch filling it
    assert "Lightning Bolt" in html
    assert "JA" in html                      # language
    assert "$2.50" in html                   # the line's sale price


def test_the_lines_are_not_on_the_picklist_tab(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        order, item, wave = seed_order(session)
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "1 line</summary>" not in html


def test_an_order_with_no_lines_says_so_rather_than_rendering_an_empty_table(
    tmp_path, monkeypatch,
):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        order = SalesOrder(external_order_id="o-empty", source="manapool",
                           status="picked")
        session.add(order)
        session.flush()
        wave = PickWave(label="Wave 1", status="picked")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        session.commit()
        wave_id = wave.id
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=orders").text
    assert "This order has no line items." in html


# --- the shared component (2026-10-09) -----------------------------------
#
# ★ ONE COMPONENT FOR EVERY ORDER LISTING. It was
# _wave_order_lines_disclosure, built for one tab; four near-copies is how
# one page starts showing a condition the others do not.

def test_the_disclosure_is_open_by_default(db):
    from main import order_lines_disclosure, order_lines_prefetch

    with Session(db) as session:
        order, item, _ = seed_order(session, batch_codes=("A2",))
        items, codes = order_lines_prefetch(session, [order])
        html = order_lines_disclosure(order, items[order.id], batch_codes=codes)
    assert "<details class=\"section-disclosure order-lines\" open>" in html
    assert "1 line</summary>" in html


def test_the_disclosure_can_be_asked_to_start_closed(db):
    """`open` is a parameter only so a caller with a genuinely different
    need can say so; nothing passes False today."""
    from main import order_lines_disclosure, order_lines_prefetch

    with Session(db) as session:
        order, item, _ = seed_order(session)
        items, codes = order_lines_prefetch(session, [order])
        html = order_lines_disclosure(
            order, items[order.id], batch_codes=codes, open=False,
        )
    assert 'class="section-disclosure order-lines">' in html


def test_an_order_with_no_lines_says_so(db):
    from main import order_lines_disclosure

    with Session(db) as session:
        order, _, _ = seed_order(session)
        assert "no line items" in order_lines_disclosure(order, [])


def test_the_prefetch_takes_two_queries_for_any_number_of_orders(db):
    """★ THE N+1 GUARD. Two queries whatever the page size -- one for every
    line, one for every line's filling batch."""
    from sqlalchemy import event

    from main import order_lines_prefetch

    with Session(db) as session:
        orders = []
        for n in range(6):
            order, _, _ = seed_order(session, batch_codes=("A2",))
            order.external_order_id = f"o-{n}"
            orders.append(order)
        session.commit()

        # Touch the ids first: session.commit() above EXPIRED these
        # instances, so reading order.id would lazy-reload each one and
        # count as a query here. A route never does that -- its orders come
        # straight from a live query with nothing committed in between -- so
        # counting the reloads would measure the test, not the code.
        order_ids = [order.id for order in orders]
        assert len(order_ids) == 6

        statements = []
        engine = session.get_bind()

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            items, codes = order_lines_prefetch(session, orders)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert len(statements) == 2, statements
        assert len(items) == 6
        assert all(codes[i.id] == "A2" for its in items.values() for i in its)


def test_the_prefetch_queries_nothing_for_no_orders(db):
    from main import order_lines_prefetch

    with Session(db) as session:
        assert order_lines_prefetch(session, []) == ({}, {})


def test_rendering_with_prefetched_batch_codes_issues_no_query(db):
    """Given the codes, the renderer must not go back to the database --
    that is the whole point of the seam."""
    from sqlalchemy import event

    from main import order_lines_disclosure, order_lines_prefetch

    with Session(db) as session:
        order, _, _ = seed_order(session, batch_codes=("A2",))
        items, codes = order_lines_prefetch(session, [order])
        statements = []
        engine = session.get_bind()

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            order_lines_disclosure(
                order, items[order.id], batch_codes=codes,
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert statements == [], statements


def test_the_toolbar_targets_the_disclosure_class_and_adds_no_other_script():
    from main import ORDER_LINES_DISCLOSURE_CLASS, order_lines_toolbar

    html = order_lines_toolbar()
    assert f"details.{ORDER_LINES_DISCLOSURE_CLASS}" in html
    assert "d.open = true" in html
    assert "d.open = false" in html
    assert "Expand all order details" in html
    assert "Collapse all order details" in html
    # Same mechanism as "Expand all batches": inline onclick, no <script>.
    assert "<script" not in html
    assert html.count("onclick=") == 2


def test_the_wave_tab_offers_the_toolbar(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        order, item, wave = seed_order(session, batch_codes=("A2",))
        wave_id = wave.id
    client = TestClient(main.app)
    orders_tab = client.get(f"/pick-waves/{wave_id}?tab=orders").text
    assert "Expand all order details" in orders_tab
    assert "Collapse all order details" in orders_tab
    # ...and the lines are open without touching anything.
    assert 'class="section-disclosure order-lines" open>' in orders_tab
    # The picklist tab has its own batch toolbar and not this one.
    picklist = client.get(f"/pick-waves/{wave_id}").text
    assert "Expand all order details" not in picklist
