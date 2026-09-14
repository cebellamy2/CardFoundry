"""Ticket C (v1.158.0): bulk close-out of the decision-free missing-card
fulfillment exceptions, draining the backlog Tickets A and B exposed.

The guarantees that matter here:
  1. rows needing an individual decision are NEVER closed out -- not by
     the bulk action, not by being swept along with eligible neighbours,
     and they are not even offered a checkbox;
  2. the action is all-or-nothing, so a selection containing one
     ineligible row changes nothing rather than partially applying;
  3. it writes through the existing guarded resolver, so the card's own
     status is untouched and the audit trail is the normal one.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, FulfillmentException, FulfillmentExceptionEvent, InventoryCard,
    InventoryChangeLog, PickAllocation,
)
from tests.test_fulfillment_exception_reconciliation import submit
from tests.test_fulfillment_exception_service import seed

PAGE = "/orders/shipment-sync-issues"
ROUTE = "/fulfillment-exceptions/bulk-accept-missing"


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'bulk-accept.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_missing(session):
    """The uniform, decision-free shape: card already pulled from
    inventory as fulfillment_missing, exception already reported."""
    _, _, _, allocation = seed(session)
    exception = submit(session, allocation)
    session.commit()
    return exception


def make_mismatch(session):
    """An inventory_mismatch: the card is quarantined pending a printing
    decision nobody has made yet. Never decision-free."""
    _, _, _, allocation = seed(session)
    exception = submit(session, allocation, exception_type="inventory_mismatch")
    session.commit()
    return exception


# --- the core guarantee: individual-review rows are never auto-resolved --

def test_mismatch_exception_is_never_closed_out_by_the_bulk_action(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        exception_id = make_mismatch(session).id

    client = TestClient(main.app)
    response = client.post(ROUTE, data={"exception_ids": [str(exception_id)]})
    assert response.status_code == 409

    with Session(db) as session:
        exception = session.get(FulfillmentException, exception_id)
        assert exception.inventory_resolution_state == "unresolved"
        card = session.get(InventoryCard, exception.inventory_card_id)
        assert card.inventory_exception_state == "exception_unresolved"


def test_one_ineligible_row_blocks_the_whole_selection(tmp_path, monkeypatch):
    """All-or-nothing. Partially applying a bulk action does something
    other than what the operator approved."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        good_one = make_missing(session).id
        good_two = make_missing(session).id
        bad = make_mismatch(session).id

    client = TestClient(main.app)
    response = client.post(
        ROUTE, data={"exception_ids": [str(good_one), str(bad), str(good_two)]},
    )
    assert response.status_code == 409
    assert f"#{bad}" in response.text
    assert "individual review" in response.text

    with Session(db) as session:
        for exception_id in (good_one, good_two, bad):
            assert session.get(
                FulfillmentException, exception_id
            ).inventory_resolution_state == "unresolved"


def test_unsubmitted_exception_is_not_eligible(tmp_path, monkeypatch):
    """An exception still at needs_submission has not reached Mana Pool.
    Closing CardFoundry's side of a problem the customer's side has not
    heard about is not decision-free."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, _, allocation = seed(session)
        exception = main.mark_fulfillment_exception(session, allocation.id, "missing")
        session.commit()
        exception_id = exception.id
        assert exception.submission_state == "needs_submission"

    client = TestClient(main.app)
    assert client.post(ROUTE, data={"exception_ids": [str(exception_id)]}).status_code == 409
    with Session(db) as session:
        assert session.get(
            FulfillmentException, exception_id
        ).inventory_resolution_state == "unresolved"


def test_removal_reason_changed_underneath_makes_it_ineligible(tmp_path, monkeypatch):
    """The card being removed is not enough -- it must have been removed
    BY this fulfillment flow. A card removed for some other reason is a
    different story that needs reading."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        exception = make_missing(session)
        exception_id = exception.id
        card = session.get(InventoryCard, exception.inventory_card_id)
        card.removal_reason = "other"
        session.commit()

    client = TestClient(main.app)
    assert client.post(ROUTE, data={"exception_ids": [str(exception_id)]}).status_code == 409
    with Session(db) as session:
        assert session.get(
            FulfillmentException, exception_id
        ).inventory_resolution_state == "unresolved"


# --- the happy path -----------------------------------------------------

