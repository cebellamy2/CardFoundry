"""The serve loop: approved tickets, one at a time, until told to stop.

★ WHAT THIS ADDS, AND WHAT IT DOES NOT. coordinator.run_task already
takes ONE task through manager -> developer -> gates -> reviewer and
stops. This module is the thing that decides which task is next, sends a
rejected one back to the developer once, counts the day's work against
the cap, and writes an entry to the outbox for every ending. It adds no
new power: every safety control still lives where it did (scrubbed
environment, deny rules, guard hook, worktree isolation, path denylist,
independent reviewer, retry cap), and this loop cannot push, tag, deploy
or touch railway because guard.py blocks all of it.

★ IT RUNS ONLY APPROVED TASKS. approvals.py is the gate: the operator's
"AutoDev-ready" OK, written down by Cowork, per task -- or, since
2026-11-04, Cowork's own note under the operator's STANDING LOW-RISK
RULE, which the note must name as its `approval_basis` and back with a
`low_risk_assessment`. Either way the authority is recorded in the note
and carried into the outbox entry, so a reader can tell which one a run
had. Every ticket already in the queue predates all of this and none of
them is approved, so this loop will not pick up any of them. A refusal is
AUDITED and written to the outbox rather than skipped quietly -- an
unapproved ticket sitting in the queue being silently ignored is the kind
of absence that CLAUDE.md warns about.

★ AND IT RUNS NOTHING TWICE. A task with a `done` outcome in the audit
log is refused unless the operator has re-approved it since -- the gate
in completions.py, added after the first real task shipped and stayed
runnable. Three gates now, each its own module: policy.py asks whether
the work is eligible, approvals.py whether a human agreed to it, and
completions.py whether it has already been done.

★ ITS OWN DAILY COUNTER, AND WHY. state's `tasks_today` counts tasks that
PASSED; coordinator only increments it on success. For a single run that
an operator is watching, that is the right number. For an unattended
loop it is the wrong cap: a day of tasks that all park would never reach
it, and the loop would keep spending a subscription on failures. So this
module counts tasks it STARTS, under its own key, and caps that at
max_tasks_per_day. A rework does NOT consume a second slot -- the
approval covers the retry, and the cap counts tasks, not attempts.
coordinator's own counter is left exactly as it was.

★ SOMETHING SCHEDULES THIS NOW, AND IT IS NOT THIS MODULE. A LaunchAgent
runs scripts/autodev/autoserve.py every five minutes (operator,
2026-11-01: "sets it to start running automatically"), and THAT is where
the decision to start lives: it reads `scheduling_enabled`, honours STOP
and PAUSE, checks read-only whether anything is runnable, and only then
calls into here for one bounded cycle. This module still never reads
`scheduling_enabled` -- so `python -m scripts.autodev.serve` run by hand
behaves the same either way, and the flag means exactly one thing: may
the machine start this on its own. There is a test for that.

★ AND IT STILL CANNOT SHIP ANYTHING. Every task lands on its own
autodev/* branch. serve cannot push, tag, deploy or touch railway, and
merging to main is a Code CLI release that happens only after the
operator says so. That is the whole reason the daily cap could go: the
cost of a bad day is compute, not trust.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import (agents, approvals, completions, coordinator, outbox,
               policy, state)

logger = logging.getLogger("cardfoundry")

# How long to wait when there is nothing approved to do. Short enough
# that an approval written by Cowork is picked up promptly, long enough
# that the loop is not spinning on the filesystem.
DEFAULT_IDLE_SECONDS = 60

# serve's own counter, kept beside coordinator's in the same state file.
# Additive: coordinator never reads these keys and its behaviour is
# unchanged.
SERVE_DAY_KEY = "serve_day"
SERVE_STARTED_KEY = "serve_tasks_started_today"


@dataclass
class ServeReport:
    """What one serve session did. Returned for tests and printed at exit."""

    cycles: int = 0
    started: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)
    refused: dict[str, list[str]] = field(default_factory=dict)
    reworked: list[str] = field(default_factory=list)
    halted: str | None = None
    stopped: bool = False
    capped: bool = False

    def as_dict(self) -> dict:
        return {
            "cycles": self.cycles, "started": self.started,
            "done": self.done, "parked": self.parked,
            "refused": self.refused, "reworked": self.reworked,
            "halted": self.halted, "stopped": self.stopped,
            "capped": self.capped,
        }


def _roll_serve_day(st: state.State) -> None:
    today = date.today().isoformat()
    if st.data.get(SERVE_DAY_KEY) != today:
        st.data[SERVE_DAY_KEY] = today
        st.data[SERVE_STARTED_KEY] = 0


def seconds_until_next_local_day(now: datetime | None = None) -> float:
    """Until just after local midnight.

    LOCAL, not UTC, and that is the operator's decision: the cap is a
    day's work as they experience a day. (Railway crons are UTC; this is
    not a cron and has nothing to do with them.) The extra minute is so a
    clock a second fast cannot wake into the same day it just left.
    """
    now = now or datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (tomorrow - now).total_seconds() + 60)


def queued_task_ids() -> list[str]:
    """Deterministic order. Sorted, so two runs pick the same next task
    and a reader of the audit log can follow why."""
    root = state.home() / "queue"
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.json"))


def assess(task_id: str) -> tuple[policy.Ticket, approvals.Approval]:
    """Both gates for one task: is the work eligible, and was it approved."""
    ticket = policy.load(state.home() / "queue" / f"{task_id}.json")
    digest = coordinator.ticket_digest(ticket) if ticket.data else None
    approval = approvals.load(task_id, ticket_digest=digest)
    return ticket, approval


def next_runnable(seen: set[str]) -> tuple[str | None, dict[str, list[str]]]:
    """The next task this loop may run, plus why each other one was not.

    Returns the refusals so the caller can report them. Nothing is
    skipped silently: a queue of tickets that all fail the gates must
    look different from an empty queue -- and that includes a task that
    is refused for having already been done, which is why the completion
    gate reports a reason rather than quietly filtering the queue.
    """
    refusals: dict[str, list[str]] = {}
    done_index = completions.newest_done()
    for task_id in queued_task_ids():
        if task_id in seen:
            continue
        ticket, approval = assess(task_id)
        reasons = (
            list(approval.refusals) + list(ticket.refusals)
            # Read from the one audit pass above: the log only grows, and
            # this question is asked of every queued ticket every cycle.
            + completions.refusals(task_id, approval.approved_at,
                                   index=done_index)
        )
        if reasons:
            refusals[task_id] = reasons
            continue
        return task_id, refusals
    return None, refusals


def already_reported(task_id: str, reasons: list[str],
                     rows: list[dict] | None = None) -> bool:
    """Has this exact refusal, for this task, already been recorded?

    ★ WHY THIS EXISTS AT ALL -- the auto-start changed the arithmetic.
    serve used to dedupe refusals in memory (`reported_refusals`), which
    was enough when one long-lived process did the serving. Under the
    launchd agent a FRESH process runs every few minutes, so that set is
    empty every time: five unapproved tickets would have written five
    outbox entries and five audit rows per interval -- on the order of a
    thousand files a day, all saying what the first one said. The dedupe
    therefore has to live where it survives a process, which is the
    append-only audit log.

    Deduped on the REASONS too, not just the task: a ticket refused for
    a new reason is news and gets reported again.
    """
    if rows is None:
        rows = state.read_audit()
    wanted = list(reasons)
    for row in reversed(rows):
        if row.get("event") != "serve_task_refused":
            continue
        if row.get("task") != task_id:
            continue
        return list(row.get("reasons") or []) == wanted
    return False


def report_refusals(refusals: dict[str, list[str]],
                    reported: set[str] | None = None) -> list[str]:
    """Record each refusal once -- audit row, outbox entry, stdout line.

    ★ ONE PATH, TWO CALLERS. The loop below reports the refusals it sees
    on a cycle that runs something; autoserve reports the ones it sees on
    a firing where NOTHING is runnable, which under a five-minute agent
    is most of them. Written twice, one of the two would have drifted
    into reporting less -- and a ticket refused in silence is exactly the
    absence CLAUDE.md says is worse than an uncategorised one.

    Returns the ids actually reported.
    """
    rows = state.read_audit()
    said = []
    for refused_id, reasons in sorted(refusals.items()):
        if reported is not None:
            if refused_id in reported:
                continue
            reported.add(refused_id)
        if already_reported(refused_id, reasons, rows):
            continue
        state.audit("serve_task_refused", task=refused_id, reasons=reasons)
        outbox.write(refused_id, "refused", detail="; ".join(reasons))
        print(f"refused {refused_id}:")
        for reason in reasons:
            print(f"  - {reason}")
        said.append(refused_id)
    return said


def _last_outcome_row(task_id: str) -> dict:
    """The most recent task_outcome row for this task, from the audit log.

    Read back rather than returned by run_task on purpose: the audit log
    is the append-only record of what happened, and an outbox entry built
    from anywhere else could disagree with it.
    """
    for row in reversed(state.read_audit()):
        if row.get("event") == "task_outcome" and row.get("task") == task_id:
            return row
    return {}


def _diffstat(task_id: str, attempt: int) -> str | None:
    """The diff stat for this attempt, from the evidence the run wrote."""
    patch = state.home() / "runs" / task_id / str(attempt) / "diff.patch"
    if not patch.exists():
        return None
    try:
        text = patch.read_text()
    except OSError as exc:
        logger.warning("autodev serve: cannot read %s: %s", patch, exc)
        return None
    files = [line for line in text.splitlines() if line.startswith("+++ b/")]
    added = sum(1 for line in text.splitlines()
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in text.splitlines()
                  if line.startswith("-") and not line.startswith("---"))
    return (f"{len(files)} file(s), +{added}/-{removed} "
            f"({', '.join(name[6:] for name in files) or 'none named'})")


def _gates_summary(task_id: str, attempt: int) -> dict | None:
    path = state.home() / "runs" / task_id / str(attempt) / "gates.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("autodev serve: cannot read %s: %s", path, exc)
        return {"unreadable": str(exc)}


def _entry_for(task_id: str, outcome: str, approval: approvals.Approval,
               detail: str | None = None) -> Path:
    """One outbox entry, built from the audit row and the run evidence."""
    row = _last_outcome_row(task_id)
    attempt = int(row.get("attempt") or 0)
    return outbox.write(
        task_id, outcome,
        branch=row.get("branch"), head=row.get("head"),
        base=coordinator.config()["base_branch"],
        attempt=attempt or None,
        gates=_gates_summary(task_id, attempt) if attempt else None,
        review=row.get("review"),
        diffstat=_diffstat(task_id, attempt) if attempt else None,
        approval=approval.as_evidence() if approval.is_approved else None,
        evidence_dir=str(state.home() / "runs" / task_id / str(attempt))
        if attempt else None,
        detail=detail,
    )


def _reviewer_rejected(st: state.State, task_id: str) -> bool:
    """Was the last ending a reviewer REJECT on this task, and nothing else?

    The operator's answer to Q2 names the reviewer's reject specifically.
    A failed GATE is a different ending and is NOT auto-reworked here --
    a gate failure means the change did not build, test or stay in scope,
    which is a question about the ticket rather than about the review.
    """
    if st.data.get("active_task") != task_id:
        return False
    return "review=reject" in str(st.data.get("last_outcome") or "")


def run_one(task_id: str, approval: approvals.Approval, report: ServeReport,
            *, stub_ok: bool = False) -> int:
    """One task, including its one automatic rework. Returns run_task's code.

    The lock is already held by the caller for the whole session, so this
    calls run_task directly rather than through coordinator.cmd_run.
    """
    cfg = coordinator.config()
    state.audit("serve_task_started", task=task_id,
                approval=approval.as_evidence())
    report.started.append(task_id)

    code = coordinator.run_task(task_id, stub_ok=stub_ok)
    if code == 0:
        report.done.append(task_id)
        _entry_for(task_id, "done", approval)
        return code

    st = state.State.load()
    attempt = int(st.data.get("attempt") or 0)
    if code == 1 and _reviewer_rejected(st, task_id) and attempt < cfg["max_attempts"]:
        # ★ NO SECOND SIGN-OFF. The operator settled that one approval
        # covers the task including this retry, so the loop goes straight
        # back to the developer. run_task itself increments the attempt
        # and audits `rework_started`; calling it again IS the rework.
        state.audit("serve_rework", task=task_id, after=st.data.get("last_outcome"),
                    attempt_before=attempt, max_attempts=cfg["max_attempts"])
        report.reworked.append(task_id)
        code = coordinator.run_task(task_id, stub_ok=stub_ok)
        if code == 0:
            report.done.append(task_id)
            _entry_for(task_id, "done", approval,
                       detail="passed on the automatic rework")
            return code

    report.parked.append(task_id)
    _entry_for(task_id, "parked", approval,
               detail=f"run_task exit {code}; "
                      f"last outcome {state.State.load().data.get('last_outcome')}")
    return code


def serve(*, max_cycles: int | None = None,
          idle_seconds: float = DEFAULT_IDLE_SECONDS,
          sleeper=time.sleep, stub_ok: bool = False) -> tuple[int, ServeReport]:
    """The loop. Returns (exit code, report).

    max_cycles bounds it for tests; a real run leaves it None and the loop
    ends only on STOP, Ctrl-C or a halt.
    """
    report = ServeReport()
    reported_refusals: set[str] = set()
    finished: set[str] = set()

    if not stub_ok:
        ok, detail = agents.preflight_auth()
        state.audit("serve_preflight_auth", ok=ok, detail=detail)
        if not ok:
            coordinator.escalate("serve", "BLOCKER: the CLI cannot authenticate",
                                 detail)
            report.halted = detail
            return 3, report
        turn_failures = agents.max_turns_preflight()
        state.audit("serve_preflight_max_turns", failures=turn_failures)
        if turn_failures:
            coordinator.escalate(
                "serve", "BLOCKER: a role would run without a turn limit",
                "\n".join(f"- {item}" for item in turn_failures))
            report.halted = "; ".join(turn_failures)
            return 3, report

    with state.single_owner_lock():
        state.audit("serve_started", max_cycles=max_cycles)
        while max_cycles is None or report.cycles < max_cycles:
            report.cycles += 1

            if state.stopped():
                state.audit("serve_stopped_by_sentinel")
                report.stopped = True
                print("STOP is set; serve is exiting. "
                      "`coordinator.py resume --clear-stop` to clear it.")
                return 2, report
            if state.paused():
                print("PAUSE is set; serve is waiting.")
                sleeper(idle_seconds)
                continue

            st = state.State.load()
            _roll_serve_day(st)
            started_today = int(st.data.get(SERVE_STARTED_KEY) or 0)
            # None means no cap (operator, 2026-11-01). The counter is
            # still kept and still audited, so "how many did it start
            # today" stays answerable; only the refusal is gone.
            cap = coordinator.config().get("max_tasks_per_day")
            if cap is not None and started_today >= cap:
                report.capped = True
                state.audit("serve_daily_cap", started_today=started_today, cap=cap)
                print(f"daily cap reached: {started_today} of {cap} tasks "
                      f"started today; waiting for the next local day.")
                st.save()
                sleeper(seconds_until_next_local_day())
                continue
            st.save()

            task_id, refusals = next_runnable(finished)
            report.refused.update(refusals)
            report_refusals(refusals, reported_refusals)

            if task_id is None:
                print("nothing approved to do; waiting.")
                sleeper(idle_seconds)
                continue

            _, approval = assess(task_id)
            st = state.State.load()
            _roll_serve_day(st)
            st.data[SERVE_STARTED_KEY] = int(st.data.get(SERVE_STARTED_KEY) or 0) + 1
            st.save()

            try:
                run_one(task_id, approval, report, stub_ok=stub_ok)
            except coordinator.Halt as exc:
                # A rate limit lands here. HALT -- never a retry, and
                # never a switch to API billing: agents.child_env scrubs
                # ANTHROPIC_API_KEY so there is nothing to switch to.
                state.audit("serve_halted", task=task_id, detail=str(exc)[:500])
                coordinator.escalate(task_id, "AutoDev halted", str(exc))
                _entry_for(task_id, "halted", approval, detail=str(exc)[:1000])
                report.halted = str(exc)
                print(f"HALTED: {exc}")
                return 3, report
            except state.CoordinatorBusy as exc:
                state.audit("serve_busy", task=task_id, detail=str(exc)[:300])
                print(str(exc))
                return 2, report
            finished.add(task_id)

        state.audit("serve_finished", **report.as_dict())
        return 0, report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--max-cycles", type=int, default=None,
        help="stop after this many cycles (a bounded run; default: forever)",
    )
    parser.add_argument(
        "--idle-seconds", type=float, default=DEFAULT_IDLE_SECONDS,
        help=f"wait this long when there is nothing to do "
             f"(default {DEFAULT_IDLE_SECONDS})",
    )
    parser.add_argument(
        "--stub-ok", action="store_true",
        help="skip the auth and turn-limit preflight (tests with a stub CLI "
             "only; never a real run)",
    )
    args = parser.parse_args(argv)

    try:
        code, report = serve(
            max_cycles=args.max_cycles, idle_seconds=args.idle_seconds,
            stub_ok=args.stub_ok,
        )
    except KeyboardInterrupt:
        # Ctrl-C is how the operator stops it. The phase in flight has
        # already written its `started` audit row with no `finished` row,
        # which the next run reads as the crash it is and resumes from.
        state.audit("serve_interrupted")
        print("\ninterrupted; nothing further started.")
        return 2
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
