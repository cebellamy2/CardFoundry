import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_resolution_service import resolve_missing_inventory_exception
from fulfillment_exception_service import mark_fulfillment_exception
from fulfillment_exception_submission_service import confirm_fulfillment_exception_submitted
from models import (Base, FulfillmentExceptionEvent, PickWave, PickWaveEvent,
                    PickWaveOrder)
from order_service import mark_packed, mark_shipped
from pick_wave_service import (
    PickWaveSelectionError,
    cancel_pick_wave,
    complete_pick_wave,
    reopen_pick_wave,
)
from tests.test_fulfillment_exception_progression import exception_order
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reopen.db'}")
    Base.metadata.create_all(engine)
    return engine


def completed_wave_with_orders(session, *, count=2, order_status="in_pick_wave"):
    orders = []
    wave = PickWave(label="wave", status="active")
    session.add(wave)
    session.flush()
    for _ in range(count):
        order, item, card, allocation = seed(session, order_status=order_status)
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        orders.append(order)
    session.commit()
    complete_pick_wave(session, wave)
    session.commit()
    return wave, orders


def test_reopen_reverts_wave_and_all_orders_when_everything_is_clean(db):
    with Session(db) as session:
        wave, orders = completed_wave_with_orders(session, count=3)
        order_ids = [order.id for order in orders]
        assert wave.status == "completed"
        assert all(order.status == "picked" for order in orders)

        reverted = reopen_pick_wave(session, wave)
        session.commit()

        assert wave.status == "active"
        assert wave.completed_at is None
        assert {order.id for order in reverted} == set(order_ids)
        for order in orders:
            assert order.status == "in_pick_wave"
            assert order.picked_at is None
        memberships = session.query(PickWaveOrder).filter(
            PickWaveOrder.wave_id == wave.id,
        ).all()
        assert all(m.status == "active" for m in memberships)


def test_reopen_writes_immutable_audit_event(db):
    with Session(db) as session:
        wave, orders = completed_wave_with_orders(session, count=2)
        reopen_pick_wave(session, wave, note="closed too early")
        session.commit()

        event = session.query(PickWaveEvent).filter(
            PickWaveEvent.pick_wave_id == wave.id,
        ).one()
        assert event.event_type == "reopened"
        assert event.note == "closed too early"
        assert "Mana Pool" in event.evidence_json
        assert str(orders[0].id) in event.evidence_json


def test_reopen_fails_closed_if_an_order_was_packed(db):
    with Session(db) as session:
        wave, orders = completed_wave_with_orders(session, count=2)
        mark_packed(session, orders[0])
        session.commit()

        with pytest.raises(PickWaveSelectionError, match="packed"):
            reopen_pick_wave(session, wave)

        session.rollback()
        assert wave.status == "completed"
        assert orders[0].status == "packed"
        assert orders[1].status == "picked"


def test_reopen_fails_closed_if_an_order_was_shipped(db):
    with Session(db) as session:
        wave, orders = completed_wave_with_orders(session, count=1)
        mark_packed(session, orders[0])
        mark_shipped(session, orders[0], "1Z999")
        session.commit()

        with pytest.raises(PickWaveSelectionError, match="shipped"):
            reopen_pick_wave(session, wave)

        session.rollback()
        assert wave.status == "completed"


def test_reopen_is_all_or_nothing_leaves_clean_orders_untouched_too(db):
    with Session(db) as session:
        wave, orders = completed_wave_with_orders(session, count=2)
        mark_packed(session, orders[0])
        session.commit()

        with pytest.raises(PickWaveSelectionError):
            reopen_pick_wave(session, wave)

        session.rollback()
        assert wave.status == "completed"
        assert orders[1].status == "picked"
        membership = session.query(PickWaveOrder).filter(
            PickWaveOrder.wave_id == wave.id,
            PickWaveOrder.order_id == orders[1].id,
        ).one()
        assert membership.status == "closed"


