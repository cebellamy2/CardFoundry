#!/usr/bin/env python3
"""One bounded pass at releasing a finished branch: merge, number, prove, push.

★ WHAT THIS IS AND WHAT IT IS NOT. It is the release half of the hands-off
loop: a finished `inbox/*` or `autodev/*` branch whose report says it is
done, whose authority is an approval note, and whose ACTUAL diff passes
release_gate, is merged to main, numbered, tested, pushed through the
pre-push hook, tagged and verified live. It is NOT a builder: it writes no
code, fixes nothing, and makes no judgement about whether the work is
good. The branch is already finished when this sees it.

★ THERE IS NO AGENT IN THIS LOOP, AND THAT IS THE POINT. Every other
unattended runner here starts a `claude` session, because building needs
one. Releasing does not: the sequence is the one written down in the
2026-11-05 scoping report and performed by hand at every release since
go-live -- fetch, take the number late, merge, write VERSION and the
entry, run the suite, run the checks, push through the hook, chain the
tag, verify the footer. Every step is mechanical and every step has a
pass/fail answer. So this is ordinary Python: no prompt, no model, no
room for a confident wrong decision in the one program here that can
reach production.

★ FIVE BOUNDS, AND IT SHIPS OFF.
  1. `releases_enabled` defaults FALSE in coordinator.DEFAULT_CONFIG, so
     this does nothing at all until the operator turns it on.
  2. At most MAX_RELEASES_PER_DAY in a local day (operator, 2026-11-06).
  3. Nothing between QUIET_START_HOUR and QUIET_END_HOUR local time
     (operator, 2026-11-06) -- the tick windows protect the crons, they
     do not protect the operator's sleep.
  4. One at a time, under state.single_owner_lock -- the same flock
     AutoDev and the inbox take, so a release can never be in flight
     beside a build.
  5. release_gate on the diff, and approvals.py on the authority. Both
     required, neither sufficient.
And the STOP and PAUSE sentinels stop it, like everything else here.

★ IT PUSHES, SO IT NEVER BYPASSES. The push goes through the pre-push
hook exactly as a human's does, and this module does not know the
`--no-verify` flag exists. There is no code path to it and no
configuration that reaches it: a refusal from the hook is either waited
out (a cron tick window, a job in flight) or reported and left alone. The
hook's own five checks are the backstop for everything this program
believes about version numbers.

★ WHY THE WORK HAPPENS IN A WORKTREE AND NOT IN THE OPERATOR'S CHECKOUT.
Because the operator and another session are often in that checkout, and
an unattended merge in a tree somebody is editing is the collision
neither side can see. The release is staged in its own worktree at
origin/main, and only two things ever reach the real checkout: the push
(which goes to the remote) and, after a successful release, one
`--ff-only` fast-forward of the local main -- refused unless that tree is
clean and on main, because the next inbox ticket branches off local main
and a stale one would build on last week's code.

★ THE INTERRUPTION THAT ACTUALLY MATTERS. Between the main push and the
tag push, main is live and the tag is absent -- and changelog_guard will
complain about that forever. So the tag is created locally BEFORE the
push and the two pushes are chained, and a pass that finds "origin/main
is at version X with no tag v X" finishes only that, before considering
any branch. Nothing else is ever resumed: a half-run release is
abandoned in its worktree and reported, never continued on a guess.

Run one pass by hand with `python -m scripts.autodev.release_runner`,
which is a DRY RUN and writes nothing. `--confirm` is what releases.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import (approvals, coordinator, inbox, inbox_runner, release_gate,
               state)

logger = logging.getLogger("cardfoundry")

LOG_NAME = "release.log"
# Remembers which branches were last reported as waiting, so an unchanged
# situation is not re-reported every hour. See _waiting_changed.
WAITING_NAME = "release_waiting.json"
WORKTREE_NAME = "release-staging"
BRANCH_PREFIXES = ("inbox/", "autodev/")

# ★ THE OPERATOR'S TWO BOUNDS, 2026-11-06, in their own terms: "at most 3
# automatic releases per day" and "quiet hours 23:00-07:00 ET, no
# automatic release then". Both were offered in the 2026-11-05 scoping
# report as things that would cost nothing now and be much harder to add
# after the first surprise; both were taken.
#
# LOCAL TIME, DELIBERATELY. datetime.now() is the operator's own clock and
# their machine is Eastern, which is exactly what "quiet hours" means --
# the same choice inbox.report_path makes for the mailbox stamp, and for
# the same reason: no timezone arithmetic that could be wrong in one half
# of the year. The CRON windows are UTC and stay UTC; that is the hook's
# business, not this one's.
MAX_RELEASES_PER_DAY = 3
QUIET_START_HOUR = 23
QUIET_END_HOUR = 7

# How often launchd fires a pass. Hourly: with quiet hours and a cap of
# three, a release has sixteen chances a day and a finished branch waits
# well under an hour. Owned here so install_release cannot drift from it.
PASS_INTERVAL_SECONDS = 3600

# A hook refusal that will pass on its own: a cron tick window is at most
# fifteen minutes, and a job in flight is minutes. Four attempts six
# minutes apart covers both without the pass outliving its own interval.
PUSH_ATTEMPTS = 4
PUSH_RETRY_SECONDS = 360

# Somebody else pushed while the suite was running. Start again from the
# fetch: new base, new number, and the FULL suite on the new merged tree,
# because a conflict can be semantic and a diff-only re-check would not
# see it. Twice, then stop and report -- a main this busy is not one to
# keep racing unattended.
MAX_RESTARTS = 2

# How many times to step past an already-tagged number before giving up.
# Ten is far past any real case (it would mean ten releases tagged and
# none of them on main) and it keeps the search finite.
MAX_RENUMBERS = 10

# Railway takes about two and a half minutes from push to the new version
# answering, measured repeatedly. Twelve tries thirty seconds apart is six
# minutes, comfortably past that without waiting for a deploy that failed.
FOOTER_ATTEMPTS = 12
FOOTER_RETRY_SECONDS = 30
FOOTER = re.compile(r"CardFoundry v(\d+\.\d+\.\d+)")

# The suite is about fifteen minutes. An hour is not a budget, it is a
# deadlock breaker: a pytest that never returns would otherwise hold the
# flock against every other runner for as long as the Mac stays awake.
SUITE_TIMEOUT_SECONDS = 3600

KEYCHAIN_SERVICE = "cardfoundry-deploy-readiness"
# The same seam the pre-push hook uses, so the suite's stub serves both and
# no test ever reads the real Keychain. Never set by hand.
SECURITY_ENV = "CARDFOUNDRY_HOOK_SECURITY"
# Test seam: read the live footer from a file instead of the network, so no
# test touches the network. Never set by hand.
FOOTER_FILE_ENV = "CARDFOUNDRY_RELEASE_FOOTER_FILE"

# What a hook refusal means. A tick window and a job in flight pass on
# their own, so they are waited out; everything else is a decision and is
# reported. Matched against the hook's own wording, which tests pin.
RETRYABLE_REFUSALS = (
    "cron tick",
    "job in flight",
)
NON_FAST_FORWARD = ("non-fast-forward", "fetch first", "rejected")


@dataclass
class ReleasePass:
    """What one pass did. Returned for the tests and printed at exit."""

    enabled: bool = True
    confirmed: bool = False
    quiet: bool = False
    released: str | None = None
    version: str | None = None
    head: str | None = None
    tag: str | None = None
    live: str | None = None
    report: str | None = None
    waiting: list[dict] = field(default_factory=list)
    detail: str | None = None
    steps: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.steps.append(line)
        say(line)

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled, "confirmed": self.confirmed,
            "quiet": self.quiet, "released": self.released,
            "version": self.version, "head": self.head, "tag": self.tag,
            "live": self.live, "report": self.report,
            "waiting": self.waiting, "detail": self.detail,
            "steps": self.steps,
        }


def log_path() -> Path:
    return state.ensure_home() / LOG_NAME


def say(message: str) -> None:
    """One timestamped line, to the log and to stdout.

    ★ NO SECRETS, EVER. Only this module's own sentences reach here --
    branch names, version numbers, refusal reasons, step names. The
    readiness password is read into a local and passed to urllib; its
    VALUE is never an argument to this function, and _readiness_creds
    reports only where each credential came from.
    """
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    try:
        with log_path().open("a") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        # Catch-and-continue, so it logs: a log we cannot write must not
        # stop the work, and must not vanish either.
        logger.warning("autodev release: cannot write %s: %s", log_path(), exc)
    print(line)


def _git(args: list[str], cwd: Path | None = None,
         timeout: float | None = 300) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd or state.REPO_ROOT),
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


# --------------------------------------------------------------------------
# The bounds
# --------------------------------------------------------------------------

def quiet_now(now: datetime | None = None) -> bool:
    """True inside the operator's quiet hours. Wraps midnight."""
    hour = (now or datetime.now()).hour
    if QUIET_START_HOUR > QUIET_END_HOUR:
        return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR
    return QUIET_START_HOUR <= hour < QUIET_END_HOUR


