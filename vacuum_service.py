"""Reclaim the pages the retention sweep frees.

job_retention_service trims old job blobs in place, which frees pages
INSIDE the SQLite file and never returns them to the OS. Until this
existed that reclamation was a manual step in docs/DEVELOPMENT.md that
nothing scheduled, so the file only ever grew: measured 2026-09-22 at
914.2 MB with 109.9 MB (12.0%) sitting on the freelist, roughly two
nights of sweep output.

What actually fills that freelist is worth stating, because it is not
what you would guess: Perform Sync's `maintenance_preview` rows are
9.57 MB each and run three times a day (~28.7 MB/day), while a pricing
run under the bulk flow costs ~0.1 MB. See CHANGELOG 1.193.0.

VACUUM cannot run inside a transaction and takes an EXCLUSIVE lock on
the whole database for its duration, so this is deliberately built to
fail rather than wait: a short busy timeout, no retry, and a loud log
line. Blocking would be worse than skipping -- the next run is a day
away, the freelist is not urgent, and holding a connection open waiting
for an exclusive lock is exactly how a cron tick lands on top of the
04:05 order sync.
"""

import logging
import os
import sqlite3
import time
from pathlib import Path

# Same shared logger as the rest of the app (v1.155.0).
logger = logging.getLogger("cardfoundry")

# Short on purpose. If the database is busy right now, something else is
# mid-write and this should stand down, not queue behind it.
BUSY_TIMEOUT_SECONDS = 5.0


class VacuumError(RuntimeError):
    """VACUUM could not run. Always logged before it is raised."""


def database_path(database_url: str) -> Path:
    """The on-disk file behind a SQLAlchemy SQLite URL."""
    url = str(database_url or "")
    if not url.startswith("sqlite"):
        raise VacuumError(f"Refusing to VACUUM a non-SQLite database: {url[:40]!r}")
    _, _, tail = url.partition("///")
    tail = tail.split("?", 1)[0]
    if tail.startswith("file:"):
        tail = tail[len("file:"):]
    if not tail:
        raise VacuumError(f"Could not read a database path out of {url[:60]!r}")
    return Path(tail)


def _sizes(path: Path, *, busy_timeout: float = BUSY_TIMEOUT_SECONDS) -> dict:
    """Page-level sizes, read-only.

    Takes the same short busy timeout as the VACUUM itself: an EXCLUSIVE
    lock blocks even `PRAGMA page_count`, so without this the size read
    that happens BEFORE the vacuum would raise a raw OperationalError and
    bypass every bit of the fail-safe handling below. Found by
    test_a_held_lock_fails_fast_and_loudly_without_retrying.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                 timeout=busy_timeout)
    try:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        connection.close()
    return {
        "bytes": page_count * page_size,
        "freelist_bytes": freelist * page_size,
        "freelist_fraction": (freelist / page_count) if page_count else 0.0,
    }


def run_vacuum(database_url: str, *, busy_timeout: float = BUSY_TIMEOUT_SECONDS) -> dict:
    """VACUUM the database, or fail loudly having changed nothing.

    Returns a report of before/after sizes. Raises VacuumError -- after
    logging it at ERROR -- if the file is missing, the lock is held, or
    SQLite refuses for any other reason.
    """
    path = database_path(database_url)
    if not path.exists():
        message = f"database file does not exist: {path}"
        logger.error("vacuum: refusing to run -- %s", message)
        raise VacuumError(message)

    try:
        before = _sizes(path, busy_timeout=busy_timeout)
    except sqlite3.OperationalError as exc:
        # The database is busy before we have even started. Same outcome
        # as a busy VACUUM: stand down, loudly, changing nothing.
        logger.error(
            "vacuum: FAILED before starting, database left untouched -- "
            "could not read page counts: %s: %s (busy_timeout=%.1fs; "
            "the next scheduled run will retry)",
            type(exc).__name__, exc, busy_timeout,
        )
        raise VacuumError(str(exc)) from exc
    free_needed = before["bytes"]
    logger.info(
        "vacuum: starting on %s (%.1f MB, freelist %.1f MB / %.1f%%); "
        "needs ~%.1f MB of free space to rewrite",
        path, before["bytes"] / 1e6, before["freelist_bytes"] / 1e6,
        before["freelist_fraction"] * 100, free_needed / 1e6,
    )

    started = time.monotonic()
    # isolation_level=None: VACUUM cannot run inside a transaction, and
    # Python's sqlite3 opens one implicitly for anything else.
    connection = sqlite3.connect(str(path), isolation_level=None,
                                 timeout=busy_timeout)
    try:
        connection.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        elapsed = time.monotonic() - started
        # The expected failure: someone else holds the lock. Not a crash,
        # not a retry -- the next scheduled run picks it up tomorrow.
        logger.error(
            "vacuum: FAILED after %.1fs, database left untouched -- %s: %s "
            "(busy_timeout=%.1fs; the next scheduled run will retry)",
            elapsed, type(exc).__name__, exc, busy_timeout,
        )
        raise VacuumError(str(exc)) from exc
    finally:
        connection.close()

    elapsed = time.monotonic() - started
    after = _sizes(path, busy_timeout=busy_timeout)
    reclaimed = before["bytes"] - after["bytes"]
    logger.info(
        "vacuum: completed in %.1fs -- %.1f MB -> %.1f MB, reclaimed %.1f MB; "
        "freelist %.1f MB -> %.1f MB",
        elapsed, before["bytes"] / 1e6, after["bytes"] / 1e6, reclaimed / 1e6,
        before["freelist_bytes"] / 1e6, after["freelist_bytes"] / 1e6,
    )
    return {
        "path": str(path),
        "seconds": round(elapsed, 2),
        "bytes_before": before["bytes"],
        "bytes_after": after["bytes"],
        "bytes_reclaimed": reclaimed,
        "freelist_bytes_before": before["freelist_bytes"],
        "freelist_bytes_after": after["freelist_bytes"],
    }
