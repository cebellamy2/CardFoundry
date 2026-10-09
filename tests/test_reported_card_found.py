"""Acting on a card that turned up AFTER it was reported to Mana Pool.

★ THE GAP. Pressing "Submitted to ManaPool" runs
auto_resolve_after_submission, which closes the exception's inventory
record (CF-AUTORESOLVE-001). From that moment
revert_fulfillment_exception_mark refuses (it needs needs_submission AND
an unresolved record) and so did confirm_substitution (it needed an
unresolved record). So the operator had nothing to click on a card he then
found -- and reporting is exactly what prompts him to go and look again.

★ THE REPORT STAYS IN THE RECORD. submission_state is never touched by any
of this, and the find gets its own event type rather than reusing
FULFILLMENT_EXCEPTION_MARK_REVERTED_EVENT, which means "this exception
should never have been filed".

★ HE CHOOSES THE CASE, because only he knows what Mana Pool did:
  back_to_order -- Mana Pool has NOT acted: the card goes back on the
      order and ships. No sellable stock moves, so NO Mana Pool write.
  back_to_stock -- Mana Pool refunded or replaced the line: the card
      becomes sellable again and the ORDER IS LEFT ALONE. Sellable stock
      goes up by one, so the caller MUST push.
A contradiction REFUSES rather than being overridden.
"""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_constants import (
    FOUND_OUTCOME_BACK_TO_ORDER,
    FOUND_OUTCOME_BACK_TO_STOCK,
    FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
    FULFILLMENT_EXCEPTION_CARD_FOUND_UNDONE_EVENT,
    REMOTE_STATES_MANA_POOL_HAS_ACTED,
)
from fulfillment_exception_resolution_service import (
    mark_reported_card_found,
    undo_reported_card_found,
)
from fulfillment_exception_service import (
    FulfillmentExceptionError,
    mark_fulfillment_exception,
)
from models import (
    Base, FulfillmentException, FulfillmentExceptionEvent, InventoryChangeLog,
)
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'found.db'}")
    Base.metadata.create_all(engine)
    return engine


def reported(session, *, kind="missing", remote="awaiting",
             order_status="picked", submitted=True):
    """A reported exception in the state slice 7 exists for: submitted to
    Mana Pool, inventory record auto-closed, nothing left to click."""
    order, item, card, allocation = seed(session, order_status=order_status)
    exception = mark_fulfillment_exception(session, allocation.id, kind)
    session.flush()
    if submitted:
        exception.submission_state = "submitted"
        exception.submitted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        exception.inventory_resolution_state = "resolved"
    exception.remote_resolution_state = remote
    session.commit()
    return order, item, card, allocation, exception


# --- case (a): Mana Pool has not acted -----------------------------------

def test_back_to_order_puts_the_card_on_the_order_and_pushes_nothing(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        assert card.status == "removed"

        result = mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found in the A2 box",
        )
        session.commit()

        assert card.status == "reserved"
        assert card.removal_reason is None and card.removed_at is None
        assert card.inventory_exception_state == "none"
        assert allocation.status == "picked"
        # ★ NO QUANTITY WRITE: a reserved card was never sellable stock.
        assert result["cards_to_push"] == []


def test_back_to_order_leaves_the_report_in_the_record(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        # Nothing may claim the report never happened.
        assert exception.submission_state == "submitted"
        assert exception.submitted_at is not None
        event = session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
        ).one()
        evidence = json.loads(event.evidence_json)
        assert evidence["outcome"] == FOUND_OUTCOME_BACK_TO_ORDER
        assert evidence["submission_state_untouched"] == "submitted"
        assert evidence["previous_card_status"] == "removed"
        # and the card's own audit row
        assert session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == card.id,
        ).count() >= 1


def test_back_to_order_works_for_a_mismatch_too(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, kind="inventory_mismatch",
        )
        assert card.status == "unsellable"
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="it was mis-sleeved",
        )
        session.commit()
        assert card.status == "reserved"
        assert card.unsellable_reason is None


