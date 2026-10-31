"""The coordinator, driven for real against a STUB Claude CLI.

★ WHY A STUB AND A TINY REPO. Requirement 11 of the operator's approval
is that the coordinator be tested before any real agent is invoked, and
CLAUDE.md forbids a test that touches the network. So these tests run the
REAL coordinator -- the real lock, the real state machine, the real
audit log, the real gates, the real git worktree -- against a stub binary
standing in for `claude`, over a throwaway git repository small enough
that the gates finish in milliseconds. Nothing here is mocked at the
coordinator's own layer, which is the layer being tested.

★ WHAT THESE PROVE, in the operator's terms: that an interrupted run
resumes, that a second coordinator cannot start, that a refused ticket
writes nothing, that the daily cap holds, that a rejected review does not
ship, that a diff outside the ticket's files is caught, and that an
unauthenticated CLI stops the run instead of grinding.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.autodev import agents, coordinator, gates, policy, state

STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({
        "loggedIn": os.environ.get("STUB_AUTH", "1") == "1",
        "authMethod": "claude.ai", "subscriptionType": "max",
    }))
    raise SystemExit(0)

blob = " ".join(args)
prompt = ""
for index, value in enumerate(args):
    if value == "-p":
        prompt = args[index + 1]
        break
brief = {}
if "{" in prompt:
    try:
        brief = json.loads(prompt[prompt.index("{"):])
    except ValueError:
        brief = {}


def emit(structured=None, text="stub", is_error=False):
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error,
        "session_id": "stub-session-1", "result": text,
        "structured_output": structured, "total_cost_usd": 0, "num_turns": 1,
    }))


ticket = brief.get("ticket") or {}
files = ticket.get("files") or []

if "Assess this CardFoundry ticket" in prompt:
    emit({
        "verdict": os.environ.get("STUB_MANAGER", "ready"),
        "reason": "stub manager",
        "acceptance_criteria": ["the parsed meaning of the file is unchanged"],
        "expected_files": files,
        "notes_for_developer": "append one comment line",
    })
elif "Implement this ticket" in prompt:
    if os.environ.get("STUB_RATE_LIMIT") == "1":
        emit(None, "Rate limit exceeded; try again later", is_error=True)
        raise SystemExit(0)
    mode = os.environ.get("STUB_DEV_MODE", "comment")
    target = Path(files[0]) if files else Path("widget.py")
    if mode == "comment":
        target.write_text(target.read_text() + "# a corrected comment line\n")
    elif mode == "noncomment":
        target.write_text(target.read_text() + "REAL_LINE = 1\n")
    elif mode == "outofscope":
        Path("undeclared.py").write_text("X = 1\n")
    elif mode == "broken":
        target.write_text(target.read_text() + "def (:\n")
    if mode != "nodiff":
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m", "stub: " + mode], check=True,
                       capture_output=True)
    emit(None, "stub developer applied mode=" + mode)
elif "Review this change" in prompt:
    emit({
        "verdict": os.environ.get("STUB_REVIEWER", "approve"),
        "summary": "stub reviewer",
        "criteria": [{"criterion": "stub", "met": True}],
        "findings": [],
    })
else:
    emit(None, "stub saw an unknown role")
'''

PILOT_TICKET = {
    "id": "pilot-comment",
    "title": "Correct a wrong reason in a config comment",
    "autonomous": "yes",
    "eligibility_reviewed_by": "cowork",
    "eligibility_reviewed_at": "2026-10-31",
    "intent": "The comment states a reason that is not true.",
    "acceptance": ["the file's non-comment lines are unchanged"],
    "files": ["guarded-crons"],
    "tests": [],
    "risk_flags": [],
    "comment_only_paths": ["guarded-crons"],
}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A throwaway repo, a stub CLI and a private AutoDev home."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "guarded-crons").write_text(
        "# a comment that gives the wrong reason\n25 1,6 pricing\n"
    )
    (repo / "widget.py").write_text("VALUE = 1\n")
    # Mirrors the real repo: without this, `git add -A` in a later
    # attempt sweeps up whatever the gates left behind.
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n*.db\n")
    (repo / "tests" / "test_widget.py").write_text(
        "def test_value():\n    assert True\n"
    )
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "autodev@test"], repo)
    _git(["config", "user.name", "autodev"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial"], repo)

    stub = tmp_path / "stub-claude"
    stub.write_text(STUB)
    stub.chmod(0o755)

    autodev_home = tmp_path / "autodev"
    worktrees = tmp_path / "worktrees"
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_HOME", str(autodev_home))
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_WORKTREES", str(worktrees))
    monkeypatch.setenv("CARDFOUNDRY_AUTODEV_CLAUDE", f"{sys.executable} {stub}")
    monkeypatch.setattr(state, "REPO_ROOT", repo)
    monkeypatch.setattr(agents, "claude_bin", lambda: str(stub))
    monkeypatch.setattr(
        agents, "build_command",
        lambda role, prompt, worktree, **kw: [
            sys.executable, str(stub), "-p", prompt, "--role", role,
        ],
    )
    monkeypatch.setattr(
        agents, "preflight_auth",
        lambda: ((os.environ.get("STUB_AUTH", "1") == "1"),
                 "stub auth status"),
    )
    state.ensure_home()
    (autodev_home / "queue" / "pilot-comment.json").write_text(
        json.dumps(PILOT_TICKET)
    )
    return {"repo": repo, "home": autodev_home, "stub": stub,
            "worktrees": worktrees}


def run(task="pilot-comment", **env):
    for key, value in env.items():
        os.environ[key] = value
    try:
        with state.single_owner_lock():
            return coordinator.run_task(task)
    finally:
        for key in env:
            os.environ.pop(key, None)


def test_a_clean_task_ends_on_its_own_branch_with_nothing_pushed(world):
    assert run() == 0
    worktree = world["worktrees"] / "pilot-comment"
    head = _git(["rev-parse", "HEAD"], worktree).stdout.strip()
    branch = _git(["symbolic-ref", "--short", "HEAD"], worktree).stdout.strip()
    assert branch == "autodev/pilot-comment"
    assert head
    # main is untouched: the operator's branch never moved.
    main_head = _git(["rev-parse", "main"], world["repo"]).stdout.strip()
    assert main_head != head
    assert _git(["log", "--oneline", "main"], world["repo"]).stdout.count("\n") == 1
    # and there is no remote, so nothing could have been pushed anywhere.
    assert _git(["remote"], world["repo"]).stdout.strip() == ""


def test_the_gates_really_ran_and_their_evidence_is_on_disk(world):
    assert run() == 0
    evidence = world["home"] / "runs" / "pilot-comment" / "1"
    verdict = json.loads((evidence / "gates.json").read_text())
    names = {result["name"] for result in verdict["results"]}
    assert {"git_diff_check", "diff_scope", "comment_only", "compileall",
            "pytest_full"} <= names
    assert verdict["ok"] is True
    assert (evidence / "pytest-full.txt").read_text().strip()
    assert (evidence / "diff.patch").read_text().startswith("diff --git")


def test_a_skipped_gate_is_reported_as_skipped_not_as_a_pass(world):
    assert run() == 0
    verdict = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "1" / "gates.json").read_text()
    )
    # No cardfoundry.db and no changelog_guard.py in this throwaway repo.
    assert set(verdict["skipped"]) == {"changelog_guard", "sqlite_integrity"}


def test_the_audit_log_pairs_every_phase_and_records_the_outcome(world):
    assert run() == 0
    rows = state.read_audit()
    started = [r["phase"] for r in rows if r["event"] == "phase_started"]
    finished = [r["phase"] for r in rows if r["event"] == "phase_finished"]
    assert started == ["manager", "developer", "gate", "reviewer"]
    assert finished == started
    outcome = [r for r in rows if r["event"] == "task_outcome"][-1]
    assert outcome["passed"] is True


def test_a_rejected_review_does_not_count_as_a_finished_task(world):
    assert run(STUB_REVIEWER="reject") == 1
    assert state.State.load().data["tasks_today"] == 0
    escalations = list((world["home"] / "escalations").glob("*.md"))
    assert escalations and "Gates or review" in escalations[0].read_text()


def test_a_manager_that_wants_a_decision_parks_the_task(world):
    assert run(STUB_MANAGER="needs_decision") == 1
    text = "\n".join(p.read_text() for p in
                     (world["home"] / "escalations").glob("*.md"))
    assert "needs_decision" in text
    # No developer phase ever started.
    assert not any(r.get("phase") == "developer" for r in state.read_audit())


def test_a_diff_outside_the_tickets_files_fails_the_gate(world):
    assert run(STUB_DEV_MODE="outofscope") == 1
    verdict = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "1" / "gates.json").read_text()
    )
    scope = [r for r in verdict["results"] if r["name"] == "diff_scope"][0]
    assert scope["ok"] is False
    assert "undeclared.py" in scope["detail"]


def test_a_non_comment_change_to_a_comment_only_path_fails_the_gate(world):
    assert run(STUB_DEV_MODE="noncomment") == 1
    verdict = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "1" / "gates.json").read_text()
    )
    only = [r for r in verdict["results"] if r["name"] == "comment_only"][0]
    assert only["ok"] is False


def test_broken_python_fails_compileall(world):
    ticket = {**PILOT_TICKET, "files": ["widget.py"], "comment_only_paths": []}
    (world["home"] / "queue" / "pilot-comment.json").write_text(json.dumps(ticket))
    assert run(STUB_DEV_MODE="broken") == 1
    verdict = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "1" / "gates.json").read_text()
    )
    assert [r for r in verdict["results"] if r["name"] == "compileall"][0]["ok"] is False


def test_a_developer_that_changes_nothing_is_parked(world):
    assert run(STUB_DEV_MODE="nodiff") == 1
    assert any("no diff" in p.read_text() or "no diff" in p.name
               for p in (world["home"] / "escalations").glob("*.md"))


def test_an_ineligible_ticket_writes_nothing_at_all(world):
    (world["home"] / "queue" / "bad.json").write_text(json.dumps(
        {**PILOT_TICKET, "id": "bad", "files": ["models.py"],
         "comment_only_paths": []}
    ))
    assert run("bad") == 1
    assert not (world["worktrees"] / "bad").exists()
    assert not any(r["event"] == "phase_started" for r in state.read_audit())


def test_an_unauthenticated_cli_stops_before_any_worktree(world):
    assert run(STUB_AUTH="0") == 3
    assert not (world["worktrees"] / "pilot-comment").exists()
    text = "\n".join(p.read_text() for p in
                     (world["home"] / "escalations").glob("*.md"))
    assert "cannot authenticate" in text


def test_a_rate_limit_halts_the_run_and_does_not_retry(world):
    with pytest.raises(coordinator.Halt):
        run(STUB_RATE_LIMIT="1")
    rows = state.read_audit()
    assert any(r["event"] == "phase_halted" and r["error"] == "RateLimited"
               for r in rows)
    assert state.State.load().data["halted_reason"].startswith("RateLimited")
    # One developer attempt only: nothing retried into a closed limit.
    assert sum(1 for r in rows
               if r["event"] == "phase_started" and r["phase"] == "developer") == 1


def test_pause_blocks_a_run_and_resume_clears_it(world):
    coordinator.cmd_pause(None)
    assert run() == 2
    coordinator.cmd_resume(type("A", (), {"clear_stop": False})())
    assert run() == 0


def test_stop_blocks_a_run_until_cleared_explicitly(world):
    coordinator.cmd_stop(None)
    assert run() == 2
    coordinator.cmd_resume(type("A", (), {"clear_stop": False})())
    assert run() == 2, "resume without --clear-stop must not clear STOP"
    coordinator.cmd_resume(type("A", (), {"clear_stop": True})())
    assert run() == 0


def test_the_daily_cap_is_four_and_holds(world):
    st = state.State.load()
    st.roll_day()
    st.data["tasks_today"] = 4
    st.save()
    assert coordinator.config()["max_tasks_per_day"] == 4
    assert run() == 2


def test_automatic_continuation_and_scheduling_are_off_by_default(world):
    cfg = coordinator.config()
    assert cfg["auto_continue"] is False
    assert cfg["scheduling_enabled"] is False


def test_a_second_coordinator_cannot_start(world):
    with state.single_owner_lock():
        with pytest.raises(state.CoordinatorBusy):
            with state.single_owner_lock():
                pass


def test_the_lock_is_released_when_the_holder_dies(world, tmp_path):
    """A killed coordinator must not wedge the system for ever."""
    script = tmp_path / "holder.py"
    script.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, %r)\n" % str(state.REPO_ROOT.parents[0]) +
        "from scripts.autodev import state\n"
        "ctx = state.single_owner_lock(); ctx.__enter__()\n"
        "print('held', flush=True)\n"
        "time.sleep(30)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    proc = subprocess.Popen([sys.executable, str(script)], env=env,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    with pytest.raises(state.CoordinatorBusy):
        with state.single_owner_lock():
            pass
    proc.kill()
    proc.wait(timeout=10)
    with state.single_owner_lock():
        pass  # the OS released it; no stale pid file to clean up


def test_an_interrupted_phase_is_detected_and_the_attempt_increments(world):
    st = state.State.load()
    st.data.update({"active_task": "pilot-comment", "phase": "developer",
                    "attempt": 1, "phase_started_at": "2026-10-31T04:00:00"})
    st.save()
    assert run() == 0
    rows = state.read_audit()
    crash = [r for r in rows if r["event"] == "crash_detected"]
    assert crash and crash[0]["phase"] == "developer"
    assert any(r.get("attempt") == 2 for r in rows if r["event"] == "phase_started")


def test_the_retry_limit_parks_the_task(world):
    st = state.State.load()
    st.data.update({"active_task": "pilot-comment", "phase": "developer",
                    "attempt": 2, "phase_started_at": "2026-10-31T04:00:00"})
    st.save()
    assert run() == 1
    assert any("Retry limit" in p.read_text()
               for p in (world["home"] / "escalations").glob("*.md"))


def test_another_task_mid_flight_refuses_a_new_one(world):
    (world["home"] / "queue" / "other.json").write_text(
        json.dumps({**PILOT_TICKET, "id": "other"})
    )
    st = state.State.load()
    st.data.update({"active_task": "pilot-comment", "phase": "developer",
                    "attempt": 1, "phase_started_at": "2026-10-31T04:00:00"})
    st.save()
    assert run("other") == 2


def test_no_credential_reaches_a_child_process(monkeypatch):
    for key in ("MANAPOOL_EMAIL", "MANAPOOL_API_TOKEN", "ANTHROPIC_API_KEY",
                "CARDFOUNDRY_SERVICE_PASSWORD", "RAILWAY_TOKEN",
                "SOME_OTHER_SECRET", "MY_API_KEY"):
        monkeypatch.setenv(key, "sensitive-value")
    env = agents.child_env()
    for key in gates.SCRUBBED_ENV:
        assert key not in env, f"{key} leaked into the child environment"
    assert "SOME_OTHER_SECRET" not in env
    assert "MY_API_KEY" not in env
    assert "sensitive-value" not in " ".join(env.values())


def test_api_billing_cannot_be_reached_from_a_role(monkeypatch):
    """Requirement 10: never switch to metered API billing by accident."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert "ANTHROPIC_API_KEY" not in agents.child_env()


