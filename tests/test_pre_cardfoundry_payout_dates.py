"""Two display gaps on the Pre-CardFoundry rows, one release.

OPERATOR DECISIONS, 2026-10-21. v2.64.0 gathered the sold-but-undated
legacy rows into one "Sold Pre-CardFoundry" group and left every date
cell inside it blank, because no sale date was ever recorded for them.
That was right about the SALE date and wrong about the PAID date:

  1. A grouped row that has been paid now shows its payout's real
     actual-paid date. That date is a fact about the payout record, not
     about the missing sale, and it is as true here as on any other row.
     A payout with genuinely no date leaves the cell blank -- never a
     derived or invented one, and never the word "unknown" (the group's
     heading has already said these records are incomplete).

  2. The payout DETAIL page showed a lone "unknown" in its Sold column
     for these same cards -- a word with no room to explain itself. It
     now reads "Pre-CardFoundry", the operator's own wording, decided by
     the SAME _is_sold_pre_cardfoundry the grouping uses, so the two
     pages cannot disagree about which rows those are.

Display only. No amount, total or stored value moves; asserted below in
both directions on every surface.

A SIDE EFFECT WORTH ASSERTING: the payout detail page asked only the
shipped allocation for its Sold column, so a counter sale -- which
carries its own disposal date, and which the consignor pages have shown
since v2.57.0 -- read as "unknown" there alone. Sharing the rule fixes
that too, and the test for it is below.

FOUR PAGES render these cells:
  /portal/                              the consignor's own portal
  /consignors/{id}/edit  Inventory      the operator's own table
  /consignors/{id}/edit  Portal Preview the mirror of the portal
  /consignors/payouts/{id} + /portal/payouts/{id}   one shared renderer
"""
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from consignment_service import LEGACY_SHEET_IMPORT_SOURCE, record_consignor_payout
from consignor_auth_service import set_consignor_portal_credentials
from models import (
    Base, Batch, Consignor, InventoryCard, OrderItem, PickAllocation, SalesOrder,
)

# A fixed "today" (a Thursday), so "overdue" means something exact.
TODAY = datetime(2026, 10, 15).date()
HEADING = "Sold Pre-CardFoundry"
CELL = "Pre-CardFoundry"
# The date the payouts below are recorded on, and how _format_date prints
# it. Written out rather than derived, so a change to either is visible.
PAID_AT = datetime(2026, 10, 2, 14, 30)
PAID_TEXT = "Oct 2, 2026"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'precf-dates.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "_payout_today_local", lambda: TODAY)
    return engine


def make_consignor(session, name="Ransom", *, username="jane@example.com",
                   password="secretpw"):
    consignor = Consignor(name=name, payout_method="Cash App: @x", is_active=True)
    session.add(consignor)
    session.flush()
    if username:
        set_consignor_portal_credentials(session, consignor.id, username, password)
    batch = Batch(batch_code=f"CON-{name.replace(' ', '')}", is_consignment=True,
                  consignor_id=consignor.id, is_archived=False)
    session.add(batch)
    session.flush()
    return consignor, batch


def days_before_today(days):
    return datetime.combine(TODAY - timedelta(days=days), datetime.min.time()).replace(hour=12)


