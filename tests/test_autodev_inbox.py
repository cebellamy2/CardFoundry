"""The ticket inbox: every refusal, and the one path that runs.

★ WHAT THESE PROVE. The inbox replaces a PASTE, and the paste's failure
mode was silent loss -- a truncated prompt that looked complete, a stale
one re-sent. So the tests here are mostly refusals: a ticket that cannot
be read, cannot be identified, is not authorised, or names something it
may not touch has to be moved aside and REPORTED, never guessed at and
never left to be re-read every firing.

★ THE REAL MODULES, A STUB CLI. Like the other AutoDev tests, these run
the real inbox, the real approval gate, the real flock and a real git
worktree against a stub standing in for `claude`, over a throwaway
repository. CLAUDE.md forbids a test that touches the network, and the
runner's whole job is to start a session -- so the session is the only
thing stubbed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scripts.autodev import (agents, approvals, coordinator, guard, inbox,
                             inbox_runner, state, validate_ticket)

TICKET_ID = "20261105-0900-widen-the-widget"

# The stub session: it edits the file the ticket declared, commits, and
# returns a report block the way CLAUDE.md asks for one.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    raise SystemExit(0)

mode = os.environ.get("STUB_INBOX_MODE", "done")
if mode == "ratelimit":
    print(json.dumps({"type": "result", "is_error": True,
                      "session_id": "s1", "result": "usage limit reached"}))
    raise SystemExit(0)

target = Path("widget.py")
target.write_text(target.read_text() + "WIDER = 1\n")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-m", "stub: widen the widget"], check=True,
               capture_output=True)

if mode == "nostatus":
    body = "```\nI did the thing but never said how it ended.\n```"
elif mode == "liar":
    body = "```\nSTATUS: done\nI claim success.\n```"
else:
    body = "```\nSTATUS: done\nWidened the widget. Suite: 1 passed.\n```"
print(json.dumps({"type": "result", "subtype": "success",
                  "is_error": mode == "liar",
                  "session_id": "s1", "result": body,
                  "structured_output": None}))
'''

GOOD_TICKET = {
    "id": TICKET_ID,
    "title": "Widen the widget",
    "autonomous": "yes",
    "eligibility_reviewed_by": "cowork (operator OK 2026-11-05 09:00 ET)",
    "eligibility_reviewed_at": "2026-11-05",
    "intent": "The widget is too narrow.",
    "acceptance": ["widget.py defines WIDER"],
    "files": ["widget.py"],
    "tests": ["tests/test_widget.py"],
}

GOOD_NOTE = {
    "task": TICKET_ID,
    "autodev_ready": True,
    "operator_words": '09:00 ET "yes" to: widen the widget',
    "approved_at": "2026-11-05T09:00:00-05:00",
    "written_by": "cowork",
    "covers_automatic_retry": True,
}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A throwaway repo, a private AutoDev home, a private mailbox."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "widget.py").write_text("VALUE = 1\n")
    (repo / "tests" / "test_widget.py").write_text(
        "def test_value():\n    assert True\n")
    (repo / "CLAUDE.md").write_text("# how we build here\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n/claude/\n")
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "autodev@test"], repo)
    _git(["config", "user.name", "autodev"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial"], repo)

    stub = tmp_path / "stub-claude"
    stub.write_text(STUB)
    stub.chmod(0o755)

    home = tmp_path / "autodev"
    mailbox = tmp_path / "mailbox"
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_HOME", str(home))
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_WORKTREES", str(tmp_path / "wt"))
    monkeypatch.setenv(inbox.MAILBOX_ENV, str(mailbox))
    monkeypatch.setattr(state, "REPO_ROOT", repo)
    monkeypatch.setattr(agents, "claude_bin", lambda: str(stub))
    monkeypatch.setattr(
        agents, "build_command",
        lambda role, prompt, worktree, **kw: [
            sys.executable, str(stub), "-p", prompt, "--role", role,
        ],
    )
    state.ensure_home()
    inbox.ensure_mailbox()
    (home / "config.json").write_text(json.dumps({
        "inbox_enabled": True, "base_branch": "main",
    }))
    return {"repo": repo, "home": home, "mailbox": mailbox, "stub": stub}


def put(data: dict, *, name: str | None = None) -> Path:
    path = inbox.inbox_dir() / (name or f"{data.get('id')}.json")
    return inbox.atomic_write(path, json.dumps(data))


def approve(note: dict | None = None, *, ticket: dict | None = None,
            digest: str | None = None) -> Path:
    body = dict(note or GOOD_NOTE)
    if digest is not None:
        body["ticket_digest"] = digest
    elif ticket is not None:
        body["ticket_digest"] = inbox.ticket_digest(ticket)
    path = approvals.path_for(body["task"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    return path


def reports() -> list[Path]:
    return sorted(inbox.reports_dir().glob("*.md"))


# --------------------------------------------------------------------------
# The ticket's own validity
# --------------------------------------------------------------------------

def test_a_truncated_ticket_cannot_parse_and_is_refused(world):
    """★ THIS IS WHY THE INBOX IS JSON. A truncated PASTE looked
    complete; the only thing that caught one was a human noticing the
    missing END OF PROMPT line. Half a JSON file does not parse."""
    whole = json.dumps(GOOD_TICKET)
    path = inbox.atomic_write(
        inbox.inbox_dir() / f"{TICKET_ID}.json", whole[: len(whole) // 2])
    candidate = inbox.load(path)
    assert not candidate.is_runnable
    assert any("unreadable ticket" in reason for reason in candidate.refusals)


def test_a_ticket_that_is_not_an_object_is_refused(world):
    path = put({})  # noqa: F841 - replaced below
    inbox.atomic_write(path, json.dumps(["not", "an", "object"]))
    candidate = inbox.load(path)
    assert candidate.refusals == ["ticket is not a JSON object"]


@pytest.mark.parametrize("field", inbox.REQUIRED_FIELDS)
def test_every_required_field_is_required(world, field):
    data = {key: value for key, value in GOOD_TICKET.items() if key != field}
    path = put(data, name=f"{TICKET_ID}.json")
    refusals = inbox.load(path).refusals
    assert any(f"missing required field '{field}'" in r for r in refusals), refusals
    if field == "id":
        # A ticket with no id also cannot be identified at all, and both
        # reasons are reported: one refusal should say everything.
        assert any("must be YYYYMMDD-HHMM" in r for r in refusals), refusals


def test_the_id_must_carry_a_timestamp_because_that_is_the_order(world):
    """Oldest-first is a property of the NAME, not of mtime -- a file
    copied or restored gets a new mtime and 'oldest' quietly changes."""
    data = {**GOOD_TICKET, "id": "widen-the-widget"}
    path = put(data)
    assert any("YYYYMMDD-HHMM" in r for r in inbox.load(path).refusals)


def test_the_id_must_match_the_filename(world):
    path = put(GOOD_TICKET, name="20261105-0901-something-else.json")
    assert any("does not match the filename" in r
               for r in inbox.load(path).refusals)


def test_autonomous_must_be_yes(world):
    path = put({**GOOD_TICKET, "autonomous": "maybe"})
    assert any("autonomous is not 'yes'" in r
               for r in inbox.load(path).refusals)


def test_acceptance_must_be_a_non_empty_list(world):
    path = put({**GOOD_TICKET, "acceptance": "it works"})
    assert any("acceptance must be a non-empty list" in r
               for r in inbox.load(path).refusals)


def test_a_ticket_with_no_files_has_no_scope_and_is_refused(world):
    """★ THERE IS NO WIDER DEFAULT. The ticket's own files list IS the
    fence for this channel, so an empty one is not 'anything'."""
    path = put({**GOOD_TICKET, "files": []})
    refusals = inbox.load(path).refusals
    assert any("files must name the scope" in r for r in refusals), refusals


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.py", "a/../../b.py"])
def test_a_path_outside_the_repo_is_refused(world, bad):
    path = put({**GOOD_TICKET, "files": [bad]})
    assert any("not a repo-relative path" in r
               for r in inbox.load(path).refusals)


@pytest.mark.parametrize("bad,mark", [
    (".env", "credentials"),
    (".env.example", "credentials"),
    ("cardfoundry.db", "database"),
    ("backups/nightly.sql", "backups"),
    ("imports/consignor.csv", "operator-provided"),
    ("audits/2026.jsonl", "append-only"),
    ("claude/autodev/config.json", "mailbox"),
    (".git/config", "plumbing"),
])
def test_the_absolute_denylist_is_absolute(world, bad, mark):
    """★ THE ONE THING THE TICKET'S OWN FILES LIST MAY NOT OVERRIDE. Every
    other path question is the ticket's to answer; these are not, however
    the ticket is approved."""
    path = put({**GOOD_TICKET, "files": [bad]})
    refusals = inbox.load(path).refusals
    assert any("may never be touched unattended" in r for r in refusals), refusals
    assert any(mark in r for r in refusals), refusals


def test_a_ticket_may_declare_a_path_autodevs_policy_forbids(world):
    """★ THE WHOLE REASON THIS CHANNEL EXISTS, as a test rather than a
    comment. policy.py reserves the deploy guard and order_service.py for
    the operator, which is right for the unattended five-minute builder
    and is why it could not have taken two of the three items shipped in
    v2.110.0. An inbox ticket may declare them -- and policy.py is
    untouched, which the second half asserts."""
    from scripts.autodev import policy

    for path_name in ("scripts/hooks/pre-push", "order_service.py",
                      "VERSION", "CHANGELOG.md"):
        path = put({**GOOD_TICKET, "files": [path_name]})
        assert inbox.load(path).refusals == [], path_name
        assert policy._forbidden_reason(path_name), (
            f"{path_name} must still be forbidden to AutoDev")
        path.unlink()


# --------------------------------------------------------------------------
# The authority gate -- approvals.py, unchanged
# --------------------------------------------------------------------------

def test_a_ticket_with_no_approval_note_is_refused(world):
    put(GOOD_TICKET)
    candidate, refused = inbox.next_runnable()
    assert candidate is None
    assert refused and not refused[0].is_runnable


def test_an_approved_ticket_is_runnable(world):
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    candidate, refused = inbox.next_runnable()
    assert candidate is not None and candidate.id == TICKET_ID
    assert refused == []


def test_a_digest_mismatch_invalidates_the_approval(world):
    """★ EDITING A TICKET AFTER THE OK INVALIDATES IT. The same property
    AutoDev's queue has, through the same unchanged gate."""
    put(GOOD_TICKET)
    approve(digest="0" * 64)
    candidate, refused = inbox.next_runnable()
    assert candidate is None
    assert any("changed since it was approved" in reason
               for reason in refused[0].refusals), refused[0].refusals