def test_roles_get_separate_contexts_and_read_only_tools():
    assert agents.ROLE_TOOLS["manager"] == ["Read", "Grep", "Glob"]
    assert agents.ROLE_TOOLS["reviewer"] == ["Read", "Grep", "Glob"]
    assert "Bash" not in agents.ROLE_TOOLS["reviewer"]
    assert "Bash" in agents.ROLE_TOOLS["developer"]
    for denied in ("Bash(git push:*)", "Bash(railway:*)", "Agent"):
        assert denied in agents.DENIED_TOOLS


def test_the_real_invocation_never_uses_bypass_permissions(tmp_path):
    for role in ("manager", "developer", "reviewer"):
        cmd = agents.build_command(role, "do it", tmp_path)
        assert "bypassPermissions" not in " ".join(cmd)
        assert "dangerously" not in " ".join(cmd)
        assert cmd[cmd.index("--permission-mode") + 1] == (
            "acceptEdits" if role == "developer" else "auto"
        )
    cmd = agents.build_command("developer", "do it", tmp_path)
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"
    # The repo's permissive .claude/settings.local.json must NOT be loaded.
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert str(agents.SETTINGS_PATH) in cmd


def test_isolation_preflight_refuses_a_worktree_on_main(world):
    repo = world["repo"]
    assert "worktree is not on its own branch" in " ".join(
        coordinator.isolation_preflight(repo)
    )


