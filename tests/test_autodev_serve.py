"""The serve loop and the approval gate, driven for real against the STUB.

★ THE ONE RULE THESE EXIST TO HOLD. The operator's decision of
2026-10-31: no backlog item reaches AutoDev until they and Cowork have
assessed it as AutoDev-worthy with every open question answered, and
their "AutoDev-ready" OK -- written down by Cowork -- IS the approval.
Every ticket already in the queue predates that rule. So the first thing
proved here is that the loop picks up NONE of them, by name.

★ DAMAGE TESTS MATTER MOST. An approval gate that passes a good note is
worthless unless it refuses a bad one, so most of the approvals tests
below feed it deliberately broken notes and assert it refuses. Same
stance as tests/test_changelog_discipline.py.

★ NO REAL AGENT, NO NETWORK, NO REAL QUEUE. Everything runs against the
shared stub harness over a throwaway repository. The real
claude/autodev/ home is never touched: the harness redirects
CARDFOUNDRY_AUTODEV_HOME and state.REPO_ROOT.
"""

import json
from datetime import datetime

import pytest

from autodev_harness import PILOT_TICKET, _git, world  # noqa: F401
from scripts.autodev import agents, approvals, coordinator, outbox, serve, state

# The five tickets sitting in the real queue when the approval gate
# shipped. Named here on purpose: the hard rule for this ticket is that
# serve must not pick any of them up, and a test that only checked "an
# unapproved task is refused" in the abstract would not notice if one of
# these grew an approval it was never given.
PRE_EXISTING_QUEUE_IDS = (
    "candidate-active-allocation-statuses",
    "candidate-orphan-picked-route",
    "condition-label-round-trip",
    "condition-label-round-trip-2",
    "deploy-guard-crons-comment",
)

GOOD_NOTE = {
    "task": "pilot-comment",
    "autodev_ready": True,
    "operator_words": "yes, the crons comment one is AutoDev-ready",
    "approved_at": "2026-11-01T09:12:00",
    "written_by": "cowork",
    "covers_automatic_retry": True,
}


class FakeSleeper:
    """Records what the loop asked to wait for instead of waiting."""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def approve(world, task="pilot-comment", **overrides):
    note = {**GOOD_NOTE, "task": task, **overrides}
    path = approvals.path_for(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(note, indent=2))
    return path


def queue_ticket(world, task_id, **overrides):
    data = {**PILOT_TICKET, "id": task_id, **overrides}
    path = world["home"] / "queue" / f"{task_id}.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def serve_once(cycles=1, **kwargs):
    sleeper = FakeSleeper()
    code, report = serve.serve(max_cycles=cycles, sleeper=sleeper,
                               stub_ok=True, **kwargs)
    return code, report, sleeper


# --- the hard rule: the existing queue is not approved -------------------

def test_the_five_pre_existing_queue_entries_are_every_one_refused(world):
    """★ THE RULE THIS TICKET EXISTS TO ENFORCE. None of them was ever
    given the operator's "AutoDev-ready" OK under the new gate, so none
    of them may run -- including the two that pass the eligibility
    policy and the one that was the original approved pilot."""
    for task_id in PRE_EXISTING_QUEUE_IDS:
        queue_ticket(world, task_id)
    (world["home"] / "queue" / "pilot-comment.json").unlink()

    task_id, refusals = serve.next_runnable(set())

    assert task_id is None, f"serve would have run {task_id}"
    assert set(refusals) == set(PRE_EXISTING_QUEUE_IDS)
    for name in PRE_EXISTING_QUEUE_IDS:
        assert any("no approval note" in reason for reason in refusals[name]), \
            f"{name} was refused, but not for want of an approval: {refusals[name]}"


def test_serve_runs_nothing_when_the_whole_queue_is_unapproved(world):
    for task_id in PRE_EXISTING_QUEUE_IDS:
        queue_ticket(world, task_id)
    (world["home"] / "queue" / "pilot-comment.json").unlink()

    code, report, sleeper = serve_once()

    assert code == 0
    assert report.started == [] and report.done == []
    assert set(report.refused) == set(PRE_EXISTING_QUEUE_IDS)
    assert sleeper.calls, "it should have waited rather than spun"


