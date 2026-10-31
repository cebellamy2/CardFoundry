"""The eligibility policy refuses what the operator reserved for themselves.

These assert the OUTCOME an operator would see -- which tickets are
refused and with what reason -- rather than that some function was
called, per CLAUDE.md's testing rule.
"""

import json

import pytest

from scripts.autodev import policy


def base_ticket(**overrides) -> dict:
    ticket = {
        "id": "deploy-guard-crons-comment",
        "title": "Correct the wrong reason in deploy-guard-crons' comment",
        "autonomous": "yes",
        "eligibility_reviewed_by": "cowork",
        "eligibility_reviewed_at": "2026-10-31",
        "intent": "The comment gives a wrong reason for leaving two crons unguarded.",
        "acceptance": ["the parsed tick list is unchanged"],
        "files": ["scripts/hooks/deploy-guard-crons"],
        "tests": ["tests/test_pre_push_hook.py"],
        "risk_flags": [],
        "comment_only_paths": ["scripts/hooks/deploy-guard-crons"],
    }
    ticket.update(overrides)
    return ticket


def test_the_pilot_ticket_is_eligible():
    assert policy.evaluate(base_ticket()).is_eligible


def test_a_ticket_not_marked_autonomous_is_refused():
    ticket = policy.evaluate(base_ticket(autonomous="no"))
    assert not ticket.is_eligible
    assert any("autonomous is not 'yes'" in reason for reason in ticket.refusals)


@pytest.mark.parametrize("path", [
    "models.py", "database.py", "manapool_service.py",
    "consignment_service.py", "new_listing_pricing_service.py",
    "VERSION", "CHANGELOG.md", ".env", "cardfoundry.db",
    "scripts/hooks/pre-push", "order_service.py",
])
def test_protected_paths_are_refused_however_the_ticket_is_marked(path):
    ticket = policy.evaluate(base_ticket(files=[path], comment_only_paths=[]))
    assert not ticket.is_eligible, f"{path} should never be autonomous"


@pytest.mark.parametrize("flag", [
    "money", "pricing", "payout", "marketplace_write", "migration",
    "destructive", "production", "browser_measurement", "irreversible",
])
def test_blocking_risk_flags_escalate(flag):
    ticket = policy.evaluate(base_ticket(risk_flags=[flag]))
    assert not ticket.is_eligible
    assert any(flag in reason for reason in ticket.refusals)


def test_a_new_file_in_a_protected_area_is_refused_by_pattern():
    ticket = policy.evaluate(base_ticket(files=["new_payout_report_service.py"]))
    assert not ticket.is_eligible
    assert any("payout" in reason for reason in ticket.refusals)


def test_every_refusal_is_reported_not_just_the_first():
    ticket = policy.evaluate({"id": "x", "autonomous": "no"})
    assert len(ticket.refusals) >= 3


def test_paths_outside_the_repo_are_refused():
    for path in ("/etc/passwd", "../other-repo/main.py"):
        ticket = policy.evaluate(base_ticket(files=[path], comment_only_paths=[]))
        assert not ticket.is_eligible


def test_comment_only_paths_must_be_declared_files():
    ticket = policy.evaluate(base_ticket(comment_only_paths=["README.md"]))
    assert not ticket.is_eligible


def test_an_unreadable_ticket_file_is_a_refusal_not_a_crash(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    ticket = policy.load(path)
    assert not ticket.is_eligible
    assert any("unreadable" in reason for reason in ticket.refusals)


def test_a_ticket_file_round_trips(tmp_path):
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(base_ticket()))
    assert policy.load(path).is_eligible
