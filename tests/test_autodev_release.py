"""The auto-release runner: what it refuses, and one release end to end.

★ WHAT THESE PIN, IN THE ORDER THAT MATTERS.

  1. THE GATE. A release may touch VERSION and CHANGELOG.md and nothing
     else policy.py forbids -- and policy.py still forbids both of them
     for everybody else. Asserted together, in one test, because the
     whole design is a composition and either half alone is wrong.
  2. EVERY BOUND. Off by default, STOP, PAUSE, quiet hours, three a day,
     the flock. Each one asserted by running a pass and finding that
     nothing happened.
  3. THE SEQUENCE, END TO END, AGAINST A REAL GIT REMOTE. A throwaway
     repository with a bare origin, a real branch, a real merge, the real
     changelog_guard, a real tag and a real push -- then the assertions
     are on what origin ended up holding. No test touches the network,
     the real remote, the real Keychain or a live marketplace: the remote
     is a directory, the footer is a file and the Keychain reader is a
     stub that is never reached.
  4. THE FAILURE CASES. A red suite pushes nothing. A hook refusal that
     passes on its own is waited out. A real non-fast-forward renumbers
     and runs the suite again. A missing tag is finished, but never
     invented.

★ WHY THE SUITE STEP IS STUBBED IN THE END-TO-END TEST AND NOTHING ELSE
IS. The step under test is the SEQUENCE -- merge, number, prove, commit,
push, tag, verify -- and running the real fifteen-minute suite inside it
would say nothing about that sequence while making the suite recursive.
Everything the sequence actually produces is real: the merge, the version
number, the changelog entry (checked by the real changelog_guard), the
commit, the tag and both pushes. The red-suite test then asserts the
other half: that a failing suite stops the whole thing before the push.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.autodev import (approvals, coordinator, inbox, install_release,
                             policy, release_gate, release_runner, state)

REPO = Path(__file__).resolve().parents[1]
TICKET = "20261106-0900-widget-polish"

PREAMBLE = """# Changelog

All notable changes to CardFoundry are documented in this file.

## [1.0.0] - 2026-01-01

### Added

- The beginning.
"""

# A stub pre-push hook whose behaviour comes from a file, so a test can
# make the guard refuse the way the real one refuses without this suite
# ever owning a copy of the real hook's logic.
HOOK = """#!/bin/sh
# Driven by files in $CARDFOUNDRY_TEST_HOOK_DIR, an ABSOLUTE path: git runs
# a hook with the WORKTREE as its working directory, so a path relative to
# the hook would land in whichever tree is being pushed from.
cat > /dev/null
dir="${CARDFOUNDRY_TEST_HOOK_DIR:?}"
mode="$(cat "$dir/MODE" 2>/dev/null || echo ok)"
count_file="$dir/COUNT.$mode"
count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$count_file"
case "$mode" in
  ok) exit 0 ;;
  tick)
    if [ "$count" -ge 2 ]; then exit 0; fi
    echo "pre-push: refusing -- 03:21 UTC is within 5 minutes BEFORE the 03:25 UTC cron tick."
    exit 1 ;;
  busy)
    echo "pre-push: refusing -- the app reports a job in flight that a deploy would kill:"
    exit 1 ;;
  bump)
    echo "pre-push: refusing -- these commits do not bump VERSION."
    exit 1 ;;
esac
exit 0
"""


def _run(args, cwd, **kw):
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, **kw)
    assert proc.returncode == 0, f"{args}: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


def _git(args, cwd):
    return _run(["git", *args], cwd)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A throwaway repository, a bare origin, and a finished branch.

    Everything the runner reads is private to this fixture: its own
    AutoDev home, its own mailbox, its own worktree root, its own remote.
    """
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    repo.mkdir()
    _git(["init", "-b", "main", "."], repo)
    _git(["config", "user.email", "test@example.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)

    (repo / "VERSION").write_text("1.0.0\n")
    (repo / "CHANGELOG.md").write_text(PREAMBLE)
    (repo / "widget.py").write_text("VALUE = 1\n")
    # The slashless spelling, because a trailing slash matches a DIRECTORY
    # and the linked .venv is a symlink -- the trap that was one `git add
    # -A` away from committing the operator's virtualenv.
    (repo / ".gitignore").write_text(".venv\n")
    (repo / "tests").mkdir()
    # The REAL guard, so the entry this runner writes has to satisfy the
    # same code the pre-push hook calls.
    shutil.copy(REPO / "changelog_guard.py", repo / "changelog_guard.py")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "chore: start"], repo)

    _run(["git", "init", "--bare", str(origin)], tmp_path)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-u", "origin", "main"], repo)

    # A finished branch, exactly the shape the inbox runner leaves behind.
    _git(["checkout", "-b", f"inbox/{TICKET}"], repo)
    (repo / "widget.py").write_text("VALUE = 2\n")
    _git(["commit", "-am", "feat: the widget counts to two"], repo)
    _git(["checkout", "main"], repo)

    # A linked interpreter, the way an inbox worktree gets one.
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)

    hooks = repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text(HOOK)
    hook.chmod(0o755)
    _git(["config", "core.hooksPath", ".githooks"], repo)
    # Set BEFORE the first push through the hook. The hook refuses outright
    # without it, which is deliberate: every push in this file then has to
    # have gone through the hook, so "it pushes through the guard" is
    # proved by the pushes working at all rather than by a comment.
    control = tmp_path / "hook-control"
    control.mkdir()
    monkeypatch.setenv("CARDFOUNDRY_TEST_HOOK_DIR", str(control))
    # .githooks only: `add -A` here would commit the .venv symlink made
    # just above, which is the trap the real repo's .gitignore now covers.
    _git(["add", ".githooks"], repo)
    _git(["commit", "-m", "chore: the stub hook"], repo)
    _git(["push", "origin", "main"], repo)

    home = tmp_path / "home"
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_HOME", str(home))
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_WORKTREES", str(tmp_path / "wt"))
    monkeypatch.setenv("CARDFOUNDRY_MAILBOX_HOME", str(tmp_path / "mailbox"))
    monkeypatch.setattr(state, "REPO_ROOT", repo)
    state.ensure_home()
    inbox.ensure_mailbox()
    (home / "config.json").write_text(json.dumps({
        "releases_enabled": True, "base_branch": "main",
        "models": dict(coordinator.DEFAULT_CONFIG["models"]),
    }))

    ticket = {
        "id": TICKET, "title": "Polish the widget", "autonomous": "yes",
        "eligibility_reviewed_by": "cowork",
        "eligibility_reviewed_at": "2026-11-06T09:00:00-05:00",
        "intent": "Count to two.", "acceptance": ["It counts to two."],
        "files": ["widget.py"],
    }
    inbox.write_ticket(ticket, root=inbox.done_dir())
    approve(ticket)
    file_done_report(TICKET)

    footer = tmp_path / "footer.html"
    footer.write_text("<footer>CardFoundry v1.1.0</footer>")
    monkeypatch.setenv(release_runner.FOOTER_FILE_ENV, str(footer))

    # Nothing here may reach the real Keychain even by accident.
    monkeypatch.setenv(release_runner.SECURITY_ENV, "/usr/bin/false")

    return {"repo": repo, "origin": origin, "home": home, "footer": footer,
            "hooks": control, "ticket": ticket}