def test_a_refusal_is_reported_not_hidden(world):
    """CLAUDE.md: a row silently dropped from a report is worse than one
    that cannot be categorised. An unapproved ticket must be visible."""
    code, report, _ = serve_once()

    assert "pilot-comment" in report.refused
    assert any(row.get("event") == "serve_task_refused"
               and row.get("task") == "pilot-comment"
               for row in state.read_audit())
    entries = outbox.recent()
    assert [e for e in entries if e["outcome"] == "refused"], \
        "the refusal must reach the outbox too"


def test_the_same_refusal_is_not_written_to_the_outbox_every_cycle(world):
    """It fails closed forever, so an unbounded loop would otherwise fill
    the outbox with the same entry."""
    serve.serve(max_cycles=4, sleeper=FakeSleeper(), stub_ok=True)
    refused = [e for e in outbox.recent(limit=50) if e["outcome"] == "refused"]
    assert len(refused) == 1, f"wrote {len(refused)} entries for one refusal"


# --- the approval note: damage tests ------------------------------------

def test_a_good_note_authorises_the_task(world):
    approve(world)
    approval = approvals.load("pilot-comment")
    assert approval.is_approved, approval.refusals
    assert approval.as_evidence()["operator_words"] == GOOD_NOTE["operator_words"]


def test_a_missing_note_refuses(world):
    approval = approvals.load("pilot-comment")
    assert not approval.is_approved
    assert any("no approval note" in r for r in approval.refusals)


@pytest.mark.parametrize("field", sorted(approvals.REQUIRED_FIELDS))
def test_every_required_field_is_actually_required(world, field):
    note = {k: v for k, v in GOOD_NOTE.items() if k != field}
    approvals.path_for("pilot-comment").parent.mkdir(parents=True, exist_ok=True)
    approvals.path_for("pilot-comment").write_text(json.dumps(note))
    approval = approvals.load("pilot-comment")
    assert not approval.is_approved, f"a note with no {field} was accepted"
    assert any(field in reason for reason in approval.refusals)


@pytest.mark.parametrize("value", ["yes", "true", 1, "TRUE", None, False])
def test_autodev_ready_must_be_the_boolean_true(world, value):
    """A template with a placeholder must not authorise a run."""
    approve(world, autodev_ready=value)
    approval = approvals.load("pilot-comment")
    assert not approval.is_approved, f"autodev_ready={value!r} was accepted"


@pytest.mark.parametrize("value", ["yes", 1, False, None])
def test_covers_automatic_retry_must_be_the_boolean_true(world, value):
    approve(world, covers_automatic_retry=value)
    assert not approvals.load("pilot-comment").is_approved


def test_only_cowork_may_write_an_approval_note(world):
    approve(world, written_by="autodev")
    approval = approvals.load("pilot-comment")
    assert not approval.is_approved
    assert any("only 'cowork'" in r for r in approval.refusals)


def test_a_note_for_another_task_does_not_authorise_this_one(world):
    """The note names its task, so one OK cannot be copied across the
    queue by renaming a file."""
    path = approvals.path_for("pilot-comment")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**GOOD_NOTE, "task": "some-other-task"}))
    approval = approvals.load("pilot-comment")
    assert not approval.is_approved
    assert any("not 'pilot-comment'" in r for r in approval.refusals)


def test_an_unparseable_timestamp_refuses(world):
    approve(world, approved_at="last tuesday")
    assert not approvals.load("pilot-comment").is_approved


def test_an_unreadable_note_refuses_rather_than_passing(world):
    path = approvals.path_for("pilot-comment")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all")
    approval = approvals.load("pilot-comment")
    assert not approval.is_approved
    assert any("unreadable" in r for r in approval.refusals)


def test_a_note_that_is_not_an_object_refuses(world):
    path = approvals.path_for("pilot-comment")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["approved"]))
    assert not approvals.load("pilot-comment").is_approved


def test_a_digest_that_matches_is_accepted_and_a_stale_one_is_not(world):
    ticket = coordinator.policy.load(world["home"] / "queue" / "pilot-comment.json")
    digest = coordinator.ticket_digest(ticket)

    approve(world, ticket_digest=digest)
    assert approvals.load("pilot-comment", ticket_digest=digest).is_approved

    approval = approvals.load("pilot-comment", ticket_digest="0" * 64)
    assert not approval.is_approved
    assert any("changed since it was approved" in r for r in approval.refusals)


