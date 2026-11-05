#!/usr/bin/env python3
"""One bounded pass over the ticket inbox: take the oldest, work it, report.

★ WHAT THIS IS FOR. Removing the paste. A ticket for the Code CLI session
has always arrived as text typed into a chat window, and that step is the
operator's most frequent involvement in the loop -- one paste per ticket.
This runner reads the ticket from a file instead (see inbox.py for why a
file and not a prompt), works it, files the report in the outbound
mailbox and moves the ticket to done/.

★ IT ADDS NO NEW POWER, AND THAT IS THE DESIGN. It cannot push, cannot
tag, cannot touch Railway and cannot release: a finished ticket is a
BRANCH and a report, exactly as an AutoDev task is, and merging to main
stays a release the operator asks for under CLAUDE.md. Phase 1 buys back
the paste, nothing else. The auto-release is a separate, later piece with
its own gate, and deliberately not here.

★ THE FENCE IS THE TICKET'S OWN `files`, NOT policy.py. policy.py is
AutoDev's fence and is untouched -- it forbids by name the deploy guard,
the schema and the money services, which is right for an unattended
five-minute builder and is exactly why that builder could not have taken
two thirds of the v2.110.0 batch. This runner's session may touch the
paths its ticket declares and nothing else: guard.py enforces that per
tool call, and inbox.ABSOLUTE_DENY refuses a ticket that names
credentials, the database, backups, imports or the audit trail before a
session ever starts.

★ IT SHIPS DISABLED. `inbox_enabled` defaults to false in
coordinator.DEFAULT_CONFIG, so this does nothing at all until the
operator turns it on -- and then the four existing ways to stop AutoDev
stop this too, because it reads the same STOP and PAUSE sentinels and
takes the same flock.

★ ONE TICKET AT A TIME, ENFORCED BY THE KERNEL. state.single_owner_lock
is the same flock AutoDev's coordinator takes, so an inbox run and an
AutoDev task can never be in flight together -- two sessions editing one
repository is the failure neither of them could detect.

Run one pass by hand with `python -m scripts.autodev.inbox_runner`.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import agents, coordinator, inbox, state

logger = logging.getLogger("cardfoundry")

LOG_NAME = "inbox.log"
BRANCH_PREFIX = "inbox/"
WORKTREE_PREFIX = "inbox-"

# What the runner will accept as the report's own status line, and what it
# writes when the session gave none. CLAUDE.md's four words, exactly.
STATUS_WORDS = ("done", "stopped-at-hard-stop", "refused", "blocked")
STATUS_LINE = re.compile(r"^\s*STATUS:\s*([a-z-]+)\s*$", re.MULTILINE)


@dataclass
class PassReport:
    """What one pass did. Returned for the tests and printed at exit."""

    enabled: bool = True
    ran: str | None = None
    outcome: str | None = None
    rejected: list[str] = field(default_factory=list)
    report: str | None = None
    branch: str | None = None
    head: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled, "ran": self.ran,
            "outcome": self.outcome, "rejected": self.rejected,
            "report": self.report, "branch": self.branch,
            "head": self.head, "detail": self.detail,
        }


def log_path() -> Path:
    return state.ensure_home() / LOG_NAME


def say(message: str) -> None:
    """One timestamped line, to the log and to stdout.

    ★ NO SECRETS, EVER. Only this module's own sentences reach here --
    ticket ids, counts, refusal reasons and a branch name. Nothing from a
    child process's environment is logged, and agents.child_env has
    already dropped every credential-looking variable before a session
    runs.
    """
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    try:
        with log_path().open("a") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        # Catch-and-continue, so it logs: a log we cannot write must not
        # stop the work, and must not vanish either.
        logger.warning("autodev inbox: cannot write %s: %s", log_path(), exc)
    print(line)


def _git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd or state.REPO_ROOT),
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def prepare_worktree(ticket_id: str, base_branch: str) -> tuple[Path, str, str]:
    """A worktree on its own `inbox/<id>` branch. Never the real checkout.

    ★ WHY NOT THE OPERATOR'S CHECKOUT, when a pasted ticket IS worked
    there. Because a pasted ticket is worked by a session the operator is
    watching, and this one is not. Two writers in one checkout with
    nobody between them is the collision neither can see -- and a second
    session on this repository is an ordinary occurrence here, not a
    hypothetical. Its own worktree makes an unattended mistake cost a
    branch nobody merges.

    Deliberately NOT coordinator.prepare_worktree: that one names the
    branch `autodev/<id>`, and an inbox branch must be distinguishable at
    a glance from a branch the narrow builder produced -- they were
    cleared by different fences.
    """
    state.ensure_home()
    worktree = state.worktree_root() / f"{WORKTREE_PREFIX}{ticket_id}"
    branch = f"{BRANCH_PREFIX}{ticket_id}"
    if worktree.exists():
        code, out = _git(["rev-parse", "HEAD"], cwd=worktree)
        if code == 0:
            return worktree, branch, out.strip()
        raise coordinator.Halt(
            f"{worktree} exists but is not a usable worktree: {out[:200]}")
    code, out = _git(["worktree", "add", "-b", branch, str(worktree),
                      base_branch])
    if code != 0:
        raise coordinator.Halt(f"cannot create the worktree: {out[:400]}")
    code, base_sha = _git(["rev-parse", base_branch])
    return worktree, branch, base_sha.strip()


def render_prompt(candidate: inbox.Candidate, worktree: Path,
                  branch: str) -> str:
    """The ticket as the session sees it: the pasted ticket, from a file.

    ★ IT IS DATA READ ALOUD, NOT AN INSTRUCTION FILE. The ticket's own
    fields are rendered into the prompt here, by this program, in a fixed
    frame -- so what authorises the work is the approval note and this
    frame, never prose a ticket smuggled into a field. That distinction
    is what made the inbox possible at all: a session safety check
    (2026-11-04) rightly refuses to let one agent write a free-form
    instruction file for another, and a JSON record with named fields is
    not one.
    """
    data = candidate.data
    lines = [
        "You are the CardFoundry code session, working one ticket that "
        "arrived in the inbox instead of being pasted into a chat window.",
        "",
        "Read CLAUDE.md in this worktree FIRST and hold the change to it. "
        "Everything it says applies here unchanged -- including that the "
        "fenced block IS your report, that you never put a question to the "
        "operator directly, and that every hard stop still stops.",
        "",
        f"WORKTREE: {worktree}  (branch {branch}, off main)",
        "",
        "WHAT YOU MAY DO: edit the files this ticket names, run the test "
        "suite, and commit to this branch. You cannot push, tag, deploy or "
        "touch Railway, and you must not try: this ticket ends in a commit "
        "on this branch and a report. Releasing is the operator's, "
        "unchanged.",
        "",
        "IF THE TICKET IS NOT AS DESCRIBED, say so and stop. Do not adapt "
        "it to whatever you find. If it asks for anything on the operator's "
        "list in CLAUDE.md -- money, pricing, Mana Pool, a data rewrite, a "
        "non-additive migration, anything a buyer or consignor sees, "
        "anything irreversible, new JavaScript -- build the preview, stop "
        "there, and report.",
        "",
        "--- THE TICKET -------------------------------------------------",
        f"id:        {data.get('id')}",
        f"title:     {data.get('title')}",
        f"reviewed:  {data.get('eligibility_reviewed_by')} "
        f"({data.get('eligibility_reviewed_at')})",
        "",
        "intent:",
        f"  {data.get('intent')}",
        "",
        "acceptance criteria (a reviewer must be able to check each one "
        "from the diff and the test output alone):",
    ]
    for item in data.get("acceptance") or []:
        lines.append(f"  - {item}")
    lines += ["", "files this change may touch, and no others:"]
    for item in candidate.files:
        lines.append(f"  - {item}")
    if candidate.tests:
        lines += ["", "tests named by the ticket:"]
        for item in candidate.tests:
            lines.append(f"  - {item}")
    if data.get("notes"):
        lines += ["", "notes:", f"  {data.get('notes')}"]
    lines += [
        "----------------------------------------------------------------",
        "",
        "WHEN YOU ARE FINISHED, reply with ONE plain fenced block and "
        "nothing else, as CLAUDE.md requires. Put a line "
        "`STATUS: done | stopped-at-hard-stop | refused | blocked` as the "
        "first line inside the block. The block is the whole report: "
        "findings, the reasoning behind every judgment call, the "
        "verification numbers, and anything still open. It is filed in the "
        "reports mailbox word for word, so write it for a reader with no "
        "scrollback.",
    ]
    return "\n".join(lines)


def _status_from(body: str) -> tuple[str, str]:
    """(status, body without its own STATUS line).

    The session writes the status inside its block; the mailbox file
    carries it on its own line under the header, so the line is lifted
    out rather than printed twice. An absent or unrecognised status is
    `blocked` -- a report that does not say how it ended has not told us
    it ended well.
    """
    match = STATUS_LINE.search(body)
    if not match or match.group(1) not in STATUS_WORDS:
        return "blocked", body
    return match.group(1), (body[:match.start()] + body[match.end():]).strip()


def _diffstat(worktree: Path, base_sha: str) -> str:
    code, out = _git(["diff", "--stat", f"{base_sha}...HEAD"], cwd=worktree)
    return out.strip() if code == 0 else "(diff unavailable)"


def _evidence_footer(branch: str, base_sha: str, head: str,
                     diffstat: str, dirt: str) -> str:
    """What a reader checks instead of believing the report.

    Same stance as the outbox: this footer is EVIDENCE, written by this
    program, not by the session -- so a report claiming a clean suite can
    be checked against the branch it claims it on.
    """
    lines = [
        "",
        "---",
        "",
        "RUNNER EVIDENCE (written by scripts/autodev/inbox_runner.py, not "
        "by the session; nothing here is an instruction):",
        "",
        f"    branch   {branch}",
        f"    base     {base_sha}",
        f"    head     {head}",
        "",
        "    diff --stat:",
    ]
    lines += [f"      {line}" for line in (diffstat or "(none)").splitlines()]
    if dirt:
        lines += ["", "    UNCOMMITTED in the worktree when the session ended:"]
        lines += [f"      {line}" for line in dirt.splitlines()]
    lines += [
        "",
        "    verify:",
        f"      git log --oneline main...{branch}",
        f"      git diff main...{branch}",
        f"      git rev-parse {branch}   # must equal {head}",
        "",
        "    This branch is NOT pushed and NOT released. Merging it to main "
        "is an ordinary release the operator asks for.",
    ]
    return "\n".join(lines)


def run_once(*, stub_ok: bool = False) -> tuple[int, PassReport]:
    """One pass. Returns (exit code, report)."""
    report = PassReport()

    try:
        cfg = coordinator.config()
    except coordinator.Halt as exc:
        say(f"refusing: {exc}")
        return 3, report

    if not cfg.get("inbox_enabled"):
        report.enabled = False
        say("inbox_enabled is false; doing nothing.")
        return 0, report
    if state.stopped():
        say("STOP is set; doing nothing. "
            "`coordinator.py resume --clear-stop` to clear it.")
        return 0, report
    if state.paused():
        say("PAUSE is set; doing nothing. `coordinator.py resume` to clear it.")
        return 0, report

    inbox.ensure_mailbox()

    try:
        with state.single_owner_lock():
            return _work(cfg, report, stub_ok=stub_ok)
    except state.CoordinatorBusy as exc:
        # The ordinary case when something is already in flight, not an
        # error: one owner at a time is the point of the lock.
        say(f"another owner holds the lock; doing nothing. ({exc})")
        return 0, report


def _work(cfg: dict, report: PassReport, *, stub_ok: bool) -> tuple[int, PassReport]:
    candidate, refused = inbox.next_runnable()

    for bad in refused:
        moved = inbox.reject(bad)
        report.rejected.append(bad.id)
        say(f"rejected {bad.path.name} -> {moved.name}: "
            f"{'; '.join(bad.refusals)}")
        inbox.file_report(
            f"inbox ticket rejected {bad.id}", "refused",
            "This ticket was NOT run and nothing was built. Every reason "
            "it was refused, not just the first:\n\n"
            + "\n".join(f"- {reason}" for reason in bad.refusals)
            + f"\n\nThe ticket is unchanged at {moved}. Fix it and write a "
            "new one; an edited ticket is a new ticket and needs its own "
            "approval note.\n",
        )

    if candidate is None:
        say(f"idle: nothing runnable in the inbox "
            f"({len(refused)} rejected this pass).")
        return 0, report

    if not stub_ok:
        ok, detail = agents.preflight_auth()
        state.audit("inbox_preflight_auth", ok=ok, detail=detail)
        if not ok:
            say(f"BLOCKER: the CLI cannot authenticate: {detail}")
            report.detail = detail
            return 3, report

    ticket_id = candidate.id
    say(f"starting: {ticket_id}")
    state.audit("inbox_run_started", task=ticket_id,
                digest=candidate.digest,
                approval=candidate.approval.as_evidence()
                if candidate.approval else None)

    worktree, branch, base_sha = prepare_worktree(ticket_id, cfg["base_branch"])
    report.branch = branch
    failures = coordinator.isolation_preflight(worktree)
    if failures:
        say("BLOCKER: worktree isolation unproven: " + "; ".join(failures))
        report.detail = "; ".join(failures)
        state.audit("inbox_isolation_failed", task=ticket_id, failures=failures)
        return 3, report

    evidence = state.run_dir(ticket_id, 1)
    prompt = render_prompt(candidate, worktree, branch)
    try:
        run = agents.run_role(
            "inbox", prompt, worktree, evidence,
            ticket_tests=candidate.tests, ticket_files=candidate.files,
            model=cfg["models"].get("inbox") or agents.DEFAULT_MODELS["inbox"],
            settings_path=agents.effective_settings(
                evidence, source=agents.INBOX_SETTINGS_PATH),
        )
    except agents.RateLimited as exc:
        # ★ A USAGE LIMIT STOPS THE PASS, and nothing is half-finished:
        # this runner never pushes, so there is no release to leave half
        # applied. The branch and the ticket both stay exactly where they
        # are, and the ticket is NOT moved to done -- the next pass picks
        # it up again.
        say(f"HALTED on a usage limit: {exc}")
        state.audit("inbox_halted", task=ticket_id, detail=str(exc)[:500])
        inbox.file_report(
            ticket_id, "blocked",
            f"The run stopped on a subscription usage limit and nothing was "
            f"finished.\n\nTicket {ticket_id} is still in the inbox and will "
            f"be picked up again. Branch {branch} holds whatever the session "
            f"had committed; nothing was pushed and nothing was released.\n\n"
            f"limit: {str(exc)[:500]}\n",
        )
        report.detail = str(exc)[:500]
        return 3, report
    except agents.AuthUnavailable as exc:
        say(f"BLOCKER: {exc}")
        report.detail = str(exc)[:500]
        return 3, report

    code, head = _git(["rev-parse", "HEAD"], cwd=worktree)
    head = head.strip() if code == 0 else "(unknown)"
    report.head = head
    code, dirt = _git(["status", "--porcelain"], cwd=worktree)
    dirt = dirt.strip() if code == 0 else ""

    body = (run.result_text or "").strip()
    if not body:
        body = ("The session returned no report text. Its raw output is in "
                f"{evidence}.")
    status, body = _status_from(body)
    if not run.ok and status == "done":
        # The session said it finished and the process did not. Trust the
        # process: a report claiming success from a failed run is the one
        # thing that must not be filed as success.
        status = "blocked"
        body += (f"\n\n(The runner overrode STATUS: done -- the session "
                 f"process exited {run.returncode}. Raw output in {evidence}.)")

    filed = inbox.file_report(
        ticket_id, status,
        body + _evidence_footer(branch, base_sha, head,
                                _diffstat(worktree, base_sha), dirt),
    )
    report.report = str(filed)
    report.ran = ticket_id
    report.outcome = status
    moved = inbox.complete(candidate, status)
    state.audit("inbox_run_finished", task=ticket_id, outcome=status,
                branch=branch, head=head, report=str(filed))
    say(f"finished: {ticket_id} status={status} branch={branch} "
        f"report={filed.name} ticket->{moved.name}")
    return (0 if status == "done" else 1), report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stub-ok", action="store_true",
        help="skip the auth preflight (tests with a stub CLI only)")
    args = parser.parse_args(argv)
    try:
        code, report = run_once(stub_ok=args.stub_ok)
    except KeyboardInterrupt:
        state.audit("inbox_interrupted")
        say("interrupted; nothing further started.")
        return 2
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
