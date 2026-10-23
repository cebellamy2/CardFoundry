"""A locked database must not kill the webhook retry loop.

Delivery 721 (2026-10-23) is the case these pin. perform-sync held a
SQLite write transaction; the retry loop's own "attempts = 4" bookkeeping
write waited out the busy timeout and raised; the OperationalError
escaped and ended the loop with the row still `pending` and attempts
frozen at 3. The ingest had a handler for every outcome and the
bookkeeping had none -- so the one unguarded write was the one that
ended it, under exactly the condition the loop exists to ride out.

Driven through the real process_delivery against a real SQLite file,
with the lock injected at the one commit that failed in production.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
import manapool_webhook_service as wh
from inventory_sync_service import InventoryLeaseBusy
from models import Base, WebhookDelivery

ORDER_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'webhook_locked.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    return engine


def body(order_id=ORDER_ID) -> bytes:
    return json.dumps({"order": {"id": order_id, "label": "123-456"}}).encode()


def factory(engine):
    return lambda: Session(engine)


def record(engine) -> int:
    with Session(engine) as session:
        return wh.record_delivery(
            session, event="order_created", raw_body=body(),
            signature_status="verified", timestamp_header="1",
        )


def locked(statement="UPDATE webhook_deliveries SET attempts=?"):
    """The exact shape production raised, down to the driver exception."""
    import sqlite3
    return OperationalError(statement, {}, sqlite3.OperationalError("database is locked"))


class Clock:
    """A fake clock so a 600s budget costs no wall time."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def now(self):
        return self.t


def ingest_ok(monkeypatch, imported=1, already_known=0):
    def fake(session, orders, loader, **kw):
        return {"imported": imported, "already_known": already_known,
                "failed": [], "deferred": 0}
    monkeypatch.setattr(wh, "ingest_manapool_orders", fake)


def lock_the_bookkeeping(monkeypatch, times):
    """Make the FIRST `times` session commits raise a lock.

    Patched at the factory rather than inside the service, so the real
    commit the loop makes is the one that fails.
    """
    state = {"left": times}
    real_commit = Session.commit

    def commit(self):
        if state["left"] > 0:
            state["left"] -= 1
            raise locked()
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", commit)
    return state


# ---------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------

def test_a_locked_bookkeeping_write_is_waited_out_not_fatal(db, monkeypatch):
    ingest_ok(monkeypatch)
    delivery_id = record(db)
    lock_the_bookkeeping(monkeypatch, times=3)
    clock = Clock()

    status = wh.process_delivery(
        delivery_id, session_factory=factory(db),
        sleep=clock.sleep, now=clock.now,
    )

    assert status == "processed"
    assert clock.slept == [5, 10, 20]
    with Session(db) as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert row.processing_status == "processed"
        assert row.processed_at is not None


def test_a_lock_is_not_counted_as_an_attempt(db, monkeypatch):
    """Nothing was tried, so nothing is recorded as tried -- otherwise a
    run of locks would burn the sweep's 5-attempt cap without the order
    ever having been looked at."""
    ingest_ok(monkeypatch)
    delivery_id = record(db)
    lock_the_bookkeeping(monkeypatch, times=4)
    clock = Clock()

    wh.process_delivery(
        delivery_id, session_factory=factory(db),
        sleep=clock.sleep, now=clock.now,
    )

    with Session(db) as session:
        assert session.get(WebhookDelivery, delivery_id).attempts == 1


def test_the_row_never_ends_pending_with_the_work_done(db, monkeypatch):
    """The production symptom, stated as the thing that must not happen:
    an order already in the pick queue, and a row still asking to be
    chased."""
    ingest_ok(monkeypatch, imported=0, already_known=1)
    delivery_id = record(db)
    lock_the_bookkeeping(monkeypatch, times=3)
    clock = Clock()

    wh.process_delivery(
        delivery_id, session_factory=factory(db),
        sleep=clock.sleep, now=clock.now,
    )

    with Session(db) as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert row.processing_status == "already_known"
        assert row.processing_status not in wh.UNFINISHED_STATUSES
        assert session.query(WebhookDelivery).filter(
            WebhookDelivery.signature_status == "verified",
            WebhookDelivery.processing_status.in_(wh.UNFINISHED_STATUSES),
        ).count() == 0


def test_a_lock_that_never_clears_strands_rather_than_vanishing(db, monkeypatch):
    ingest_ok(monkeypatch)
    delivery_id = record(db)
    state = lock_the_bookkeeping(monkeypatch, times=10_000)
    clock = Clock()

    status = wh.process_delivery(
        delivery_id, session_factory=factory(db),
        sleep=clock.sleep, now=clock.now,
    )

    assert status == "stranded"
    assert sum(clock.slept) >= wh.RETRY_BUDGET_SECONDS
    assert clock.slept[:4] == [5, 10, 20, 30]
    # The terminal write is attempted too; it is locked as well here, so
    # the row stays unfinished for the sweep. Reported honestly either way.
    assert state["left"] < 10_000


