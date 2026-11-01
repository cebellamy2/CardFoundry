"""The launchd agent's entry point, and the read-only validate command.

★ WHAT TURNING THIS ON ACTUALLY MEANS (operator, 2026-11-01: "sets it to
start running automatically"). A machine now starts AutoDev without
anyone asking, on no daily cap, minutes after Cowork writes a ticket and
an approval note. So the tests that matter most here are the ones that
prove it does NOTHING in every case where it should: the flag off, STOP,
PAUSE, an empty queue, an unapproved ticket, another owner holding the
lock. The one test that proves it runs is the easy one.

★ STUB TASKS ONLY. Nothing here invokes a real `claude -p`, touches the
real AutoDev home or reaches the network -- the shared harness redirects
CARDFOUNDRY_AUTODEV_HOME and swaps in the stub CLI.
"""
import json

import pytest

from autodev_harness import PILOT_TICKET, world  # noqa: F401
from scripts.autodev import (autoserve, completions, coordinator, outbox,
                             serve, state, validate_ticket)
from tests.test_autodev_serve import GOOD_NOTE, approve, record_done


def enable(world, **overrides):
    """config.json as the operator's own Mac has it."""
    path = world["home"] / "config.json"
    path.write_text(json.dumps(
        {**coordinator.config(), "scheduling_enabled": True, **overrides},
        indent=2, sort_keys=True))


def run(**kwargs):
    return autoserve.main(["--stub-ok", *kwargs.pop("argv", [])], **kwargs)


def log_text(world):
    path = world["home"] / autoserve.LOG_NAME
    return path.read_text() if path.exists() else ""


def files_under(world):
    """Every file under the AutoDev home, with its size and mtime."""
    return {
        str(path.relative_to(world["home"])): (path.stat().st_size,
                                               path.stat().st_mtime_ns)
        for path in sorted(world["home"].rglob("*")) if path.is_file()
    }


# --- it does nothing, in every case where it should ----------------------

def test_it_does_nothing_while_scheduling_is_disabled(world):
    """★ THE SWITCH THAT SURVIVES A REBOOT. PAUSE and STOP are files an
    operator can delete by accident; this one is the config."""
    approve(world)

    assert run() == 0
    assert "scheduling_enabled is false" in log_text(world)
    assert not (world["worktrees"] / "pilot-comment").exists()


def test_a_fresh_clone_serves_nothing(world):
    """DEFAULT_CONFIG fails closed, so an agent installed somewhere it
    was not wanted still runs no task."""
    assert coordinator.DEFAULT_CONFIG["scheduling_enabled"] is False


def test_stop_stops_it(world):
    enable(world)
    approve(world)
    (world["home"] / state.STOP_FILE).write_text("")

    assert run() == 0
    assert "STOP is set" in log_text(world)
    assert not (world["worktrees"] / "pilot-comment").exists()


def test_pause_stops_it(world):
    enable(world)
    approve(world)
    (world["home"] / state.PAUSE_FILE).write_text("")

    assert run() == 0
    assert "PAUSE is set" in log_text(world)
    assert not (world["worktrees"] / "pilot-comment").exists()


def test_an_empty_queue_idles(world):
    enable(world)
    (world["home"] / "queue" / "pilot-comment.json").unlink()

    assert run() == 0
    assert "idle: nothing approved to run (0 ticket(s) refused)" in log_text(world)


def test_an_unapproved_ticket_is_refused_and_named(world):
    """The ticket is there, the approval note is not."""
    enable(world)

    assert run() == 0
    text = log_text(world)
    assert "idle: nothing approved to run (1 ticket(s) refused)" in text
    assert "refused pilot-comment: no approval note" in text
    assert not (world["worktrees"] / "pilot-comment").exists()


def test_an_already_done_ticket_is_refused(world):
    """The completion gate is as strict under the agent as by hand."""
    enable(world)
    approve(world, approved_at="2020-01-01T09:00:00")
    record_done(world)

    assert run() == 0
    assert "already done at" in log_text(world)


def test_another_owner_holding_the_lock_is_not_an_error(world):
    """One task at a time is the point of the lock, and under a job that
    fires every five minutes it is also what bounds the whole thing."""
    enable(world)
    approve(world)

    with state.single_owner_lock():
        code = run()

    assert code == 0
    assert "another owner holds the lock" in log_text(world)


def test_an_unreadable_config_refuses_rather_than_guessing(world):
    (world["home"] / "config.json").write_text("{not json")

    assert run() == 3
    assert "refusing:" in log_text(world)


# --- and it does run an approved one ------------------------------------

def test_an_approved_stub_task_is_picked_up_and_left_on_its_branch(world):
    enable(world)
    approve(world)

    assert run() == 0
    text = log_text(world)
    assert "starting: pilot-comment" in text
    assert '"done": ["pilot-comment"]' in text
    assert (world["worktrees"] / "pilot-comment").exists()
    entry = [e for e in outbox.recent() if e["outcome"] == "done"][0]
    assert entry["pushed"] is False and entry["deployed"] is False


