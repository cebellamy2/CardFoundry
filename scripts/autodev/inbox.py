"""The ticket inbox: work that arrives as a FILE instead of a paste.

★ WHY THIS EXISTS. Every ticket for the Code CLI session has arrived as
text pasted by the operator into a chat window. That step is not merely
slow, it is LOSSY: in two days it lost one prompt to truncation and
re-sent two stale ones, which is why tickets carry an "END OF PROMPT"
sentinel at all. A JSON file either parses or it does not, so moving the
channel to a file removes that whole class of failure -- a half-written
ticket fails at the parse instead of being built on.

★ WHAT THIS IS NOT: AutoDev's queue. They look alike on purpose -- the
same ticket shape, the same `approvals.py` gate, the same flock -- and
they are deliberately NOT the same fence:

    claude/autodev/queue/            AutoDev. policy.py decides what the
                                     work may touch, and it forbids by
                                     name the deploy guard, the schema
                                     and the money services.
    claude/mailbox/to-code-autodev/  this inbox. The ticket's OWN `files`
                                     list decides, minus the absolute
                                     denylist below.

The difference is the point. Of the three items shipped in v2.110.0, two
named paths `policy.py` forbids outright (`scripts/hooks/pre-push` and
`order_service.py`), so AutoDev could not have taken two thirds of an
approved batch. Widening `policy.py` to fix that would widen the fence
around the unattended five-minute builder as well, which is the one
thing not to do. So this inbox has its own, different fence, and
`policy.py` is untouched.

★ THE FENCE, SAID PLAINLY. A ticket here may name any path it declares,
including ones AutoDev may never touch -- and NOTHING it does not
declare. That is stricter than policy.py in one direction and wider in
the other. On top of it sits ABSOLUTE_DENY, which no ticket may name
however it is approved, because no unattended run has any business in
credentials, the inventory database, backups, imports or the audit
trail.

★ AND IT CANNOT RELEASE. The runner that works these tickets cannot
push, tag or touch Railway: a finished ticket is a BRANCH and a report.
Releasing still follows CLAUDE.md, which means the operator's OK. That
is why this module needs no release gate: there is no release to gate.

★ OLDEST FIRST, AND THE ORDER IS IN THE NAME. A ticket id is
`YYYYMMDD-HHMM-<slug>`, so sorting the ids lexicographically sorts them
chronologically. Not mtime: a file copied, restored from a backup or
touched by an editor gets a new mtime, and then "oldest" silently means
something else.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import approvals, policy, state

logger = logging.getLogger("cardfoundry")

MAILBOX_ENV = "CARDFOUNDRY_MAILBOX_HOME"

INBOX_NAME = "to-code-autodev"
DONE_NAME = "done"
REJECTED_NAME = "rejected"
REPORTS_NAME = "from-code-autodev"

# ★ THE ONE PART THE TICKET'S OWN `files` LIST MAY NOT OVERRIDE. Every
# other path question is answered by the ticket; these are answered here,
# because an unattended run has no business in any of them whatever a
# note says. Matched against the whole repo-relative path, like
# policy.FORBIDDEN_PATTERNS, and deliberately broad: a false refusal
# costs one ticket and is visible, a false allow costs the operator's
# data and is not.
ABSOLUTE_DENY: tuple[tuple[str, str], ...] = (
    (r"(?i)^\.env", "credentials are never touched unattended"),
    (r"(?i)\.db$|\.sqlite3?$", "the inventory database is not source code"),
    (r"(?i)^backups?/", "backups are production data"),
    (r"(?i)^imports?/", "operator-provided data"),
    (r"(?i)^audits?/", "the audit trail is append-only"),
    (r"(?i)^claude/", "the mailbox and AutoDev's own state are not a ticket's work"),
    (r"(?i)(^|/)\.git(/|$)", "the repository's own plumbing"),
    # ★ ADDED 2026-11-06, with the venv symlink that made it reachable.
    # Each inbox worktree now has a `.venv` symlink to the main
    # checkout's virtualenv, so a ticket declaring a path under it could
    # edit the environment every session and the operator's own checkout
    # share. Nothing legitimate needs to: `pip` is denied, and a
    # dependency change is a release the operator asks for.
    (r"(?i)(^|/)\.venv(/|$)", "the virtualenv is shared with the main checkout"),
)

# The ticket shape is AutoDev's, imported rather than copied: two lists
# of required fields would drift, and then Cowork would be writing to
# whichever one it last read about.
REQUIRED_FIELDS: tuple[str, ...] = policy.REQUIRED_FIELDS

# `YYYYMMDD-HHMM-<slug>`. Narrower than policy.ID_PATTERN on purpose --
# the timestamp prefix is what makes "oldest first" a property of the
# name rather than of the filesystem.
TICKET_ID = re.compile(r"^(20\d{6})-([0-2]\d[0-5]\d)-([a-z0-9][a-z0-9-]{1,40})$")


def mailbox_root() -> Path:
    """Where the mailbox lives, overridable so tests never touch the real one."""
    override = os.environ.get(MAILBOX_ENV)
    return Path(override) if override else state.REPO_ROOT / "claude" / "mailbox"


def inbox_dir() -> Path:
    return mailbox_root() / INBOX_NAME


def done_dir() -> Path:
    return inbox_dir() / DONE_NAME


def rejected_dir() -> Path:
    return inbox_dir() / REJECTED_NAME


def reports_dir() -> Path:
    return mailbox_root() / REPORTS_NAME


def ensure_mailbox() -> Path:
    root = mailbox_root()
    for path in (inbox_dir(), done_dir(), rejected_dir(), reports_dir()):
        path.mkdir(parents=True, exist_ok=True)
    return root


def atomic_write(path: Path, text: str) -> Path:
    """Write beside, then rename. The whole reason the inbox is files.

    ★ A READER MUST NEVER SEE HALF A TICKET. os.replace is atomic within
    a filesystem, so the ticket appears under its real name complete or
    not at all -- there is no window in which `*.json` matches a file
    still being written. Same mechanism state.State.save already uses,
    for the same reason: a half-written file is worse than a missing one,
    because recovery would believe it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    os.replace(part, path)
    return path


