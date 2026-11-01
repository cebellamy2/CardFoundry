#!/usr/bin/env python3
"""Print one queued ticket's verdict, digest and runnable state. READ-ONLY.

★ WHO THIS IS FOR. Cowork writes both the queue ticket and the approval
note now (operator, 2026-11-01), and the approval note may carry the
ticket's digest -- which invalidates the approval if the ticket is edited
afterwards. So Cowork needs one command it can ask for on the Mac,
BEFORE writing the note, that answers: would the policy accept this
ticket, what is its digest, and what is standing between it and a run.

★ IT WRITES NOTHING AND NEEDS NO SECRETS. No config.json is created, no
directory is made, no audit row is added, no network call is made and no
credential is read. It reads the queue file, the approval note if there
is one, and the audit log. Proved by a test that snapshots every file
under the AutoDev home before and after.

    python -m scripts.autodev.validate_ticket <task-id>
    python -m scripts.autodev.validate_ticket --all
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import approvals, completions, policy, state


def _digest(ticket: policy.Ticket) -> str | None:
    """coordinator.ticket_digest without importing coordinator, which
    would create config.json on import of its config()."""
    import hashlib

    if not ticket.data:
        return None
    blob = json.dumps(ticket.data, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def inspect(task_id: str, done_index: dict | None = None) -> dict:
    path = state.home() / "queue" / f"{task_id}.json"
    ticket = policy.load(path)
    digest = _digest(ticket)
    approval = approvals.load(task_id, ticket_digest=digest)
    if done_index is None:
        done_index = completions.newest_done()
    completion = completions.load(task_id, index=done_index)
    completion_refusals = completions.refusals(
        task_id, approval.approved_at, index=done_index)
    return {
        "task": task_id,
        "queue_file": str(path),
        "queue_file_exists": path.exists(),
        "ticket_digest": digest,
        "policy_eligible": ticket.is_eligible,
        "policy_refusals": ticket.refusals,
        "approval_note": str(approvals.path_for(task_id)),
        "approved": approval.is_approved,
        "approval_refusals": approval.refusals,
        "already_done": completion.is_done,
        "done_at": completion.when or None,
        "completion_refusals": completion_refusals,
        "runnable": not (ticket.refusals or approval.refusals
                         or completion_refusals),
    }


def _report(row: dict) -> None:
    print(f"task:              {row['task']}")
    print(f"queue file:        {row['queue_file']}"
          f"{'' if row['queue_file_exists'] else '   (MISSING)'}")
    print(f"ticket_digest:     {row['ticket_digest']}")
    print(f"policy_eligible:   {row['policy_eligible']}")
    for reason in row["policy_refusals"]:
        print(f"    policy says:   {reason}")
    print(f"approved:          {row['approved']}")
    for reason in row["approval_refusals"]:
        print(f"    approval says: {reason}")
    print(f"already_done:      {row['already_done']}"
          f"{'  at ' + row['done_at'] if row['done_at'] else ''}")
    for reason in row["completion_refusals"]:
        print(f"    completion:    {reason}")
    print(f"RUNNABLE:          {row['runnable']}")
    if row["runnable"]:
        print("\nThe policy and completion gates are satisfied. Write the "
              "approval note with this exact ticket_digest; editing the "
              "ticket afterwards invalidates it.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--all", action="store_true",
                        help="every ticket in the queue")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = state.home() / "queue"
    if args.all or not args.task_id:
        ids = sorted(p.stem for p in root.glob("*.json")) if root.exists() else []
    else:
        ids = [args.task_id]
    if not ids:
        print(f"no tickets in {root}")
        return 0

    done_index = completions.newest_done()
    rows = [inspect(task_id, done_index) for task_id in ids]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for index, row in enumerate(rows):
        if index:
            print()
        _report(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