def test_editing_an_approved_ticket_invalidates_a_digest_bearing_note(world):
    """The whole point of the optional digest: the operator approved a
    ticket, not a task name."""
    path = world["home"] / "queue" / "pilot-comment.json"
    ticket = coordinator.policy.load(path)
    approve(world, ticket_digest=coordinator.ticket_digest(ticket))
    assert serve.next_runnable(set())[0] == "pilot-comment"

    data = json.loads(path.read_text())
    data["intent"] = "something the operator never read"
    path.write_text(json.dumps(data))

    task_id, refusals = serve.next_runnable(set())
    assert task_id is None
    assert any("changed since it was approved" in r
               for r in refusals["pilot-comment"])


def test_a_note_without_a_digest_still_works(world):
    """Cowork writes these by hand and may have no shell here, so the
    digest is optional -- deliberately weaker, not refused."""
    approve(world)
    assert serve.next_runnable(set())[0] == "pilot-comment"


def test_the_policy_still_applies_on_top_of_the_approval(world):
    """An approval is not a bypass: an approved ticket that names a
    forbidden path is still refused."""
    approve(world)
    path = world["home"] / "queue" / "pilot-comment.json"
    data = json.loads(path.read_text())
    data["files"] = ["models.py"]
    path.write_text(json.dumps(data))

    task_id, refusals = serve.next_runnable(set())
    assert task_id is None
    assert any("models.py" in r for r in refusals["pilot-comment"])


# --- the loop ------------------------------------------------------------

def test_an_approved_task_runs_and_is_left_on_its_own_branch(world):
    approve(world)
    code, report, _ = serve_once()

    assert code == 0
    assert report.done == ["pilot-comment"], report.as_dict()
    worktree = world["worktrees"] / "pilot-comment"
    branch = _git(["symbolic-ref", "--short", "HEAD"], worktree).stdout.strip()
    assert branch == "autodev/pilot-comment"


def test_nothing_is_pushed(world):
    approve(world)
    serve_once()
    worktree = world["worktrees"] / "pilot-comment"
    assert _git(["branch", "-r"], worktree).stdout.strip() == "", \
        "a remote-tracking branch means something was pushed"
    entry = [e for e in outbox.recent() if e["outcome"] == "done"][0]
    assert entry["pushed"] is False and entry["deployed"] is False


def test_a_reviewer_reject_is_reworked_automatically_once(world, monkeypatch):
    """★ THE OPERATOR'S Q2 ANSWER. One approval covers the retry, so the
    loop goes straight back to the developer with no second sign-off."""
    approve(world)
    monkeypatch.setenv("STUB_REVIEWER", "reject")

    rejections = {"count": 0}
    real_run = coordinator.run_task

    def run_then_approve(task_id, **kwargs):
        rejections["count"] += 1
        if rejections["count"] > 1:
            monkeypatch.delenv("STUB_REVIEWER", raising=False)
        return real_run(task_id, **kwargs)

    monkeypatch.setattr(coordinator, "run_task", run_then_approve)
    code, report, _ = serve_once()

    assert rejections["count"] == 2, "the rework did not happen"
    assert report.reworked == ["pilot-comment"]
    assert report.done == ["pilot-comment"]
    assert any(row.get("event") == "serve_rework" for row in state.read_audit())


def test_a_second_reject_parks_the_task_and_writes_an_outbox_entry(world, monkeypatch):
    approve(world)
    monkeypatch.setenv("STUB_REVIEWER", "reject")

    code, report, _ = serve_once()

    assert report.parked == ["pilot-comment"]
    assert report.done == []
    parked = [e for e in outbox.recent() if e["outcome"] == "parked"]
    assert parked, "a parked task must leave an outbox entry"
    assert parked[0]["reviewer_verdict"] == "reject"


