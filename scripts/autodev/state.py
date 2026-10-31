"""Where AutoDev keeps its mind: the lock, the state file, the audit log.

★ FILES, NOT A DAEMON OR A MESSAGE BUS. Two desktop sessions cannot talk
to each other, and a process that dies takes its memory with it, so every
hand-off here goes through a file on disk. A file survives a crash, a
restart and a closed terminal; a session does not.

★ THE AUDIT LOG IS APPEND-ONLY AND IS THE RECOVERY LOG TOO. Every phase
writes a row BEFORE it starts and a row AFTER it ends, which is what makes
"what was running when the machine slept?" answerable: a `started` row
with no matching `finished` row IS the crash. Same shape as the app's own
append-only audit rows, for the same reason -- a swallowed event is a bug
waiting to be invisible.

★ STATE LIVES UNDER claude/, WHICH IS GITIGNORED. Machine state is not
source code and must never enter a release. The code lives in
scripts/autodev/ and is reviewable; everything mutable lives here.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("cardfoundry")

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_SCHEMA = 1

PAUSE_FILE = "PAUSE"
STOP_FILE = "STOP"


def home() -> Path:
    """The AutoDev home, overridable so tests never touch the real one."""
    override = os.environ.get("CARDFOUNDRY_AUTODEV_HOME")
    return Path(override) if override else REPO_ROOT / "claude" / "autodev"


def worktree_root() -> Path:
    """Where task worktrees live: OUTSIDE the repository, always.

    ★ MEASURED THE HARD WAY, 2026-10-31. The first version put worktrees
    under claude/autodev/worktrees -- inside the repo, gitignored, which
    seemed tidy. It broke the whole test suite: pytest run from the repo
    root collected every test file twice, once from tests/ and once from
    the worktree's copy, and aborted with 292 import-mismatch collection
    errors. `git status` stayed clean the entire time, so nothing pointed
    at the cause. A worktree is a second copy of the repo and must not sit
    inside the first one.
    """
    override = os.environ.get("CARDFOUNDRY_AUTODEV_WORKTREES")
    if override:
        return Path(override)
    return Path.home() / ".cardfoundry-autodev" / "worktrees"


def ensure_home() -> Path:
    root = home()
    for child in ("", "queue", "runs", "digest", "escalations", "grants"):
        (root / child).mkdir(parents=True, exist_ok=True)
    worktree_root().mkdir(parents=True, exist_ok=True)
    return root


class CoordinatorBusy(RuntimeError):
    pass


@contextmanager
def single_owner_lock():
    """One task owner, enforced by the kernel rather than by politeness.

    ★ flock, NOT A PID FILE WE PARSE. A pid file goes stale the moment a
    process is killed, and then the next run either wedges forever or
    ignores a lock that is real. An flock is released by the OS when the
    holder dies, so a killed coordinator leaves nothing to clean up. The
    pid/host/time written INSIDE the file are for a human reading it, not
    for the locking decision.
    """
    ensure_home()
    path = home() / "coordinator.lock"
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            existing = ""
            try:
                existing = os.pread(handle, 400, 0).decode().strip()
            except OSError:
                pass
            raise CoordinatorBusy(
                "Another AutoDev coordinator holds the lock"
                + (f" ({existing})" if existing else "")
            ) from exc
        os.ftruncate(handle, 0)
        os.pwrite(handle, json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": datetime.now().isoformat(),
        }).encode(), 0)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


@dataclass
class State:
    """The coordinator's whole mind, small enough to read at a glance."""

    data: dict

    @classmethod
    def load(cls) -> "State":
        ensure_home()
        path = home() / "state.json"
        if not path.exists():
            return cls({
                "schema": STATE_SCHEMA,
                "active_task": None,
                "phase": None,
                "attempt": 0,
                "phase_started_at": None,
                "day": None,
                "tasks_today": 0,
                "last_outcome": None,
                "halted_reason": None,
            })
        try:
            return cls(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            # Logs rather than guesses: a corrupt state file is a real
            # event and starting from a blank mind would silently lose a
            # task that is mid-flight.
            logger.warning("autodev state: unreadable state.json: %s", exc)
            raise

    def save(self) -> None:
        """Atomic: write beside, then rename. A half-written state file is
        worse than no state file, because recovery would believe it."""
        ensure_home()
        path = home() / "state.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.replace(tmp, path)

    def roll_day(self) -> None:
        today = date.today().isoformat()
        if self.data.get("day") != today:
            self.data["day"] = today
            self.data["tasks_today"] = 0

    @property
    def interrupted_phase(self) -> str | None:
        """A phase that started and never finished -- i.e. a crash."""
        if self.data.get("active_task") and self.data.get("phase_started_at"):
            return self.data.get("phase")
        return None


def audit(event: str, **fields) -> dict:
    """One append-only row. Never rewritten, never trimmed here."""
    ensure_home()
    row = {
        "at": datetime.now().isoformat(),
        "epoch": time.time(),
        "event": event,
        "pid": os.getpid(),
        **fields,
    }
    with (home() / "audit.jsonl").open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    logger.info("autodev audit: %s %s", event, fields.get("task") or "")
    return row


def read_audit(limit: int | None = None) -> list[dict]:
    path = home() / "audit.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            # A damaged line is reported, not skipped silently: an
            # unreadable audit row is exactly the thing an audit exists
            # to make visible.
            logger.warning("autodev audit: unreadable row: %s", exc)
            rows.append({"event": "unreadable_audit_row", "error": str(exc)})
    return rows[-limit:] if limit else rows


def paused() -> bool:
    return (home() / PAUSE_FILE).exists()


def stopped() -> bool:
    return (home() / STOP_FILE).exists()


def run_dir(task_id: str, attempt: int) -> Path:
    path = home() / "runs" / task_id / str(attempt)
    path.mkdir(parents=True, exist_ok=True)
    return path
