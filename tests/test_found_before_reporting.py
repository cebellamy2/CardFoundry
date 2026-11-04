"""A card found BEFORE the exception was reported must not strand its order.

★ WHAT WENT WRONG. Order 744602-2620955, 2026-11-04. The operator marked
a line missing, then found the card, then pressed "I found this card ->
Mana Pool has NOT acted". The find left submission_state at
"needs_submission" -- correct when the exception really had been
reported, which is the case slice 7 was written for. But only an
unsubmitted exception blocks order completion, so the order could never
leave "in_pick_wave", and every exit refused:

  Undo Exception Mark  needs needs_submission AND unresolved; the find
                       had resolved it
  Substitute           needs unresolved, or a resolution that was the
                       auto-close on submission; a find is neither
  Found                already done, and it never touched submission

The only action the ORDER page offered was "Submitted to ManaPool",
which would have recorded a manual report that never happened -- and
would itself have been refused, because the find had already moved the
allocation off "exception". The page also showed no link to the wave
holding the order, because that link required an ACTIVE wave and the
wave had already completed. Chris unblocked it by hand on the wave page.

★ THE RULE NOW. A back_to_order find on an exception still at
"needs_submission" moves it to "not_required": the card is in hand and
going back to the customer, so there is nothing left to tell Mana Pool.
A report that DID happen still stays "submitted". back_to_stock is
deliberately unchanged and still blocks -- that outcome says Mana Pool
settled the line, and whether they still need telling is the operator's
question.

Nothing here contacts Mana Pool: a back_to_order find moves no sellable
stock, so there is no quantity write in either direction.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_constants import (
    FOUND_OUTCOME_BACK_TO_ORDER,
    FOUND_OUTCOME_BACK_TO_STOCK,
    FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
)
from fulfillment_exception_invariants import (
    order_has_fulfillment_submission_block,
)
from fulfillment_exception_resolution_service import (
    mark_reported_card_found,
    undo_reported_card_found,
)
from models import (
    Base, FulfillmentException, FulfillmentExceptionEvent, InventoryCard,
    PickAllocation, SalesOrder,
)
from tests.test_deferred_pick_promotion import stranded_order


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'found-first.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    return engine


def unreported(session):
    """Exactly order 744602-2620955: a completed wave, one sibling line
    already picked, and an exception nobody has reported."""
    return stranded_order(session, submit_exception=False)


# ---------------------------------------------------------------------
# ★ THE OPERATOR'S OWN SEQUENCE, THROUGH THE REAL ROUTE
# ---------------------------------------------------------------------

def test_a_find_before_reporting_takes_the_order_to_picked(db):
    with Session(db) as session:
        order, exception, _wave = unreported(session)
        order_id, exception_id = order.id, exception.id
        assert order.status == "in_pick_wave"
        assert exception.submission_state == "needs_submission"

    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": FOUND_OUTCOME_BACK_TO_ORDER, "note": "found it"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        order = session.get(SalesOrder, order_id)
        # Nothing left to tell Mana Pool, so nothing left blocking.
        assert exception.submission_state == "not_required"
        assert order_has_fulfillment_submission_block([exception]) is False
        # ★ AND THE ORDER ACTUALLY MOVED. This is the outcome the
        # operator sees: it can be packed and shipped with its wave.
        assert order.status == "picked"
        assert order.picked_at is not None
        allocation = session.get(PickAllocation, exception.pick_allocation_id)
        assert allocation.status == "picked"
        card = session.get(InventoryCard, exception.inventory_card_id)
        assert card.status == "reserved"
        assert card.inventory_exception_state == "none"


def test_the_find_records_both_submission_states(db):
    with Session(db) as session:
        _order, exception, _wave = unreported(session)
        exception_id = exception.id
        result = mark_reported_card_found(
            session, exception_id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        # No marketplace write: a reserved card was never sellable stock.
        assert result["cards_to_push"] == []
        event = session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
        ).one()
        evidence = json.loads(event.evidence_json)
        assert evidence["previous_submission_state"] == "needs_submission"
        assert evidence["new_submission_state"] == "not_required"
        # The retired key must not come back under a name that lies.
        assert "submission_state_untouched" not in evidence


# ---------------------------------------------------------------------
# ★ THE UNDO ROUND-TRIPS IT
# ---------------------------------------------------------------------

def test_undoing_the_find_puts_needs_submission_back(db):
    with Session(db) as session:
        _order, exception, _wave = unreported(session)
        exception_id = exception.id
        mark_reported_card_found(
            session, exception_id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        assert exception.submission_state == "not_required"

        undo_reported_card_found(session, exception_id, "put it back")
        session.commit()
        assert exception.submission_state == "needs_submission"
        assert exception.inventory_resolution_state == "unresolved"
        assert order_has_fulfillment_submission_block([exception]) is True


def test_an_undo_does_not_erase_a_report_made_after_the_find(db):
    """Somebody's later decision outranks the undo. Putting
    "needs_submission" back here would claim a report never happened."""
    with Session(db) as session:
        _order, exception, _wave = unreported(session)
        exception_id = exception.id
        mark_reported_card_found(
            session, exception_id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        exception.submission_state = "submitted"
        session.commit()

        undo_reported_card_found(session, exception_id, "put it back")
        session.commit()
        assert exception.submission_state == "submitted"


def test_an_old_find_with_no_recorded_state_still_undoes(db):
    """Evidence written before this existed carries no
    previous_submission_state. The undo must still work, and must leave
    submission_state exactly where it is -- there is nothing honest to
    restore without a recorded before-state."""
    with Session(db) as session:
        _order, exception, _wave = unreported(session)
        exception_id = exception.id
        mark_reported_card_found(
            session, exception_id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        event = session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
        ).one()
        evidence = json.loads(event.evidence_json)
        del evidence["previous_submission_state"]
        del evidence["new_submission_state"]
        event.evidence_json = json.dumps(evidence, sort_keys=True)
        session.commit()

        undo_reported_card_found(session, exception_id, "put it back")
        session.commit()
        assert exception.submission_state == "not_required"
        assert exception.inventory_resolution_state == "unresolved"


# ---------------------------------------------------------------------
# ★ WHAT MUST NOT CHANGE
# ---------------------------------------------------------------------

def test_a_real_report_is_never_overwritten(db):
    with Session(db) as session:
        _order, exception, _wave = stranded_order(session, submit_exception=True)
        exception_id = exception.id
        session.get(FulfillmentException, exception_id).inventory_resolution_state = "resolved"
        session.commit()
        mark_reported_card_found(
            session, exception_id,
            outcome=FOUND_OUTCOME_BACK_TO_ORDER, note="found it",
        )
        session.commit()
        assert exception.submission_state == "submitted"


def test_back_to_stock_still_blocks(db):
    """Unchanged on purpose: that outcome says Mana Pool settled the
    line, so whether they still need telling is the operator's call."""
    with Session(db) as session:
        order, exception, _wave = unreported(session)
        order_id, exception_id = order.id, exception.id
        result = mark_reported_card_found(
            session, exception_id,
            outcome=FOUND_OUTCOME_BACK_TO_STOCK, note="they refunded it",
        )
        session.commit()
        assert exception.submission_state == "needs_submission"
        assert order_has_fulfillment_submission_block([exception]) is True
        assert session.get(SalesOrder, order_id).status == "in_pick_wave"
        # A back_to_stock find DOES move sellable stock, so it pushes.
        assert len(result["cards_to_push"]) == 1