def add_card(session, batch, *, name, owed=10.0, status="sold", legacy=True,
             shipped_at=None, disposed_at=None, disposition_type="local_sale",
             set_code="ONE", collector_number="1"):
    """A sold, OWED card with no resolvable sale date by default -- the
    shape the group is made of. Pass shipped_at for the chain
    mark_shipped() leaves behind, or disposed_at for a counter sale.

    Payouts are never hand-built here: pay_for() runs the real
    record_consignor_payout, which is what sets payout_status/payout_id
    in production and what refuses a card that is not owed.
    """
    card = InventoryCard(
        batch_id=batch.id, name=name, status=status, condition="Near Mint",
        finish="normal", consignment_value=owed, sold_price=owed + 2,
        set_code=set_code, collector_number=collector_number,
        condition_id="NM", finish_id="NF", language_id="EN",
        consignment_amount_owed=owed if status == "sold" else None,
        consignment_payout_status="owed" if status == "sold" else None,
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
        item = OrderItem(order_id=order.id, quantity=1, name=name, price_cents=1000)
        session.add(item)
        session.flush()
        session.add(PickAllocation(order_item_id=item.id, inventory_card_id=card.id,
                                   batch_id=batch.id, status="shipped"))
        session.flush()
    return card


def pay_for(session, consignor_id, cards, *, paid_at=PAID_AT, method="Cash App",
            note="Paid in full"):
    """Through the real service, not a hand-built row."""
    payout = record_consignor_payout(
        session, consignor_id, [card.id for card in cards],
        method, note, paid_at,
    )
    session.flush()
    return payout


# --- driving the real pages ----------------------------------------------

def operator_page(consignor_id):
    response = TestClient(main.app).get(f"/consignors/{consignor_id}/edit")
    assert response.status_code == 200
    return response.text


def portal_page(username="jane@example.com", password="secretpw"):
    client = TestClient(main.app)
    login = client.post("/portal/login", data={"username": username, "password": password},
                        follow_redirects=False)
    assert login.status_code == 303, login.text
    response = client.get("/portal/")
    assert response.status_code == 200
    return response.text


def operator_payout_page(payout_id):
    response = TestClient(main.app).get(f"/consignors/payouts/{payout_id}")
    assert response.status_code == 200
    return response.text


def portal_payout_page(payout_id, username="jane@example.com", password="secretpw"):
    client = TestClient(main.app)
    login = client.post("/portal/login", data={"username": username, "password": password},
                        follow_redirects=False)
    assert login.status_code == 303, login.text
    response = client.get(f"/portal/payouts/{payout_id}")
    assert response.status_code == 200
    return response.text


def table_after(html, marker):
    start = html.index(marker)
    table_start = html.index("<table", start)
    return html[table_start:html.index("</table>", table_start)]


def portal_table(html):
    """The portal's card table has no heading above it, so slice the table
    that CONTAINS the header row."""
    start = html.index("<th>Card</th>")
    table_start = html.rindex("<table", 0, start)
    return html[table_start:html.index("</table>", table_start)]


def all_three_card_tables(consignor_id, username="jane@example.com"):
    operator = operator_page(consignor_id)
    return [
        ("operator Inventory", table_after(operator, "<h2>Inventory</h2>")),
        ("operator Portal Preview", table_after(operator, "Sees In Their Portal")),
        ("consignor portal", portal_table(portal_page(username))),
    ]


def payout_cards_table(html):
    """The payout detail page's "Cards paid in this payout" table.

    Sliced BACKWARDS from its last column header: "Paid for this card" is
    inside the table, not above it, so table_after would run past it.
    """
    start = html.index("Paid for this card")
    return html[html.rindex("<table", 0, start):html.index("</table>", start)]


def both_payout_tables(payout_id, username="jane@example.com"):
    return [
        ("operator payout detail",
         payout_cards_table(operator_payout_page(payout_id))),
        ("portal payout detail",
         payout_cards_table(portal_payout_page(payout_id, username))),
    ]


def name_at(table, name):
    return table.index(f"<td>{name} <")


def row_for(table, name):
    start = name_at(table, name)
    return table[table.rindex("<tr", 0, start):table.index("</tr>", start)]


def payout_row_for(table, name):
    """The payout detail table's name cell carries no colour badge for a
    colourless test card, so it is "<td>Name </td>" there too -- but the
    first cell is matched on the name alone to stay independent of that."""
    start = table.index(name)
    return table[table.rindex("<tr", 0, start):table.index("</tr>", start)]


def cells_of(row):
    return re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)


def money_in(fragment):
    return re.findall(r"\$([0-9]+\.[0-9]{2})", fragment)


def all_money_on(html):
    return re.findall(r"\$([0-9]+\.[0-9]{2})", html)


def group_summary(table):
    if HEADING not in table:
        return None
    start = table.index(HEADING)
    return table[table.rindex("<tr", 0, start):table.index("</tr>", start)]


# === 1. the actual-paid date inside the group ============================

