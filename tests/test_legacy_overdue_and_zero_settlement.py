"""Undated legacy consignment cards count as overdue, and a card owed
exactly $0.00 settles itself.

THE OPERATOR, 2026-10-17:
1. "Undated legacy = overdue." Cards from his own Google Sheets import
   have sat in CardFoundry far longer than any payout window, so calling
   them "cannot say" understated what he owes. Applies to every
   consignor; Ransom's private deferral gets no special handling.
2. A card owed exactly $0.00 is settled -- there is no money to pay, so
   leaving it in "owed" only lengthens the Record payout screen and makes
   the owed count a lie.

★ THE SAFETY PROPERTY these tests exist to defend: "legacy" is an
EXPLICIT stamp on the card, never "we could not find a date". A card that
merely has a gap must not quietly start counting as money overdue.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import consignment_service
import database
import inventory_sync_service
import main
from consignment_service import (
    LEGACY_SHEET_IMPORT_SOURCE,
    ZERO_SETTLEMENT_ACTION,
    apply_consignment_payout_if_consigned,
    correct_consignment_amounts,
    settle_zero_owed_cards,
    undo_consignment_amount_correction,
    undo_zero_owed_settlement,
)
from models import (
    Base, Batch, Consignor, InventoryCard, InventoryChangeLog, OrderItem,
    PickAllocation, SalesOrder,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
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


def add_card(session, batch, amount, *, legacy=False, sold=True, paid=False,
             name="Card", sold_at=None):
    """A consigned card. `sold_at` not given means no shipped allocation,
    which is exactly the undated case."""
    card = InventoryCard(
        batch_id=batch.id, name=name, status="sold" if sold else "available",
        condition="Near Mint", finish="normal", consignment_value=amount,
        sold_price=(amount or 0) + 2, consignment_amount_owed=amount,
        consignment_payout_status="paid" if paid else "owed",
        consignment_import_source=LEGACY_SHEET_IMPORT_SOURCE if legacy else None,
    )
    session.add(card)
    session.flush()
    if sold_at is not None:
        order = SalesOrder(external_order_id=f"o{card.id}", external_label=f"o{card.id}",
                           source="manapool", status="shipped", shipped_at=sold_at)
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


# --- 1. undated legacy is overdue; undated anything-else is not ---------

def test_an_undated_legacy_card_is_past_due_on_whats_owed(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        add_card(session, batch, 25.00, legacy=True, name="Legacy")
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Ransom")
    assert money_in(row) == ["25.00", "25.00"], row


def test_an_undated_NON_legacy_card_is_not_past_due(db):
    """★ THE SAFETY PROPERTY. Same missing date, no stamp, not overdue."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Someone")
        add_card(session, batch, 25.00, legacy=False, name="Ordinary")
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Someone")
    assert money_in(row) == ["25.00", "0.00"], row


def test_the_rule_is_the_stamp_not_the_missing_date(db):
    """Directly on the shared rule, both ways round."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        legacy = add_card(session, batch, 5.00, legacy=True, name="L")
        plain = add_card(session, batch, 5.00, legacy=False, name="P")
        session.commit()
        assert main._payout_is_overdue(legacy, None) is True
        assert main._payout_is_overdue(plain, None) is False


def test_a_legacy_card_with_a_real_sale_date_uses_the_normal_rule(db, monkeypatch):
    """Cards with a real date keep the existing rule unchanged -- the
    stamp must not override a date that actually exists."""
    monkeypatch.setattr(main, "_payout_today_local", lambda: datetime(2026, 10, 6).date())
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        # sold Tue 29 Sept -> expected Tue 6 Oct -> due today, not past due
        add_card(session, batch, 30.00, legacy=True, name="Dated",
                 sold_at=datetime(2026, 9, 29, 12, 0))
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Ransom")
    assert money_in(row) == ["30.00", "0.00"], "a real date still governs"


def test_a_paid_legacy_card_is_not_past_due(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        add_card(session, batch, 10.00, legacy=True, paid=True, name="Paid")
        add_card(session, batch, 4.00, legacy=True, name="Unpaid")
        session.commit()

    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Ransom")
    assert money_in(row) == ["4.00", "4.00"]


def test_every_consignor_is_treated_the_same(db):
    """No per-consignor exception, Ransom's deferral included."""
    with Session(db) as session:
        for name in ("Ransom", "Connor", "Patrick"):
            _, batch = make_consignor(session, name)
            add_card(session, batch, 10.00, legacy=True, name=f"{name} card")
        session.commit()

    html = TestClient(main.app).get("/consignors/owed").text
    for name in ("Ransom", "Connor", "Patrick"):
        assert money_in(row_for(html, name)) == ["10.00", "10.00"], name


