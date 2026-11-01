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
TICKET_FILE = "normalized_condition_id.py"


def run_guard(payload, *, role="developer",
              ticket_tests=TICKET_TEST,
              ticket_files=TICKET_FILE) -> subprocess.CompletedProcess:
    """Drive the hook exactly as Claude Code does: a subprocess fed JSON.

    The role, the ticket's tests and the ticket's files reach the hook
    through the environment, because a PreToolUse hook is given the tool
    call and nothing of the coordinator's state.
    """
    env = {**os.environ}
    env.pop("CARDFOUNDRY_AUTODEV_ROLE", None)
    env.pop("CARDFOUNDRY_AUTODEV_TICKET_TESTS", None)
    env.pop("CARDFOUNDRY_AUTODEV_TICKET_FILES", None)
    if role:
        env["CARDFOUNDRY_AUTODEV_ROLE"] = role
    if ticket_tests:
        env["CARDFOUNDRY_AUTODEV_TICKET_TESTS"] = ticket_tests
    if ticket_files:
        env["CARDFOUNDRY_AUTODEV_TICKET_FILES"] = ticket_files
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


# --- The spellings the first real developer run could not use -------------
# ★ MEASURED, 2026-11-01. The developer on AutoDev's first real task ran
# no test and no syntax check at all, and said so in its summary. Four
# separate causes, each of which refused a command the role prompt
# promised it could run: bare `pytest` was on guard.py's allowlist but
# not the CLI's; a `PYTHONPATH=.` prefix broke every ^-anchored pattern;
# `-k`'s value was read as a test path; and py_compile was on neither
# allowlist. These pin all four, and the blocks they must not have
# loosened.

@pytest.mark.parametrize("command", [
    f"pytest {TICKET_TEST}",
    f"pytest -q {TICKET_TEST}",
    f"pytest {TICKET_TEST} -k 'broken or missing'",
    f"pytest {TICKET_TEST} -k broken -q",
    f"python -m pytest {TICKET_TEST} -k 'summary' -q",
    f"python -m pytest {TICKET_TEST} -m 'not slow'",
    f"pytest {TICKET_TEST} -p no:cacheprovider -q",
    f"pytest {TICKET_TEST} --maxfail 1 -q",
    f"pytest {TICKET_TEST} --tb short",
    f"PYTHONPATH=. pytest {TICKET_TEST} -q",
    f"PYTHONPATH=. python -m pytest {TICKET_TEST}",
    f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest {TICKET_TEST}",
    f"PYTHONPATH=.:tests pytest {TICKET_TEST}",
    f'PYTHONWARNINGS="ignore::DeprecationWarning" pytest {TICKET_TEST}',
    f"python -m py_compile {TICKET_FILE}",
    f"python3 -m py_compile {TICKET_FILE} {TICKET_TEST}",
    f".venv/bin/python -m py_compile {TICKET_FILE}",
    f"PYTHONPATH=. python -m py_compile {TICKET_FILE}",
])
def test_the_spellings_a_developer_reaches_for_are_allowed(command):
    proc = run_guard(bash(command))
    assert proc.returncode == 0, f"{command!r} was blocked: {proc.stderr}"


@pytest.mark.parametrize("command,expected", [
    # The scope checks still bite, whichever spelling reaches them.
    (f"PYTHONPATH=. pytest tests/test_order_service.py", "not one of this ticket's tests"),
    (f"PYTHONPATH=. pytest", "coordinator"),
    (f"pytest -k broken", "coordinator"),
    ("python -m py_compile order_service.py", "not one of this ticket's paths"),
    ("python -m py_compile", "nothing to check"),
])
def test_an_env_prefix_does_not_widen_the_scope_checks(command, expected):
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed"
    assert expected in proc.stderr, proc.stderr


@pytest.mark.parametrize("command", [
    # ★ AN ASSIGNMENT CAN CHANGE WHICH BINARY RUNS. PATH and the dynamic
    # loader variables are the whole reason this is a name allowlist and
    # not "any NAME=VALUE".
    "PATH=/tmp/evil pytest tests/test_normalized_condition_id.py",
    "LD_PRELOAD=/tmp/x.so python -m pytest tests/test_normalized_condition_id.py",
    "DYLD_INSERT_LIBRARIES=/tmp/x.dylib pytest tests/test_normalized_condition_id.py",
    "PYTHONHOME=/tmp python -m pytest tests/test_normalized_condition_id.py",
    "PYTHONSTARTUP=/tmp/x.py python -m pytest tests/test_normalized_condition_id.py",
    "ANTHROPIC_API_KEY=sk-x python -m pytest tests/test_normalized_condition_id.py",
    "GIT_SSH_COMMAND=x git status",
    "PYTHONPATH=.",
])
def test_an_assignment_that_could_change_the_command_is_refused(command):
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed: {proc.stdout}"


@pytest.mark.parametrize("command", [
    "PYTHONPATH=. git push origin main",
    "PYTHONPATH=. railway ssh -s CardFoundry 'ls'",
    "PYTHONPATH=. python correct_dormant_returnable_prices.py --confirm",
    "PYTHONPATH=. sqlite3 cardfoundry.db '.tables'",
    "PYTHONPATH=. cat .env",
    "PYTHONPATH=. python scratch_check.py",
    "PYTHONPATH=. rm -rf claude",
    "PYTHONPATH=. git tag v2.81.0",
    "PYTHONPATH=. bash -c 'git push'",
    f"PYTHONPATH=. pytest {TICKET_TEST} > out.txt",
])
def test_an_env_prefix_smuggles_nothing_through(command):
    """Every existing block stands in front of a prefixed command too."""
    proc = run_guard(bash(command))
    assert proc.returncode == 2, f"{command!r} was allowed: {proc.stdout}"


def test_grepping_for_the_word_pytest_is_not_a_pytest_run():
    """`if "pytest" in tokens` got this wrong in both directions: it
    refused a grep for naming scripts/ as a test path, and it did not
    recognise `.venv/bin/pytest` as an invocation at all."""
    assert run_guard(bash("grep -rn pytest scripts/autodev")).returncode == 0
    assert run_guard(bash("cat pytest.ini")).returncode == 0

    proc = run_guard(bash(f".venv/bin/pytest tests/test_order_service.py"))
    assert proc.returncode == 2, "an out-of-scope test ran under a path spelling"
    assert "test_order_service.py" in proc.stderr


def test_a_syntax_check_needs_the_tickets_paths_in_scope():
    """With nothing in scope there is nothing to check against, so it
    refuses -- the same stance the pytest scope check takes."""
    proc = run_guard(bash(f"python -m py_compile {TICKET_FILE}"),
                     ticket_files=None, ticket_tests=None)
    assert proc.returncode == 2
    assert "no ticket paths" in proc.stderr

    # The ticket's files alone are enough: a ticket need not have tests.
    assert run_guard(bash(f"python -m py_compile {TICKET_FILE}"),
                     ticket_tests=None).returncode == 0


def test_a_syntax_check_may_name_the_tickets_tests_too():
    """It is scoped to files AND tests: a test file is a file the ticket
    edits. pytest stays scoped to the tests alone."""
    assert run_guard(bash(f"python -m py_compile {TICKET_TEST}")).returncode == 0
    proc = run_guard(bash(f"python -m pytest {TICKET_FILE}"))
    assert proc.returncode == 2, "a source file is not a test to run"