def approve(ticket: dict, **overrides) -> Path:
    note = {
        "task": ticket["id"], "autodev_ready": True,
        "operator_words": approvals.STANDING_BASES[
            "standing-low-risk-2026-11-04"]["operator_words"],
        "approved_at": "2026-11-06T09:05:00-05:00", "written_by": "cowork",
        "covers_automatic_retry": True,
        "approval_basis": "standing-low-risk-2026-11-04",
        "low_risk_assessment": "tooling only; no money, marketplace, schema, "
                               "JS or buyer-facing surface",
        "ticket_digest": inbox.ticket_digest(ticket),
    }
    note.update(overrides)
    path = approvals.path_for(ticket["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(note, indent=2, sort_keys=True))
    return path


def file_done_report(task_id: str, status: str = "done") -> Path:
    return inbox.file_report(task_id, status, "The work is finished.\n")


def stub_checks(monkeypatch, *, suite_ok=True,
                summary="7306 passed in 800.00s"):
    monkeypatch.setattr(release_runner, "_suite",
                        lambda worktree: (suite_ok, summary))
    monkeypatch.setattr(release_runner, "_compileall",
                        lambda worktree: (True, "ok"))
    monkeypatch.setattr(release_runner, "_integrity_check",
                        lambda: (True, "ok"))


def remote_tags(origin: Path) -> set[str]:
    out = _run(["git", "ls-remote", "--tags", str(origin)], origin)
    return {line.split("refs/tags/")[-1] for line in out.splitlines()
            if "refs/tags/" in line and not line.endswith("^{}")}


def remote_main(origin: Path) -> str:
    out = _run(["git", "ls-remote", str(origin), "refs/heads/main"], origin)
    return out.split()[0] if out.strip() else ""


# --------------------------------------------------------------------------
# 1. The gate: policy.py composed with a two-file exception
# --------------------------------------------------------------------------

def test_a_release_may_touch_the_two_files_policy_still_forbids_everyone():
    """★ THE WHOLE DESIGN, IN ONE ASSERTION PAIR. policy.py forbids VERSION
    and CHANGELOG.md under 'releases are the operator's', and a release
    must touch both -- so the gate is policy COMPOSED with an exception,
    and the exception must not leak back into policy for anybody else."""
    for path in ("VERSION", "CHANGELOG.md"):
        assert release_gate.allowed(path), path
        assert policy.forbidden_reason(path) is not None, path
        assert not policy.allows(path), path


@pytest.mark.parametrize("path", [
    "models.py", "database.py", "order_service.py", "manapool_service.py",
    "bulk_pricing_service.py", "consignment_service.py",
    "scheduled_pricing_apply.py", "scripts/hooks/pre-push",
    "changelog_guard.py", ".env", "cardfoundry.db",
    "new_payout_helper.py", "some_pricing_thing.py", "backups/x.sql",
])
def test_the_gate_refuses_every_path_the_operator_reserved(path):
    assert not release_gate.allowed(path)
    assert release_gate.path_refusal(path)


@pytest.mark.parametrize("path", [
    "claude/mailbox/x.md", ".git/config", ".venv/bin/python",
])
def test_the_gate_also_refuses_the_absolute_denylist(path):
    """policy.py has no opinion on these because a ticket could never name
    them usefully; a diff must not carry them either."""
    assert release_gate.path_refusal(path)


@pytest.mark.parametrize("path", ["/etc/passwd", "../elsewhere.py", "", "  "])
def test_the_gate_refuses_anything_that_is_not_a_repo_relative_path(path):
    assert release_gate.path_refusal(path)


def test_the_public_and_private_policy_spellings_agree_on_every_name():
    """The release gate asks policy.py its question from outside the
    module, so the public spelling must be the same check and not a copy
    of it."""
    names = [name for group in policy.FORBIDDEN_PATHS.values() for name in group]
    for name in names + ["main.py", "tests/test_x.py", "scripts/autodev/x.py"]:
        assert policy.forbidden_reason(name) == policy._forbidden_reason(name)


def test_an_ordinary_change_passes_the_gate():
    for path in ("main.py", "tests/test_widget.py", "scripts/autodev/inbox.py",
                 "AGENTS.md", "CLAUDE.md"):
        assert release_gate.allowed(path), path


def test_assess_paths_reports_every_refusal_not_just_the_first():
    refusals = release_gate.assess_paths(
        ["main.py", "models.py", "order_service.py", ".env"])
    assert len(refusals) == 3


# --------------------------------------------------------------------------
# The bump rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subjects,expected", [
    (["feat: a thing"], "minor"),
    (["fix: a thing"], "patch"),
    (["chore: a thing", "test: another"], "patch"),
    (["fix: a thing", "feat: and a feature"], "minor"),
    (["feat(scope): a thing"], "minor"),
])
def test_the_bump_comes_from_the_branch_s_own_subjects(subjects, expected):
    bump, refusal = release_gate.bump_for(subjects)
    assert (bump, refusal) == (expected, None)


