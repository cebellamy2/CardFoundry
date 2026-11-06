"""What an unattended release may contain: policy.py plus exactly two files.

★ THE CENTRAL DESIGN POINT OF THE AUTO-RELEASE, and it is a catch rather
than a convenience. policy.py forbids `VERSION` and `CHANGELOG.md` by name
under the rule "releases are the operator's" -- and a release MUST touch
both of them. So the release gate cannot be policy.py, and it must not be
a loosened copy of policy.py either: a second denylist would start
disagreeing with the first the first time one of them was edited.

It is therefore policy.py COMPOSED with a two-file exception:

    allowed(path) = (path in {VERSION, CHANGELOG.md})
                    or (policy.allows(path) and not absolutely denied)

written once, here, so there is exactly one place in the repository that
says a release may touch those two files and nothing else.

★ THE DIFF, NOT THE TICKET'S `files` LIST. The ticket says what the work
was ALLOWED to touch; `git diff --name-only` says what it DID touch. Those
are the same number almost always, and the one time they are not is the
time this gate exists for -- a ticket whose scope was wrong, a merge that
dragged in a neighbour, a stray file committed by accident. The ticket's
list is already enforced per tool call by guard.py while the work happens;
this gate is the independent second look at the result.

★ WHAT THIS REFUSES, SAID PLAINLY. A diff touching models.py,
database.py, order_service.py, any pricing/payout/manapool/consignment
service, any scheduled_*.py, `.env`, the database, or the deploy guard
itself (scripts/hooks/pre-push, changelog_guard.py) is refused and left
for the operator. That means an automatic release could not have shipped
v2.110.0's own item 6, which changed the pre-push hook. That is correct:
the guard's own changes stay the operator's, and a runner that could
re-write the guard it pushes through is a runner with no guard.

★ AND IT FAILS CLOSED, like every other gate here. An unreadable diff, a
branch with no commits, an absolute path, a path containing `..`, a
missing or unapproved note -- every one of them refuses. "No opinion"
has to mean "no" in a program nobody is watching.

★ AUTHORITY IS A SEPARATE QUESTION AND BOTH ANSWERS ARE REQUIRED. This
module answers "may a release contain this diff". approvals.py answers
"did anyone agree to this work" -- a closed set of standing bases or a
per-item operator OK, failing closed on an unknown basis. Neither is
sufficient: an approved ticket whose diff touches pricing is refused, and
a harmless-looking diff with no approval note is refused too.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import inbox, policy, state

logger = logging.getLogger("cardfoundry")

# The ONLY two paths a release may touch that policy.py forbids, and the
# reason each one is here. Both are forbidden to AutoDev for the right
# reason -- a builder has no business numbering a release -- and both are
# unavoidable in a release commit, which is the whole of the exception.
RELEASE_ONLY_PATHS: dict[str, str] = {
    "VERSION": "the release's own number",
    "CHANGELOG.md": "the release's own entry",
}


def _normalise(path: str) -> str:
    cleaned = str(path).strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def path_refusal(path: str) -> str | None:
    """Why this path may not be in an unattended release, or None.

    The composition, in the order that matters: the two-file exception
    first (so the release's own number and entry pass), then BOTH
    denylists -- policy.py's, which is what keeps an agent away from
    money, the schema and the guard, and inbox.ABSOLUTE_DENY, which adds
    the repository's own plumbing, the mailbox and the virtualenv.
    """
    cleaned = _normalise(path)
    if not cleaned:
        return "an empty path is not a path"
    if cleaned.startswith("/") or ".." in cleaned.split("/"):
        return f"{cleaned} is not a repo-relative path"
    if cleaned in RELEASE_ONLY_PATHS:
        return None
    reason = policy.forbidden_reason(cleaned)
    if reason:
        return reason
    # inbox.py's own absolute denylist, which policy.py does not cover in
    # full: claude/, .git/ and .venv/ are not in policy's lists because a
    # ticket could never name them usefully, and a diff must not either.
    return inbox.denied(cleaned)


def allowed(path: str) -> bool:
    return path_refusal(path) is None


def _git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd or state.REPO_ROOT),
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def diff_paths(base: str, branch: str, *,
               cwd: Path | None = None) -> tuple[list[str], str | None]:
    """Every path this branch changes against `base`. (paths, error).

    ★ THREE DOTS, NOT TWO. `base...branch` diffs the branch against the
    MERGE BASE, so a base that has moved on since the branch was cut does
    not make every file somebody else touched look like this branch's
    work. Two dots would widen the gate's input with other people's
    commits, which is both wrong and, on a shared main, noisy enough to
    refuse every release.
    """
    code, out = _git(["diff", "--name-only", f"{base}...{branch}"], cwd=cwd)
    if code != 0:
        return [], f"cannot diff {base}...{branch}: {out.strip()[:300]}"
    paths = [line.strip() for line in out.splitlines() if line.strip()]
    return paths, None


@dataclass
class Verdict:
    """One branch, and every reason it may not be released unattended."""

    branch: str
    task_id: str = ""
    base: str = ""
    head: str = ""
    paths: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    report: Path | None = None
    status: str | None = None
    approval: object | None = None
    bump: str | None = None
    subjects: list[str] = field(default_factory=list)

    @property
    def releasable(self) -> bool:
        return not self.refusals

    def as_dict(self) -> dict:
        return {
            "branch": self.branch, "task": self.task_id, "base": self.base,
            "head": self.head, "paths": self.paths,
            "refusals": self.refusals, "status": self.status,
            "report": str(self.report) if self.report else None,
            "bump": self.bump,
        }


def assess_paths(paths: list[str]) -> list[str]:
    """Every path refusal in this diff, not just the first one.

    All of them together, the same stance as policy.evaluate and
    approvals.evaluate: one refusal should tell a reader everything that
    is wrong rather than making them find the next problem next time.
    """
    refusals = []
    for path in paths:
        reason = path_refusal(path)
        if reason:
            refusals.append(reason)
    return refusals


# `feat` is a minor, everything else is a patch, and a breaking change is
# NEVER automatic. The same rule CHANGELOG.md's own preamble records, and
# the same rule every release in this repo has followed.
FEAT_SUBJECT = re.compile(r"^(?:feat)(?:\([^)]*\))?!?:", re.IGNORECASE)
BREAKING_SUBJECT = re.compile(
    r"^[a-z]+(?:\([^)]*\))?!:|BREAKING[ -]CHANGE", re.IGNORECASE)


def bump_for(subjects: list[str]) -> tuple[str | None, str | None]:
    """(bump, refusal) from the branch's own commit subjects.

    ★ A MAJOR IS NEVER AUTOMATIC. A breaking change is the one bump that
    says something to every reader of the version number, and deciding it
    from a `!` in a commit subject written by an agent is not a decision
    this runner gets to make. It refuses and leaves the branch for the
    operator, who releases it by hand as they always have.
    """
    if not subjects:
        return None, "the branch has no commits of its own to release"
    for subject in subjects:
        if BREAKING_SUBJECT.search(subject):
            return None, (
                f"a breaking change is never released unattended: "
                f"{subject.strip()[:120]!r}"
            )
    if any(FEAT_SUBJECT.match(subject.strip()) for subject in subjects):
        return "minor", None
    return "patch", None


def next_version(current: str, bump: str) -> str:
    major, minor, patch = (int(part) for part in current.strip().split("."))
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"an unattended release never bumps {bump!r}")
