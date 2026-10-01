"""Mana Pool orders that never became local orders.

WHY THIS EXISTS. Every attention category iterates LOCAL rows, so an
order with no local SalesOrder is invisible to all of them -- including
v2.15.0's shipping-deadline alert, which queries SalesOrder. Order
638925-2261040 (local 4303) was delivered by webhook eight seconds after
it was placed, failed ingest on the pre-v2.8.0 UNIQUE index in BOTH
paths, had no local row for 4.7 days, and shipped ~6 days late. The
account was restricted. No alarm fired because there was nothing to
alarm about yet.

The replay of that exact situation is `test_order_4303_situation_fires`.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service as att
import uningested_order_service as svc
from models import Base, SalesOrder, UningestedRemoteOrder

PLACED = "2026-09-24T06:17:35.339Z"
REMOTE_ID = "1706883c-475a-4c0b-b2c5-cd296b64a55f"
LABEL = "638925-2261040"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'uningested.db'}")
    Base.metadata.create_all(engine)
    return engine


def listing(remote_id=REMOTE_ID, label=LABEL, created_at=PLACED):
    return [{"id": remote_id, "label": label, "created_at": created_at}]


def local_order(remote_id=REMOTE_ID, status="ready_to_pick"):
    return SalesOrder(
        external_order_id=remote_id, external_label=LABEL,
        source="manapool", status=status,
    )


# --- the core condition -------------------------------------------------

def test_remote_order_with_no_local_row_is_recorded(db):
    with Session(db) as session:
        summary = svc.record_uningested_orders(session, listing(), [])
        session.commit()
        assert summary["unresolved"] == 1
        assert summary["newly_recorded"] == 1
        rows = svc.unresolved_orders(session)
        assert len(rows) == 1
        # The label is what the operator can actually look up on Mana
        # Pool; the UUID is not searchable there.
        assert rows[0].external_label == LABEL
        # The order's OWN date, not when we noticed.
        assert rows[0].remote_created_at == datetime(2026, 9, 24, 6, 17, 35, 339000)
        assert rows[0].failure_reason is None


def test_it_appears_on_attention_and_raises_the_badge(db):
    with Session(db) as session:
        svc.record_uningested_orders(session, listing(), [])
        session.commit()

        items = att.collect(session)
        mine = [i for i in items if i.category == att.CATEGORY_UNINGESTED_ORDER]
        assert len(mine) == 1
        assert mine[0].urgency == "high"
        assert LABEL in mine[0].summary
        # The badge must move: a blind alarm that does not raise the badge
        # reproduces the original failure exactly.
        assert att.badge_count(session) >= 1
        assert att._candidate_counts(session)[att.CATEGORY_UNINGESTED_ORDER] == 1


def test_a_failed_ingest_is_recorded_with_its_reason(db):
    with Session(db) as session:
        # The order row exists, but THIS pass failed against it -- the
        # shape that leaves placed_at NULL forever, because
        # _apply_placed_at runs before _build_remote_items and the
        # per-order rollback discards it.
        session.add(local_order())
        session.commit()
        svc.record_uningested_orders(
            session, listing(),
            [f"{REMOTE_ID}: IntegrityError: UNIQUE constraint failed"],
        )
        session.commit()
        rows = svc.unresolved_orders(session)
        assert len(rows) == 1
        assert "UNIQUE constraint failed" in rows[0].failure_reason
        items = [i for i in att.collect(session)
                 if i.category == att.CATEGORY_UNINGESTED_ORDER]
        assert "could not be synced" in items[0].summary
        assert "UNIQUE constraint failed" in items[0].detail


def test_it_clears_once_the_order_is_ingested(db):
    with Session(db) as session:
        svc.record_uningested_orders(session, listing(), [])
        session.commit()
        assert len(svc.unresolved_orders(session)) == 1

        # The next tick: the order landed and nothing failed.
        session.add(local_order())
        session.commit()
        summary = svc.record_uningested_orders(session, listing(), [])
        session.commit()

        assert summary["resolved"] == 1
        assert summary["unresolved"] == 0
        assert svc.unresolved_orders(session) == []
        assert att._candidate_counts(session)[att.CATEGORY_UNINGESTED_ORDER] == 0
        # The row is KEPT as the record that the gap happened.
        assert session.query(UningestedRemoteOrder).count() == 1


def test_order_4303_situation_fires(db):
    """The replay: verified webhook delivered, ingest raises, the hourly
    sync fails the same way. No local row exists, so no other alarm can
    see it -- this one must."""
    with Session(db) as session:
        failure = (f"{REMOTE_ID}: IntegrityError: UNIQUE constraint failed: "
                   "pick_allocations.inventory_card_id")
        # Five consecutive hourly ticks, all failing identically.
        for _ in range(5):
            svc.record_uningested_orders(session, listing(), [failure])
            session.commit()

        rows = svc.unresolved_orders(session)
        assert len(rows) == 1, "repeated failures must not duplicate the row"
        assert rows[0].first_seen_at <= rows[0].last_seen_at
        items = [i for i in att.collect(session)
                 if i.category == att.CATEGORY_UNINGESTED_ORDER]
        assert len(items) == 1
        assert items[0].urgency == "high"
        # No local SalesOrder exists at all, which is the whole point.
        assert session.query(SalesOrder).count() == 0


# --- dismissal ----------------------------------------------------------

def test_a_second_missing_order_re_raises_a_dismissal(db):
    """One order Mana Pool has and we do not is a fault; two at once is a
    different and worse fact about the sync, so a judgement made about the
    first must not silence the pair."""
    with Session(db) as session:
        svc.record_uningested_orders(session, listing(), [])
        session.commit()
        item = [i for i in att.collect(session)
                if i.category == att.CATEGORY_UNINGESTED_ORDER][0]
        att.dismiss(
            session, category=item.category, item_key=item.item_key,
            condition_hash_value=item.condition_hash, reason="chasing it",
        )
        session.commit()
        assert [i for i in att.outstanding(session)
                if i.category == att.CATEGORY_UNINGESTED_ORDER] == []

        # A second one shows up.
        svc.record_uningested_orders(
            session,
            listing() + listing(remote_id="other-uuid", label="999-111"),
            [],
        )
        session.commit()
        still = [i for i in att.outstanding(session)
                 if i.category == att.CATEGORY_UNINGESTED_ORDER]
        assert len(still) == 2, "the dismissal must not survive a new order"


# --- the edges ----------------------------------------------------------

def test_an_order_that_leaves_the_listing_unresolved_stays_unresolved(db):
    """Dropping out of needs_shipping is not evidence the order is fine.
    If we never ingested it we have permanently missed it, and the listing
    can no longer tell us anything -- so only a local row settles it."""
    with Session(db) as session:
        svc.record_uningested_orders(session, listing(), [])
        session.commit()
        summary = svc.record_uningested_orders(session, [], [])
        session.commit()
        assert summary["unresolved"] == 1
        assert len(svc.unresolved_orders(session)) == 1


def test_an_order_that_left_the_listing_resolves_once_it_exists_locally(db):
    with Session(db) as session:
        svc.record_uningested_orders(session, listing(), [])
        session.commit()
        session.add(local_order(status="shipped"))
        session.commit()
        summary = svc.record_uningested_orders(session, [], [])
        session.commit()
        assert summary["resolved"] == 1
        assert svc.unresolved_orders(session) == []


def test_failures_from_other_sync_steps_are_not_mis_attributed():
    """Only ingest formats its failures as "<remote_id>: <error>". The
    cancellation pass and the short-order retry add free text to the same
    list, and a mis-attributed reason is worse than none."""
    parsed = svc.parse_ingest_failures([
        f"{REMOTE_ID}: IntegrityError: boom",
        "Could not reach Mana Pool",          # no colon -- skipped
        "",                                   # empty -- skipped
    ])
    assert parsed == {REMOTE_ID: "IntegrityError: boom"}


def test_a_listing_entry_with_no_id_is_ignored(db):
    with Session(db) as session:
        summary = svc.record_uningested_orders(session, [{"label": "no id"}], [])
        session.commit()
        assert summary["unresolved"] == 0
        assert session.query(UningestedRemoteOrder).count() == 0


def test_a_missing_remote_date_sorts_last_and_still_reports(db):
    with Session(db) as session:
        svc.record_uningested_orders(
            session,
            listing(remote_id="no-date", label="111-222", created_at=None)
            + listing(),
            [],
        )
        session.commit()
        rows = svc.unresolved_orders(session)
        assert [r.external_order_id for r in rows] == [REMOTE_ID, "no-date"]
        items = [i for i in att.collect(session)
                 if i.category == att.CATEGORY_UNINGESTED_ORDER]
        assert any("an unknown date" in i.summary for i in items)


# --- end to end, through the route the cron actually calls --------------

def test_the_hourly_sync_records_and_surfaces_a_missing_order(tmp_path, monkeypatch):
    """The real wiring: POST /manapool/sync is what the hourly cron hits.
    The listing is stubbed; everything downstream is the real code."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine as _create_engine

    import inventory_sync_service
    import main
    from models import AppSetting

    db = _create_engine(f"sqlite:///{tmp_path / 'route.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    with Session(db) as session:
        session.add(AppSetting(key=main.GO_LIVE_SETTING_KEY,
                               value="2026-01-01T00:00:00Z"))
        session.commit()

    # Mana Pool has this order. Ingest fails on it, exactly as it did for
    # order 4303 under the pre-v2.8.0 UNIQUE index.
    monkeypatch.setattr(main, "get_seller_orders", lambda since: {"orders": listing()})
    monkeypatch.setattr(
        main, "ingest_manapool_orders",
        lambda *a, **k: {
            "imported": 0, "already_known": 0, "deferred": 0,
            "failed": [f"{REMOTE_ID}: IntegrityError: UNIQUE constraint failed"],
        },
    )

    response = TestClient(main.app).post("/manapool/sync")
    assert response.status_code == 200

    with Session(db) as session:
        rows = svc.unresolved_orders(session)
        assert len(rows) == 1
        assert rows[0].external_label == LABEL
        assert "UNIQUE constraint failed" in rows[0].failure_reason
        assert att._candidate_counts(session)[att.CATEGORY_UNINGESTED_ORDER] == 1
