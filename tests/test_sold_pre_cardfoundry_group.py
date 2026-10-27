"""Sold Pre-CardFoundry: one group instead of scattered "unknown | unknown".

OPERATOR DECISION, 2026-10-19: a consignor's card list was two thirds
noise. 233 of Ransom's rows are legacy Google-Sheets imports -- sold
before CardFoundry existed, with a price and a payout state but no sale
moment anywhere -- and each one printed "unknown" in the Sold column and
"unknown" in the Expected payout column, interleaved with the rows that
do have real dates. They are now collected under ONE heading that says
how many there are and how much of the money is still owed.

THE GROUP IS A QUESTION ABOUT THE DATA, NOT THE IMPORT SOURCE: a row is
in it because its sale date cannot be resolved from anywhere -- no
shipped allocation, no local-sale fallback -- which is exactly the
condition the date cells already rendered as unknown. Keying it on the
legacy import stamp would silently miss any future row with the same
problem. Tested below both ways.

NOT in the group: a settled row whose only "unknown" is the actual-paid
column (paid, no payout record, but a real sale date). Eight of Ransom's
rows are that shape, and the operator excluded them explicitly.

THREE PAGES render these columns, all from one implementation:
  /portal/                             the consignor's own portal
  /consignors/{id}/edit  Inventory     the operator's own table
  /consignors/{id}/edit  Portal Preview the mirror of the portal
Every test below that matters is run against all three.

NOTHING ABOUT THE MONEY MOVES. The page totals, the per-card amounts and
the Overdue marker are unchanged -- only where a row sits on the page.
Page totals are asserted before and after in both directions.
"""
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import consignment_service
import main
from consignment_service import LEGACY_SHEET_IMPORT_SOURCE
from consignor_auth_service import set_consignor_portal_credentials
from models import (
    Base, Batch, Consignor, InventoryCard, OrderItem, PickAllocation, SalesOrder,
)

# A fixed "today" (a Thursday), so "overdue" means something exact.
TODAY = datetime(2026, 10, 15).date()
HEADING = "Sold Pre-CardFoundry"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'precf.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(consignment_service, "payout_today_local", lambda: TODAY)
    return engine


def make_consignor(session, name, *, username=None, password="secretpw"):
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


