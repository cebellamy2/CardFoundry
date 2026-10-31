"""The AutoDev coordinator: one task, one owner, one phase at a time.

★ WHY SEQUENTIAL. Parallel agents multiply subscription burn and put two
writers in one checkout, which is the exact trap this repo already
carries notes about. One task at a time is slower and it is the version
that can be reasoned about after the fact.

★ WHAT IT WILL NOT DO, BY CONSTRUCTION. It never pushes, never tags,
never commits to main, never calls Mana Pool, never runs a write script,
never touches the production database, and never schedules itself. The
operator's approval of 2026-10-31 is for local work on a worktree branch
that a human then reviews, and the controls that hold that are in
agents.py (scrubbed environment, deny rules, guard hook, worktree) rather
than in a prompt.

★ THE PHASE LOG IS THE RECOVERY LOG. Every phase writes a `started` row
before it runs and a `finished` row after. A `started` row with no
`finished` row IS the crash, and that is what `run` reads on startup to
decide whether to resume or restart. See state.audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import agents, gates, policy, state

logger = logging.getLogger("cardfoundry")

DEFAULT_CONFIG = {
    "max_tasks_per_day": 4,
    "max_attempts": 2,
    # CLAUDE.md's standard is that the full suite passes, so this is
    # false: a red suite fails the gate even when every failure was
    # already red at the base commit. The pytest_baseline gate reports
    # the distinction either way. Turning this on is the operator's
    # decision, not a convenience.
    "allow_preexisting_failures": False,
    "auto_continue": False,
    "scheduling_enabled": False,
    "base_branch": "main",
    "models": dict(agents.DEFAULT_MODELS),
}


MANAGER_VERDICT_FILE = "manager-verdict.json"


class Halt(RuntimeError):
    """Stop the run and report. Never a retry, never a workaround."""


def config() -> dict:
    path = state.ensure_home() / "config.json"
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, sort_keys=True))
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # Fails closed on purpose: a config we cannot read is not a
        # config we may guess at, because every cap lives in it.
        raise Halt(f"cannot read {path}: {exc}") from exc
    return {**DEFAULT_CONFIG, **data}


def escalate(task_id: str, title: str, body: str) -> Path:
    """Write a Cowork-ready block. AutoDev never asks the operator directly."""
    root = state.ensure_home() / "escalations"
    path = root / f"{datetime.now():%Y%m%dT%H%M%S}-{task_id}.md"
    path.write_text(
        f"▶ AUTODEV ESCALATION · {task_id}\n\n{title}\n\n{body}\n\n"
        "This is for Cowork to carry to the operator. AutoDev has stopped "
        "and written nothing further.\n"
    )
    state.audit("escalated", task=task_id, path=str(path), title=title)
    return path


def digest(task_id: str, line: str) -> None:
    path = state.ensure_home() / "digest" / f"{datetime.now():%Y-%m-%d}.md"
    with path.open("a") as handle:
        handle.write(f"- {datetime.now():%H:%M} {task_id}: {line}\n")


def _git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd or state.REPO_ROOT),
        capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def isolation_preflight(worktree: Path) -> list[str]:
    """Prove the three protections hold BEFORE a role is ever started.

    Requirement 15 of the approval: if authentication, sandbox isolation
    or credential protection cannot be verified, stop and report rather
    than running. So this returns the list of things that could not be
    verified, and an empty list is the only way past it.
    """
    failures = []

    env = agents.child_env()
    leaked = [key for key in gates.SCRUBBED_ENV if key in env]
    if leaked:
        failures.append(f"credentials still present in the child env: {leaked}")
    suspicious = [
        key for key in env
        if any(mark in key.upper() for mark in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
    ]
    if suspicious:
        failures.append(f"credential-shaped variables still present: {suspicious}")

    guard = Path(__file__).resolve().parent / "guard.py"
    if not guard.exists():
        failures.append("the PreToolUse guard hook is missing")
    else:
        probe = json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
        })
        proc = subprocess.run(
            [sys.executable, str(guard)], input=probe,
            capture_output=True, text=True,
        )
        if proc.returncode != 2:
            failures.append(
                f"the guard hook did not block `git push` (exit {proc.returncode})"
            )

    if not worktree.exists():
        failures.append(f"worktree {worktree} was not created")
    else:
        code, out = _git(["rev-parse", "--show-toplevel"], cwd=worktree)
        if code != 0 or Path(out.strip()) != worktree.resolve():
            failures.append(f"worktree isolation unverified: {out.strip()[:200]}")
        code, out = _git(["symbolic-ref", "--short", "HEAD"], cwd=worktree)
        if code != 0 or out.strip() in ("main", ""):
            failures.append(f"worktree is not on its own branch: {out.strip()}")

    return failures


def prepare_worktree(task_id: str, base_branch: str) -> tuple[Path, str, str]:
    """A worktree on its own branch. Never the operator's checkout."""
    state.ensure_home()
    worktree = state.worktree_root() / task_id
    branch = f"autodev/{task_id}"
    if worktree.exists():
        code, out = _git(["rev-parse", "HEAD"], cwd=worktree)
        if code == 0:
            return worktree, branch, out.strip()
        raise Halt(f"{worktree} exists but is not a usable worktree: {out[:200]}")
    code, out = _git(["worktree", "add", "-b", branch, str(worktree), base_branch])
    if code != 0:
        raise Halt(f"cannot create the worktree: {out[:400]}")
    code, base_sha = _git(["rev-parse", base_branch])
    return worktree, branch, base_sha.strip()