def test_an_unknown_approval_basis_authorises_nothing(world):
    """A typo or an invented rule id is not an authority: STANDING_BASES
    is a closed set and this gate fails closed."""
    put(GOOD_TICKET)
    approve({**GOOD_NOTE,
             "approval_basis": "standing-low-risk-2027-01-01",
             "low_risk_assessment": "a comment-only change, nothing else"},
            ticket=GOOD_TICKET)
    candidate, refused = inbox.next_runnable()
    assert candidate is None
    assert any("basis" in reason for reason in refused[0].refusals), \
        refused[0].refusals


def test_the_known_standing_basis_still_works_here(world):
    put(GOOD_TICKET)
    approve({**GOOD_NOTE,
             "operator_words": approvals.STANDING_BASES[
                 "standing-low-risk-2026-11-04"]["operator_words"],
             "approval_basis": "standing-low-risk-2026-11-04",
             "low_risk_assessment":
                 "one comment line in one service; no money, marketplace, "
                 "schema, JS or buyer-facing surface"},
            ticket=GOOD_TICKET)
    candidate, _ = inbox.next_runnable()
    assert candidate is not None


# --------------------------------------------------------------------------
# Ordering, atomicity, and moving tickets aside
# --------------------------------------------------------------------------

def test_the_oldest_ticket_goes_first_and_the_order_is_in_the_name(world):
    late = {**GOOD_TICKET, "id": "20261105-1700-later-work"}
    early = {**GOOD_TICKET, "id": "20261101-0800-earlier-work"}
    put(late)
    put(early)
    approve({**GOOD_NOTE, "task": late["id"]}, ticket=late)
    approve({**GOOD_NOTE, "task": early["id"]}, ticket=early)
    candidate, _ = inbox.next_runnable()
    assert candidate.id == "20261101-0800-earlier-work"


