"""Stopped-working alarms for the remaining crons (operator, 2026-10-27).

Colour backfill, VACUUM and the job-retention sweep join perform-sync and
order-sync in the SAME mechanism -- attention_service.SCHEDULED_JOB_CHECKS
and its states -- rather than getting an alarm system of their own.

★ THE HARD PART IS NOT STALENESS, IT IS "NOTHING HAPPENED".

  * the colour backfill usually fills NOTHING, because there is usually
    nothing left to fill. "No rows changed" is its healthy case, so no
    amount of looking at local rows can tell a healthy quiet run from a
    cron that stopped firing;
  * VACUUM keeps no record at all -- it reclaims pages and returns a
    report over HTTP.

Both now stamp a success, filled or not, and these tests pin that the
alarm reads the stamp and nothing else.

★ AND "NO RECORD YET" IS NOT ONE ANSWER BUT THREE: no baseline at all,
within the first window after the baseline, and past it. The first two
are quiet; the third alarms. A job must never read as healthy forever
just because it has never run.

Every clock here is pinned, so "one missed tick" and "two missed ticks"
mean exactly what the schedules say they mean.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service as att
import scheduled_job_runs
from models import AppSetting, Base, Batch, PricingJob


NOW = datetime(2026, 10, 27, 12, 0, 0)


@pytest.fixture
def db(tmp_path):
    """Pricing healthy so its own separate collector stays out of these."""
    import json
    engine = create_engine(f"sqlite:///{tmp_path / 'cronalarm.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        session.add(PricingJob(
            action="bulk_market_price_apply", status="completed",
            request_json="{}",
            response_json=json.dumps(
                {"summary": {"successful_items": 5000, "total_items": 5000}}),
            created_at=NOW - timedelta(hours=1),
        ))
        session.commit()
    return engine


def stamp(session, job, *, hours_ago):
    scheduled_job_runs.record_success(session, job, when=NOW - timedelta(hours=hours_ago))
    session.flush()


def baseline(session, *, hours_ago):
    scheduled_job_runs.ensure_watch_since(session, when=NOW - timedelta(hours=hours_ago))
    session.flush()


def retention_sweep(session, *, hours_ago):
    """The sweep's OWN record -- job-retention gets no second stamp."""
    import json
    import job_retention_service as jr
    session.add(AppSetting(
        key=jr.LAST_SWEEP_SETTING_KEY,
        value=json.dumps({"ran_at": (NOW - timedelta(hours=hours_ago)).isoformat()}),
    ))
    session.flush()


# job, check, its thresholds, and how a success is recorded
CRONS = [
    ("color-backfill", att.color_backfill_health,
     att.COLOR_BACKFILL_STALE_WARN_HOURS, att.COLOR_BACKFILL_STALE_ALARM_HOURS,
     lambda s, h: stamp(s, scheduled_job_runs.COLOR_BACKFILL, hours_ago=h)),
    ("vacuum", att.vacuum_health,
     att.VACUUM_STALE_WARN_HOURS, att.VACUUM_STALE_ALARM_HOURS,
     lambda s, h: stamp(s, scheduled_job_runs.VACUUM, hours_ago=h)),
    ("job-retention", att.job_retention_health,
     att.JOB_RETENTION_STALE_WARN_HOURS, att.JOB_RETENTION_STALE_ALARM_HOURS,
     lambda s, h: retention_sweep(s, hours_ago=h)),
]
IDS = [c[0] for c in CRONS]


# ---------------------------------------------------------------------
# fresh / one missed / two missed, per cron, on a pinned clock
# ---------------------------------------------------------------------

@pytest.mark.parametrize("job,check,warn,alarm,record", CRONS, ids=IDS)
def test_a_recent_successful_run_is_fresh(db, job, check, warn, alarm, record):
    with Session(db) as session:
        baseline(session, hours_ago=1000)
        record(session, 0.5)
        status = check(session, now=NOW)
    assert status["state"] == "fresh", status
    assert status["job"] == job


@pytest.mark.parametrize("job,check,warn,alarm,record", CRONS, ids=IDS)
def test_one_missed_tick_does_not_alarm(db, job, check, warn, alarm, record):
    """★ A deploy landing mid-tick kills one, and that is expected."""
    with Session(db) as session:
        baseline(session, hours_ago=1000)
        record(session, warn - 0.5)
        status = check(session, now=NOW)
    assert status["state"] == "fresh", status


@pytest.mark.parametrize("job,check,warn,alarm,record", CRONS, ids=IDS)
def test_the_warn_threshold_warns_but_does_not_alarm(db, job, check, warn, alarm, record):
    with Session(db) as session:
        baseline(session, hours_ago=1000)
        record(session, warn)
        status = check(session, now=NOW)
    assert status["state"] == "warn", status


@pytest.mark.parametrize("job,check,warn,alarm,record", CRONS, ids=IDS)
def test_two_missed_ticks_alarm(db, job, check, warn, alarm, record):
    with Session(db) as session:
        baseline(session, hours_ago=1000)
        record(session, alarm)
        status = check(session, now=NOW)
    assert status["state"] == "alarm", status
    assert status["reason"], "an alarm must say why"


