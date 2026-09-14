"""Tests for the one-off correction that closed fulfillment exception #17.

The script writes to a single hand-identified row, so the property that
matters is not what it changes but what it REFUSES to change. Every guard
is a re-read of live state at run time, because the investigation's
snapshot is hours old by the time anyone runs it.

Pinned in particular: the script must refuse when the card and the order
line turn out to AGREE. That is exception #37's case -- a mark filed in
error -- and the right action there is a revert, not an unfulfillable
close-out. Closing a false positive as "could not be fulfilled" would
write a permanent lie into the audit trail.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import fix_exception_17_fire_crystal as fix
from fulfillment_exception_service import mark_fulfillment_exception
from fulfillment_exception_submission_service import (
    confirm_fulfillment_exception_submitted,
)
from models import (
    Base, FulfillmentException, FulfillmentExceptionEvent, InventoryCard,
    InventoryChangeLog, PickAllocation,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture(autouse=True)
def restore_module_constants():
    """The script hardcodes the row it corrects, so these tests have to
    repoint it at their own fixture rows. Restore them afterwards rather
    than leaving a mutated module behind for whatever imports it next."""
    original = (fix.EXCEPTION_ID, fix.CARD_ID, fix.ALLOCATION_ID, fix.ORDER_ID)
    yield
    (fix.EXCEPTION_ID, fix.CARD_ID, fix.ALLOCATION_ID, fix.ORDER_ID) = original


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fix17.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(fix, "engine", engine)
    return engine


def build(session, *, card_language="JA", item_language="EN", card_status="available"):
    """Recreate exception #17's exact shape: a submitted inventory_mismatch
    whose card has been manually corrected and returned to sellable."""
    order, item, card, allocation = seed(session)
    exception = mark_fulfillment_exception(session, allocation.id, "inventory_mismatch")
    session.flush()
    confirm_fulfillment_exception_submitted(session, exception.id, "Reported")
    session.flush()

    # what the operator did by hand: corrected the printing and relisted,
    # which cleared the quarantine reason the type resolver needs
    card.status = card_status
    card.unsellable_reason = None
    card.removal_reason = None
    card.language_id = card_language
    item.language_id = item_language
    session.commit()

    monkeypatch_ids(exception, card, allocation, order)
    return exception, order, item, allocation, card


def monkeypatch_ids(exception, card, allocation, order):
    fix.EXCEPTION_ID = exception.id
    fix.CARD_ID = card.id
    fix.ALLOCATION_ID = allocation.id
    fix.ORDER_ID = order.id


def set_remote_replaced(session, order):
    order.remote_fulfillment_status = "replaced"
    session.commit()


# --- refusals -----------------------------------------------------------

def test_refuses_when_the_card_and_order_line_actually_agree(db):
    """Exception #37's case. Agreeing identities mean the mark was filed
    in error, so this must abort rather than record it as unfulfillable."""
    with Session(db) as session:
        _, order, _, _, _ = build(session, card_language="JA", item_language="JA")
        set_remote_replaced(session, order)

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify_preconditions(session)


def test_refuses_when_the_card_is_not_available(db):
    """A card still quarantined is reachable by the real type resolver,
    so this one-off has no business touching it."""
    with Session(db) as session:
        _, order, _, _, _ = build(session, card_status="unsellable")
        set_remote_replaced(session, order)

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify_preconditions(session)


def test_refuses_when_the_exception_is_already_resolved(db):
    with Session(db) as session:
        exception, order, _, _, _ = build(session)
        set_remote_replaced(session, order)
        exception.inventory_resolution_state = "resolved"
        session.commit()

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify_preconditions(session)


def test_refuses_when_mana_pool_never_replaced_the_order(db):
    with Session(db) as session:
        _, order, _, _, _ = build(session)
        order.remote_fulfillment_status = "shipped"
        session.commit()

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify_preconditions(session)


def test_refuses_a_second_run(db):
    """Re-running must not write a second event or a second audit row."""
    with Session(db) as session:
        _, order, _, _, _ = build(session)
        set_remote_replaced(session, order)

    with Session(db) as session:
        exception, _, _, allocation, card = fix.verify_preconditions(session)
        fix.apply_fix(session, exception, allocation, card)
        session.commit()

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify_preconditions(session)

    with Session(db) as session:
        events = session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=fix.EXCEPTION_ID,
        ).all()
        assert len([e for e in events if e.new_state == "resolved"]) == 1


# --- what it writes -----------------------------------------------------

def test_closes_the_record_without_moving_any_inventory(db):
    with Session(db) as session:
        _, order, _, _, _ = build(session)
        set_remote_replaced(session, order)

    with Session(db) as session:
        exception, _, _, allocation, card = fix.verify_preconditions(session)
        fix.apply_fix(session, exception, allocation, card)
        session.commit()

    with Session(db) as session:
        exception = session.get(FulfillmentException, fix.EXCEPTION_ID)
        assert exception.inventory_resolution_state == "resolved"
        assert exception.inventory_resolved_at is not None
        assert exception.resolution_note.startswith("Closed as unfulfillable")
        # the remote side is NOT claimed to be settled -- Mana Pool never
        # reported a terminal outcome for this line
        assert exception.remote_resolution_state == "awaiting"

        card = session.get(InventoryCard, fix.CARD_ID)
        assert card.inventory_exception_state == "none"
        assert card.status == "available"      # live stock, untouched
        assert card.language_id == "JA"

        allocation = session.get(PickAllocation, fix.ALLOCATION_ID)
        assert allocation.status == "exception"  # matches Ticket C's 27


def test_audit_row_records_a_status_that_did_not_change(db):
    """previous_status and new_status are deliberately identical. The
    projection moved; the card did not."""
    with Session(db) as session:
        _, order, _, _, _ = build(session)
        set_remote_replaced(session, order)

    with Session(db) as session:
        exception, _, _, allocation, card = fix.verify_preconditions(session)
        fix.apply_fix(session, exception, allocation, card)
        session.commit()

    with Session(db) as session:
        logs = [
            l for l in session.query(InventoryChangeLog).filter_by(
                inventory_card_id=fix.CARD_ID,
            ).all()
            if "fulfillment_exception_inventory_resolved" in (l.change_summary or "")
        ]
        assert len(logs) == 1
        import json
        entry = json.loads(logs[0].change_summary)
        assert entry["previous_status"] == entry["new_status"] == "available"
        assert entry["previous_inventory_exception_state"] == "exception_unresolved"
        assert entry["new_inventory_exception_state"] == "none"
