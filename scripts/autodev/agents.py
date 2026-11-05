"""How a role is actually invoked: one `claude -p` process per phase.

★ WHY THE CLI AND NOT THE AGENT SDK. The Agent SDK's own documentation
says third-party agents built on it may not use claude.ai login or its
rate limits and must authenticate with an ANTHROPIC_API_KEY -- i.e. metered
per-token billing. Claude Code's headless mode runs on the operator's own
subscription. Requirement 10 of the approval says not to switch to API
billing, so ANTHROPIC_API_KEY is SCRUBBED from every child environment:
that is not a preference, it is the mechanism that makes accidental
metered spend impossible.

★ EVERY ROLE IS A FRESH PROCESS WITH ITS OWN CONTEXT. The reviewer never
sees the developer's transcript -- it gets the ticket, the acceptance
criteria, the diff and the gate output as files. A reviewer that read the
developer's reasoning would be agreeing with it, not checking it.

★ FOUR INDEPENDENT SAFETY LAYERS, because any one of them can be wrong:
  1. no credentials in the child environment (SCRUBBED_ENV) -- the only
     layer that does not depend on a model, a rule or a hook behaving;
  2. deny rules in settings.autodev.json, loaded with
     `--setting-sources project` so the repo's permissive
     .claude/settings.local.json allowlist (railway ssh, railway api,
     git commit) is NOT loaded;
  3. a PreToolUse guard hook that exits 2 on the denylist (guard.py);
  4. worktree isolation, and `--permission-mode auto` with
     `--permission-prompts none`, so anything unanticipated is DENIED
     rather than waved through. bypassPermissions is never used.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gates import SCRUBBED_ENV
from . import state

logger = logging.getLogger("cardfoundry")

HERE = Path(__file__).resolve().parent
SETTINGS_PATH = HERE / "settings.autodev.json"
# ★ A SECOND SETTINGS FILE, FOR A SECOND FENCE. The inbox session works
# tickets whose declared scope may include paths policy.py reserves for
# the operator -- the deploy guard, order_service.py -- which is the
# whole reason it is not an AutoDev task. Its deny rules therefore
# differ, and they differ in a FILE rather than by a flag, so a reader
# can diff the two fences side by side. See inbox.py for why the two
# exist at all.
INBOX_SETTINGS_PATH = HERE / "settings.inbox.json"
ROLES_DIR = HERE / "roles"
SCHEMAS_DIR = HERE / "schemas"

# Tool sets per role. The manager and the reviewer have NO Bash and no
# write tools at all -- a role that only has to read should not be able
# to run a command, and taking the tool away is stronger than any rule
# about not using it.
ROLE_TOOLS = {
    "manager": ["Read", "Grep", "Glob"],
    "developer": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
    "reviewer": ["Read", "Grep", "Glob"],
    # The inbox session does a whole ticket on its own -- there is no
    # manager in front of it and no reviewer behind it -- so it has the
    # developer's tools. What bounds it is scope, not tool count: every
    # edit must land in a path its ticket declared, and it cannot push.
    "inbox": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
}

# ★ THE DEVELOPER'S ALLOWLIST, and why it has to exist at all. Pilot #2
# died here: `--permission-mode acceptEdits` auto-accepts EDITS only, so
# with `--permission-prompts none` every Bash call fell through to a
# prompt and a prompt is a denial. The developer could write files and
# could not run one command -- including the `git commit` that crash
# recovery depends on.
#
# ★ NARROW, AND NOT THE REAL ENFORCEMENT. These rules exist to stop the
# prompt; guard.py is what actually decides, and it checks EVERY SEGMENT
# of a chained command against the same intent. A rule like
# `Bash(git add:*)` can match the head of `git add -A && git push`, so an
# allowlist on its own would be a bypass waiting to happen.
ROLE_ALLOWED_TOOLS = {
    "manager": [],
    "reviewer": [],
    "developer": [
        # Local git only. push/tag/remote are denied three ways over.
        "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
        "Bash(git show:*)", "Bash(git add:*)", "Bash(git commit:*)",
        # Read-only inspection.
        "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
        "Bash(grep:*)", "Bash(rg:*)", "Bash(wc:*)", "Bash(find:*)",
        "Bash(sed:*)", "Bash(awk:*)", "Bash(diff:*)",
        # The ticket's own tests, and a syntax check. guard.py further
        # requires a pytest run to name ONLY the ticket's test paths.
        "Bash(python:*)", "Bash(python3:*)",
        # ★ AND BARE `pytest`, which the first real developer run reached
        # for and could not use. guard.py allowed it -- it is the same
        # command, scoped the same way -- but this list did not, so every
        # attempt fell through to a prompt, and with
        # `--permission-prompts none` a prompt is a denial. The developer
        # ran no test at all and said so, honestly, in its summary.
        "Bash(pytest:*)",
        # ★ AND THE SAME TWO WITH AN ENVIRONMENT ASSIGNMENT IN FRONT.
        # These rules are matched as literal command prefixes, so
        # `PYTHONPATH=. pytest ...` does not match `Bash(pytest:*)`
        # however the guard feels about it. guard.py accepts an allowed
        # assignment before any allowed command; these two spellings are
        # the ones an agent actually reaches for, and developer.md tells
        # the developer the prefix is not needed in the worktree.
        "Bash(PYTHONPATH=. pytest:*)", "Bash(PYTHONPATH=. python:*)",
    ],
}

# ★ THE INBOX SESSION GETS THE DEVELOPER'S LIST, REFERENCED AND NOT
# COPIED. The two roles may run the same commands; what differs is SCOPE,
# and that lives in guard.py -- the developer's pytest run must name the
# ticket's own tests, because a coordinator gate runs the full suite for
# it, while the inbox session has no gate in front of it and runs the
# suite itself. Two literal copies of this list would drift, and then one
# role would quietly be able to run something the other could not for no
# reason anybody wrote down.
ROLE_ALLOWED_TOOLS["inbox"] = list(ROLE_ALLOWED_TOOLS["developer"])

DENIED_TOOLS = [
    "Bash(git push:*)", "Bash(git tag:*)", "Bash(railway:*)",
    "Bash(gh:*)", "Bash(sqlite3:*)", "Bash(curl:*)", "Bash(rm:*)",
    "Agent", "WebFetch", "WebSearch",
]

# ★ PER-ROLE PERMISSION MODE, and why the developer's differs. With
# `--permission-prompts none` anything that would prompt is DENIED, and in
# `auto` mode an ordinary file edit can be a prompt -- which would leave
# the developer unable to edit the very files its ticket names. acceptEdits
# auto-accepts edits and nothing else; every Bash command still passes the
# deny rules and the guard hook, which bind in EVERY mode, and the
# read-only roles stay in auto because they have no write tools at all.
# bypassPermissions is never used by any role.
ROLE_PERMISSION_MODE = {
    "manager": "auto", "developer": "acceptEdits", "reviewer": "auto",
    "inbox": "acceptEdits",
}

DEFAULT_MODELS = {"manager": "sonnet", "developer": "opus", "reviewer": "opus",
                  "inbox": "opus"}
# A whole ticket, including reading CLAUDE.md, running the suite and
# writing the report, so a higher ceiling than the developer's -- and
# still a ceiling, because --max-turns is the only thing bounding how
# long an unwatched session can keep going.
DEFAULT_MAX_TURNS = {"manager": 40, "developer": 120, "reviewer": 60,
                     "inbox": 200}


class AuthUnavailable(RuntimeError):
    """The CLI could not authenticate. Requirement 15: stop and report."""


class RateLimited(RuntimeError):
    """A subscription limit was hit. Stop the day; never switch to API billing."""


@dataclass
class AgentRun:
    role: str
    ok: bool
    session_id: str | None
    result_text: str
    structured: dict | None
    raw: dict
    returncode: int


def child_env(role: str | None = None, ticket_tests: list[str] | None = None,
              ticket_files: list[str] | None = None) -> dict:
    """The environment a role runs in: the operator's, minus every secret.

    The role and the ticket's test paths are passed to guard.py this way,
    because a PreToolUse hook gets the tool call and the session -- not
    the coordinator's own state. The hook treats a missing role as "no
    Bash at all", so forgetting to set it fails closed.
    """
    env = {key: value for key, value in os.environ.items()}
    for key in SCRUBBED_ENV:
        env.pop(key, None)
    # Also drop anything that merely looks like a credential, so a new
    # variable added to .env later is covered without editing this list.
    for key in list(env):
        upper = key.upper()
        if any(mark in upper for mark in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            env.pop(key, None)
    env["CARDFOUNDRY_AUTODEV"] = "1"
    if role:
        env["CARDFOUNDRY_AUTODEV_ROLE"] = role
    if ticket_tests:
        env["CARDFOUNDRY_AUTODEV_TICKET_TESTS"] = ":".join(ticket_tests)
    if ticket_files:
        # The files the ticket may touch, so the guard can scope a syntax
        # check to them. Separate from the tests on purpose: pytest is
        # scoped to the tests alone, because running a source file as a
        # test is not a thing to allow.
        env["CARDFOUNDRY_AUTODEV_TICKET_FILES"] = ":".join(ticket_files)
    return env


def claude_bin() -> str:
    return os.environ.get("CARDFOUNDRY_AUTODEV_CLAUDE", "claude")


def effective_settings(evidence_dir: Path | None = None,
                       source: Path | None = None) -> Path:
    """settings.autodev.json with the hook path resolved to an absolute one.

    ★ WHY THIS EXISTS. The shipped file names the hook as
    `$CLAUDE_PROJECT_DIR/scripts/autodev/guard.py`, and for a role the
    project dir is the WORKTREE -- which branches off main and therefore
    does not contain scripts/autodev at all until this work is merged. The
    hook command would be missing, and with `"onFailure": "block"` a
    missing hook blocks every tool call, so the first pilot would have
    failed for a reason that had nothing to do with the ticket. Resolving
    the path to this checkout's own guard.py fixes it, and keeps ONE guard
    for every worktree rather than whatever copy a branch happens to
    carry.
    """
    source = source or SETTINGS_PATH
    data = json.loads(source.read_text())
    for matcher in data.get("hooks", {}).get("PreToolUse", []):
        for hook in matcher.get("hooks", []):
            if "guard.py" in str(hook.get("command", "")):
                hook["command"] = str(HERE / "guard.py")
    # Named after the source, so an inbox run and an AutoDev run writing
    # into the same evidence directory cannot overwrite each other's.
    target = (evidence_dir or HERE) / f"{source.stem}.effective.json"
    target.write_text(json.dumps(data, indent=2, sort_keys=True))
    return target


def build_command(role: str, prompt: str, worktree: Path, *,
                  model: str | None = None, max_turns: int | None = None,
                  resume: str | None = None,
                  settings_path: Path | None = None) -> list[str]:
    role_file = ROLES_DIR / f"{role}.md"
    schema_file = SCHEMAS_DIR / f"{role}.json"
    cmd = [
        claude_bin(), "-p", prompt,
        "--output-format", "json",
        "--permission-mode", ROLE_PERMISSION_MODE[role],
        "--permission-prompts", "none",
        "--setting-sources", "project",
        "--settings", str(settings_path or SETTINGS_PATH),
        "--add-dir", str(worktree),
        "--tools", *ROLE_TOOLS[role],
        "--disallowedTools", *DENIED_TOOLS,
        "--max-turns", str(max_turns or DEFAULT_MAX_TURNS[role]),
        "--model", model or DEFAULT_MODELS[role],
    ]
    if ROLE_ALLOWED_TOOLS[role]:
        cmd += ["--allowedTools", *ROLE_ALLOWED_TOOLS[role]]
    if role_file.exists():
        cmd += ["--append-system-prompt", role_file.read_text()]
    if schema_file.exists():
        cmd += ["--json-schema", schema_file.read_text()]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def _classify_failure(raw: dict, output: str) -> None:
    """Turn the two failures that must stop the system into exceptions."""
    text = " ".join([
        str(raw.get("result") or ""), str(raw.get("subtype") or ""), output[:2000],
    ]).lower()
    if "failed to authenticate" in text or "oauth" in text and "expired" in text:
        raise AuthUnavailable(
            "the Claude Code CLI could not authenticate: " + text[:300]
        )
    for mark in ("rate limit", "usage limit", "quota exceeded", "too many requests"):
        if mark in text:
            raise RateLimited("subscription limit reached: " + text[:300])


def run_role(role: str, prompt: str, worktree: Path, evidence_dir: Path,
             ticket_tests: list[str] | None = None,
             ticket_files: list[str] | None = None, **kwargs) -> AgentRun:
    """One phase, one process, everything it said kept on disk."""
    kwargs.setdefault("settings_path", effective_settings(evidence_dir))
    cmd = build_command(role, prompt, worktree, **kwargs)
    (evidence_dir / f"{role}-command.txt").write_text(
        "\n".join(cmd[:2] + ["<prompt elided: see %s-prompt.txt>" % role] + cmd[3:])
    )
    (evidence_dir / f"{role}-prompt.txt").write_text(prompt)
    try:
        proc = subprocess.run(
            cmd, cwd=str(worktree),
            env=child_env(role=role, ticket_tests=ticket_tests,
                          ticket_files=ticket_files),
            capture_output=True, text=True, timeout=3600,
        )
        stdout, stderr, code = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired:
        logger.warning("autodev agents: %s timed out after 3600s", role)
        (evidence_dir / f"{role}-raw.json").write_text("timeout")
        return AgentRun(role, False, None, "timed out", None, {}, 124)
    except OSError as exc:
        logger.warning("autodev agents: cannot start %s: %s", role, exc)
        raise AuthUnavailable(f"cannot start the Claude CLI: {exc}") from exc

    (evidence_dir / f"{role}-raw.json").write_text(stdout)
    if stderr.strip():
        (evidence_dir / f"{role}-stderr.txt").write_text(stderr)
    try:
        raw = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (ValueError, IndexError) as exc:
        # Logged, not swallowed: unparseable output is a real failure
        # mode and the evidence file above holds exactly what came back.
        logger.warning("autodev agents: %s returned unparseable JSON: %s", role, exc)
        raw = {}
    _classify_failure(raw, stdout + stderr)
    structured = raw.get("structured_output")
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except ValueError:
            structured = None
    ok = code == 0 and not raw.get("is_error")
    return AgentRun(
        role=role, ok=ok, session_id=raw.get("session_id"),
        result_text=str(raw.get("result") or ""), structured=structured,
        raw=raw, returncode=code,
    )


def preflight_auth() -> tuple[bool, str]:
    """Can a role actually get a model turn? Requirement 15's gate.

    ★ `claude auth status` IS NOT ENOUGH, measured 2026-10-31. Inside the
    Claude desktop app's own shell `auth status` reports
    `loggedIn: true` -- it sees the host application's session -- while a
    real `claude -p` run in that SAME shell fails with "Failed to
    authenticate: OAuth session expired and could not be refreshed". A
    preflight that trusted the cheap check would hand the blocker to the
    developer phase twenty minutes later, after a worktree had been built
    and the operator had been told the run was under way.

    So this asks the only question that matters: one real, minimal turn,
    no tools, one turn maximum. It costs a few tokens and it is the
    difference between a clear blocker and a confusing failure.
    """
    try:
        status = subprocess.run(
            [claude_bin(), "auth", "status"], capture_output=True, text=True,
            timeout=60, env=child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("autodev agents: auth preflight failed: %s", exc)
        return False, f"cannot run `{claude_bin()} auth status`: {exc}"
    try:
        data = json.loads(status.stdout or "{}")
    except ValueError:
        data = {}
    if data.get("loggedIn") is not True:
        return False, (
            "the CLI is not logged in (`loggedIn: false`). Run "
            "`claude setup-token` or `claude auth login` in a Terminal, "
            "then re-run."
        )
    method = data.get("authMethod")
    try:
        probe = subprocess.run(
            [claude_bin(), "-p", "Reply with exactly: ok",
             "--output-format", "json", "--max-turns", "1",
             "--tools", "", "--permission-mode", "dontAsk",
             "--no-session-persistence"],
            capture_output=True, text=True, timeout=180, env=child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("autodev agents: auth probe failed: %s", exc)
        return False, f"the live auth probe could not run: {exc}"
    try:
        raw = json.loads((probe.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, (
            "the live auth probe returned nothing readable: "
            + (probe.stdout or probe.stderr or "")[:200]
        )
    if raw.get("is_error"):
        return False, (
            f"`auth status` says logged in via {method}, but a real turn "
            f"failed: {str(raw.get('result'))[:200]}. Run "
            "`claude setup-token` in a Terminal and re-run."
        )
    return True, (
        f"logged in via {method}, and one live turn succeeded "
        f"(session {raw.get('session_id')})"
    )


def max_turns_preflight() -> list[str]:
    """Every role's command must carry a positive --max-turns. Returns
    the failures, empty when all is well.

    ★ WHY THIS IS A PREFLIGHT AND NOT A COMMENT. --max-turns is the only
    thing bounding how long one role can keep going, and an unattended
    loop is exactly where an unbounded role costs the most: nobody is
    watching to stop it. The flag is built in build_command, so a refactor
    that dropped it or a DEFAULT_MAX_TURNS entry that went missing for a
    new role would leave no trace until a role ran away. This proves the
    built command for every role, rather than trusting the dict.
    """
    failures = []
    for role in sorted(ROLE_TOOLS):
        if role not in DEFAULT_MAX_TURNS:
            failures.append(f"{role}: no DEFAULT_MAX_TURNS entry")
            continue
        cmd = build_command(role, "preflight", Path("/tmp"))
        if "--max-turns" not in cmd:
            failures.append(f"{role}: the built command has no --max-turns")
            continue
        value = cmd[cmd.index("--max-turns") + 1]
        try:
            turns = int(value)
        except ValueError:
            failures.append(f"{role}: --max-turns {value!r} is not an integer")
            continue
        if turns < 1:
            failures.append(f"{role}: --max-turns {turns} is not a positive limit")
    return failures