def test_one_firing_starts_at_most_one_task(world):
    """A bounded cycle, so the interval -- not a counter -- is what paces
    it. Two approved tickets, one firing, one task."""
    enable(world)
    for task_id in ("pilot-comment", "pilot-two"):
        path = world["home"] / "queue" / f"{task_id}.json"
        path.write_text(json.dumps({**PILOT_TICKET, "id": task_id}))
        approve(world, task=task_id)

    assert run() == 0

    started = [row for row in state.read_audit()
               if row.get("event") == "serve_task_started"]
    assert len(started) == 1, [row.get("task") for row in started]


def test_the_log_carries_no_environment_and_no_secret(world):
    enable(world)
    approve(world)
    run()

    text = log_text(world)
    for mark in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "sk-"):
        assert mark not in text, mark


def test_a_refusal_is_not_rewritten_on_every_firing(world):
    """★ THE ARITHMETIC THE AGENT CHANGED. serve used to dedupe refusals
    in memory, which was enough for one long-lived process. A fresh
    process every five minutes would have written five outbox entries
    and five audit rows per interval -- about a thousand files a day, all
    saying what the first one said."""
    enable(world)

    for _ in range(4):
        run()

    refused = [e for e in outbox.recent(limit=99) if e["outcome"] == "refused"]
    assert len(refused) == 1, f"wrote {len(refused)} entries for one refusal"
    rows = [r for r in state.read_audit()
            if r.get("event") == "serve_task_refused"]
    assert len(rows) == 1, f"wrote {len(rows)} audit rows for one refusal"


def test_a_new_reason_is_reported_again(world):
    """Deduped on the reasons, not just the task: a ticket refused for
    something new is news."""
    enable(world)
    run()
    assert len([e for e in outbox.recent(limit=99)
                if e["outcome"] == "refused"]) == 1

    path = world["home"] / "queue" / "pilot-comment.json"
    data = json.loads(path.read_text())
    data["files"] = ["models.py"]          # a different refusal
    path.write_text(json.dumps(data))
    run()

    refused = [e for e in outbox.recent(limit=99) if e["outcome"] == "refused"]
    assert len(refused) == 2, "a new reason was swallowed"


# --- a rate limit still halts the day -----------------------------------

def test_a_rate_limit_halts_and_does_not_retry(world, monkeypatch):
    """★ THE CAP THAT MATTERS, AND IT STAYS. The daily limit is gone; a
    usage limit still stops everything, with no retry and no fallback to
    API billing -- there is nothing to fall back to, because child_env
    scrubs ANTHROPIC_API_KEY."""
    enable(world)
    approve(world)
    monkeypatch.setenv("STUB_RATE_LIMIT", "1")

    assert run() == 3
    assert any(row.get("event") == "serve_halted" for row in state.read_audit())
    escalations = list((world["home"] / "escalations").glob("*.md"))
    assert escalations, "the operator was not told"
    from scripts.autodev import agents, gates
    assert "ANTHROPIC_API_KEY" in gates.SCRUBBED_ENV
    assert "ANTHROPIC_API_KEY" not in agents.child_env(role="developer")


# --- the read-only command Cowork runs before writing the note ----------

def test_validate_ticket_writes_absolutely_nothing(world):
    """★ PROVED BY SNAPSHOT, not by reading the code. Every file under the
    AutoDev home, with its size and mtime, before and after."""
    approve(world)
    record_done(world, task="some-other-task")
    before = files_under(world)

    assert validate_ticket.main(["pilot-comment"]) == 0
    assert validate_ticket.main(["--all"]) == 0
    assert validate_ticket.main(["--json"]) == 0

    assert files_under(world) == before


def test_validate_ticket_creates_no_config_file(world):
    """coordinator.config() WRITES config.json when it is missing, so the
    command must not reach for it -- a read-only tool that creates a file
    on first use is not read-only."""
    path = world["home"] / "config.json"
    if path.exists():
        path.unlink()

    validate_ticket.main(["pilot-comment"])

    assert not path.exists()


def test_validate_ticket_digest_is_the_one_the_gate_checks(world):
    """Two implementations of the digest would be two digests."""
    ticket = coordinator.policy.load(
        world["home"] / "queue" / "pilot-comment.json")

    assert (validate_ticket.inspect("pilot-comment")["ticket_digest"]
            == coordinator.ticket_digest(ticket))


def test_validate_ticket_answers_the_question_cowork_asks(world, capsys):
    approve(world)
    validate_ticket.main(["pilot-comment"])
    out = capsys.readouterr().out

    assert "policy_eligible:   True" in out
    assert "RUNNABLE:          True" in out
    assert "ticket_digest:" in out
    assert "Write the approval note with this exact ticket_digest" in out


def test_validate_ticket_reports_a_missing_file_rather_than_crashing(world):
    row = validate_ticket.inspect("no-such-task")

    assert row["queue_file_exists"] is False
    assert row["runnable"] is False
    assert row["policy_refusals"], "a missing ticket must say why"


def test_validate_ticket_agrees_with_what_serve_would_do(world):
    """The whole point: if it says runnable, serve runs it."""
    approve(world)
    assert validate_ticket.inspect("pilot-comment")["runnable"] is True
    assert serve.next_runnable(set())[0] == "pilot-comment"

    record_done(world)
    assert validate_ticket.inspect("pilot-comment")["runnable"] is False
    assert serve.next_runnable(set())[0] is None
