"""What's Owed: the Amount past due column, and the totals row.

THE OPERATOR, 2026-10-16: "past due and overdue are the same." So this
column asks exactly the question the consignor portal's Overdue marker
already asks, and both go through ONE rule -- _payout_is_overdue -- so
they cannot drift apart and give a consignor two different answers.

THE RULE, unchanged and not re-implemented here: a card's expected payout
date is its sold date (America/New_York) plus 7 days, then forward to the
next Tuesday or Thursday -- and a +7 landing ON a Tuesday or Thursday IS
the date, never skipped past. Overdue means sold, not paid, and today is
past that date.

Every fixture sells cards through a real shipped allocation, because that
is what _sold_at_by_card_id keys on -- a card's sale date comes from the
allocation that actually shipped, not from any allocation it has ever
held.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import (
    Base, Batch, Consignor, InventoryCard, OrderItem, PickAllocation, SalesOrder,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'past-due.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    return engine


def make_consignor(session, name):
    consignor = Consignor(name=name, payout_method="Cash App: @x", is_active=True)
    session.add(consignor)
    session.flush()
    batch = Batch(batch_code=f"CON-{name.replace(' ', '')}", is_consignment=True,
                  consignor_id=consignor.id, is_archived=False)
    session.add(batch)
    session.flush()
    return consignor, batch


def sell_card(session, batch, amount, sold_at, *, paid=False, name="Card"):
    """A consigned card sold through a really-shipped allocation."""
    card = InventoryCard(
        batch_id=batch.id, name=name, status="sold", condition="Near Mint",
        finish="normal", consignment_value=amount, sold_price=amount + 2,
        consignment_amount_owed=amount,
        consignment_payout_status="paid" if paid else "owed",
    )
    session.add(card)
    session.flush()
    order = SalesOrder(
        external_order_id=f"ord-{card.id}", external_label=f"ord-{card.id}",
        source="manapool", status="shipped", shipped_at=sold_at,
    )
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, quantity=1, name=name)
    session.add(item)
    session.flush()
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id,
        batch_id=batch.id, status="shipped",
    ))
    session.flush()
    return card


def owed_table(html):
    return html[html.index('<table class="data-table'):html.index("</table>")]


def row_for(html, name):
    table = owed_table(html)
    start = table.index(f"<td>{name}</td>")
    return table[table.rindex("<tr", 0, start):table.index("</tr>", start)]


def money_in(row):
    return re.findall(r"\$([0-9]+\.[0-9]{2})", row)


def total_row(html):
    table = owed_table(html)
    start = table.index('class="owed-total-row"')
    return table[start:table.index("</tr>", start)]


# A fixed "today" so the boundary tests mean something. Thursday.
TODAY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc).date()


@pytest.fixture
def frozen_today(monkeypatch):
    monkeypatch.setattr(main, "_payout_today_local", lambda: TODAY)
    return TODAY


def days_before_today(days):
    """A UTC sale moment `days` before the frozen today, at midday so the
    America/New_York calendar day is unambiguous."""
    return datetime.combine(TODAY - timedelta(days=days), datetime.min.time()).replace(
        hour=12, tzinfo=timezone.utc,
    ).replace(tzinfo=None)


# --- the column itself ---------------------------------------------------

def test_only_the_overdue_card_is_counted(db, frozen_today):
    """★ THE CASE THE OPERATOR DESCRIBED: one consignor, one card past its
    expected date and one sold yesterday. Only the first is past due, and
    Amount owed still shows both."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        sell_card(session, batch, 30.00, days_before_today(40), name="Old Card")
        sell_card(session, batch, 12.00, days_before_today(1), name="Fresh Card")
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    assert money_in(row) == ["42.00", "30.00"], row


