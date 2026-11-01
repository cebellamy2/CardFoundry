#!/usr/bin/env python3
"""The launchd agent's entry point: one bounded serve pass, then exit.

★ WHY A PERIODIC, BOUNDED PASS AND NOT ONE LONG-LIVED LOOP. A
`KeepAlive` daemon running `serve` forever looks simpler and is worse in
two specific ways. STOP makes serve exit 2, and launchd would restart it
immediately, so the operator's own kill switch would turn into a restart
loop that only launchd's 10-second throttle slows down. And a process
that never exits holds the coordinator lock forever, so `coordinator.py
run` by hand would be locked out indefinitely rather than for the length
of one task. A `StartInterval` job that runs ONE cycle and exits has
neither problem: STOP costs one cheap exit per interval, and the lock is
held only while a task is actually being built.

★ WHAT BOUNDS IT NOW THAT THE DAILY CAP IS GONE. The lock, and real
time. A task takes roughly a quarter of an hour, and it holds the lock
for all of it, so every interval that fires meanwhile bounces off
CoordinatorBusy and exits. The effect is one task at a time, with the
next picked up within one interval of the last one finishing -- self
throttling, with no number to tune.

★ IT DOES NOTHING, LOUDLY ENOUGH TO AUDIT, WHEN THERE IS NOTHING TO DO.
The queue is checked READ-ONLY first and serve is not even entered
unless something is runnable. That is not only tidiness: serve writes an
outbox entry the first time it sees a refusal, and under a fresh process
every few minutes "the first time" is every few minutes. The dedupe in
serve.already_reported fixes that at the source; this check means the
ordinary idle case does not reach it at all.

★ WHAT IT STILL CANNOT DO. Everything serve cannot do, because it is
serve: no push, no tag, no deploy, no railway, no Mana Pool write, no
API-key fallback. A rate limit HALTS the day rather than retrying. Each
task lands on its own autodev/* branch and merging to main stays a Code
CLI release that the operator asks for.

Install it with `python -m scripts.autodev.install_autoserve --confirm`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from . import coordinator, serve, state

logger = logging.getLogger("cardfoundry")

LOG_NAME = "serve.log"
# One cycle: at most one task STARTED per firing. A rework is part of
# that task and does not need a second cycle.
DEFAULT_MAX_CYCLES = 1


def log_path():
    return state.ensure_home() / LOG_NAME


def say(message: str) -> None:
    """One timestamped line, to the log and to stdout.

    ★ NO SECRETS, EVER. Only this module's own sentences reach here:
    task ids, counts and refusal reasons, which are already in the audit
    log. Nothing from a child process's environment is logged, and
    agents.child_env has already dropped every credential-looking
    variable before any agent runs.
    """
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    try:
        with log_path().open("a") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        # Catch-and-continue, so it logs: a log we cannot write must not
        # stop the serving, but it must not vanish either.
        logger.warning("autodev autoserve: cannot write %s: %s",
                       log_path(), exc)
    print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES)
    parser.add_argument(
        "--stub-ok", action="store_true",
        help="skip the auth preflight (tests with a stub CLI only)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = coordinator.config()
    except coordinator.Halt as exc:
        say(f"refusing: {exc}")
        return 3

    if not cfg.get("scheduling_enabled"):
        say("scheduling_enabled is false; doing nothing.")
        return 0
    if state.stopped():
        say("STOP is set; doing nothing. "
            "`coordinator.py resume --clear-stop` to clear it.")
        return 0
    if state.paused():
        say("PAUSE is set; doing nothing. `coordinator.py resume` to clear it.")
        return 0

    # Read-only: reads the queue, the approval notes and the audit log,
    # and writes none of them.
    task_id, refusals = serve.next_runnable(set())
    if task_id is None:
        say(f"idle: nothing approved to run "
            f"({len(refusals)} ticket(s) refused).")
        for refused_id, reasons in sorted(refusals.items()):
            say(f"  refused {refused_id}: {'; '.join(reasons)}")
        # Through serve's own reporter, so a refusal reaches the audit log
        # and the outbox exactly once however many times this fires -- and
        # so there is one implementation of "report a refusal", not two.
        serve.report_refusals(refusals)
        return 0

    say(f"starting: {task_id}")
    try:
        code, report = serve.serve(
            max_cycles=args.max_cycles, idle_seconds=0,
            stub_ok=args.stub_ok,
        )
    except state.CoordinatorBusy as exc:
        # The ordinary case when a task is already in flight, not an
        # error: one owner at a time is the point of the lock.
        say(f"another owner holds the lock; doing nothing. ({exc})")
        return 0
    except KeyboardInterrupt:
        say("interrupted.")
        return 2
    say(f"finished: exit {code} {json.dumps(report.as_dict(), sort_keys=True)}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