def test_a_half_written_ticket_is_never_seen_under_its_real_name(world):
    """★ WHY atomic_write EXISTS. A reader must never match a file still
    being written, so the bytes land beside and are renamed into place."""
    part = inbox.inbox_dir() / f"{TICKET_ID}.json.part"
    part.write_text('{"id": "incomp')
    assert inbox.pending() == []
    inbox.atomic_write(inbox.inbox_dir() / f"{TICKET_ID}.json",
                       json.dumps(GOOD_TICKET))
    assert [p.name for p in inbox.pending()] == [f"{TICKET_ID}.json"]


def test_write_ticket_leaves_no_part_file_behind(world):
    path = inbox.write_ticket(GOOD_TICKET)
    assert path.exists()
    assert list(inbox.inbox_dir().glob("*.part")) == []


def test_a_rejected_ticket_is_moved_with_its_reasons_and_kept_intact(world):
    """Moved, not deleted and not left: left in place it is re-read and
    re-reported every firing, deleted the evidence of what arrived is
    gone."""
    original = json.dumps({**GOOD_TICKET, "autonomous": "maybe"})
    path = inbox.atomic_write(inbox.inbox_dir() / f"{TICKET_ID}.json", original)
    moved = inbox.reject(inbox.load(path))
    assert not path.exists()
    assert moved.parent == inbox.rejected_dir()
    assert moved.read_text() == original, "the ticket itself must be unchanged"
    refusal = moved.with_name(moved.stem + ".refusal.txt").read_text()
    assert "autonomous is not 'yes'" in refusal
    assert "nothing was built" in refusal.lower()


