"""The third gate: has this task already been done?

★ WHY THIS EXISTS, AND WHAT IT COST TO FIND OUT. On 2026-11-01 AutoDev's
first real task was built, verified and released as v2.80.1. Afterwards
the queue file was still there, the approval note was still valid, and
`serve.next_runnable` -- which asks only "is the work eligible" and "did
the operator approve it" -- still named it as the next task to run. A
fresh serve process would have cut a worktree and asked the developer to
redo work that was already live. Nothing was at risk (serve was not
running, and the guard blocks push, tag and railway), but it would have
spent a slot from the day's cap on a second, confusing branch -- and
against a ticket whose work had since been edited by hand it would be
worse than confusing. The in-session `finished` set that stopped a second
run DURING the cycle lives in memory and dies with the process.

★ THE AUDIT LOG IS THE SOURCE, NOT A NEW FILE. The operator chose this
shape (2026-11-01 09:04 ET): a task with a `done` outcome in
audit.jsonl is refused. The append-only log already records every ending
and is the record a human would check, so reading it back makes "already
done" a GATE rather than a convention someone has to remember at release
time. A marker file written by the release step would be a second source
of truth, and a human step that is skipped silently is how the first
failure happens.

★ RE-APPROVAL IS THE DELIBERATE WAY BACK. An approval note dated AFTER
the done outcome runs the task again. That is not a loophole: it is the
operator saying, in the one place that carries their decision, that they
want it built a second time. A note dated before -- which is every note
that authorised the first run -- does not.

★ FAILS CLOSED. An outcome whose time cannot be read, or an approval
whose `approved_at` cannot be parsed, REFUSES. Same stance as
approvals.load with a missing note and policy._forbidden_reason with an
unrecognised path: "no opinion" has to mean "no" in a loop that runs
unattended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from . import state

logger = logging.getLogger("cardfoundry")

# The audit event that records how a task ended, and the field in it that
# says whether it passed. Named here rather than inline so a reader can
# see what this gate reads and compare it with what coordinator.run_task
# writes -- the two must not drift.
OUTCOME_EVENT = "task_outcome"
PASSED_FIELD = "passed"


@dataclass
class Completion:
    """The newest `done` outcome for one task, if there is one."""

    task: str
    row: dict | None = None

    @property
    def is_done(self) -> bool:
        return self.row is not None

    @property
    def when(self) -> str:
        """A readable time for the refusal message."""
        if not self.row:
            return ""
        at = str(self.row.get("at") or "").strip()
        if at:
            return at
        moment = self.moment
        return (datetime.fromtimestamp(moment).isoformat() if moment
                else "an unrecorded time")

    @property
    def moment(self) -> float | None:
        """When it was done, as an epoch, or None if unreadable.

        `epoch` first because state.audit writes it from time.time() and
        it needs no parsing; `at` is the fallback for a row written
        before that field existed. Naive ISO text is local, which is what
        state.audit wrote, so fromisoformat().timestamp() reads it back
        in the same frame.
        """
        if not self.row:
            return None
        epoch = self.row.get("epoch")
        if isinstance(epoch, (int, float)):
            return float(epoch)
        at = str(self.row.get("at") or "").strip()
        if not at:
            return None
        try:
            return datetime.fromisoformat(at).timestamp()
        except ValueError as exc:
            logger.warning("autodev completions: %s has an unreadable "
                           "outcome time %r: %s", self.task, at, exc)
            return None

    def as_evidence(self) -> dict:
        """What status and the audit record about this completion."""
        return {
            "task": self.task,
            "done": self.is_done,
            "done_at": self.when or None,
            "attempt": (self.row or {}).get("attempt"),
            "branch": (self.row or {}).get("branch"),
            "commit": (self.row or {}).get("head"),
        }


def newest_done(rows: list[dict] | None = None) -> dict[str, dict]:
    """The newest passing outcome row per task, from one pass of the log.

    One pass and a dict, because next_runnable asks about every queued
    ticket on every cycle and the audit log only grows. The caller reads
    the log once and hands the rows down.
    """
    if rows is None:
        rows = state.read_audit()
    index: dict[str, dict] = {}
    for row in rows:
        if row.get("event") != OUTCOME_EVENT:
            continue
        if row.get(PASSED_FIELD) is not True:
            # A parked or rejected attempt is NOT done, and must stay
            # runnable: condition-label-round-trip-2 has a failed outcome
            # and holds work nobody has decided about yet.
            continue
        task = str(row.get("task") or "")
        if task:
            index[task] = row
    return index


def load(task_id: str, rows: list[dict] | None = None,
         index: dict[str, dict] | None = None) -> Completion:
    if index is None:
        index = newest_done(rows)
    return Completion(task=task_id, row=index.get(task_id))


def refusals(task_id: str, approved_at: str = "", *,
             rows: list[dict] | None = None,
             index: dict[str, dict] | None = None) -> list[str]:
    """Why this task may not run again, in the operator's words.

    `approved_at` is the approval note's own timestamp, passed in rather
    than read here so this module stays independent of approvals.py --
    serve already holds both and the dependency only needs to go one way.
    """
    completion = load(task_id, rows=rows, index=index)
    if not completion.is_done:
        return []

    done_at = completion.moment
    if done_at is None:
        return [
            f"already done at {completion.when}, and the time of that "
            "outcome cannot be read, so this fails closed; re-approve to "
            "run again"
        ]

    stamp = str(approved_at or "").strip()
    if stamp:
        try:
            if datetime.fromisoformat(stamp).timestamp() > done_at:
                # The operator re-approved it AFTER it was done. That is
                # the deliberate way back, so this gate steps aside.
                return []
        except ValueError:
            # approvals.evaluate refuses this note anyway; saying so here
            # too costs one line and keeps this gate readable on its own.
            return [
                f"already done at {completion.when}, and the approval's "
                f"approved_at {stamp!r} is not a readable timestamp, so it "
                "cannot be newer; re-approve to run again"
            ]

    return [
        f"already done at {completion.when}; re-approve to run again "
        f"(the approval note is dated "
        f"{stamp or 'nothing this gate could read'}, which is not newer)"
    ]