def releases_today(today: date | None = None) -> list[dict]:
    """The releases this runner pushed today, from the AUDIT LOG.

    ★ COUNTED FROM THE APPEND-ONLY RECORD, NOT A COUNTER FILE. state.audit
    already writes a row for every ending and is the existing recovery
    log, so a second file holding the same number could only ever
    disagree with it -- and the number that bounds how often this touches
    production is not one to keep two copies of. The row is written the
    moment main is pushed, which is the moment a release became live.
    """
    stamp = (today or date.today()).isoformat()
    rows = []
    for row in state.read_audit():
        if row.get("event") != "release_pushed":
            continue
        if str(row.get("at") or "").startswith(stamp):
            rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Finding a branch that is finished, authorised and in scope
# --------------------------------------------------------------------------

def _branches() -> list[str]:
    code, out = _git(["for-each-ref", "--format=%(refname:short)",
                      "refs/heads/inbox", "refs/heads/autodev"])
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines()
            if line.strip().startswith(BRANCH_PREFIXES)]


def _slug(task_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")


def _newest(paths: list[Path]) -> Path:
    """The most recently written of these files.

    ★ BY MTIME, NOT BY NAME, and a test found out why. Both the mailbox
    and the outbox avoid overwriting by appending a counter -- `foo-2.md`
    beside `foo.md` -- and `-` sorts BEFORE `.`, so the plain name comes
    LAST in a lexicographic sort and "newest wins" picked the OLDER file
    every time two reports landed in the same minute. Which is exactly
    when it matters: a ticket re-run after a fix.
    """
    return max(paths, key=lambda path: (path.stat().st_mtime, path.name))


def mailbox_status(task_id: str) -> tuple[Path | None, str | None]:
    """The NEWEST mailbox report for this ticket, and its STATUS word.

    Reports are named `<stamp>-<slug-of-topic>.md` by inbox.report_path,
    and the inbox runner files them with the ticket id as the topic -- so
    the ticket's own report is the one whose name, after the stamp, is the
    ticket's slug (plus the `-2` counter _free_name adds when two reports
    land in the same minute). Newest wins: a ticket re-run after a fix has
    two reports and the old one is not the answer.
    """
    root = inbox.reports_dir()
    if not root.exists():
        return None, None
    slug = _slug(task_id)
    found: list[Path] = []
    for path in sorted(root.glob("*.md")):
        rest = re.sub(r"^\d{8}-\d{4}-", "", path.stem)
        if rest == slug or re.fullmatch(rf"{re.escape(slug)}-\d+", rest):
            found.append(path)
    if not found:
        return None, None
    newest = _newest(found)
    try:
        text = newest.read_text()
    except OSError as exc:
        logger.warning("autodev release: cannot read %s: %s", newest, exc)
        return newest, None
    match = inbox_runner.STATUS_LINE.search(text)
    return newest, (match.group(1) if match else None)


def outbox_status(task_id: str) -> tuple[Path | None, str | None]:
    """The newest AutoDev outbox entry for this task, and its outcome.

    AutoDev's finished-work record is a JSON entry rather than a mailbox
    report, so an `autodev/*` branch's "report says done" is this.
    """
    root = state.home() / "outbox"
    if not root.exists():
        return None, None
    found = sorted(root.glob(f"*-{task_id}-*.json"))
    if not found:
        return None, None
    newest = _newest(found)
    try:
        data = json.loads(newest.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("autodev release: cannot read %s: %s", newest, exc)
        return newest, None
    return newest, str(data.get("outcome") or "") or None


def ticket_data(task_id: str) -> dict | None:
    """The ticket itself, wherever it ended up: the inbox, done/, or the
    AutoDev queue. Needed to verify the approval note's digest, which is
    what makes editing a ticket after its OK invalidate the OK."""
    for candidate in (inbox.inbox_dir() / f"{task_id}.json",
                      inbox.done_dir() / f"{task_id}.json",
                      state.home() / "queue" / f"{task_id}.json"):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text())
            except (OSError, ValueError) as exc:
                logger.warning("autodev release: cannot read %s: %s",
                               candidate, exc)
                return None
            if isinstance(data, dict):
                return data
    # done/ keeps the outcome in the name when a ticket was renamed.
    for path in sorted(inbox.done_dir().glob(f"{task_id}*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def assess(branch: str, base: str = "origin/main") -> release_gate.Verdict:
    """Every reason this branch may not be released unattended.

    All of them at once, the same stance as every other gate here: one
    refusal should tell Cowork everything rather than revealing the next
    problem on the next pass.
    """
    task_id = branch.split("/", 1)[1] if "/" in branch else branch
    verdict = release_gate.Verdict(branch=branch, task_id=task_id, base=base)

    code, head = _git(["rev-parse", branch])
    verdict.head = head.strip() if code == 0 else ""
    if code != 0:
        verdict.refusals.append(f"cannot resolve {branch}: {head[:200]}")
        return verdict

    code, subjects = _git(["log", "--format=%s", f"{base}..{branch}"])
    if code != 0:
        verdict.refusals.append(f"cannot log {base}..{branch}: {subjects[:200]}")
        return verdict
    verdict.subjects = [line for line in subjects.splitlines() if line.strip()]

    # Is it finished? One question, two records, depending on the track.
    if branch.startswith("inbox/"):
        report, status = mailbox_status(task_id)
    else:
        report, status = outbox_status(task_id)
    verdict.report = report
    verdict.status = status
    if report is None:
        verdict.refusals.append(
            f"no finished report for {task_id}; a branch with no report is "
            "not a branch anyone has said is done")
    elif status != "done":
        verdict.refusals.append(
            f"the report for {task_id} says {status or 'nothing'}, not done "
            f"({report.name})")

    # Who authorised it? approvals.py, unchanged, including the digest.
    data = ticket_data(task_id)
    digest = inbox.ticket_digest(data) if data else None
    approval = approvals.load(task_id, ticket_digest=digest)
    verdict.approval = approval
    if not approval.is_approved:
        verdict.refusals.extend(approval.refusals)
    elif approval.ticket_digest and digest is None:
        # ★ FAILS CLOSED ON A TICKET IT CANNOT FIND. The note carries a
        # digest, so the ticket it approved is knowable -- and if we cannot
        # read that ticket we cannot tell whether it changed after the OK.
        # "Probably fine" is not an authority.
        verdict.refusals.append(
            f"the approval note for {task_id} carries a ticket digest but "
            "the ticket itself cannot be read, so nothing can check that it "
            "is the ticket that was approved")

    # What does it actually change? The diff, never the ticket's own list.
    paths, error = release_gate.diff_paths(base, branch)
    if error:
        verdict.refusals.append(error)
    verdict.paths = paths
    if not paths:
        verdict.refusals.append(
            f"{branch} changes nothing against {base}; there is no release "
            "in it")
    verdict.refusals.extend(release_gate.assess_paths(paths))

    bump, bump_refusal = release_gate.bump_for(verdict.subjects)
    verdict.bump = bump
    if bump_refusal:
        verdict.refusals.append(bump_refusal)

    if verdict.refusals:
        logger.info("autodev release: %s not releasable: %s",
                    branch, "; ".join(verdict.refusals))
    return verdict


def candidates(base: str = "origin/main") -> list[release_gate.Verdict]:
    """Every inbox/autodev branch, assessed, OLDEST COMMIT FIRST.

    Oldest first so a queue drains in the order the work finished rather
    than in whatever order git prints refs, and so the three-a-day cap
    spends itself on the oldest finished work.
    """
    verdicts = [assess(branch, base) for branch in _branches()]

    def when(verdict: release_gate.Verdict) -> str:
        code, out = _git(["log", "-1", "--format=%cI", verdict.branch])
        return out.strip() if code == 0 else ""

    return sorted(verdicts, key=when)


# --------------------------------------------------------------------------
# The release itself
# --------------------------------------------------------------------------

def release_worktree(sha: str) -> Path:
    """A detached worktree at `sha`, reused across passes, always clean.

    Detached on purpose: this never creates a branch of its own, so there
    is no ref anybody could mistake for work in progress, and the thing
    pushed to main is a commit rather than somebody's branch.
    """
    state.ensure_home()
    path = state.worktree_root() / WORKTREE_NAME
    if path.exists():
        code, out = _git(["rev-parse", "--git-dir"], cwd=path)
        if code != 0:
            raise coordinator.Halt(
                f"{path} exists but is not a usable worktree: {out[:200]}")
        # Anything left over from a pass that died is abandoned, never
        # continued: a half-staged release is not a thing to guess at.
        _git(["merge", "--abort"], cwd=path)
        _git(["reset", "--hard", sha], cwd=path)
        _git(["clean", "-fdx", "--exclude=.venv"], cwd=path)
    else:
        code, out = _git(["worktree", "add", "--detach", str(path), sha])
        if code != 0:
            raise coordinator.Halt(f"cannot create {path}: {out[:400]}")
    inbox_runner.link_venv(path)
    return path


def changelog_entry(version: str, bump: str, subjects: list[str], *,
                    branch: str, basis: str | None,
                    today: date | None = None) -> str:
    """The CHANGELOG entry for this release, in the format the guard parses.

    The bullets are the branch's own commit subjects, which is the most
    honest summary available to a program: they were written by whoever
    did the work, about the work. The last line records that the release
    itself was automatic and under which authority -- so a reader of the
    changelog alone can tell, which is the same reason approvals.py
    carries the basis into its evidence.
    """
    heading = f"## [{version}] - {(today or date.today()).isoformat()}"
    section = "### Added" if bump == "minor" else "### Fixed"
    lines = [heading, "", section, ""]
    for subject in subjects:
        text = re.sub(r"^[a-z]+(\([^)]*\))?!?:\s*", "", subject.strip())
        text = text[:1].upper() + text[1:] if text else subject.strip()
        lines.append(f"- {text}")
    lines += [
        "",
        f"  Released automatically from `{branch}` by "
        f"scripts/autodev/release_runner.py, under "
        f"{basis or 'a per-item operator approval'}. The diff was checked "
        f"against scripts/autodev/release_gate.py and the full suite ran on "
        f"the merged tree before the push.",
        "",
    ]
    return "\n".join(lines)


def insert_entry(text: str, entry: str) -> tuple[str, str | None]:
    """The changelog with this entry above the newest one. (text, error).

    ★ AND THE PROOF THAT NOTHING ELSE MOVED. Removing the entry again must
    reproduce the original byte for byte -- the check I run by hand at
    every release, because it is what catches a bad conflict resolution
    that quietly rewrote somebody else's entry. Here it is not a check
    afterwards, it is the construction: the new text IS the old text with
    one block inserted, and the caller proves it.
    """
    marker = "\n## ["
    at = text.find(marker)
    if at < 0:
        return text, "CHANGELOG.md has no '## [' heading to insert above"
    at += 1
    return text[:at] + entry + text[at:], None


def _suite(worktree: Path) -> tuple[bool, str]:
    """The full suite, in the worktree, through its own linked venv."""
    python = worktree / ".venv" / "bin" / "python"
    if not python.exists():
        return False, (f"no interpreter at {python}; the worktree has no "
                       "linked virtualenv so the suite cannot run")
    env = {**os.environ, "PYTHONPATH": str(worktree),
           "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(worktree), capture_output=True, text=True,
            env=env, timeout=SUITE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, (f"the suite did not finish within "
                       f"{SUITE_TIMEOUT_SECONDS}s; nothing was pushed")
    tail = [line for line in (proc.stdout or "").strip().splitlines()
            if line.strip()]
    summary = tail[-1] if tail else "(no pytest output)"
    return proc.returncode == 0, summary


def _compileall(worktree: Path) -> tuple[bool, str]:
    python = worktree / ".venv" / "bin" / "python"
    proc = subprocess.run(
        [str(python), "-m", "compileall", "-q", ".",
         "-x", r"/\.git/|/\.venv/"],
        cwd=str(worktree), capture_output=True, text=True, timeout=900)
    return proc.returncode == 0, (
        "ok" if proc.returncode == 0
        else (proc.stdout or proc.stderr or "")[-400:])


def _integrity_check() -> tuple[bool, str]:
    """PRAGMA integrity_check on the dev database, READ-ONLY.

    The database lives in the real checkout and a worktree carries none,
    so this opens the checkout's copy in read-only URI mode -- the check
    is about the operator's dev database being sound, and a release must
    never write to it. Absent is reported, not failed: a clone without one
    is a normal state and not a reason to refuse a release.
    """
    path = state.REPO_ROOT / "cardfoundry.db"
    if not path.exists():
        return True, f"skipped -- no database at {path}"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            answer = conn.execute("PRAGMA integrity_check;").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # Catch-and-continue, so it logs: an unreadable database is a real
        # finding and must not pass as a tick.
        logger.warning("autodev release: integrity_check failed: %s", exc)
        return False, f"integrity_check could not run: {exc}"
    verdict = (answer or ("(no answer)",))[0]
    return verdict == "ok", str(verdict)


def _readiness_creds() -> tuple[str, str, str]:
    """(base_url, password, where each came from). The VALUES never log.

    Env first, then the macOS Keychain, exactly as scripts/hooks/pre-push
    resolves the same two secrets -- same service name, same accounts, so
    there is one place on this machine where they live and the hook and
    this program cannot disagree about where that is.
    """
    security = os.environ.get(SECURITY_ENV) or "/usr/bin/security"

    def keychain(account: str) -> str:
        try:
            proc = subprocess.run(
                [security, "find-generic-password", "-s", KEYCHAIN_SERVICE,
                 "-a", account, "-w"],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("autodev release: cannot read the Keychain: %s", exc)
            return ""
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""

    base_url = os.environ.get("CARDFOUNDRY_BASE_URL") or ""
    base_source = "env" if base_url else "keychain"
    if not base_url:
        base_url = keychain("base-url")
    password = os.environ.get("CARDFOUNDRY_SERVICE_PASSWORD") or ""
    pw_source = "env" if password else "keychain"
    if not password:
        password = keychain("service-password")
    sources = (f"base-url={base_source if base_url else 'missing'} "
               f"password={pw_source if password else 'missing'}")
    return base_url, password, sources


def live_version(sleeper=time.sleep) -> tuple[str | None, str]:
    """The version in the footer of /login, polled while Railway deploys.

    ★ /login AND ITS FOOTER, because there is no /app/VERSION endpoint --
    established 2026-10 and still true. GET /login is unauthenticated;
    the credentials are sent anyway because the readiness check uses the
    same pair and a redirect to the gate would otherwise answer nothing.
    """
    override = os.environ.get(FOOTER_FILE_ENV)
    if override:
        # Test seam: no test touches the network, and this is how.
        try:
            text = Path(override).read_text()
        except OSError as exc:
            return None, f"cannot read {override}: {exc}"
        match = FOOTER.search(text)
        return (match.group(1) if match else None), f"read from {override}"

    base_url, password, sources = _readiness_creds()
    if not base_url or not password:
        return None, (f"cannot check the live footer: {sources}. See "
                      "KEYCHAIN SETUP in scripts/hooks/pre-push.")
    url = f"{base_url.rstrip('/')}/login"
    token = base64.b64encode(f"hook:{password}".encode()).decode()
    last = ""
    for attempt in range(1, FOOTER_ATTEMPTS + 1):
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
            match = FOOTER.search(body)
            if match:
                return match.group(1), f"{url} after {attempt} read(s)"
            last = "the page carried no CardFoundry version footer"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Catch-and-continue, so it logs: a deploy in progress refuses
            # connections for a while, and that is the expected case here.
            last = f"{type(exc).__name__}: {exc}"
            logger.info("autodev release: footer read %s failed: %s",
                        attempt, last)
        if attempt < FOOTER_ATTEMPTS:
            sleeper(FOOTER_RETRY_SECONDS)
    return None, f"{url}: {last}"


def _push(worktree: Path, version: str, rp: ReleasePass,
          sleeper=time.sleep) -> tuple[str, str]:
    """Push main through the hook, then chain the tag. ('ok'|..., detail).

    ★ THROUGH THE HOOK, AND THERE IS NO WAY ROUND IT. `--no-verify` does
    not appear in this module. A refusal that will pass on its own is
    waited out; a non-fast-forward means somebody else released while the
    suite ran, and the caller starts again with a new number and a new
    full suite; anything else is reported and left alone.

    ★ THE TAG IS CHAINED, NOT PIPED, and it exists locally already -- so
    the window where main is live and the tag is missing is one failed
    command wide, and the next pass finishes it.
    """
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        code, out = _git(["push", "origin", "HEAD:refs/heads/main"],
                         cwd=worktree, timeout=600)
        rp.say(f"push attempt {attempt}: exit {code}")
        if code == 0:
            state.audit("release_pushed", version=version,
                        branch=str(rp.released), head=rp.head)
            # Chained on the push above succeeding, which is the rule.
            tag_code, tag_out = _git(["push", "origin", f"v{version}"],
                                     cwd=worktree, timeout=600)
            if tag_code != 0:
                return "tag-failed", f"main is pushed; the tag is not: {tag_out[:400]}"
            state.audit("release_tag_pushed", version=version)
            return "ok", out[-400:]
        lowered = out.lower()
        if any(mark in lowered for mark in NON_FAST_FORWARD) and \
                "pre-push" not in lowered:
            return "non-fast-forward", out[-400:]
        if any(mark in lowered for mark in RETRYABLE_REFUSALS):
            if attempt < PUSH_ATTEMPTS:
                rp.say(f"the hook refused for a reason that passes on its "
                       f"own; waiting {PUSH_RETRY_SECONDS}s")
                sleeper(PUSH_RETRY_SECONDS)
                continue
            return "hook-window", out[-400:]
        return "refused", out[-400:]
    return "hook-window", "the hook refused every attempt"


def finish_missing_tag(confirm: bool, rp: ReleasePass) -> bool:
    """The one interruption that is resumed: main pushed, tag absent.

    Nothing else is ever continued. This is here because the gap between
    the two pushes is the only state where doing nothing leaves the
    repository permanently wrong -- changelog_guard counts an untagged
    shipped version as a defect forever -- and because finishing it is one
    unambiguous command with nothing to decide.
    """
    code, out = _git(["fetch", "origin", "--tags"], timeout=300)
    if code != 0:
        rp.say(f"cannot fetch: {out[:200]}")
        return False
    code, version = _git(["show", "origin/main:VERSION"])
    if code != 0:
        return False
    version = version.strip()
    code, _ = _git(["rev-parse", "--verify", "--quiet", f"refs/tags/v{version}"])
    local_tag = code == 0
    code, remote = _git(["ls-remote", "--tags", "origin", f"refs/tags/v{version}"],
                        timeout=300)
    if code == 0 and remote.strip():
        return False
    rp.say(f"origin/main is at {version} with no remote tag v{version}")
    if not local_tag:
        rp.say(f"no local tag v{version} either -- NOT creating one: a tag "
               f"this runner did not make is not one it may invent. "
               f"Reported for the operator.")
        state.audit("release_tag_missing", version=version, local=False)
        return False
    if not confirm:
        rp.say("DRY RUN -- would push the missing tag.")
        return False
    code, out = _git(["push", "origin", f"v{version}"], timeout=600)
    rp.say(f"pushed the missing tag v{version}: exit {code}")
    state.audit("release_tag_pushed", version=version, resumed=True)
    return code == 0


def _attempt(verdict: release_gate.Verdict, rp: ReleasePass,
             confirm: bool, sleeper) -> tuple[str, str]:
    """One whole release attempt. ('ok'|'restart'|'stop', detail)."""
    code, out = _git(["fetch", "origin", "--tags"], timeout=300)
    if code != 0:
        return "stop", f"cannot fetch origin: {out[:300]}"
    code, base_sha = _git(["rev-parse", "origin/main"])
    if code != 0:
        return "stop", f"cannot resolve origin/main: {base_sha[:200]}"
    base_sha = base_sha.strip()

    # ★ RE-ASSESSED AGAINST THE BASE WE ARE ACTUALLY MERGING INTO, not the
    # one the pass started with. origin/main may have moved since, and a
    # diff against a stale base is a diff of somebody else's work too.
    fresh = assess(verdict.branch, base_sha)
    if not fresh.releasable:
        return "stop", ("the branch stopped being releasable against the "
                        "current origin/main: " + "; ".join(fresh.refusals))
    verdict.paths = fresh.paths
    verdict.subjects = fresh.subjects
    verdict.bump = fresh.bump
    rp.head = fresh.head

    worktree = release_worktree(base_sha)
    rp.say(f"staged at {worktree} on origin/main {base_sha[:9]}")

    code, out = _git(["merge", "--no-ff", "--no-edit", verdict.branch],
                     cwd=worktree, timeout=300)
    if code != 0:
        _git(["merge", "--abort"], cwd=worktree)
        return "stop", f"the merge does not apply cleanly: {out[:400]}"
    rp.say(f"merged {verdict.branch} (no-ff)")

    # ★ THE NUMBER IS TAKEN AS LATE AS POSSIBLE, right here, after the
    # fetch and the merge -- because VERSION is part of the commit, so a
    # number is reserved when the commit is WRITTEN and not when it is
    # pushed. Two sessions three minutes apart both took 2.106.0 on
    # 2026-11-04 exactly this way.
    changelog_before = (worktree / "CHANGELOG.md").read_text()
    current = (worktree / "VERSION").read_text().strip()
    version = release_gate.next_version(current, verdict.bump or "patch")
    # ★ RENUMBER PAST A TAKEN NUMBER, which is what the hook tells a human
    # to do in the same situation ("RENUMBER: bump VERSION, move the
    # CHANGELOG entry to the new number"). Re-fetching instead would ask
    # the same question again and get the same answer: origin/main's
    # VERSION is not what makes a number taken -- the TAG is, and a tag can
    # exist for a number no commit on main carries (a release pushed from
    # another checkout, say). Bounded, because a loop that renumbers
    # forever is not a renumber.
    for _ in range(MAX_RENUMBERS):
        code, taken = _git(["ls-remote", "--tags", "origin",
                            f"refs/tags/v{version}"], timeout=300)
        if code != 0:
            return "stop", f"cannot read origin's tags: {taken[:200]}"
        if not taken.strip():
            break
        nxt = release_gate.next_version(version, "patch")
        rp.say(f"v{version} is already tagged on origin; renumbering to "
               f"v{nxt}")
        version = nxt
    else:
        return "stop", (f"gave up looking for a free version number after "
                        f"{MAX_RENUMBERS} tries, last tried v{version}")
    rp.version = version
    rp.say(f"numbering this release v{version} ({verdict.bump}, from {current})")

    basis = None
    approval = verdict.approval
    if approval is not None and getattr(approval, "approval_basis", ""):
        basis = f"the standing rule {approval.approval_basis}"
    entry = changelog_entry(version, verdict.bump or "patch",
                            verdict.subjects, branch=verdict.branch,
                            basis=basis)
    changelog_after, error = insert_entry(changelog_before, entry)
    if error:
        return "stop", error
    # The proof, before anything is written: removing the new entry must
    # reproduce the file byte for byte.
    if changelog_after.replace(entry, "", 1) != changelog_before:
        return "stop", ("the CHANGELOG insertion does not reproduce the "
                        "original when removed; refusing to rewrite history")
    rp.say("CHANGELOG proof: removing the new entry reproduces origin/main's "
           "file byte for byte")

    if not confirm:
        rp.say(f"DRY RUN -- would commit v{version}, run the suite, push "
               f"through the hook and tag. Nothing written.")
        _git(["reset", "--hard", base_sha], cwd=worktree)
        return "dry-run", f"v{version} from {verdict.branch}"

    (worktree / "VERSION").write_text(f"{version}\n")
    (worktree / "CHANGELOG.md").write_text(changelog_after)
    subject = re.sub(r"\s*\(v\d+\.\d+\.\d+\)\s*$", "",
                     (verdict.subjects[0] if verdict.subjects else
                      f"chore: release {verdict.branch}").strip())
    code, out = _git(
        ["commit", "VERSION", "CHANGELOG.md",
         "-m", f"{subject} (v{version})",
         "-m", f"Released automatically from {verdict.branch} by "
               f"scripts/autodev/release_runner.py. Authority: "
               f"{basis or 'a per-item operator approval'}. The diff was "
               f"checked against release_gate.py; the full suite ran on this "
               f"merged tree before the push.",
         "-m", "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"],
        cwd=worktree, timeout=300)
    if code != 0:
        return "stop", f"cannot write the release commit: {out[:300]}"
    code, head = _git(["rev-parse", "HEAD"], cwd=worktree)
    rp.head = head.strip()
    rp.say(f"release commit {rp.head[:9]} v{version}")

    # --- the checks, in the order CLAUDE.md names them -------------------
    ok, summary = _suite(worktree)
    rp.say(f"suite: {summary}")
    if not ok:
        return "stop", f"the suite is not green on the merged tree: {summary}"
    ok, detail = _compileall(worktree)
    rp.say(f"compileall: {detail}")
    if not ok:
        return "stop", f"compileall failed: {detail}"
    ok, detail = _integrity_check()
    rp.say(f"integrity_check: {detail}")
    if not ok:
        return "stop", f"the database integrity check failed: {detail}"
    code, out = _git(["diff", "--check", base_sha, "HEAD"], cwd=worktree)
    rp.say(f"diff --check: {'clean' if code == 0 else out[:200]}")
    if code != 0:
        return "stop", f"git diff --check found whitespace errors: {out[:300]}"
    python = worktree / ".venv" / "bin" / "python"
    proc = subprocess.run([str(python), "changelog_guard.py", "--check-head"],
                          cwd=str(worktree), capture_output=True, text=True,
                          timeout=300)
    rp.say(f"changelog_guard: {(proc.stdout or proc.stderr or '').strip()[:200]}")
    if proc.returncode != 0:
        return "stop", f"changelog_guard refused: {(proc.stdout or '')[:300]}"

    # The tag exists BEFORE the push, so the gap between the two pushes is
    # one failed command wide and the next pass can finish it.
    code, out = _git(["tag", "-a", f"v{version}", "-m",
                      f"CardFoundry v{version}", rp.head], cwd=worktree)
    if code != 0:
        return "stop", f"cannot create the tag v{version}: {out[:300]}"
    rp.tag = f"v{version}"
    rp.say(f"tagged {rp.head[:9]} as v{version} locally")

    outcome, detail = _push(worktree, version, rp, sleeper=sleeper)
    if outcome == "non-fast-forward":
        _git(["tag", "-d", f"v{version}"], cwd=worktree)
        return "restart", f"somebody else pushed first: {detail}"
    if outcome != "ok":
        _git(["tag", "-d", f"v{version}"], cwd=worktree)
        return "stop", f"the push did not land ({outcome}): {detail}"

    # --- verify, rather than believe -------------------------------------
    code, remote_main = _git(["ls-remote", "origin", "refs/heads/main"],
                             timeout=300)
    landed = remote_main.split()[0] if code == 0 and remote_main.strip() else ""
    rp.say(f"origin/main is now {landed[:9]} "
           f"({'matches' if landed == rp.head else 'DOES NOT MATCH'} the "
           f"release commit)")
    code, remote_tag = _git(["ls-remote", "--tags", "origin",
                             f"refs/tags/v{version}"], timeout=300)
    rp.say(f"remote tag v{version}: "
           f"{remote_tag.split()[0][:9] if remote_tag.strip() else 'ABSENT'}")

    live, how = live_version(sleeper=sleeper)
    rp.live = live
    rp.say(f"live footer: {live or 'unreadable'} ({how})")
    state.audit("release_verified", version=version, head=rp.head,
                origin_main=landed, live=live)
    if live != version:
        return "live-mismatch", (
            f"main and the tag are pushed, but the live footer says "
            f"{live or 'nothing readable'} rather than {version}. NOT "
            f"re-pushed and NOT rolled back -- that is the operator's call.")
    return "ok", f"v{version} live"


def _fast_forward_checkout(rp: ReleasePass) -> None:
    """Fast-forward the real checkout's main, if that is safe to do.

    ★ WHY TOUCH IT AT ALL. The next inbox ticket branches off LOCAL main
    (coordinator's base_branch), so leaving it behind after every release
    means every future unattended build starts from older and older code.
    ★ AND WHY ONLY LIKE THIS. --ff-only on a clean tree that is already on
    main cannot lose work and cannot merge anything: it is exactly `git
    pull` with the fast-forward the operator would do themselves. A dirty
    tree or any other branch is somebody else working, and this leaves it
    alone and says so.
    """
    # ★ TRACKED CHANGES ONLY. An untracked file is somebody's scratch
    # export or a build artifact, and a fast-forward does not touch one --
    # refusing on untracked files would mean the checkout almost never got
    # fast-forwarded, which is the whole point of doing this. If a
    # fast-forward genuinely WOULD overwrite an untracked file, git refuses
    # it itself and the refusal is reported below. Measured: the linked
    # .venv symlink alone was enough to make the all-files check skip every
    # time.
    code, out = _git(["status", "--porcelain", "--untracked-files=no"])
    if code != 0 or out.strip():
        rp.say("the real checkout has uncommitted changes; leaving its main "
               "behind origin/main (it needs a `git pull`)")
        return
    code, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    if code != 0 or branch.strip() != "main":
        rp.say(f"the real checkout is on {branch.strip() or '?'}, not main; "
               "leaving it alone")
        return
    code, out = _git(["merge", "--ff-only", "origin/main"])
    rp.say(f"fast-forwarded the real checkout's main: "
           f"{'ok' if code == 0 else out[:200]}")


def run_once(*, confirm: bool = False, sleeper=time.sleep,
             now: datetime | None = None) -> tuple[int, ReleasePass]:
    """One pass. Returns (exit code, report)."""
    rp = ReleasePass(confirmed=confirm)

    try:
        cfg = coordinator.config()
    except coordinator.Halt as exc:
        say(f"refusing: {exc}")
        rp.detail = str(exc)
        return 3, rp

    if not cfg.get("releases_enabled"):
        rp.enabled = False
        say("releases_enabled is false; doing nothing.")
        return 0, rp
    if state.stopped():
        say("STOP is set; doing nothing. "
            "`coordinator.py resume --clear-stop` to clear it.")
        return 0, rp
    if state.paused():
        say("PAUSE is set; doing nothing. `coordinator.py resume` to clear it.")
        return 0, rp
    if quiet_now(now):
        rp.quiet = True
        say(f"quiet hours ({QUIET_START_HOUR:02d}:00-{QUIET_END_HOUR:02d}:00 "
            f"local); doing nothing.")
        return 0, rp
    done_today = releases_today()
    if len(done_today) >= MAX_RELEASES_PER_DAY:
        rp.detail = (f"{len(done_today)} releases already today, the cap is "
                     f"{MAX_RELEASES_PER_DAY}")
        say(f"{rp.detail}; doing nothing.")
        return 0, rp

    inbox.ensure_mailbox()
    try:
        with state.single_owner_lock():
            return _work(cfg, rp, confirm=confirm, sleeper=sleeper)
    except state.CoordinatorBusy as exc:
        # The ordinary case when a build is in flight, not an error.
        say(f"another owner holds the lock; doing nothing. ({exc})")
        return 0, rp


def _work(cfg: dict, rp: ReleasePass, *, confirm: bool,
          sleeper) -> tuple[int, ReleasePass]:
    state.audit("release_pass_started", confirm=confirm)

    if finish_missing_tag(confirm, rp):
        rp.detail = "finished an interrupted release by pushing its tag only"
        inbox.file_report(
            "release runner finished a missing tag", "done",
            "A previous release had pushed main and not its tag -- the one "
            "interruption this runner resumes. The tag is now on origin and "
            "nothing else was done.\n\n"
            + "\n".join(f"- {line}" for line in rp.steps) + "\n")
        return 0, rp

    code, out = _git(["fetch", "origin", "--tags"], timeout=300)
    if code != 0:
        rp.detail = f"cannot fetch origin: {out[:300]}"
        say(f"refusing: {rp.detail}")
        return 3, rp

    assessed = candidates()
    ready = [v for v in assessed if v.releasable]
    rp.waiting = [v.as_dict() for v in assessed if not v.releasable]
    say(f"{len(assessed)} branch(es) seen, {len(ready)} releasable, "
        f"{len(rp.waiting)} waiting for the operator")

    digest = _waiting_digest(assessed)
    if not ready:
        # Named, never silently skipped -- but named ONCE per situation,
        # not once per pass. See _waiting_changed.
        if rp.waiting and _waiting_changed(digest):
            rp.report = str(inbox.file_report(
                "release runner: nothing releasable", "blocked",
                "Nothing was released and nothing was changed. Every branch "
                "this runner can see, and why each one is waiting for the "
                "operator rather than going out automatically:\n\n"
                + _waiting_block(assessed)
                + "\nThese are left exactly as they are. Releasing any of "
                  "them is an ordinary release the operator asks for.\n\n"
                  "This is reported when the set above CHANGES, not every "
                  "pass -- a released branch stays in the repository and is "
                  "refused from then on for having nothing left to release, "
                  "which would otherwise fill this mailbox with one "
                  "identical report an hour. "
                  "`install_release --status` prints the current set at any "
                  "time.\n"))
            _remember_waiting(digest)
        else:
            say("nothing releasable, and the same as last time; no report.")
        say("idle: nothing releasable.")
        return 0, rp
    _remember_waiting(digest)

    verdict = ready[0]
    rp.released = verdict.branch
    rp.say(f"releasing {verdict.branch} "
           f"({len(verdict.paths)} file(s), bump {verdict.bump}, "
           f"report {verdict.report.name if verdict.report else '?'})")
    state.audit("release_started", branch=verdict.branch, task=verdict.task_id,
                paths=verdict.paths, bump=verdict.bump,
                approval=verdict.approval.as_evidence()
                if verdict.approval is not None
                and verdict.approval.is_approved else None)

    outcome, detail = "restart", ""
    for restart in range(MAX_RESTARTS + 1):
        if restart:
            rp.say(f"restart {restart}: {detail}")
        try:
            outcome, detail = _attempt(verdict, rp, confirm, sleeper)
        except coordinator.Halt as exc:
            outcome, detail = "stop", str(exc)
        if outcome != "restart":
            break
    else:
        outcome = "stop"
        detail = f"gave up after {MAX_RESTARTS} restarts: {detail}"

    rp.detail = detail
    if outcome == "dry-run":
        rp.say(f"DRY RUN finished: {detail}")
        return 0, rp
    if outcome == "ok":
        _fast_forward_checkout(rp)
        rp.report = str(_file_release_report(verdict, rp, "done", assessed))
        state.audit("release_finished", branch=verdict.branch,
                    version=rp.version, head=rp.head, live=rp.live)
        rp.say(f"released {verdict.branch} as v{rp.version}, live confirmed")
        return 0, rp

    status = "blocked" if outcome != "refused" else "stopped-at-hard-stop"
    rp.report = str(_file_release_report(verdict, rp, status, assessed))
    state.audit("release_blocked", branch=verdict.branch, outcome=outcome,
                detail=detail[:500], version=rp.version)
    rp.say(f"STOPPED ({outcome}): {detail}")
    return 3, rp


def _waiting_digest(assessed: list[release_gate.Verdict]) -> str:
    blob = json.dumps(
        [[v.branch, sorted(v.refusals)] for v in assessed if not v.releasable],
        sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _waiting_changed(digest: str) -> bool:
    """Has the set of waiting branches changed since the last report?

    ★ WHY THIS EXISTS, AND IT IS NOT A NICETY. A released branch stays in
    the repository, and from the next pass onwards it is refused with
    "changes nothing against origin/main" -- correctly. Without this the
    runner would file a "nothing releasable" report every single pass:
    sixteen a day, each one identical, into the mailbox whose whole
    purpose is that reports are not lost. A report nobody can face
    reading is a report that is lost.

    So the report is filed when the situation CHANGES -- a new branch, a
    new refusal, a refusal that went away -- and not when it is the same
    as last time. Nothing is suppressed: a new reason is reported on the
    very next pass, and `install_release --status` prints the current set
    on demand whatever has been reported.
    """
    path = state.home() / WAITING_NAME
    try:
        previous = json.loads(path.read_text()).get("digest")
    except (OSError, ValueError):
        previous = None
    return previous != digest


def _remember_waiting(digest: str) -> None:
    path = state.ensure_home() / WAITING_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"digest": digest,
                               "at": datetime.now().isoformat()}, indent=2))
    os.replace(tmp, path)


def _waiting_block(assessed: list[release_gate.Verdict]) -> str:
    lines = []
    for verdict in assessed:
        if verdict.releasable:
            continue
        lines.append(f"- {verdict.branch}")
        for reason in verdict.refusals:
            lines.append(f"    - {reason}")
    return "\n".join(lines) + "\n" if lines else "(none)\n"


def _file_release_report(verdict: release_gate.Verdict, rp: ReleasePass,
                         status: str, assessed: list[release_gate.Verdict]):
    """The mailbox report, in CLAUDE.md's shape, written by this program.

    Evidence rather than narrative: what was released, every number a
    reader can check, every step in order, and every branch that was left
    alone with the reason. One file per report, never overwritten, the
    same as every other report in that mailbox.
    """
    authority = (getattr(verdict.approval, "approval_basis", "")
                 or "a per-item operator approval")
    head = f"""Released automatically by scripts/autodev/release_runner.py.
No agent was involved: this is the fixed release sequence as Python, and
every number below can be checked against origin.

    branch      {verdict.branch}
    task        {verdict.task_id}
    report      {verdict.report.name if verdict.report else '(none)'}
    authority   {authority}
    version     {rp.version or '(none assigned)'}
    commit      {rp.head or '(none)'}
    tag         {rp.tag or '(none)'}
    live footer {rp.live or '(not confirmed)'}
    files       {', '.join(verdict.paths) or '(none)'}

STEPS, IN ORDER:

"""
    body = head + "\n".join(f"    {line}" for line in rp.steps) + "\n"
    if rp.detail:
        body += f"\nOUTCOME: {rp.detail}\n"
    if status != "done":
        body += ("\nNOTHING WAS LEFT HALF-DONE ON PURPOSE: the release "
                 "stopped at the step above. main is only ever pushed after "
                 "the whole suite is green, and the tag is chained on that "
                 "push, so a stop before the push leaves the repository "
                 "exactly as it was.\n")
    body += "\nOTHER BRANCHES, LEFT ALONE:\n\n" + _waiting_block(assessed)
    body += ("\nVERIFY:\n\n"
             f"    git fetch origin --tags\n"
             f"    git log --oneline -3 origin/main\n"
             f"    git ls-remote --tags origin refs/tags/{rp.tag or 'vX.Y.Z'}\n"
             "    curl -s -u hook:<service-password> <base-url>/login "
             "| grep 'CardFoundry v'\n")
    return inbox.file_report(
        f"auto release {rp.version or verdict.task_id}", status, body)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--confirm", action="store_true",
        help="actually release (default: a dry run that writes nothing)")
    args = parser.parse_args(argv)
    try:
        code, rp = run_once(confirm=args.confirm)
    except KeyboardInterrupt:
        state.audit("release_interrupted")
        say("interrupted; nothing further started.")
        return 2
    print(json.dumps(rp.as_dict(), indent=2, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
