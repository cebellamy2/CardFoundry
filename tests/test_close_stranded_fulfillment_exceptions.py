"""Tests for the closer that cleared the stranded exceptions.

The script's value is entirely in its classification, so that is what
these pin. It may close an exception as "unfulfillable" ONLY when the
card's identity genuinely differs from what the order line asked for. If
they agree, the mark was filed in error and the truthful close is a
revert -- writing "could not be fulfilled" instead would put a permanent
falsehood in the audit trail.

The trap this exists to avoid is pinned directly: for exceptions #5, #18
and #33 the scryfall_id matched the order line EXACTLY and only the
finish differed. A check that stopped at scryfall_id would have read all
three as false positives and closed them the wrong way.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import close_stranded_fulfillment_exceptions as closer
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, FulfillmentException, FulfillmentExceptionEvent, InventoryCard,
    InventoryChangeLog, OrderItem, PickAllocation,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'stranded.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(closer, "engine", engine)
    return engine


def build(session, *, card_finish="NF", item_finish="NF", card_status="removed",
          removal_reason="import_error"):
    order, item, card, allocation = seed(session)
    exception = mark_fulfillment_exception(session, allocation.id, "inventory_mismatch")
    session.flush()
    exception.submission_state = "submitted"
    # the manual edit that stranded it, applied directly: these rows
    # pre-date the v1.159.0 guard that now prevents exactly this
    card.status = card_status
    card.unsellable_reason = None
    card.removal_reason = removal_reason
    card.finish_id = card_finish
    item.finish_id = item_finish
    order.remote_fulfillment_status = "shipped"
    session.commit()
    return exception.id, card.id, allocation.id


# --- the classification that matters ------------------------------------

def test_refuses_when_identity_agrees_on_every_field(db):
    """#2's shape, and #37's. A script cannot know whether an agreeing
    identity means a false positive or something else, so it must not
    guess."""
    with Session(db) as session:
        exception_id, _, _ = build(session, card_finish="NF", item_finish="NF")

    with Session(db) as session:
        record = closer.classify(session, exception_id)
    assert record["ok"] is False
    assert "AGREES" in record["why"]
    assert "operator decision" in record["why"]


def test_a_finish_only_difference_is_still_a_real_mismatch(db):
    """The trap: #5, #18 and #33 all had an identical scryfall_id and
    differed only on finish. Comparing scryfall_id alone would have
    classified them as false positives."""
    with Session(db) as session:
        exception_id, card_id, _ = build(session, card_finish="EF", item_finish="NF")

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        card = session.get(InventoryCard, exception.inventory_card_id)
        item = session.get(OrderItem, exception.order_item_id)
        assert card.scryfall_id == item.scryfall_id, "precondition: scryfall ids match"
        record = closer.classify(session, exception_id)
    assert record["ok"] is True
    assert "finish_id" in record["differences"]


def test_refuses_an_already_resolved_exception(db):
    with Session(db) as session:
        exception_id, _, _ = build(session, card_finish="EF")
        session.get(FulfillmentException, exception_id).inventory_resolution_state = "resolved"
        session.commit()

    with Session(db) as session:
        assert closer.classify(session, exception_id)["ok"] is False


def test_refuses_an_exception_mana_pool_was_never_told_about(db):
    with Session(db) as session:
        exception_id, _, _ = build(session, card_finish="EF")
        session.get(FulfillmentException, exception_id).submission_state = "needs_submission"
        session.commit()

    with Session(db) as session:
        record = closer.classify(session, exception_id)
    assert record["ok"] is False
    assert "never told" in record["why"]


# --- what it writes -----------------------------------------------------

def test_closing_moves_no_inventory_and_no_allocation(db):
    with Session(db) as session:
        exception_id, card_id, allocation_id = build(session, card_finish="FO")

    with Session(db) as session:
        record = closer.classify(session, exception_id)
        assert record["ok"]
        closer.apply_close(session, record)
        session.commit()

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        assert exception.inventory_resolution_state == "resolved"
        assert exception.inventory_resolved_at is not None
        assert "unfulfillable" in exception.resolution_note
        card = session.get(InventoryCard, card_id)
        assert card.status == "removed"            # untouched
        assert card.removal_reason == "import_error"  # untouched
        assert card.inventory_exception_state == "none"
        assert session.get(PickAllocation, allocation_id).status == "exception"


def test_the_note_names_the_actual_difference(db):
    """A note that just says "unfulfillable" is unreadable a year later."""
    with Session(db) as session:
        exception_id, _, _ = build(session, card_finish="EF", item_finish="NF")

    with Session(db) as session:
        record = closer.classify(session, exception_id)
        note = closer.note_for(record)
    assert "finish_id requested 'NF' but the card is 'EF'" in note


def test_audit_rows_record_a_status_that_did_not_change(db):
    with Session(db) as session:
        exception_id, card_id, _ = build(session, card_finish="EF")

    with Session(db) as session:
        record = closer.classify(session, exception_id)
        closer.apply_close(session, record)
        session.commit()

    with Session(db) as session:
        events = [
            e for e in session.query(FulfillmentExceptionEvent)
            .filter_by(fulfillment_exception_id=exception_id).all()
            if e.new_state == "resolved"
        ]
        assert len(events) == 1
        import json
        logs = [
            l for l in session.query(InventoryChangeLog).filter_by(inventory_card_id=card_id).all()
            if "fulfillment_exception_inventory_resolved" in (l.change_summary or "")
        ]
        assert len(logs) == 1
        entry = json.loads(logs[0].change_summary)
        assert entry["previous_status"] == entry["new_status"] == "removed"


def test_rerunning_refuses_rather_than_double_writing(db):
    with Session(db) as session:
        exception_id, _, _ = build(session, card_finish="EF")

    with Session(db) as session:
        record = closer.classify(session, exception_id)
        closer.apply_close(session, record)
        session.commit()

    with Session(db) as session:
        assert closer.classify(session, exception_id)["ok"] is False