# --- case (b): Mana Pool settled the line --------------------------------

@pytest.mark.parametrize("remote", sorted(REMOTE_STATES_MANA_POOL_HAS_ACTED))
def test_back_to_stock_returns_the_card_to_sale_and_pushes(db, remote):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote=remote,
        )
        result = mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_STOCK, note="found after the refund",
        )
        session.commit()

        assert card.status == "available"
        assert card.inventory_exception_state == "none"
        # The ORDER IS LEFT ALONE: the allocation stays as history.
        assert allocation.status == "exception"
        assert order.status == "picked"
        # ★ A QUANTITY WRITE: sellable stock went up by one.
        assert [c.id for c in result["cards_to_push"]] == [card.id]


def test_back_to_stock_is_allowed_even_while_mana_pool_is_still_silent(db):
    """He may know something we do not -- a phone call, an email we have not
    synced. Only the back_to_order direction is constrained, because that
    one promises a card to a customer."""
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="awaiting",
        )
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_STOCK, note="they refunded by email",
        )
        session.commit()
        assert card.status == "available"


# --- the contradiction refusal -------------------------------------------

@pytest.mark.parametrize("remote", sorted(REMOTE_STATES_MANA_POOL_HAS_ACTED))
def test_back_to_order_refuses_when_mana_pool_already_settled_the_line(db, remote):
    """Putting the card back on that order would promise the customer a card
    they have already been settled for. Refused, never silently
    reinterpreted as the other case."""
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote=remote,
        )
        with pytest.raises(FulfillmentExceptionError, match="already settled"):
            mark_reported_card_found(
                session, exception.id,
                outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
            )
        session.rollback()
        assert card.status == "removed"
        assert allocation.status == "exception"


def test_review_required_does_not_count_as_mana_pool_having_acted(db):
    """"Mana Pool needs a review" means they want a human to look, not that
    they have settled anything."""
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="review_required",
        )
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        assert card.status == "reserved"