def test_a_paid_grouped_row_shows_its_payouts_real_actual_paid_date(db):
    """The headline change. Same row, same group, same empty sale-date
    cells -- the Actual paid column is the one that fills in."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        paid = add_card(session, batch, name="Undated Paid", owed=12.00)
        add_card(session, batch, name="Undated Owed", owed=5.00)
        pay_for(session, consignor.id, [paid])
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_card_tables(consignor_id):
        assert group_summary(table) is not None, f"{label}: no group rendered"
        row = cells_of(row_for(table, "Undated Paid"))
        assert len(row) == 8, f"{label}: {row}"
        # Card, status, value, sold price, owed, Sold, Expected, Actual paid.
        assert row[5].strip() == "", f"{label}: a sold date appeared: {row}"
        assert row[6].strip() == "", f"{label}: an expected date appeared: {row}"
        assert PAID_TEXT in row[7], f"{label}: actual paid cell was {row[7]!r}"


def test_an_owed_grouped_row_still_has_three_empty_date_cells(db):
    """Nothing to show, nothing shown -- and no "unknown" either."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        add_card(session, batch, name="Undated Owed", owed=5.00)
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_card_tables(consignor_id):
        row = cells_of(row_for(table, "Undated Owed"))
        assert row[5].strip() == "", f"{label}: {row}"
        assert row[7].strip() == "", f"{label}: {row}"
        # The Overdue marker is in the Expected cell and must survive.
        assert "overdue" in row[6].lower(), f"{label}: {row}"
        assert "unknown" not in row_for(table, "Undated Owed").lower(), f"{label}: {row}"


def test_a_grouped_row_whose_payout_has_no_date_stays_blank_not_unknown(db):
    """_paid_at_by_payout_id drops an id it could not resolve. The cell
    says nothing rather than guessing, and does not say "unknown": the
    group's heading has already said these records are incomplete."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        paid = add_card(session, batch, name="Undated Paid", owed=12.00)
        pay_for(session, consignor.id, [paid])
        session.commit()
        consignor_id = consignor.id

    # The one shape that produces it: the lookup returns nothing for the id.
    real_lookup = main._paid_at_by_payout_id
    main._paid_at_by_payout_id = lambda session, payout_ids: {}
    try:
        tables = all_three_card_tables(consignor_id)
    finally:
        main._paid_at_by_payout_id = real_lookup

    for label, table in tables:
        row = cells_of(row_for(table, "Undated Paid"))
        assert row[7].strip() == "", f"{label}: {row}"
        assert "unknown" not in row_for(table, "Undated Paid").lower(), f"{label}: {row}"


def test_no_sale_date_is_invented_on_a_paid_grouped_row(db):
    """The paid date is the ONLY date that may appear on these rows. The
    payout date is deliberately 13 days after the fixed today, so a sale
    date derived from it could not hide inside the paid cell's own text."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        paid = add_card(session, batch, name="Undated Paid", owed=12.00)
        pay_for(session, consignor.id, [paid])
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_card_tables(consignor_id):
        row = cells_of(row_for(table, "Undated Paid"))
        pattern = r"\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2} \d{1,2}, \d{4}"
        assert not re.search(pattern, row[5]), f"{label}: {row[5]!r}"
        assert not re.search(pattern, row[6]), f"{label}: {row[6]!r}"
        assert re.findall(pattern, row[7]) == [PAID_TEXT], f"{label}: {row[7]!r}"