def ticket_digest(data: dict) -> str:
    """The same digest coordinator.ticket_digest computes, over the ticket
    body, so one approval note works for either channel."""
    blob = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def write_ticket(data: dict, *, root: Path | None = None) -> Path:
    """Write one ticket atomically, named by its own id. Returns the path."""
    target = (root or inbox_dir()) / f"{data.get('id')}.json"
    return atomic_write(target, json.dumps(data, indent=2, sort_keys=True))


def _denied(path: str) -> str | None:
    cleaned = path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    for pattern, reason in ABSOLUTE_DENY:
        if re.search(pattern, cleaned):
            return f"{cleaned} may never be touched unattended: {reason}"
    return None


@dataclass
class Candidate:
    """One inbox file and everything standing between it and a run."""

    path: Path
    data: dict
    refusals: list[str] = field(default_factory=list)
    approval: approvals.Approval | None = None

    @property
    def id(self) -> str:
        return str(self.data.get("id") or self.path.stem)

    @property
    def files(self) -> list[str]:
        return [str(value) for value in (self.data.get("files") or [])]

    @property
    def tests(self) -> list[str]:
        return [str(value) for value in (self.data.get("tests") or [])]

    @property
    def report_only(self) -> bool:
        # `is True`, not truthiness: validate() refuses anything but a real
        # boolean, so a string "false" can never quietly read as yes.
        return self.data.get("report_only") is True

    @property
    def digest(self) -> str | None:
        return ticket_digest(self.data) if self.data else None

    @property
    def is_runnable(self) -> bool:
        return not self.refusals