def test_the_pilot_ticket_shape_is_what_the_policy_accepts():
    assert policy.evaluate(PILOT_TICKET).is_eligible


def test_the_hook_path_handed_to_a_role_is_absolute_and_exists(tmp_path):
    """A worktree branched off main does not contain scripts/autodev.

    The shipped settings file uses $CLAUDE_PROJECT_DIR, which for a role
    IS the worktree -- so the hook would be missing there and, with
    onFailure=block, would block every tool call. The effective settings
    must name this checkout's guard.py by absolute path.
    """
    resolved = agents.effective_settings(tmp_path)
    data = json.loads(resolved.read_text())
    command = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command.startswith("/"), command
    assert Path(command).exists(), command
    assert "$CLAUDE_PROJECT_DIR" not in resolved.read_text()
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"] == "block"
    # and the deny rules survive the rewrite untouched
    assert "Bash(git push:*)" in data["permissions"]["deny"]


def test_the_real_command_points_at_the_effective_settings(tmp_path):
    resolved = agents.effective_settings(tmp_path)
    cmd = agents.build_command("developer", "do it", tmp_path,
                               settings_path=resolved)
    assert str(resolved) in cmd


def test_a_rework_is_a_new_attempt_with_its_own_evidence(world):
    """A rejected review then a clean run must not reuse attempt 1.

    Reusing it overwrote the first attempt's evidence and meant the
    retry limit could never be reached -- the cap the operator approved
    would have been decorative.
    """
    assert run(STUB_REVIEWER="reject") == 1
    first = world["home"] / "runs" / "pilot-comment" / "1" / "gates.json"
    assert first.exists()
    assert run() == 0
    second = world["home"] / "runs" / "pilot-comment" / "2" / "gates.json"
    assert second.exists(), "the rework did not get its own evidence directory"
    assert any(r["event"] == "rework_started" and r["attempt"] == 2
               for r in state.read_audit())


