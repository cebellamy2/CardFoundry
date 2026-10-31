"""The PreToolUse guard blocks with exit 2, and fails closed.

Driven as the hook itself is driven -- a subprocess fed JSON on stdin --
because that is the only thing Claude Code will actually do with it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "autodev" / "guard.py"


TICKET_TEST = "tests/test_normalized_condition_id.py"


def run_guard(payload, *, role="developer",
              ticket_tests=TICKET_TEST) -> subprocess.CompletedProcess:
    """Drive the hook exactly as Claude Code does: a subprocess fed JSON.

    The role and the ticket's tests reach the hook through the
    environment, because a PreToolUse hook is given the tool call and
    nothing of the coordinator's state.
    """
    env = {**os.environ}
    env.pop("CARDFOUNDRY_AUTODEV_ROLE", None)
    env.pop("CARDFOUNDRY_AUTODEV_TICKET_TESTS", None)
    if role:
        env["CARDFOUNDRY_AUTODEV_ROLE"] = role
    if ticket_tests:
        env["CARDFOUNDRY_AUTODEV_TICKET_TESTS"] = ticket_tests
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, env=env,
    )


def bash(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": command},
    }


@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push --no-verify",
    "git tag v2.78.0",
    "git switch main",
    "git remote set-url origin git@example.com:x/y.git",
    "railway ssh -s CardFoundry 'cat /app/VERSION'",
    "gh pr create --fill",
    "python correct_dormant_returnable_prices.py --confirm --max-cards 1",
    "sqlite3 cardfoundry.db 'update inventory_cards set current_price=1'",
    "cat .env",
    "grep MANAPOOL .env",
    "rm -rf claude/autodev",
    "curl -s https://manapool.com/api/v1/seller/inventory",
    "pip install requests",
    "python scheduled_pricing_apply.py",
    "python production_reset.py",
    "crontab -e",
    "uvicorn main:app",
])
def test_denied_commands_exit_2(command):
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was not blocked"
    assert "autodev guard: BLOCKED" in proc.stderr


@pytest.mark.parametrize("command", [
    "git status --porcelain",
    "git diff --stat",
    "git log --oneline -5",
    "git show HEAD",
    "git add -A",
    "git commit -m 'fix: correct a comment'",
    "git add -A && git commit -m 'fix: two steps, both allowed'",
    "grep -rn 'deploy-guard' scripts/",
    "grep -rn foo . | head -5",
    "ls -la scripts/autodev",
    "wc -l import_service.py",
    "find . -name 'test_normalized*'",
    "sed -n '1,40p' scripts/hooks/deploy-guard-crons",
    f"python -m pytest -q {TICKET_TEST}",
    f".venv/bin/python -m pytest -q {TICKET_TEST}::test_blank_or_none_is_none",
    "python -m compileall -q import_service.py",
])
def test_allowed_developer_commands_succeed(command):
    proc = run_guard(bash(command))
    assert proc.returncode == 0, f"{command!r} was blocked: {proc.stderr}"


def test_writing_a_protected_path_is_blocked():
    proc = run_guard({
        "hook_event_name": "PreToolUse", "tool_name": "Edit",
        "tool_input": {"file_path": "models.py"},
    })
    assert proc.returncode == 2
    assert "protected path" in proc.stderr


def test_writing_an_allowed_path_passes():
    proc = run_guard({
        "hook_event_name": "PreToolUse", "tool_name": "Edit",
        "tool_input": {"file_path": "scripts/hooks/deploy-guard-crons"},
    })
    assert proc.returncode == 0


def test_unparseable_input_fails_closed():
    proc = run_guard("{not json")
    assert proc.returncode == 2


def test_empty_bash_command_fails_closed():
    proc = run_guard(bash("   "))
    assert proc.returncode == 2


def test_a_tool_the_hook_does_not_gate_passes():
    proc = run_guard({
        "hook_event_name": "PreToolUse", "tool_name": "Read",
        "tool_input": {"file_path": "main.py"},
    })
    assert proc.returncode == 0


# --- The developer allowlist, added after pilot #2 -------------------------
# Pilot #2 reached the developer, which wrote the right change and then
# could not run one command: acceptEdits covers EDITS only, so with
# prompts set to none every Bash call was denied. The allowlist exists to
# stop that; these pin that it stayed NARROW.

@pytest.mark.parametrize("command", [
    "curl -s https://example.com",
    "python manage.py shell",
    "python -c 'import os; os.system(\"git push\")'",
    "./scripts/hooks/pre-push",
    "make deploy",
    "npm install",
    "ssh server 'ls'",
    "open http://localhost:8000",
    "git stash drop",
    "git reset --hard origin/main",
    "git clean -fd",
])
def test_commands_off_the_allowlist_are_denied(command):
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed: {proc.stdout}"


@pytest.mark.parametrize("command", [
    "git add -A && git push origin main",
    "git status; railway ssh -s CardFoundry 'ls'",
    "git diff || sqlite3 cardfoundry.db '.tables'",
    "grep foo . | rm -rf /tmp/x",
    "git status && cat .env",
])
def test_a_chained_command_cannot_smuggle_a_denied_one(command):
    """A rule like Bash(git add:*) matches the HEAD of a chained command,
    so an allowlist alone would be a bypass. Every segment is checked."""
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed: {proc.stdout}"


@pytest.mark.parametrize("command", [
    "git commit -m \"$(cat .env)\"",
    "eval \"$(echo git push)\"",
    "bash -c 'git push'",
    "sh -c 'railway ssh'",
    "echo x | xargs git push",
    "git status `whoami`",
])
def test_shell_re_entry_and_substitution_are_refused_on_sight(command):
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed: {proc.stdout}"


@pytest.mark.parametrize("command", [
    "echo hi > import_service.py",
    "cat main.py >> /tmp/copy.py",
    "sed -i '' s/a/b/ import_service.py",
    "sed -i.bak s/a/b/ models.py",
])
def test_writing_files_outside_the_edit_tool_is_denied(command):
    """Edits go through the Edit tool, where the protected-path check
    runs. A redirection or sed -i would go round it."""
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed: {proc.stdout}"


def test_redirecting_to_dev_null_is_still_fine():
    proc = run_guard(bash("git diff --stat 2>/dev/null"))
    assert proc.returncode == 0, proc.stderr


def test_pytest_must_name_only_this_tickets_tests():
    assert run_guard(bash(f"python -m pytest -q {TICKET_TEST}")).returncode == 0
    assert run_guard(bash("python -m pytest -q tests/test_order_service.py")).returncode == 2
    assert run_guard(bash("python -m pytest -q tests/")).returncode == 2


def test_a_bare_pytest_is_the_coordinators_gate_not_the_agents():
    proc = run_guard(bash("python -m pytest -q"))
    assert proc.returncode == 2
    assert "coordinator" in proc.stderr


def test_with_no_ticket_tests_in_scope_no_pytest_runs():
    proc = run_guard(bash(f"python -m pytest -q {TICKET_TEST}"), ticket_tests=None)
    assert proc.returncode == 2


@pytest.mark.parametrize("role", [None, "manager", "reviewer", "something-else"])
def test_only_the_developer_has_a_shell_at_all(role):
    """The read-only roles have no Bash tool; a missing role gets none
    either, so forgetting to set it fails closed."""
    proc = run_guard(bash("git status"), role=role)
    assert proc.returncode == 2
    assert "no shell" in proc.stderr


def test_an_attributed_commit_message_is_not_mistaken_for_a_redirection():
    """`<noreply@anthropic.com>` contains a '>'.

    The first version of the redirection check was a regex over the whole
    line, so it matched that angle bracket and blocked every commit
    carrying the attribution trailer. The real developer smoke test found
    it; no unit test had. A redirection is an operator TOKEN, not any '>'
    in the text.
    """
    proc = run_guard(bash(
        'git commit -m "fix: a thing" '
        '-m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"'
    ))
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("command", [
    "git status > status.txt",
    "cat import_service.py >> /tmp/x",
    "python -m pytest -q tests/test_normalized_condition_id.py > out",
    "echo x 1> file",
])
def test_a_real_redirection_is_still_denied(command):
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed"


@pytest.mark.parametrize("command", [
    "git diff 2>/dev/null",
    "python -m compileall -q import_service.py 2>&1",
])
def test_dev_null_and_stderr_merging_stay_allowed(command):
    assert run_guard(bash(command)).returncode == 0


def test_bare_pytest_with_the_tickets_test_is_allowed():
    """The smoke test reached for `pytest tests/...` before
    `python -m pytest`. Both are the same thing and both are scoped."""
    assert run_guard(bash(f"pytest {TICKET_TEST} -v")).returncode == 0
    assert run_guard(bash("pytest tests/test_order_service.py")).returncode == 2


def test_an_absolute_path_to_the_tickets_test_is_in_scope():
    proc = run_guard(bash(f"python3 -m pytest /tmp/wt/{TICKET_TEST}"))
    assert proc.returncode == 0, proc.stderr


def test_cd_stays_inside_the_worktree():
    assert run_guard(bash("cd tests")).returncode == 0
    assert run_guard(bash("cd scripts/autodev")).returncode == 0
    assert run_guard(bash("cd /etc")).returncode == 2
    assert run_guard(bash("cd ../../../etc")).returncode == 2, \
        "a relative path can still walk out of the worktree"


def test_a_redirection_is_not_mistaken_for_a_test_path():
    """`2>&1` is a redirection, not a test file.

    Pilot #3's developer ran `pytest tests/test_x.py -q 2>&1 | tail -5`
    and was told 2>&1 was not one of the ticket's tests.
    """
    done = run_guard(bash(
        f"python -m pytest {TICKET_TEST} -q 2>&1 | tail -5"
    ))
    assert done.returncode == 0, done.stderr
    # and a real out-of-scope path is still refused next to a redirection
    refused = run_guard(bash("python -m pytest tests/test_other.py -q 2>&1"))
    assert refused.returncode == 2
    assert "test_other.py" in refused.stderr