@pytest.mark.parametrize("subject", [
    "feat!: a breaking thing", "fix(api)!: breaking",
    "feat: a thing\n\nBREAKING CHANGE: it moved",
])
def test_a_breaking_change_is_never_released_unattended(subject):
    bump, refusal = release_gate.bump_for([subject])
    assert bump is None and "breaking" in refusal


def test_a_branch_with_no_commits_has_no_release_in_it():
    bump, refusal = release_gate.bump_for([])
    assert bump is None and "no commits" in refusal


@pytest.mark.parametrize("current,bump,expected", [
    ("2.115.0", "minor", "2.116.0"), ("2.115.0", "patch", "2.115.1"),
    ("2.115.3", "minor", "2.116.0"), ("1.0.0", "patch", "1.0.1"),
])
def test_next_version(current, bump, expected):
    assert release_gate.next_version(current, bump) == expected


def test_next_version_refuses_a_major():
    with pytest.raises(ValueError):
        release_gate.next_version("2.115.0", "major")


# --------------------------------------------------------------------------
# The changelog entry and its proof
# --------------------------------------------------------------------------

def test_removing_the_new_entry_reproduces_the_original_byte_for_byte():
    """★ THE CHECK THAT CATCHES A BAD CONFLICT RESOLUTION, done by hand at
    every release since it first caught one. Here it is the construction
    itself: the new file IS the old file with one block inserted."""
    entry = release_runner.changelog_entry(
        "1.1.0", "minor", ["feat: a thing"], branch="inbox/x", basis=None,
        today=date(2026, 11, 6))
    after, error = release_runner.insert_entry(PREAMBLE, entry)
    assert error is None
    assert after != PREAMBLE
    assert after.replace(entry, "", 1) == PREAMBLE


def test_the_entry_goes_above_the_newest_one_and_the_guard_parses_it():
    entry = release_runner.changelog_entry(
        "1.1.0", "minor", ["feat: a thing"], branch="inbox/x", basis=None,
        today=date(2026, 11, 6))
    after, _ = release_runner.insert_entry(PREAMBLE, entry)
    import changelog_guard

    headings = changelog_guard.parse_headings(after)
    assert headings[0] == ("1.1.0", "2026-11-06")
    assert headings[1][0] == "1.0.0"
    assert changelog_guard.check_dates(after, today=date(2026, 11, 6)) == []
    assert changelog_guard.check_order_and_duplicates(after) == []


def test_a_changelog_with_nowhere_to_insert_is_an_error_not_a_guess():
    after, error = release_runner.insert_entry("no headings here\n", "x")
    assert error and "no '## ['" in error


def test_the_entry_records_that_the_release_was_automatic_and_its_authority():
    entry = release_runner.changelog_entry(
        "1.1.0", "patch", ["fix: a thing"], branch="inbox/x",
        basis="the standing rule standing-low-risk-2026-11-04",
        today=date(2026, 11, 6))
    assert "Released automatically" in entry
    assert "standing-low-risk-2026-11-04" in entry
    assert "### Fixed" in entry


# --------------------------------------------------------------------------
# 2. Every bound
# --------------------------------------------------------------------------

def test_it_does_nothing_at_all_while_releases_enabled_is_false(world):
    (world["home"] / "config.json").write_text(json.dumps(
        {"releases_enabled": False}))
    code, rp = release_runner.run_once(confirm=True)
    assert (code, rp.enabled, rp.released) == (0, False, None)
    assert remote_tags(world["origin"]) == set()


def test_releases_enabled_is_false_by_default():
    """The switch ships off, so a config.json written before this existed
    turns nothing on by omission."""
    assert coordinator.DEFAULT_CONFIG["releases_enabled"] is False


def test_the_stop_sentinel_stops_it(world, monkeypatch):
    stub_checks(monkeypatch)
    (world["home"] / state.STOP_FILE).write_text("")
    code, rp = release_runner.run_once(confirm=True)
    assert (code, rp.released) == (0, None)
    assert remote_tags(world["origin"]) == set()


def test_the_pause_sentinel_stops_it(world, monkeypatch):
    stub_checks(monkeypatch)
    (world["home"] / state.PAUSE_FILE).write_text("")
    code, rp = release_runner.run_once(confirm=True)
    assert (code, rp.released) == (0, None)
    assert remote_tags(world["origin"]) == set()


@pytest.mark.parametrize("hour", [23, 0, 3, 6])
def test_quiet_hours_wrap_midnight(hour):
    assert release_runner.quiet_now(datetime(2026, 11, 6, hour, 30))


@pytest.mark.parametrize("hour", [7, 12, 18, 22])
def test_outside_quiet_hours_it_may_run(hour):
    assert not release_runner.quiet_now(datetime(2026, 11, 6, hour, 30))