def test_rejecting_twice_never_overwrites_the_first_record(world):
    for _ in range(2):
        path = inbox.atomic_write(inbox.inbox_dir() / f"{TICKET_ID}.json",
                                  json.dumps({**GOOD_TICKET, "files": []}))
        inbox.reject(inbox.load(path))
    assert len(list(inbox.rejected_dir().glob("*.json"))) == 2


def test_a_report_never_overwrites_an_existing_one(world):
    now = datetime(2026, 11, 5, 9, 30)
    first = inbox.file_report("same topic", "done", "first", now=now)
    second = inbox.file_report("same topic", "done", "second", now=now)
    assert first != second
    assert first.read_text().endswith("first\n")
    assert second.read_text().endswith("second\n")


def test_a_report_carries_the_header_and_the_status_line(world):
    path = inbox.file_report("widen the widget", "done", "the block")
    text = path.read_text()
    assert text.startswith("◀ FROM CODE-AUTODEV → CHAT-AUTODEV · widen the widget\n")
    assert "\nSTATUS: done\n" in text


# --------------------------------------------------------------------------
# The runner: its switches, its lock, and one real pass
# --------------------------------------------------------------------------

def test_the_runner_does_nothing_while_the_switch_is_off(world):
    (world["home"] / "config.json").write_text(json.dumps(
        {"inbox_enabled": False, "base_branch": "main"}))
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)
    assert code == 0 and report.enabled is False and report.ran is None
    assert inbox.pending(), "the ticket must still be waiting"


def test_the_switch_defaults_to_off_even_in_an_older_config(world):
    """★ IT SHIPS DISABLED. A config.json written before this existed has
    no such key, and the default must be false rather than absent."""
    (world["home"] / "config.json").write_text(json.dumps(
        {"base_branch": "main", "scheduling_enabled": True}))
    assert coordinator.config()["inbox_enabled"] is False
    assert coordinator.DEFAULT_CONFIG["inbox_enabled"] is False


