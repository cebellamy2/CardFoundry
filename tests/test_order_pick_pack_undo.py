import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, PickWave, PickWaveOrder
from order_service import (
    InventoryAllocationError,
    mark_packed,
    mark_picked,
    unmark_packed,
    unmark_picked,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pick-pack-undo.db'}")
    Base.metadata.create_all(engine)
    return engine


# CF-UNDO-002 item 1: unpick / unpack.

def test_unmark_picked_reverts_allocation_and_order(db):
    with Session(db) as session:
        order, item, card, allocation = seed(
            session, order_status="picked", allocation_status="picked",
        )
        order.picked_at = None
        session.commit()
        unmark_picked(session, order)
        session.commit()
        assert order.status == "ready_to_pick"
        assert order.picked_at is None
        assert allocation.status == "allocated"


def test_unmark_picked_refused_unless_picked(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="ready_to_pick")
        with pytest.raises(InventoryAllocationError, match="not picked"):
            unmark_picked(session, order)


def test_unmark_picked_refused_for_wave_linked_order_even_after_wave_closed(db):
    with Session(db) as session:
        order, item, card, allocation = seed(
            session, order_status="picked", allocation_status="picked",
        )
        wave = PickWave(label="Wave 1", status="completed")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="closed"))
        session.commit()
        with pytest.raises(InventoryAllocationError, match="pick wave"):
            unmark_picked(session, order)
        assert order.status == "picked"
        assert allocation.status == "picked"


def test_unmark_picked_leaves_exceptioned_allocations_untouched(db):
    with Session(db) as session:
        order, item, card, allocation = seed(
            session, order_status="picked", allocation_status="picked",
        )
        # A second line on the same order whose allocation has since
        # moved to "exception" (a fulfillment exception reported after
        # picking) -- unmark_picked must only touch allocations still
        # exactly "picked", never one already tracked elsewhere.
        allocation.status = "exception"
        session.commit()
        unmark_picked(session, order)
        session.commit()
        assert order.status == "ready_to_pick"
        assert allocation.status == "exception"


def test_unmark_packed_reverts_allocation_and_order(db):
    with Session(db) as session:
        order, item, card, allocation = seed(
            session, order_status="packed", allocation_status="packed",
        )
        session.commit()
        unmark_packed(session, order)
        session.commit()
        assert order.status == "picked"
        assert order.packed_at is None
        assert allocation.status == "picked"


def test_unmark_packed_refused_unless_packed(db):
    with Session(db) as session:
        order, item, card, allocation = seed(
            session, order_status="picked", allocation_status="picked",
        )
        with pytest.raises(InventoryAllocationError, match="not packed"):
            unmark_packed(session, order)


def test_unmark_packed_works_regardless_of_pick_wave_origin(db):
    # Packing never touches pick-wave state, so a wave-picked order
    # (unlike the picked-status case above) unpacks freely.
    with Session(db) as session:
        order, item, card, allocation = seed(
            session, order_status="packed", allocation_status="packed",
        )
        wave = PickWave(label="Wave 1", status="completed")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="closed"))
        session.commit()
        unmark_packed(session, order)
        session.commit()
        assert order.status == "picked"
        assert allocation.status == "picked"


def test_mark_then_unmark_round_trips_back_to_original_state(db):
    with Session(db) as session:
        order, item, card, allocation = seed(session, order_status="ready_to_pick")
        mark_picked(session, order)
        session.commit()
        assert order.status == "picked" and allocation.status == "picked"
        mark_packed(session, order)
        session.commit()
        assert order.status == "packed" and allocation.status == "packed"
        unmark_packed(session, order)
        session.commit()
        assert order.status == "picked" and allocation.status == "picked"
        unmark_picked(session, order)
        session.commit()
        assert order.status == "ready_to_pick" and allocation.status == "allocated"
