"""Ticket D: the hourly sync retries allocation for short orders.

approve_reserved_order was reachable only from the per-order "Retry
Allocation" button; nothing scheduled ever called it. So an order that
came in short while stock was missing stayed short after the stock
arrived, until a human happened to look.

Zero orders are short today, so this is insurance, not cleanup. The tests
therefore construct the states rather than relying on live data.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import order_service
from models import (
    Base, InventoryCard, OrderItem, PickAllocation, PickWave, PickWaveOrder, SalesOrder,
)
from order_service import retry_short_orders
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shortretry.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def short_order(session, status="short"):
    order, item, card, allocation = seed(session)
    # release the allocation so the order genuinely has nothing reserved
    session.delete(allocation)
    card.status = "available"
    order.status = status
    order.review_detail = "Only 0 of 1 copies available."
    session.commit()
    return order, item, card


def test_it_allocates_once_the_stock_has_arrived(db):
    with Session(db) as session:
        order, item, card = short_order(session)
        order_id, card_id = order.id, card.id

    with Session(db) as session:
        result = retry_short_orders(session)

    assert result["attempted"] == 1
    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status not in order_service.RETRYABLE_SHORT_STATUSES
        assert result["allocated"] == 1


def test_it_leaves_the_order_short_when_stock_has_not_arrived(db):
    """approve_reserved_order never raises for a plain shortfall, so the
    order simply stays short and remains on Orders Needing Attention."""
    with Session(db) as session:
        order, item, card = short_order(session)
        card.status = "sold"           # nothing available to allocate
        session.commit()
        order_id = order.id

    with Session(db) as session:
        result = retry_short_orders(session)

    assert result["still_short"] == 1
    assert result["allocated"] == 0
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status in (
            order_service.RETRYABLE_SHORT_STATUSES
        )


def test_it_never_touches_an_order_in_a_live_pick_wave(db):
    """Re-allocating underneath a live wave would move inventory the
    picker is holding a printed list for."""
    with Session(db) as session:
        order, item, card = short_order(session)
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        session.commit()
        order_id = order.id

    with Session(db) as session:
        result = retry_short_orders(session)

    assert result["skipped"] == 1
    assert result["attempted"] == 0
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "short"


@pytest.mark.parametrize("status", ["shipped", "cancelled", "ready_to_pick", "in_pick_wave", "packed"])
def test_it_never_touches_an_ineligible_status(db, status):
    with Session(db) as session:
        order, item, card = short_order(session)
        order.status = status
        session.commit()
        order_id = order.id

    with Session(db) as session:
        result = retry_short_orders(session)

    assert result == {"attempted": 0, "allocated": 0, "still_short": 0,
                      "skipped": 0, "failed": []}
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == status


def test_needs_review_is_retried_too(db):
    with Session(db) as session:
        order, item, card = short_order(session, status="needs_review")
        order_id = order.id

    with Session(db) as session:
        result = retry_short_orders(session)

    assert result["attempted"] == 1


def test_with_nothing_short_it_does_nothing_at_all(db):
    """The live state today: zero short orders."""
    with Session(db) as session:
        seed(session)
        result = retry_short_orders(session)
    assert result["attempted"] == 0
    assert result["failed"] == []


def test_one_failure_leaves_the_others_alone(db, monkeypatch):
    with Session(db) as session:
        first, _, _ = short_order(session)
        second, _, _ = short_order(session)
        first_id, second_id = first.id, second.id

    calls = []

    def flaky(session, order):
        calls.append(order.id)
        if order.id == first_id:
            raise RuntimeError("boom")
        return order_service.approve_reserved_order.__wrapped__(session, order) \
            if hasattr(order_service.approve_reserved_order, "__wrapped__") else None

    monkeypatch.setattr(order_service, "approve_reserved_order", flaky)

    with Session(db) as session:
        result = retry_short_orders(session)

    assert len(result["failed"]) == 1
    assert str(first_id) in result["failed"][0]
    assert second_id in calls, "the second order must still be attempted"


def test_it_goes_through_the_same_approve_path_as_the_button():
    """No second allocation implementation that could drift from the one
    the operator's button uses."""
    source = open("order_service.py").read()
    block = source[source.index("def retry_short_orders"):]
    assert "approve_reserved_order(session, order)" in block
    assert "allocate_order" not in block


def test_the_retry_makes_no_mana_pool_calls():
    """It must cost the sync's request budget nothing."""
    source = open("order_service.py").read()
    block = source[source.index("def retry_short_orders"):]
    for forbidden in ("get_seller_order", "detail_loader", "httpx", "pacer"):
        assert forbidden not in block, forbidden