@pytest.mark.parametrize("sentinel", [state.STOP_FILE, state.PAUSE_FILE])
def test_the_existing_kill_switches_stop_the_inbox_too(world, sentinel):
    (state.home() / sentinel).write_text("")
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)
    assert code == 0 and report.ran is None
    assert inbox.pending()


def test_a_second_runner_does_nothing_while_the_lock_is_held(world):
    """★ ONE TICKET AT A TIME, ENFORCED BY THE KERNEL. The same flock
    AutoDev's coordinator takes, so an inbox run and an AutoDev task can
    never both be editing this repository."""
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    with state.single_owner_lock():
        code, report = inbox_runner.run_once(stub_ok=True)
    assert code == 0 and report.ran is None
    assert inbox.pending(), "the ticket must be untouched"
    assert reports() == []


def test_an_idle_pass_touches_nothing(world):
    code, report = inbox_runner.run_once(stub_ok=True)
    assert code == 0 and report.ran is None and report.rejected == []
    assert reports() == []


def test_one_pass_works_the_ticket_files_the_report_and_moves_it(world):
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)

    assert code == 0, report.as_dict()
    assert report.ran == TICKET_ID and report.outcome == "done"
    assert report.branch == f"inbox/{TICKET_ID}"

    # the ticket is in done/, not in the inbox
    assert inbox.pending() == []
    assert (inbox.done_dir() / f"{TICKET_ID}.json").exists()

    # one report, with the header, the status and the session's own words
    filed = reports()
    assert len(filed) == 1
    text = filed[0].read_text()
    assert text.startswith("◀ FROM CODE-AUTODEV → CHAT-AUTODEV")
    assert "\nSTATUS: done\n" in text
    assert "Widened the widget. Suite: 1 passed." in text
    # the status line is lifted out of the block, not printed twice
    assert text.count("STATUS: done") == 1
    # and the runner's own evidence is there to check the claim against
    assert "RUNNER EVIDENCE" in text
    assert f"inbox/{TICKET_ID}" in text
    assert "NOT pushed and NOT released" in text


def test_the_work_lands_on_its_own_branch_and_not_on_main(world):
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    inbox_runner.run_once(stub_ok=True)
    assert "WIDER" not in (world["repo"] / "widget.py").read_text()
    out = _git(["log", "--oneline", f"main..inbox/{TICKET_ID}"],
               world["repo"]).stdout
    assert "widen the widget" in out


def test_a_report_with_no_status_is_filed_as_blocked(world, monkeypatch):
    """A report that does not say how it ended has not said it went well."""
    monkeypatch.setenv("STUB_INBOX_MODE", "nostatus")
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)
    # 0 because the PASS worked; the ticket's outcome is in the report.
    assert code == 0 and report.outcome == "blocked"
    assert "\nSTATUS: blocked\n" in reports()[0].read_text()


def test_a_session_claiming_success_after_a_failed_process_is_overridden(
        world, monkeypatch):
    """★ TRUST THE PROCESS, NOT THE PROSE. A report claiming success from
    a run that failed is the one thing that must not be filed as
    success."""
    monkeypatch.setenv("STUB_INBOX_MODE", "liar")
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)
    assert report.outcome == "blocked" and code == 0
    text = reports()[0].read_text()
    assert "overrode STATUS: done" in text


def test_a_usage_limit_leaves_the_ticket_in_the_inbox(world, monkeypatch):
    """★ NEVER HALF-FINISHED. CLAUDE.md: stop before pushing on a usage
    limit. This runner never pushes, so there is no release to leave half
    applied -- and the ticket stays put so the next pass picks it up."""
    monkeypatch.setenv("STUB_INBOX_MODE", "ratelimit")
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)
    assert code == 3
    assert inbox.pending(), "the ticket must not be moved to done"
    assert "\nSTATUS: blocked\n" in reports()[0].read_text()


def test_a_bad_ticket_in_front_does_not_block_a_good_one_behind(world):
    """The bad one is rejected and reported; the good one still runs on
    the same pass. A queue of refusals must not be a stuck queue."""
    bad = {**GOOD_TICKET, "id": "20261101-0800-bad-ticket", "files": []}
    put(bad)
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)
    assert report.rejected == ["20261101-0800-bad-ticket"]
    assert report.ran == TICKET_ID and code == 0
    assert len(reports()) == 2


