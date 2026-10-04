"""Filling placed_at automatically, and reporting what could not be filled.

WHY. An OPEN order with no placed_at has no shipping deadline, so
attention_service._late_order_items skips it -- and a skipped order is
INDISTINGUISHABLE from a punctual one: zero late orders and zero
measurable orders render identically. Order 638925-2261040 sat in that
state and shipped ~6 days late; the account was restricted.

So the tick fills what it can (one documented read per order, paced and
capped) and the alarm reports only the residue. A warning with no action
attached would just be an instruction to go and run a script.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service as att
import order_service as osvc
from models import Base, SalesOrder

PLACED_ISO = "2026-09-24T06:17:35.339Z"
PLACED = datetime(2026, 9, 24, 6, 17, 35, 339000)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'placed.db'}")
    Base.metadata.create_all(engine)
    return engine


def order(session, uuid, status="ready_to_pick", placed_at=None):
    row = SalesOrder(external_order_id=uuid, external_label=f"L-{uuid}",
                     source="manapool", status=status, placed_at=placed_at)
    session.add(row)
    session.flush()
    return row


def loader(created_at=PLACED_ISO):
    return lambda remote_id: {"order": {"id": remote_id, "created_at": created_at}}


# --- the fill ------------------------------------------------------------

def test_an_open_order_with_no_date_is_filled_by_the_tick(db):
    with Session(db) as s:
        order(s, "u1")
        s.commit()
        report = osvc.fill_missing_placed_at(s, loader(), min_request_interval=0)
        assert report["filled"] == 1
        assert s.query(SalesOrder).one().placed_at == PLACED


def test_the_per_tick_cap_holds_and_a_sixth_order_waits(db):
    """Mana Pool's limit bounds total request COUNT in a rolling window, not
    just rate, so the cap matters as much as the pacing."""
    with Session(db) as s:
        for n in range(6):
            order(s, f"u{n}")
        s.commit()
        report = osvc.fill_missing_placed_at(s, loader(), min_request_interval=0)
        assert report["candidates"] == osvc.PLACED_AT_FILL_MAX_PER_RUN == 5
        assert report["filled"] == 5
        # deferred is computed from the POPULATION, so the cap cannot make a
        # backlog look smaller than it is.
        assert report["deferred"] == 1
        assert osvc.count_orders_missing_placed_at(s) == 1

        # The next tick picks up the remaining one.
        assert osvc.fill_missing_placed_at(s, loader(), min_request_interval=0)["filled"] == 1
        assert osvc.count_orders_missing_placed_at(s) == 0


def test_a_settled_order_is_never_a_candidate(db):
    """4,325 settled orders have no placed_at and must not be touched --
    they cannot be late, and the operator decided a historical backfill is
    not worth the requests."""
    with Session(db) as s:
        for status in ("shipped", "cancelled", "delivered"):
            order(s, f"settled-{status}", status=status)
        s.commit()
        assert osvc.fill_missing_placed_at(s, loader(), min_request_interval=0)["candidates"] == 0
        assert osvc.count_orders_missing_placed_at(s) == 0


def test_the_fill_is_newest_first(db):
    with Session(db) as s:
        for n in range(8):
            order(s, f"u{n}")
        s.commit()
        picked = [o.external_order_id
                  for o in osvc.orders_missing_placed_at(s, limit=3)]
        assert picked == ["u7", "u6", "u5"]


def test_one_unreadable_order_does_not_stop_the_rest(db):
    with Session(db) as s:
        order(s, "bad")
        order(s, "good")
        s.commit()

        def flaky(remote_id):
            if remote_id == "bad":
                raise RuntimeError("Mana Pool said no")
            return {"order": {"id": remote_id, "created_at": PLACED_ISO}}

        report = osvc.fill_missing_placed_at(s, flaky, min_request_interval=0)
        assert report["filled"] == 1 and report["failed"] == 1
        # The successful one is COMMITTED -- a later failure must not roll
        # back a date already recovered.
        assert s.query(SalesOrder).filter(
            SalesOrder.external_order_id == "good").one().placed_at == PLACED


def test_a_payload_with_no_date_is_counted_not_crashed(db):
    with Session(db) as s:
        order(s, "u1")
        s.commit()
        report = osvc.fill_missing_placed_at(s, loader(created_at=None),
                                             min_request_interval=0)
        assert report["no_remote_date"] == 1 and report["filled"] == 0


def test_an_existing_date_is_never_overwritten(db):
    """created_at is immutable, and letting a re-sync move it would quietly
    push a shipping deadline back."""
    earlier = datetime(2026, 9, 1, 0, 0, 0)
    with Session(db) as s:
        order(s, "u1", placed_at=earlier)
        s.commit()
        assert osvc.fill_missing_placed_at(s, loader(), min_request_interval=0)["candidates"] == 0
        assert s.query(SalesOrder).one().placed_at == earlier


# --- the coverage row ----------------------------------------------------

def test_the_residue_appears_on_attention_as_high(db):
    with Session(db) as s:
        order(s, "u1")
        s.commit()
        items = [i for i in att.collect(s)
                 if i.item_key == "coverage:placed_at"]
        assert len(items) == 1
        assert items[0].category == att.CATEGORY_LATE_ORDER
        assert items[0].urgency == "high"
        assert "cannot be checked for lateness" in items[0].summary
        assert att.badge_count(s) >= 1


def test_the_coverage_row_clears_once_filled(db):
    with Session(db) as s:
        order(s, "u1")
        s.commit()
        assert any(i.item_key == "coverage:placed_at" for i in att.collect(s))
        osvc.fill_missing_placed_at(s, loader(), min_request_interval=0)
        assert not any(i.item_key == "coverage:placed_at" for i in att.collect(s))


def test_a_dismissal_at_one_re_raises_at_two(db):
    with Session(db) as s:
        order(s, "u1")
        s.commit()
        item = [i for i in att.collect(s) if i.item_key == "coverage:placed_at"][0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    condition_hash_value=item.condition_hash, reason="chasing")
        s.commit()
        assert not any(i.item_key == "coverage:placed_at"
                       for i in att.outstanding(s))

        order(s, "u2")
        s.commit()
        assert any(i.item_key == "coverage:placed_at" for i in att.outstanding(s))


def test_the_coverage_count_uses_the_alarms_own_population(db):
    """If this measured a different population than the alarm, the row could
    read zero while the alarm was genuinely blind."""
    with Session(db) as s:
        order(s, "open-no-date")
        order(s, "shipped-no-date", status="shipped")
        order(s, "open-with-date", placed_at=PLACED)
        s.commit()
        assert osvc.count_orders_missing_placed_at(s) == 1
        assert len(osvc.orders_missing_placed_at(s)) == 1


# --- the skip paths, closed --------------------------------------------

def test_placed_at_survives_a_failed_ingest_rollback(db, monkeypatch):
    """THE REGRESSION. _apply_placed_at runs BEFORE _build_remote_items; a
    per-order rollback used to discard the date along with everything else,
    so a pre-existing order that failed every pass kept placed_at NULL
    forever -- no date, no deadline, no alarm. Exactly order 4303's state,
    where the raise was an IntegrityError on pick_allocations and so was
    NOT one of the InventoryAllocationErrors handled inside sync_one.
    """
    with Session(db) as s:
        order(s, "u1")
        s.commit()

    def exploding_build(*a, **k):
        raise RuntimeError("UNIQUE constraint failed: pick_allocations.inventory_card_id")

    monkeypatch.setattr(osvc, "_build_remote_items", exploding_build)

    with Session(db) as s:
        result = osvc.ingest_manapool_orders(
            s,
            [{"id": "u1", "created_at": PLACED_ISO}],
            loader(),
            min_request_interval=0,
        )
        assert len(result["failed"]) == 1, "the ingest must still report failure"

    with Session(db) as s:
        saved = s.query(SalesOrder).filter(
            SalesOrder.external_order_id == "u1").one()
        assert saved.placed_at == PLACED, "the order date must survive the rollback"
        # The failure itself is NOT papered over: the order keeps whatever
        # status it had and no items were invented for it.
        assert saved.status == "ready_to_pick"


def test_the_rollback_rescue_does_not_recreate_a_brand_new_order(db, monkeypatch):
    """After the rollback a NEW order's row is gone, and re-creating it here
    would be re-doing the ingest that just failed. That case has its own
    alarm (uningested_order_service), so the rescue must leave it alone."""
    def exploding_build(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(osvc, "_build_remote_items", exploding_build)

    with Session(db) as s:
        result = osvc.ingest_manapool_orders(
            s,
            [{"id": "brand-new", "created_at": PLACED_ISO}],
            loader(),
            min_request_interval=0,
        )
        assert len(result["failed"]) == 1

    with Session(db) as s:
        assert s.query(SalesOrder).count() == 0


def test_a_missing_created_at_is_logged_not_silent():
    """parse_remote_timestamp already logs a MALFORMED value; an ABSENT one
    returned None with no trace, the one unlogged path in the chain.

    A handler is attached to the `cardfoundry` logger directly -- caplog
    cannot see a logger with propagate=False, and that makes the assertion
    pass vacuously.
    """
    import logging

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    log = logging.getLogger("cardfoundry")
    log.addHandler(handler)
    previous = log.level
    log.setLevel(logging.WARNING)
    try:
        row = SalesOrder(external_order_id="u-nodate", source="manapool",
                         status="ready_to_pick")
        osvc._apply_placed_at(row, {}, {})
        assert row.placed_at is None
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)

    assert any("no order date" in m for m in records), records
    assert any("u-nodate" in m for m in records), records


# --- the backfill script -----------------------------------------------

def test_the_backfill_takes_the_NEWEST_orders(db):
    """It used to end `.order_by(SalesOrder.id)` -- ascending -- so --limit
    took the OLDEST orders, the wrong bias in both directions."""
    import backfill_placed_at as script

    with Session(db) as s:
        for n in range(6):
            order(s, f"u{n}")
        s.commit()
        picked = script.candidates(s, all_orders=False, limit=2)
        assert [o.external_order_id for o in picked] == ["u5", "u4"]


def test_the_backfill_can_target_one_order_whatever_its_status(db):
    """Order 4303 is `shipped`, so without this the only way to reach it was
    --all, which widens scope to every settled order."""
    import backfill_placed_at as script

    with Session(db) as s:
        shipped = order(s, "u-shipped", status="shipped")
        order(s, "u-open")
        s.commit()
        picked = script.candidates(s, all_orders=False, limit=None,
                                   order_id=shipped.id)
        assert [o.external_order_id for o in picked] == ["u-shipped"]


def test_the_backfill_dry_run_writes_nothing(db):
    import backfill_placed_at as script

    with Session(db) as s:
        row = order(s, "u1")
        s.commit()
        order_id = row.id

    with Session(db) as s:
        report = script.run(s, confirm=False, all_orders=False, limit=None,
                            order_id=order_id, detail_loader=loader(),
                            min_request_interval=0)
    assert report["filled"] == 1
    with Session(db) as s:
        assert s.get(SalesOrder, order_id).placed_at is None

    with Session(db) as s:
        report = script.run(s, confirm=True, all_orders=False, limit=None,
                            order_id=order_id, detail_loader=loader(),
                            min_request_interval=0)
    assert report["filled"] == 1
    with Session(db) as s:
        assert s.get(SalesOrder, order_id).placed_at == PLACED


def test_all_without_an_explicit_limit_is_refused():
    """Mana Pool's limit bounds total request COUNT in a rolling window, so
    pacing alone does not make an unbounded run safe."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "backfill_placed_at.py", "--all"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "--all needs an explicit --limit" in result.stderr


def test_all_with_a_limit_is_accepted(db):
    import backfill_placed_at as script

    with Session(db) as s:
        order(s, "u-shipped", status="shipped")
        s.commit()
        # --all reaches settled orders; the open-only default does not.
        assert len(script.candidates(s, all_orders=True, limit=5)) == 1
        assert len(script.candidates(s, all_orders=False, limit=5)) == 0
