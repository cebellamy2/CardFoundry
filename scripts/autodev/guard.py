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

# The roles that have a shell at all. A role not named here is refused,
# and so is a missing one -- forgetting to set CARDFOUNDRY_AUTODEV_ROLE
# fails closed, which is why the check is membership and not inequality.
SHELL_ROLES: frozenset[str] = frozenset({"developer", "inbox"})

# ★ WHAT THE DEVELOPER MAY RUN. Narrow by design: read-only inspection,
# the ticket's own tests, and LOCAL git. Every other role has no Bash
# tool at all, and a missing role is treated as having none either, so
# forgetting to set it fails closed.
ALLOWED_DEVELOPER_SEGMENTS: tuple[tuple[str, str], ...] = (
    (r"^git\s+(status|diff|log|show|add|commit)\b", "local git"),
    (r"^(ls|cat|head|tail|grep|rg|wc|find|sed|awk|diff|pwd|echo|basename|dirname|"
     r"sort|uniq|cut|tr|nl|true)\b", "read-only inspection"),
    (r"^(?:\S*python3?)\s+-m\s+(?:pytest|compileall|py_compile)\b",
     "tests and a syntax check"),
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

# ★ AN ENVIRONMENT ASSIGNMENT IN FRONT OF A COMMAND. The first real
# developer run reached for `PYTHONPATH=. python -m pytest ...` -- the
# habit every Python developer has -- and the allowlist refused it,
# because every pattern above is anchored with ^ and the assignment was
# what sat at the start of the line. The command itself was one the
# developer was meant to be able to run.
#
# ★ WHY A NAME ALLOWLIST AND NOT "ANY NAME=VALUE". An assignment can
# change what the command IS: `PATH=/tmp/evil pytest` runs a different
# binary, and LD_PRELOAD / DYLD_INSERT_LIBRARIES load someone else's
# code into it. So the names are listed, and a name not on the list is
# refused -- the same fail-closed stance as an unrecognised path in
# policy.py. Nothing here can carry a credential: child_env has already
# dropped every variable whose name looks like one.
ENV_PREFIX = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)="
    r'("[^"]*"|\'[^\']*\'|[^\s]*)'
    r"(\s+|$)"
)
ALLOWED_ENV_PREFIX_NAMES: frozenset[str] = frozenset({
    "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONWARNINGS",
    "PYTHONHASHSEED", "PYTHONUNBUFFERED", "PY_COLORS", "NO_COLOR",
    "FORCE_COLOR", "COLUMNS", "LINES", "TZ",
})

# Flags whose VALUE is the next token, not a test path. `-k broken` was
# read as a request to run a test file called "broken" and refused with
# the baffling message that it was not one of the ticket's tests -- which
# is exactly what happened on the first real developer run.
PYTEST_VALUE_FLAGS: frozenset[str] = frozenset({
    "-k", "-m", "-n", "-o", "-p", "-r", "-W", "-c",
    "--deselect", "--ignore", "--ignore-glob", "--maxfail", "--junitxml",
    "--rootdir", "--tb", "--durations", "--override-ini", "--log-level",
    "--log-cli-level", "--dist", "--color",
})

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


def _strip_env_prefix(segment: str) -> tuple[str, str | None]:
    """(the command with its leading assignments removed, refusal or None).

    Textual rather than shlex-based on purpose: the allowlist patterns
    are regexes over the segment as written, and rejoining shlex tokens
    would requote the command (`git commit -m \'x\'` becomes
    `git commit -m x`), which is not the string those patterns were
    written against.
    """
    rest = segment
    while True:
        match = ENV_PREFIX.match(rest)
        if not match:
            return rest.strip(), None
        name = match.group(1)
        if name.upper() not in ALLOWED_ENV_PREFIX_NAMES:
            return rest.strip(), (
                f"{name} may not be set in front of a command; only "
                f"{', '.join(sorted(ALLOWED_ENV_PREFIX_NAMES))} may be, "
                "because an assignment can change which binary runs"
            )
        remainder = rest[match.end():]
        if not remainder.strip():
            return "", "an environment assignment with no command after it"
        rest = remainder


