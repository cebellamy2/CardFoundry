"""Tests for the one-off that closed exception #2, "Expansion Algorithm".

This row was the single case the generalised closer deliberately refused:
the card's identity agrees with the order line on every field, so nothing
in the data could tell "filed in error" from "genuinely not fulfilled".
Only an operator check against Mana Pool could, and it came back
refunded/replaced for both short lines on the order.

The property worth pinning is the restraint: the card's removal_reason
stays "other". Rewriting it to "fulfillment_missing" would have unlocked
the ordinary missing-card resolver and saved this script existing, at the
cost of recording that a card was lost when it was actually found and
kept. The test asserts the field is untouched.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import close_exception_2_expansion_algorithm as fix
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, FulfillmentException, FulfillmentExceptionEvent, InventoryCard,
    InventoryChangeLog, PickAllocation,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture(autouse=True)
def restore_constants():
    original = (fix.EXCEPTION_ID, fix.CARD_ID, fix.ORDER_ID,
                fix.MANA_POOL_LABEL, fix.SIBLING_EXCEPTION_ID)
    yield
    (fix.EXCEPTION_ID, fix.CARD_ID, fix.ORDER_ID,
     fix.MANA_POOL_LABEL, fix.SIBLING_EXCEPTION_ID) = original


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'exc2.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(fix, "engine", engine)
    return engine


def build(session, *, removal_reason="other", sibling_resolved=True):
    """Exception #2's shape: a submitted missing-type exception whose card
    was removed by the exception flow, then metadata-corrected to "other"."""
    order, item, card, allocation = seed(session)
    exception = mark_fulfillment_exception(session, allocation.id, "missing")
    session.flush()
    exception.submission_state = "submitted"
    card.removal_reason = removal_reason
    order.external_label = "533175-1893490"
    order.status = "shipped"
    session.flush()

    # the sibling short line on the same order, already closed
    _, _, sibling_card, sibling_allocation = seed(session)
    sibling = mark_fulfillment_exception(session, sibling_allocation.id, "missing")
    session.flush()
    sibling.sales_order_id = order.id
    if sibling_resolved:
        sibling.inventory_resolution_state = "resolved"
        sibling_card.inventory_exception_state = "none"
    session.commit()

    fix.EXCEPTION_ID = exception.id
    fix.CARD_ID = card.id
    fix.ORDER_ID = order.id
    fix.SIBLING_EXCEPTION_ID = sibling.id
    return exception.id, card.id, allocation.id


# --- refusals -----------------------------------------------------------

def test_refuses_if_the_sibling_line_is_not_already_closed(db):
    """The operator's evidence covers BOTH short lines together. If the
    sibling is still open, the premise of the note does not hold."""
    with Session(db) as session:
        build(session, sibling_resolved=False)

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify(session)


def test_refuses_if_the_removal_reason_is_not_the_corrected_one(db):
    """A card still reading fulfillment_missing is reachable by the
    ordinary resolver, so this one-off has no business touching it."""
    with Session(db) as session:
        build(session, removal_reason="fulfillment_missing")

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify(session)


def test_refuses_a_second_run(db):
    with Session(db) as session:
        exception_id, _, _ = build(session)

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        exception.inventory_resolution_state = "resolved"
        session.commit()

    with Session(db) as session:
        with pytest.raises(SystemExit):
            fix.verify(session)


# --- the restraint that matters -----------------------------------------

def test_preconditions_pass_on_the_real_shape(db):
    with Session(db) as session:
        build(session)

    with Session(db) as session:
        exception, order, item, allocation, card = fix.verify(session)
        assert card.removal_reason == "other"
        assert exception.exception_type == "missing"


def test_the_note_explains_why_it_is_not_a_false_positive(db):
    """The identity agreeing is exactly what made this ambiguous, so the
    note has to address it head on rather than leave a future reader to
    rediscover the question."""
    assert "Not a false positive" in fix.RESOLUTION_NOTE
    assert "matches order item" in fix.RESOLUTION_NOTE
    assert "refunded or replaced" in fix.RESOLUTION_NOTE
    assert "533175-1893490" in fix.RESOLUTION_NOTE
    # and it must say why the reason field was left alone
    assert "truthful record" in fix.RESOLUTION_NOTE


def test_the_removal_reason_is_never_rewritten(db):
    """The whole reason this script exists instead of reusing the
    missing-card resolver. Rewriting "other" back to "fulfillment_missing"
    would record that a found card was lost."""
    assert '"fulfillment_missing"' not in fix.__doc__ or "NOT rewritten" in fix.__doc__
    source = open("close_exception_2_expansion_algorithm.py").read()
    assert "card.removal_reason =" not in source, "script must never assign removal_reason"
    assert "card.status =" not in source, "script must never assign card status"