def test_a_non_lock_operational_error_still_propagates(db, monkeypatch):
    """A missing column re-fails identically forever. Burning the budget
    on it would hide a real fault behind a retry."""
    ingest_ok(monkeypatch)
    delivery_id = record(db)
    import sqlite3
    real_commit = Session.commit
    state = {"first": True}

    def commit(self):
        if state["first"]:
            state["first"] = False
            raise OperationalError(
                "UPDATE webhook_deliveries SET attempts=?", {},
                sqlite3.OperationalError("no such column: attempts"),
            )
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", commit)
    clock = Clock()

    with pytest.raises(OperationalError, match="no such column"):
        wh.process_delivery(
            delivery_id, session_factory=factory(db),
            sleep=clock.sleep, now=clock.now,
        )
    assert clock.slept == []


# ---------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "database is locked", "database table is locked", "database is busy",
    "DATABASE IS LOCKED",
])
def test_lock_shapes_are_recognised(text):
    import sqlite3
    assert wh.is_locked_error(
        OperationalError("stmt", {}, sqlite3.OperationalError(text))
    ) is True


@pytest.mark.parametrize("exc", [
    ValueError("nope"),
    InventoryLeaseBusy("held"),
])
def test_other_exceptions_are_not_locks(exc):
    assert wh.is_locked_error(exc) is False


def test_a_non_lock_operational_error_is_not_a_lock():
    import sqlite3
    assert wh.is_locked_error(
        OperationalError("stmt", {}, sqlite3.OperationalError("no such table: x"))
    ) is False


# ---------------------------------------------------------------------
# The terminal write
# ---------------------------------------------------------------------

def test_the_terminal_write_waits_out_a_lock(db, monkeypatch):
    delivery_id = record(db)
    lock_the_bookkeeping(monkeypatch, times=2)
    slept = []

    status = wh._finish(
        factory(db), delivery_id, "processed", sleep=slept.append,
    )

    assert status == "processed"
    assert slept == [wh.FINISH_LOCK_WAIT_SECONDS] * 2
    with Session(db) as session:
        assert session.get(WebhookDelivery, delivery_id).processing_status == "processed"


def test_a_terminal_write_that_never_lands_leaves_the_row_for_the_sweep(db, monkeypatch):
    """Reported, not swallowed: the status it reached is returned, the
    row stays unfinished, and the sweep is named in the log.

    The log is captured with a handler of our own rather than caplog:
    the `cardfoundry` logger sets propagate = False (main.py), so
    caplog's root handler never sees a record from it.
    """
    import logging
    delivery_id = record(db)
    lock_the_bookkeeping(monkeypatch, times=10_000)
    said = []

    class Capture(logging.Handler):
        def emit(self, record):
            said.append(record.getMessage())

    handler = Capture()
    logging.getLogger("cardfoundry").addHandler(handler)
    try:
        status = wh._finish(factory(db), delivery_id, "processed", sleep=lambda _s: None)
    finally:
        logging.getLogger("cardfoundry").removeHandler(handler)

    assert status == "processed"
    assert any("sweep will retry it" in line for line in said), said
    with Session(db) as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert row.processing_status == "pending"
    # And the sweep genuinely does pick it up.
    with Session(db) as session:
        assert delivery_id in [r.id for r in wh.unfinished_deliveries(session)]


# ---------------------------------------------------------------------
# The backoff table, now shared by both waits
# ---------------------------------------------------------------------

def test_the_backoff_holds_at_the_last_value_rather_than_running_off_the_end():
    assert [wh._backoff_for(n) for n in (1, 2, 3, 4)] == [5, 10, 20, 30]
    assert wh._backoff_for(5) == 30
    assert wh._backoff_for(99) == 30
    # Defensive: a zeroth wait is still the first entry, never an
    # IndexError from a negative index.
    assert wh._backoff_for(0) == 5


def test_a_busy_lease_still_backs_off_the_way_it_always_did(db, monkeypatch):
    """The lease path now reads the same helper; pin that it did not
    change pace while being refactored."""
    def busy():
        raise InventoryLeaseBusy("held by another operation")
    monkeypatch.setattr(wh, "inventory_sync_lease", busy)
    delivery_id = record(db)
    clock = Clock()

    status = wh.process_delivery(
        delivery_id, session_factory=factory(db),
        sleep=clock.sleep, now=clock.now,
    )

    assert status == "stranded"
    assert clock.slept[:4] == [5, 10, 20, 30]
    assert set(clock.slept[4:-1]) <= {30}
    with Session(db) as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert "lease busy" in (row.last_error or "")