def validate(data: dict, *, path: Path) -> list[str]:
    """Every reason this ticket may not run, not just the first one.

    All of them at once, like policy.evaluate: one refusal should tell
    Cowork everything to fix rather than making it discover the next
    problem on the next firing -- and a rejected ticket is moved aside,
    so there is no next firing to discover it on.
    """
    refusals: list[str] = []
    # `files` is required by the shared shape, and a report-only ticket is
    # the one kind that must NOT carry any -- so it is exempted here,
    # rather than by editing policy.REQUIRED_FIELDS, which is AutoDev's
    # fence too and must not move for this channel's sake.
    report_only_ticket = data.get("report_only") is True
    for field_name in REQUIRED_FIELDS:
        if field_name == "files" and report_only_ticket:
            continue
        if not data.get(field_name):
            refusals.append(f"missing required field '{field_name}'")

    ticket_id = str(data.get("id") or "")
    if not TICKET_ID.match(ticket_id):
        refusals.append(
            f"id {ticket_id!r} must be YYYYMMDD-HHMM-<slug>, lower-case "
            "kebab-case -- the timestamp is what orders the inbox"
        )
    elif ticket_id != path.stem:
        refusals.append(
            f"id {ticket_id!r} does not match the filename {path.stem!r}"
        )

    if str(data.get("autonomous") or "").strip().lower() != "yes":
        refusals.append(
            "autonomous is not 'yes' -- only Cowork may set it, and only "
            "under the documented policy"
        )
    if not isinstance(data.get("acceptance"), list) or not data.get("acceptance"):
        refusals.append("acceptance must be a non-empty list of criteria")

    files = [str(value) for value in (data.get("files") or [])]
    tests = [str(value) for value in (data.get("tests") or [])]
    report_only = data.get("report_only", False)
    if not isinstance(report_only, bool):
        refusals.append(
            f"report_only must be true or false, not {report_only!r}"
        )
    elif report_only:
        # ★ A REPORT-ONLY TICKET IS SCOPED TO NOTHING, and that is what
        # makes it safe rather than a new permission. guard.py already
        # refuses every write when no paths are declared, so an empty
        # scope IS "read, run, report". Declaring files as well would make
        # the flag a lie about what the session can do -- so it is one or
        # the other. Tests are refused too, and not as tidiness: the guard
        # treats a declared test path as writable (_ticket_paths is files
        # AND tests), so a report-only ticket naming one could edit it.
        if files:
            refusals.append(
                "a report_only ticket may not declare files -- it is one or "
                "the other: a report, or a scoped change"
            )
        if tests:
            refusals.append(
                "a report_only ticket may not declare tests -- the inbox "
                "guard treats a declared test path as writable, so it would "
                "no longer be report-only; the session may run the whole "
                "suite without naming any"
            )
    elif not files:
        refusals.append(
            "files must name the scope the work may touch -- this inbox has "
            "no wider default"
        )
    for candidate in files + tests:
        if candidate.startswith("/") or ".." in candidate:
            refusals.append(f"{candidate} is not a repo-relative path")
            continue
        denial = _denied(candidate)
        if denial:
            refusals.append(denial)
    return refusals


