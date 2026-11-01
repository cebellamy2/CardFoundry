"""The gate that decides whether a queued ticket may run unattended.

★ WHY A SECOND GATE ON TOP OF policy.py. policy.py answers "is this kind
of work safe for an agent" -- a denylist over an allowlist, evaluated from
the ticket's own contents. It cannot answer "did a human agree to THIS
task", because a ticket is drafted by Cowork and a confident ticket can
mark itself `autonomous: yes`. The operator's decision of 2026-10-31 is
that nothing reaches AutoDev until they and Cowork have assessed it and
answered every open question, and that their "AutoDev-ready" OK IS the
approval. That OK lives nowhere in the ticket, so it lives here: one note
per task, written by Cowork only on an explicit OK, recording the
operator's own words.

★ FAILS CLOSED, LIKE EVERY OTHER GUARD HERE. A missing note, an
unreadable note, a note missing a field, a note naming a different task --
all of them refuse. The serve loop runs unattended, so "no opinion" has
to mean "no" or the loop becomes its own authoriser. Same stance as
policy._forbidden_reason and scripts/hooks/pre-push with a missing cron
list.

★ ONE NOTE COVERS THE TASK INCLUDING ITS AUTOMATIC RETRY. The operator
settled this: no second sign-off between a reviewer reject and the
developer's second attempt, because the retry is part of the task they
approved, and stopping for a signature the operator already gave would
make the loop pointless. `covers_automatic_retry` records that in the
note rather than leaving it as an assumption in this file.

THE NOTE. claude/autodev/approvals/<task-id>.json, written by Cowork:

    {
      "task": "some-task-id",
      "autodev_ready": true,
      "operator_words": "yes, that one is AutoDev-ready",
      "approved_at": "2026-11-01T09:12:00-04:00",
      "written_by": "cowork",
      "covers_automatic_retry": true,
      "ticket_digest": "a1b2c3d4e5f60718"
    }

`ticket_digest` is OPTIONAL and verified when present: with it, editing
the ticket after the OK invalidates the approval, which is the stronger
property. It is optional because Cowork writes these notes by hand and
may not be able to run anything in this checkout; `coordinator.py status`
prints the current digest of every queued ticket so it can be read off
and passed along.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import state

logger = logging.getLogger("cardfoundry")

# Every field Cowork must write. Each one is here because its absence
# would let something through that nobody agreed to:
#   task                   binds the note to one ticket, not to the queue
#   autodev_ready          the OK itself, and it must be exactly true
#   operator_words         the operator's own words, so the note is
#                          evidence of a decision rather than an assertion
#                          that one happened
#   approved_at            when, so a stale note is visible
#   written_by             who wrote the note (Cowork, never AutoDev)
#   covers_automatic_retry records that the retry needs no second sign-off
REQUIRED_FIELDS: tuple[str, ...] = (
    "task", "autodev_ready", "operator_words", "approved_at",
    "written_by", "covers_automatic_retry",
)

WRITER = "cowork"


@dataclass
class Approval:
    """One approval note, and every reason it does not authorise a run."""

    path: Path
    data: dict
    refusals: list[str] = field(default_factory=list)

    @property
    def task(self) -> str:
        return str(self.data.get("task") or "")

    @property
    def operator_words(self) -> str:
        return str(self.data.get("operator_words") or "")

    @property
    def approved_at(self) -> str:
        return str(self.data.get("approved_at") or "")

    @property
    def ticket_digest(self) -> str:
        return str(self.data.get("ticket_digest") or "")

    @property
    def is_approved(self) -> bool:
        return not self.refusals

    def as_evidence(self) -> dict:
        """What the outbox and the audit record about this approval.

        The operator's words are included on purpose -- an entry saying
        only "approved" is not evidence of anything.
        """
        return {
            "path": str(self.path),
            "task": self.task,
            "approved_at": self.approved_at,
            "written_by": str(self.data.get("written_by") or ""),
            "operator_words": self.operator_words,
            "covers_automatic_retry": bool(
                self.data.get("covers_automatic_retry")),
            "ticket_digest": self.ticket_digest or None,
        }


def approvals_dir() -> Path:
    return state.home() / "approvals"


def path_for(task_id: str) -> Path:
    return approvals_dir() / f"{task_id}.json"


def evaluate(data: dict, task_id: str, *, path: Path | None = None,
             ticket_digest: str | None = None) -> Approval:
    """Every reason this note does not authorise this task, not just one.

    All of them at once, for the same reason policy.evaluate reports every
    refusal together: one message should tell Cowork everything to fix.
    """
    approval = Approval(path=path or Path("<memory>"), data=data)

    for name in REQUIRED_FIELDS:
        if data.get(name) in (None, "", [], {}):
            approval.refusals.append(f"missing required field '{name}'")

    # `autodev_ready` must be the boolean true. A string "yes" or a 1 is
    # refused rather than interpreted: this is the field that carries the
    # operator's decision, and guessing at its type is how a template
    # with a placeholder value starts authorising runs.
    if data.get("autodev_ready") is not True:
        approval.refusals.append(
            "autodev_ready must be exactly true (the operator's explicit "
            "'AutoDev-ready' OK); anything else is not an approval"
        )
    if data.get("covers_automatic_retry") is not True:
        approval.refusals.append(
            "covers_automatic_retry must be exactly true -- one note covers "
            "the task including its automatic retry, and a note that does "
            "not say so is not the note this gate accepts"
        )

    writer = str(data.get("written_by") or "").strip().lower()
    if writer and writer != WRITER:
        approval.refusals.append(
            f"written_by is '{writer}'; only {WRITER!r} may write an "
            "approval note"
        )

    words = str(data.get("operator_words") or "").strip()
    if words and len(words) < 3:
        approval.refusals.append(
            "operator_words is too short to be the operator's own words"
        )

    stamp = str(data.get("approved_at") or "").strip()
    if stamp:
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            approval.refusals.append(
                f"approved_at {stamp!r} is not an ISO-8601 timestamp"
            )

    named = str(data.get("task") or "")
    if named and named != task_id:
        approval.refusals.append(
            f"this note approves {named!r}, not {task_id!r}"
        )

    # Verified only when the note carries one, so an approval that omits
    # it is weaker but still usable. A WRONG digest is always a refusal:
    # it means the ticket changed after the operator looked at it.
    if approval.ticket_digest and ticket_digest is not None:
        if approval.ticket_digest != ticket_digest:
            approval.refusals.append(
                f"the ticket has changed since it was approved "
                f"(note {approval.ticket_digest}, ticket {ticket_digest}); "
                "re-assess it and write a new note"
            )

    if approval.refusals:
        logger.info(
            "autodev approvals: %s not authorised: %s",
            task_id, "; ".join(approval.refusals),
        )
    return approval


def load(task_id: str, *, ticket_digest: str | None = None) -> Approval:
    """Read and evaluate one task's approval note.

    A missing file is the common case on purpose: every ticket already in
    the queue predates this gate and none of them is approved under it.
    """
    path = path_for(task_id)
    if not path.exists():
        return Approval(path=path, data={}, refusals=[
            f"no approval note at {path} -- the operator's 'AutoDev-ready' "
            "OK is what authorises a run, and Cowork writes that note only "
            "on an explicit OK"
        ])
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # Catch-and-continue, so it logs: an unreadable note must not take
        # the loop down, and must not pass either.
        logger.warning("autodev approvals: cannot read %s: %s", path, exc)
        return Approval(path=path, data={},
                        refusals=[f"unreadable approval note: {exc}"])
    if not isinstance(data, dict):
        return Approval(path=path, data={},
                        refusals=["approval note is not a JSON object"])
    return evaluate(data, task_id, path=path, ticket_digest=ticket_digest)
