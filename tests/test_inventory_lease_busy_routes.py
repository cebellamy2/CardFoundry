"""A busy inventory lease must never be a 500.

Found live 2026-09-15: an operator clicked Confirm on a printing
correction while the hourly Mana Pool order sync held the shared lease,
and got a raw traceback. The lease is held for the whole sync run, which
paces one Mana Pool call per second per listed order, so the collision
window is tens of seconds on every tick.

The condition itself is ordinary and expected -- a concurrent click, an
overlapping scheduled sync -- and @inventory_locked already renders it as
a clean 409. What failed is that two routes took the lease outside that
decorator: one inline inside a try whose except clause lists ValueError
(InventoryLeaseBusy is a RuntimeError, so it escaped), and one through a
lease-taking service wrapper with no handler at all.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from inventory_sync_service import InventoryLeaseBusy
from models import Base, ImportRecord


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'lease.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def test_the_busy_error_is_not_a_value_error(db):
    """The exact reason the original except clause missed it. If this ever
    becomes true, the old handler would start working by accident and the
    real lesson would be lost."""
    assert issubclass(InventoryLeaseBusy, RuntimeError)
    assert not issubclass(InventoryLeaseBusy, ValueError)


def test_printing_correction_confirm_returns_409_not_500(db, monkeypatch):
    """The route that actually broke in production."""
    def busy(*args, **kwargs):
        raise InventoryLeaseBusy("Another inventory operation is already running.")

    monkeypatch.setattr(inventory_sync_service, "acquire_inventory_lease", busy)

    response = TestClient(main.app, raise_server_exceptions=False).post(
        "/inventory/7077/printing-correction/confirm",
        data={"replacement_scryfall_id": "sf-x", "reviewed_json": "{}"},
    )
    assert response.status_code == 409
    assert "Another inventory operation is already running" in response.text


def test_import_undo_returns_409_not_500(db, monkeypatch):
    """The sibling found in the same pass. It calls a lease-taking service
    wrapper, so it catches rather than decorating -- decorating would take
    the lease a second time around one that already holds it."""
    with Session(db) as session:
        session.add(ImportRecord(
            batch_id=1, filename="x.csv", file_hash="h", card_count=1, status="active",
        ))
        session.commit()
        import_id = session.query(ImportRecord).one().id

    def busy(*args, **kwargs):
        raise InventoryLeaseBusy("Another inventory operation is already running.")

    monkeypatch.setattr(main, "remove_import_cards", busy)

    response = TestClient(main.app, raise_server_exceptions=False).post(
        f"/imports/{import_id}/undo", data={"note": "undo please"},
    )
    assert response.status_code == 409
    assert "Another inventory operation is already running" in response.text


def test_the_import_record_is_untouched_when_the_lease_is_busy(db, monkeypatch):
    """Nothing half-applies: the lease is refused before any work starts."""
    with Session(db) as session:
        session.add(ImportRecord(
            batch_id=1, filename="y.csv", file_hash="h2", card_count=1, status="active",
        ))
        session.commit()
        import_id = session.query(ImportRecord).one().id

    def busy(*args, **kwargs):
        raise InventoryLeaseBusy("Another inventory operation is already running.")

    monkeypatch.setattr(main, "remove_import_cards", busy)
    TestClient(main.app, raise_server_exceptions=False).post(
        f"/imports/{import_id}/undo", data={"note": "undo please"},
    )

    with Session(db) as session:
        assert session.get(ImportRecord, import_id).status == "active"


def test_every_route_taking_the_lease_inline_also_handles_it_being_busy(db):
    """The structural guard, stated as the actual safety property.

    A route may take the lease inline OR through @inventory_locked. What
    it may NOT do is take it inline and leave the busy case unhandled --
    that is exactly what produced the live 500. new_listing_apply_route
    takes it inline and catches explicitly, which is fine and is why this
    asserts the property rather than banning the pattern.
    """
    import re

    source = open("main.py").read()
    blocks = re.split(r"\n@app\.(?:get|post)\(", source)
    offenders = []
    for block in blocks[1:]:
        if "\ndef " not in block:
            continue
        decorators = block.split("\ndef ", 1)[0]
        name = block.split("\ndef ", 1)[1].split("(", 1)[0]
        body = block.split("\n@app.")[0]
        if "with inventory_sync_lease()" not in body:
            continue
        handled = (
            "@inventory_locked" in decorators
            or "InventoryLeaseBusy" in body
        )
        if not handled:
            offenders.append(name)
    assert offenders == [], (
        "routes take the inventory lease inline without handling it being "
        f"busy, so a concurrent sync would 500: {offenders}"
    )