def test_the_prompt_is_built_from_the_fields_and_names_claude_md(world):
    """★ DATA READ ALOUD, NOT AN INSTRUCTION FILE. The frame is written
    by this program; the ticket only fills named fields in it."""
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    candidate, _ = inbox.next_runnable()
    prompt = inbox_runner.render_prompt(candidate, Path("/wt"), "inbox/x")
    assert "Read CLAUDE.md" in prompt
    assert "widget.py" in prompt
    assert "cannot push, tag, deploy" in prompt
    assert "hard stop" in prompt.lower()
    assert "STATUS: done" in prompt


def test_the_inbox_role_cannot_push_tag_or_touch_railway(world):
    """Asserted on the SETTINGS FILE the runner actually loads, not on a
    rule written down somewhere."""
    data = json.loads(agents.INBOX_SETTINGS_PATH.read_text())
    deny = data["permissions"]["deny"]
    for rule in ("Bash(git push:*)", "Bash(git tag:*)", "Bash(railway:*)",
                 "Bash(gh:*)", "Read(./.env)", "Agent"):
        assert rule in deny, rule
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"] == "block"


def test_the_inbox_role_runs_with_a_turn_limit():
    """No `world` fixture on purpose: that one stubs build_command, and
    this has to prove the REAL built command carries the flag. --max-turns
    is the only thing bounding how long an unwatched session keeps going."""
    assert agents.DEFAULT_MAX_TURNS["inbox"] > 0
    assert agents.max_turns_preflight() == []
    cmd = agents.build_command("inbox", "do it", Path("/tmp"))
    assert "--max-turns" in cmd
    assert str(agents.INBOX_SETTINGS_PATH) not in cmd, (
        "the runner passes the inbox settings explicitly; build_command "
        "must not assume them")


# --------------------------------------------------------------------------
# The guard: the inbox session's fence
# --------------------------------------------------------------------------

def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def edit(path: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": path}}


def test_the_inbox_session_may_run_the_whole_suite_and_the_developer_may_not(
        monkeypatch):
    """★ THE ONLY DIFFERENCE BETWEEN THE TWO ALLOWLISTS IS SCOPE.
    CLAUDE.md requires the full suite before a change is finished, and
    the inbox session has no coordinator gate in front of it to run one
    for it."""
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_TESTS", "tests/test_x.py")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    assert guard.decide(bash("PYTHONPATH=. pytest -q"))[0] is True
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "developer")
    allowed, reason = guard.decide(bash("PYTHONPATH=. pytest -q"))
    assert allowed is False and "whole suite" in reason


@pytest.mark.parametrize("command", [
    "git push origin main",
    "git tag -a v9.9.9 -m x",
    "git push --no-verify",
    "railway variables",
    "gh pr create",
    "sqlite3 cardfoundry.db 'PRAGMA integrity_check;'",
    "python backfill_everything.py --confirm",
    "cat .env",
])
def test_the_denylist_binds_the_inbox_session_exactly_as_it_binds_the_developer(
        monkeypatch, command):
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_FILES", "widget.py")
    allowed, _ = guard.decide(bash(command))
    assert allowed is False, command


def test_a_role_with_no_name_still_has_no_shell(monkeypatch):
    monkeypatch.delenv("CARDFOUNDRY_AUTODEV_ROLE", raising=False)
    allowed, reason = guard.decide(bash("ls"))
    assert allowed is False and "no shell" in reason


def test_the_inbox_session_may_edit_only_what_its_ticket_declared(monkeypatch):
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_FILES",
                       "scripts/hooks/pre-push:order_service.py")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_TESTS", "tests/test_x.py")
    # ★ Paths policy.py forbids outright, allowed here because the ticket
    # declared them. That is the inbox's whole reason for existing.
    assert guard.decide(edit("scripts/hooks/pre-push"))[0] is True
    assert guard.decide(edit("order_service.py"))[0] is True
    assert guard.decide(edit("tests/test_x.py"))[0] is True
    allowed, reason = guard.decide(edit("main.py"))
    assert allowed is False and "declared" in reason


