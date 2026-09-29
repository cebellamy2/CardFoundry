"""The durable in-repo record guard.

WHY THIS EXISTS. Between v1.98.0 and v1.184.0, 137 versions shipped to main
with no CHANGELOG entry, and 160 versions across a wider span never got a git
tag. Nothing caught it, for months, because nothing was checking: AGENTS.md
said to write the entry and tag the commit, and that was the whole mechanism.
CHANGELOG.md is what a fresh session reads to learn what happened, so when it
goes unwritten the durable record goes with it.

WHY THE CHECK LIVES IN THE TEST SUITE AND NOT ONLY IN THE HOOK. A pre-push
hook can be skipped with --no-verify, and this repo's own hook advertises that
bypass for genuine emergencies. A test cannot be skipped that way, and
AGENTS.md already requires the full suite before shipping. The hook (which
calls straight into this module, so the two can never disagree) catches the
mistake at the moment it is made; the test catches it even when the hook was
bypassed.

THE RATCHET. RATCHET_FLOOR is the oldest version this guard vouches for.
Everything at or above it must have an entry. It exists so the guard can be
tightened as history is reconstructed without ever demanding the whole backlog
at once -- and, now that the reconstruction is complete, so that a future gap
cannot reopen silently.
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
VERSION_FILE = REPO_ROOT / "VERSION"

HEADING = re.compile(r"(?m)^## \[(\d+\.\d+\.\d+)\](?:\s*-\s*(\d{4}-\d{2}-\d{2}))?\s*$")

# Every version at or above this must have a CHANGELOG entry. The whole
# history from 1.0.0 is reconstructed, so the floor is the beginning.
RATCHET_FLOOR = "1.0.0"

# Versions that deliberately carry no git tag, and why. Each shipped INSIDE a
# commit that VERSION records as a different release, so no commit is that
# release alone and tagging one would name it as two versions at once.
# Operator decision, 2026-09-29: record the reason, do not tag.
UNTAGGED_BY_DECISION = {
    "1.60.1": "shipped inside ba887ba64, which VERSION records as 1.61.0",
    "1.68.0": "shipped inside c1de5dc9f, which VERSION records as 1.70.0",
    "1.69.0": "shipped inside c1de5dc9f, which VERSION records as 1.70.0",
    "1.71.0": "shipped inside d382a73f9, which VERSION records as 1.72.0",
}


def version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def at_or_above_floor(version: str, floor: str = RATCHET_FLOOR) -> bool:
    return version_key(version) >= version_key(floor)


# --- reading the changelog ------------------------------------------------

def parse_headings(text: str) -> list[tuple[str, str | None]]:
    """[(version, date-or-None)] in the order they appear."""
    return [(m.group(1), m.group(2)) for m in HEADING.finditer(text)]


def changelog_text() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


def current_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


# --- the individual checks, each returning a list of problems -------------

def check_current_version_has_entry(version: str, text: str) -> list[str]:
    versions = {v for v, _ in parse_headings(text)}
    if version not in versions:
        return [
            f"VERSION is {version} but CHANGELOG.md has no '## [{version}]' "
            "heading. Every version shipped to main is a release and needs its "
            "own entry (AGENTS.md, Versioning & Releases)."
        ]
    return []


def check_dates(text: str, today: date | None = None) -> list[str]:
    today = today or date.today()
    problems = []
    for version, raw in parse_headings(text):
        if raw is None:
            problems.append(f"[{version}] has no date on its heading.")
            continue
        try:
            when = date.fromisoformat(raw)
        except ValueError:
            problems.append(f"[{version}] has an unparseable date {raw!r}.")
            continue
        if when > today:
            problems.append(f"[{version}] is dated {raw}, which is in the future.")
    return problems


def check_order_and_duplicates(text: str) -> list[str]:
    versions = [v for v, _ in parse_headings(text)]
    problems = []
    seen = set()
    for v in versions:
        if v in seen:
            problems.append(f"[{v}] appears more than once.")
        seen.add(v)
    expected = sorted(set(versions), key=version_key, reverse=True)
    if [v for v in versions if versions.count(v) == 1] and versions != expected:
        for a, b in zip(versions, versions[1:]):
            if version_key(a) < version_key(b):
                problems.append(
                    f"[{a}] is listed above [{b}]; entries must run newest first."
                )
                break
    return problems


def check_ratchet(shipped: list[str], text: str, floor: str = RATCHET_FLOOR) -> list[str]:
    have = {v for v, _ in parse_headings(text)}
    missing = [v for v in shipped if at_or_above_floor(v, floor) and v not in have]
    if not missing:
        return []
    shown = ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else "")
    return [
        f"{len(missing)} version(s) at or above the {floor} ratchet floor have no "
        f"CHANGELOG entry: {shown}"
    ]


def check_tags(
    shipped: list[str], tags: set[str], floor: str = RATCHET_FLOOR,
    pending: str | None = None,
) -> list[str]:
    """Versions that shipped but carry no tag.

    ``pending`` is the version currently being prepared and is exempt: the
    release order is bump VERSION, write the entry, run the suite, push, THEN
    tag, so the newest version legitimately has no tag while the suite runs.
    Without this the guard would fail on every release it was meant to protect.
    """
    if not tags:
        return []  # No tags fetched at all (shallow clone): nothing to check.
    missing = [
        v for v in shipped
        if at_or_above_floor(v, floor)
        and v not in UNTAGGED_BY_DECISION
        and v != pending
        and f"v{v}" not in tags
    ]
    if not missing:
        return []
    shown = ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else "")
    return [f"{len(missing)} shipped version(s) have no git tag: {shown}"]


# --- git ------------------------------------------------------------------

def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def in_git_repo() -> bool:
    return _git("rev-parse", "--git-dir") is not None


def shipped_versions() -> list[str]:
    """Every value VERSION has ever held, plus every version the CHANGELOG
    names. The union matters: some versions shipped inside a commit that
    VERSION records as a different release, so they exist only in the
    CHANGELOG and would be invisible to a VERSION-history-only walk.
    """
    found = set()
    log = _git("log", "--reverse", "--format=%H", "--", "VERSION")
    for sha in (log or "").split():
        blob = _git("show", f"{sha}:VERSION")
        if blob and re.fullmatch(r"\d+\.\d+\.\d+", blob.strip()):
            found.add(blob.strip())
    found |= {v for v, _ in parse_headings(changelog_text())}
    return sorted(found, key=version_key)


def existing_tags() -> set[str]:
    out = _git("tag")
    if out is None:
        return set()
    return {t for t in out.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)}


def head_changes_version() -> bool:
    out = _git("show", "--name-only", "--format=", "HEAD")
    return bool(out) and "VERSION" in out.split()


# --- entry points ---------------------------------------------------------

def check_head() -> list[str]:
    """What the pre-push hook asks: does HEAD bump VERSION without an entry?"""
    if not head_changes_version():
        return []
    return check_current_version_has_entry(current_version(), changelog_text())


def main() -> int:
    if "--check-head" in sys.argv:
        problems = check_head()
    else:
        text = changelog_text()
        problems = (
            check_current_version_has_entry(current_version(), text)
            + check_dates(text)
            + check_order_and_duplicates(text)
        )
        if in_git_repo():
            shipped = shipped_versions()
            problems += check_ratchet(shipped, text)
            problems += check_tags(shipped, existing_tags(), pending=current_version())
    for problem in problems:
        print(f"changelog-guard: {problem}")
    if problems:
        return 1
    print("changelog-guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