def test_back_to_order_refuses_once_the_order_can_no_longer_ship(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        order.status = "shipped"
        session.commit()
        with pytest.raises(FulfillmentExceptionError, match="no longer carry"):
            mark_reported_card_found(
                session, exception.id,
                outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
            )


def test_an_unknown_outcome_and_a_missing_note_both_refuse(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        with pytest.raises(FulfillmentExceptionError, match="Choose whether"):
            mark_reported_card_found(
                session, exception.id, outcome="whatever", note="x",
            )
        with pytest.raises(FulfillmentExceptionError, match="note is required"):
            mark_reported_card_found(
                session, exception.id,
                outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="   ",
            )


def test_a_card_that_has_moved_since_the_report_refuses(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        card.status = "sold"
        session.commit()
        with pytest.raises(FulfillmentExceptionError, match="state this exception left it in"):
            mark_reported_card_found(
                session, exception.id,
                outcome=FOUND_OUTCOME_BACK_TO_STOCK, note="found it",
            )


# --- universal undo ------------------------------------------------------

def test_undo_of_back_to_order_restores_everything_and_pushes_nothing(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()

        result = undo_reported_card_found(session, exception.id, "my mistake")
        session.commit()

        assert card.status == "removed"
        assert card.removal_reason == "fulfillment_missing"
        assert card.inventory_exception_state == "exception_unresolved"
        assert allocation.status == "exception"
        assert result["cards_to_push"] == []
        assert exception.submission_state == "submitted"   # still honest


def test_undo_of_back_to_stock_takes_the_card_back_off_the_shelf_and_pushes(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_refunded",
        )
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_STOCK, note="found it",
        )
        session.commit()
        assert card.status == "available"

        result = undo_reported_card_found(session, exception.id, "wrong card")
        session.commit()

        assert card.status == "removed"
        assert [c.id for c in result["cards_to_push"]] == [card.id]


def test_undo_writes_its_own_event_and_cannot_be_run_twice(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        undo_reported_card_found(session, exception.id, "undo once")
        session.commit()

        event = session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_UNDONE_EVENT,
        ).one()
        assert json.loads(event.evidence_json)["outcome_undone"] == (
            FOUND_OUTCOME_BACK_TO_ORDER
        )
        with pytest.raises(FulfillmentExceptionError, match="already been undone"):
            undo_reported_card_found(session, exception.id, "again")


def test_undo_refuses_with_nothing_to_undo(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        with pytest.raises(FulfillmentExceptionError, match="no recorded found-card"):
            undo_reported_card_found(session, exception.id, "nothing here")


def test_undo_refuses_once_the_card_has_moved_on(db):
    """Somebody else's decision now rests on the find -- pulling the card
    back out from under it would be worse than refusing."""
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_refunded",
        )
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_STOCK, note="found it",
        )
        session.commit()
        card.status = "sold"
        session.commit()
        with pytest.raises(FulfillmentExceptionError, match="not the 'available'"):
            undo_reported_card_found(session, exception.id, "undo")


def test_a_find_can_be_redone_after_an_undo(db):
    """Universal undo means undoable, not one-shot."""
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        undo_reported_card_found(session, exception.id, "undo")
        session.commit()
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it again",
        )
        session.commit()
        assert card.status == "reserved"
        assert allocation.status == "picked"


# --- the page and the routes ---------------------------------------------

def _page_wave(tmp_path, monkeypatch, *, wave_status="picked",
               remote="awaiting", submitted=True):
    from tests.test_pick_wave_detail_item15_redesign import (
        add_order_with_card, make_wave, setup_db,
    )
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, wave_status=wave_status)
        add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status="exception", with_exception=True,
        )
        exception = session.query(FulfillmentException).one()
        # add_order_with_card writes the exception row directly, so the CARD
        # never received the disposition a real mark_fulfillment_exception
        # applies. The found action reads that state, so put it there.
        from models import InventoryCard
        card = session.get(InventoryCard, exception.inventory_card_id)
        card.status = "removed"
        card.removal_reason = "fulfillment_missing"
        card.removed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        card.inventory_exception_state = "exception_unresolved"
        if submitted:
            exception.submission_state = "submitted"
            exception.inventory_resolution_state = "resolved"
        exception.remote_resolution_state = remote
        session.commit()
        return engine, wave.id, exception.id


def test_the_found_action_appears_on_a_reported_line_after_submission(
    tmp_path, monkeypatch,
):
    """The whole point: before slice 7 there was nothing to click here."""
    import main
    from fastapi.testclient import TestClient

    _, wave_id, _ = _page_wave(tmp_path, monkeypatch)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "I found this card" in html
    assert 'value="back_to_order"' in html
    assert 'value="back_to_stock"' in html
    assert "/found" in html


def test_the_settled_case_warns_on_back_to_order_and_says_why(
    tmp_path, monkeypatch,
):
    import main
    from fastapi.testclient import TestClient

    _, wave_id, _ = _page_wave(tmp_path, monkeypatch, remote="resolved_refunded")
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    # SUPERSEDED wording: the radio is no longer disabled (that would put
    # the override out of reach), so the warning says what will happen.
    assert "our record says Mana Pool already resolved_refunded" in html
    assert "so this will ask you to confirm" in html


def test_no_actions_on_a_shipped_wave(tmp_path, monkeypatch):
    """A shipped wave sold the cards and told Mana Pool; there is nothing
    honest left to do, so the cell stays read-only."""
    import main
    from fastapi.testclient import TestClient

    _, wave_id, _ = _page_wave(tmp_path, monkeypatch, wave_status="shipped")
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "I found this card" not in html
    assert "Reported missing" in html          # still visible, read-only


