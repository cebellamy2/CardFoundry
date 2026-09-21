"""The bulk "accept as permanently absent" action was removed.

CF-AUTORESOLVE-001 (2026-09-21). It offered a checkbox only for
exceptions already reported to Mana Pool whose inventory record was
still open -- a combination submission now closes by itself, so the
selection could only ever come back empty.

This file replaces tests/test_bulk_accept_missing_exceptions.py, which
tested the removed feature in full. Kept as a test rather than deleted
outright so the removal is pinned: a route quietly coming back, or the
checkbox column reappearing, should fail here.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base
from tests.test_fulfillment_exception_reconciliation import submit_unresolved
from tests.test_fulfillment_exception_service import seed


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'bulk-accept-removed.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_the_bulk_accept_route_no_longer_exists(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/fulfillment-exceptions/bulk-accept-missing",
        data={"exception_ids": ["1"]},
    )
    assert response.status_code == 404


def test_the_close_out_route_no_longer_exists(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/fulfillment-exceptions/1/close-out-inventory")
    assert response.status_code == 404


def test_the_attention_page_offers_neither_control(tmp_path, monkeypatch):
    """Rendered with a real open exception present, so an empty table is
    not what makes this pass."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, _, allocation = seed(session)
        submit_unresolved(session, allocation)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert "bulk-accept-missing" not in response.text
    assert "close-out-inventory" not in response.text
    assert "Accept checked as permanently absent" not in response.text
    assert "Close out inventory record" not in response.text
    # the row itself is still there -- only the two controls went
    assert "Fulfillment exceptions awaiting close-out" in response.text


def test_the_resolve_button_is_untouched(tmp_path, monkeypatch):
    """The path the operator kept. Resolve records what Mana Pool said;
    it never closed the inventory record and still does not."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _, _, _, allocation = seed(session)
        exception = submit_unresolved(session, allocation)
        session.commit()
        exception_id = exception.id

    client = TestClient(main.app)
    response = client.get("/orders/shipment-sync-issues")
    assert response.status_code == 200
    assert f"/fulfillment-exceptions/{exception_id}/resolve" in response.text
