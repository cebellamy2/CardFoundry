"""The scheduled-job alarm: a cron that stopped working must say so.

★ WHY THIS EXISTS. On 2026-10-12 the 02:30 perform-sync tick failed at the
reconciliation apply (job 531). That step runs BEFORE the new-listing
preview, so publishing never happened and 882 newly imported cards sat
unlisted for about six hours. Nothing anywhere said so: the cron scripts
only print() to their own service's stdout, which reads back empty, and
`inventory_sync_jobs` was the sole record -- which nothing read.

The same reconciliation error had already fired ten times (jobs 212, 215,
218, 221, 229, 396, 399, 402, 531, 544).

Two sides, tested separately because they fail differently:
  - a FAILED run fires immediately, because the hours lost are the point
  - a STALE run catches what a failure row cannot: the job never fired at
    all, or was skipped over and over

And one thing it must NOT do: alarm on a quiet hour. order_sync_health has
no run record to read, only `SalesOrder.last_synced_at`, which cannot tell
"nothing to sync" from "did not run" -- so it reports no signal rather
than guessing.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service as att
from models import (
    Base, Batch, DismissedAttentionItem, InventorySyncJob, PricingJob,
    SalesOrder,
)


@pytest.fixture
def db(tmp_path):
    """Pricing healthy so its own collector stays out of these assertions."""
    import json
    engine = create_engine(f"sqlite:///{tmp_path / 'sched.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        session.add(PricingJob(
            action="bulk_market_price_apply", status="completed",
            request_json="{}",
            response_json=json.dumps(
                {"summary": {"successful_items": 5000, "total_items": 5000}}),
            created_at=datetime.now() - timedelta(hours=1),
        ))
        session.commit()
    return engine


def sync_job(session, *, hours_ago, mode=None, status="completed"):
    session.add(InventorySyncJob(
        mode=mode or att.PERFORM_SYNC_SUCCESS_MODE, status=status,
        snapshot_json="{}",
        created_at=datetime.now() - timedelta(hours=hours_ago),
    ))
    session.flush()


def order(session, oid, *, status="ready_to_pick", synced_hours_ago=None):
    session.add(SalesOrder(
        id=oid, external_order_id=f"ext-{oid}", external_label=f"L{oid}",
        source="manapool", status=status, placed_at=datetime.now(),
        last_synced_at=(None if synced_hours_ago is None
                        else datetime.now() - timedelta(hours=synced_hours_ago)),
    ))
    session.flush()


def scheduled_items(session):
    return [i for i in att.collect(session, drift_rows=[])
            if i.category == att.CATEGORY_SCHEDULED_JOB]


# --- perform-sync: the failure side -------------------------------------

def test_a_failed_run_fires_immediately_even_when_the_success_is_recent(db):
    """★ THE CASE THAT COST SIX HOURS. Job 531 failed at 02:36 while the
    last success was only 4.6h earlier -- well inside the 9h warn window.
    Staleness alone would have stayed silent until 07:00."""
    with Session(db) as s:
        sync_job(s, hours_ago=4.6)
        sync_job(s, hours_ago=0.1, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                 status="failed")
        s.commit()

        health = att.perform_sync_health(s)
        assert health["state"] == "failed"
        assert "failed" in health["reason"]
        assert "unlisted" in health["reason"], (
            "it must say what the operator loses, not just that a job broke"
        )
        items = scheduled_items(s)
        assert len(items) == 1
        assert items[0].urgency == "high"
        assert "Perform Sync failed" in items[0].summary


def test_a_failure_older_than_the_last_success_does_not_fire(db):
    """The run after it worked. A healed job must go quiet by itself."""
    with Session(db) as s:
        sync_job(s, hours_ago=5, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                 status="failed")
        sync_job(s, hours_ago=1)
        s.commit()
        assert att.perform_sync_health(s)["state"] == "fresh"
        assert scheduled_items(s) == []


def test_a_skipped_run_alone_never_fires(db):
    """"Another inventory operation is already running" is a DESIGNED
    outcome -- the cron script itself treats it as success (exit 0).
    Alarming on it would be noise: 3 of the 10 attempt rows in the last 30
    days were skips."""
    with Session(db) as s:
        sync_job(s, hours_ago=1)
        for ago in (0.5, 0.3, 0.1):
            sync_job(s, hours_ago=ago, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                     status="skipped")
        s.commit()
        assert att.perform_sync_health(s)["state"] == "fresh"
        assert scheduled_items(s) == []


def test_repeated_skips_are_caught_by_staleness_not_by_the_skips(db):
    """Skips are not failures, but a job that only ever skips never runs.
    Staleness is what notices -- which is why both sides exist."""
    with Session(db) as s:
        sync_job(s, hours_ago=20)
        for ago in (12, 8, 4, 1):
            sync_job(s, hours_ago=ago, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                     status="skipped")
        s.commit()
        health = att.perform_sync_health(s)
        assert health["state"] == "alarm", "20h with no success is an alarm"
        assert len(scheduled_items(s)) == 1


# --- perform-sync: the staleness side -----------------------------------

@pytest.mark.parametrize("hours,expected", [
    (1, "fresh"),
    (8.9, "fresh"),
    (9, "warn"),
    (16.9, "warn"),
    (17, "alarm"),
    (40, "alarm"),
])
def test_the_thresholds_are_nine_and_seventeen_hours(db, hours, expected):
    """Perform-sync runs every 8h, so 9h is one missed cycle plus slack and
    17h is two, which cannot be a blip."""
    with Session(db) as s:
        sync_job(s, hours_ago=hours)
        s.commit()
        assert att.perform_sync_health(s)["state"] == expected


def test_never_having_succeeded_is_an_alarm(db):
    with Session(db) as s:
        assert att.perform_sync_health(s)["state"] == "alarm"
        assert "ever" in att.perform_sync_health(s)["reason"]


def test_maintenance_preview_alone_is_not_treated_as_success(db):
    """★ THE SIGNAL CHOICE, PINNED. maintenance_preview is written BEFORE
    the step that failed on 2026-10-12 -- it was 0.01h old at the moment
    job 531 failed. Counting it as success would have called that healthy.
    Only reaching new_listing_preview proves the chain finished."""
    with Session(db) as s:
        sync_job(s, hours_ago=0.01, mode="maintenance_preview")
        s.commit()
        health = att.perform_sync_health(s)
        assert health["state"] == "alarm", (
            "a maintenance preview proves the cron fired, not that it finished"
        )


def test_new_listing_apply_is_not_required_for_health(db):
    """★ THE OTHER HALF OF THE SIGNAL CHOICE. new_listing_apply is written
    only when there IS something to publish, so a genuinely quiet period
    has none. It was 32h old when job 531 failed -- requiring it would
    false-alarm for two days of normal trading."""
    with Session(db) as s:
        sync_job(s, hours_ago=1)  # preview only, nothing to publish
        s.commit()
        assert att.perform_sync_health(s)["state"] == "fresh"


# --- order sync ---------------------------------------------------------

def test_a_quiet_hour_with_no_open_orders_does_not_false_alarm(db):
    """★ THE SAFETY THAT MATTERS MOST HERE. last_synced_at cannot tell
    "the sync ran and had nothing to do" from "the sync did not run". With
    no open order to stamp the signal carries no information, so it must
    report that rather than guess."""
    with Session(db) as s:
        order(s, 1, status="shipped", synced_hours_ago=95)
        order(s, 2, status="cancelled", synced_hours_ago=94)
        s.commit()
        health = att.order_sync_health(s)
        assert health["state"] == "unknown"
        assert health["open_orders"] == 0
        assert "cannot tell" in health["reason"]
        assert [i for i in scheduled_items(s)
                if i.payload.get("job") == "order_sync"] == []


def test_open_orders_never_stamped_report_no_signal_rather_than_alarm(db):
    with Session(db) as s:
        order(s, 1, status="ready_to_pick", synced_hours_ago=None)
        s.commit()
        assert att.order_sync_health(s)["state"] == "unknown"


@pytest.mark.parametrize("hours,expected", [
    (0.5, "fresh"),
    (2.9, "fresh"),
    (3, "warn"),
    (5.9, "warn"),
    (6, "alarm"),
    (24, "alarm"),
])
def test_order_sync_thresholds_are_three_and_six_hours(db, hours, expected):
    """Hourly schedule, so a single miss is routine -- a deploy swapping
    the container does it. 3h is two misses plus slack; 6h is five."""
    with Session(db) as s:
        sync_job(s, hours_ago=1)
        order(s, 1, status="ready_to_pick", synced_hours_ago=hours)
        s.commit()
        assert att.order_sync_health(s)["state"] == expected


def test_a_settled_order_does_not_keep_order_sync_looking_healthy(db):
    """Measured on production: shipped and cancelled orders sat 94-95h
    stale while open ones were refreshed within the hour. Reading the max
    across ALL orders would have been meaningless; it reads open ones."""
    with Session(db) as s:
        sync_job(s, hours_ago=1)
        order(s, 1, status="shipped", synced_hours_ago=0.1)
        order(s, 2, status="ready_to_pick", synced_hours_ago=10)
        s.commit()
        health = att.order_sync_health(s)
        assert health["state"] == "alarm", (
            "a freshly-stamped SHIPPED order must not mask a stale open one"
        )


# --- clearing, dismissal and re-surfacing -------------------------------

def test_it_auto_clears_on_the_next_success(db):
    with Session(db) as s:
        sync_job(s, hours_ago=0.1, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                 status="failed")
        s.commit()
        assert len(scheduled_items(s)) == 1
        sync_job(s, hours_ago=0)  # the next run succeeds
        s.commit()
        assert scheduled_items(s) == [], "no acknowledgement needed"


def test_a_dismissal_silences_it_but_the_record_survives(db):
    with Session(db) as s:
        sync_job(s, hours_ago=0.1, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                 status="failed")
        s.commit()
        item = scheduled_items(s)[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="known, fixing the reconciliation step",
                    condition_hash_value=item.condition_hash)
        s.commit()

        assert att.outstanding(s, drift_rows=[]) == [] or all(
            i.category != att.CATEGORY_SCHEDULED_JOB
            for i in att.outstanding(s, drift_rows=[])
        )
        row = s.query(DismissedAttentionItem).one()
        assert row.reason == "known, fixing the reconciliation step"


def test_the_item_comes_back_when_the_condition_gets_worse(db):
    """★ A DISMISS IS NOT A MUTE. The hash is the BUCKET, so warn -> alarm
    returns. Hashing the hour instead would re-surface it every hour it
    stayed broken, which is nagging."""
    with Session(db) as s:
        sync_job(s, hours_ago=10)  # warn
        s.commit()
        warn_item = scheduled_items(s)[0]
        assert warn_item.payload["state"] == "warn"
        att.dismiss(s, category=warn_item.category,
                    item_key=warn_item.item_key, reason="seen it",
                    condition_hash_value=warn_item.condition_hash)
        s.commit()
        assert [i for i in att.outstanding(s, drift_rows=[])
                if i.category == att.CATEGORY_SCHEDULED_JOB] == []

    # It gets worse: the success recedes past the alarm threshold.
    with Session(db) as s:
        s.query(InventorySyncJob).filter(
            InventorySyncJob.mode == att.PERFORM_SYNC_SUCCESS_MODE,
        ).delete()
        sync_job(s, hours_ago=30)
        s.commit()
        back = [i for i in att.outstanding(s, drift_rows=[])
                if i.category == att.CATEGORY_SCHEDULED_JOB]
        assert len(back) == 1, "a worsening condition must re-surface"
        assert back[0].payload["state"] == "alarm"


def test_the_same_bucket_stays_dismissed_as_the_hours_tick_by(db):
    """Still warn, just later. Hashing the clock would re-nag hourly."""
    with Session(db) as s:
        sync_job(s, hours_ago=9.5)
        s.commit()
        item = scheduled_items(s)[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="seen it",
                    condition_hash_value=item.condition_hash)
        s.commit()
    with Session(db) as s:
        s.query(InventorySyncJob).filter(
            InventorySyncJob.mode == att.PERFORM_SYNC_SUCCESS_MODE,
        ).delete()
        sync_job(s, hours_ago=14)  # still warn
        s.commit()
        assert [i for i in att.outstanding(s, drift_rows=[])
                if i.category == att.CATEGORY_SCHEDULED_JOB] == [], (
            "the same bucket must stay quiet"
        )


# --- the badge ----------------------------------------------------------

def test_the_badge_count_includes_it(db):
    with Session(db) as s:
        # A healthy baseline FIRST: an empty inventory_sync_jobs table is
        # legitimately "no Perform Sync has ever succeeded", so without
        # this the count already includes an item and the test would
        # compare 1 to 1 and prove nothing.
        sync_job(s, hours_ago=1)
        s.commit()
        before = att.badge_count(s)

        sync_job(s, hours_ago=0.1, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                 status="failed")
        s.commit()
        assert att.badge_count(s) == before + 1


def test_the_badge_count_drops_again_when_dismissed(db):
    with Session(db) as s:
        sync_job(s, hours_ago=1)
        sync_job(s, hours_ago=0.1, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                 status="failed")
        s.commit()
        raised = att.badge_count(s)
        item = scheduled_items(s)[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="seen it",
                    condition_hash_value=item.condition_hash)
        s.commit()
        assert att.badge_count(s) == raised - 1


def test_both_jobs_can_raise_at_once_as_two_items(db):
    with Session(db) as s:
        sync_job(s, hours_ago=30)
        order(s, 1, status="ready_to_pick", synced_hours_ago=10)
        s.commit()
        items = scheduled_items(s)
        assert {i.payload["job"] for i in items} == {"perform_sync", "order_sync"}
        assert len(items) == 2


def test_the_detector_is_cheap_enough_for_every_page_load(db):
    """badge_count runs on EVERY page. attention_service's own rule: no
    Mana Pool calls and aggregate reads only. This pins the statement
    count the two new checks add."""
    from sqlalchemy import event

    with Session(db) as s:
        sync_job(s, hours_ago=1)
        order(s, 1, status="ready_to_pick", synced_hours_ago=0.5)
        s.commit()

        seen = []
        @event.listens_for(db, "before_cursor_execute")
        def count(conn, cursor, statement, params, context, many):
            seen.append(statement)

        try:
            att.perform_sync_health(s)
            att.order_sync_health(s)
        finally:
            event.remove(db, "before_cursor_execute", count)

        assert len(seen) == 2, (
            f"expected 2 aggregate reads (one per job, via conditional "
            f"aggregation), got {len(seen)}: {seen}"
        )
        for statement in seen:
            assert "max(" in statement.lower() or "count(" in statement.lower(), (
                f"every read must be an aggregate, not a row load: {statement}"
            )


# --- the log line and the page ------------------------------------------

def test_a_failed_attempt_logs_one_warning(caplog):
    """★ THE cardfoundry LOGGER DOES NOT PROPAGATE, so caplog cannot see it
    without being attached explicitly. A test that forgets this passes
    vacuously -- it asserts nothing and reports success."""
    import logging
    import main

    cardfoundry = logging.getLogger("cardfoundry")
    cardfoundry.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="cardfoundry"):
            class _FakeSession:
                def add(self, _obj):
                    pass
            main._record_sync_attempt_outcome(
                _FakeSession(), status="failed",
                reason="None of the reviewed rows are still valid to reconcile",
                triggered_by="scheduled",
            )
    finally:
        cardfoundry.removeHandler(caplog.handler)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {warnings}"
    text = warnings[0].getMessage()
    assert "perform sync FAILED" in text
    assert "scheduled" in text
    assert "still valid to reconcile" in text


def test_a_skipped_attempt_logs_nothing(caplog):
    """A busy lease is routine. Logging it at WARNING would make the line
    stop meaning "something broke"."""
    import logging
    import main

    cardfoundry = logging.getLogger("cardfoundry")
    cardfoundry.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="cardfoundry"):
            class _FakeSession:
                def add(self, _obj):
                    pass
            main._record_sync_attempt_outcome(
                _FakeSession(), status="skipped",
                reason="Another inventory operation is already running.",
                triggered_by="scheduled",
            )
    finally:
        cardfoundry.removeHandler(caplog.handler)

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_the_attention_page_shows_each_job_and_its_age(tmp_path, monkeypatch):
    """The outcome, not the mechanism: render the real page and read it."""
    import json
    import main
    import inventory_sync_service
    from fastapi.testclient import TestClient

    engine = create_engine(f"sqlite:///{tmp_path / 'page.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory",
                        lambda **kw: [])

    with Session(engine) as s:
        s.add(Batch(id=1, batch_code="B1"))
        s.add(PricingJob(
            action="bulk_market_price_apply", status="completed",
            request_json="{}",
            response_json=json.dumps(
                {"summary": {"successful_items": 50, "total_items": 50}}),
            created_at=datetime.now() - timedelta(hours=1)))
        sync_job(s, hours_ago=1)
        sync_job(s, hours_ago=0.1, mode=att.PERFORM_SYNC_ATTEMPT_MODE,
                 status="failed")
        order(s, 1, status="ready_to_pick", synced_hours_ago=0.5)
        s.commit()

    # The real path is /orders/needs-attention (Ticket B kept the URL and
    # added an alias); there is no /attention route.
    page = TestClient(main.app).get("/orders/needs-attention")
    assert page.status_code == 200
    assert "Scheduled jobs" in page.text
    assert "Perform Sync" in page.text
    assert "Order Sync" in page.text
    assert "Last run failed" in page.text, "the failed state must be named"
    assert "Healthy" in page.text, "and a healthy job must be confirmable"
    # The badge, which renders on every page via page_start.
    assert 'class="nav-badge"' in page.text
