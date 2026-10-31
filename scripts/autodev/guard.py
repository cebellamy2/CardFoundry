#!/usr/bin/env python3
"""AutoDev's PreToolUse guard. Exit 2 blocks the tool call.

★ EXIT 2, NOT A JSON "deny". Claude Code's hook documentation is explicit
that exit 2 blocks the action even when the hook also prints an allow
decision, and even under bypassPermissions. A policy hook that exits 0
with JSON is advisory; this one is not meant to be advisory.

★ IT FAILS CLOSED. If this hook cannot read the policy module, or cannot
parse its own input, it BLOCKS. That is the same stance
scripts/hooks/pre-push takes when its cron list is unreadable: a guard
that quietly stops guarding is the failure the guard exists to prevent.

★ IT IS THE SECOND LAYER, NOT THE FIRST. settings.autodev.json's deny
rules bind in every permission mode and are the load-bearing control;
this hook catches the command shapes a rule's glob does not quite cover,
because Claude Code documents the per-handler `if:` matcher as
best-effort. Neither is the first layer -- that is the scrubbed
environment, which means a blocked command could not have authenticated
anyway.

Wired in via settings.autodev.json. Reads the hook JSON on stdin.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Command shapes no role may run, with the reason the operator would give.
DENIED_COMMANDS: tuple[tuple[str, str], ...] = (
    (r"\bgit\s+push\b", "AutoDev never pushes; a human pushes"),
    (r"\bgit\s+tag\b", "tagging is part of releasing, which is the operator's"),
    (r"--no-verify", "there is no emergency exception to the pre-push hook"),
    (r"\bgit\s+(checkout|switch)\s+(main|origin/main)\b",
     "work stays on the task's own worktree branch"),
    (r"\bgit\s+remote\b", "remotes are not AutoDev's to change"),
    (r"\brailway\b", "production operations are the operator's"),
    (r"\bgh\s+(pr|release|api|repo)\b", "AutoDev opens nothing on GitHub"),
    (r"--confirm\b", "a --confirm run writes; those need operator approval"),
    (r"\bsqlite3\b", "the inventory database is not AutoDev's to open"),
    (r"cardfoundry\.db", "the inventory database is not AutoDev's to open"),
    (r"(?:^|[\s/])(?:cat|less|more|head|tail|grep|sed|awk)\b[^\n]*\.env\b",
     "never read credentials"),
    (r"\brm\s+-[a-zA-Z]*[rf]", "destructive deletes need a human"),
    (r"\b(curl|wget|http|httpie)\b[^\n]*manapool", "no marketplace calls"),
    (r"\bpip\s+(install|uninstall)\b", "the environment is not AutoDev's to change"),
    (r"\buvicorn\b", "the coordinator runs the app, not the agent"),
    (r"\b(?:python3?|\./)?(?:scheduled_|production_|correct_|backfill_|retro_|"
     r"batch_delete|clean_rebuild|floor_correction|import_consignment)",
     "write scripts are run by the operator, never by an agent"),
    (r"\bcrontab\b|\blaunchctl\b", "scheduling is disabled by the approval"),
    (r"\bchmod\s+[0-7]*7[0-7]*\b.*hooks", "the deploy hooks stay as they are"),
    # ★ BYPASS SHAPES. Each of these can smuggle a denied command past a
    # per-segment check, so they are refused on sight rather than parsed.
    (r"\$\(|`", "command substitution could hide anything"),
    (r"\beval\b|\bexec\b|\bsource\b|^\s*\.\s+", "re-entering the shell"),
    (r"\b(ba|z|)sh\s+-c\b|\bxargs\b", "running a command through another shell"),
    (r"\bsed\b[^\n]*\s-i\b", "sed -i writes files behind the Edit tool's back"),
    (r"&\s*$", "backgrounding a command hides its outcome"),
)

# ★ WHAT THE DEVELOPER MAY RUN. Narrow by design: read-only inspection,
# the ticket's own tests, and LOCAL git. Every other role has no Bash
# tool at all, and a missing role is treated as having none either, so
# forgetting to set it fails closed.
ALLOWED_DEVELOPER_SEGMENTS: tuple[tuple[str, str], ...] = (
    (r"^git\s+(status|diff|log|show|add|commit)\b", "local git"),
    (r"^(ls|cat|head|tail|grep|rg|wc|find|sed|awk|diff|pwd|echo|basename|dirname|"
     r"sort|uniq|cut|tr|nl|true)\b", "read-only inspection"),
    (r"^(?:\S*python3?)\s+-m\s+(?:pytest|compileall)\b", "tests and a syntax check"),
    # The smoke test reached for bare `pytest` and for `cd` first. Both
    # are fine: a pytest run is scoped to the ticket's tests below
    # whichever way it is spelled, and a relative cd cannot leave the
    # worktree. An absolute cd can, so it is not allowed.
    (r"^(?:\S*/)?pytest\b", "the ticket's tests"),
    # Relative, and no `..`: `cd ../../../etc` is relative too, and would
    # walk straight out of the worktree.
    (r"^cd\s+(?!/)(?!.*\.\.)[^\s;&|]+$", "moving within the worktree"),
    (r"^which\b|^type\s", "finding an interpreter"),
)

# Splitting on the shell operators that join commands. A pipeline is
# fine when BOTH sides are allowed (grep x | head), which is why every
# segment is checked rather than just the first.
SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")

# A redirection as the SHELL sees it: an operator token, not any '>' in
# the text. The regex version of this check matched the '>' inside
# "Co-Authored-By: ... <noreply@anthropic.com>" and so blocked every
# attributed commit made with -m -- found by the real developer smoke
# test on 2026-10-31, not by a unit test. shlex keeps a quoted '>' inside
# its token, which is exactly the distinction that was missing.
REDIRECT_TOKEN = re.compile(r"^(?:\d*|&)>>?(.*)$")
REDIRECT_SAFE_TARGETS = ("/dev/null", "&1", "&2")


def _redirection_breach(segment: str) -> str | None:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return "unparseable command"
    for index, token in enumerate(tokens):
        match = REDIRECT_TOKEN.match(token)
        if not match:
            continue
        target = match.group(1) or (
            tokens[index + 1] if index + 1 < len(tokens) else ""
        )
        if target not in REDIRECT_SAFE_TARGETS:
            return f"redirecting output into {target or 'a file'}"
    return None


def _ticket_tests() -> set[str]:
    raw = os.environ.get("CARDFOUNDRY_AUTODEV_TICKET_TESTS", "")
    return {part.strip() for part in raw.split(":") if part.strip()}


def _pytest_scope_breach(segment: str) -> str | None:
    """A pytest run must name ONLY the ticket's own test paths.

    A bare `pytest` runs the whole suite, which is the coordinator's gate
    to run and keep evidence for -- not something an agent does and then
    reports on. And a run naming someone else's test file is outside the
    ticket.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return "unparseable command"
    if "pytest" not in tokens:
        return None
    paths = [
        token for token in tokens[tokens.index("pytest") + 1:]
        if not token.startswith("-") and not token.isdigit()
        # `2>&1` is a redirection, not a test path. Treating it as one
        # refused `pytest tests/x.py -q 2>&1 | tail -5` with the
        # baffling message that 2>&1 was not one of the ticket's tests.
        # _redirection_breach has already passed judgement on the
        # redirections in this segment.
        and not REDIRECT_TOKEN.match(token)
    ]
    allowed = _ticket_tests()
    if not paths:
        return (
            "a bare pytest runs the whole suite; that is the coordinator's "
            "gate, which keeps the evidence"
        )
    if not allowed:
        return "no ticket test paths are in scope for this run"
    for path in paths:
        cleaned = path.split("::")[0]
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        # An absolute path inside the worktree is the same file as the
        # ticket's relative one; anything else is out of scope.
        if cleaned in allowed or any(
            cleaned.endswith("/" + candidate) for candidate in allowed
        ):
            continue
        return f"{cleaned} is not one of this ticket's tests {sorted(allowed)}"
    return None