def test_nothing_is_released_during_quiet_hours(world, monkeypatch):
    stub_checks(monkeypatch)
    monkeypatch.setattr(release_runner, "quiet_now", lambda now=None: True)
    code, rp = release_runner.run_once(confirm=True)
    assert (code, rp.quiet, rp.released) == (0, True, None)
    assert remote_tags(world["origin"]) == set()


def test_the_third_release_of_the_day_is_the_last(world, monkeypatch):
    """★ THE OPERATOR'S OWN BOUND (2026-11-06), counted from the
    append-only audit log rather than a counter file that could disagree
    with it."""
    stub_checks(monkeypatch)
    for n in range(release_runner.MAX_RELEASES_PER_DAY):
        state.audit("release_pushed", version=f"1.0.{n}")
    assert len(release_runner.releases_today()) == 3
    code, rp = release_runner.run_once(confirm=True)
    assert (code, rp.released) == (0, None)
    assert "cap is 3" in (rp.detail or "")
    assert remote_tags(world["origin"]) == set()


def test_yesterday_s_releases_do_not_count_against_today(world):
    state.audit("release_pushed", version="1.0.0")
    rows = release_runner.releases_today(date(2020, 1, 1))
    assert rows == []


def test_another_owner_holding_the_lock_stops_the_pass(world, monkeypatch):
    stub_checks(monkeypatch)
    with state.single_owner_lock():
        code, rp = release_runner.run_once(confirm=True)
    assert (code, rp.released) == (0, None)
    assert remote_tags(world["origin"]) == set()


# --------------------------------------------------------------------------
# What it refuses to release
# --------------------------------------------------------------------------

def test_a_branch_whose_report_is_not_done_is_left_alone(world, monkeypatch):
    stub_checks(monkeypatch)
    for path in inbox.reports_dir().glob("*.md"):
        path.unlink()
    file_done_report(TICKET, "blocked")
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("says blocked, not done" in reason
               for row in rp.waiting for reason in row["refusals"])
    assert remote_tags(world["origin"]) == set()


def test_a_branch_with_no_report_at_all_is_left_alone(world, monkeypatch):
    stub_checks(monkeypatch)
    for path in inbox.reports_dir().glob("*.md"):
        path.unlink()
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("no finished report" in reason
               for row in rp.waiting for reason in row["refusals"])


def test_a_branch_with_no_approval_note_is_left_alone(world, monkeypatch):
    stub_checks(monkeypatch)
    approvals.path_for(TICKET).unlink()
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("no approval note" in reason
               for row in rp.waiting for reason in row["refusals"])
    assert remote_tags(world["origin"]) == set()


def test_an_unknown_approval_basis_authorises_nothing(world, monkeypatch):
    stub_checks(monkeypatch)
    approve(world["ticket"], approval_basis="standing-whatever-i-like")
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("not a standing rule" in reason
               for row in rp.waiting for reason in row["refusals"])


def test_a_ticket_edited_after_its_ok_invalidates_the_ok(world, monkeypatch):
    stub_checks(monkeypatch)
    edited = dict(world["ticket"], intent="something else entirely")
    inbox.write_ticket(edited, root=inbox.done_dir())
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("changed since it was approved" in reason
               for row in rp.waiting for reason in row["refusals"])


def test_a_note_with_a_digest_whose_ticket_cannot_be_read_fails_closed(
        world, monkeypatch):
    stub_checks(monkeypatch)
    (inbox.done_dir() / f"{TICKET}.json").unlink()
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("cannot be read" in reason
               for row in rp.waiting for reason in row["refusals"])