def test_a_live_wave_still_holds_the_order(db):
    """promote_if_pick_complete refuses while a live wave owns the order,
    and the found route must not talk it round. Promoting mid-pick would
    claim a pick that has not finished."""
    with Session(db) as session:
        order, exception, _wave = stranded_order(
            session, wave_status="active", membership="active",
            submit_exception=False,
        )
        order_id, exception_id = order.id, exception.id

    TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": FOUND_OUTCOME_BACK_TO_ORDER, "note": "found it"},
        follow_redirects=False,
    )
    with Session(db) as session:
        # The block is gone -- there is genuinely nothing to report --
        # but the wave still owns the pick.
        assert session.get(
            FulfillmentException, exception_id,
        ).submission_state == "not_required"
        assert session.get(SalesOrder, order_id).status == "in_pick_wave"


def test_an_unpicked_sibling_still_holds_the_order(db):
    with Session(db) as session:
        order, exception, _wave = stranded_order(
            session, submit_exception=False, second_line_status="allocated",
        )
        order_id, exception_id = order.id, exception.id

    TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": FOUND_OUTCOME_BACK_TO_ORDER, "note": "found it"},
        follow_redirects=False,
    )
    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "in_pick_wave"


# ---------------------------------------------------------------------
# ★ THE ORDER PAGE IS NO LONGER A DEAD END
# ---------------------------------------------------------------------

