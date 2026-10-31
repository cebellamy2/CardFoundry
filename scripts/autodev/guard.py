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
import re
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
)


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
        for pattern, reason in DENIED_COMMANDS:
            if re.search(pattern, command):
                return False, f"{reason} (matched {pattern!r})"
        return True, "no denied command shape"
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