def _fail_closed(reason: str) -> None:
    print(f"autodev guard: BLOCKED -- {reason}", file=sys.stderr)
    sys.exit(2)


def _load_policy():
    """One source of truth for protected paths, imported rather than copied.

    Copying the path list here is how two callers start disagreeing (the
    ACTIVE_ALLOCATION_STATUSES drift this repo already tracks), so the
    hook imports policy.py. If that import fails the hook blocks.
    """
    sys.path.insert(0, str(HERE))
    try:
        import policy  # noqa: PLC0415 -- deliberately late and guarded
    except Exception as exc:  # pragma: no cover - fail-closed path
        _fail_closed(f"cannot read the eligibility policy: {exc}")
    return policy


def decide(payload: dict) -> tuple[bool, str]:
    """(allowed, reason). Called by main() and by the tests directly."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        if not command.strip():
            return False, "an empty Bash command is not something to wave through"
        # Denials first, against the WHOLE line: a denied shape anywhere
        # in it blocks, before any segment is considered allowed.
        for pattern, reason in DENIED_COMMANDS:
            if re.search(pattern, command):
                return False, f"{reason} (matched {pattern!r})"
        role = os.environ.get("CARDFOUNDRY_AUTODEV_ROLE", "")
        if role != "developer":
            return False, (
                f"role {role or 'unset'!r} has no shell; only the developer "
                "runs commands, and only the allowlisted ones"
            )
        for raw_segment in SEGMENT_SPLIT.split(command):
            segment = raw_segment.strip()
            if not segment:
                continue
            if not any(re.match(pattern, segment)
                       for pattern, _ in ALLOWED_DEVELOPER_SEGMENTS):
                return False, (
                    f"not on the developer allowlist: {segment[:120]!r}"
                )
            redirect = _redirection_breach(segment)
            if redirect:
                return False, redirect
            breach = _pytest_scope_breach(segment)
            if breach:
                return False, f"pytest out of scope: {breach}"
        return True, "every segment is on the developer allowlist"
    if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        policy = _load_policy()
        target = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if not target:
            return False, f"{tool} with no file path"
        cleaned = target.replace(str(HERE.parents[1]) + "/", "")
        reason = policy._forbidden_reason(cleaned)
        if reason:
            return False, f"protected path: {reason}"
        return True, "path is not protected"
    return True, f"{tool} is not gated by this hook"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        _fail_closed(f"unparseable hook input: {exc}")
    if not isinstance(payload, dict):
        _fail_closed("hook input was not a JSON object")
    allowed, reason = decide(payload)
    if not allowed:
        _fail_closed(reason)
    # Exit 0 with no output: the call proceeds through the ordinary
    # permission flow, which still has the deny rules in front of it.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
