"""An open fulfillment exception must put a card off-limits to manual
inventory edits.

Without this guard, marking a card Not For Sale, returning it to sellable,
removing it, un-removing it, or rewriting its removal metadata all clear
the reason fields the exception's own resolvers require. Clear them and
every resolution path is locked out permanently, silently. Five real
exceptions reached that state before the guard existed (#2, #5, #17, #18,
#33) and #17 had to be closed by hand.

The two properties that matter most here are the ones that are easy to get
wrong in opposite directions:
  * a RESOLVED exception must NOT block, or the 28 cards whose allocation
    still sits at "exception" would be frozen forever;
  * the mismatch resolver itself must still work, because it restores
    sellability while its own exception is deliberately still open.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import sellability_service as sell
from fulfillment_exception_service import mark_fulfillment_exception
from models import Base, FulfillmentException, InventoryCard
from sellability_service import (
    SellabilityError,
    correct_removal_metadata,
    disposition_identity_hash,
    removal_metadata_state_hash,
    transition_card_un_removal,
    transition_inventory_removal,
    transition_manual_disposition,
    transition_sellability,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    Base.metadata.create_all(engine)
    return engine


def raise_exception_on(session, kind="inventory_mismatch"):
    order, item, card, allocation = seed(session)
    exception = mark_fulfillment_exception(session, allocation.id, kind)
    session.commit()
    return exception, card


def force_status(session, card, status):
    """Put the card in the state the surface under test requires. The real
    incidents reached these states through the very edits now blocked."""
    card.status = status
    session.commit()


# --- every surface refuses ---------------------------------------------

def test_return_to_sellable_is_refused(db):
    with Session(db) as session:
        _, card = raise_exception_on(session)
        assert card.status == "unsellable"
        with pytest.raises(SellabilityError) as caught:
            transition_sellability(session, card.id, "unsellable", "available")
    assert "unresolved fulfillment exception" in str(caught.value)


def test_mark_not_for_sale_is_refused(db):
    with Session(db) as session:
        _, card = raise_exception_on(session)
        force_status(session, card, "available")
        with pytest.raises(SellabilityError):
            transition_sellability(session, card.id, "available", "unsellable", "hold", "note")


def test_removal_is_refused(db):
    """#5, #18 and #33 were all removed for unrelated reasons while their
    exception was open, which is what stranded them."""
    with Session(db) as session:
        _, card = raise_exception_on(session)
        force_status(session, card, "available")
        with pytest.raises(SellabilityError):
            transition_inventory_removal(
                session, card.id, "available", disposition_identity_hash(card),
                "other", "grabbed for me",
            )


def test_removal_metadata_correction_is_refused(db):
    """This is exactly what broke #2: a card removed by the exception flow
    as fulfillment_missing, then corrected to "other"."""
    with Session(db) as session:
        _, card = raise_exception_on(session, kind="missing")
        assert card.status == "removed"
        assert card.removal_reason == "fulfillment_missing"
        with pytest.raises(SellabilityError) as caught:
            correct_removal_metadata(
                session, card.id, removal_metadata_state_hash(card),
                "other", "found in another batch", None, "was found",
            )
    assert "unresolved fulfillment exception" in str(caught.value)


def test_un_removal_is_refused(db):
    with Session(db) as session:
        _, card = raise_exception_on(session, kind="missing")
        with pytest.raises(SellabilityError):
            transition_card_un_removal(
                session, card.id, removal_metadata_state_hash(card), "undo",
            )


def test_manual_disposition_is_refused(db):
    with Session(db) as session:
        _, card = raise_exception_on(session)
        force_status(session, card, "available")
        with pytest.raises(SellabilityError):
            transition_manual_disposition(
                session, card.id, "available", disposition_identity_hash(card),
                "trade", "traded away",
            )


# --- missing-type exceptions are covered too ----------------------------

def test_missing_type_exceptions_block_as_well_as_mismatches(db):
    """Open question from the ticket, answered by #2: a missing-type card
    is normally "removed", so sellability edits do not reach it -- but the
    removal-metadata surface does, and that is what stranded #2."""
    with Session(db) as session:
        exception, card = raise_exception_on(session, kind="missing")
        assert exception.exception_type == "missing"
        with pytest.raises(SellabilityError):
            correct_removal_metadata(
                session, card.id, removal_metadata_state_hash(card),
                "other", "note", None, "reason",
            )


# --- the refusal has to be useful ---------------------------------------

def test_refusal_names_the_exception_and_says_what_to_do(db):
    with Session(db) as session:
        exception, card = raise_exception_on(session)
        exception_id, order_id = exception.id, exception.sales_order_id
        with pytest.raises(SellabilityError) as caught:
            transition_sellability(session, card.id, "unsellable", "available")
    message = str(caught.value)
    assert f"#{exception_id}" in message
    assert f"#{order_id}" in message
    assert "inventory mismatch" in message
    assert "Resolve or close out that exception first" in message


