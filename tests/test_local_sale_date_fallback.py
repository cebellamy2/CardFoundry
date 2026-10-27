"""The overdue rule uses a card's real local-sale date when it has no
shipped Mana Pool order.

THE OPERATOR, 2026-10-17: cards he sold across the counter were being
treated as undated, so either they counted as nothing at all or -- if
they happened to be legacy imports -- they were swept in with the records
that genuinely have no date. They do have a date: the moment he recorded
the sale.

ORDER OF SOURCES, and the tests below pin each step:
  1. the shipped allocation's order            (unchanged)
  2. the card's own date, if it left as a SALE (new here)
  3. the legacy sheet-import stamp             (unchanged, v2.56.0)
  4. otherwise unassessable                    (unchanged)

★ IT MUST BE A SALE, not merely a disposition. A gift is not a sale, and
neither is a disposition recorded with no value received -- using either
date as a payout deadline would invent a debt. Both are tested.

Costs NO extra query: the date is a column on the card the page has
already loaded.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import consignment_service
import main
from consignment_service import LEGACY_SHEET_IMPORT_SOURCE
from models import (
    Base, Batch, Consignor, InventoryCard, OrderItem, PickAllocation, SalesOrder,
)

# A fixed "today" (a Thursday) so the deadline cases mean something.
TODAY = datetime(2026, 10, 15).date()


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'localsale.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(consignment_service, "payout_today_local", lambda: TODAY)
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


def days_before_today(days):
    return datetime.combine(TODAY - timedelta(days=days), datetime.min.time()).replace(hour=12)


def add_card(session, batch, amount, *, name="Card", disposed_at=None,
             disposition_type="local_sale", sold_price="auto", legacy=False,
             shipped_at=None):
    card = InventoryCard(
        batch_id=batch.id, name=name, status="sold", condition="Near Mint",
        finish="normal", consignment_value=amount,
        sold_price=((amount or 0) + 2) if sold_price == "auto" else sold_price,
        consignment_amount_owed=amount, consignment_payout_status="owed",
        disposed_at=disposed_at,
        disposition_type=disposition_type if disposed_at else None,
        consignment_import_source=LEGACY_SHEET_IMPORT_SOURCE if legacy else None,
    )
    session.add(card)
    session.flush()
    if shipped_at is not None:
        order = SalesOrder(external_order_id=f"o{card.id}", external_label=f"o{card.id}",
                           source="manapool", status="shipped", shipped_at=shipped_at)
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, quantity=1, name=name)
        session.add(item)
        session.flush()
        session.add(PickAllocation(order_item_id=item.id, inventory_card_id=card.id,
                                   batch_id=batch.id, status="shipped"))
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


def portal_cells(card):
    return main._portal_payout_date_cells(card, {}, {})


# --- a local cash sale past its deadline --------------------------------

def test_a_local_sale_past_its_deadline_is_past_due_on_whats_owed(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Patrick")
        add_card(session, batch, 14.89, name="Sliver Hivelord",
                 disposed_at=days_before_today(40))
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Patrick")
    assert money_in(row) == ["14.89", "14.89"], row


def test_the_same_card_is_overdue_on_the_portal_too(db):
    """One rule, one answer -- and the portal now shows the REAL date
    where it used to say "unknown"."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Patrick")
        card = add_card(session, batch, 14.89, disposed_at=days_before_today(40))
        session.commit()
        cells = portal_cells(card)

    assert "overdue" in cells.lower()
    assert "unknown" not in cells, "the real sale date must replace unknown"


def test_a_local_sale_not_yet_due_is_not_past_due(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Patrick")
        card = add_card(session, batch, 20.00, disposed_at=days_before_today(1))
        session.commit()
        cells = portal_cells(card)

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Patrick")
    assert money_in(row) == ["20.00", "0.00"], row
    assert "overdue" not in cells.lower()
    assert "unknown" not in cells, "it still shows the real date"


def test_the_tuesday_thursday_rule_is_applied_to_that_date(db, monkeypatch):
    """Sold Tue 29 Sept -> +7 lands on Tue 6 Oct, which IS the expected
    date. Due that day is not yet past due; the next day it is."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Patrick")
        add_card(session, batch, 10.00, disposed_at=datetime(2026, 9, 29, 12, 0))
        session.commit()

    monkeypatch.setattr(consignment_service, "payout_today_local", lambda: datetime(2026, 10, 6).date())
    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Patrick")
    assert money_in(row) == ["10.00", "0.00"], "due today is not past due"

    monkeypatch.setattr(consignment_service, "payout_today_local", lambda: datetime(2026, 10, 7).date())
    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Patrick")
    assert money_in(row) == ["10.00", "10.00"]


# --- the order of sources -----------------------------------------------

def test_a_shipped_order_still_wins_over_the_card_date(db):
    """★ SOURCE ORDER. The order is the real sale; a stale disposal date
    on the same card must not override it."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Patrick")
        card = add_card(session, batch, 25.00,
                        shipped_at=days_before_today(1),     # recent: not due
                        disposed_at=days_before_today(300))  # ancient
        session.commit()
        card_id = card.id

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Patrick")
    assert money_in(row) == ["25.00", "0.00"], (
        "the shipped order's recent date must win over the old card date"
    )
    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert main._payout_is_overdue(card, days_before_today(1)) is False