def test_eligible_exceptions_are_closed_out_without_touching_the_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        first = make_missing(session)
        second = make_missing(session)
        ids = [first.id, second.id]
        card_ids = [first.inventory_card_id, second.inventory_card_id]

    client = TestClient(main.app)
    response = client.post(ROUTE, data={"exception_ids": [str(i) for i in ids]})
    assert response.status_code == 200
    assert "2" in response.text

    with Session(db) as session:
        for exception_id, card_id in zip(ids, card_ids):
            exception = session.get(FulfillmentException, exception_id)
            assert exception.inventory_resolution_state == "resolved"
            assert exception.inventory_resolved_at is not None
            assert "permanently absent" in exception.resolution_note
            card = session.get(InventoryCard, card_id)
            # the card was already gone; closing the record must not move it
            assert card.status == "removed"
            assert card.inventory_exception_state == "none"
            assert session.get(PickAllocation, exception.pick_allocation_id).status == "exception"


def test_close_out_writes_the_normal_audit_trail(tmp_path, monkeypatch):
    """Same event and same projection audit the single-exception resolver
    already writes -- this action adds no audit format of its own."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        exception = make_missing(session)
        exception_id, card_id = exception.id, exception.inventory_card_id

    TestClient(main.app).post(ROUTE, data={"exception_ids": [str(exception_id)]})

    with Session(db) as session:
        events = session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception_id,
        ).all()
        resolved = [e for e in events if e.new_state == "resolved"]
        assert len(resolved) == 1
        assert resolved[0].previous_state == "unresolved"

        logs = session.query(InventoryChangeLog).filter_by(inventory_card_id=card_id).all()
        assert any("fulfillment_exception_inventory_resolved" in l.change_summary for l in logs)


def test_empty_selection_changes_nothing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        exception_id = make_missing(session).id

    client = TestClient(main.app)
    assert client.post(ROUTE, data={}).status_code == 400
    with Session(db) as session:
        assert session.get(
            FulfillmentException, exception_id
        ).inventory_resolution_state == "unresolved"


def test_rerunning_the_same_selection_does_not_double_resolve(tmp_path, monkeypatch):
    """Second run: the rows are no longer unresolved, so they are no
    longer eligible and the action refuses rather than re-writing."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        exception_id = make_missing(session).id

    client = TestClient(main.app)
    assert client.post(ROUTE, data={"exception_ids": [str(exception_id)]}).status_code == 200
    assert client.post(ROUTE, data={"exception_ids": [str(exception_id)]}).status_code == 409

    with Session(db) as session:
        events = session.query(FulfillmentExceptionEvent).filter_by(
            fulfillment_exception_id=exception_id,
        ).all()
        assert len([e for e in events if e.new_state == "resolved"]) == 1


# --- what the page offers -----------------------------------------------

def test_page_offers_a_checkbox_only_for_the_decision_free_rows(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        eligible = make_missing(session).id
        needs_review = make_mismatch(session).id

    text = TestClient(main.app).get(PAGE).text
    assert f'name="exception_ids" value="{eligible}"' in text
    assert f'name="exception_ids" value="{needs_review}"' not in text
    assert 'id="bulk-accept-missing-form"' in text
    assert "Accept checked as permanently absent" in text


def test_bulk_control_is_absent_when_nothing_qualifies(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_mismatch(session)

    text = TestClient(main.app).get(PAGE).text
    assert 'id="bulk-accept-missing-form"' not in text
    assert "Accept checked as permanently absent" not in text


def test_checkboxes_are_not_nested_inside_the_per_row_action_forms(tmp_path, monkeypatch):
    """The action cell already contains forms; a nested <form> is invalid
    HTML and browsers drop the inner one. The checkbox is bound by the
    form= attribute instead, so it must carry one."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        make_missing(session)

    text = TestClient(main.app).get(PAGE).text
    section = text[text.index("<h2>Fulfillment exceptions awaiting close-out</h2>"):]
    table = section[:section.index("</table>")]

    # the checkbox is inside the table and carries the binding attribute
    assert 'name="exception_ids"' in table
    assert 'form="bulk-accept-missing-form"' in table
    # and the form it names is declared OUTSIDE that table, after it
    assert '<form id="bulk-accept-missing-form"' not in table
    assert '<form id="bulk-accept-missing-form"' in section[section.index("</table>"):]