def test_a_diff_touching_a_reserved_path_is_left_for_the_operator(
        world, monkeypatch):
    """★ THE DIFF, NOT THE TICKET'S FILES LIST. The ticket says widget.py;
    the branch also changed order_service.py, which is the case this gate
    exists for."""
    stub_checks(monkeypatch)
    repo = world["repo"]
    _git(["checkout", f"inbox/{TICKET}"], repo)
    (repo / "order_service.py").write_text("# money\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "fix: also touch the money service"], repo)
    _git(["checkout", "main"], repo)
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("money and the marketplace" in reason
               for row in rp.waiting for reason in row["refusals"])
    assert remote_tags(world["origin"]) == set()


def test_a_breaking_branch_is_left_for_the_operator(world, monkeypatch):
    stub_checks(monkeypatch)
    repo = world["repo"]
    _git(["checkout", f"inbox/{TICKET}"], repo)
    (repo / "widget.py").write_text("VALUE = 3\n")
    _git(["commit", "-am", "feat!: the widget counts differently now"], repo)
    _git(["checkout", "main"], repo)
    code, rp = release_runner.run_once(confirm=True)
    assert rp.released is None
    assert any("breaking change is never released" in reason
               for row in rp.waiting for reason in row["refusals"])


def test_a_branch_it_will_not_touch_is_named_in_a_report_not_dropped(
        world, monkeypatch):
    """★ NEVER SILENTLY SKIPPED. A route dropped from a report is worse
    than one that cannot be categorised, and the same goes for a branch."""
    stub_checks(monkeypatch)
    approvals.path_for(TICKET).unlink()
    release_runner.run_once(confirm=True)
    reports = sorted(inbox.reports_dir().glob("*nothing-releasable*.md"))
    assert reports
    text = reports[-1].read_text()
    assert "STATUS: blocked" in text
    assert f"inbox/{TICKET}" in text
    assert "no approval note" in text


def test_the_same_situation_is_not_reported_every_pass(world, monkeypatch):
    """★ A RELEASED BRANCH STAYS IN THE REPOSITORY and is refused from
    then on for having nothing left to release. Reporting that every pass
    would put sixteen identical reports a day into the mailbox whose
    whole purpose is that reports are not lost."""
    stub_checks(monkeypatch)
    approvals.path_for(TICKET).unlink()

    release_runner.run_once(confirm=True)
    first = sorted(inbox.reports_dir().glob("*nothing-releasable*.md"))
    assert len(first) == 1

    release_runner.run_once(confirm=True)
    release_runner.run_once(confirm=True)
    assert sorted(inbox.reports_dir().glob("*nothing-releasable*.md")) == first


def test_a_new_refusal_is_reported_on_the_very_next_pass(world, monkeypatch):
    """Nothing is suppressed -- only repetition is."""
    stub_checks(monkeypatch)
    approvals.path_for(TICKET).unlink()
    release_runner.run_once(confirm=True)
    first = sorted(inbox.reports_dir().glob("*nothing-releasable*.md"))
    assert len(first) == 1

    repo = world["repo"]
    _git(["checkout", "-b", "inbox/20261106-1200-another-one"], repo)
    (repo / "widget.py").write_text("VALUE = 7\n")
    _git(["commit", "-am", "fix: another branch nobody approved"], repo)
    _git(["checkout", "main"], repo)

    release_runner.run_once(confirm=True)
    reports = sorted(inbox.reports_dir().glob("*nothing-releasable*.md"))
    assert len(reports) == 2
    # NOT reports[-1]: the counter suffix `-2.md` sorts BEFORE `.md`.
    added = [path for path in reports if path not in first][0]
    assert "inbox/20261106-1200-another-one" in added.read_text()


def test_the_newest_report_wins_even_with_a_counter_suffix(world):
    """★ THE TRAP: the mailbox appends `-2` to avoid overwriting, and `-`
    sorts BEFORE `.`, so a lexicographic 'last' is the OLDER file. Two
    reports in one minute is exactly the case that matters -- a ticket
    re-run after a fix."""
    for path in inbox.reports_dir().glob("*.md"):
        path.unlink()
    now = datetime(2026, 11, 6, 9, 30)
    older = inbox.file_report(TICKET, "blocked", "the first try\n", now=now)
    newer = inbox.file_report(TICKET, "done", "the second try\n", now=now)
    assert older.name.endswith(".md") and newer.name.endswith("-2.md")
    assert sorted([older, newer])[-1] == older, "the trap, in one line"
    os.utime(newer, (older.stat().st_mtime + 10,) * 2)
    path, status = release_runner.mailbox_status(TICKET)
    assert (path, status) == (newer, "done")


# --------------------------------------------------------------------------
# 3. One release, end to end, against a real git remote
# --------------------------------------------------------------------------

def test_one_release_end_to_end(world, monkeypatch):
    stub_checks(monkeypatch)
    origin = world["origin"]
    before = remote_main(origin)

    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)

    assert code == 0, rp.as_dict()
    assert rp.released == f"inbox/{TICKET}"
    # feat -> minor, from 1.0.0
    assert rp.version == "1.1.0"
    assert rp.tag == "v1.1.0"
    assert rp.live == "1.1.0"

    # What origin actually ended up holding.
    assert remote_main(origin) == rp.head != before
    assert "v1.1.0" in remote_tags(origin)

    # The release commit: VERSION and the entry, and the merge beneath it.
    body = _run(["git", "show", "--stat", "--format=%s%n%b", rp.head],
                world["repo"])
    assert "(v1.1.0)" in body
    assert "VERSION" in body and "CHANGELOG.md" in body
    assert "widget.py" not in body.split("CHANGELOG.md")[-1]
    parents = _git(["rev-list", "--parents", "-n", "1", rp.head], world["repo"])
    assert len(parents.split()) == 2  # the release commit has one parent
    merge = _git(["log", "--format=%s", "-n", "2", rp.head], world["repo"])
    assert "Merge" in merge

    # The files, as they landed.
    version = _git(["show", f"{rp.head}:VERSION"], world["repo"])
    assert version.strip() == "1.1.0"
    changelog = _git(["show", f"{rp.head}:CHANGELOG.md"], world["repo"])
    assert "## [1.1.0]" in changelog
    assert "The widget counts to two" in changelog
    assert "## [1.0.0]" in changelog
    assert changelog.index("## [1.1.0]") < changelog.index("## [1.0.0]")
    assert _git(["show", f"{rp.head}:widget.py"], world["repo"]).strip() \
        == "VALUE = 2"

    # The audit trail, which is what the daily cap counts.
    assert len(release_runner.releases_today()) == 1
    events = [row["event"] for row in state.read_audit()]
    for event in ("release_pass_started", "release_started", "release_pushed",
                  "release_tag_pushed", "release_verified", "release_finished"):
        assert event in events, event

    # The report, filed in the mailbox like every other report.
    assert rp.report and Path(rp.report).exists()
    text = Path(rp.report).read_text()
    assert text.startswith("◀ FROM CODE-AUTODEV → CHAT-AUTODEV")
    assert "STATUS: done" in text
    assert "1.1.0" in text and rp.head in text
    assert "standing-low-risk-2026-11-04" in text


def test_the_real_checkout_is_fast_forwarded_so_the_next_build_is_current(
        world, monkeypatch):
    """★ WHY IT TOUCHES THE CHECKOUT AT ALL: the next inbox ticket
    branches off LOCAL main, so leaving it behind means every future
    unattended build starts from older code."""
    stub_checks(monkeypatch)
    release_runner.run_once(confirm=True, sleeper=lambda s: None)
    local = _git(["rev-parse", "main"], world["repo"])
    assert local == remote_main(world["origin"])