def test_the_rework_stops_at_max_attempts(world, monkeypatch):
    """max_attempts is 2, so there is exactly one automatic rework --
    never a third attempt without an operator grant."""
    approve(world)
    monkeypatch.setenv("STUB_REVIEWER", "reject")
    calls = []
    real_run = coordinator.run_task
    monkeypatch.setattr(coordinator, "run_task",
                        lambda t, **k: (calls.append(t), real_run(t, **k))[1])

    serve_once()

    assert len(calls) == 2, f"ran {len(calls)} attempts; max_attempts is 2"


def test_a_gate_failure_is_parked_and_not_reworked(world, monkeypatch):
    """Deliberately narrower than the reviewer case. The operator's answer
    named a reviewer REJECT; a gate failure means the change did not
    build, test or stay in scope, which is a question about the ticket."""
    approve(world)
    monkeypatch.setenv("STUB_DEV_MODE", "outofscope")

    code, report, _ = serve_once()

    assert report.parked == ["pilot-comment"]
    assert report.reworked == []


# --- the caps ------------------------------------------------------------

def test_the_daily_cap_counts_tasks_started_and_waits_for_the_next_day(world):
    """★ serve COUNTS WHAT IT STARTS, not what passes. coordinator's own
    tasks_today only increments on success, which for an unattended loop
    would mean a day of failures never reaching the cap."""
    approve(world)
    st = state.State.load()
    st.data[serve.SERVE_DAY_KEY] = datetime.now().date().isoformat()
    st.data[serve.SERVE_STARTED_KEY] = 4
    st.save()

    code, report, sleeper = serve_once()

    assert report.capped and report.started == []
    assert sleeper.calls and sleeper.calls[0] > 3600, \
        f"it should wait for the next local day, waited {sleeper.calls}"


def test_the_cap_increments_once_per_task_not_once_per_attempt(world, monkeypatch):
    """A rework is part of the approved task, so it must not spend a
    second slot from the day's budget."""
    approve(world)
    monkeypatch.setenv("STUB_REVIEWER", "reject")
    serve_once()
    assert int(state.State.load().data[serve.SERVE_STARTED_KEY]) == 1


def test_a_new_local_day_resets_serves_counter(world):
    st = state.State.load()
    st.data[serve.SERVE_DAY_KEY] = "2020-01-01"
    st.data[serve.SERVE_STARTED_KEY] = 99
    st.save()
    approve(world)

    code, report, _ = serve_once()

    assert report.started == ["pilot-comment"], report.as_dict()


def test_seconds_until_next_local_day_is_a_day_at_most():
    at_noon = serve.seconds_until_next_local_day(
        datetime(2026, 11, 1, 12, 0, 0))
    assert 12 * 3600 < at_noon <= 12 * 3600 + 120
    just_before = serve.seconds_until_next_local_day(
        datetime(2026, 11, 1, 23, 59, 30))
    assert 0 < just_before < 180


def test_coordinators_own_counter_is_left_alone(world):
    """Additive, as every change to state here must be: the single-task
    `run` path still sees exactly the counter it always did."""
    approve(world)
    serve_once()
    data = state.State.load().data
    assert data["tasks_today"] == 1, "the success counter still counts successes"
    assert data[serve.SERVE_STARTED_KEY] == 1


# --- stopping ------------------------------------------------------------

def test_STOP_exits_the_loop_without_starting_anything(world):
    approve(world)
    (state.home() / state.STOP_FILE).write_text("stopped\n")

    code, report, _ = serve_once()

    assert code == 2 and report.stopped and report.started == []


def test_PAUSE_waits_instead_of_starting_a_task(world):
    approve(world)
    (state.home() / state.PAUSE_FILE).write_text("paused\n")

    code, report, sleeper = serve_once()

    assert report.started == []
    assert sleeper.calls, "it should have waited"


def test_a_rate_limit_halts_and_never_retries(world, monkeypatch):
    """★ HALT, NOT A RETRY, AND NEVER API BILLING. The operator's Q3
    answer. agents.child_env scrubs ANTHROPIC_API_KEY, so there is no
    metered fallback to switch to even if something tried."""
    approve(world)
    monkeypatch.setenv("STUB_RATE_LIMIT", "1")

    code, report, _ = serve_once()

    assert code == 3
    assert report.halted and "limit" in report.halted.lower()
    halted = [e for e in outbox.recent() if e["outcome"] == "halted"]
    assert halted, "a halt must leave an outbox entry"
    assert "ANTHROPIC_API_KEY" not in agents.child_env()


