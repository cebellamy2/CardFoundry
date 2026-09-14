"""Detectability for the class that produced five stranded exceptions.

validate_exception_card_projection could never catch these: it compares
the projection against inventory_resolution_state, and that pair stays
consistent while the card underneath drifts. All five stranded exceptions
passed it. This is the missing half -- whether the exception's own
resolver would still accept the card.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from fulfillment_exception_invariants import (
    exception_resolver_is_reachable,
    stranded_exception_reason,
    validate_exception_card_projection,
)
from fulfillment_exception_service import mark_fulfillment_exception
from models import Base, InventoryCard
from tests.test_fulfillment_exception_service import seed

PAGE = "/orders/shipment-sync-issues"


class FakeCard:
    def __init__(self, **kw):
        self.status = kw.get("status")
        self.removal_reason = kw.get("removal_reason")
        self.unsellable_reason = kw.get("unsellable_reason")
        self.inventory_exception_state = kw.get(
            "inventory_exception_state", "exception_unresolved",
        )


class FakeException:
    def __init__(self, **kw):
        self.exception_type = kw.get("exception_type", "inventory_mismatch")
        self.submission_state = kw.get("submission_state", "submitted")
        self.remote_resolution_state = kw.get("remote_resolution_state", "awaiting")
        self.inventory_resolution_state = kw.get("inventory_resolution_state", "unresolved")


def test_healthy_mismatch_is_reachable_and_not_stranded():
    exception = FakeException()
    card = FakeCard(status="unsellable", unsellable_reason="fulfillment_inventory_mismatch")
    assert exception_resolver_is_reachable(exception, card)
    assert stranded_exception_reason(exception, card) is None


def test_healthy_missing_is_reachable_and_not_stranded():
    exception = FakeException(exception_type="missing")
    card = FakeCard(status="removed", removal_reason="fulfillment_missing")
    assert exception_resolver_is_reachable(exception, card)
    assert stranded_exception_reason(exception, card) is None


def test_cleared_reason_is_detected_as_stranded():
    """#5, #18 and #33: removed for an unrelated reason."""
    exception = FakeException()
    card = FakeCard(status="removed", removal_reason="import_error")
    assert not exception_resolver_is_reachable(exception, card)
    reason = stranded_exception_reason(exception, card)
    assert reason and "no resolution path can close it" in reason


def test_returned_to_sellable_is_detected_as_stranded():
    """#17: available with no reason at all."""
    exception = FakeException()
    card = FakeCard(status="available")
    assert stranded_exception_reason(exception, card) is not None


def test_rewritten_removal_reason_is_detected_as_stranded():
    """#2: removed by the exception flow, then corrected to "other"."""
    exception = FakeException(exception_type="missing")
    card = FakeCard(status="removed", removal_reason="other")
    assert stranded_exception_reason(exception, card) is not None


def test_a_terminal_remote_outcome_is_not_stranded():
    """Close out inventory record still reaches these -- a different door,
    but a real one. Reporting them as stranded would be wrong."""
    exception = FakeException(remote_resolution_state="resolved_replaced")
    card = FakeCard(status="available")
    assert stranded_exception_reason(exception, card) is None


def test_a_resolved_exception_is_never_stranded():
    exception = FakeException(inventory_resolution_state="resolved")
    card = FakeCard(status="available", inventory_exception_state="none")
    assert stranded_exception_reason(exception, card) is None


def test_the_old_projection_invariant_cannot_see_this():
    """The point of the new check: the projection invariant passes on a
    card the resolver can no longer accept. That is why nothing flagged
    five stranded exceptions."""
    exception = FakeException()
    card = FakeCard(status="available", inventory_exception_state="exception_unresolved")
    assert validate_exception_card_projection(exception, card) is True
    assert stranded_exception_reason(exception, card) is not None


# --- surfaced on the page -----------------------------------------------

def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'stranded.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_page_flags_a_stranded_exception_and_says_why(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, card, allocation = seed(session)
        mark_fulfillment_exception(session, allocation.id, "inventory_mismatch")
        session.commit()
        # the manual edit that strands it, applied directly so the test
        # reproduces a pre-guard row rather than fighting the new guard
        card = session.get(InventoryCard, card.id)
        card.status = "available"
        card.unsellable_reason = None
        session.commit()

    text = TestClient(main.app).get(PAGE).text
    assert "cannot be closed by any action here" in text
    assert "Stranded:" in text


def test_page_shows_no_stranded_warning_when_everything_is_reachable(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, _, allocation = seed(session)
        mark_fulfillment_exception(session, allocation.id, "inventory_mismatch")
        session.commit()

    text = TestClient(main.app).get(PAGE).text
    assert "cannot be closed by any action here" not in text
    assert "Stranded:" not in text