def test_the_inbox_session_with_no_declared_paths_may_write_nothing(monkeypatch):
    """Fails closed: a runner that forgot to pass the ticket's files
    blocks the session rather than freeing it."""
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    monkeypatch.delenv("CARDFOUNDRY_AUTODEV_TICKET_FILES", raising=False)
    monkeypatch.delenv("CARDFOUNDRY_AUTODEV_TICKET_TESTS", raising=False)
    allowed, reason = guard.decide(edit("widget.py"))
    assert allowed is False and "nothing" in reason


def test_the_absolute_denylist_binds_the_guard_too(monkeypatch):
    """Belt and braces: the ticket could not have declared .env and been
    accepted, so a run where the environment says otherwise stops."""
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_FILES", ".env")
    allowed, reason = guard.decide(edit(".env"))
    assert allowed is False and "absolutely denied" in reason


def test_the_developer_fence_is_unchanged(monkeypatch):
    """policy.py still decides for AutoDev, and nothing here widened it."""
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "developer")
    allowed, reason = guard.decide(edit("scripts/hooks/pre-push"))
    assert allowed is False and "protected path" in reason


# --------------------------------------------------------------------------
# The front door
# --------------------------------------------------------------------------

def test_validate_ticket_inbox_is_the_front_door_and_writes_nothing(world):
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    before = _snapshot(world)
    row = validate_ticket.inspect_inbox(TICKET_ID)
    assert row["runnable"] is True
    assert row["ticket_digest"] == inbox.ticket_digest(GOOD_TICKET)
    assert _snapshot(world) == before, "the front door must write nothing"


def test_validate_ticket_inbox_names_every_refusal(world):
    put({**GOOD_TICKET, "autonomous": "maybe", "files": []})
    row = validate_ticket.inspect_inbox(TICKET_ID)
    assert row["runnable"] is False
    assert len(row["policy_refusals"]) >= 2
    assert row["approved"] is False


def test_the_inbox_cli_lists_without_writing(world, capsys):
    put(GOOD_TICKET)
    before = _snapshot(world)
    assert inbox.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tickets"][0]["id"] == TICKET_ID
    assert _snapshot(world) == before


def _snapshot(world) -> dict:
    """Every file under the home and the mailbox, with its bytes."""
    out = {}
    for root in (world["home"], world["mailbox"]):
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[str(path)] = path.read_bytes()
    return out


# --------------------------------------------------------------------------
# The fixes the first real ticket asked for (2026-11-05)
# --------------------------------------------------------------------------

def test_a_pass_that_worked_exits_zero_whatever_the_ticket_outcome(world):
    """★ WHY THIS IS PINNED. The first real inbox ticket ran correctly,
    committed, and honestly filed `blocked` because it could not run the
    suite -- and the runner returned 1, so launchd recorded "last exit
    code = 1" and the next investigation went looking for a crash that
    had never happened. A pass that picked up a ticket, worked it and
    filed a report succeeded AS A PASS."""
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, report = inbox_runner.run_once(stub_ok=True)
    assert code == 0 and report.outcome == "done"


def test_only_a_failed_pass_exits_non_zero(world, monkeypatch):
    monkeypatch.setenv("STUB_INBOX_MODE", "ratelimit")
    put(GOOD_TICKET)
    approve(ticket=GOOD_TICKET)
    code, _ = inbox_runner.run_once(stub_ok=True)
    assert code == 3, "a usage limit is a failed pass, not a finished one"


def test_the_inbox_session_may_run_the_suite_with_the_venv_interpreter(
        monkeypatch):
    """★ WHAT ACTUALLY BLOCKED THE FIRST REAL TICKET, and where. The guard
    always allowed this command -- its pattern matches an interpreter
    named by path. What refused was the PERMISSION layer, whose rules are
    literal command prefixes, so `Bash(python:*)` does not cover
    `.venv/bin/python`. Both spellings the session tried are pinned here."""
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_FILES", "AGENTS.md")
    for command in (".venv/bin/python -m pytest -q",
                    "PYTHONPATH=. .venv/bin/python -m pytest -q"):
        allowed, reason = guard.decide(bash(command))
        assert allowed is True, (command, reason)
        assert any(command.startswith(rule[len("Bash("):-len(":*)")])
                   for rule in agents.ROLE_ALLOWED_TOOLS["inbox"]
                   if rule.startswith("Bash(") and rule.endswith(":*)")), \
            f"no permission rule covers {command!r}"