def test_a_legacy_card_with_a_real_date_uses_the_date_not_the_stamp(db):
    """★ The stamp is the LAST resort. A legacy card that was actually
    sold across the counter yesterday is not overdue."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, 30.00, legacy=True,
                        disposed_at=days_before_today(1))
        session.commit()
        cells = portal_cells(card)

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Ransom")
    assert money_in(row) == ["30.00", "0.00"], (
        "a real date must beat the legacy stamp"
    )
    assert "overdue" not in cells.lower()
    assert "unknown" not in cells


def test_a_legacy_card_with_no_date_at_all_is_still_overdue(db):
    """v2.56.0's behaviour, unchanged."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, 30.00, legacy=True)
        session.commit()
        cells = portal_cells(card)

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Ransom")
    assert money_in(row) == ["30.00", "30.00"]
    assert "overdue" in cells.lower()
    assert "unknown" in cells, "the date is still honestly unknown"


def test_a_plain_card_with_no_date_is_still_unassessable(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Someone")
        card = add_card(session, batch, 30.00)
        session.commit()
        cells = portal_cells(card)

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Someone")
    assert money_in(row) == ["30.00", "0.00"]
    assert "overdue" not in cells.lower()


# --- it must be a SALE ---------------------------------------------------

def test_a_gift_is_not_used_as_a_sale_date(db):
    """★ A gift moves a card without selling it. Using its date as a
    payout deadline would invent a debt."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Someone")
        card = add_card(session, batch, 30.00, disposed_at=days_before_today(90),
                        disposition_type="gift")
        session.commit()
        cells = portal_cells(card)

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Someone")
    assert money_in(row) == ["30.00", "0.00"], "a gift date is not a sale date"
    assert "overdue" not in cells.lower()


def test_a_disposition_with_no_value_recorded_is_not_a_sale(db):
    """The label alone is not enough -- nothing was received."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Someone")
        card = add_card(session, batch, 30.00, disposed_at=days_before_today(90),
                        disposition_type="other", sold_price=None)
        session.commit()
        card_id = card.id

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Someone")
    assert money_in(row) == ["30.00", "0.00"]
    with Session(db) as session:
        assert main._local_sale_date(session.get(InventoryCard, card_id)) is None


@pytest.mark.parametrize("kind", ["local_sale", "trade", "other"])
def test_every_disposal_for_value_counts_as_a_sale(db, kind):
    """The operator's own 5 cards are a mix of local_sale and other, and
    a trade receives value too."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Someone")
        add_card(session, batch, 12.00, disposed_at=days_before_today(40),
                 disposition_type=kind)
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Someone")
    assert money_in(row) == ["12.00", "12.00"], kind


def test_a_paid_card_is_never_past_due(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Someone")
        card = add_card(session, batch, 12.00, disposed_at=days_before_today(90))
        card.consignment_payout_status = "paid"
        session.commit()
        card_id = card.id
    with Session(db) as session:
        assert main._payout_is_overdue(session.get(InventoryCard, card_id), None) is False


# --- the whole page still adds up ---------------------------------------

def test_the_totals_still_add_up(db):
    with Session(db) as session:
        _, a = make_consignor(session, "Patrick")
        add_card(session, a, 14.89, disposed_at=days_before_today(40))
        add_card(session, a, 3.21, disposed_at=days_before_today(1))
        _, b = make_consignor(session, "Ransom")
        add_card(session, b, 8.80, disposed_at=days_before_today(60))
        session.commit()

    html = TestClient(main.app).get("/consignors/owed").text
    owed = [float(money_in(row_for(html, n))[0]) for n in ("Patrick", "Ransom")]
    past = [float(money_in(row_for(html, n))[1]) for n in ("Patrick", "Ransom")]
    totals = money_in(total_row(html))
    assert float(totals[0]) == round(sum(owed), 2) == 26.90
    assert float(totals[1]) == round(sum(past), 2) == 23.69
    assert float(totals[1]) <= float(totals[0])


def test_the_card_date_costs_no_extra_query(db):
    """It is a column on the card the page already loaded."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Patrick")
        for i in range(12):
            add_card(session, batch, 5.00, name=f"C{i}", disposed_at=days_before_today(40))
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