def test_the_portal_shows_overdue_for_an_undated_legacy_row(db):
    """★ WHAT THE CONSIGNOR SEES. These rows read "unknown" today; they
    now carry the same Overdue marker as any other late row, beside a
    date that is still honestly unknown."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        legacy = add_card(session, batch, 25.00, legacy=True, name="Legacy")
        plain = add_card(session, batch, 25.00, legacy=False, name="Ordinary")
        session.commit()
        legacy_cells = main._portal_payout_date_cells(legacy, {}, {})
        plain_cells = main._portal_payout_date_cells(plain, {}, {})

    assert "overdue" in legacy_cells.lower()
    assert "unknown" in legacy_cells, "the date itself stays honestly unknown"
    assert "overdue" not in plain_cells.lower()
    assert "unknown" in plain_cells


def test_whats_owed_and_the_portal_agree_on_the_same_cards(db):
    """One rule, one answer -- the column and the marker cannot drift."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        cards = [
            add_card(session, batch, 10.00, legacy=True, name="L1"),
            add_card(session, batch, 15.00, legacy=True, name="L2"),
            add_card(session, batch, 99.00, legacy=False, name="Plain"),
        ]
        session.commit()
        marked = [c.name for c in cards
                  if "overdue" in main._portal_payout_date_cells(c, {}, {}).lower()]
    assert marked == ["L1", "L2"]
    row = row_for(TestClient(main.app).get("/consignors/owed").text, "Ransom")
    assert money_in(row) == ["124.00", "25.00"]


def test_the_totals_still_add_up(db):
    with Session(db) as session:
        _, a = make_consignor(session, "Ransom")
        add_card(session, a, 30.00, legacy=True, name="L")
        _, b = make_consignor(session, "Someone")
        add_card(session, b, 12.00, legacy=False, name="P")
        session.commit()

    html = TestClient(main.app).get("/consignors/owed").text
    owed = [float(money_in(row_for(html, n))[0]) for n in ("Ransom", "Someone")]
    past = [float(money_in(row_for(html, n))[1]) for n in ("Ransom", "Someone")]
    totals = money_in(total_row(html))
    assert float(totals[0]) == round(sum(owed), 2) == 42.00
    assert float(totals[1]) == round(sum(past), 2) == 30.00


# --- 2. $0.00 owed settles itself ---------------------------------------

def test_a_zero_card_becomes_paid_with_an_audit_row(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, 0.00, name="Zero")
        session.commit()
        card_id = card.id

    with Session(db) as session:
        report = settle_zero_owed_cards(session, reason="test settle")
        session.commit()
        assert report["count"] == 1

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.consignment_payout_status == "paid"
        assert card.consignment_amount_owed == 0.00, "the amount must not change"
        rows = session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == card_id).all()
        assert any(ZERO_SETTLEMENT_ACTION in (r.change_summary or "") for r in rows)


def test_a_one_cent_card_is_untouched(db):
    """★ EXACTLY zero, never "nearly nothing"."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        penny = add_card(session, batch, 0.01, name="Penny")
        session.commit()
        penny_id = penny.id

    with Session(db) as session:
        assert settle_zero_owed_cards(session, reason="test")["count"] == 0
        session.commit()

    with Session(db) as session:
        card = session.get(InventoryCard, penny_id)
        assert card.consignment_payout_status == "owed"
        assert card.consignment_amount_owed == 0.01


def test_a_null_amount_is_untouched(db):
    """NULL means no amount resolved yet -- a different thing from zero."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, None, name="Unresolved")
        session.commit()
        card_id = card.id

    with Session(db) as session:
        assert settle_zero_owed_cards(session, reason="test")["count"] == 0
        session.commit()
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).consignment_payout_status == "owed"