def _wave_around(session, order, *, label="wave"):
    """Put one order in a wave and complete it."""
    wave = PickWave(label=label, status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
    session.commit()
    complete_pick_wave(session, wave)
    session.commit()
    return wave


def test_reopen_fails_closed_if_an_OPERATOR_resolved_a_fulfillment_exception(db):
    """The operator-resolved case this test was written for.

    It previously reached that case via exception_order(submitted=True),
    which since CF-AUTORESOLVE-001 resolves the exception by itself --
    so resolve_missing_inventory_exception() below was a silent no-op and
    the test passed on the AUTO-resolve it was not trying to test. The
    exception is now left un-submitted, so the explicit resolve is the
    only thing that closes it and the test measures what it claims.
    """
    with Session(db) as session:
        order, _, card, allocation, exception = exception_order(session)
        assert exception.submission_state == "needs_submission"
        wave = _wave_around(session, order)

        resolved = resolve_missing_inventory_exception(
            session, exception.id, "accepted as lost",
        )
        session.commit()
        # the resolve genuinely did the work -- not a no-op this time
        assert resolved.inventory_resolution_state == "resolved"
        assert session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception.id,
            event_type="fulfillment_exception_inventory_resolved",
        ).count() == 1

        with pytest.raises(PickWaveSelectionError, match="fulfillment exception"):
            reopen_pick_wave(session, wave)


def test_submitting_an_exception_does_not_foreclose_reopening_its_wave(db):
    """CF-AUTORESOLVE-002 (2026-09-21), the regression this fixes.

    CF-AUTORESOLVE-001 made submission close the inventory record, and
    reopen's guard keyed on "resolved" -- so reporting one missing card
    silently made the whole wave un-undoable. The operator's standing
    universal-undo principle: there should be no risk in undoing
    something you did yourself.
    """
    with Session(db) as session:
        order, _, card, allocation, exception = exception_order(session, submitted=True)
        # submission closed it, with its own event type
        assert exception.submission_state == "submitted"
        assert exception.inventory_resolution_state == "resolved"
        assert session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception.id,
            event_type="fulfillment_exception_auto_resolved_on_submission",
        ).count() == 1

        wave = _wave_around(session, order)
        reverted = reopen_pick_wave(session, wave)
        session.commit()

        assert wave.status == "active"
        assert [o.id for o in reverted] == [order.id]
        assert order.status == "in_pick_wave"


def test_reopening_leaves_the_auto_resolved_exception_closed(db):
    """The deliberate asymmetry, stated as a test.

    The report to Mana Pool happened and a local reopen cannot un-send
    it. Rewinding only the inventory flag would recreate "submitted +
    unresolved" -- the state CF-AUTORESOLVE-001 made unreachable, which
    no button can close again. Reopen therefore rewinds the WAVE, not
    the exception: the card's raise-time disposition, the allocation and
    the submission all stay exactly as they were.
    """
    with Session(db) as session:
        order, _, card, allocation, exception = exception_order(session, submitted=True)
        wave = _wave_around(session, order)
        reopen_pick_wave(session, wave)
        session.commit()

        assert exception.inventory_resolution_state == "resolved"
        assert exception.inventory_resolved_at is not None
        assert exception.submission_state == "submitted"
        assert card.inventory_exception_state == "none"
        assert card.status == "removed"           # set when RAISED, never by resolution
        assert card.removal_reason == "fulfillment_missing"
        assert allocation.status == "exception"


def test_a_reopened_wave_can_be_completed_and_reopened_again(db):
    """The undo has to survive being exercised, not just succeed once."""
    with Session(db) as session:
        order, _, _, _, exception = exception_order(session, submitted=True)
        wave = _wave_around(session, order)

        reopen_pick_wave(session, wave)
        session.commit()
        assert wave.status == "active"

        complete_pick_wave(session, wave)
        session.commit()
        assert wave.status == "completed"

        reopen_pick_wave(session, wave)
        session.commit()
        assert wave.status == "active"
        assert order.status == "in_pick_wave"