# ---------------------------------------------------------------------
# never run yet
# ---------------------------------------------------------------------

@pytest.mark.parametrize("job,check,warn,alarm,record", CRONS, ids=IDS)
def test_no_record_and_no_baseline_is_quiet_and_says_so(db, job, check, warn, alarm, record):
    with Session(db) as session:
        status = check(session, now=NOW)
    assert status["state"] == "unknown", status
    assert status["state"] in att.SCHEDULED_JOB_QUIET_STATES
    assert "no recorded run" in status["reason"]


@pytest.mark.parametrize("job,check,warn,alarm,record", CRONS, ids=IDS)
def test_no_record_just_after_deploy_reads_as_not_run_yet(db, job, check, warn, alarm, record):
    """★ Day one. Every job is in this state the moment this ships, and a
    page full of alarms for jobs that have not come round yet is noise."""
    with Session(db) as session:
        baseline(session, hours_ago=alarm - 1)
        status = check(session, now=NOW)
    assert status["state"] == "unknown", status
    assert "has not run yet" in status["reason"]


@pytest.mark.parametrize("job,check,warn,alarm,record", CRONS, ids=IDS)
def test_no_record_for_longer_than_its_window_alarms(db, job, check, warn, alarm, record):
    """★ And it is never healthy forever: once the job has had its
    chance and left nothing, that is the alarm."""
    with Session(db) as session:
        baseline(session, hours_ago=alarm + 1)
        status = check(session, now=NOW)
    assert status["state"] == "alarm", status
    assert "not run once" in status["reason"]


# ---------------------------------------------------------------------
# The things only a stamp can tell apart
# ---------------------------------------------------------------------

def test_a_colour_backfill_that_filled_nothing_is_still_a_healthy_run(db):
    """★ THE WHOLE POINT OF STAMPING. Filling nothing is the normal
    outcome; it must not look like a cron that stopped."""
    with Session(db) as session:
        baseline(session, hours_ago=1000)
        stamp(session, scheduled_job_runs.COLOR_BACKFILL, hours_ago=0.25)
        status = att.color_backfill_health(session, now=NOW)
    assert status["state"] == "fresh"


def test_an_unreadable_stamp_is_treated_as_no_record_not_as_recent(db):
    with Session(db) as session:
        session.add(AppSetting(
            key=f"{scheduled_job_runs.LAST_SUCCESS_KEY_PREFIX}vacuum",
            value="not a timestamp",
        ))
        baseline(session, hours_ago=1000)
        status = att.vacuum_health(session, now=NOW)
    assert status["state"] == "alarm", status


def test_the_baseline_is_stamped_once_and_never_moved(db):
    """A job dead for a week must not be forgiven by the next deploy."""
    with Session(db) as session:
        first = scheduled_job_runs.ensure_watch_since(session, when=NOW - timedelta(hours=500))
        again = scheduled_job_runs.ensure_watch_since(session, when=NOW)
        session.commit()
    assert first == again == NOW - timedelta(hours=500)


def test_recording_a_success_never_raises(db):
    """It reports on the work; it must not be able to undo work that
    already succeeded."""
    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("database gone")
    assert scheduled_job_runs.record_success(Broken(), "vacuum") is False


# ---------------------------------------------------------------------
# It is the same mechanism, not a parallel one
# ---------------------------------------------------------------------

def test_all_five_crons_go_through_the_one_checks_tuple():
    names = [c.__name__ for c in att.SCHEDULED_JOB_CHECKS]
    assert names == [
        "perform_sync_health", "order_sync_health",
        "color_backfill_health", "vacuum_health", "job_retention_health",
    ]


def test_an_alarming_cron_becomes_an_attention_item(db):
    with Session(db) as session:
        baseline(session, hours_ago=1000)
        stamp(session, scheduled_job_runs.VACUUM,
              hours_ago=att.VACUUM_STALE_ALARM_HOURS + 1)
        stamp(session, scheduled_job_runs.COLOR_BACKFILL, hours_ago=0.1)
        retention_sweep(session, hours_ago=1)
        session.commit()
        items = [i for i in att.collect(session, drift_rows=[], now=NOW)
                 if i.category == att.CATEGORY_SCHEDULED_JOB]
        keys = {i.item_key for i in items}
    assert "scheduled_job:vacuum" in keys
    assert "scheduled_job:color-backfill" not in keys, "a healthy cron adds no noise"


def test_healthy_crons_add_nothing_to_the_badge(db):
    """Keeping the badge honest: three more checks must not mean three
    more items when everything is fine."""
    with Session(db) as session:
        baseline(session, hours_ago=1000)
        stamp(session, scheduled_job_runs.VACUUM, hours_ago=1)
        stamp(session, scheduled_job_runs.COLOR_BACKFILL, hours_ago=0.1)
        retention_sweep(session, hours_ago=1)
        session.commit()
        items = [i for i in att.collect(session, drift_rows=[], now=NOW)
                 if i.category == att.CATEGORY_SCHEDULED_JOB]
    assert [i.item_key for i in items if "color-backfill" in i.item_key
            or "vacuum" in i.item_key or "job-retention" in i.item_key] == []