def test_a_dated_paid_row_outside_the_group_is_unchanged(db):
    """The ordinary branch now asks the same helper; it must still print
    all three dates, and still say "unknown" when the payout date does
    not resolve -- that wording was only dropped inside the group."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        dated = add_card(session, batch, name="Shipped Paid", owed=20.00,
                         shipped_at=days_before_today(40), legacy=False)
        pay_for(session, consignor.id, [dated])
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_card_tables(consignor_id):
        row = cells_of(row_for(table, "Shipped Paid"))
        assert "Sep 5, 2026" in row[5], f"{label}: {row[5]!r}"       # 40 days back
        assert "Sep 15, 2026" in row[6], f"{label}: {row[6]!r}"      # +7, next Tue/Thu
        assert PAID_TEXT in row[7], f"{label}: {row[7]!r}"

    real_lookup = main._paid_at_by_payout_id
    main._paid_at_by_payout_id = lambda session, payout_ids: {}
    try:
        for label, table in all_three_card_tables(consignor_id):
            row = cells_of(row_for(table, "Shipped Paid"))
            assert "unknown" in row[7].lower(), f"{label}: {row[7]!r}"
    finally:
        main._paid_at_by_payout_id = real_lookup


def test_the_group_heading_and_every_money_figure_are_untouched(db):
    """Display only: the heading's count and owed amount, and every dollar
    figure on all three pages, are the same as before this change. The
    paid card contributes nothing owed, exactly as in v2.64.0."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        paid = add_card(session, batch, name="Undated Paid", owed=99.00)
        add_card(session, batch, name="Undated A", owed=5.25)
        add_card(session, batch, name="Undated B", owed=10.50)
        pay_for(session, consignor.id, [paid])
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_card_tables(consignor_id):
        summary = group_summary(table)
        assert "3 card(s)" in summary, f"{label}: {summary}"
        assert money_in(summary) == ["15.75"], f"{label}: {summary}"
        # Per-card money on the paid row: value, sold price, owed.
        assert money_in(row_for(table, "Undated Paid")) == ["99.00", "101.00", "99.00"], label

    operator = operator_page(consignor_id)
    assert re.findall(r"owed: <strong>\$([0-9]+\.[0-9]{2})</strong>", operator) == ["15.75"]
    assert re.findall(
        r"<dt>Currently owed</dt><dd><strong>\$([0-9]+\.[0-9]{2})</strong>", operator,
    ) == ["15.75"]
    assert re.findall(
        r"owed: <strong>\$([0-9]+\.[0-9]{2})</strong>", portal_page(),
    ) == ["15.75"]


# === 2. "Pre-CardFoundry" on the payout detail page ======================

def test_the_payout_detail_sold_cell_reads_pre_cardfoundry(db):
    """The lone "unknown" is gone, on both surfaces of the one renderer."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        undated = add_card(session, batch, name="Undated Card", owed=12.00)
        payout = pay_for(session, consignor.id, [undated])
        session.commit()
        payout_id = payout.id

    for label, table in both_payout_tables(payout_id):
        row = cells_of(payout_row_for(table, "Undated Card"))
        assert len(row) == 6, f"{label}: {row}"
        # Card, Set, Condition/Finish/Language, Sold, Sale price, Paid.
        assert CELL in row[3], f"{label}: sold cell was {row[3]!r}"
        assert "unknown" not in row[3].lower(), f"{label}: {row[3]!r}"


def test_no_date_is_invented_in_the_pre_cardfoundry_sold_cell(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        undated = add_card(session, batch, name="Undated Card", owed=12.00)
        payout = pay_for(session, consignor.id, [undated])
        session.commit()
        payout_id = payout.id

    for label, table in both_payout_tables(payout_id):
        cell = cells_of(payout_row_for(table, "Undated Card"))[3]
        assert not re.search(r"\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2} \d{1,2}, \d{4}", cell), \
            f"{label}: {cell!r}"


def test_a_card_with_a_real_sale_date_is_unchanged_on_the_payout_detail(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        dated = add_card(session, batch, name="Shipped Card", owed=20.00,
                         shipped_at=days_before_today(40), legacy=False)
        payout = pay_for(session, consignor.id, [dated])
        session.commit()
        payout_id = payout.id

    for label, table in both_payout_tables(payout_id):
        cell = cells_of(payout_row_for(table, "Shipped Card"))[3]
        assert "Sep 5, 2026" in cell, f"{label}: {cell!r}"
        assert CELL not in cell, f"{label}: {cell!r}"


def test_a_counter_sale_now_shows_its_own_date_instead_of_unknown(db):
    """The side effect of sharing the rule. This page asked only the
    shipped allocation, so a card sold across the counter -- which the
    consignor pages have dated since v2.57.0 -- read "unknown" here
    alone. It is not a Pre-CardFoundry row and must not be labelled one."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        counter = add_card(session, batch, name="Counter Sale", owed=8.00,
                           disposed_at=days_before_today(30), legacy=False)
        payout = pay_for(session, consignor.id, [counter])
        session.commit()
        payout_id = payout.id

    for label, table in both_payout_tables(payout_id):
        cell = cells_of(payout_row_for(table, "Counter Sale"))[3]
        assert "Sep 15, 2026" in cell, f"{label}: {cell!r}"   # 30 days back
        assert CELL not in cell, f"{label}: {cell!r}"
        assert "unknown" not in cell.lower(), f"{label}: {cell!r}"


