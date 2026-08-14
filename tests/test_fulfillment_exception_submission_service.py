import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_invariants import order_has_fulfillment_submission_block
from fulfillment_exception_service import FulfillmentExceptionError, mark_fulfillment_exception
from fulfillment_exception_submission_service import confirm_fulfillment_exception_submitted
from models import Base, FulfillmentExceptionEvent
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'submission.db'}")
    Base.metadata.create_all(engine)
    return engine


def create_exception(session, *, order_status="in_pick_wave"):
    order, item, card, allocation = seed(session, order_status=order_status)
    exception = mark_fulfillment_exception(session, allocation.id, "missing", "Card missing")
    session.commit()
    return order, item, card, allocation, exception


def test_submission_transition_default_and_custom_notes_preserves_original(db):
    with Session(db) as session:
        order, _, card, allocation, exception = create_exception(session)
        original_note = exception.note
        confirm_fulfillment_exception_submitted(session, exception.id)
        session.commit()
        assert exception.submission_state == "submitted"
        assert exception.submitted_at is not None
        assert exception.note == original_note
        event = session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception.id,
            event_type="fulfillment_exception_submitted",
        ).one()
        assert event.note.startswith("Exception submitted to ManaPool — ")
        assert allocation.status == "exception"
        assert card.status == "removed"
        assert order.status == "in_pick_wave"

    with Session(db) as session:
        _, _, _, _, exception = create_exception(session)
        confirm_fulfillment_exception_submitted(session, exception.id, "Submitted manually in ManaPool")
        session.commit()
        event = session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception.id,
            event_type="fulfillment_exception_submitted",
        ).one()
        assert event.note == "Submitted manually in ManaPool"


def test_submission_replay_is_idempotent_and_derived_block_clears(db):
    with Session(db) as session:
        order, _, _, _, exception = create_exception(session)
        assert order_has_fulfillment_submission_block([exception])
        first = confirm_fulfillment_exception_submitted(session, exception.id, "submitted")
        session.commit()
        second = confirm_fulfillment_exception_submitted(session, exception.id, "different")
        assert first.id == second.id
        assert not order_has_fulfillment_submission_block([exception])
        assert session.query(FulfillmentExceptionEvent).filter_by(
            event_type="fulfillment_exception_submitted",
        ).count() == 1


def test_two_exceptions_clear_block_only_after_both_submitted(db):
    with Session(db) as session:
        order, item, _, first_alloc, first = create_exception(session)
        _, _, sibling, second_alloc = seed(session)
        # Reuse the same order/item for a second physical unit.
        second_alloc.order_item_id = item.id
        second_alloc.inventory_card_id = sibling.id
        session.commit()
        second = mark_fulfillment_exception(session, second_alloc.id, "missing", "Second missing")
        session.commit()
        assert order_has_fulfillment_submission_block([first, second])
        confirm_fulfillment_exception_submitted(session, first.id, "first submitted")
        session.commit()
        assert order_has_fulfillment_submission_block([second])
        confirm_fulfillment_exception_submitted(session, second.id, "second submitted")
        session.commit()
        assert not order_has_fulfillment_submission_block([first, second])


@pytest.mark.parametrize("resolved", [False, True])
def test_submission_is_independent_of_inventory_and_remote_resolution(db, resolved):
    with Session(db) as session:
        _, _, card, _, exception = create_exception(session)
        if resolved:
            card.inventory_exception_state = "none"
            exception.inventory_resolution_state = "resolved"
        exception.remote_resolution_state = "resolved_refunded"
        session.commit()
        confirm_fulfillment_exception_submitted(session, exception.id)
        session.commit()
        assert exception.submission_state == "submitted"
        assert exception.inventory_resolution_state == ("resolved" if resolved else "unresolved")
        assert exception.remote_resolution_state == "resolved_refunded"


def test_inconsistent_linkage_rolls_back_submission(db):
    with Session(db) as session:
        _, item, _, _, exception = create_exception(session)
        item.order_id = 9999
        session.commit()
        with pytest.raises(FulfillmentExceptionError):
            confirm_fulfillment_exception_submitted(session, exception.id)
        session.rollback()
        session.refresh(exception)
        assert exception.submission_state == "needs_submission"
        assert session.query(FulfillmentExceptionEvent).filter_by(
            event_type="fulfillment_exception_submitted",
        ).count() == 0