def ticket_digest(ticket: policy.Ticket) -> str:
    """A fingerprint of the approved ticket text.

    Stored beside a manager verdict so that verdict can never be reused
    for a ticket someone has edited since. An edited ticket is a new
    ticket and gets its own validation.
    """
    canonical = json.dumps(ticket.data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _verdict_reuse_problems(stored: dict, ticket: policy.Ticket,
                            source: Path) -> list[str]:
    """Why this stored verdict may NOT stand in for a fresh validation.

    Every check is a reason to run the manager again rather than a reason
    to fail, and all of them are reported at once -- same stance as
    cap_breaches in the price correction.
    """
    problems = []
    if stored.get("verdict") != "ready":
        problems.append(f"verdict is {stored.get('verdict')!r}, not 'ready'")
    stored_task = stored.get("task")
    if stored_task is None:
        # A verdict written before this field existed is bound to its
        # task by the directory run_dir() put it in, which is the same
        # binding. Checked explicitly rather than assumed.
        if source.parent.parent.name != ticket.id:
            problems.append(
                f"verdict lives under task {source.parent.parent.name!r}"
            )
    elif stored_task != ticket.id:
        problems.append(f"verdict was written for task {stored_task!r}")
    stored_digest = stored.get("ticket_digest")
    if stored_digest and stored_digest != ticket_digest(ticket):
        problems.append("the ticket has been edited since it was validated")
    permitted = {policy._repo_relative(path)
                 for path in ticket.files + ticket.tests}
    outside = sorted(
        policy._repo_relative(path)
        for path in (stored.get("expected_files") or [])
        if policy._repo_relative(path) not in permitted
    )
    if outside:
        problems.append(f"expected files outside the ticket's scope: {outside}")
    if not (stored.get("acceptance_criteria") or []):
        problems.append("no acceptance criteria to carry forward")
    return problems


def reusable_manager_verdict(ticket: policy.Ticket, attempt: int):
    """The newest EARLIER attempt's `ready` verdict for this same task.

    ★ WHY A REWORK MUST NOT RE-VALIDATE. Measured on pilot #2,
    2026-10-31: attempt 1 returned `ready`, the developer's work was left
    uncommitted in the worktree, and attempt 2's manager -- doing its job
    properly -- read that dirty worktree, found the fix already present
    and returned `not_as_described`. A correctly built ticket became
    unbuildable, and no rework could ever get past phase one. The manager
    validates a TICKET against the code it was written for; once it has
    said `ready`, a rework exists to fix what the gates or the reviewer
    rejected, not to ask the same question of a tree that has since been
    worked in.

    Returns (verdict, reference) or (None, None). A new task has no
    earlier attempt under its own runs/ directory, so it always validates
    fresh -- that is requirement 6 of the 2026-10-31 approval, and it
    falls out of the structure rather than needing a flag.
    """
    runs = state.home() / "runs" / ticket.id
    candidates = []
    for path in runs.glob("*/" + MANAGER_VERDICT_FILE):
        try:
            number = int(path.parent.name)
        except ValueError:
            continue
        if number < attempt:
            candidates.append((number, path))
    for number, path in sorted(candidates, reverse=True):
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            # Logged, never skipped in silence: an unreadable verdict is
            # exactly what an audit log exists to make visible.
            logger.warning("autodev: unreadable manager verdict %s: %s",
                           path, exc)
            state.audit("manager_verdict_unreadable", task=ticket.id,
                        source=str(path), detail=str(exc)[:200])
            continue
        problems = _verdict_reuse_problems(stored, ticket, path)
        if problems:
            state.audit("manager_verdict_not_reusable", task=ticket.id,
                        attempt=attempt, source=str(path),
                        source_attempt=number, problems=problems)
            continue
        return stored, {
            "path": str(path), "attempt": number,
            "digest_verified": bool(stored.get("ticket_digest")),
        }
    return None, None


def worktree_dirt(worktree: Path) -> str:
    code, out = _git(["status", "--porcelain"], cwd=worktree)
    return out.strip() if code == 0 else ""


def _grant_path(task_id: str, attempt: int) -> Path:
    return state.ensure_home() / "grants" / f"{task_id}-attempt-{attempt}.json"


def consume_attempt_grant(task_id: str, attempt: int) -> dict | None:
    """A one-time, one-attempt exception to the retry limit.

    ★ NOT A HIGHER max_attempts. The operator's approval of 2026-10-31
    authorises exactly one further attempt for one named task and says
    the global setting must not move, so the exception is a file that
    names the task AND the attempt number: it cannot authorise a fourth
    attempt, and it cannot be found by any other task. It is consumed
    BEFORE the attempt runs -- renamed to a .used.json record and audited
    -- so a crash spends it rather than leaving a second use lying
    around. One attempt means one attempt.
    """
    path = _grant_path(task_id, attempt)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("autodev: unreadable attempt grant %s: %s", path, exc)
        state.audit("attempt_grant_unreadable", task=task_id, attempt=attempt,
                    path=str(path), detail=str(exc)[:200])
        return None
    problems = []
    if data.get("task") != task_id:
        problems.append(f"grant names task {data.get('task')!r}")
    if int(data.get("attempt") or 0) != attempt:
        problems.append(f"grant is for attempt {data.get('attempt')!r}")
    if data.get("consumed_at"):
        problems.append(f"already used at {data.get('consumed_at')}")
    if problems:
        state.audit("attempt_grant_refused", task=task_id, attempt=attempt,
                    path=str(path), problems=problems)
        return None
    record = path.with_suffix(".used.json")
    data["consumed_at"] = datetime.now().isoformat()
    record.write_text(json.dumps(data, indent=2, sort_keys=True))
    path.unlink()
    state.audit("attempt_exception_used", task=task_id, attempt=attempt,
                reason=data.get("reason"), granted_by=data.get("granted_by"),
                granted_at=data.get("granted_at"), record=str(record),
                max_attempts_unchanged=True)
    return data


def _phase(name: str, task_id: str, attempt: int, st: state.State, fn):
    """Run one phase between an audited start row and an audited end row."""
    if state.stopped():
        raise Halt("STOP sentinel present")
    if state.paused():
        raise Halt("PAUSE sentinel present")
    st.data.update({
        "active_task": task_id, "phase": name, "attempt": attempt,
        "phase_started_at": datetime.now().isoformat(),
    })
    st.save()
    state.audit("phase_started", task=task_id, phase=name, attempt=attempt)
    try:
        result = fn()
    except (agents.AuthUnavailable, agents.RateLimited) as exc:
        state.audit("phase_halted", task=task_id, phase=name, attempt=attempt,
                    error=type(exc).__name__, detail=str(exc)[:500])
        st.data["phase_started_at"] = None
        st.data["halted_reason"] = f"{type(exc).__name__}: {exc}"
        st.save()
        raise Halt(str(exc)) from exc
    st.data["phase_started_at"] = None
    st.save()
    state.audit("phase_finished", task=task_id, phase=name, attempt=attempt)
    return result


def run_task(task_id: str, *, stub_ok: bool = False) -> int:
    cfg = config()
    st = state.State.load()
    st.roll_day()

    if state.stopped():
        print("STOP is set. `coordinator.py resume --clear-stop` to clear it.")
        return 2
    if state.paused():
        print("PAUSE is set. `coordinator.py resume` to clear it.")
        return 2
    if st.data["tasks_today"] >= cfg["max_tasks_per_day"]:
        print(f"daily cap reached: {st.data['tasks_today']} of "
              f"{cfg['max_tasks_per_day']} tasks today")
        return 2

    ticket = policy.load(state.home() / "queue" / f"{task_id}.json")
    if not ticket.is_eligible:
        escalate(task_id, "Ticket refused by the eligibility policy",
                 "\n".join(f"- {reason}" for reason in ticket.refusals))
        print("REFUSED by policy:")
        for reason in ticket.refusals:
            print(f"  - {reason}")
        return 1

    interrupted = st.interrupted_phase
    previous_task = st.data.get("active_task")
    attempt = int(st.data.get("attempt") or 0)
    if interrupted:
        if previous_task != task_id:
            print(f"another task is mid-flight: {st.data.get('active_task')} "
                  f"at phase {interrupted}. Finish or park it first.")
            return 2
        state.audit("crash_detected", task=task_id, phase=interrupted,
                    attempt=attempt)
        attempt += 1
        print(f"recovering: {interrupted} was interrupted; attempt {attempt}")
    elif previous_task == task_id and attempt:
        # ★ A REWORK IS A NEW ATTEMPT, not a re-run of the old one. A
        # failed gate or a rejected review leaves the task active with no
        # phase in flight; without this branch the next run reused the
        # same attempt number, OVERWROTE that attempt's evidence
        # directory and never reached max_attempts -- so the retry limit
        # the operator approved would not have held. Found by reading
        # this function back, not by a failure.
        attempt += 1
        state.audit("rework_started", task=task_id, attempt=attempt,
                    after=st.data.get("last_outcome"))
        print(f"reworking after {st.data.get('last_outcome')}; attempt {attempt}")
    elif previous_task not in (None, task_id):
        # ★ ANOTHER TASK'S ATTEMPT COUNT IS NOT THIS TASK'S. The counter
        # used to carry over, so a task following one that had ended at
        # attempt 2 started at 2 -- one attempt from the cap, with its
        # evidence written into a directory numbered after work it had
        # nothing to do with. A crash resume for the SAME task still
        # increments (above): that is the one case where the previous
        # number is this task's own.
        state.audit("attempt_counter_reset", task=task_id,
                    previous_task=previous_task, previous_attempt=attempt)
        attempt = 0
    attempt = max(attempt, 1)
    if attempt > cfg["max_attempts"]:
        grant = consume_attempt_grant(task_id, attempt)
        if grant is None:
            escalate(task_id, "Retry limit reached",
                     f"{attempt - 1} attempts already made; the limit is "
                     f"{cfg['max_attempts']}. Parked for a human.")
            print("retry limit reached; parked")
            return 1
        print(f"attempt {attempt} runs under a one-time exception "
              f"granted by {grant.get('granted_by')}: {grant.get('reason')}")

    ok, detail = agents.preflight_auth()
    state.audit("auth_preflight", task=task_id, ok=ok, detail=detail)
    if not ok and not stub_ok:
        escalate(task_id, "BLOCKER: the Claude Code CLI cannot authenticate", detail)
        print(f"BLOCKED: {detail}")
        return 3

    worktree, branch, base_sha = prepare_worktree(task_id, cfg["base_branch"])
    failures = isolation_preflight(worktree)
    state.audit("isolation_preflight", task=task_id, failures=failures)
    if failures:
        escalate(task_id, "BLOCKER: isolation or credential protection unverified",
                 "\n".join(f"- {item}" for item in failures))
        print("BLOCKED: isolation preflight failed:")
        for item in failures:
            print(f"  - {item}")
        return 3

    evidence = state.run_dir(task_id, attempt)
    st.data["active_task"] = task_id
    st.save()

    brief = json.dumps({
        "ticket": ticket.data, "worktree": str(worktree),
        "branch": branch, "base": base_sha,
    }, indent=2)

    stored_verdict, reuse = (
        reusable_manager_verdict(ticket, attempt) if attempt > 1 else (None, None)
    )
    if stored_verdict is not None:
        state.audit("manager_reused", task=task_id, attempt=attempt,
                    source_attempt=reuse["attempt"], source=reuse["path"],
                    digest_verified=reuse["digest_verified"],
                    reason="rework of a task that already has a ready verdict")
        (evidence / MANAGER_VERDICT_FILE).write_text(json.dumps(
            {**stored_verdict, "reused_from": reuse}, indent=2,
        ))
        print(f"manager verdict reused from attempt {reuse['attempt']} "
              f"({reuse['path']}); not re-validated against a worked-in tree")
        manager_verdict = stored_verdict
    else:
        dirt = worktree_dirt(worktree)
        if attempt > 1 and dirt:
            # ★ NEVER VALIDATE A TICKET AGAINST ITS OWN LEFTOVERS. With no
            # reusable `ready` verdict there is nothing to carry forward,
            # and running the manager here asks it whether a fix is needed
            # while the fix sits uncommitted in front of it. The honest
            # answer is `not_as_described` and the honest move is to stop.
            escalate(task_id, "A rework has prior work in the worktree and no "
                              "reusable manager verdict",
                     f"attempt {attempt}; uncommitted in {worktree}:\n{dirt}\n\n"
                     "No earlier attempt returned a `ready` verdict that this "
                     "ticket's scope still matches, so there is nothing to "
                     "carry forward -- and validating the ticket against a "
                     "tree that already contains its work would read as "
                     "'already done'. A human decides whether that work is "
                     "this ticket's and should be reviewed, or is a stray "
                     "edit to set aside.")
            st.data["last_outcome"] = "manager:blocked-by-prior-work"
            st.save()
            print("a rework with prior work and no reusable verdict; parked")
            return 1
        manager = _phase("manager", task_id, attempt, st, lambda: agents.run_role(
            "manager",
            "Assess this CardFoundry ticket and return your JSON verdict.\n\n" + brief,
            worktree, evidence, model=cfg["models"]["manager"],
        ))
        manager_verdict = manager.structured or {}
        verdict = manager_verdict.get("verdict")
        (evidence / MANAGER_VERDICT_FILE).write_text(json.dumps(
            {**(manager.structured or {"unparsed": manager.result_text}),
             "task": task_id, "ticket_digest": ticket_digest(ticket)},
            indent=2,
        ))
        if verdict != "ready":
            escalate(task_id, f"Manager verdict: {verdict or 'unreadable'}",
                     json.dumps(manager.structured
                                or {"result": manager.result_text}, indent=2))
            st.data["last_outcome"] = f"manager:{verdict}"
            st.save()
            print(f"task parked by the manager: {verdict}")
            return 1

    criteria = manager_verdict.get("acceptance_criteria") or []
    # ★ PRIOR WORK IS TOLD, NOT DISCOVERED. A resumed attempt starts in a
    # worktree that already holds what the interrupted one wrote, and a
    # developer that meets that by surprise either reports "already done"
    # and commits nothing (the whole attempt wasted) or quietly reverts
    # it. Either way the operator loses work that was correct. So it is
    # in the brief, with what to do about it.
    prior_work = worktree_dirt(worktree) if attempt > 1 else ""
    dev_brief = json.dumps({
        "ticket": ticket.data, "acceptance_criteria": criteria,
        "notes_for_developer": manager_verdict.get("notes_for_developer"),
        "branch": branch, "base": base_sha,
        "uncommitted_work_from_an_earlier_attempt": prior_work or None,
    }, indent=2)
    prior_note = (
        "\n\nThis worktree ALREADY CONTAINS uncommitted work from an earlier "
        "attempt at this same ticket (see "
        "uncommitted_work_from_an_earlier_attempt). Read it first and judge "
        "it against the acceptance criteria. Where it is right, keep it and "
        "commit it -- `git add` the ticket's own files by name, never -A. "
        "Where it is wrong, correct it and say what was wrong. Do not "
        "discard it silently, and do not report the ticket as already done "
        "while the work is still uncommitted: an uncommitted change is not "
        "a change the coordinator can see."
    ) if prior_work else ""
    developer = _phase("developer", task_id, attempt, st, lambda: agents.run_role(
        "developer",
        "Implement this ticket in this worktree, to these acceptance "
        "criteria. Commit as you go." + prior_note + "\n\n" + dev_brief,
        worktree, evidence, model=cfg["models"]["developer"],
        # ★ WITHOUT THIS THE DEVELOPER CANNOT RUN ITS OWN TESTS. The guard
        # scopes a pytest run to the ticket's test paths, read from
        # CARDFOUNDRY_AUTODEV_TICKET_TESTS -- and with that unset the
        # scope is empty, so EVERY pytest run is refused with "no ticket
        # test paths are in scope". Pilot #3 hit exactly that: the
        # developer committed and reported, honestly, that the
        # coordinator's gates were the only test evidence for its change.
        # The guard's own tests set the variable by hand, so nothing
        # caught that the coordinator never did.
        ticket_tests=ticket.tests,
    ))
    (evidence / "developer-summary.txt").write_text(developer.result_text)

    code, diff = _git(["diff", f"{base_sha}...HEAD"], cwd=worktree)
    (evidence / "diff.patch").write_text(diff)
    if code != 0 or not diff.strip():
        escalate(task_id, "The developer produced no diff",
                 developer.result_text[:2000] or "no summary returned")
        st.data["last_outcome"] = "developer:no-diff"
        st.save()
        print("no diff produced; parked")
        return 1

    verdict_gates = _phase("gate", task_id, attempt, st, lambda: gates.run_all(
        worktree, base_sha, ticket, evidence,
        allow_preexisting_failures=cfg["allow_preexisting_failures"],
    ))
    (evidence / "gates.json").write_text(json.dumps(verdict_gates.as_dict(), indent=2))
    state.audit("gates", task=task_id, attempt=attempt, ok=verdict_gates.ok,
                summary=verdict_gates.summary(), skipped=verdict_gates.skips)

    review_brief = json.dumps({
        "ticket": ticket.data, "acceptance_criteria": criteria,
        "gate_summary": verdict_gates.summary(),
        "gate_detail": verdict_gates.as_dict(),
        "diff_file": str(evidence / "diff.patch"),
        "evidence_dir": str(evidence),
    }, indent=2)
    reviewer = _phase("reviewer", task_id, attempt, st, lambda: agents.run_role(
        "reviewer",
        "Review this change independently and return your JSON verdict. "
        "Read the diff at the path given; you have not seen the "
        "developer's reasoning and should not ask for it.\n\n" + review_brief,
        worktree, evidence, model=cfg["models"]["reviewer"],
    ))
    (evidence / "reviewer-verdict.json").write_text(
        json.dumps(reviewer.structured or {"unparsed": reviewer.result_text}, indent=2)
    )
    review_verdict = (reviewer.structured or {}).get("verdict")

    code, head = _git(["rev-parse", "HEAD"], cwd=worktree)
    head_sha = head.strip()
    passed = verdict_gates.ok and review_verdict == "approve"
    state.audit("task_outcome", task=task_id, attempt=attempt, passed=passed,
                gates=verdict_gates.summary(), review=review_verdict,
                branch=branch, head=head_sha)
    st.data.update({
        "phase": None, "attempt": attempt,
        "last_outcome": f"gates={verdict_gates.ok} review={review_verdict}",
        "active_task": None if passed else task_id,
    })
    if passed:
        st.data["tasks_today"] = int(st.data.get("tasks_today") or 0) + 1
    st.save()

    if passed:
        digest(task_id, f"done on {branch} ({head_sha[:10]}), "
                        f"gates {verdict_gates.summary()}, reviewer approved")
        print(f"DONE. branch {branch} at {head_sha} -- left for human review. "
              f"Nothing was pushed.")
        return 0

    escalate(task_id, "Gates or review did not pass",
             f"gates: {verdict_gates.summary()}\n"
             f"review: {review_verdict}\n"
             f"evidence: {evidence}\n")
    digest(task_id, f"NOT done: gates {verdict_gates.summary()}, review {review_verdict}")
    print(f"NOT PASSED. gates={verdict_gates.summary()} review={review_verdict}")
    print(f"evidence: {evidence}")
    return 1


def cmd_status(_args) -> int:
    cfg = config()
    st = state.State.load()
    st.roll_day()
    print(json.dumps({
        "home": str(state.home()),
        "paused": state.paused(),
        "stopped": state.stopped(),
        "auto_continue": cfg["auto_continue"],
        "scheduling_enabled": cfg["scheduling_enabled"],
        "max_tasks_per_day": cfg["max_tasks_per_day"],
        "tasks_today": st.data.get("tasks_today"),
        "active_task": st.data.get("active_task"),
        "phase": st.data.get("phase"),
        "attempt": st.data.get("attempt"),
        "interrupted_phase": st.interrupted_phase,
        "halted_reason": st.data.get("halted_reason"),
        "last_outcome": st.data.get("last_outcome"),
        "queue": sorted(p.stem for p in (state.home() / "queue").glob("*.json")),
    }, indent=2))
    for row in state.read_audit(limit=8):
        print(f"  {row.get('at', '?')} {row.get('event')} "
              f"{row.get('task') or ''} {row.get('phase') or ''}")
    return 0


def cmd_pause(_args) -> int:
    (state.ensure_home() / state.PAUSE_FILE).write_text(
        f"paused at {datetime.now().isoformat()}\n"
    )
    state.audit("paused")
    print("paused: no phase will start until you resume")
    return 0


def cmd_resume(args) -> int:
    for name in (state.PAUSE_FILE,) + ((state.STOP_FILE,) if args.clear_stop else ()):
        path = state.home() / name
        if path.exists():
            path.unlink()
            print(f"cleared {name}")
    state.audit("resumed", clear_stop=bool(args.clear_stop))
    return 0


def cmd_stop(_args) -> int:
    (state.ensure_home() / state.STOP_FILE).write_text(
        f"stopped at {datetime.now().isoformat()}\n"
    )
    state.audit("stopped")
    print("STOP set: the current phase finishes, nothing else starts")
    return 0


def cmd_preflight(_args) -> int:
    ok, detail = agents.preflight_auth()
    print(f"auth: {'OK' if ok else 'BLOCKED'} -- {detail}")
    env = agents.child_env()
    leaked = [key for key in gates.SCRUBBED_ENV if key in env]
    print(f"credential scrub: {'OK' if not leaked else 'LEAKED ' + str(leaked)}")
    guard = Path(__file__).resolve().parent / "guard.py"
    proc = subprocess.run(
        [sys.executable, str(guard)],
        input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": "git push origin main"}}),
        capture_output=True, text=True,
    )
    print(f"guard hook blocks `git push`: "
          f"{'OK' if proc.returncode == 2 else 'FAILED exit ' + str(proc.returncode)}")
    return 0 if ok and not leaked and proc.returncode == 2 else 3


def cmd_grant(args) -> int:
    """Record a one-time exception to the retry limit, for ONE attempt.

    Deliberately a separate command a human runs, not something a phase
    can do for itself: the developer has no path to it (its allowlist
    permits `python -m pytest` and `python -m compileall` only), and the
    grant is auditable before it is used and again when it is.
    """
    path = _grant_path(args.task, args.attempt)
    used = path.with_suffix(".used.json")
    if path.exists() or used.exists():
        print(f"a grant for {args.task} attempt {args.attempt} already exists: "
              f"{path if path.exists() else used}")
        return 2
    cfg = config()
    if args.attempt <= cfg["max_attempts"]:
        print(f"attempt {args.attempt} is within max_attempts "
              f"({cfg['max_attempts']}); no exception is needed")
        return 2
    payload = {
        "task": args.task, "attempt": args.attempt, "reason": args.reason,
        "granted_by": args.granted_by,
        "granted_at": datetime.now().isoformat(),
        "max_attempts_at_grant": cfg["max_attempts"],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    state.audit("attempt_exception_granted", path=str(path), **payload)
    print(f"granted: {args.task} may make attempt {args.attempt} once. "
          f"max_attempts stays at {cfg['max_attempts']}.")
    return 0


def cmd_run(args) -> int:
    try:
        with state.single_owner_lock():
            return run_task(args.task, stub_ok=args.stub_ok)
    except state.CoordinatorBusy as exc:
        print(str(exc))
        return 2
    except Halt as exc:
        state.audit("halted", task=args.task, detail=str(exc)[:500])
        print(f"HALTED: {exc}")
        return 3


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run ONE task through every phase")
    run.add_argument("--task", required=True)
    run.add_argument(
        "--stub-ok", action="store_true",
        help="skip the auth blocker (tests with a stub CLI only; never real runs)",
    )
    run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="what the coordinator thinks").set_defaults(
        func=cmd_status)
    sub.add_parser("pause", help="stop starting new phases").set_defaults(
        func=cmd_pause)
    resume = sub.add_parser("resume", help="clear PAUSE (and STOP with --clear-stop)")
    resume.add_argument("--clear-stop", action="store_true")
    resume.set_defaults(func=cmd_resume)
    sub.add_parser("stop", help="hard stop after the current phase").set_defaults(
        func=cmd_stop)
    sub.add_parser("preflight", help="auth, credential scrub, guard hook").set_defaults(
        func=cmd_preflight)

    grant = sub.add_parser(
        "grant", help="allow ONE attempt past the retry limit (operator only)")
    grant.add_argument("--task", required=True)
    grant.add_argument("--attempt", required=True, type=int)
    grant.add_argument("--reason", required=True)
    grant.add_argument("--granted-by", required=True, dest="granted_by")
    grant.set_defaults(func=cmd_grant)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