def test_the_found_route_refuses_a_contradiction_with_a_reason(
    tmp_path, monkeypatch,
):
    import main
    from fastapi.testclient import TestClient

    _, wave_id, exception_id = _page_wave(
        tmp_path, monkeypatch, remote="resolved_replaced",
    )
    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": "back_to_order", "note": "found it",
              "return_wave_id": str(wave_id)},
    )
    assert response.status_code == 409
    assert "already settled this line" in response.text


def test_the_found_route_then_offers_the_undo(tmp_path, monkeypatch):
    import main
    from fastapi.testclient import TestClient

    _, wave_id, exception_id = _page_wave(tmp_path, monkeypatch)
    client = TestClient(main.app)
    posted = client.post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": "back_to_order", "note": "found it",
              "return_wave_id": str(wave_id)},
        follow_redirects=False,
    )
    assert posted.status_code in (302, 303)
    html = client.get(f"/pick-waves/{wave_id}").text
    # ★ The undo lives on the EXCEPTION TABLE, not the pick-list cell: a
    # back_to_order find moves the allocation to "picked", so that line is
    # an ordinary picked line now and no longer offers reported-line
    # actions. The exception table lists every exception regardless of
    # allocation status, which is why the undo is always reachable there.
    assert "Undo found card" in html
    assert "I found this card" not in html


# --- the escape hatch: overriding a stale remote record ------------------
#
# ★ OPERATOR DECISION 2026-10-08. Our record can be stale -- a phone call,
# an email we have not synced -- so the one refusal that rests on OUR
# RECORD rather than on what already happened may be overridden. It
# REQUIRES him to say in writing that Mana Pool has not acted, and those
# words are stored with the action, so the override is exactly as auditable
# as the thing it permits.

def test_the_contradiction_refusal_is_its_own_error_carrying_the_state(db):
    from fulfillment_exception_resolution_service import RemoteStateContradiction

    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_refunded",
        )
        with pytest.raises(RemoteStateContradiction) as caught:
            mark_reported_card_found(
                session, exception.id,
                outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
            )
        assert caught.value.recorded_remote_state == "resolved_refunded"
        assert "confirm that below" in str(caught.value)


def test_an_override_note_lets_back_to_order_through(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_replaced",
        )
        result = mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
            override_remote_state_note="Mana Pool emailed me, the replacement never went out",
        )
        session.commit()
        assert card.status == "reserved"
        assert allocation.status == "picked"
        assert result["overrode_remote_state"] is True
        # Still no Mana Pool write -- the override changes who decides, not
        # what moves.
        assert result["cards_to_push"] == []


def test_an_override_without_a_note_still_refuses(db):
    """A blank note would turn an assertion about the world into a
    click-through."""
    from fulfillment_exception_resolution_service import RemoteStateContradiction

    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_refunded",
        )
        for blank in ("", "   ", None):
            with pytest.raises(RemoteStateContradiction):
                mark_reported_card_found(
                    session, exception.id,
                    outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
                    override_remote_state_note=blank,
                )


def test_the_override_and_its_reason_are_recorded_in_the_event(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_fulfilled",
        )
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
            override_remote_state_note="spoke to Mana Pool support at 14:10",
        )
        session.commit()
        event = session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
        ).one()
        override = json.loads(event.evidence_json)["remote_state_override"]
        assert override["recorded_remote_state"] == "resolved_fulfilled"
        assert override["operator_note"] == "spoke to Mana Pool support at 14:10"
        audit = json.loads(session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == card.id,
        ).order_by(InventoryChangeLog.id.desc()).first().change_summary)
        assert audit["remote_state_override_note"] == (
            "spoke to Mana Pool support at 14:10"
        )


def test_an_ordinary_find_records_no_override_key_at_all(db):
    """Absent, not False -- a reader should never have to interpret a
    falsy override."""
    with Session(db) as session:
        order, item, card, allocation, exception = reported(session)
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        event = session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
        ).one()
        assert "remote_state_override" not in json.loads(event.evidence_json)