def test_the_two_pages_agree_about_which_rows_are_pre_cardfoundry(db):
    """★ The reason the rule is shared. Every card named Pre-CardFoundry
    on the payout detail is in the group on the consignor's page, and
    every card outside the group is dated on both."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        undated = add_card(session, batch, name="Undated Card", owed=12.00)
        # Undated and NOT legacy-stamped: membership is the data, not the
        # stamp, so this one belongs in the group too.
        no_stamp = add_card(session, batch, name="Undated No Stamp", owed=3.00,
                            legacy=False)
        shipped = add_card(session, batch, name="Shipped Card", owed=20.00,
                           shipped_at=days_before_today(40), legacy=False)
        counter = add_card(session, batch, name="Counter Sale", owed=8.00,
                           disposed_at=days_before_today(30), legacy=False)
        payout = pay_for(session, consignor.id,
                         [undated, no_stamp, shipped, counter])
        session.commit()
        consignor_id, payout_id = consignor.id, payout.id

    grouped_names = {"Undated Card", "Undated No Stamp"}
    dated_names = {"Shipped Card", "Counter Sale"}

    for label, table in all_three_card_tables(consignor_id):
        heading_at = table.index(HEADING)
        for name in grouped_names:
            assert name_at(table, name) > heading_at, f"{label}: {name} not grouped"
        for name in dated_names:
            assert name_at(table, name) < heading_at, f"{label}: {name} grouped"

    for label, table in both_payout_tables(payout_id):
        for name in grouped_names:
            cell = cells_of(payout_row_for(table, name))[3]
            assert CELL in cell, f"{label}: {name} sold cell was {cell!r}"
        for name in dated_names:
            cell = cells_of(payout_row_for(table, name))[3]
            assert CELL not in cell, f"{label}: {name} sold cell was {cell!r}"


def test_a_card_not_sold_yet_still_reads_unknown_never_pre_cardfoundry(db):
    """The unclassified bucket. A payout linked to a card that is not
    sold should not happen, but if it does the page must not assert the
    card sold before CardFoundry existed -- it says it does not know."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        sold = add_card(session, batch, name="Undated Card", owed=12.00)
        payout = pay_for(session, consignor.id, [sold])
        # Through the real service first, then the one state it refuses
        # to create: the card is walked back to available afterwards.
        available = add_card(session, batch, name="Still Listed",
                             status="available", legacy=False)
        available.consignment_payout_id = payout.id
        session.commit()
        payout_id = payout.id

    for label, table in both_payout_tables(payout_id):
        cell = cells_of(payout_row_for(table, "Still Listed"))[3]
        assert "unknown" in cell.lower(), f"{label}: {cell!r}"
        assert CELL not in cell, f"{label}: {cell!r}"