def _order_page_wave(tmp_path, monkeypatch, *, wave_status="packed"):
    """An order whose wave has already completed -- the state that left
    order 744602-2620955 with no route forward on this page."""
    from datetime import datetime, timezone

    from tests.test_pick_wave_detail_item15_redesign import (
        add_order_with_card, make_wave, setup_db,
    )
    engine = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    with Session(engine) as session:
        wave = make_wave(session, wave_status=wave_status)
        add_order_with_card(
            session, wave, batch_code="A1",
            allocation_status="exception", with_exception=True,
        )
        exception = session.query(FulfillmentException).one()
        card = session.get(InventoryCard, exception.inventory_card_id)
        card.status = "removed"
        card.removal_reason = "fulfillment_missing"
        card.removed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        card.inventory_exception_state = "exception_unresolved"
        session.commit()
        return engine, wave.id, exception.id, exception.sales_order_id


def test_the_order_page_offers_the_found_action(tmp_path, monkeypatch):
    _db, _wave_id, _exception_id, order_id = _order_page_wave(
        tmp_path, monkeypatch,
    )
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "I found this card" in html
    assert 'value="back_to_order"' in html
    assert 'value="back_to_stock"' in html
    assert "/found" in html


def test_the_order_page_links_to_a_wave_that_has_already_completed(
    tmp_path, monkeypatch,
):
    """The link required an ACTIVE wave, so the one order that most
    needed it -- still in_pick_wave BECAUSE its wave finished without
    it -- was the one that never got it."""
    _db, wave_id, _exception_id, order_id = _order_page_wave(
        tmp_path, monkeypatch,
    )
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Open Master Pick Wave" in html
    assert f'/pick-waves/{wave_id}' in html


def test_the_found_action_from_the_order_page_comes_back_to_the_order(
    tmp_path, monkeypatch,
):
    """No wave id on these forms, and _pick_wave_return_url answers ""
    without one -- an empty redirect target would have put the operator
    nowhere."""
    _db, _wave_id, exception_id, order_id = _order_page_wave(
        tmp_path, monkeypatch,
    )
    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": FOUND_OUTCOME_BACK_TO_ORDER, "note": "found it"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/orders/{order_id}"


def test_the_undo_from_the_order_page_comes_back_to_the_order(
    tmp_path, monkeypatch,
):
    _db, _wave_id, exception_id, order_id = _order_page_wave(
        tmp_path, monkeypatch,
    )
    client = TestClient(main.app)
    client.post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": FOUND_OUTCOME_BACK_TO_ORDER, "note": "found it"},
        follow_redirects=False,
    )
    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/found/undo",
        data={"note": "put it back"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/orders/{order_id}"


def test_the_order_page_offers_the_undo_once_a_card_is_found(
    tmp_path, monkeypatch,
):
    _db, _wave_id, exception_id, order_id = _order_page_wave(
        tmp_path, monkeypatch,
    )
    client = TestClient(main.app)
    client.post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": FOUND_OUTCOME_BACK_TO_ORDER, "note": "found it"},
        follow_redirects=False,
    )
    html = client.get(f"/orders/{order_id}").text
    assert "Undo Found Card" in html
    assert "I found this card" not in html


def test_no_new_javascript_on_the_order_page_actions(tmp_path, monkeypatch):
    _db, _wave_id, _exception_id, order_id = _order_page_wave(
        tmp_path, monkeypatch,
    )
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    block = html[html.index("I found this card"):]
    block = block[:block.index("</details>")]
    assert "<script" not in block
    assert "onclick" not in block


def test_a_shipped_order_still_offers_the_action_and_refuses_the_wrong_half(
    tmp_path, monkeypatch,
):
    """★ A DELIBERATE DIFFERENCE FROM THE WAVE PAGE, which hides these
    actions once the wave is terminal. The wave is a picking workspace
    and finishes; the ORDER page is the record, and it is where an
    operator stands weeks later holding a card that turned up. Finding it
    then is a real event with a real answer -- back_to_stock, the card
    becomes sellable again -- so hiding the action would recreate exactly
    the dead end this release exists to remove.

    back_to_order is the half that cannot apply, and the service refuses
    it by order status with a reason rather than the page quietly
    withholding it."""
    _db, _wave_id, exception_id, order_id = _order_page_wave(
        tmp_path, monkeypatch, wave_status="shipped",
    )
    with Session(main.engine) as session:
        session.get(SalesOrder, order_id).status = "shipped"
        session.commit()

    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "I found this card" in html

    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": FOUND_OUTCOME_BACK_TO_ORDER, "note": "found it"},
    )
    assert "can no longer carry this card to the customer" in response.text
    with Session(main.engine) as session:
        assert session.get(
            FulfillmentException, exception_id,
        ).submission_state == "needs_submission"