def test_a_paid_card_is_excluded_even_when_long_overdue(db, frozen_today):
    """Paid is settled. It leaves the owed figure too -- the report only
    ever carried owed cards -- so the row must show nothing at all."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        sell_card(session, batch, 30.00, days_before_today(90), paid=True, name="Paid")
        sell_card(session, batch, 5.00, days_before_today(40), name="Unpaid")
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    assert money_in(row) == ["5.00", "5.00"], row


def test_a_consignor_with_nothing_overdue_shows_zero(db, frozen_today):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Sam Roe")
        sell_card(session, batch, 20.00, days_before_today(1))
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Sam Roe")
    assert money_in(row) == ["20.00", "0.00"], row


def test_past_due_never_exceeds_owed(db, frozen_today):
    """Both columns are built from the same per-card amount, so the most
    past due can ever be is all of it."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        for index in range(4):
            sell_card(session, batch, 10.00, days_before_today(60), name=f"C{index}")
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    owed, past_due = (float(x) for x in money_in(row))
    assert past_due == owed == 40.00
    assert past_due <= owed


def test_a_card_whose_sale_cannot_be_resolved_is_not_called_past_due(db, frozen_today):
    """No shipped allocation means no expected date. Counting it would be
    asserting a deadline nobody ever computed."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        card = InventoryCard(
            batch_id=batch.id, name="Orphan", status="sold", condition="Near Mint",
            finish="normal", consignment_value=9.00, sold_price=11.00,
            consignment_amount_owed=9.00, consignment_payout_status="owed",
        )
        session.add(card)
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    assert money_in(row) == ["9.00", "0.00"], row


# --- the Tuesday / Thursday boundary ------------------------------------

@pytest.mark.parametrize("sold_on,expected_date,label", [
    # +7 lands ON a Tuesday -> that IS the expected date, not skipped
    ("2026-09-29", "2026-10-06", "+7 lands on Tuesday"),
    # +7 lands ON a Thursday -> likewise
    ("2026-10-01", "2026-10-08", "+7 lands on Thursday"),
    # +7 lands on a Friday -> forward to the next Tuesday
    ("2026-10-02", "2026-10-13", "+7 lands on Friday"),
    # +7 lands on a Wednesday -> forward to Thursday
    ("2026-09-30", "2026-10-08", "+7 lands on Wednesday"),
])
def test_the_expected_date_rule_is_unchanged(sold_on, expected_date, label):
    """Pinned directly on the rule the column reuses."""
    sold = datetime.fromisoformat(sold_on + "T12:00:00")
    assert main._expected_payout_date(sold).isoformat() == expected_date, label


def test_the_day_the_payout_is_due_is_not_yet_past_due(db, monkeypatch):
    """★ THE BOUNDARY: overdue is today > expected, so ON the expected day
    nothing is past due yet. A card sold Tue 29 Sept is expected Tue 6
    Oct."""
    monkeypatch.setattr(main, "_payout_today_local",
                        lambda: datetime(2026, 10, 6).date())
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        sell_card(session, batch, 25.00, datetime(2026, 9, 29, 12, 0))
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    assert money_in(row) == ["25.00", "0.00"], "due today is not past due"


def test_the_day_after_it_is_due_it_is_past_due(db, monkeypatch):
    monkeypatch.setattr(main, "_payout_today_local",
                        lambda: datetime(2026, 10, 7).date())
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        sell_card(session, batch, 25.00, datetime(2026, 9, 29, 12, 0))
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    assert money_in(row) == ["25.00", "25.00"]


# --- the totals ----------------------------------------------------------

def test_both_totals_equal_the_sum_of_the_rows_shown(db, frozen_today):
    with Session(db) as session:
        _, jane = make_consignor(session, "Jane Doe")
        sell_card(session, jane, 30.00, days_before_today(40))
        sell_card(session, jane, 12.00, days_before_today(1))
        _, sam = make_consignor(session, "Sam Roe")
        sell_card(session, sam, 100.00, days_before_today(60))
        _, avery = make_consignor(session, "Avery Lin")
        sell_card(session, avery, 7.50, days_before_today(2))
        session.commit()

    html = TestClient(main.app).get("/consignors/owed").text
    rows = [row_for(html, name) for name in ("Jane Doe", "Sam Roe", "Avery Lin")]
    owed = [float(money_in(r)[0]) for r in rows]
    past_due = [float(money_in(r)[1]) for r in rows]

    totals = money_in(total_row(html))
    assert float(totals[0]) == round(sum(owed), 2) == 149.50
    assert float(totals[1]) == round(sum(past_due), 2) == 130.00


def test_the_total_row_sits_under_its_own_columns(db, frozen_today):
    with Session(db) as session:
        _, jane = make_consignor(session, "Jane Doe")
        sell_card(session, jane, 30.00, days_before_today(40))
        session.commit()

    html = TestClient(main.app).get("/consignors/owed").text
    headers = [h.strip() for h in re.findall(r"<th>(.*?)</th>", owed_table(html), re.S)]
    assert headers == ["Consignor", "Amount owed", "Amount past due", "", ""]
    cells = re.findall(r"<td>(.*?)</td>", total_row(html), re.S)
    assert [c.strip() for c in cells] == ["Total", "$30.00", "$30.00", "", ""]


def test_totals_are_zero_when_nothing_is_overdue(db, frozen_today):
    with Session(db) as session:
        _, jane = make_consignor(session, "Jane Doe")
        sell_card(session, jane, 30.00, days_before_today(1))
        session.commit()

    totals = money_in(total_row(TestClient(main.app).get("/consignors/owed").text))
    assert totals == ["30.00", "0.00"]


# --- one rule, one answer -----------------------------------------------

def test_the_portal_marker_and_this_column_agree_on_the_same_cards(db, frozen_today):
    """★ THE POINT OF SHARING THE RULE. For one consignor's cards, the
    number of Overdue markers the portal renders must match the number of
    cards this column's figure is built from."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        overdue_cards = [
            sell_card(session, batch, 10.00, days_before_today(40), name="Old A"),
            sell_card(session, batch, 15.00, days_before_today(30), name="Old B"),
        ]
        sell_card(session, batch, 99.00, days_before_today(1), name="Fresh")
        session.commit()
        card_ids = [c.id for c in overdue_cards]

    # what the column says
    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    assert money_in(row) == ["124.00", "25.00"]

    # what the shared rule says, card by card -- the same answer the
    # portal's marker is rendered from
    with Session(db) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        sold_at = main._sold_at_by_card_id(session, [c.id for c in cards])
        flagged = [c.id for c in cards
                   if main._payout_is_overdue(c, sold_at.get(c.id), TODAY)]
    assert flagged == card_ids

    # and the portal really renders a marker for exactly those
    with Session(db) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        sold_at = main._sold_at_by_card_id(session, [c.id for c in cards])
        marked = [
            c.id for c in cards
            if "overdue" in main._portal_payout_date_cells(c, sold_at, {}).lower()
        ]
    assert marked == card_ids, "the portal marker and the column must agree"


