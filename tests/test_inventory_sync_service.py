from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
from inventory_sync_service import LEASE_NAME, InventoryLeaseBusy, inventory_locked
from models import Base, InventorySyncLease


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory_lock.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def _seed_active_lease(db, ttl_seconds=900):
    with Session(db) as session:
        session.add(InventorySyncLease(
            name=LEASE_NAME, owner_token="someone-elses-run",
            acquired_at=datetime.now(),
            expires_at=datetime.now() + timedelta(seconds=ttl_seconds),
        ))
        session.commit()


def test_inventory_locked_runs_the_function_when_lease_is_free(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    calls = []

    @inventory_locked
    def route():
        calls.append("ran")
        return "ok"

    assert route() == "ok"
    assert calls == ["ran"]


def test_inventory_locked_returns_clean_409_when_lease_is_busy(tmp_path, monkeypatch):
    """The exact production incident: an orphaned/active lease from another
    operation must not crash this route with a raw traceback."""
    db = setup_db(tmp_path, monkeypatch)
    _seed_active_lease(db)
    calls = []

    @inventory_locked
    def route():
        calls.append("ran")  # pragma: no cover -- must never actually run
        return "ok"

    response = route()

    assert calls == []
    assert response.status_code == 409
    body = response.body.decode()
    assert "Another inventory operation is already running" in body
    assert "Traceback" not in body


def test_inventory_locked_never_raises_inventory_lease_busy(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    _seed_active_lease(db)

    @inventory_locked
    def route():
        raise AssertionError("should never run")  # pragma: no cover

    try:
        route()
    except InventoryLeaseBusy:
        raise AssertionError("InventoryLeaseBusy must be caught, not propagated")