# --- and must NOT over-block --------------------------------------------

def test_a_resolved_exception_does_not_block_anything(db):
    """No resolver has ever moved an allocation off "exception", so 28
    already-resolved exceptions still sit at that status. Blocking on the
    allocation status alone would freeze those cards forever."""
    with Session(db) as session:
        exception, card = raise_exception_on(session)
        exception.inventory_resolution_state = "resolved"
        card.inventory_exception_state = "none"
        session.commit()
        card_id = card.id

    with Session(db) as session:
        card = transition_sellability(session, card_id, "unsellable", "available")
        session.commit()
        assert card.status == "available"


def test_a_card_with_no_exception_at_all_is_untouched(db):
    with Session(db) as session:
        # allocation_status "released": seed() defaults to "allocated",
        # which the PRE-EXISTING active-allocation check blocks for its
        # own unrelated reason and would mask what this test asserts.
        _, _, card, _ = seed(session, allocation_status="released", card_status="unsellable")
        card.unsellable_reason = "hold"
        session.commit()
        card_id = card.id

    with Session(db) as session:
        card = transition_sellability(session, card_id, "unsellable", "available")
        session.commit()
        assert card.status == "available"


def test_the_guard_is_scoped_to_the_card_that_has_the_exception(db):
    """A neighbour in the same batch must stay fully editable."""
    with Session(db) as session:
        raise_exception_on(session)
        _, _, other, _ = seed(session, allocation_status="released", card_status="unsellable")
        other.unsellable_reason = "hold"
        session.commit()
        other_id = other.id

    with Session(db) as session:
        card = transition_sellability(session, other_id, "unsellable", "available")
        session.commit()
        assert card.status == "available"


# --- the sanctioned resolver keeps working ------------------------------

def test_the_mismatch_resolver_is_exempt_and_still_restores_sellability(db):
    """resolve_inventory_mismatch_exception restores sellability BEFORE it
    marks the exception resolved, so at that instant the exception is
    still open. Without the exemption the guard would refuse the one
    function that exists to clear it."""
    with Session(db) as session:
        _, card = raise_exception_on(session)
        card_id = card.id

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert session.query(FulfillmentException).filter_by(
            inventory_card_id=card_id, inventory_resolution_state="unresolved",
        ).count() == 1
        restored = transition_sellability(
            session, card.id, "unsellable", "available",
            allow_open_exception=True,
        )
        session.commit()
        assert restored.status == "available"


def test_the_exemption_is_off_by_default(db):
    with Session(db) as session:
        _, card = raise_exception_on(session)
        with pytest.raises(SellabilityError):
            transition_sellability(session, card.id, "unsellable", "available")


# --- the refusal reaches the server log ---------------------------------

def test_the_guard_logs_when_it_fires(db, caplog):
    """The bulk helpers catch SellabilityError per card and fold it into a
    "skipped" row on the results page, so the guard firing across a large
    selection would otherwise leave no server-side trace. Logged at the
    guard itself, so every surface is covered at once.

    `import main` is load-bearing, not tidy-up: main.py is what configures
    the shared "cardfoundry" logger with its own handler and
    propagate=False. Without it the logger is unconfigured here, records
    reach caplog by propagation, and this test would pass for a reason
    that does not hold in production. The companion test below pins that.
    """
    import logging

    import main  # configures the shared "cardfoundry" logger

    assert main.logger.propagate is False, "production config not applied"
    caplog.set_level(logging.WARNING)
    sell.logger.addHandler(caplog.handler)
    try:
        with Session(db) as session:
            exception, card = raise_exception_on(session)
            exception_id = exception.id
            with pytest.raises(SellabilityError):
                transition_sellability(session, card.id, "unsellable", "available")
    finally:
        sell.logger.removeHandler(caplog.handler)

    refusals = [
        r for r in caplog.records
        if "manual inventory edit refused" in r.getMessage()
    ]
    assert len(refusals) == 1
    message = refusals[0].getMessage()
    assert f"id={exception_id}" in message
    assert "inventory_mismatch" in message


def test_without_attaching_the_handler_caplog_sees_nothing(db, caplog):
    """Proves the assertion above is not vacuous: under the production
    logger configuration, records appear ONLY because the handler was
    attached directly. propagate=False is what makes that true, and it is
    the exact trap that made an earlier logging test pass against an empty
    list."""
    import logging

    import main  # same production config as above

    caplog.set_level(logging.WARNING)
    with Session(db) as session:
        _, card = raise_exception_on(session)
        with pytest.raises(SellabilityError):
            transition_sellability(session, card.id, "unsellable", "available")

    assert not [
        r for r in caplog.records
        if "manual inventory edit refused" in r.getMessage()
    ], "propagate=False means caplog alone must see nothing"
