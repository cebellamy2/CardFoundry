"""A partially-allocated `short` order must be able to finish.

Until 2026-09-23, approve_reserved_order only called allocate_order when
NOTHING was allocated yet. A short order with some lines filled just had
its status re-stamped, so the Retry Allocation button, POST
/orders/{id}/approve and the hourly retry_short_orders sweep all
silently did nothing to it. Live victim: order 4210, two of three lines
allocated, stuck since 2026-09-21.

Relaxing that guard is only safe because allocate_order now subtracts
what each line already holds. The two subtracted terms are disjoint:
an exception's allocation carries status "exception", deliberately
outside ACTIVE_ALLOCATION_STATUSES.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import (Base, Batch, FulfillmentException, InventoryCard, OrderItem,
                    PickAllocation, PickWave, PickWaveOrder, SalesOrder)
from order_service import (ACTIVE_ALLOCATION_STATUSES, InventoryAllocationError,
                           allocate_order, approve_reserved_order,
                           retry_short_orders)

KEY = ("MTG-ALPHA", "EN", "LP", "NF")


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def batch(session, code="B1"):
    b = session.query(Batch).filter(Batch.batch_code == code).first()
    if not b:
        b = Batch(batch_code=code, is_archived=False)
        session.add(b)
        session.flush()
    return b


def card(session, *, name="Alpha", status="available", **kw):
    values = dict(
        batch_id=batch(session).id, name=name, set_code="ONE",
        collector_number="1", mtgjson_id=KEY[0], language_id=KEY[1],
        condition_id=KEY[2], finish_id=KEY[3], status=status,
        imported_at=datetime.now(),
    )
    values.update(kw)
    c = InventoryCard(**values)
    session.add(c)
    session.flush()
    return c


def order_with(session, lines, status="short"):
    """lines = [(name, quantity), ...]"""
    o = SalesOrder(external_order_id=f"ord-{len(lines)}-{id(lines)}", status=status)
    session.add(o)
    session.flush()
    items = []
    for name, qty in lines:
        it = OrderItem(
            order_id=o.id, name=name, mtgjson_id=KEY[0], language_id=KEY[1],
            condition_id=KEY[2], finish_id=KEY[3], quantity=qty,
            set_code="ONE", collector_number="1",
        )
        session.add(it)
        session.flush()
        items.append(it)
    return o, items


def allocate_line(session, item, c):
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=c.id,
        batch_id=c.batch_id, status="allocated",
    ))
    c.status = "reserved"
    session.flush()


# --- (a) the regression this whole change rests on -----------------------

def test_a_retry_never_puts_a_second_copy_on_an_already_filled_line(session):
    """MUST FAIL on pre-2026-09-23 code. The family query filters
    status == "available", so the SAME card can never be re-allocated --
    but a second copy of the same printing in stock was fair game."""
    filled = card(session)
    spare = card(session)                      # a second copy, in stock
    o, (item,) = order_with(session, [("Alpha", 1)])
    allocate_line(session, item, filled)

    allocate_order(session, o)
    session.flush()

    allocs = session.query(PickAllocation).filter(
        PickAllocation.order_item_id == item.id).all()
    assert len(allocs) == 1, "a filled qty-1 line must not gain a second copy"
    assert allocs[0].inventory_card_id == filled.id
    assert spare.status == "available", "the spare must be left alone"


# --- (b) idempotency, and the requested=0 arithmetic ---------------------

def test_retrying_twice_changes_nothing_and_reports_ready(session):
    c1 = card(session)
    o, (item,) = order_with(session, [("Alpha", 1)])
    allocate_line(session, item, c1)

    approve_reserved_order(session, o)
    session.flush()
    first = [(a.order_item_id, a.inventory_card_id) for a in
             session.query(PickAllocation).all()]
    status_after_first = o.status

    approve_reserved_order(session, o)
    session.flush()
    second = [(a.order_item_id, a.inventory_card_id) for a in
              session.query(PickAllocation).all()]

    assert first == second, "a second retry must not change allocations"
    assert session.query(PickAllocation).count() == 1
    assert status_after_first == o.status == "ready_to_pick"


def test_a_fully_covered_order_reports_ready_not_short(session):
    """Pins the arithmetic: every line needs 0, so allocated == requested
    == 0 and fully_matched is True."""
    c1, c2 = card(session), card(session)
    o, items = order_with(session, [("Alpha", 1), ("Alpha", 1)])
    allocate_line(session, items[0], c1)
    allocate_line(session, items[1], c2)

    approve_reserved_order(session, o)
    assert o.status == "ready_to_pick"
    assert session.query(PickAllocation).count() == 2


# --- (c) order 4210's exact shape ---------------------------------------

def test_a_partially_allocated_order_finishes_its_missing_line(session):
    """Order 4210: three lines, two allocated, stock available for the
    third. Only the third must allocate, and the order must leave short."""
    c1, c2 = card(session), card(session)
    third = card(session, name="Gamma")
    o, items = order_with(session, [("Alpha", 1), ("Alpha", 1), ("Gamma", 1)])
    allocate_line(session, items[0], c1)
    allocate_line(session, items[1], c2)
    assert o.status == "short"

    approve_reserved_order(session, o)
    session.flush()

    by_item = {
        it.id: [a.inventory_card_id for a in session.query(PickAllocation)
                .filter(PickAllocation.order_item_id == it.id).all()]
        for it in items
    }
    assert by_item[items[0].id] == [c1.id], "already-filled line unchanged"
    assert by_item[items[1].id] == [c2.id], "already-filled line unchanged"
    assert by_item[items[2].id] == [third.id], "the missing line allocated"
    assert o.status == "ready_to_pick"
    assert third.status == "reserved"


def test_it_stays_short_when_the_missing_line_still_has_no_stock(session):
    c1 = card(session)
    o, items = order_with(session, [("Alpha", 1), ("Gamma", 1)])
    allocate_line(session, items[0], c1)

    approve_reserved_order(session, o)
    session.flush()
    assert o.status == "short"
    assert session.query(PickAllocation).count() == 1


# --- (d) exceptions: subtracted, never double-counted -------------------

def test_an_exception_settled_line_is_not_reallocated(session):
    """The disjointness property. The exception's own allocation carries
    status "exception", outside ACTIVE_ALLOCATION_STATUSES, so it is
    counted by represented_exceptions and NOT by the active count."""
    assert "exception" not in ACTIVE_ALLOCATION_STATUSES
    gone = card(session, status="removed")
    spare = card(session)
    o, (item,) = order_with(session, [("Alpha", 1)])
    alloc = PickAllocation(
        order_item_id=item.id, inventory_card_id=gone.id,
        batch_id=gone.batch_id, status="exception",
    )
    session.add(alloc)
    session.flush()
    session.add(FulfillmentException(
        sales_order_id=o.id, order_item_id=item.id, pick_allocation_id=alloc.id,
        inventory_card_id=gone.id, exception_type="missing",
        note="card missing",
    ))
    session.flush()

    allocate_order(session, o)
    session.flush()

    assert session.query(PickAllocation).filter(
        PickAllocation.order_item_id == item.id,
        PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES)).count() == 0, \
        "an exception-settled line must not be re-allocated"
    assert spare.status == "available"


def test_a_mixed_order_subtracts_both_terms_without_double_counting(session):
    """One line settled by an exception, one line already allocated, one
    line still open. Only the open line may allocate."""
    gone = card(session, status="removed")
    filled = card(session)
    open_stock = card(session, name="Gamma")
    o, items = order_with(session, [("Alpha", 1), ("Alpha", 1), ("Gamma", 1)])
    allocate_line(session, items[1], filled)
    alloc = PickAllocation(
        order_item_id=items[0].id, inventory_card_id=gone.id,
        batch_id=gone.batch_id, status="exception",
    )
    session.add(alloc)
    session.flush()
    session.add(FulfillmentException(
        sales_order_id=o.id, order_item_id=items[0].id,
        pick_allocation_id=alloc.id, inventory_card_id=gone.id,
        exception_type="missing", note="missing",
    ))
    session.flush()

    approve_reserved_order(session, o)
    session.flush()

    active = session.query(PickAllocation).filter(
        PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES)).all()
    assert sorted(a.inventory_card_id for a in active) == sorted(
        [filled.id, open_stock.id])
    assert o.status == "ready_to_pick"


# --- (e) the common case must be byte-identical -------------------------

def test_a_fully_unallocated_order_is_unchanged(session):
    c1, c2 = card(session), card(session)
    o, items = order_with(session, [("Alpha", 1), ("Alpha", 1)], status="needs_review")

    approve_reserved_order(session, o)
    session.flush()

    allocs = session.query(PickAllocation).all()
    assert len(allocs) == 2
    assert sorted(a.inventory_card_id for a in allocs) == sorted([c1.id, c2.id])
    assert o.status == "ready_to_pick"
    assert c1.status == c2.status == "reserved"


def test_a_fully_unallocated_order_with_no_stock_goes_short(session):
    o, _ = order_with(session, [("Alpha", 1)], status="needs_review")
    approve_reserved_order(session, o)
    assert o.status == "short"
    assert session.query(PickAllocation).count() == 0


# --- (f) the v1.193.1 ambiguity guard is untouched ----------------------

def test_a_family_of_two_genuinely_different_cards_still_raises(session):
    card(session, name="Alpha")
    card(session, name="Completely Different Card")
    o, _ = order_with(session, [("Alpha", 1)], status="needs_review")
    with pytest.raises(InventoryAllocationError, match="Ambiguous"):
        allocate_order(session, o)


# --- (g) the wave skip must survive -------------------------------------

def test_the_sweep_still_skips_an_order_in_an_active_wave(session):
    """A live wave owns that order's picking; re-allocating underneath it
    would move inventory the picker is holding a list for."""
    filled = card(session)
    card(session, name="Gamma")
    o, items = order_with(session, [("Alpha", 1), ("Gamma", 1)])
    allocate_line(session, items[0], filled)
    wave = PickWave(label="W1", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=o.id, status="active"))
    session.commit()

    result = retry_short_orders(session)

    assert result["skipped"] == 1
    assert result["attempted"] == 0
    assert o.status == "short", "the wave-held order must be left alone"
    assert session.query(PickAllocation).count() == 1


def test_the_sweep_completes_a_partial_order_not_in_a_wave(session):
    """Operator decision Q1: automatic, no click."""
    filled = card(session)
    open_stock = card(session, name="Gamma")
    o, items = order_with(session, [("Alpha", 1), ("Gamma", 1)])
    allocate_line(session, items[0], filled)
    session.commit()

    result = retry_short_orders(session)

    assert result["attempted"] == 1
    assert result["allocated"] == 1
    assert o.status == "ready_to_pick"
    assert open_stock.status == "reserved"