def test_a_remote_outcome_still_blocks_reopen_even_when_auto_resolved(db):
    """Discounting the auto-close must not discount the OTHER half of the
    guard: once Mana Pool has reported an outcome, the wave has moved on
    regardless of how the inventory record got closed."""
    with Session(db) as session:
        order, _, _, _, exception = exception_order(session, submitted=True)
        wave = _wave_around(session, order)
        exception.remote_resolution_state = "resolved_refunded"
        session.commit()

        with pytest.raises(PickWaveSelectionError, match="fulfillment exception"):
            reopen_pick_wave(session, wave)


def test_reopen_fails_closed_if_remote_resolution_progressed(db):
    with Session(db) as session:
        order, _, card, allocation, exception = exception_order(session, submitted=True)
        wave = PickWave(label="wave", status="active")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        session.commit()
        complete_pick_wave(session, wave)
        session.commit()
        assert order.status == "picked"

        exception.remote_resolution_state = "resolved_refunded"
        session.commit()

        with pytest.raises(PickWaveSelectionError, match="fulfillment exception"):
            reopen_pick_wave(session, wave)


def test_reopen_succeeds_when_completion_left_an_order_blocked_in_pick_wave(db):
    """complete_pick_wave() can leave an order at in_pick_wave (not picked)
    if it was blocked by an open exception at completion time. Reopening
    such a wave must not choke on that order -- it's already at the
    target state, nothing to revert for it."""
    with Session(db) as session:
        blocked_order, _, _, _, _ = exception_order(session)
        clean_order, _, _, _ = seed(session, order_status="in_pick_wave")
        wave = PickWave(label="wave", status="active")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=blocked_order.id))
        session.add(PickWaveOrder(wave_id=wave.id, order_id=clean_order.id))
        session.commit()

        newly_picked = complete_pick_wave(session, wave)
        session.commit()
        assert [order.id for order in newly_picked] == [clean_order.id]
        assert blocked_order.status == "in_pick_wave"

        reverted = reopen_pick_wave(session, wave)
        session.commit()

        assert wave.status == "active"
        assert [order.id for order in reverted] == [clean_order.id]
        assert blocked_order.status == "in_pick_wave"
        assert clean_order.status == "in_pick_wave"
        memberships = {
            m.order_id: m.status
            for m in session.query(PickWaveOrder).filter(PickWaveOrder.wave_id == wave.id)
        }
        assert memberships[blocked_order.id] == "active"
        assert memberships[clean_order.id] == "active"


def test_reopen_requires_completed_wave(db):
    with Session(db) as session:
        order, _, _, _ = seed(session)
        wave = PickWave(label="wave", status="active")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        session.commit()

        with pytest.raises(PickWaveSelectionError, match="completed"):
            reopen_pick_wave(session, wave)


def test_reopen_requires_completed_not_cancelled_wave(db):
    with Session(db) as session:
        order, _, _, _ = seed(session)
        wave = PickWave(label="wave", status="active")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id))
        session.commit()
        cancel_pick_wave(session, wave)
        session.commit()

        with pytest.raises(PickWaveSelectionError, match="completed"):
            reopen_pick_wave(session, wave)


def test_reopened_wave_orders_are_selectable_for_a_new_wave_after_recompleting(db):
    """A reopened order can be picked again and re-completed cleanly --
    the reopen doesn't leave stray state that blocks normal progression."""
    with Session(db) as session:
        wave, orders = completed_wave_with_orders(session, count=1)
        reopen_pick_wave(session, wave)
        session.commit()

        newly_picked = complete_pick_wave(session, wave)
        session.commit()
        assert [order.id for order in newly_picked] == [orders[0].id]
        assert orders[0].status == "picked"
