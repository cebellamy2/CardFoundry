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

from autodev_harness import PILOT_TICKET, STUB, _git, run, world  # noqa: F401
from scripts.autodev import agents, coordinator, gates, policy, state


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


def test_there_is_no_daily_cap_by_default(world):
    """★ THE OPERATOR'S DECISION, 2026-11-01: "no limit per day since it
    is going to put them on their own branch and requires approval to
    move them to main." A finished task is a branch, not a release."""
    assert coordinator.config()["max_tasks_per_day"] is None

    st = state.State.load()
    st.roll_day()
    st.data["tasks_today"] = 99
    st.save()

    assert run() == 0, "99 tasks today must not block the hundredth"


def test_a_cap_the_operator_configures_is_still_honoured(world):
    """The mechanism is kept, not deleted: a number here re-arms it with
    no release."""
    config_path = world["home"] / "config.json"
    config_path.write_text(json.dumps(
        {**coordinator.config(), "max_tasks_per_day": 2}))
    st = state.State.load()
    st.roll_day()
    st.data["tasks_today"] = 2
    st.save()

    assert run() == 2, "a configured cap stopped holding"


def test_auto_continue_is_gone_and_scheduling_is_off_by_default(world):
    """auto_continue named no behaviour -- serve never read it and
    already continues past a parked task -- so it was removed rather
    than left as a flag that does nothing. scheduling_enabled now MEANS
    something (autoserve reads it) and still ships off."""
    cfg = coordinator.config()
    assert "auto_continue" not in cfg
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
    """The guard hook must be named by absolute path, not $CLAUDE_PROJECT_DIR.

    The shipped settings file uses $CLAUDE_PROJECT_DIR, which for a role
    IS the worktree. That resolved to nothing while AutoDev lived only on
    its own branch, and with onFailure=block it would have blocked every
    tool call. Now that scripts/autodev is on main a worktree cut from it
    does carry guard.py, but the absolute path stays: a worktree branched
    from any OLDER commit still would not, and the hook that must fail
    closed is the last place to depend on where it was branched from.
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


def test_the_developer_gets_a_narrow_allowlist_and_the_others_get_none(tmp_path):
    cmd = agents.build_command("developer", "do it", tmp_path)
    assert "--allowedTools" in cmd
    allowed = agents.ROLE_ALLOWED_TOOLS["developer"]
    assert "Bash" not in allowed, "a bare Bash rule would allow everything"
    for forbidden in ("git push", "git tag", "railway", "sqlite3", "rm",
                      "curl", "gh ", "pip"):
        assert not any(forbidden in rule for rule in allowed), forbidden
    for expected in ("Bash(git add:*)", "Bash(git commit:*)",
                     "Bash(git status:*)", "Bash(git diff:*)",
                     # ★ The first real developer run could not test its
                     # own work: guard.py allowed bare `pytest` and this
                     # list did not, so every attempt fell through to a
                     # prompt -- and a prompt is a denial.
                     "Bash(pytest:*)", "Bash(python:*)"):
        assert expected in allowed
    for role in ("manager", "reviewer"):
        assert agents.ROLE_ALLOWED_TOOLS[role] == []
        assert "--allowedTools" not in agents.build_command(role, "x", tmp_path)


def test_the_role_and_ticket_paths_reach_the_hook_through_the_environment():
    env = agents.child_env(role="developer", ticket_tests=["tests/test_a.py"],
                           ticket_files=["a_service.py", "b_service.py"])
    assert env["CARDFOUNDRY_AUTODEV_ROLE"] == "developer"
    assert env["CARDFOUNDRY_AUTODEV_TICKET_TESTS"] == "tests/test_a.py"
    # The files too, so the guard can scope a syntax check to them.
    assert env["CARDFOUNDRY_AUTODEV_TICKET_FILES"] == "a_service.py:b_service.py"
    # and a role with no shell still carries no secrets
    for key in gates.SCRUBBED_ENV:
        assert key not in env


def test_the_developer_is_told_its_tickets_files_on_a_real_run(world):
    """The coordinator passes them, not just child_env's signature.

    The same gap one layer down cost pilot #3 its test evidence: the
    guard read the ticket's tests from an environment variable the
    coordinator never set, and the guard's own tests set it by hand.
    """
    recorded = {}
    real = agents.run_role

    def capture(role, prompt, worktree, evidence_dir, **kwargs):
        if role == "developer":
            recorded.update(kwargs)
        return real(role, prompt, worktree, evidence_dir, **kwargs)

    import unittest.mock
    with unittest.mock.patch.object(agents, "run_role", capture):
        assert run() == 0
    assert recorded["ticket_files"] == PILOT_TICKET["files"]
    assert recorded["ticket_tests"] == PILOT_TICKET["tests"]


def test_an_unset_role_means_no_shell(monkeypatch):
    env = agents.child_env()
    assert "CARDFOUNDRY_AUTODEV_ROLE" not in env


def _manager_phase_count():
    return sum(1 for row in state.read_audit()
               if row["event"] == "phase_started" and row["phase"] == "manager")


def test_a_rework_reuses_the_ready_verdict_instead_of_revalidating(world):
    """The hole pilot #2 fell into, pinned.

    Attempt 1 said `ready`, left its work uncommitted, and parked. Attempt
    2's manager then read that worked-in tree, found the change already
    there and said `not_as_described` -- correctly, and fatally: no rework
    could ever reach the developer again. A rework must carry the verdict
    forward instead of asking the same question of a dirty tree.
    """
    assert run(STUB_DEV_MODE="uncommitted") == 1
    worktree = world["worktrees"] / "pilot-comment"
    assert _git(["status", "--porcelain"], worktree).stdout.strip(), \
        "the first attempt was supposed to leave work behind"
    assert _manager_phase_count() == 1

    assert run() == 0, "the rework must get past the manager phase"
    assert _manager_phase_count() == 1, \
        "the manager must NOT run again against the worked-in tree"
    reused = [r for r in state.read_audit() if r["event"] == "manager_reused"]
    assert reused and reused[0]["attempt"] == 2
    assert reused[0]["source_attempt"] == 1
    assert Path(reused[0]["source"]).exists()
    assert reused[0]["digest_verified"] is True


def test_the_reused_verdict_carries_its_own_acceptance_criteria(world):
    assert run(STUB_DEV_MODE="uncommitted") == 1
    first = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "1"
         / "manager-verdict.json").read_text()
    )
    assert first["verdict"] == "ready"
    criterion = first["acceptance_criteria"][0]

    assert run() == 0
    second_dir = world["home"] / "runs" / "pilot-comment" / "2"
    carried = json.loads((second_dir / "manager-verdict.json").read_text())
    assert carried["acceptance_criteria"] == first["acceptance_criteria"]
    assert carried["reused_from"]["attempt"] == 1
    # and the developer was actually briefed with them, not just the file
    assert criterion in (second_dir / "developer-prompt.txt").read_text()


def test_a_rework_does_not_lose_the_work_left_in_the_worktree(world):
    """Crash recovery keeps what the interrupted attempt had written."""
    assert run(STUB_DEV_MODE="uncommitted") == 1
    worktree = world["worktrees"] / "pilot-comment"
    assert "left uncommitted" in (worktree / "guarded-crons").read_text()

    assert run() == 0
    text = (worktree / "guarded-crons").read_text()
    assert "left uncommitted" in text, "the prior attempt's work was discarded"
    assert "a corrected comment line" in text, "the rework's own work is missing"
    assert not _git(["status", "--porcelain"], worktree).stdout.strip(), \
        "the rework should have committed what was there"


def test_a_verdict_from_another_task_is_never_reused(world):
    """Requirement 4: the stored verdict must belong to THIS task."""
    (world["home"] / "queue" / "other.json").write_text(
        json.dumps({**PILOT_TICKET, "id": "other"})
    )
    foreign = state.run_dir("other", 1)
    (foreign / "manager-verdict.json").write_text(json.dumps({
        "verdict": "ready", "task": "other",
        "acceptance_criteria": ["someone else's criterion"],
        "expected_files": ["guarded-crons"],
    }))
    ticket = policy.evaluate(PILOT_TICKET)
    stored, reuse = coordinator.reusable_manager_verdict(ticket, attempt=2)
    assert stored is None and reuse is None

    # and the same file, read as if it were this task's, is still refused
    problems = coordinator._verdict_reuse_problems(
        json.loads((foreign / "manager-verdict.json").read_text()),
        ticket, foreign / "manager-verdict.json",
    )
    assert any("written for task 'other'" in problem for problem in problems)


def test_a_verdict_outside_the_tickets_scope_is_never_reused(world):
    evidence = state.run_dir("pilot-comment", 1)
    (evidence / "manager-verdict.json").write_text(json.dumps({
        "verdict": "ready", "task": "pilot-comment",
        "acceptance_criteria": ["c"], "expected_files": ["models.py"],
    }))
    ticket = policy.evaluate(PILOT_TICKET)
    stored, _ = coordinator.reusable_manager_verdict(ticket, attempt=2)
    assert stored is None
    refused = [r for r in state.read_audit()
               if r["event"] == "manager_verdict_not_reusable"]
    assert refused and "models.py" in str(refused[-1]["problems"])


def test_an_edited_ticket_does_not_reuse_its_old_verdict(world):
    ticket = policy.evaluate(PILOT_TICKET)
    evidence = state.run_dir("pilot-comment", 1)
    (evidence / "manager-verdict.json").write_text(json.dumps({
        "verdict": "ready", "task": "pilot-comment",
        "ticket_digest": coordinator.ticket_digest(ticket),
        "acceptance_criteria": ["c"], "expected_files": ["guarded-crons"],
    }))
    assert coordinator.reusable_manager_verdict(ticket, 2)[0] is not None

    edited = policy.evaluate({**PILOT_TICKET, "intent": "a different intent"})
    assert coordinator.reusable_manager_verdict(edited, 2)[0] is None
    refused = [r for r in state.read_audit()
               if r["event"] == "manager_verdict_not_reusable"]
    assert any("edited" in str(r["problems"]) for r in refused)


def test_a_genuinely_new_task_still_gets_a_fresh_manager_validation(world):
    """Requirement 6: reuse is for reworks, never for a new task."""
    assert run() == 0
    assert _manager_phase_count() == 1
    (world["home"] / "queue" / "other.json").write_text(
        json.dumps({**PILOT_TICKET, "id": "other"})
    )
    assert run("other") == 0
    assert _manager_phase_count() == 2, "the new task must be validated itself"
    assert not any(r["event"] == "manager_reused" for r in state.read_audit())


def test_a_rework_with_prior_work_and_no_ready_verdict_stops(world):
    """Requirement 3, in the case where there is nothing to carry forward."""
    assert run(STUB_MANAGER="not_as_described") == 1
    worktree = world["worktrees"] / "pilot-comment"
    (worktree / "guarded-crons").write_text(
        (worktree / "guarded-crons").read_text() + "# a stray edit\n"
    )
    assert run() == 1
    text = "\n".join(p.read_text() for p in
                     (world["home"] / "escalations").glob("*.md"))
    assert "prior work in the worktree" in text
    assert _manager_phase_count() == 1, \
        "the manager must not be asked about a tree containing the work"
    assert (worktree / "guarded-crons").read_text().count("a stray edit") == 1


def test_the_retry_limit_still_holds_without_a_grant(world):
    assert run(STUB_REVIEWER="reject") == 1
    assert run(STUB_REVIEWER="reject") == 1
    assert run() == 1, "a third attempt needs an explicit exception"
    assert any("Retry limit" in p.read_text()
               for p in (world["home"] / "escalations").glob("*.md"))
    assert coordinator.config()["max_attempts"] == 2


def test_a_one_time_grant_allows_exactly_one_further_attempt(world):
    assert run(STUB_REVIEWER="reject") == 1
    assert run(STUB_REVIEWER="reject") == 1
    assert coordinator.cmd_grant(type("A", (), {
        "task": "pilot-comment", "attempt": 3,
        "reason": "the rework fix landed after attempt 2 was spent",
        "granted_by": "operator via cowork 2026-10-31",
    })()) == 0
    granted = [r for r in state.read_audit()
               if r["event"] == "attempt_exception_granted"]
    assert granted and granted[-1]["attempt"] == 3

    assert run() == 0, "attempt 3 should run under the exception"
    used = [r for r in state.read_audit()
            if r["event"] == "attempt_exception_used"]
    assert used and used[-1]["attempt"] == 3
    assert used[-1]["max_attempts_unchanged"] is True
    assert "the rework fix landed" in used[-1]["reason"]
    # the global cap did not move
    assert coordinator.config()["max_attempts"] == 2
    assert (world["home"] / "grants" /
            "pilot-comment-attempt-3.used.json").exists()
    assert not (world["home"] / "grants" /
                "pilot-comment-attempt-3.json").exists()


def test_a_spent_grant_cannot_be_used_again(world):
    grants = state.ensure_home() / "grants"
    (grants / "pilot-comment-attempt-3.json").write_text(json.dumps({
        "task": "pilot-comment", "attempt": 3, "reason": "one time",
        "granted_by": "operator",
    }))
    assert coordinator.consume_attempt_grant("pilot-comment", 3) is not None
    assert coordinator.consume_attempt_grant("pilot-comment", 3) is None
    # and it cannot be re-granted silently either
    assert coordinator.cmd_grant(type("A", (), {
        "task": "pilot-comment", "attempt": 3, "reason": "again",
        "granted_by": "operator",
    })()) == 2


def test_a_grant_cannot_be_stretched_to_another_attempt_or_task(world):
    grants = state.ensure_home() / "grants"
    (grants / "pilot-comment-attempt-3.json").write_text(json.dumps({
        "task": "pilot-comment", "attempt": 3, "reason": "one time",
        "granted_by": "operator",
    }))
    assert coordinator.consume_attempt_grant("pilot-comment", 4) is None
    assert coordinator.consume_attempt_grant("other", 3) is None
    # a grant file whose contents name a different task is refused too
    (grants / "other-attempt-3.json").write_text(json.dumps({
        "task": "pilot-comment", "attempt": 3, "reason": "mislabelled",
        "granted_by": "operator",
    }))
    assert coordinator.consume_attempt_grant("other", 3) is None
    refused = [r for r in state.read_audit()
               if r["event"] == "attempt_grant_refused"]
    assert refused and "names task" in str(refused[-1]["problems"])
    assert (grants / "pilot-comment-attempt-3.json").exists(), \
        "a refused lookup must not consume the real grant"


def test_a_grant_within_the_limit_is_refused_as_unnecessary(world):
    assert coordinator.cmd_grant(type("A", (), {
        "task": "pilot-comment", "attempt": 2, "reason": "x",
        "granted_by": "operator",
    })()) == 2
    assert not list((state.ensure_home() / "grants").glob("*.json"))


def test_the_developer_is_told_about_work_an_earlier_attempt_left(world):
    """Prior work is told, not discovered.

    A developer that meets a dirty worktree by surprise either reports
    "already done" and commits nothing, or quietly reverts work that was
    correct. Both lose the operator something.
    """
    assert run(STUB_DEV_MODE="uncommitted") == 1
    assert run() == 0
    prompt = (world["home"] / "runs" / "pilot-comment" / "2"
              / "developer-prompt.txt").read_text()
    assert "ALREADY CONTAINS uncommitted work" in prompt
    assert '"uncommitted_work_from_an_earlier_attempt": "M guarded-crons"' in prompt
    assert "never -A" in prompt
    # and a first attempt is told no such thing
    first = (world["home"] / "runs" / "pilot-comment" / "1"
             / "developer-prompt.txt").read_text()
    assert "ALREADY CONTAINS" not in first


def test_the_developer_is_told_which_tests_are_in_scope(world, monkeypatch):
    """Without this the developer cannot run any test at all.

    The guard scopes pytest to CARDFOUNDRY_AUTODEV_TICKET_TESTS, so with
    that unset every pytest run is refused as out of scope. Pilot #3 hit
    it: the developer committed and reported that the coordinator's gates
    were the only test evidence for its change. The guard's own tests set
    the variable by hand, so nothing caught that the coordinator did not.
    """
    seen = {}
    real = agents.run_role

    def spy(role, prompt, worktree, evidence, **kwargs):
        seen[role] = kwargs.get("ticket_tests")
        return real(role, prompt, worktree, evidence, **kwargs)

    monkeypatch.setattr(agents, "run_role", spy)
    ticket = {**PILOT_TICKET, "tests": ["tests/test_widget.py"]}
    (world["home"] / "queue" / "pilot-comment.json").write_text(json.dumps(ticket))
    assert run() == 0
    assert seen["developer"] == ["tests/test_widget.py"]
    env = agents.child_env(role="developer", ticket_tests=seen["developer"])
    assert env["CARDFOUNDRY_AUTODEV_TICKET_TESTS"] == "tests/test_widget.py"


def _broken_test_on_main(world, name="tests/test_broken.py"):
    """Leave a test failing at the base commit, as the real repo does."""
    (world["repo"] / name).write_text("def test_already_broken():\n    assert False\n")
    _git(["add", "-A"], world["repo"])
    _git(["commit", "-m", "a test that already fails"], world["repo"])


def test_a_failure_that_was_already_red_is_named_as_pre_existing(world):
    """Pilot #3's lesson: measure which failures are the change's.

    158 failures came back and every phase read them as "probably the
    environment". 157 were. One was a real regression, found only by
    running the suite at the base commit and diffing the lists.
    """
    _broken_test_on_main(world)
    assert run() == 1, "the default standard is that the full suite passes"
    verdict = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "1" / "gates.json").read_text()
    )
    results = {r["name"]: r for r in verdict["results"]}
    assert results["pytest_full"]["ok"] is False
    assert results["pytest_baseline"]["ok"] is True
    assert "no new failure" in results["pytest_baseline"]["detail"]
    assert "1 already failing" in results["pytest_baseline"]["detail"]


def test_a_new_failure_is_separated_from_the_pre_existing_ones(world):
    _broken_test_on_main(world)
    assert run(STUB_DEV_MODE="regression") == 1
    verdict = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "1" / "gates.json").read_text()
    )
    baseline = [r for r in verdict["results"] if r["name"] == "pytest_baseline"][0]
    assert baseline["ok"] is False
    assert "NEW failures caused by this change" in baseline["detail"]
    assert "test_widget.py::test_value" in baseline["detail"]
    # and the one that was already red is NOT blamed on the change
    assert "test_already_broken" not in baseline["detail"].split("NEW failures")[1]


def test_the_baseline_is_measured_in_a_throwaway_worktree_and_cached(world):
    _broken_test_on_main(world)
    assert run() == 1
    cached = list((world["home"] / "baselines").glob("*.txt"))
    assert len(cached) == 1, cached
    assert "test_already_broken" in cached[0].read_text()
    # nothing of the probe is left behind, inside the repo or out
    assert not list(world["worktrees"].glob("_baseline-*"))
    worktrees = _git(["worktree", "list"], world["repo"]).stdout
    assert "_baseline-" not in worktrees
    assert (world["home"] / "runs" / "pilot-comment" / "1"
            / "pytest-baseline.txt").exists()


def test_the_cached_baseline_is_reused_rather_than_measured_again(world):
    _broken_test_on_main(world)
    base = _git(["rev-parse", "main"], world["repo"]).stdout.strip()
    (state.ensure_home() / "baselines" / f"{base}.txt").write_text(
        "tests/test_broken.py::test_already_broken\n"
    )
    known, note = gates.baseline_failures(base, state.run_dir("probe", 1))
    assert known == {"tests/test_broken.py::test_already_broken"}
    assert note.startswith("cached baseline")
    assert not (state.run_dir("probe", 1) / "pytest-baseline.txt").exists()


def test_accepting_pre_existing_failures_is_off_by_default_and_opt_in(world):
    """CLAUDE.md's standard is that the full suite passes.

    A gate that accepts a red suite is a weaker standard than the repo is
    held to, so the operator turns it on, in config.json, or it stays off.
    """
    assert coordinator.config()["allow_preexisting_failures"] is False
    _broken_test_on_main(world)
    assert run() == 1

    config_path = world["home"] / "config.json"
    config_path.write_text(json.dumps(
        {**coordinator.config(), "allow_preexisting_failures": True}
    ))
    assert run() == 0, "with the flag on, a change that adds no failure passes"
    verdict = json.loads(
        (world["home"] / "runs" / "pilot-comment" / "2" / "gates.json").read_text()
    )
    full = [r for r in verdict["results"] if r["name"] == "pytest_full"][0]
    assert full["ok"] is True
    assert "already failing at" in full["detail"]
    # and it never excuses a NEW failure
    assert run(STUB_DEV_MODE="regression") == 1


def test_an_unmeasurable_baseline_is_skipped_not_treated_as_green(world):
    """A baseline that could not be measured must not read as "nothing
    was failing" -- that would turn every pre-existing failure into a
    false regression, and a skipped gate must never fold into a pass."""
    known, note = gates.baseline_failures("deadbeefdeadbeef", state.run_dir("probe", 1))
    assert known is None
    assert "could not build a baseline worktree" in note


# --- acceptance criteria that name paths outside the ticket ---------------
# ★ MEASURED ON AUTODEV'S FIRST REAL TASK, 2026-11-01. The manager
# returned six good criteria and one requiring a VERSION bump and a
# CHANGELOG entry. Both paths are reserved by policy.FORBIDDEN_PATHS and
# neither is in any ticket's `files`, so the developer could only choose
# which failure to take: fail the diff-scope gate by obeying it, or fail
# a stated criterion by ignoring it. That choice is not a developer's to
# make. manager.md now says an AutoDev branch is not a release; this is
# the layer that does not depend on a model having read it.

RELEASE_CRITERION = "VERSION is bumped and a CHANGELOG.md entry is added"


def test_a_release_criterion_is_dropped_before_the_developer_sees_it(world):
    ticket = policy.load(world["home"] / "queue" / "pilot-comment.json")
    kept, dropped = coordinator.criteria_in_scope(ticket, [
        "the file's non-comment lines are unchanged",
        RELEASE_CRITERION,
    ])

    assert kept == ["the file's non-comment lines are unchanged"]
    assert len(dropped) == 1
    assert dropped[0]["criterion"] == RELEASE_CRITERION
    assert dropped[0]["paths"] == ["VERSION", "CHANGELOG.md"]


def test_the_tickets_own_files_and_tests_are_in_scope(world):
    ticket = policy.load(world["home"] / "queue" / "pilot-comment.json")
    kept, dropped = coordinator.criteria_in_scope(ticket, [
        "guarded-crons keeps its parsed tick list",
        "the comment explains WHY, as CLAUDE.md requires",
        "nothing is added to the schedule",
    ])
    assert len(kept) == 3 and dropped == []


def test_another_tickets_file_is_out_of_scope(world):
    ticket = policy.load(world["home"] / "queue" / "pilot-comment.json")
    kept, dropped = coordinator.criteria_in_scope(
        ticket, ["tests/test_order_service.py still passes"])
    assert kept == []
    assert dropped[0]["paths"] == ["tests/test_order_service.py"]


def test_the_drop_is_logged_and_kept_as_evidence(world):
    """Dropped quietly, it would look like the manager never asked --
    which is exactly the absence CLAUDE.md says to report."""
    assert run(STUB_CRITERIA=json.dumps([
        "the file's non-comment lines are unchanged", RELEASE_CRITERION,
    ])) == 0

    rows = [r for r in state.read_audit()
            if r.get("event") == "acceptance_criteria_dropped"]
    assert rows, "the drop was not audited"
    assert rows[-1]["kept"] == 1
    assert rows[-1]["dropped"][0]["paths"] == ["VERSION", "CHANGELOG.md"]

    evidence = (world["home"] / "runs" / "pilot-comment" / "1"
                / "acceptance-criteria-dropped.json")
    assert json.loads(evidence.read_text())[0]["criterion"] == RELEASE_CRITERION


def test_neither_the_developer_nor_the_reviewer_is_given_a_dropped_one(world):
    """Both are handed `criteria`, and a reviewer holding a criterion the
    developer was never asked to meet would reject correct work."""
    assert run(STUB_CRITERIA=json.dumps([
        "the file's non-comment lines are unchanged", RELEASE_CRITERION,
    ])) == 0

    evidence = world["home"] / "runs" / "pilot-comment" / "1"
    for role in ("developer", "reviewer"):
        prompt = (evidence / f"{role}-prompt.txt").read_text()
        assert "non-comment lines are unchanged" in prompt
        assert "CHANGELOG" not in prompt, f"the {role} was given it anyway"


def test_a_run_whose_every_criterion_is_out_of_scope_is_parked(world):
    """Nothing left to build to. Handing the developer an empty list would
    produce a diff nobody agreed the shape of."""
    assert run(STUB_CRITERIA=json.dumps([RELEASE_CRITERION])) == 1

    st = state.State.load()
    assert st.data["last_outcome"] == "manager:all-criteria-out-of-scope"
    escalations = list((world["home"] / "escalations").glob("*.md"))
    assert escalations, "the operator was not told"
    assert "outside the ticket" in "".join(
        path.read_text() for path in escalations)
    # and it stopped before the developer ran
    assert not (world["home"] / "runs" / "pilot-comment" / "1"
                / "developer-prompt.txt").exists()