def test_the_payout_detail_money_and_reconciliation_are_untouched(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        undated = add_card(session, batch, name="Undated Card", owed=12.00)
        shipped = add_card(session, batch, name="Shipped Card", owed=20.00,
                           shipped_at=days_before_today(40), legacy=False)
        payout = pay_for(session, consignor.id, [undated, shipped])
        session.commit()
        payout_id = payout.id

    for label, html in (("operator", operator_payout_page(payout_id)),
                        ("portal", portal_payout_page(payout_id))):
        assert "matches the payout amount" in html, label
        assert "do not reconcile" not in html, label
        table = payout_cards_table(html)
        assert money_in(payout_row_for(table, "Undated Card")) == ["14.00", "12.00"], label
        assert money_in(payout_row_for(table, "Shipped Card")) == ["22.00", "20.00"], label
        # The payout's own amount, stated twice on the page.
        assert all_money_on(html).count("32.00") >= 2, label


def test_the_two_payout_detail_routes_still_render_identical_content(db):
    """v2.56.0's shared-renderer property, re-asserted over the cell this
    release changed: the operator and the consignor see the same string."""
    with Session(db) as session:
        consignor, batch = make_consignor(session)
        undated = add_card(session, batch, name="Undated Card", owed=12.00)
        shipped = add_card(session, batch, name="Shipped Card", owed=20.00,
                           shipped_at=days_before_today(40), legacy=False)
        payout = pay_for(session, consignor.id, [undated, shipped])
        session.commit()
        payout_id = payout.id

    def shared_region(body):
        start = body.index("<h2>Payout</h2>")
        return body[start:body.index("</table>", body.index("Paid for this card"))]

    operator = shared_region(operator_payout_page(payout_id))
    portal = shared_region(portal_payout_page(payout_id))
    assert operator == portal
    assert CELL in operator


# === cost: both pages pinned by GROWTH ===================================

def _count_selects(engine, render):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        html = render()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements), html


def test_the_actual_paid_date_adds_no_queries_as_the_group_grows(db):
    """Pinned by GROWTH, not a ceiling: tripling the number of PAID
    grouped rows must not add a single query. The paid date comes out of
    the batched _paid_at_by_payout_id the page already built for the
    ungrouped rows, so there is no per-row lookup behind this cell.

    ONE PAYOUT COVERS THEM ALL, deliberately. Giving each card its own
    payout measures something else: consignor_payout_history runs one
    query per payout to list that payout's cards, so the operator page's
    Payout History table grows 1:1 with the number of PAYOUTS. That N+1
    is real and pre-existing -- measured here at 10 payouts -> 32 selects
    and 30 payouts -> 52 -- and this release neither adds to it nor fixes
    it. It is noted rather than left for the next person to rediscover.
    Holding the payout count fixed is what isolates the new cell.
    """
    def selects_for(paid_rows):
        # A FRESH consignor per size, so each render sees exactly one
        # payout. Topping up an existing consignor would record a second
        # payout and bill this test for the history N+1 above.
        with Session(db) as session:
            consignor, batch = make_consignor(
                session, name=f"R{paid_rows}",
                username=f"r{paid_rows}@example.com",
            )
            cards = [
                add_card(session, batch, name=f"Undated {n:03d}", owed=1.00)
                for n in range(paid_rows)
            ]
            pay_for(session, consignor.id, cards)
            session.commit()
            consignor_id = consignor.id

        count, html = _count_selects(db, lambda: operator_page(consignor_id))
        table = table_after(html, "<h2>Inventory</h2>")
        assert f"{paid_rows} card(s)" in group_summary(table)
        # Every one of them really is showing its paid date.
        assert html.count(PAID_TEXT) >= paid_rows
        return count

    ten = selects_for(10)
    thirty = selects_for(30)
    assert ten == thirty, f"query count grew with the group: {ten} -> {thirty}"


def test_the_payout_detail_sold_column_adds_no_queries_as_the_payout_grows(db):
    """The Sold cell's rule reads the batched _sold_at_by_card_id and two
    columns already on the loaded card, so a payout covering three times
    as many cards -- a mix of grouped and dated rows -- must not cost a
    query more. This page had no pin at all before today."""
    def selects_for(cards_in_payout):
        with Session(db) as session:
            consignor, batch = make_consignor(
                session, name=f"R{cards_in_payout}",
                username=f"r{cards_in_payout}@example.com",
            )
            cards = []
            for n in range(cards_in_payout):
                # Alternating shapes: half grouped, half with a real date.
                cards.append(add_card(
                    session, batch, name=f"Card {n:03d}", owed=1.00,
                    shipped_at=days_before_today(40) if n % 2 else None,
                    legacy=n % 2 == 0,
                ))
            payout = pay_for(session, consignor.id, cards)
            session.commit()
            payout_id = payout.id

        count, html = _count_selects(db, lambda: operator_payout_page(payout_id))
        table = payout_cards_table(html)
        assert table.count(CELL) == cards_in_payout // 2 + cards_in_payout % 2
        return count

    assert selects_for(6) == selects_for(18)
