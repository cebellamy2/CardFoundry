import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_invariants import fulfillment_unit_counts
from fulfillment_exception_service import mark_fulfillment_exception
from fulfillment_exception_submission_service import confirm_fulfillment_exception_submitted
from models import Base, FulfillmentException, PickAllocation
from order_service import (
    InventoryAllocationError, mark_packed, mark_picked, mark_shipped, release_order,
)
from pick_wave_service import cancel_pick_wave, complete_pick_wave, get_wave_picklist
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'progression.db'}")
    Base.metadata.create_all(engine)
    return engine


def exception_order(session, *, submitted=False, status="in_pick_wave"):
    order, item, card, allocation = seed(session, order_status=status)
    exception = mark_fulfillment_exception(session, allocation.id, "missing", "not found")
    session.commit()
    if submitted:
        confirm_fulfillment_exception_submitted(session, exception.id, "reported")
        session.commit()
    return order, item, card, allocation, exception


def test_needs_submission_blocks_progression_but_submitted_allows_it(db):
    with Session(db) as session:
        order, _, card, allocation, exception = exception_order(session)
        with pytest.raises(InventoryAllocationError):
            mark_picked(session, order)
        assert order.status == "in_pick_wave"
        assert allocation.status == "exception"
        exception.submission_state = "submitted"
        session.commit()
        mark_picked(session, order)
        assert order.status == "picked"
        assert card.status == "removed"


def test_submitted_exception_allows_packing_and_shipping_without_replacement(db):
    with Session(db) as session:
        order, _, card, allocation, exception = exception_order(session, submitted=True, status="in_pick_wave")
        mark_picked(session, order)
        mark_packed(session, order)
        mark_shipped(session, order, "TRACK")
        assert order.status == "shipped"
        assert allocation.status == "exception"
        assert card.status == "removed"
        assert session.query(PickAllocation).count() == 1


def test_remote_and_inventory_resolution_do_not_reblock_submitted_exception(db):
    with Session(db) as session:
        order, _, _, _, exception = exception_order(session, submitted=True)
        exception.remote_resolution_state = "awaiting"
        exception.inventory_resolution_state = "unresolved"
        session.commit()
        mark_picked(session, order)
        assert order.status == "picked"


def test_multi_quantity_counts_two_physical_and_one_exception(db):
    with Session(db) as session:
        order, item, card, allocation, exception = exception_order(session)
        item.quantity = 3
        session.commit()
        sibling1 = seed(session)[2:4]
        sibling2 = seed(session)[2:4]
        for sibling_card, sibling_alloc in (sibling1, sibling2):
            sibling_alloc.order_item_id = item.id
        session.commit()
        sibling1[1].status = "picked"; sibling2[1].status = "picked"
        session.commit()
        counts = fulfillment_unit_counts(
            3, [sibling1[1], sibling2[1], allocation], [exception],
        )
        assert counts["requested_units"] == 3
        assert counts["picked_physical_units"] == 2
        assert counts["needs_submission_exception_units"] == 1
        assert counts["order_side_satisfied_units"] == 2
        confirm_fulfillment_exception_submitted(session, exception.id, "reported")
        session.commit()
        counts = fulfillment_unit_counts(3, [sibling1[1], sibling2[1], allocation], [exception])
        assert counts["order_side_satisfied_units"] == 3
        assert item.quantity == 3


def test_two_exceptions_require_both_submitted(db):
    with Session(db) as session:
        order, item, _, first_alloc, first = exception_order(session)
        _, _, second_card, second_alloc = seed(session)
        second_alloc.order_item_id = item.id
        session.commit()
        second = mark_fulfillment_exception(session, second_alloc.id, "missing", "not found")
        session.commit()
        with pytest.raises(InventoryAllocationError):
            mark_picked(session, order)
        confirm_fulfillment_exception_submitted(session, first.id, "reported")
        session.commit()
        with pytest.raises(InventoryAllocationError):
            mark_picked(session, order)
        confirm_fulfillment_exception_submitted(session, second.id, "reported")
        session.commit()
        mark_picked(session, order)
        assert order.status == "picked"
        assert second_card.status == "removed"


def test_wave_completion_preserves_blocked_order_and_exception_allocation(db):
    from models import PickWave, PickWaveOrder
    with Session(db) as session:
        order, _, _, allocation, _ = exception_order(session)
        wave = PickWave(label="wave", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id)); session.commit()
        newly_picked = complete_pick_wave(session, wave)
        assert wave.status == "completed"
        assert order.status == "in_pick_wave"
        assert allocation.status == "exception"
        assert newly_picked == []


def test_wave_completion_return_value_excludes_blocked_orders(db):
    """complete_pick_wave's return value is what a caller should notify
    Mana Pool about -- it must only include orders that actually reached
    "picked" in this call, not full wave membership."""
    from models import PickWave, PickWaveOrder
    with Session(db) as session:
        blocked_order, _, _, _, _ = exception_order(session)
        clean_order, _, _, _ = seed(session, order_status="in_pick_wave")
        wave = PickWave(label="wave", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=blocked_order.id))
        session.add(PickWaveOrder(wave_id=wave.id, order_id=clean_order.id))
        session.commit()
        newly_picked = complete_pick_wave(session, wave)
        assert [order.id for order in newly_picked] == [clean_order.id]
        assert blocked_order.status == "in_pick_wave"
        assert clean_order.status == "picked"


def test_wave_picklist_and_cancel_do_not_touch_exception(db):
    from models import PickWave, PickWaveOrder
    with Session(db) as session:
        order, _, card, allocation, _ = exception_order(session)
        wave = PickWave(label="wave", status="active")
        session.add(wave); session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id)); session.commit()
        assert get_wave_picklist(session, wave.id) == {}
        cancel_pick_wave(session, wave)
        assert allocation.status == "exception"
        assert card.status == "removed"


def test_release_order_leaves_exception_card_and_releases_unaffected_allocations(db):
    with Session(db) as session:
        order, item, card, exception_alloc, _ = exception_order(session)
        _, _, sibling_card, sibling_alloc = seed(session)
        sibling_alloc.order_item_id = item.id
        session.commit()
        release_order(session, order)
        assert exception_alloc.status == "exception"
        assert card.status == "removed"
        assert sibling_alloc.status == "released"
        assert sibling_card.status == "available"