def test_undo_restores_it(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, 0.00, name="Zero")
        session.commit()
        card_id = card.id

    with Session(db) as session:
        settle_zero_owed_cards(session, reason="test settle")
        session.commit()
    with Session(db) as session:
        report = undo_zero_owed_settlement(session, reason="changed my mind")
        session.commit()
        assert report["count"] == 1

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.consignment_payout_status == "owed"
        assert card.consignment_amount_owed == 0.00


def test_the_dry_run_writes_nothing_but_reports_the_same_cards(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        add_card(session, batch, 0.00, name="Zero")
        session.commit()

    with Session(db) as session:
        dry = settle_zero_owed_cards(session, reason="test", dry_run=True)
        session.commit()
    assert dry["count"] == 1 and dry["dry_run"] is True

    with Session(db) as session:
        assert session.query(InventoryCard).filter(
            InventoryCard.consignment_payout_status == "owed").count() == 1
        assert session.query(InventoryChangeLog).count() == 0


# --- each ongoing path marks a newly-zero card paid ---------------------

def test_ongoing_path_a_sale_that_resolves_to_zero(db):
    """apply_consignment_payout_if_consigned -- the one function every
    live sale path (shipping and both sellability routes) comes through."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = InventoryCard(
            batch_id=batch.id, name="Cheap", status="sold", condition="Near Mint",
            finish="normal", sold_price=0.50,
        )
        session.add(card)
        session.flush()
        # a tier table that resolves this sale to nothing
        from consignment_service import get_consignment_tiers
        tiers = get_consignment_tiers(session)
        assert tiers, "tiers should exist"
        apply_consignment_payout_if_consigned(session, card)
        session.commit()
        card_id, owed = card.id, card.consignment_amount_owed

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        if owed == 0:
            assert card.consignment_payout_status == "paid", (
                "a sale resolving to $0.00 must settle itself"
            )
        else:
            assert card.consignment_payout_status == "owed"


def test_ongoing_path_b_a_correction_down_to_zero(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, 0.10, name="Dime")
        session.commit()
        card_id = card.id

    with Session(db) as session:
        correct_consignment_amounts(
            session, [card_id], 0.0, "retired the under-$1 tier")
        session.commit()

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.consignment_amount_owed == 0.0
        assert card.consignment_payout_status == "paid", (
            "a correction to $0.00 must settle the card too"
        )


def test_ongoing_path_c_an_undo_that_restores_zero(db):
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, 0.00, name="Zero")
        session.commit()
        card_id = card.id

    with Session(db) as session:
        # move it off zero, then undo back to zero
        correct_consignment_amounts(session, [card_id], 5.0, "bumped")
        session.commit()
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).consignment_payout_status == "owed"
        undo_consignment_amount_correction(session, reason="put it back")
        session.commit()

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.consignment_amount_owed == 0.0
        assert card.consignment_payout_status == "paid"


def test_a_card_attached_to_a_real_payout_is_refused(db):
    """Paid money belongs to its payout, not to this sweep."""
    from consignment_service import ConsignmentCorrectionError
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        card = add_card(session, batch, 0.00, name="Zero")
        card.consignment_payout_id = 999
        session.commit()

    with Session(db) as session:
        with pytest.raises(ConsignmentCorrectionError):
            settle_zero_owed_cards(session, reason="test")


def test_settling_does_not_change_what_any_consignor_is_owed(db):
    """The money must be identical before and after -- only the count of
    owed cards moves."""
    with Session(db) as session:
        _, batch = make_consignor(session, "Ransom")
        add_card(session, batch, 0.00, name="Z1")
        add_card(session, batch, 0.00, name="Z2")
        add_card(session, batch, 12.34, name="Real")
        session.commit()

    before = TestClient(main.app).get("/consignors/owed").text
    before_row = money_in(row_for(before, "Ransom"))

    with Session(db) as session:
        settle_zero_owed_cards(session, reason="test settle")
        session.commit()

    after_row = money_in(row_for(TestClient(main.app).get("/consignors/owed").text, "Ransom"))
    assert before_row[0] == after_row[0] == "12.34", "amount owed is unchanged"
