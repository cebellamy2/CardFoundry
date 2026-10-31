"""The PreToolUse guard blocks with exit 2, and fails closed.

Driven as the hook itself is driven -- a subprocess fed JSON on stdin --
because that is the only thing Claude Code will actually do with it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "autodev" / "guard.py"


def run_guard(payload) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True,
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
    "git add -A",
    "git commit -m 'fix: correct a comment'",
    "grep -rn 'deploy-guard' scripts/",
    "python -m pytest -q tests/test_pre_push_hook.py",
    "sed -n '1,40p' scripts/hooks/deploy-guard-crons",
])
def test_ordinary_commands_are_allowed(command):
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