# --- the preflight -------------------------------------------------------

def test_a_role_without_a_turn_limit_halts_serve_before_any_task(world, monkeypatch):
    approve(world)
    monkeypatch.setattr(agents, "max_turns_preflight",
                        lambda: ["developer: the built command has no --max-turns"])
    sleeper = FakeSleeper()
    code, report = serve.serve(max_cycles=1, sleeper=sleeper, stub_ok=False)

    assert code == 3 and report.started == []
    assert "--max-turns" in report.halted


def test_every_real_role_has_a_positive_turn_limit():
    assert agents.max_turns_preflight() == []


# --- the outbox ----------------------------------------------------------

def test_the_outbox_entry_carries_the_evidence_a_reader_can_check(world):
    approve(world)
    serve_once()

    entry = [e for e in outbox.recent() if e["outcome"] == "done"][0]
    assert entry["task"] == "pilot-comment"
    assert entry["branch"] == "autodev/pilot-comment"
    assert entry["commit"], "an entry with no commit is not evidence"
    assert entry["reviewer_verdict"] == "approve"
    assert entry["gates"], "the gate results must be in the entry"
    assert entry["diffstat"] and "file(s)" in entry["diffstat"]
    assert entry["approval"]["operator_words"] == GOOD_NOTE["operator_words"]
    assert entry["written_by"] == "autodev-serve"


def test_the_entry_says_it_is_evidence_and_how_to_verify_it(world):
    """★ EVIDENCE, NEVER INSTRUCTIONS. The operator's Q4 answer. An
    outbox written by a machine and read by the session that writes the
    next ticket is exactly where an unverified claim would do damage."""
    approve(world)
    serve_once()

    entry = [e for e in outbox.recent() if e["outcome"] == "done"][0]
    assert "EVIDENCE" in entry["notice"]
    assert "not an instruction" in entry["notice"]
    assert "authorises" in entry["notice"]
    assert any("git diff" in command for command in entry["verify"])
    assert any(entry["commit"] in command for command in entry["verify"])


def test_an_unreadable_outbox_entry_is_reported_not_skipped(world):
    (outbox.outbox_dir()).mkdir(parents=True, exist_ok=True)
    (outbox.outbox_dir() / "20260101T000000-x-done.json").write_text("{broken")
    rows = outbox.recent()
    assert any(row["outcome"] == "unreadable_outbox_entry" for row in rows)


# --- it ships not started ------------------------------------------------

def test_the_loop_ships_with_continuation_and_scheduling_off(world):
    cfg = coordinator.config()
    assert cfg["auto_continue"] is False
    assert cfg["scheduling_enabled"] is False


def test_serve_does_not_read_scheduling_enabled_as_permission_to_start():
    """Nothing schedules this. The flag exists for the operator to turn
    on a future thing deliberately; serve must not consult it, or turning
    it on would silently start a loop."""
    source = (coordinator.Path(serve.__file__)).read_text()
    assert "scheduling_enabled" in source, "sanity: it is discussed in the module"
    body = source.split('"""', 2)[-1]
    assert "scheduling_enabled" not in body, \
        "serve reads scheduling_enabled outside its docstring"


def test_the_gate_is_what_blocks_the_pre_existing_entries_and_nothing_else(world):
    """★ TEETH FOR THE TEST ABOVE. If the five entries were refused for
    some incidental reason -- a missing queue file, a path mistake -- that
    test would pass while the gate did nothing. So: write the approval
    one of them never got, and it becomes runnable. That is the gate, and
    only the gate, holding them back.

    This is also exactly what must NOT be done in the real home. The
    harness redirects CARDFOUNDRY_AUTODEV_HOME, so this note is written
    into a tmp directory and the real queue is untouched.
    """
    queue_ticket(world, "deploy-guard-crons-comment")
    (world["home"] / "queue" / "pilot-comment.json").unlink()

    assert serve.next_runnable(set())[0] is None

    approve(world, task="deploy-guard-crons-comment")

    assert serve.next_runnable(set())[0] == "deploy-guard-crons-comment", \
        "with its approval written the gate lets it through, so the gate " \
        "is what was refusing it"
