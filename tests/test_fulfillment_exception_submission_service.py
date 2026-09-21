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
def test_submission_closes_the_inventory_record_and_leaves_remote_alone(db, resolved):
    """CF-AUTORESOLVE-001 (2026-09-21) replaces this test's predecessor,
    test_submission_is_independent_of_inventory_and_remote_resolution.
    That independence is exactly what the operator rule removes: once an
    exception is reported to Mana Pool, the inventory record is closed.

    What stays independent is the REMOTE side -- submission still says
    nothing about what Mana Pool decided, and must not touch it."""
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
        assert exception.inventory_resolution_state == "resolved"
        assert card.inventory_exception_state == "none"
        assert exception.remote_resolution_state == "resolved_refunded"


def test_auto_resolve_leaves_the_card_status_exactly_where_it_was(db):
    """The card's own status records what physically happened. A report to
    Mana Pool is evidence about the customer's order, not about the card."""
    with Session(db) as session:
        _, _, card, _, exception = create_exception(session)
        assert card.status == "removed"  # "missing" is set at RAISE time
        assert card.removal_reason == "fulfillment_missing"
        confirm_fulfillment_exception_submitted(session, exception.id)
        session.commit()
        assert exception.inventory_resolution_state == "resolved"
        assert card.status == "removed"
        assert card.removal_reason == "fulfillment_missing"
        assert card.inventory_exception_state == "none"


def test_auto_resolve_records_its_own_event_type_not_a_verified_resolution(db):
    """An auto-close must never read back as an operator-verified fix."""
    with Session(db) as session:
        _, _, _, _, exception = create_exception(session)
        confirm_fulfillment_exception_submitted(session, exception.id)
        session.commit()
        events = {e.event_type for e in session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception.id).all()}
        assert "fulfillment_exception_auto_resolved_on_submission" in events
        assert "fulfillment_exception_inventory_resolved" not in events
        event = session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception.id,
            event_type="fulfillment_exception_auto_resolved_on_submission").one()
        assert event.previous_state == "unresolved"
        assert event.new_state == "resolved"
        assert "reported to Mana Pool" in event.note
        assert "Not an operator-verified inventory fix" in event.note


def test_auto_resolve_closes_an_exception_submitted_before_the_rule_existed(db):
    """The invariant is "submitted implies resolved" after any successful
    return, not only on the transition -- otherwise a pre-rule exception
    stays open forever no matter how many times submission runs."""
    from fulfillment_exception_resolution_service import auto_resolve_after_submission
    with Session(db) as session:
        _, _, card, _, exception = create_exception(session)
        confirm_fulfillment_exception_submitted(session, exception.id)
        session.commit()
        # Rewind to the pre-rule shape: submitted, but never closed.
        exception.inventory_resolution_state = "unresolved"
        card.inventory_exception_state = "exception_unresolved"
        session.commit()
        confirm_fulfillment_exception_submitted(session, exception.id)
        session.commit()
        assert exception.inventory_resolution_state == "resolved"
        assert card.inventory_exception_state == "none"


def test_auto_resolve_is_a_no_op_when_there_is_nothing_to_close(db):
    """Returns None rather than raising: a submission must not fail
    because its follow-on close was already done."""
    from fulfillment_exception_resolution_service import auto_resolve_after_submission
    with Session(db) as session:
        _, _, _, _, exception = create_exception(session)
        confirm_fulfillment_exception_submitted(session, exception.id)
        session.commit()
        assert auto_resolve_after_submission(session, exception.id) is None
        assert auto_resolve_after_submission(session, 999999) is None


def test_auto_resolve_refuses_an_exception_that_was_never_submitted(db):
    from fulfillment_exception_resolution_service import auto_resolve_after_submission
    with Session(db) as session:
        _, _, card, _, exception = create_exception(session)
        assert exception.submission_state == "needs_submission"
        assert auto_resolve_after_submission(session, exception.id) is None
        assert exception.inventory_resolution_state == "unresolved"
        assert card.inventory_exception_state == "exception_unresolved"


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