def test_a_checkout_with_uncommitted_work_is_left_exactly_as_it_is(
        world, monkeypatch):
    """Somebody is editing in there. The release still goes out -- it is
    staged in its own worktree -- and their work is untouched."""
    stub_checks(monkeypatch)
    (world["repo"] / "widget.py").write_text("VALUE = 99  # mid-edit\n")
    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)
    assert code == 0 and rp.version == "1.1.0"
    assert (world["repo"] / "widget.py").read_text() == "VALUE = 99  # mid-edit\n"
    assert any("uncommitted changes" in line for line in rp.steps)
    assert "v1.1.0" in remote_tags(world["origin"])


def test_an_untracked_scratch_file_does_not_block_the_fast_forward(
        world, monkeypatch):
    """★ MEASURED: the linked .venv symlink alone made an all-files clean
    check skip the fast-forward every single time, which would have left
    every future unattended build on older and older code."""
    stub_checks(monkeypatch)
    (world["repo"] / "scratch.txt").write_text("somebody's export\n")
    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)
    assert code == 0
    assert _git(["rev-parse", "main"], world["repo"]) \
        == remote_main(world["origin"])
    assert (world["repo"] / "scratch.txt").exists()


def test_a_dry_run_writes_nothing_and_pushes_nothing(world, monkeypatch):
    stub_checks(monkeypatch)
    before_main = remote_main(world["origin"])
    code, rp = release_runner.run_once(confirm=False, sleeper=lambda s: None)
    assert code == 0
    assert rp.version == "1.1.0"          # it says what it WOULD do
    assert remote_main(world["origin"]) == before_main
    assert remote_tags(world["origin"]) == set()
    assert _git(["show", "main:VERSION"], world["repo"]).strip() == "1.0.0"
    assert "DRY RUN" in " ".join(rp.steps)


def test_the_dry_run_is_the_default(world, monkeypatch):
    """A bare `python -m scripts.autodev.release_runner` must not release."""
    stub_checks(monkeypatch)
    release_runner.main([])
    assert remote_tags(world["origin"]) == set()


# --------------------------------------------------------------------------
# 4. The failure cases
# --------------------------------------------------------------------------

def test_a_red_suite_pushes_nothing(world, monkeypatch):
    stub_checks(monkeypatch, suite_ok=False, summary="3 failed, 7300 passed")
    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)
    assert code == 3
    assert "not green" in (rp.detail or "")
    assert remote_main(world["origin"]) != (rp.head or "")
    assert remote_tags(world["origin"]) == set()
    report = Path(rp.report).read_text()
    assert "STATUS: blocked" in report
    assert "3 failed" in report


def test_a_hook_refusal_that_passes_on_its_own_is_waited_out(world, monkeypatch):
    """A cron tick window is fifteen minutes at most, so the pass waits
    rather than giving up -- and the second attempt lands."""
    stub_checks(monkeypatch)
    (world["hooks"] / "MODE").write_text("tick")
    slept = []
    code, rp = release_runner.run_once(confirm=True, sleeper=slept.append)
    assert code == 0 and rp.version == "1.1.0"
    assert release_runner.PUSH_RETRY_SECONDS in slept
    assert "v1.1.0" in remote_tags(world["origin"])


def test_a_hook_refusal_that_will_not_pass_stops_at_once(world, monkeypatch):
    """A refusal that is a DECISION is reported, never waited out and
    never worked around: this module does not know --no-verify exists."""
    stub_checks(monkeypatch)
    (world["hooks"] / "MODE").write_text("bump")
    slept = []
    code, rp = release_runner.run_once(confirm=True, sleeper=slept.append)
    assert code == 3
    assert slept == []
    assert "do not bump VERSION" in (rp.detail or "")
    assert remote_tags(world["origin"]) == set()


def test_a_job_in_flight_is_waited_out_then_reported(world, monkeypatch):
    stub_checks(monkeypatch)
    (world["hooks"] / "MODE").write_text("busy")
    slept = []
    code, rp = release_runner.run_once(confirm=True, sleeper=slept.append)
    assert code == 3
    assert len(slept) == release_runner.PUSH_ATTEMPTS - 1
    assert remote_tags(world["origin"]) == set()
    assert "hook-window" in (rp.detail or "")


def test_a_real_non_fast_forward_renumbers_and_runs_the_suite_again(
        world, monkeypatch):
    """★ TWO TRACKS PUSHING THE SAME DAY, WHICH HAS ALREADY BITTEN TWICE.
    Somebody else releases while the suite runs, so the number this pass
    reserved is gone. It must fetch, renumber, and run the FULL suite on
    the NEW merged tree -- not re-check the diff, because a conflict can
    be semantic."""
    suites = []

    def counting_suite(worktree):
        suites.append(str(worktree))
        if len(suites) == 1:
            # The moment our suite "starts", somebody else ships 1.1.0.
            other = world["repo"].parent / "other"
            _run(["git", "clone", str(world["origin"]), str(other)],
                 world["repo"].parent)
            _git(["config", "user.email", "other@example.invalid"], other)
            _git(["config", "user.name", "Other"], other)
            (other / "VERSION").write_text("1.1.0\n")
            (other / "CHANGELOG.md").write_text(
                PREAMBLE.replace("## [1.0.0]",
                                 "## [1.1.0] - 2026-11-06\n\n### Added\n\n"
                                 "- Somebody else's work.\n\n## [1.0.0]", 1))
            _git(["commit", "-am", "feat: somebody else's release (v1.1.0)"],
                 other)
            _git(["tag", "-a", "v1.1.0", "-m", "v1.1.0"], other)
            _git(["push", "origin", "main"], other)
            _git(["push", "origin", "v1.1.0"], other)
        return True, f"7306 passed (run {len(suites)})"

    monkeypatch.setattr(release_runner, "_suite", counting_suite)
    monkeypatch.setattr(release_runner, "_compileall", lambda w: (True, "ok"))
    monkeypatch.setattr(release_runner, "_integrity_check", lambda: (True, "ok"))
    world["footer"].write_text("<footer>CardFoundry v1.2.0</footer>")

    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)

    assert code == 0, rp.as_dict()
    # Renumbered over the version that appeared underneath it.
    assert rp.version == "1.2.0"
    assert len(suites) == 2, "the full suite must run again on the new tree"
    assert {"v1.1.0", "v1.2.0"} <= remote_tags(world["origin"])
    assert remote_main(world["origin"]) == rp.head
    # And the other session's entry survived, which is what the
    # byte-for-byte proof is there to guarantee.
    changelog = _git(["show", f"{rp.head}:CHANGELOG.md"], world["repo"])
    assert "Somebody else's work." in changelog
    assert "## [1.2.0]" in changelog


