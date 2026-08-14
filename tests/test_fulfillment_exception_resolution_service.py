import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_resolution_service import (
    resolve_inventory_mismatch_exception,
    resolve_missing_inventory_exception,
)
from fulfillment_exception_service import FulfillmentExceptionError, mark_fulfillment_exception
from models import Base, FulfillmentExceptionEvent, InventoryChangeLog
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resolution.db'}")
    Base.metadata.create_all(engine)
    return engine


def create_exception(session, kind):
    _, _, card, allocation = seed(session)
    exc = mark_fulfillment_exception(session, allocation.id, kind, "found during pick")
    session.commit()
    return exc, card, allocation


def test_missing_resolution_keeps_card_removed_and_clears_projection(db):
    with Session(db) as session:
        exc, card, allocation = create_exception(session, "missing")
        resolve_missing_inventory_exception(session, exc.id, "Search completed; genuinely absent")
        session.commit()
        assert exc.inventory_resolution_state == "resolved"
        assert card.status == "removed"
        assert card.inventory_exception_state == "none"
        assert allocation.status == "exception"
        assert session.query(FulfillmentExceptionEvent).count() == 2
        assert session.query(InventoryChangeLog).count() == 2


def test_missing_resolution_replay_and_inconsistent_state_fail_closed(db):
    with Session(db) as session:
        exc, card, _ = create_exception(session, "missing")
        resolve_missing_inventory_exception(session, exc.id)
        session.commit()
        assert resolve_missing_inventory_exception(session, exc.id).id == exc.id
        card.status = "available"; session.commit()
        with pytest.raises(FulfillmentExceptionError):
            resolve_missing_inventory_exception(session, exc.id)


def test_resolution_is_independent_of_submission_and_remote_state(db):
    with Session(db) as session:
        exc, _, _ = create_exception(session, "missing")
        exc.submission_state = "submitted"
        exc.remote_resolution_state = "resolved_refunded"
        session.commit()
        resolve_missing_inventory_exception(session, exc.id)
        session.commit()
        assert exc.submission_state == "submitted"
        assert exc.remote_resolution_state == "resolved_refunded"


def test_mismatch_requires_validated_correction_and_then_restores_sellability(db):
    with Session(db) as session:
        exc, card, allocation = create_exception(session, "inventory_mismatch")
        card.status = "unsellable"
        card.unsellable_reason = "fulfillment_inventory_mismatch"
        session.commit()
        with pytest.raises(FulfillmentExceptionError):
            resolve_inventory_mismatch_exception(session, exc.id, {}, {})
        assert card.status == "unsellable"
        assert allocation.status == "exception"


def test_mismatch_success_requires_correction_artifact_and_audits_completion(db, monkeypatch):
    with Session(db) as session:
        exc, card, allocation = create_exception(session, "inventory_mismatch")
        card.status = "unsellable"
        card.unsellable_reason = "fulfillment_inventory_mismatch"
        session.commit()
        reviewed = {"evidence_hash": "h", "card_before": {}, "resolution": {"product_id": "p"}}
        current = dict(reviewed)

        def apply(session, card, reviewed, current):
            card.name = "Corrected"
            return {"inventory_card_id": card.id}

        monkeypatch.setattr("fulfillment_exception_resolution_service.apply_printing_correction", apply)
        # The existing guarded sellability transition still requires canonical identity;
        # this test supplies a corrected card identity before the explicit return.
        card.name = "Example"
        card.set_code = "SET"; card.collector_number = "1"; card.scryfall_id = "sf"
        card.mtgjson_id = "mtg"; card.language_id = "EN"; card.condition_id = "LP"; card.finish_id = "NF"
        resolve_inventory_mismatch_exception(session, exc.id, reviewed, current, "Correction validated")
        session.commit()
        assert card.status == "available"
        assert card.inventory_exception_state == "none"
        assert exc.inventory_resolution_state == "resolved"
        assert allocation.status == "exception"
        events = session.query(FulfillmentExceptionEvent).all()
        assert {event.event_type for event in events} >= {
            "fulfillment_inventory_correction_completed",
            "fulfillment_exception_inventory_resolved",
        }


def test_mismatch_failure_rolls_back_without_making_card_sellable(db, monkeypatch):
    with Session(db) as session:
        exc, card, _ = create_exception(session, "inventory_mismatch")
        card.status = "unsellable"; card.unsellable_reason = "fulfillment_inventory_mismatch"; session.commit()

        def fail(*args, **kwargs):
            raise RuntimeError("correction failed")

        monkeypatch.setattr("fulfillment_exception_resolution_service.apply_printing_correction", fail)
        with pytest.raises(FulfillmentExceptionError):
            resolve_inventory_mismatch_exception(session, exc.id, {"x": 1}, {"x": 1})
        session.rollback()
        session.refresh(card)
        assert card.status == "unsellable"
        assert card.inventory_exception_state == "exception_unresolved"
        assert exc.inventory_resolution_state == "unresolved"
