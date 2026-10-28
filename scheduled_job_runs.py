"""When each scheduled job last SUCCEEDED, as a durable record.

Some crons leave a natural success signal behind -- perform-sync writes
an InventorySyncJob row, pricing writes a PricingJob, the retention sweep
writes its own AppSetting summary. Two do not:

  * VACUUM reclaims pages and returns a report over HTTP, keeping nothing;
  * the colour backfill usually fills NOTHING, because there is usually
    nothing to fill -- so "no rows changed" is its healthy case and
    cannot be read as evidence it ran.

Without a record, "has not run in three days" and "ran fine and had
nothing to do" look identical, and an alarm built on local rows cannot
tell them apart. This writes the one fact that distinguishes them: the
job finished successfully at this moment.

★ RECORDING MUST NEVER FAIL THE JOB. The record exists to report on the
work, so a failure to write it must not undo work that actually
succeeded. Every write is wrapped and logged through the `cardfoundry`
logger instead of raising -- a missing record then shows up as the alarm
it should be, rather than as a 500 from a job that did its work.

★ AND A MISSING RECORD IS NOT "HEALTHY". The first time a job runs after
this ships there is no record yet, which must read as "not run yet"
rather than as a false alarm on the day of deploy or as healthy forever.
`watch_since` is the baseline that makes that answerable: it is stamped
once, at startup, and a job with no record is quiet only until its own
alarm threshold has elapsed SINCE THAT STAMP. After that it alarms, on
the truthful grounds that monitoring has been watching for longer than
the job's own interval and has seen nothing.
"""

import logging
from datetime import datetime

from models import AppSetting

logger = logging.getLogger("cardfoundry")

LAST_SUCCESS_KEY_PREFIX = "scheduled_job_last_success:"
WATCH_SINCE_KEY = "scheduled_job_watch_since"

# The job names are the ones main.py already derives from a cron's route
# for audit attribution (_SERVICE_JOB_BY_EXACT_PATH), so a job is spelled
# the same way in an audit row and in its alarm.
COLOR_BACKFILL = "color-backfill"
VACUUM = "vacuum"


def _key(job: str) -> str:
    return f"{LAST_SUCCESS_KEY_PREFIX}{job}"


def _upsert(session, key: str, when: datetime) -> None:
    setting = session.query(AppSetting).filter(AppSetting.key == key).first()
    if setting:
        setting.value = when.isoformat()
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=key, value=when.isoformat()))


def record_success(session, job: str, *, when: datetime | None = None) -> bool:
    """Stamp `job` as having succeeded now. True if it was written.

    Never raises: see the module docstring. The caller is a cron route
    that has already done its real work by this point.
    """
    try:
        _upsert(session, _key(job), when or datetime.now())
        return True
    except Exception as exc:  # noqa: BLE001 -- must not fail the job
        logger.warning(
            "scheduled job run: could not record a successful %s run, so its "
            "freshness alarm may fire incorrectly: %s: %s",
            job, type(exc).__name__, exc,
        )
        return False


def _read(session, key: str) -> datetime | None:
    setting = session.query(AppSetting).filter(AppSetting.key == key).first()
    if not setting or not setting.value:
        return None
    try:
        return datetime.fromisoformat(setting.value)
    except (TypeError, ValueError) as exc:
        # A malformed stamp is not evidence of a recent run. Treated as
        # absent, and said out loud, rather than parsed optimistically.
        logger.warning(
            "scheduled job run: %s holds an unreadable timestamp %r (%s); "
            "treating it as no record at all", key, setting.value, exc,
        )
        return None


def last_success(session, job: str) -> datetime | None:
    return _read(session, _key(job))


def watch_since(session) -> datetime | None:
    """When this monitoring started watching. None if never stamped."""
    return _read(session, WATCH_SINCE_KEY)


def ensure_watch_since(session, *, when: datetime | None = None) -> datetime | None:
    """Stamp the baseline once, and never move it afterwards.

    Called at startup. It must NOT be refreshed on later deploys, or a
    job that has been silently dead for a week would be forgiven every
    time the app restarted.
    """
    try:
        existing = watch_since(session)
        if existing is not None:
            return existing
        stamped = when or datetime.now()
        _upsert(session, WATCH_SINCE_KEY, stamped)
        return stamped
    except Exception as exc:  # noqa: BLE001 -- must not fail startup
        logger.warning(
            "scheduled job run: could not stamp the monitoring baseline; "
            "never-run alarms stay quiet until it exists: %s: %s",
            type(exc).__name__, exc,
        )
        return None


def load_records(session, *, extra_keys=()) -> dict:
    """Every stamp this module knows about, in ONE query.

    ★ THIS EXISTS FOR badge_count, WHICH RUNS ON EVERY PAGE LOAD. The
    freshness checks are read on each render, and reading each job's
    stamp separately would add a statement per cron to every page in the
    app -- the exact growth the N+1 statement-count tests pin. One
    `IN (...)` read serves all of them, and callers that also need a
    neighbouring key (the retention sweep's own record) pass it in rather
    than issuing a second query for it.
    """
    keys = [WATCH_SINCE_KEY, _key(COLOR_BACKFILL), _key(VACUUM), *extra_keys]
    rows = (
        session.query(AppSetting.key, AppSetting.value)
        .filter(AppSetting.key.in_(keys))
        .all()
    )
    return {key: value for key, value in rows}


def _parse(value, *, key: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "scheduled job run: %s holds an unreadable timestamp %r (%s); "
            "treating it as no record at all", key, value, exc,
        )
        return None


def last_success_from(records: dict, job: str):
    return _parse(records.get(_key(job)), key=_key(job))


def watch_since_from(records: dict):
    return _parse(records.get(WATCH_SINCE_KEY), key=WATCH_SINCE_KEY)