def add_card(session, batch, *, name, owed=10.0, status="sold",
             payout_status="owed", legacy=True, shipped_at=None,
             disposed_at=None, disposition_type="local_sale"):
    """A sold card with NO resolvable sale date by default -- the shape the
    group is made of. Pass shipped_at for the real-order chain
    mark_shipped() leaves behind, or disposed_at for a counter sale."""
    card = InventoryCard(
        batch_id=batch.id, name=name, status=status, condition="Near Mint",
        finish="normal", consignment_value=owed, sold_price=owed + 2,
        consignment_amount_owed=owed if status == "sold" else None,
        consignment_payout_status=payout_status if status == "sold" else None,
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


# --- driving the three real pages ----------------------------------------

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


def table_after(html, marker):
    """The one <table> that follows a heading on the page."""
    start = html.index(marker)
    table_start = html.index("<table", start)
    return html[table_start:html.index("</table>", table_start)]


def operator_inventory_table(html):
    return table_after(html, "<h2>Inventory</h2>")


def operator_preview_table(html):
    return table_after(html, "Sees In Their Portal")


def portal_table(html):
    """The portal has one table and no heading above it, so slice the
    table that CONTAINS the header row rather than one that follows it."""
    start = html.index("<th>Card</th>")
    table_start = html.rindex("<table", 0, start)
    return html[table_start:html.index("</table>", table_start)]


def all_three_tables(consignor_id, username="jane@example.com"):
    """(name, table html) for every surface that renders these columns."""
    operator = operator_page(consignor_id)
    return [
        ("operator Inventory", operator_inventory_table(operator)),
        ("operator Portal Preview", operator_preview_table(operator)),
        ("consignor portal", portal_table(portal_page(username))),
    ]


def name_at(table, name):
    """Where a card's own row starts. The name cell carries the colour
    badge too (empty for a colourless test card), so the cell is
    "<td>Name </td>", not "<td>Name</td>" -- match the opening only."""
    return table.index(f"<td>{name} <")


def row_for(table, name):
    start = name_at(table, name)
    return table[table.rindex("<tr", 0, start):table.index("</tr>", start)]


def group_summary(table):
    """The group's heading row, or None when the page shows no group."""
    if HEADING not in table:
        return None
    start = table.index(HEADING)
    return table[table.rindex("<tr", 0, start):table.index("</tr>", start)]


def money_in(fragment):
    return re.findall(r"\$([0-9]+\.[0-9]{2})", fragment)


def currently_owed(html):
    """The "Currently owed: $X" line above a card table -- the portal's own
    total, and the operator page's mirror of it."""
    return re.findall(r"owed: <strong>\$([0-9]+\.[0-9]{2})</strong>", html)


def balance_summary_owed(html):
    """The operator page's separate Currently owed figure in the balance
    summary at the top -- a different builder, same question."""
    return re.findall(
        r"<dt>Currently owed</dt><dd><strong>\$([0-9]+\.[0-9]{2})</strong>", html,
    )


# --- the group itself ----------------------------------------------------

def test_the_group_heading_shows_the_count_and_the_amount_still_owed(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated A", owed=5.25)
        add_card(session, batch, name="Undated B", owed=10.50)
        # Already paid: counted in the group, contributes nothing owed.
        add_card(session, batch, name="Undated Paid", owed=99.00, payout_status="paid")
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        summary = group_summary(table)
        assert summary is not None, f"{label}: no group rendered"
        assert "3 card(s)" in summary, f"{label}: {summary}"
        assert money_in(summary) == ["15.75"], f"{label}: {summary}"


def test_dated_rows_stay_outside_the_group_and_above_it(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Shipped Card", shipped_at=days_before_today(30))
        add_card(session, batch, name="Counter Sale", disposed_at=days_before_today(30),
                 legacy=False)
        add_card(session, batch, name="Undated Card")
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        heading_at = table.index(HEADING)
        for dated in ("Shipped Card", "Counter Sale"):
            assert name_at(table, dated) < heading_at, f"{label}: {dated} fell into the group"
        assert name_at(table, "Undated Card") > heading_at, label


def test_a_consignor_with_no_undated_rows_shows_no_group_at_all(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Patrick", username="pat@example.com")
        add_card(session, batch, name="Shipped Card", shipped_at=days_before_today(30))
        add_card(session, batch, name="Unsold Card", status="available", legacy=False)
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id, username="pat@example.com"):
        assert group_summary(table) is None, f"{label}: {table[:400]}"
        assert HEADING not in table, label


def test_a_consignor_with_no_cards_at_all_still_says_so(db):
    with Session(db) as session:
        consignor, _ = make_consignor(session, "Empty", username="empty@example.com")
        session.commit()
        consignor_id = consignor.id

    operator = operator_page(consignor_id)
    assert "No cards on consignment yet." in operator_inventory_table(operator)
    assert "No cards on consignment yet." in operator_preview_table(operator)
    assert "No cards on consignment yet." in portal_table(portal_page("empty@example.com"))


# --- what the grouped rows say -------------------------------------------

def test_grouped_rows_no_longer_print_unknown_in_the_date_columns(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated Card")
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        row = row_for(table, "Undated Card")
        assert "unknown" not in row.lower(), f"{label}: {row}"


def test_the_overdue_marker_survives_inside_the_group(db):
    """v2.56.0's rule is untouched: an undated legacy owed row IS overdue,
    and the consignor must still be able to see that."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated Owed", owed=7.00)
        add_card(session, batch, name="Undated Paid", owed=7.00, payout_status="paid")
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        owed_row = row_for(table, "Undated Owed")
        assert "overdue" in owed_row.lower(), f"{label}: {owed_row}"
        paid_row = row_for(table, "Undated Paid")
        assert "overdue" not in paid_row.lower(), f"{label}: {paid_row}"


def test_no_date_is_invented_anywhere_in_the_group(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated Card")
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        row = row_for(table, "Undated Card")
        assert not re.search(r"\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2} \d{1,2}, \d{4}", row), \
            f"{label}: a date appeared on an undated row: {row}"


def test_the_per_card_money_on_a_grouped_row_is_unchanged(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated Card", owed=5.25)
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        # value at consignment, sold price, owed -- exactly as before.
        assert money_in(row_for(table, "Undated Card")) == ["5.25", "7.25", "5.25"], label


# --- the rows the operator explicitly kept OUT ---------------------------

def test_a_settled_row_whose_only_unknown_is_the_paid_date_is_not_grouped(db):
    """Ransom's other eight rows: paid, no payout record to date, but a
    REAL sale date. The operator excluded them by name."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Settled Card", payout_status="paid",
                 shipped_at=days_before_today(40), legacy=False)
        add_card(session, batch, name="Undated Card")
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        heading_at = table.index(HEADING)
        assert name_at(table, "Settled Card") < heading_at, label
        settled = row_for(table, "Settled Card")
        # Its one honest unknown -- the paid date -- is still there.
        assert "unknown" in settled.lower(), f"{label}: {settled}"
        assert "1 card(s)" in group_summary(table), label


def test_an_unsold_card_is_never_in_the_group(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Still Listed", status="available", legacy=True)
        add_card(session, batch, name="Undated Card")
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        assert name_at(table, "Still Listed") < table.index(HEADING), label
        assert "1 card(s)" in group_summary(table), label


def test_membership_is_the_missing_date_not_the_legacy_import_stamp(db):
    """A sold card with no date and NO legacy stamp still groups; a legacy
    -stamped card that has a real date does not."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated No Stamp", legacy=False)
        add_card(session, batch, name="Legacy With Date", legacy=True,
                 shipped_at=days_before_today(30))
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        heading_at = table.index(HEADING)
        assert name_at(table, "Undated No Stamp") > heading_at, label
        assert name_at(table, "Legacy With Date") < heading_at, label
        assert "1 card(s)" in group_summary(table), label


# --- the totals do not move ----------------------------------------------

def test_page_totals_are_identical_with_and_without_the_grouping(db):
    """The grouping is a reordering. Both pages' own Currently owed totals
    must read exactly the same as the sum of every owed card, grouped or
    not -- so this asserts the figure, not merely that it is unchanged."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Shipped Owed", owed=20.00,
                 shipped_at=days_before_today(30), legacy=False)
        add_card(session, batch, name="Undated Owed", owed=5.25)
        add_card(session, batch, name="Undated Paid", owed=99.00, payout_status="paid")
        session.commit()
        consignor_id = consignor.id

    # 20.00 + 5.25 owed; the paid card contributes nothing.
    operator = operator_page(consignor_id)
    # The operator page prints it twice, through two different builders --
    # the balance summary at the top and the Portal Preview mirror -- and
    # both must agree with what the consignor's own portal says.
    assert balance_summary_owed(operator) == ["25.25"], operator[:200]
    assert currently_owed(operator) == ["25.25"], currently_owed(operator)
    assert currently_owed(portal_page()) == ["25.25"]

    # And the group's own heading is a SUBSET of that total, never more.
    for label, table in all_three_tables(consignor_id):
        assert money_in(group_summary(table)) == ["5.25"], label


def test_whats_owed_is_untouched_by_the_grouping(db):
    """The operator's own What's Owed page reads the same frozen amounts
    and must not move because a portal table reordered."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated Owed", owed=5.25)
        add_card(session, batch, name="Shipped Owed", owed=20.00,
                 shipped_at=days_before_today(30), legacy=False)
        session.commit()

    html = TestClient(main.app).get("/consignors/owed").text
    table = html[html.index('<table class="data-table'):]
    start = table.index("Ransom")
    row = table[table.rindex("<tr", 0, start):table.index("</tr>", start)]
    # amount owed then amount past due: both undated-legacy and the
    # 30-day-old shipped card are past their deadline.
    assert money_in(row) == ["25.25", "25.25"], row


# --- cost ----------------------------------------------------------------

def test_the_grouping_adds_no_queries_as_the_group_grows(db):
    """Pinned by GROWTH, not by a magic ceiling: tripling the number of
    grouped rows must not add a single query. The split reads only columns
    already on the loaded cards -- there is no per-row lookup behind it."""
    def select_count_for(consignor_id, undated):
        with Session(db) as session:
            batch = session.query(Batch).filter(Batch.consignor_id == consignor_id).one()
            existing = session.query(InventoryCard).filter(
                InventoryCard.batch_id == batch.id).count()
            for n in range(existing, undated):
                add_card(session, batch, name=f"Undated {n:03d}", owed=1.00)
            session.commit()

        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(db, "before_cursor_execute", record)
        try:
            html = operator_page(consignor_id)
        finally:
            event.remove(db, "before_cursor_execute", record)
        assert f"{undated} card(s)" in group_summary(operator_inventory_table(html))
        return len(statements)

    with Session(db) as session:
        consignor, _ = make_consignor(session, "Ransom", username="jane@example.com")
        session.commit()
        consignor_id = consignor.id

    ten = select_count_for(consignor_id, 10)
    thirty = select_count_for(consignor_id, 30)
    assert ten == thirty, f"query count grew with the group: {ten} -> {thirty}"


# --- the cell that escaped the table -------------------------------------
#
# dated_marker_cell() returns a cell's CONTENT; every caller wraps it in
# a td. Two branches of _portal_payout_date_cells did not, so the browser
# foster-parented the bare span OUT of the table: on Ransom's portal, 137
# loose "overdue" badges stacked up above the table and each of those
# rows rendered a column short. One branch shipped in v2.56.0; the other
# was written here and had copied it. Both are pinned below, by COLUMN
# COUNT against the header row -- the property that actually broke.

def _cells_in(fragment):
    return len(re.findall(r"<td\b", fragment))


def test_every_row_has_as_many_cells_as_the_table_has_columns(db):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated Owed", owed=7.00)
        add_card(session, batch, name="Undated Paid", owed=7.00, payout_status="paid")
        add_card(session, batch, name="Shipped Owed", owed=20.00,
                 shipped_at=days_before_today(30), legacy=False)
        add_card(session, batch, name="Still Listed", status="available", legacy=False)
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        columns = len(re.findall(r"<th\b", table))
        assert columns == 8, f"{label}: header changed shape ({columns})"
        for name in ("Undated Owed", "Undated Paid", "Shipped Owed", "Still Listed"):
            row = row_for(table, name)
            assert _cells_in(row) == columns, f"{label}: {name} has {_cells_in(row)}: {row}"


def test_an_overdue_marker_never_escapes_its_cell(db):
    """The regression itself: the marker span must sit INSIDE a td, in the
    grouped rows and in the ungrouped undated-overdue branch alike."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Ransom", username="jane@example.com")
        add_card(session, batch, name="Undated Owed", owed=7.00)
        session.commit()
        consignor_id = consignor.id

    for label, table in all_three_tables(consignor_id):
        row = row_for(table, "Undated Owed")
        marker_at = row.index("cell-badge-danger")
        # The nearest cell boundary before the marker must be an opening
        # td, not a closing one -- a bare span sits after a </td>.
        assert row.rindex("<td", 0, marker_at) > row.rindex("</td>", 0, marker_at), \
            f"{label}: marker is outside a cell: {row}"


def test_the_ungrouped_undated_overdue_branch_is_still_a_whole_row(db):
    """Not reachable from the three pages any more -- they group those
    rows -- but the branch is still live for any caller that renders a
    single card's cells, and it is where the bug shipped."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, name="Undated Owed", owed=7.00)
        session.commit()
        cells = main._portal_payout_date_cells(card, {}, {})

    assert _cells_in(cells) == 3, cells
    assert "overdue" in cells.lower()
    assert cells.count("unknown") == 2, cells
    marker_at = cells.index("cell-badge-danger")
    assert cells.rindex("<td", 0, marker_at) > cells.rindex("</td>", 0, marker_at), cells