def test_an_already_tagged_number_is_renumbered_before_anything_is_written(
        world, monkeypatch):
    stub_checks(monkeypatch)
    repo = world["repo"]
    _git(["tag", "-a", "v1.1.0", "-m", "taken"], repo)
    _git(["push", "origin", "v1.1.0"], repo)
    world["footer"].write_text("<footer>CardFoundry v1.1.1</footer>")
    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)
    # 1.1.0 is taken by a tag, so the next free number is 1.1.1 -- the same
    # renumber the hook tells a human to do in this exact situation.
    assert code == 0, rp.as_dict()
    assert rp.version == "1.1.1"
    assert any("already tagged" in line for line in rp.steps)
    assert {"v1.1.0", "v1.1.1"} <= remote_tags(world["origin"])


def test_a_footer_that_never_catches_up_is_reported_not_re_pushed(
        world, monkeypatch):
    """★ NOT RE-PUSHED AND NOT ROLLED BACK -- that is the operator's
    call. main and the tag are live; what is wrong is the deploy."""
    stub_checks(monkeypatch)
    world["footer"].write_text("<footer>CardFoundry v1.0.0</footer>")
    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)
    assert code == 3
    assert rp.live == "1.0.0" and rp.version == "1.1.0"
    # The push DID land -- this is a live discrepancy, not a failed push.
    assert remote_main(world["origin"]) == rp.head
    assert "v1.1.0" in remote_tags(world["origin"])
    report = Path(rp.report).read_text()
    assert "rather than 1.1.0" in report
    assert "operator's call" in report


