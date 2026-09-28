"""pick_allocations' card uniqueness applies to ACTIVE rows only.

WHY. `ix_pick_allocations_inventory_card_id` was UNIQUE over every row,
and allocation rows are never deleted -- release keeps them as "released"
so uncancel_order can restore them from released_from_status, and an
exception keeps them for audit. So a card that had ever been allocated to
ANY order could never be allocated again; the INSERT died on the index.

Live victim: order 4279's The Fire Crystal (card 6688), available on the
shelf, blocked by allocation 445 -- an "exception" row belonging to order
3877, which shipped in August. Four available cards were blocked in total
(1 exception, 3 released).

The operator approved replacing it with a PARTIAL unique index over
status IN ('allocated','picked','packed') -- one explicit exception to the
additive-only migration rule. NO allocation rows are deleted.
"""
import logging
import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import database
import main
from database import (ACTIVE_ALLOCATION_INDEX, LEGACY_ALLOCATION_CARD_INDEX,
                      initialize_database)
from models import (Base, Batch, InventoryCard, OrderItem, PickAllocation,
                    SalesOrder)
from order_service import allocate_order, uncancel_order, InventoryAllocationError

KEY = ("MTG-ONE-1", "EN", "LP", "NF")


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alloc.db'}")
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


def card(session, **kw):
    values = dict(
        batch_id=batch(session).id, name="Alpha", set_code="ONE",
        collector_number="1", mtgjson_id=KEY[0], language_id=KEY[1],
        condition_id=KEY[2], finish_id=KEY[3], status="available",
        imported_at=datetime.now(),
    )
    values.update(kw)
    c = InventoryCard(**values)
    session.add(c)
    session.flush()
    return c


def order_with_line(session, *, status="short", external=None):
    o = SalesOrder(external_order_id=external or f"ord-{id(session)}-{status}",
                   status=status)
    session.add(o)
    session.flush()
    it = OrderItem(
        order_id=o.id, name="Alpha", mtgjson_id=KEY[0], language_id=KEY[1],
        condition_id=KEY[2], finish_id=KEY[3], quantity=1,
        set_code="ONE", collector_number="1",
    )
    session.add(it)
    session.flush()
    return o, it


def existing_allocation(session, card_obj, *, status, order=None, item=None):
    if item is None:
        order, item = order_with_line(session, status="shipped",
                                      external=f"old-{card_obj.id}-{status}")
    a = PickAllocation(
        order_item_id=item.id, inventory_card_id=card_obj.id,
        batch_id=card_obj.batch_id, status=status,
    )
    session.add(a)
    session.flush()
    return a, order, item


# --- the blocker is gone -------------------------------------------------

def test_a_card_with_an_EXCEPTION_row_can_be_allocated_again(session):
    """★ Order 4279's exact shape: card 6688 / allocation 445."""
    c = card(session)
    old, _o, _i = existing_allocation(session, c, status="exception")
    order, item = order_with_line(session)

    result = allocate_order(session, order)

    assert sum(r["allocated"] for r in result["line_results"]) == 1
    assert c.status == "reserved"
    # The audited row is untouched.
    assert old.status == "exception"
    assert session.query(PickAllocation).filter(
        PickAllocation.inventory_card_id == c.id).count() == 2


def test_a_card_with_a_RELEASED_row_can_be_allocated_again(session):
    c = card(session)
    old, _o, _i = existing_allocation(session, c, status="released")
    order, item = order_with_line(session)

    assert sum(r["allocated"] for r in allocate_order(session, order)["line_results"]) == 1
    assert c.status == "reserved"
    assert old.status == "released"


def test_a_card_with_a_SHIPPED_row_can_be_allocated_again(session):
    c = card(session)
    old, _o, _i = existing_allocation(session, c, status="shipped")
    order, item = order_with_line(session)

    assert sum(r["allocated"] for r in allocate_order(session, order)["line_results"]) == 1
    assert old.status == "shipped"


# --- but one ACTIVE claim per card is still enforced BY THE DATABASE -----