def test_the_retry_limit_stops_a_third_attempt(world):
    assert run(STUB_REVIEWER="reject") == 1
    assert run(STUB_REVIEWER="reject") == 1
    assert run() == 1, "a third attempt must be parked, not run"
    assert any("Retry limit" in p.read_text()
               for p in (world["home"] / "escalations").glob("*.md"))


def test_worktrees_never_live_inside_the_repository(monkeypatch):
    """A worktree inside the repo made pytest collect every test twice.

    It aborted the suite with 292 import-mismatch collection errors while
    `git status` stayed clean, so nothing pointed at the cause. The
    default root must sit outside the checkout.
    """
    monkeypatch.delenv("CARDFOUNDRY_AUTODEV_WORKTREES", raising=False)
    root = state.worktree_root()
    assert not str(root).startswith(str(state.REPO_ROOT)), root
    assert str(root).startswith(str(Path.home()))


def test_a_worktree_is_created_outside_the_repo_for_a_real_run(world):
    assert run() == 0
    created = world["worktrees"] / "pilot-comment"
    assert created.exists()
    assert not str(created).startswith(str(world["repo"]))


def test_the_attempt_counter_resets_when_the_task_changes(world):
    """Another task's attempt count is not this task's.

    The counter used to carry over, so a task following one that ended at
    attempt 2 started at 2 -- one attempt from the cap, writing its
    evidence into a directory numbered after unrelated work.
    """
    (world["home"] / "queue" / "other.json").write_text(
        json.dumps({**PILOT_TICKET, "id": "other"})
    )
    st = state.State.load()
    st.data.update({"active_task": "other", "phase": None, "attempt": 2,
                    "phase_started_at": None, "last_outcome": "gates=False"})
    st.save()

    assert run() == 0
    assert (world["home"] / "runs" / "pilot-comment" / "1").exists(), \
        "a different task must start at attempt 1"
    assert not (world["home"] / "runs" / "pilot-comment" / "2").exists()
    reset = [r for r in state.read_audit() if r["event"] == "attempt_counter_reset"]
    assert reset and reset[0]["previous_task"] == "other"
    assert reset[0]["previous_attempt"] == 2


def test_the_attempt_counter_does_not_reset_when_resuming_the_same_task(world):
    """A crash resume for the SAME task keeps counting -- that is its own
    number, and the retry limit depends on it."""
    st = state.State.load()
    st.data.update({"active_task": "pilot-comment", "phase": "developer",
                    "attempt": 1, "phase_started_at": "2026-10-31T04:00:00"})
    st.save()

    assert run() == 0
    rows = state.read_audit()
    assert any(r["event"] == "crash_detected" for r in rows)
    assert not any(r["event"] == "attempt_counter_reset" for r in rows), \
        "resuming the same task must not reset the counter"
    assert (world["home"] / "runs" / "pilot-comment" / "2").exists()
    assert any(r["event"] == "phase_started" and r["attempt"] == 2 for r in rows)