def test_the_sold_date_comes_from_the_shipped_allocation_only(db, frozen_today):
    """A card can hold older released/exception rows. Only the shipped one
    is its sale, and only that date may drive past due."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Jane Doe")
        card = sell_card(session, batch, 20.00, days_before_today(1), name="Recent")
        # an ancient exception allocation on the same card
        stale = SalesOrder(
            external_order_id="stale", external_label="stale", source="manapool",
            status="shipped", shipped_at=days_before_today(300),
        )
        session.add(stale)
        session.flush()
        stale_item = OrderItem(order_id=stale.id, quantity=1, name="Recent")
        session.add(stale_item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=stale_item.id, inventory_card_id=card.id,
            batch_id=batch.id, status="exception",
        ))
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Jane Doe")
    assert money_in(row) == ["20.00", "0.00"], (
        "the stale allocation's date must not make this card past due"
    )


def test_the_page_loads_the_sale_dates_in_one_query_not_one_per_card(db, frozen_today):
    with Session(db) as session:
        _, jane = make_consignor(session, "Jane Doe")
        for index in range(12):
            sell_card(session, jane, 5.00, days_before_today(40), name=f"C{index}")
        session.commit()

    from sqlalchemy import event
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "pick_allocations" in statement.lower():
            statements.append(statement)

    event.listen(main.engine, "before_cursor_execute", record)
    try:
        TestClient(main.app).get("/consignors/owed")
    finally:
        event.remove(main.engine, "before_cursor_execute", record)

    assert len(statements) <= 2, f"expected a batched load, got {len(statements)}"
    assert any(" IN (" in s.upper() for s in statements)