@pytest.mark.parametrize("second_status", ["allocated", "picked", "packed"])
def test_two_ACTIVE_allocations_for_one_card_are_refused_by_the_database(
    session, second_status,
):
    """★ Not a service-layer check -- the partial unique index itself."""
    c = card(session)
    existing_allocation(session, c, status="allocated")
    _order, item = order_with_line(session)

    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=c.id,
        batch_id=c.batch_id, status=second_status,
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_many_FINISHED_allocations_for_one_card_are_allowed(session):
    c = card(session)
    for status in ("exception", "released", "shipped", "released"):
        existing_allocation(session, c, status=status)
    assert session.query(PickAllocation).filter(
        PickAllocation.inventory_card_id == c.id).count() == 4


def test_the_index_is_partial_and_unique_on_a_fresh_database(tmp_path):
    """create_all alone must produce the partial index, so a brand-new
    database and production end up in the same shape."""
    path = tmp_path / "fresh.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
    conn = sqlite3.connect(path)
    indexes = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='pick_allocations' AND sql IS NOT NULL"
    ))
    conn.close()
    active = indexes[ACTIVE_ALLOCATION_INDEX].upper()
    assert "UNIQUE" in active
    assert "WHERE" in active and "ALLOCATED" in active and "PICKED" in active and "PACKED" in active
    # The plain lookup index must NOT be unique any more.
    assert "UNIQUE" not in indexes[LEGACY_ALLOCATION_CARD_INDEX].upper()


# --- the migration on a live database -----------------------------------