def test_an_overridden_find_is_undoable_and_the_undo_says_so(db):
    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_refunded",
        )
        mark_reported_card_found(
            session, exception.id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
            override_remote_state_note="they confirmed by phone",
        )
        session.commit()

        result = undo_reported_card_found(session, exception.id, "my mistake")
        session.commit()

        assert card.status == "removed"
        assert allocation.status == "exception"
        assert result["cards_to_push"] == []
        undo_event = session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_UNDONE_EVENT,
        ).one()
        carried = json.loads(undo_event.evidence_json)["undone_remote_state_override"]
        assert carried["operator_note"] == "they confirmed by phone"


def test_a_shipped_order_is_not_overridable(db):
    """The override is for a stale RECORD. A shipped order is not a record
    problem -- it is what already happened, so there is nothing to know
    better about."""
    from fulfillment_exception_resolution_service import RemoteStateContradiction

    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_refunded",
        )
        order.status = "shipped"
        session.commit()
        with pytest.raises(FulfillmentExceptionError, match="no longer carry") as caught:
            mark_reported_card_found(
                session, exception.id,
                outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
                override_remote_state_note="I am sure",
            )
        assert not isinstance(caught.value, RemoteStateContradiction)


def test_the_override_is_logged_as_a_warning(db, caplog):
    import logging
    from contextlib import contextmanager

    @contextmanager
    def capturing():
        cf = logging.getLogger("cardfoundry")
        caplog.set_level(logging.WARNING, logger="cardfoundry")
        cf.addHandler(caplog.handler)
        try:
            yield
        finally:
            cf.removeHandler(caplog.handler)

    with Session(db) as session:
        order, item, card, allocation, exception = reported(
            session, remote="resolved_replaced",
        )
        with capturing():
            mark_reported_card_found(
                session, exception.id,
                outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
                override_remote_state_note="support ticket 881",
            )
    assert "OVERRODE the recorded remote state" in caplog.text
    assert "support ticket 881" in caplog.text


def test_the_refusal_page_offers_the_override_form(tmp_path, monkeypatch):
    import main
    from fastapi.testclient import TestClient

    _, wave_id, exception_id = _page_wave(
        tmp_path, monkeypatch, remote="resolved_replaced",
    )
    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": "back_to_order", "note": "found it",
              "return_wave_id": str(wave_id)},
    )
    assert response.status_code == 409
    assert 'name="override_remote_state_note"' in response.text
    assert "required" in response.text
    assert "Mana Pool has NOT acted" in response.text
    # Nothing pre-filled: a default would make it a click-through.
    assert 'placeholder="How do you know' in response.text


def test_the_override_form_round_trips_through_the_route(tmp_path, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    from models import InventoryCard

    engine, wave_id, exception_id = _page_wave(
        tmp_path, monkeypatch, remote="resolved_refunded",
    )
    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": "back_to_order", "note": "found it",
              "override_remote_state_note": "they never refunded, I checked",
              "return_wave_id": str(wave_id)},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    with Session(engine) as session:
        exception = session.get(FulfillmentException, exception_id)
        card = session.get(InventoryCard, exception.inventory_card_id)
        assert card.status == "reserved"
        assert exception.submission_state == "submitted"


def test_the_back_to_order_radio_stays_selectable_when_contradicted(
    tmp_path, monkeypatch,
):
    """Disabling it would put the refusal -- and the override that lets him
    correct a stale record -- out of reach entirely."""
    import main
    from fastapi.testclient import TestClient

    _, wave_id, _ = _page_wave(tmp_path, monkeypatch, remote="resolved_refunded")
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert 'value="back_to_order"' in html
    # "disabled" also appears as a CSS token name, so assert on the input.
    assert 'value="back_to_order"\n                       disabled' not in html
    assert "so this will ask you to confirm" in html
