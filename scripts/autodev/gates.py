"""The mechanical verification. The COORDINATOR runs this, never an agent.

★ WHY IT IS NOT THE DEVELOPER'S JOB TO SAY THE TESTS PASSED. CLAUDE.md's
testing rule is that a test must assert the outcome the operator sees; the
same scepticism applies one level up. An agent reporting "all tests pass"
is a claim; this module is the evidence. Every gate's raw stdout/stderr is
written to the run directory, so "what did it actually say" is answerable
from disk rather than from a transcript.

★ A SKIPPED GATE IS REPORTED, NEVER FOLDED INTO A PASS. The project has
been bitten by exactly this (v2.17.0/.1: a skipped alarm looked like a
passing one), so GateResult carries `skipped` separately and the overall
verdict names every skip in its summary.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import policy, state

logger = logging.getLogger("cardfoundry")

GATE_TIMEOUT_SECONDS = 1800  # The full suite measured 9m13s on 2026-10-31.


def python_bin() -> str:
    """The interpreter the gates run under.

    A git worktree has no .venv of its own, so the gates deliberately use
    the main checkout's interpreter with the worktree on PYTHONPATH. That
    is the same interpreter the operator runs the suite with.
    """
    override = os.environ.get("CARDFOUNDRY_AUTODEV_PYTHON")
    if override:
        return override
    venv = state.REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


@dataclass
class GateResult:
    name: str
    ok: bool
    detail: str
    skipped: bool = False
    evidence: str | None = None


@dataclass
class GateVerdict:
    results: list[GateResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def skips(self) -> list[str]:
        return [result.name for result in self.results if result.skipped]

    def summary(self) -> str:
        parts = []
        for result in self.results:
            mark = "SKIPPED" if result.skipped else ("PASS" if result.ok else "FAIL")
            parts.append(f"{result.name}={mark}")
        return " ".join(parts)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skipped": self.skips,
            "results": [
                {
                    "name": r.name, "ok": r.ok, "skipped": r.skipped,
                    "detail": r.detail, "evidence": r.evidence,
                }
                for r in self.results
            ],
        }


def _run(cmd: list[str], *, cwd: Path, evidence: Path | None,
         env_extra: dict | None = None) -> tuple[int, str]:
    env = {**os.environ, **(env_extra or {})}
    # No marketplace credentials reach a gate either -- a test that could
    # authenticate is a test that could reach the live marketplace.
    for key in SCRUBBED_ENV:
        env.pop(key, None)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=GATE_TIMEOUT_SECONDS,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired:
        output = f"timed out after {GATE_TIMEOUT_SECONDS}s"
        code = 124
    except OSError as exc:
        # Catch-and-continue with a log: a missing binary is a gate
        # failure, not a reason for the whole run to vanish.
        logger.warning("autodev gate: cannot run %s: %s", cmd[0], exc)
        output = f"cannot run {cmd[0]}: {exc}"
        code = 127
    if evidence is not None:
        evidence.write_text(f"$ {' '.join(cmd)}\n\n{output}")
    return code, output


SCRUBBED_ENV = (
    "MANAPOOL_EMAIL", "MANAPOOL_API_TOKEN", "CARDFOUNDRY_ADMIN_PASSWORD",
    "CARDFOUNDRY_SERVICE_PASSWORD", "CARDFOUNDRY_BASE_URL",
    "RAILWAY_TOKEN", "RAILWAY_API_TOKEN", "ANTHROPIC_API_KEY",
)


def changed_files(worktree: Path, base: str) -> list[str]:
    code, output = _run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=worktree, evidence=None,
    )
    if code != 0:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def diff_scope_breaches(worktree: Path, base: str, allowed: list[str],
                        tests: list[str]) -> list[str]:
    """Files the diff touched that the ticket never named.

    New test files are allowed without being listed, because "write a
    test for it" is part of every ticket and naming the file in advance
    would mean guessing its name. Everything else must be declared.
    """
    permitted = {policy._repo_relative(path) for path in allowed + tests}
    breaches = []
    for path in changed_files(worktree, base):
        cleaned = policy._repo_relative(path)
        if cleaned in permitted:
            continue
        if cleaned.startswith("tests/") and cleaned.endswith(".py"):
            continue
        breaches.append(cleaned)
    return breaches


def _non_comment_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def comment_only_breaches(worktree: Path, base: str, paths: list[str]) -> list[str]:
    """Prove a declared comment-only change really is one.

    Two independent proofs, because either alone is weak: every added and
    removed line must be a comment line, AND the file's non-comment lines
    must be identical before and after. The second is what actually
    matters for a config file like scripts/hooks/deploy-guard-crons --
    the hook parses the non-comment lines, so if those are byte-identical
    the hook's behaviour cannot have changed.
    """
    breaches = []
    for path in paths:
        code, diff = _run(
            ["git", "diff", "-U0", f"{base}...HEAD", "--", path],
            cwd=worktree, evidence=None,
        )
        if code != 0:
            breaches.append(f"{path}: cannot read diff")
            continue
        for line in diff.splitlines():
            if line.startswith(("+++", "---", "@@", "diff ", "index ")):
                continue
            if line.startswith(("+", "-")):
                body = line[1:].strip()
                if body and not body.startswith("#"):
                    breaches.append(f"{path}: changed a non-comment line: {body[:80]}")
        before_code, before = _run(
            ["git", "show", f"{base}:{path}"], cwd=worktree, evidence=None,
        )
        after_path = worktree / path
        if before_code != 0 or not after_path.exists():
            breaches.append(f"{path}: cannot compare before/after")
            continue
        if _non_comment_lines(before) != _non_comment_lines(after_path.read_text()):
            breaches.append(f"{path}: the file's non-comment lines changed")
    return breaches


def run_all(worktree: Path, base: str, ticket, evidence_dir: Path) -> GateVerdict:
    """Every gate, in cheap-to-expensive order, but ALL of them run.

    Deliberately not short-circuiting on the first failure: one run
    should tell the operator everything that is wrong, the same reason
    cap_breaches reports every breach at once.
    """
    verdict = GateVerdict()
    # ★ THE GATES MUST LEAVE THE WORKTREE AS THEY FOUND IT. The first run
    # of this left __pycache__ and .pytest_cache behind, and the next
    # attempt's `git add -A` swept them into the diff -- which the
    # diff_scope gate then correctly refused. The gate was right; the
    # mess was ours. No bytecode, no pytest cache.
    env_extra = {
        "PYTHONPATH": str(worktree),
        "CARDFOUNDRY_AUTODEV": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    code, out = _run(["git", "diff", "--check", f"{base}...HEAD"],
                     cwd=worktree, evidence=evidence_dir / "git-diff-check.txt")
    verdict.results.append(GateResult(
        "git_diff_check", code == 0,
        "clean" if code == 0 else out.strip()[:400],
        evidence="git-diff-check.txt",
    ))

    breaches = diff_scope_breaches(worktree, base, ticket.files, ticket.tests)
    verdict.results.append(GateResult(
        "diff_scope", not breaches,
        "within the ticket's files" if not breaches
        else f"touched undeclared files: {breaches}",
    ))

    if ticket.comment_only_paths:
        comment_breaches = comment_only_breaches(
            worktree, base, ticket.comment_only_paths,
        )
        verdict.results.append(GateResult(
            "comment_only", not comment_breaches,
            "comments only, parsed meaning unchanged" if not comment_breaches
            else str(comment_breaches),
        ))

    code, out = _run(
        [python_bin(), "-m", "compileall", "-q", ".",
         "-x", r"/\.git/|/\.venv/|/claude/"],
        cwd=worktree, evidence=evidence_dir / "compileall.txt", env_extra=env_extra,
    )
    verdict.results.append(GateResult(
        "compileall", code == 0,
        "clean" if code == 0 else out.strip()[-400:], evidence="compileall.txt",
    ))

    if (worktree / "changelog_guard.py").exists():
        code, out = _run(
            [python_bin(), "changelog_guard.py", "--check-head"],
            cwd=worktree, evidence=evidence_dir / "changelog-guard.txt",
            env_extra=env_extra,
        )
        verdict.results.append(GateResult(
            "changelog_guard", code == 0,
            "ok" if code == 0 else out.strip()[-400:],
            evidence="changelog-guard.txt",
        ))
    else:
        # Named and reported rather than silently absent: a gate that
        # vanishes because its script is not there must not read as a
        # pass (v2.17.0/.1, where a skipped alarm looked like a passing
        # one).
        verdict.results.append(GateResult(
            "changelog_guard", True,
            "SKIPPED: no changelog_guard.py in this worktree", skipped=True,
        ))

    db = state.REPO_ROOT / "cardfoundry.db"
    if db.exists():
        code, out = _run(
            [python_bin(), "-c",
             "import sqlite3,sys;"
             "c=sqlite3.connect('file:%s?mode=ro',uri=True);"
             "r=c.execute('PRAGMA integrity_check').fetchone()[0];"
             "print(r);sys.exit(0 if r=='ok' else 1)" % db],
            cwd=worktree, evidence=evidence_dir / "integrity-check.txt",
        )
        verdict.results.append(GateResult(
            "sqlite_integrity", code == 0, out.strip()[:200],
            evidence="integrity-check.txt",
        ))
    else:
        verdict.results.append(GateResult(
            "sqlite_integrity", True,
            f"SKIPPED: no local database at {db}", skipped=True,
        ))

    if ticket.tests:
        code, out = _run(
            [python_bin(), "-m", "pytest", "-q", "-p", "no:cacheprovider",
             *ticket.tests],
            cwd=worktree, evidence=evidence_dir / "pytest-named.txt",
            env_extra=env_extra,
        )
        verdict.results.append(GateResult(
            "pytest_named", code == 0, out.strip()[-600:],
            evidence="pytest-named.txt",
        ))

    code, out = _run(
        [python_bin(), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=worktree, evidence=evidence_dir / "pytest-full.txt",
        env_extra=env_extra,
    )
    verdict.results.append(GateResult(
        "pytest_full", code == 0, out.strip()[-600:], evidence="pytest-full.txt",
    ))

    logger.info("autodev gates: %s", verdict.summary())
    return verdict