def load(path: Path) -> Candidate:
    """Read and validate one inbox file. A broken file is a refusal.

    ★ THIS IS WHERE "END OF PROMPT" WENT. A truncated paste had to be
    spotted by a human reading the last line; a truncated JSON file
    cannot parse, so it refuses here and is reported, never guessed at.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # Catch-and-continue, so it logs: an unreadable ticket must not
        # take the runner down, and must not pass either.
        logger.warning("autodev inbox: cannot read ticket %s: %s", path, exc)
        return Candidate(path=path, data={},
                         refusals=[f"unreadable ticket: {exc}"])
    if not isinstance(data, dict):
        return Candidate(path=path, data={},
                         refusals=["ticket is not a JSON object"])
    return Candidate(path=path, data=data, refusals=validate(data, path=path))


def assess(path: Path) -> Candidate:
    """The ticket's own validity AND whether anything authorises it.

    approvals.py UNCHANGED, and that is deliberate: the authority gate is
    the same closed set of standing bases, the same required fields and
    the same digest check that AutoDev runs. One note shape, one gate, so
    a reader of either channel reads the same evidence. The note lives in
    claude/autodev/approvals/<id>.json whichever channel the ticket came
    from.
    """
    candidate = load(path)
    if not candidate.data:
        return candidate
    approval = approvals.load(candidate.id, ticket_digest=candidate.digest)
    candidate.approval = approval
    candidate.refusals = list(approval.refusals) + candidate.refusals
    return candidate


def pending() -> list[Path]:
    """Every inbox ticket, OLDEST FIRST. Sorted by name, which is by time."""
    root = inbox_dir()
    if not root.exists():
        return []
    return sorted((path for path in root.glob("*.json")), key=lambda p: p.name)


def next_runnable() -> tuple[Candidate | None, list[Candidate]]:
    """The oldest ticket that may run, plus every one refused before it.

    Nothing is skipped silently: an inbox full of refused tickets has to
    look different from an empty one, which is the same stance serve
    takes -- and the refused ones are MOVED, so the difference is visible
    on the filesystem too.
    """
    refused: list[Candidate] = []
    for path in pending():
        candidate = assess(path)
        if candidate.is_runnable:
            return candidate, refused
        refused.append(candidate)
    return None, refused


def reject(candidate: Candidate) -> Path:
    """Move a refused ticket aside, with its reasons beside it.

    ★ MOVED, NOT DELETED AND NOT LEFT. Left in place, the same bad ticket
    is re-read and re-reported every firing -- the thousand-files-a-day
    shape serve.already_reported was written to fix. Deleted, the
    evidence of what arrived is gone. So it goes to rejected/ with a
    plain-words refusal file, and the ticket itself is byte-identical to
    what was written.
    """
    ensure_mailbox()
    target = _free_name(rejected_dir() / candidate.path.name)
    reasons = "\n".join(f"- {reason}" for reason in candidate.refusals)
    atomic_write(
        target.with_name(target.stem + ".refusal.txt"),
        f"ticket:    {candidate.path.name}\n"
        f"rejected:  {datetime.now().isoformat(timespec='seconds')}\n"
        f"digest:    {candidate.digest}\n\n"
        f"This ticket was not run. Every reason, not just the first:\n\n"
        f"{reasons}\n\n"
        "Nothing was guessed at and nothing was built. Fix the ticket and "
        "write a new one; this file is a record, not an instruction.\n",
    )
    os.replace(candidate.path, target)
    state.audit("inbox_ticket_rejected", task=candidate.id,
                path=str(target), reasons=candidate.refusals)
    logger.info("autodev inbox: rejected %s: %s",
                candidate.id, "; ".join(candidate.refusals))
    return target


def complete(candidate: Candidate, outcome: str) -> Path:
    """Move a finished ticket into done/. Returns the path it now has."""
    ensure_mailbox()
    target = _free_name(done_dir() / candidate.path.name)
    os.replace(candidate.path, target)
    state.audit("inbox_ticket_done", task=candidate.id,
                outcome=outcome, path=str(target))
    return target


def _free_name(path: Path) -> Path:
    """A name nothing holds yet. NEVER overwrite a ticket or a report.

    The same counter the outbox uses, and for the same reason it needed
    one: two files that share a name means the second silently replaces
    the first, and the record whose whole job is not losing things loses
    one.
    """
    if not path.exists():
        return path
    for suffix in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{suffix}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free name beside {path}")


def report_path(topic: str, *, now: datetime | None = None) -> Path:
    """Where one report file goes: the Eastern-local stamp CLAUDE.md names.

    datetime.now() is local time, and the operator's machine is Eastern,
    which is exactly what the convention asks for -- no timezone
    arithmetic that could be wrong in one half of the year.
    """
    stamp = now or datetime.now()
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-") or "report"
    return _free_name(reports_dir() / f"{stamp:%Y%m%d-%H%M}-{slug[:60]}.md")


def file_report(topic: str, status: str, body: str, *,
                now: datetime | None = None) -> Path:
    """Write one report into the outbound mailbox, never overwriting one.

    The header and the STATUS line are CLAUDE.md's ("Reports mailbox");
    the body is whatever the session wrote, word for word, because the
    block IS the report and a runner that summarised it would be
    rewriting the thing the operator asked to stop losing.
    """
    ensure_mailbox()
    path = report_path(topic, now=now)
    header = f"◀ FROM CODE-AUTODEV → CHAT-AUTODEV · {topic}\nSTATUS: {status}\n\n"
    atomic_write(path, header + body.rstrip() + "\n")
    state.audit("inbox_report_filed", path=str(path), status=status, topic=topic)
    return path


def main(argv: list[str] | None = None) -> int:
    """READ-ONLY status, for a human or for Cowork: what is in the inbox.

    Writes nothing: no ticket is moved, no report is filed, no audit row
    is added. It is the counterpart to validate_ticket --inbox, which
    answers the same question for one ticket in detail.
    """
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = []
    for path in pending():
        candidate = assess(path)
        rows.append({
            "ticket": path.name,
            "id": candidate.id,
            "digest": candidate.digest,
            "report_only": candidate.report_only,
            "runnable": candidate.is_runnable,
            "refusals": candidate.refusals,
        })
    if args.json:
        print(json.dumps({"inbox": str(inbox_dir()), "tickets": rows},
                         indent=2, sort_keys=True))
        return 0
    print(f"inbox: {inbox_dir()}")
    if not rows:
        print("  (empty)")
        return 0
    for row in rows:
        print(f"\n  {row['ticket']}")
        print(f"    digest:      {row['digest']}")
        print(f"    report_only: {row['report_only']}")
        print(f"    runnable:    {row['runnable']}")
        for reason in row["refusals"]:
            print(f"      - {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
