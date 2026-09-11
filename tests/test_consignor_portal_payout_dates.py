"""Operator request (2026-09-10): Sold date, Expected payout date, and
Actual paid date on each consignor portal card row -- "It'll help hold
me accountable to how long it's taking to pay people out."

Sold date: SalesOrder.shipped_at via the card's own PickAllocation ->
OrderItem -> SalesOrder chain (order_service.mark_shipped() sets
card.status="sold" and order.shipped_at in the same call).
Expected payout date: computed (not stored) -- sold date (America/
New_York) + 7 days, forward to the next Tuesday or Thursday.
Actual paid date: ConsignorPayout.paid_at via card.consignment_payout_id.

Shared builder (_portal_card_rows): both the actual consignor portal
(/portal/) and the operator-facing "Portal Preview" mirror on
/consignors/{id}/edit render from it, so both get the new columns from
one implementation. /consignors/{id}/edit's OWN separate "Inventory"
section is a different, inline builder -- deliberately NOT touched,
flagged as a follow-up in the ship report.
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from consignment_service import create_consignor_payout
from consignor_auth_service import set_consignor_portal_credentials
from models import (
    Base, Batch, Consignor, InventoryCard, OrderItem, PickAllocation, SalesOrder,
)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'portal-payout-dates.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    return db


def make_consignor_with_login(db, *, name="Jane", username="jane@example.com", password="secretpw"):
    with Session(db) as session:
        consignor = Consignor(name=name, is_active=True)
        session.add(consignor)
        session.flush()
        set_consignor_portal_credentials(session, consignor.id, username, password)
        session.commit()
        session.refresh(consignor)
        return consignor


def make_batch(db, code, *, consignor_id):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_consignment=True, consignor_id=consignor_id)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def make_sold_card_with_order(db, batch_id, *, shipped_at_utc, name="Lightning Bolt", **overrides):
    """A card sold through a real order -- PickAllocation -> OrderItem ->
    SalesOrder, exactly the chain mark_shipped() leaves behind -- not a
    bare InventoryCard.sold_price, so the sold-date lookup has something
    real to join against."""
    with Session(db) as session:
        order = SalesOrder(external_order_id=f"ORD-{name}", status="shipped", shipped_at=shipped_at_utc)
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, name=name, price_cents=1000, quantity=1)
        session.add(item)
        session.flush()
        values = {
            "batch_id": batch_id, "name": name, "status": "sold", "sold_price": 10.0,
            "consignment_amount_owed": 8.0, "consignment_payout_status": "owed",
        }
        values.update(overrides)
        card = InventoryCard(**values)
        session.add(card)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id, batch_id=batch_id, status="shipped",
        ))
        session.commit()
        session.refresh(card)
        return card


def login(client, username="jane@example.com", password="secretpw"):
    return client.post("/portal/login", data={"username": username, "password": password}, follow_redirects=False)


# --- the expected-payout-date rule itself --------------------------------

# One reference week (2026-08-31 Mon .. 2026-09-06 Sun), verified real
# calendar weekdays, each ->  (sold local date, expected payout date).
# Includes both "+7 lands exactly on Tue/Thu" cases (Tue->Tue, Thu->Thu)
# and the operator's own worked example (Wed Sep 2 -> Thu Sep 10).
_WEEKDAY_CASES = [
    ("2026-08-31", "Monday", "2026-09-08"),
    ("2026-09-01", "Tuesday", "2026-09-08"),   # +7 lands exactly on Tuesday
    ("2026-09-02", "Wednesday", "2026-09-10"),  # operator's own example
    ("2026-09-03", "Thursday", "2026-09-10"),   # +7 lands exactly on Thursday
    ("2026-09-04", "Friday", "2026-09-15"),
    ("2026-09-05", "Saturday", "2026-09-15"),
    ("2026-09-06", "Sunday", "2026-09-15"),
]


def test_expected_payout_date_for_every_day_of_the_week():
    for sold_str, weekday_name, expected_str in _WEEKDAY_CASES:
        sold_at = datetime.fromisoformat(sold_str + "T15:00:00")  # naive UTC, mid-afternoon
        result = main._expected_payout_date(sold_at)
        expected = datetime.fromisoformat(expected_str).date()
        assert result == expected, f"{sold_str} ({weekday_name}): expected {expected}, got {result}"
        assert result.weekday() in (1, 3), f"{result} is not a Tuesday or Thursday"


def test_expected_payout_date_dst_boundary_week():
    """Sold Monday March 2, 2026 (EST) -- the +7/forward-search window
    crosses the March 8, 2026 US spring-forward -- must still land on
    the correct calendar Tuesday, not be thrown off by the clock change
    (pure date arithmetic, but this proves the UTC->NY conversion at the
    start doesn't get pulled into it either)."""
    sold_at = datetime(2026, 3, 2, 15, 0)  # naive UTC
    result = main._expected_payout_date(sold_at)
    assert result == datetime(2026, 3, 10).date()
    assert result.weekday() == 1  # Tuesday


def test_expected_payout_date_uses_new_york_not_utc_for_the_day_boundary():
    """A sale timestamp late enough UTC to be a different NY calendar
    day -- 2026-09-03 02:30 UTC is still 2026-09-02 22:30 in New York
    (EDT, UTC-4) -- must compute from the NY date (Wed Sep 2), not the
    UTC date (Thu Sep 3)."""
    sold_at_utc = datetime(2026, 9, 3, 2, 30)  # naive UTC
    result = main._expected_payout_date(sold_at_utc)
    assert result == datetime(2026, 9, 10).date()  # from Wed Sep 2 NY, not Thu Sep 3 UTC


# --- rendered portal row ---------------------------------------------------

def test_portal_row_shows_all_three_dates_for_a_sold_unpaid_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    make_sold_card_with_order(db, batch.id, shipped_at_utc=datetime(2026, 9, 2, 15, 0))
    client = TestClient(main.app)
    login(client)

    response = client.get("/portal/")
    assert response.status_code == 200
    assert "Sold Date" in response.text
    assert "Expected Payout Date" in response.text
    assert "Actual Paid Date" in response.text
    assert "Sep 2, 2026" in response.text  # sold date
    assert "Sep 10, 2026" in response.text  # expected payout date


def test_portal_row_shows_actual_paid_date_once_paid(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    card = make_sold_card_with_order(db, batch.id, shipped_at_utc=datetime(2026, 9, 2, 15, 0))
    create_consignor_payout(consignor.id, [card.id], "Cash App", "", datetime(2026, 9, 5))

    client = TestClient(main.app)
    login(client)
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "Sep 5, 2026" in response.text  # actual paid date
    assert "overdue" not in response.text.lower()  # paid -- never overdue


def test_portal_row_is_blank_for_an_unsold_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    with Session(db) as session:
        session.add(InventoryCard(
            batch_id=batch.id, name="Sol Ring", status="available", consignment_value=5.0,
        ))
        session.commit()

    client = TestClient(main.app)
    login(client)
    response = client.get("/portal/")
    assert response.status_code == 200
    assert "Sol Ring" in response.text
    # no date text anywhere for the unsold row
    row_start = response.text.index("Sol Ring")
    row_end = response.text.index("</tr>", row_start)
    row = response.text[row_start:row_end]
    assert "2026" not in row
    assert "overdue" not in row.lower()


# --- overdue marker ---------------------------------------------------------

def test_portal_row_marks_overdue_when_expected_date_has_passed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    # sold long enough ago that the expected payout date is certainly past
    make_sold_card_with_order(
        db, batch.id, shipped_at_utc=datetime.now() - timedelta(days=60),
    )
    client = TestClient(main.app)
    login(client)

    response = client.get("/portal/")
    assert response.status_code == 200
    assert "overdue" in response.text.lower()
    assert 'class="danger"' in response.text


def test_portal_row_does_not_mark_overdue_before_the_expected_date(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    # sold moments ago -- expected payout date is comfortably in the future
    make_sold_card_with_order(db, batch.id, shipped_at_utc=datetime.now())
    client = TestClient(main.app)
    login(client)

    response = client.get("/portal/")
    assert response.status_code == 200
    assert "overdue" not in response.text.lower()


# --- operator-side "Portal Preview" mirror, same builder --------------------

def test_operator_portal_preview_mirror_shows_the_same_three_dates(tmp_path, monkeypatch):
    """/consignors/{id}/edit's "Portal Preview" section renders through
    the SAME _portal_card_rows builder the real portal uses -- extending
    that one function covers both, per the ticket's own instruction."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    make_sold_card_with_order(db, batch.id, shipped_at_utc=datetime(2026, 9, 2, 15, 0))

    client = TestClient(main.app)
    response = client.get(f"/consignors/{consignor.id}/edit")
    assert response.status_code == 200
    assert "Portal Preview" in response.text
    preview_start = response.text.index("Portal Preview")
    preview_section = response.text[preview_start:]
    assert "Sold Date" in preview_section
    assert "Expected Payout Date" in preview_section
    assert "Actual Paid Date" in preview_section
    assert "Sep 2, 2026" in preview_section
    assert "Sep 10, 2026" in preview_section


def test_operator_own_inventory_section_is_unchanged_by_this_ticket(tmp_path, monkeypatch):
    """The OTHER, separate "Inventory" section on the same page (a
    different, inline builder, not _portal_card_rows) must not have
    picked up the new columns -- confirms the two were never conflated."""
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    make_sold_card_with_order(db, batch.id, shipped_at_utc=datetime(2026, 9, 2, 15, 0))

    client = TestClient(main.app)
    response = client.get(f"/consignors/{consignor.id}/edit")
    assert response.status_code == 200
    inventory_start = response.text.index("<h2>Inventory</h2>")
    portal_preview_start = response.text.index("<h2>Portal Preview</h2>")
    inventory_section = response.text[inventory_start:portal_preview_start]
    assert "Sold Date" not in inventory_section
    assert "Expected Payout Date" not in inventory_section
    assert "Actual Paid Date" not in inventory_section
    assert '<th>Card</th><th>Status</th><th>Value at Consignment</th><th>Sold Price</th><th>Owed</th>' in inventory_section


# --- batching / no per-row query blowup -------------------------------------

def test_sold_at_and_paid_at_lookups_are_batched_not_per_row(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    consignor = make_consignor_with_login(db)
    batch = make_batch(db, "CONSIGN-1", consignor_id=consignor.id)
    cards = [
        make_sold_card_with_order(
            db, batch.id, name=f"Card {i}", shipped_at_utc=datetime(2026, 9, 2, 15, 0),
        )
        for i in range(5)
    ]
    for card in cards[:2]:
        create_consignor_payout(consignor.id, [card.id], "Cash App", "", datetime(2026, 9, 5))

    sold_calls = {"n": 0}
    real_sold = main._sold_at_by_card_id

    def counting_sold(session, card_ids):
        sold_calls["n"] += 1
        return real_sold(session, card_ids)

    paid_calls = {"n": 0}
    real_paid = main._paid_at_by_payout_id

    def counting_paid(session, payout_ids):
        paid_calls["n"] += 1
        return real_paid(session, payout_ids)

    monkeypatch.setattr(main, "_sold_at_by_card_id", counting_sold)
    monkeypatch.setattr(main, "_paid_at_by_payout_id", counting_paid)

    client = TestClient(main.app)
    login(client)
    response = client.get("/portal/")
    assert response.status_code == 200
    assert sold_calls["n"] == 1
    assert paid_calls["n"] == 1