def test_the_two_permission_layers_hold_the_same_rules(world):
    """★ TWO LAYERS, ONE DEFINITION. The settings file and --allowedTools
    are different mechanisms, not two copies of one list, and both have
    to allow the command or `--permission-prompts none` denies it. Since
    neither can be proved without a real session, both carry the rules --
    and this test is what stops them drifting."""
    data = json.loads(agents.INBOX_SETTINGS_PATH.read_text())
    assert data["permissions"]["allow"] == list(
        agents.INBOX_EXTRA_ALLOWED_TOOLS)
    for rule in agents.INBOX_EXTRA_ALLOWED_TOOLS:
        assert rule in agents.ROLE_ALLOWED_TOOLS["inbox"]


def test_allowing_the_suite_widened_nothing_else(world):
    """Every other denial is unchanged, and the developer did not gain
    the venv interpreter along the way."""
    data = json.loads(agents.INBOX_SETTINGS_PATH.read_text())
    for rule in ("Bash(sqlite3:*)", "Bash(git push:*)", "Bash(git tag:*)",
                 "Bash(railway:*)", "Bash(gh:*)", "Bash(curl:*)",
                 "Read(./.env)", "Agent", "WebFetch", "WebSearch"):
        assert rule in data["permissions"]["deny"], rule
    for rule in agents.INBOX_EXTRA_ALLOWED_TOOLS:
        assert rule not in agents.ROLE_ALLOWED_TOOLS["developer"]


def test_sqlite3_is_still_denied_to_the_inbox_session(monkeypatch):
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_FILES", "AGENTS.md")
    allowed, _ = guard.decide(
        bash(".venv/bin/python -m pytest -q && sqlite3 cardfoundry.db '.q'"))
    assert allowed is False, "a denied segment anywhere still blocks"


def test_a_commit_message_with_a_newline_is_refused_and_repeated_m_works(
        monkeypatch):
    """The quirk the first real ticket found, pinned so the documented
    advice and the guard cannot disagree."""
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_ROLE", "inbox")
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_TICKET_FILES", "AGENTS.md")
    assert guard.decide(bash('git commit -m "one\ntwo"'))[0] is False
    assert guard.decide(bash('git commit -m "one" -m "two"'))[0] is True


def test_a_late_pass_says_so_and_an_on_time_pass_does_not(world):
    """★ AN ABSENCE THAT LEAVES NO ROW LOOKS LIKE A WORKING SYSTEM. On
    2026-11-05 nothing ran for 79 minutes and the log recorded it as two
    ordinary lines far apart. The cause was the Mac asleep -- correct
    behaviour, no defect -- but it cost an investigation."""
    import os
    import time

    log = inbox_runner.log_path()
    log.write_text("an earlier pass said something\n")
    stale = time.time() - (
        inbox_runner.LATE_PASS_INTERVALS * inbox_runner.PASS_INTERVAL_SECONDS
        + 600)
    os.utime(log, (stale, stale))
    inbox_runner.run_once(stub_ok=True)
    assert "first pass in" in log.read_text()

    log.write_text("a recent pass said something\n")
    inbox_runner.run_once(stub_ok=True)
    assert "first pass in" not in log.read_text()


def test_the_launch_agent_interval_is_the_runner_s_own(world):
    """One number, read by the installer from the runner, so what launchd
    was given and what a pass measures its lateness against agree."""
    from scripts.autodev import install_inbox

    assert install_inbox.INTERVAL_SECONDS == inbox_runner.PASS_INTERVAL_SECONDS
    assert install_inbox.plist_body()["StartInterval"] == \
        inbox_runner.PASS_INTERVAL_SECONDS