def _env_paths(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {part.strip() for part in raw.split(":") if part.strip()}


def _ticket_tests() -> set[str]:
    return _env_paths("CARDFOUNDRY_AUTODEV_TICKET_TESTS")


def _ticket_paths() -> set[str]:
    """Every path this ticket may touch: its files AND its tests.

    A syntax check is run over the files being edited, not only over the
    tests, so it is scoped to both. pytest stays scoped to the tests
    alone -- running a source file as a test is not a thing to allow.
    """
    return _ticket_tests() | _env_paths("CARDFOUNDRY_AUTODEV_TICKET_FILES")


def _out_of_scope(paths: list[str], allowed: set[str]) -> str | None:
    """The first path that is not one of the ticket's own, if any."""
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
        return cleaned
    return None


def _argument_paths(tokens: list[str], *,
                    value_flags: frozenset[str] = frozenset()) -> list[str]:
    """The path-shaped arguments in a command's tail.

    Flags are skipped, and so is the VALUE of a flag that takes one --
    without which `-k broken` reads as a test file called "broken".
    """
    paths: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in value_flags:
            skip_next = True
            continue
        if token.startswith("-") or token.isdigit():
            continue
        # `2>&1` is a redirection, not a path. _redirection_breach has
        # already passed judgement on the redirections in this segment.
        if REDIRECT_TOKEN.match(token):
            continue
        paths.append(token)
    return paths


def _invocation_index(tokens: list[str], names: tuple[str, ...], *,
                      module_only: bool = False) -> int | None:
    """Where this command actually invokes `names`, or None.

    ★ WHY NOT `if "pytest" in tokens`. That was the old test, and it got
    the question wrong in both directions: `grep -rn pytest scripts/` was
    treated as a pytest run and refused for naming scripts/ as a test
    path, while `.venv/bin/pytest tests/someone_elses.py` was not
    recognised at all and skipped the scope check entirely. An invocation
    is the first token, or the token right after `-m`; anything else is
    an argument that happens to be spelled the same.
    """
    for index, token in enumerate(tokens):
        if Path(token).name not in names:
            continue
        if tokens[index - 1:index] == ["-m"]:
            return index
        if index == 0 and not module_only:
            return index
    return None


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
    index = _invocation_index(tokens, ("pytest", "py.test"))
    if index is None:
        return None
    paths = _argument_paths(tokens[index + 1:], value_flags=PYTEST_VALUE_FLAGS)
    allowed = _ticket_tests()
    if not paths:
        return (
            "a bare pytest runs the whole suite; that is the coordinator's "
            "gate, which keeps the evidence"
        )
    if not allowed:
        return "no ticket test paths are in scope for this run"
    outside = _out_of_scope(paths, allowed)
    if outside:
        return f"{outside} is not one of this ticket's tests {sorted(allowed)}"
    return None


def _py_compile_scope_breach(segment: str) -> str | None:
    """A syntax check must name only files this ticket may touch.

    `python -m py_compile` is read-only -- it writes a .pyc into
    __pycache__ and nothing else -- so this is about scope, not safety:
    an agent checking someone else's file is working outside its ticket,
    and the diff-scope gate would have nothing to say about it.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return "unparseable command"
    index = _invocation_index(tokens, ("py_compile",), module_only=True)
    if index is None:
        return None
    paths = _argument_paths(tokens[index + 1:])
    if not paths:
        return "py_compile with no file names nothing to check"
    allowed = _ticket_paths()
    if not allowed:
        return "no ticket paths are in scope for this run"
    outside = _out_of_scope(paths, allowed)
    if outside:
        return f"{outside} is not one of this ticket's paths {sorted(allowed)}"
    return None


def _inbox_write_decision(cleaned: str) -> tuple[bool, str]:
    """The inbox session's fence: the ticket's OWN declared paths.

    ★ A DIFFERENT FENCE, NOT A WIDER ONE, and the difference is the whole
    reason the inbox exists. policy.py reserves the deploy guard, the
    schema and the money services for the operator however a ticket is
    marked -- right for AutoDev's unattended five-minute builder, and the
    reason it could not have taken two of the three items shipped in
    v2.110.0. An inbox ticket may declare such a path, so policy.py is
    not the gate here; what IS the gate is that the diff may touch
    NOTHING the ticket did not declare, which is stricter than policy.py
    in the other direction, and checked on every single tool call rather
    than once at the end.

    ★ AND IT FAILS CLOSED. No declared paths means no edits at all, so a
    runner that forgot to pass the ticket's files blocks the session
    instead of freeing it. On top of this, inbox.ABSOLUTE_DENY has
    already refused any ticket naming credentials, the database, backups,
    imports, the audit trail or claude/ -- before a session was started.
    """
    allowed = _ticket_paths()
    if not allowed:
        return False, (
            "no ticket paths are in scope for this run, so there is nothing "
            "this session may write"
        )
    outside = _out_of_scope([cleaned], allowed)
    if outside:
        return False, (
            f"{outside} is not one of this ticket's declared paths "
            f"{sorted(allowed)}"
        )
    inbox = _load_inbox()
    denial = inbox._denied(cleaned)
    if denial:
        # Belt and braces: the ticket could not have declared this path
        # and been accepted, so reaching here means the ticket and the
        # environment disagree, which is a reason to stop rather than to
        # work out which one is right.
        return False, f"absolutely denied: {denial}"
    return True, "the path is one this ticket declared"


def _load_inbox():
    """inbox.py, imported rather than copied, and the hook blocks if it
    cannot be: the absolute denylist has one definition, like the
    protected-path list in _load_policy."""
    sys.path.insert(0, str(HERE.parents[1]))
    try:
        from scripts.autodev import inbox  # noqa: PLC0415 -- late and guarded
    except Exception as exc:  # pragma: no cover - fail-closed path
        _fail_closed(f"cannot read the inbox denylist: {exc}")
    return inbox


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
        if role not in SHELL_ROLES:
            return False, (
                f"role {role or 'unset'!r} has no shell; only "
                f"{' and '.join(sorted(SHELL_ROLES))} run commands, and only "
                "the allowlisted ones"
            )
        for raw_segment in SEGMENT_SPLIT.split(command):
            segment = raw_segment.strip()
            if not segment:
                continue
            # An allowed assignment in front of the command is removed
            # before the allowlist sees it; a disallowed one refuses here.
            segment, env_refusal = _strip_env_prefix(segment)
            if env_refusal:
                return False, env_refusal
            if not any(re.match(pattern, segment)
                       for pattern, _ in ALLOWED_DEVELOPER_SEGMENTS):
                return False, (
                    f"not on the developer allowlist: {segment[:120]!r}"
                )
            redirect = _redirection_breach(segment)
            if redirect:
                return False, redirect
            # ★ SCOPE IS PER-ROLE; THE ALLOWLIST IS NOT. The developer's
            # pytest run must name the ticket's own tests, because a
            # coordinator gate runs the full suite for it and keeps the
            # evidence. The inbox session has no gate in front of it, and
            # CLAUDE.md requires the whole suite before a change is
            # finished, so a bare `pytest` is the correct command there
            # rather than a breach. Everything else -- the denylist, the
            # allowlist, the redirection check, the environment-prefix
            # check -- binds both roles identically.
            if role == "developer":
                breach = _pytest_scope_breach(segment)
                if breach:
                    return False, f"pytest out of scope: {breach}"
                breach = _py_compile_scope_breach(segment)
                if breach:
                    return False, f"py_compile out of scope: {breach}"
        return True, f"every segment is on the {role} allowlist"
    if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        target = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if not target:
            return False, f"{tool} with no file path"
        cleaned = target.replace(str(HERE.parents[1]) + "/", "")
        role = os.environ.get("CARDFOUNDRY_AUTODEV_ROLE", "")
        if role == "inbox":
            return _inbox_write_decision(cleaned)
        policy = _load_policy()
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
