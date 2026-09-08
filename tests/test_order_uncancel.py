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


def test_uncancel_refused_if_wave_completed_since_cancellation(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="in_pick_wave")
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        session.commit()
        release_order(session, order)
        session.commit()
        assert order.cancelled_from_status == "in_pick_wave"
        # The wave completes around this (already cancelled, so skipped
        # by complete_pick_wave's own status guard) order in the meantime.
        wave.status = "completed"
        membership = session.query(PickWaveOrder).filter(
            PickWaveOrder.order_id == order.id,
        ).one()
        membership.status = "closed"
        session.commit()
        with pytest.raises(InventoryAllocationError, match="pick wave has since completed"):
            uncancel_order(session, order)
        session.rollback()
        assert order.status == "cancelled"


def test_uncancel_succeeds_if_wave_membership_still_active(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="in_pick_wave")
        wave = PickWave(label="Wave 1", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        session.commit()
        release_order(session, order)
        session.commit()
        uncancel_order(session, order)
        session.commit()
        assert order.status == "in_pick_wave"
        assert allocation.status == "allocated"
