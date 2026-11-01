"""The shared STUB harness for the AutoDev tests.

★ ONE STUB, ONE DEFINITION. These tests run the REAL coordinator -- the
real lock, state machine, audit log, gates and git worktree -- against a
stub binary standing in for `claude`, over a throwaway repository small
enough that the gates finish in milliseconds. CLAUDE.md forbids a test
that touches the network, and requirement 11 of the operator's approval
is that the coordinator be tested before any real agent runs.

This lives in its own module because test_autodev_coordinator.py and
test_autodev_serve.py both need it, and two copies of a stub are two
stubs that drift apart -- which is precisely the class of trap this
repo keeps notes about. Importable as a plain module because tests/ is
not a package: pytest puts this directory on sys.path.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.autodev import agents, coordinator, state

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
    elif mode == "regression":
        # The ticket's own comment change, plus a change that breaks an
        # existing test -- the shape pilot #3 hit for real.
        target.write_text(target.read_text() + "# a corrected comment line\n")
        Path("tests/test_widget.py").write_text(
            "def test_value():\n    assert False\n"
        )
    elif mode == "uncommitted":
        target.write_text(target.read_text() + "# left uncommitted, as a crash would\n")
    if mode not in ("nodiff", "uncommitted"):
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


