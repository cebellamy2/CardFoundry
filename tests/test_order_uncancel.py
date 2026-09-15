import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, InventoryChangeLog, PickWave, PickWaveOrder
from order_service import InventoryAllocationError, release_order, uncancel_order
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'uncancel.db'}")
    Base.metadata.create_all(engine)
    return engine


# CF-UNDO-002 item 2: uncancel an order.

def test_release_order_captures_restore_snapshot(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="ready_to_pick")
        release_order(session, order)
        session.commit()
        assert order.status == "cancelled"
        assert order.cancelled_from_status == "ready_to_pick"
        assert allocation.status == "released"
        assert allocation.released_from_status == "allocated"
        assert card.status == "available"


def test_uncancel_restores_ready_to_pick_order_and_audits(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="ready_to_pick")
        release_order(session, order)
        session.commit()
        reclaimed = uncancel_order(session, order)
        session.commit()
        assert [c.id for c in reclaimed] == [card.id]
        assert order.status == "ready_to_pick"
        assert order.cancelled_from_status is None
        assert allocation.status == "allocated"
        assert allocation.released_from_status is None
        assert card.status == "reserved"
        log = session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == card.id,
        ).one()
        evidence = json.loads(log.change_summary)
        assert evidence["action_type"] == "uncancel_reclaim"
        assert evidence["previous_status"] == "available"
        assert evidence["restored_allocation_status"] == "allocated"


def test_uncancel_restores_picked_order_allocation_status(db):
    with Session(db) as session:
        order, item, card, allocation = seed(
            session, order_status="picked", allocation_status="picked",
        )
        release_order(session, order)
        session.commit()
        assert order.cancelled_from_status == "picked"
        assert allocation.released_from_status == "picked"
        uncancel_order(session, order)
        session.commit()
        assert order.status == "picked"
        assert allocation.status == "picked"


def test_uncancel_refused_unless_cancelled(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="ready_to_pick")
        with pytest.raises(InventoryAllocationError, match="not cancelled"):
            uncancel_order(session, order)


def test_uncancel_refused_without_a_snapshot(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="cancelled")
        # Simulates an order cancelled before this feature existed --
        # no cancelled_from_status was ever captured for it.
        with pytest.raises(InventoryAllocationError, match="No cancellation snapshot"):
            uncancel_order(session, order)


def test_uncancel_refused_all_or_nothing_if_a_card_moved_on(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="ready_to_pick")
        release_order(session, order)
        session.commit()
        # The released card was claimed by something else in the meantime.
        card.status = "sold"
        session.commit()
        with pytest.raises(InventoryAllocationError, match="not available"):
            uncancel_order(session, order)
        session.rollback()
        assert order.status == "cancelled"
        assert allocation.status == "released"


def test_cancelling_detaches_the_order_from_its_active_pick_wave(db):
    """Slice 1b. The membership used to be left "active" forever, occupying
    the DB-level one-active-wave-per-order slot, so an order uncancelled
    later could not join a new wave while it sat there. Cards already drop
    off the picklist on their own -- that query keys on allocation status,
    not order status -- so this is purely the membership row."""
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="in_pick_wave")
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        session.commit()

        release_order(session, order)
        session.commit()

        membership = session.query(PickWaveOrder).filter(
            PickWaveOrder.order_id == order.id,
        ).one()
        assert membership.status == "closed"
        assert wave.status == "active"          # the wave itself is untouched


def test_uncancelling_returns_the_order_to_ready_to_pick_not_the_old_wave(db):
    """Operator decision: an order coming back must NOT be re-attached to
    the wave it was cancelled out of. That wave has moved on, and its
    picklist may already be printed and worked. The order becomes pickable
    again and is free to join a NEW wave."""
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="in_pick_wave")
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        session.commit()

        release_order(session, order)
        session.commit()
        assert order.cancelled_from_status == "in_pick_wave"

        uncancel_order(session, order)
        session.commit()

        assert order.status == "ready_to_pick"
        assert allocation.status == "allocated"
        # and the slot is free, so a new wave can take it
        assert session.query(PickWaveOrder).filter(
            PickWaveOrder.order_id == order.id,
            PickWaveOrder.status == "active",
        ).count() == 0


def test_uncancel_no_longer_depends_on_the_old_wave_surviving(db):
    """The previous guard refused the uncancel outright once the wave had
    completed. Since cancelling now closes the membership itself, that
    guard would have refused EVERY in_pick_wave uncancel."""
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="in_pick_wave")
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        session.commit()

        release_order(session, order)
        session.commit()
        wave.status = "completed"
        session.commit()

        uncancel_order(session, order)
        session.commit()
        assert order.status == "ready_to_pick"