def _legacy_database(path):
    """A pre-migration database: the real table shape, with the OLD
    unconditional unique index."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.execute(f"DROP INDEX {ACTIVE_ALLOCATION_INDEX}")
    conn.execute(f"DROP INDEX {LEGACY_ALLOCATION_CARD_INDEX}")
    conn.execute(
        f"CREATE UNIQUE INDEX {LEGACY_ALLOCATION_CARD_INDEX} "
        "ON pick_allocations (inventory_card_id)"
    )
    conn.commit()
    conn.close()
    return engine


def _indexes(path):
    conn = sqlite3.connect(path)
    rows = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='pick_allocations' AND sql IS NOT NULL"
    ))
    conn.close()
    return rows


def test_migration_converts_the_unconditional_unique_index(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    engine = _legacy_database(path)
    assert "UNIQUE" in _indexes(path)[LEGACY_ALLOCATION_CARD_INDEX].upper()
    assert ACTIVE_ALLOCATION_INDEX not in _indexes(path)

    monkeypatch.setattr(database, "engine", engine)
    initialize_database()

    after = _indexes(path)
    assert "UNIQUE" not in after[LEGACY_ALLOCATION_CARD_INDEX].upper()
    assert "UNIQUE" in after[ACTIVE_ALLOCATION_INDEX].upper()
    assert "WHERE" in after[ACTIVE_ALLOCATION_INDEX].upper()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


def test_migration_is_idempotent_on_re_run(tmp_path, monkeypatch):
    path = tmp_path / "twice.db"
    engine = _legacy_database(path)
    monkeypatch.setattr(database, "engine", engine)
    initialize_database()
    first = _indexes(path)
    initialize_database()
    initialize_database()
    assert _indexes(path) == first


def test_migration_preserves_existing_rows(tmp_path, monkeypatch):
    path = tmp_path / "rows.db"
    engine = _legacy_database(path)
    with Session(engine) as s:
        c = card(s)
        a, _o, _i = existing_allocation(s, c, status="exception")
        s.commit()
        kept = (a.id, a.status, a.inventory_card_id)

    monkeypatch.setattr(database, "engine", engine)
    initialize_database()

    with Session(engine) as s:
        a = s.get(PickAllocation, kept[0])
        assert (a.id, a.status, a.inventory_card_id) == kept


def test_migration_REFUSES_when_a_card_already_holds_two_active_rows(
    tmp_path, monkeypatch, caplog,
):
    """The partial index could not be created, and dropping the old one
    first would leave the table with NO uniqueness at all. So it must
    refuse and leave the unconditional index in place.

    Manufacturing this state needs a little care: the duplicate rows have
    to be inserted with no unique index present, and the index row is then
    rewritten to read as unconditional-unique -- which is exactly the shape
    the migration inspects.
    """
    path = tmp_path / "dup.db"
    engine = _legacy_database(path)

    conn = sqlite3.connect(path)
    conn.execute(f"DROP INDEX {LEGACY_ALLOCATION_CARD_INDEX}")
    conn.commit()
    conn.close()

    with Session(engine) as s:
        c = card(s)
        _order, item = order_with_line(s)
        for _ in range(2):
            s.add(PickAllocation(
                order_item_id=item.id, inventory_card_id=c.id,
                batch_id=c.batch_id, status="allocated",
            ))
        s.commit()

    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE INDEX {LEGACY_ALLOCATION_CARD_INDEX} "
        "ON pick_allocations (inventory_card_id)"
    )
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'index' AND name = ?",
        (f"CREATE UNIQUE INDEX {LEGACY_ALLOCATION_CARD_INDEX} "
         "ON pick_allocations (inventory_card_id)",
         LEGACY_ALLOCATION_CARD_INDEX),
    )
    conn.execute("PRAGMA writable_schema=OFF")
    conn.commit()
    conn.close()
    assert "UNIQUE" in _indexes(path)[LEGACY_ALLOCATION_CARD_INDEX].upper()

    cardfoundry_logger = logging.getLogger("cardfoundry")
    caplog.set_level(logging.ERROR, logger="cardfoundry")
    cardfoundry_logger.addHandler(caplog.handler)
    try:
        monkeypatch.setattr(database, "engine", engine)
        database._migrate_pick_allocation_card_uniqueness()
    finally:
        cardfoundry_logger.removeHandler(caplog.handler)

    after = _indexes(path)
    assert ACTIVE_ALLOCATION_INDEX not in after, "must not create the partial index"
    assert "UNIQUE" in after[LEGACY_ALLOCATION_CARD_INDEX].upper(), \
        "must leave the old index alone rather than half-migrate"
    assert "more than one active allocation" in caplog.text


# --- uncancel_order ------------------------------------------------------

def test_uncancel_still_restores_a_released_allocation(session):
    c = card(session, status="available")
    order, item = order_with_line(session, status="cancelled")
    order.cancelled_from_status = "ready_to_pick"
    a = PickAllocation(
        order_item_id=item.id, inventory_card_id=c.id, batch_id=c.batch_id,
        status="released", released_from_status="allocated",
    )
    session.add(a)
    session.flush()

    reclaimed = uncancel_order(session, order)

    assert [x.id for x in reclaimed] == [c.id]
    assert c.status == "reserved"
    assert a.status == "allocated"
    assert a.released_from_status is None
    assert order.status == "ready_to_pick"


def test_uncancel_REFUSES_when_the_card_was_re_allocated_elsewhere(session):
    """★ The case the new index makes reachable: the card was released by
    a cancellation and has since gone to a DIFFERENT order.

    It must refuse, all-or-nothing, and it already does -- the guard keys
    on the card no longer being `available`, not on allocation-row
    uniqueness. Two orders both believing they hold one physical card is
    exactly what must not happen, and the operator resolves it by hand.
    """
    c = card(session, status="available")
    cancelled, cancelled_item = order_with_line(session, status="cancelled",
                                                external="cancelled-1")
    cancelled.cancelled_from_status = "ready_to_pick"
    released = PickAllocation(
        order_item_id=cancelled_item.id, inventory_card_id=c.id,
        batch_id=c.batch_id, status="released", released_from_status="allocated",
    )
    session.add(released)
    session.flush()

    # The card moves on to a new order -- now possible, and correct.
    new_order, _new_item = order_with_line(session, external="new-1")
    assert sum(r["allocated"] for r in allocate_order(session, new_order)["line_results"]) == 1
    assert c.status == "reserved"

    with pytest.raises(InventoryAllocationError, match="not available"):
        uncancel_order(session, cancelled)

    # Nothing half-done: the released row is still released.
    assert released.status == "released"
    assert released.released_from_status == "allocated"


# --- the audit sites from step 1, pinned ---------------------------------

def test_sold_at_uses_the_SHIPPED_allocation_not_just_any(session):
    """★ Pins the main.py fix. This was already wrong before the index
    changed: card 6688 is AVAILABLE and its only allocation is an
    "exception" row on order 3877, which shipped -- so the portal showed
    an available card a sold date."""
    available = card(session, status="available")
    shipped_order, shipped_item = order_with_line(session, status="shipped",
                                                  external="shipped-1")
    shipped_order.shipped_at = datetime(2026, 8, 26, 20, 56, 17)
    session.add(PickAllocation(
        order_item_id=shipped_item.id, inventory_card_id=available.id,
        batch_id=available.batch_id, status="exception",
    ))
    session.flush()

    assert main._sold_at_by_card_id(session, [available.id]) == {}, \
        "an available card whose only allocation is an exception has not sold"


def test_sold_at_reports_the_shipped_allocations_order(session):
    sold = card(session, status="sold")
    order, item = order_with_line(session, status="shipped", external="shipped-2")
    order.shipped_at = datetime(2026, 9, 1, 12, 0, 0)
    session.add(PickAllocation(
        order_item_id=item.id, inventory_card_id=sold.id,
        batch_id=sold.batch_id, status="shipped",
    ))
    session.flush()

    assert main._sold_at_by_card_id(session, [sold.id]) == {
        sold.id: datetime(2026, 9, 1, 12, 0, 0)
    }


def test_sold_at_ignores_an_older_finished_row_on_a_sold_card(session):
    """A card with BOTH an old released row (cancelled order, no
    shipped_at) and the real shipped row must report the shipped one."""
    sold = card(session, status="sold")
    old_order, old_item = order_with_line(session, status="cancelled",
                                          external="cancelled-2")
    session.add(PickAllocation(
        order_item_id=old_item.id, inventory_card_id=sold.id,
        batch_id=sold.batch_id, status="released", released_from_status="allocated",
    ))
    new_order, new_item = order_with_line(session, status="shipped",
                                          external="shipped-3")
    new_order.shipped_at = datetime(2026, 9, 20, 8, 0, 0)
    session.add(PickAllocation(
        order_item_id=new_item.id, inventory_card_id=sold.id,
        batch_id=sold.batch_id, status="shipped",
    ))
    session.flush()

    assert main._sold_at_by_card_id(session, [sold.id]) == {
        sold.id: datetime(2026, 9, 20, 8, 0, 0)
    }


def test_unpriced_shipped_cards_uses_the_shipped_allocation(session):
    """Pins the backfill_shipped_sold_price fix: with an older finished
    row present it must return the card once, against the order that
    actually shipped it."""
    from backfill_shipped_sold_price import find_unpriced_shipped_cards

    sold = card(session, status="sold", sold_price=None)
    old_order, old_item = order_with_line(session, status="cancelled",
                                          external="cancelled-3")
    old_order.source = "manapool"
    session.add(PickAllocation(
        order_item_id=old_item.id, inventory_card_id=sold.id,
        batch_id=sold.batch_id, status="released", released_from_status="allocated",
    ))
    real_order, real_item = order_with_line(session, status="shipped",
                                            external="shipped-4")
    real_order.source = "manapool"
    session.add(PickAllocation(
        order_item_id=real_item.id, inventory_card_id=sold.id,
        batch_id=sold.batch_id, status="shipped",
    ))
    session.flush()

    rows = find_unpriced_shipped_cards(session)
    assert len(rows) == 1
    _card_row, item_row, order_row = rows[0]
    assert order_row.id == real_order.id
    assert item_row.id == real_item.id