def test_an_interrupted_release_has_its_tag_finished_and_nothing_else(
        world, monkeypatch):
    """★ THE ONE RESUME. Between the two pushes, main is live and the tag
    is absent, and changelog_guard counts that as a defect forever."""
    stub_checks(monkeypatch)
    repo = world["repo"]
    # Stage exactly that state: main pushed at 1.1.0, tag local only.
    _git(["merge", "--no-ff", "--no-edit", f"inbox/{TICKET}"], repo)
    (repo / "VERSION").write_text("1.1.0\n")
    (repo / "CHANGELOG.md").write_text(
        PREAMBLE.replace("## [1.0.0]",
                         "## [1.1.0] - 2026-11-06\n\n### Added\n\n"
                         "- Interrupted.\n\n## [1.0.0]", 1))
    _git(["commit", "-am", "feat: interrupted (v1.1.0)"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    _git(["tag", "-a", "v1.1.0", "-m", "v1.1.0"], repo)
    _git(["push", "origin", "main"], repo)
    assert remote_tags(world["origin"]) == set()

    code, rp = release_runner.run_once(confirm=True, sleeper=lambda s: None)

    assert code == 0
    assert "v1.1.0" in remote_tags(world["origin"])
    assert remote_main(world["origin"]) == head
    assert rp.released is None, "it finishes the tag and releases nothing else"
    assert "tag" in (rp.detail or "")


def test_a_tag_that_does_not_exist_locally_is_never_invented(world, monkeypatch):
    """A tag this runner did not create is not one it may make up: that
    would name somebody else's commit as a release on a guess."""
    stub_checks(monkeypatch)
    repo = world["repo"]
    _git(["merge", "--no-ff", "--no-edit", f"inbox/{TICKET}"], repo)
    (repo / "VERSION").write_text("1.1.0\n")
    _git(["commit", "-am", "feat: interrupted with no tag at all"], repo)
    _git(["push", "origin", "main"], repo)
    rp = release_runner.ReleasePass()
    assert release_runner.finish_missing_tag(True, rp) is False
    assert remote_tags(world["origin"]) == set()
    assert any("not creating one" in line.lower() for line in rp.steps)


def _live_strings(source: str) -> list[str]:
    """Every string literal in the module that is NOT a docstring."""
    import ast as _ast

    tree = _ast.parse(source)
    docstrings = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.FunctionDef,
                             _ast.AsyncFunctionDef, _ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], _ast.Expr) and \
                    isinstance(body[0].value, _ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [node.value for node in _ast.walk(tree)
            if isinstance(node, _ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings]


def test_the_runner_does_not_know_the_no_verify_flag_exists():
    """★ THERE IS NO EMERGENCY EXCEPTION, so there is no code path to it.
    Asserted against the source rather than the behaviour, because the
    behaviour to assert is an absence -- and against the live strings
    only, since the docstrings are where that rule is explained."""
    for module in (release_runner, release_gate, install_release):
        source = Path(module.__file__).read_text()
        for text in _live_strings(source):
            assert "no-verify" not in text, f"{module.__name__}: {text!r}"
        # And no comment smuggles it in as a suggestion either.
        for line in source.splitlines():
            if line.strip().startswith("#"):
                assert "no-verify" not in line or "not know" in line \
                    or "does not" in line or "There is no" in line, line


def test_the_retry_markers_match_the_real_hook_s_own_wording():
    """★ A COUPLING WORTH PINNING. Which refusals are waited out is
    decided by matching the hook's own sentences, so a reword of the hook
    would silently turn "wait five minutes" into "stop and report". The
    real hook is read here, not a copy of it."""
    hook = (REPO / "scripts" / "hooks" / "pre-push").read_text().lower()
    for marker in release_runner.RETRYABLE_REFUSALS:
        assert marker in hook, (
            f"the hook no longer says {marker!r}; release_runner would stop "
            f"instead of waiting out a refusal that passes on its own")
    # And the two refusals that are DECISIONS must not match a retry
    # marker, or the runner would sit waiting for a human to act.
    for decision in ("do not bump version", "already released",
                     "goes backwards", "with no matching changelog entry"):
        assert decision in hook, decision
        assert not any(marker in decision
                       for marker in release_runner.RETRYABLE_REFUSALS)


def test_the_suite_step_refuses_when_the_worktree_has_no_interpreter(tmp_path):
    ok, detail = release_runner._suite(tmp_path)
    assert ok is False and "no interpreter" in detail


def test_the_integrity_check_reports_a_missing_database_rather_than_failing(
        world):
    ok, detail = release_runner._integrity_check()
    assert ok is True and "skipped" in detail


def test_the_integrity_check_passes_on_a_real_database(world):
    import sqlite3 as sq

    conn = sq.connect(world["repo"] / "cardfoundry.db")
    conn.execute("CREATE TABLE t (a int)")
    conn.commit()
    conn.close()
    ok, detail = release_runner._integrity_check()
    assert (ok, detail) == (True, "ok")


def test_the_live_footer_is_read_without_the_network_in_tests(world):
    version, how = release_runner.live_version()
    assert version == "1.1.0"
    assert str(world["footer"]) in how


def test_missing_credentials_are_reported_by_source_never_by_value(
        world, monkeypatch):
    monkeypatch.delenv(release_runner.FOOTER_FILE_ENV)
    monkeypatch.setenv("CARDFOUNDRY_BASE_URL", "")
    monkeypatch.setenv("CARDFOUNDRY_SERVICE_PASSWORD", "")
    version, how = release_runner.live_version(sleeper=lambda s: None)
    assert version is None
    assert "missing" in how
    assert "KEYCHAIN SETUP" in how


def test_the_readiness_password_never_reaches_the_log(world, monkeypatch):
    """★ NEVER PRINT OR LOG SECRETS. The password is read into a local and
    handed to urllib; it is never an argument to say(), and the only thing
    reported about either credential is where it came from."""
    monkeypatch.delenv(release_runner.FOOTER_FILE_ENV)
    monkeypatch.setenv("CARDFOUNDRY_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CARDFOUNDRY_SERVICE_PASSWORD", "hunter2-not-real")
    # No socket: the conftest guard forbids one and this test does not need
    # one. The failure path is what writes to the log.
    def no_network(*a, **k):
        raise OSError("refused by the test")

    monkeypatch.setattr(release_runner.urllib.request, "urlopen", no_network)
    version, how = release_runner.live_version(sleeper=lambda s: None)
    assert version is None
    assert "hunter2-not-real" not in how
    text = release_runner.log_path().read_text() \
        if release_runner.log_path().exists() else ""
    assert "hunter2-not-real" not in text


# --------------------------------------------------------------------------
# The installer and its switches
# --------------------------------------------------------------------------

def test_the_agent_runs_the_runner_with_confirm_and_does_not_fire_at_load():
    body = install_release.plist_body()
    assert body["ProgramArguments"][-3:] == [
        "-m", "scripts.autodev.release_runner", "--confirm"]
    # ★ NOT at load: --enable would otherwise release on the keystroke.
    assert body["RunAtLoad"] is False
    assert body["StartInterval"] == release_runner.PASS_INTERVAL_SECONDS
    assert body["Label"] == "com.cardfoundry.autodev.release"


def test_the_plist_carries_nothing_secret():
    body = json.dumps(install_release.plist_body()).lower()
    for mark in ("password", "token", "secret", "api_key"):
        assert mark not in body


def test_it_is_a_separate_agent_from_the_inbox_and_from_autodev():
    from scripts.autodev import install_autoserve, install_inbox

    labels = {install_autoserve.LABEL, install_inbox.LABEL,
              install_release.LABEL}
    assert len(labels) == 3
    paths = {install_autoserve.plist_path(), install_inbox.plist_path(),
             install_release.plist_path()}
    assert len(paths) == 3


def test_the_enable_command_writes_the_switch_and_the_disable_clears_it(world):
    assert install_release.set_enabled(False, True) == 0
    assert coordinator.config()["releases_enabled"] is False
    assert install_release.set_enabled(True, True) == 0
    assert coordinator.config()["releases_enabled"] is True
    events = [row["event"] for row in state.read_audit()]
    assert "releases_enabled_set" in events


def test_setting_the_switch_is_a_dry_run_without_confirm(world):
    (world["home"] / "config.json").write_text(json.dumps(
        {"releases_enabled": False}))
    assert install_release.set_enabled(True, False) == 0
    assert coordinator.config()["releases_enabled"] is False


def test_status_reads_without_writing(world, capsys):
    before = sorted(p.name for p in (world["home"]).iterdir())
    install_release.status()
    out = capsys.readouterr().out
    assert "releases_enabled: False" in out or "releases_enabled: True" in out
    assert f"inbox/{TICKET}" in out
    assert sorted(p.name for p in (world["home"]).iterdir()) == before
