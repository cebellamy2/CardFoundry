"""Slice 1: CardFoundry reflects Mana Pool-side cancellations.

The bug this closes: the order sync lists only needs_shipping=true
orders, so an order refunded on Mana Pool stops being returned and is
never read again. Order 4117 sat "ready_to_pick" against a refunded Mana
Pool order for exactly that reason.

The two rules that carry the most risk are pinned hardest:
  * a terminal remote status is NOT on its own a cancellation. One order
    was refunded AFTER shipping, and 35 of the 36 "replaced" ones were
    already shipped. Releasing inventory on those would invent stock
    sitting in a customer's hands. "replaced" joined "refunded" as a
    cancelling status on 2026-09-17 (see the test below for why); the
    shipped guard is what keeps that safe.
  * only orders ABSENT from the listing are re-read, because one still
    open on both sides is already fetched during ingest.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import order_service
from models import (
    Base, InventoryCard, OrderCancellation, OrderItem, PickAllocation, SalesOrder,
)
from order_service import (
    orders_missing_from_remote_listing, reconcile_remote_cancellations, release_order,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cancelsync.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def open_order(session, *, external="mp-open", status="ready_to_pick",
               allocation_status="allocated", card_status="reserved"):
    _, item, card, allocation = seed(
        session, allocation_status=allocation_status,
        card_status=card_status, order_status=status,
    )
    order = session.get(SalesOrder, item.order_id)
    order.external_order_id = external
    order.external_label = external
    order.status = status
    session.commit()
    return order, item, card, allocation


def loader_returning(status):
    def _load(external_id):
        return {"order": {"id": external_id, "latest_fulfillment_status": status}}
    return _load


def no_pacing(session, remote, loader, **kw):
    return reconcile_remote_cancellations(
        session, remote, loader, min_request_interval=0, **kw
    )


# --- targeting ----------------------------------------------------------

def test_only_orders_absent_from_the_listing_are_targeted(db):
    """An order still in the needs_shipping listing already costs a detail
    fetch during ingest; re-reading it here would double that for nothing."""
    with Session(db) as session:
        present, _, _, _ = open_order(session, external="mp-present")
        absent, _, _, _ = open_order(session, external="mp-absent")
        targets = orders_missing_from_remote_listing(
            session, [{"id": "mp-present"}],
        )
        assert [o.external_order_id for o in targets] == ["mp-absent"]


def test_shipped_orders_are_never_targeted(db):
    with Session(db) as session:
        open_order(session, external="mp-done", status="shipped")
        assert orders_missing_from_remote_listing(session, []) == []


def test_no_calls_are_made_when_nothing_is_absent(db):
    with Session(db) as session:
        open_order(session, external="mp-present")
        calls = []

        def counting(external_id):
            calls.append(external_id)
            return {"order": {"latest_fulfillment_status": "refunded"}}

        result = no_pacing(session, [{"id": "mp-present"}], counting)
    assert calls == []
    assert result["calls"] == 0


# --- branch 1: whole order refunded, NOT shipped ------------------------

def test_refunded_unshipped_order_is_cancelled_and_inventory_released(db):
    with Session(db) as session:
        order, _, card, allocation = open_order(session, external="mp-refund")
        order_id, card_id, allocation_id = order.id, card.id, allocation.id

    with Session(db) as session:
        result = no_pacing(session, [], loader_returning("refunded"))
    assert result["cancelled"] == 1
    assert result["status_only"] == 0

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "cancelled"
        assert order.remote_fulfillment_status == "refunded"
        assert order.cancelled_from_status == "ready_to_pick"
        assert session.get(InventoryCard, card_id).status == "available"
        assert session.get(PickAllocation, allocation_id).status == "released"


# --- branch 2: whole order refunded, ALREADY shipped --------------------

def test_a_shipped_order_is_never_targeted_so_its_cards_are_never_released(db):
    """Decision 6's safety half, which is the half that matters: a shipped
    order's cards are with a customer, and releasing them would invent
    stock that does not exist.

    Guaranteed twice over. "shipped" is not in LOCALLY_OPEN_ORDER_STATUSES
    so such an order is never even fetched, AND reconcile_remote_
    cancellations still checks previous_status == "shipped" before
    releasing anything.

    KNOWN GAP, reported not papered over: because shipped orders are never
    targeted, the other half of decision 6 -- updating a shipped order's
    local status when Mana Pool refunds it after the fact -- does not
    happen in this slice. Reaching it would mean re-reading all 4,081
    shipped orders per tick, which the Mana Pool request budget cannot
    carry. It needs its own bounded mechanism.
    """
    with Session(db) as session:
        order, _, card, allocation = open_order(
            session, external="mp-post", allocation_status="packed",
        )
        order.status = "shipped"
        session.commit()
        card_id, allocation_id = card.id, allocation.id

    with Session(db) as session:
        assert orders_missing_from_remote_listing(session, []) == []
        result = no_pacing(session, [], loader_returning("refunded"))

    assert result["checked"] == 0
    assert result["cancelled"] == 0
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "reserved"
        assert session.get(PickAllocation, allocation_id).status == "packed"


def test_replaced_on_an_unshipped_order_cancels_it_like_a_refund(db):
    """DELIBERATE REVERSAL of what this test asserted until 2026-09-17.

    It used to pin "record the status, release nothing", on the reading
    that "replaced" had only ever appeared on already-shipped orders and
    an unshipped one would be a new case to log rather than guess at. The
    case then occurred -- order 4138 sat "picked" with nothing able to
    move it -- and the operator settled what it means:

      "effectively refunded and replaced to me are the same status
       because it just means that it was taken care of and I didn't get
       the payout."

    Mana Pool sources the card from a DIFFERENT seller and charges us for
    it. Nothing ships from here and no money arrives, so the order is over
    and our card is ours again -- exactly a refund, from this side.
    """
    with Session(db) as session:
        order, _, card, allocation = open_order(session, external="mp-replaced")
        order_id, card_id, allocation_id = order.id, card.id, allocation.id

    with Session(db) as session:
        result = no_pacing(session, [], loader_returning("replaced"))
    assert result["cancelled"] == 1
    assert result["status_only"] == 0

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "cancelled"
        assert order.remote_fulfillment_status == "replaced"
        assert session.get(InventoryCard, card_id).status == "available", (
            "our card is ours again"
        )
        assert session.get(PickAllocation, allocation_id).status == "released"


def test_a_non_terminal_absent_order_is_left_completely_alone(db):
    with Session(db) as session:
        order, _, card, _ = open_order(session, external="mp-weird")
        order_id, card_id = order.id, card.id

    with Session(db) as session:
        result = no_pacing(session, [], loader_returning("processing"))
    assert result["unchanged"] == 1
    assert result["cancelled"] == 0

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "ready_to_pick"
        assert session.get(InventoryCard, card_id).status == "reserved"


# --- picked/packed release the same way (decision 2) --------------------

@pytest.mark.parametrize("allocation_status", ["allocated", "picked", "packed"])
def test_picked_and_packed_release_exactly_like_unpicked(db, allocation_status):
    with Session(db) as session:
        order, _, card, allocation = open_order(
            session, external=f"mp-{allocation_status}",
            allocation_status=allocation_status,
        )
        card_id, allocation_id = card.id, allocation.id

    with Session(db) as session:
        result = no_pacing(session, [], loader_returning("refunded"))
    assert result["cancelled"] == 1

    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "available"
        released = session.get(PickAllocation, allocation_id)
        assert released.status == "released"
        assert released.released_from_status == allocation_status


# --- the audit record ---------------------------------------------------

def test_sync_cancellation_writes_an_audit_row_with_remote_evidence(db):
    with Session(db) as session:
        order, item, card, allocation = open_order(session, external="mp-audit")
        order_id, card_id = order.id, card.id

    with Session(db) as session:
        no_pacing(session, [], loader_returning("refunded"))

    with Session(db) as session:
        rows = session.query(OrderCancellation).filter_by(sales_order_id=order_id).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.initiated_by == "manapool_sync"
        assert row.reason == "cancelled_on_manapool"
        assert row.previous_order_status == "ready_to_pick"
        assert row.remote_status_observed == "refunded"
        assert row.remote_observed_at is not None
        assert row.released_card_count == 1
        detail = json.loads(row.released_cards_json)
        assert detail[0]["inventory_card_id"] == card_id
        assert detail[0]["released_from_allocation_status"] == "allocated"


def test_manual_cancellation_also_writes_an_audit_row(db):
    """Both surfaces go through release_order, so neither can skip the
    record. Before this, cancelling wrote no audit at all while its own
    undo did -- the reversal was logged and the destructive act was not."""
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-manual")
        order_id = order.id

    client = TestClient(main.app)
    client.post(f"/orders/{order_id}/cancel",
                data={"cancel_reason": "buyer_requested", "cancel_note": "changed their mind"})

    with Session(db) as session:
        row = session.query(OrderCancellation).filter_by(sales_order_id=order_id).one()
        assert row.initiated_by == "operator"
        assert row.reason == "buyer_requested"
        assert row.note == "changed their mind"
        assert row.remote_status_observed is None   # no remote evidence
        assert session.get(SalesOrder, order_id).status == "cancelled"


def test_an_unknown_reason_falls_back_rather_than_losing_the_cancellation(db):
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-badreason")
        order_id = order.id

    client = TestClient(main.app)
    client.post(f"/orders/{order_id}/cancel", data={"cancel_reason": "nonsense"})

    with Session(db) as session:
        assert session.query(OrderCancellation).filter_by(
            sales_order_id=order_id).one().reason == "other"
        assert session.get(SalesOrder, order_id).status == "cancelled"


def test_the_audit_row_is_never_rewritten_by_a_second_cancellation(db):
    """Immutable: a re-cancel appends, it does not update."""
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-twice")
        order_id = order.id

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        release_order(session, order, reason="card_missing", initiated_by="operator")
        session.commit()
        order.status = "ready_to_pick"
        session.commit()
        release_order(session, order, reason="buyer_requested", initiated_by="operator")
        session.commit()

    with Session(db) as session:
        rows = session.query(OrderCancellation).filter_by(
            sales_order_id=order_id).order_by(OrderCancellation.id).all()
        assert [r.reason for r in rows] == ["card_missing", "buyer_requested"]


# --- the reason dropdown ------------------------------------------------

def test_the_page_offers_reasons_but_never_the_remote_only_one(db):
    """"cancelled_on_manapool" asserts remote evidence the operator does
    not have when clicking Cancel by hand."""
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-reasons")
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert 'name="cancel_reason"' in text
    assert 'value="buyer_requested"' in text
    assert 'value="card_missing"' in text
    assert 'value="cancelled_on_manapool"' not in text


# --- call budget --------------------------------------------------------

def test_the_pass_is_capped_and_defers_the_rest(db):
    with Session(db) as session:
        for n in range(5):
            open_order(session, external=f"mp-many-{n}")

    with Session(db) as session:
        result = no_pacing(session, [], loader_returning("refunded"), max_orders=2)
    assert result["calls"] == 2
    assert result["cancelled"] == 2
    assert result["deferred"] == 3


def test_one_failure_does_not_stop_the_others(db):
    with Session(db) as session:
        open_order(session, external="mp-bad")
        open_order(session, external="mp-good")

    def flaky(external_id):
        if external_id == "mp-bad":
            raise RuntimeError("boom")
        return {"order": {"latest_fulfillment_status": "refunded"}}

    with Session(db) as session:
        result = no_pacing(session, [], flaky)
    assert result["cancelled"] == 1
    assert len(result["failed"]) == 1
    assert "mp-bad" in result["failed"][0]


def test_dry_run_classifies_without_writing_anything(db):
    """The preview runs the real path, not a parallel description of it."""
    with Session(db) as session:
        order, _, card, allocation = open_order(session, external="mp-dry")
        order_id, card_id, allocation_id = order.id, card.id, allocation.id

    with Session(db) as session:
        result = no_pacing(session, [], loader_returning("refunded"), dry_run=True)
    assert result["cancelled"] == 1
    assert result["preview"][0]["action"].startswith("CANCEL")

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "ready_to_pick"
        assert session.get(SalesOrder, order_id).remote_fulfillment_status is None
        assert session.get(InventoryCard, card_id).status == "reserved"
        assert session.get(PickAllocation, allocation_id).status == "allocated"
        assert session.query(OrderCancellation).count() == 0


# --- slice 1b: per-line settlement --------------------------------------

def exception_line(session, order, *, kind="missing"):
    """Add a second line to `order` that carries an open exception."""
    from fulfillment_exception_service import mark_fulfillment_exception
    stray, item, card, allocation = seed(session)
    item.order_id = order.id
    # seed() builds a whole order around the line; move the line onto the
    # order under test and park the leftover so the reconciliation pass
    # does not pick IT up as a second open order and skew the counts.
    stray.status = "shipped"
    session.flush()
    exception = mark_fulfillment_exception(session, allocation.id, kind)
    session.commit()
    return exception, card, allocation


def test_a_refund_never_resurrects_a_card_already_declared_missing(db):
    """The phantom-stock case this slice exists to prevent. The operator
    could not find the card, told Mana Pool, and Mana Pool refunded --
    releasing it back to available would invent stock that is not there."""
    with Session(db) as session:
        order, _, shelf_card, shelf_alloc = open_order(session, external="mp-mixed")
        exception, missing_card, exc_alloc = exception_line(session, order)
        shelf_id, missing_id = shelf_card.id, missing_card.id
        exception_id, exc_alloc_id = exception.id, exc_alloc.id

    with Session(db) as session:
        result = no_pacing(session, [], loader_returning("refunded"))
    assert result["cancelled"] == 1

    with Session(db) as session:
        # the shelf card WAS released
        assert session.get(InventoryCard, shelf_id).status == "available"
        # the missing card was NOT
        missing = session.get(InventoryCard, missing_id)
        assert missing.status == "removed"
        assert missing.removal_reason == "fulfillment_missing"
        assert session.get(PickAllocation, exc_alloc_id).status == "exception"


def test_the_refund_auto_resolves_the_open_exception(db):
    """Operator-approved override of "a terminal outcome only unlocks a
    close-out": the operator declared the card missing, told Mana Pool,
    and Mana Pool agreed by refunding. Nothing is left to judge."""
    from models import FulfillmentException
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-autoresolve")
        exception, card, _ = exception_line(session, order)
        exception_id, card_id = exception.id, card.id

    with Session(db) as session:
        no_pacing(session, [], loader_returning("refunded"))

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        assert exception.inventory_resolution_state == "resolved"
        assert exception.remote_resolution_state == "resolved_refunded"
        assert exception.remote_resolved_at is not None
        assert "Mana Pool reported" in exception.resolution_note
        # projection cleared, card untouched
        card = session.get(InventoryCard, card_id)
        assert card.inventory_exception_state == "none"
        assert card.status == "removed"


def test_a_manual_cancel_leaves_the_exception_open(db):
    """No remote evidence exists, so claiming a refund would be a lie.
    The per-line SHAPE matches the sync; the fabricated field does not."""
    from models import FulfillmentException
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-manualexc")
        exception, card, _ = exception_line(session, order)
        order_id, exception_id, card_id = order.id, exception.id, card.id

    TestClient(main.app).post(f"/orders/{order_id}/cancel",
                              data={"cancel_reason": "buyer_requested"})

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        assert exception.inventory_resolution_state == "unresolved"
        assert exception.remote_resolution_state == "awaiting"
        assert session.get(InventoryCard, card_id).status == "removed"
        assert session.get(SalesOrder, order_id).status == "cancelled"


def test_every_line_outcome_lands_in_the_audit_row(db):
    with Session(db) as session:
        order, _, shelf_card, _ = open_order(session, external="mp-audit2")
        exception, missing_card, _ = exception_line(session, order)
        order_id, shelf_id, missing_id = order.id, shelf_card.id, missing_card.id

    with Session(db) as session:
        no_pacing(session, [], loader_returning("refunded"))

    with Session(db) as session:
        row = session.query(OrderCancellation).filter_by(sales_order_id=order_id).one()
        detail = {d["inventory_card_id"]: d for d in json.loads(row.released_cards_json)}
        assert detail[shelf_id]["outcome"] == "released_to_available"
        assert detail[missing_id]["outcome"] == "exception_resolved_by_refund"
        assert detail[missing_id]["card_status_after"] == "removed"
        assert detail[missing_id]["fulfillment_exception_id"]


def test_add_back_to_inventory_is_offered_once_the_exception_is_resolved(db):
    with Session(db) as session:
        order, _, _, _ = open_order(session, external="mp-addback")
        exception, card, _ = exception_line(session, order)
        order_id, card_id = order.id, card.id

    # before the refund: exception open, so no button (the v1.159.0 guard
    # would refuse the edit anyway)
    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Add Back To Inventory" not in text

    with Session(db) as session:
        no_pacing(session, [], loader_returning("refunded"))

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Add Back To Inventory" in text
    assert f'action="/inventory/{card_id}/un-remove/preview"' in text
