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

★ THE STANDING LOW-RISK RULE (operator, 2026-11-04 07:15 ET). The
operator decided that Cowork may write the note ITSELF, without asking
them per item, when the item is LOW-RISK -- and tells them afterwards
what it approved. Low-risk means the item touches NONE of:

    money · pricing · Mana Pool · database structure or a migration ·
    new JavaScript · anything a buyer or a consignor sees or receives ·
    anything irreversible · any CLAUDE.md non-negotiable

Everything else still needs their explicit OK for that item, and
policy.py's forbidden paths still apply on top, unchanged.

★ AND SINCE 2026-11-05 07:18 ET, A RELEASE MAY REST ON THIS NOTE TOO.
The operator decided that a release whose diff is low-risk by the
mechanical gate may go live without a per-release OK, reported
afterwards. This module is HALF of what authorises that, and only half:
it answers "did anyone agree to this work", while
scripts/autodev/release_gate.py answers "may a release contain this
diff" from the actual diff. Both are required. Nothing in this file
changed to allow it -- a release runner reading a note written under the
standing rule gets exactly the verdict it always would have.

A note resting on that rule says so, in two extra fields:

    {
      ... the six required fields above ...
      "approval_basis": "standing-low-risk-2026-11-04",
      "low_risk_assessment": "comment-only change to one docstring; no
                              money, marketplace, schema, JS or
                              buyer-facing surface"
    }

★ WHY IT IS A DECLARED BASIS AND NOT A LOOSER READING OF THE EXISTING
FIELDS. A note quoting the standing rule in `operator_words` would have
passed this gate mechanically, with nothing anywhere recording that it
rested on a rule rather than on an OK for that item -- so the outbox and
the audit would read identically either way, and the documented design
("the operator's own OK for that task") would be quietly untrue. The
basis is therefore named in the note, checked here against a closed set,
and carried into `as_evidence()`, so a reader of any entry can tell
which authority the run actually had.

★ AND IT FAILS CLOSED, the same way everything else here does. An
UNKNOWN basis refuses -- a typo or a made-up rule id is not an
authority. A missing or blank `low_risk_assessment` refuses, and so does
one too short to be a reason: the rule asks Cowork to make a judgement,
and a note that records no judgement is not evidence that one was made.
A `low_risk_assessment` with NO basis refuses too, because that note is
ambiguous about which path it is claiming -- and the per-item path needs
nothing extra, so it would otherwise pass while looking as though a
low-risk assessment had been checked.

★ WHAT THIS MODULE CANNOT CHECK, said plainly. `low_risk_assessment` is
Cowork's own statement about the item, not proof: this gate sees the
note and the ticket's digest, never the diff. The mechanical check on
what the work may touch is policy.py, which is unchanged and applies to
every ticket whatever its basis. Under the standing basis
`operator_words` quotes the STANDING RULE and its date -- the words
recorded in STANDING_BASES -- never words about this item, which the
operator never said.
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

# The standing rules a note may rest on instead of a per-item OK, each
# with the operator's own words that created it. A CLOSED set: an
# `approval_basis` that is not a key here is refused, so a typo or an
# invented rule id authorises nothing. The words live HERE rather than in
# the note because the note is written by Cowork -- a rule whose only
# record of the operator's decision was written by the party relying on
# it would be an assertion, not evidence.
STANDING_BASES: dict[str, dict[str, str]] = {
    "standing-low-risk-2026-11-04": {
        "decided_at": "2026-11-04T07:15:00-05:00",
        "operator_words": (
            '2026-11-04 07:15 ET "yes" to: Cowork may write an AutoDev '
            "approval note itself, without asking per item, when the item "
            "is low-risk, and tells Chris afterwards what it approved"
        ),
    },
}

# What "low-risk" excludes, in the operator's own terms. Documentation,
# not a check -- this module never sees the diff, so nothing here is
# enforced here. policy.py is the mechanical gate on what the work may
# touch, and it applies to every ticket whatever its basis.
LOW_RISK_EXCLUSIONS: tuple[str, ...] = (
    "money",
    "pricing",
    "Mana Pool",
    "database structure or a migration",
    "new JavaScript",
    "anything a buyer or a consignor sees or receives",
    "anything irreversible",
    "any CLAUDE.md non-negotiable",
)

# A one-line reason has to BE a reason. Long enough that "ok", "fine",
# "low risk" and "no risk" are refused; short enough that one honest
# clause about what the item touches passes. The same stance as the
# three-character floor on operator_words below, set higher because this
# field is a judgement rather than a quotation.
MIN_ASSESSMENT_LENGTH = 25


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
    def approval_basis(self) -> str:
        return str(self.data.get("approval_basis") or "").strip()

    @property
    def low_risk_assessment(self) -> str:
        return str(self.data.get("low_risk_assessment") or "").strip()

    @property
    def rests_on_standing_rule(self) -> bool:
        """True when this note's authority is a standing rule, not a
        per-item OK. Only ever true for a basis this module knows."""
        return self.approval_basis in STANDING_BASES

    @property
    def is_approved(self) -> bool:
        return not self.refusals

    def as_evidence(self) -> dict:
        """What the outbox and the audit record about this approval.

        The operator's words are included on purpose -- an entry saying
        only "approved" is not evidence of anything.

        ★ AND THE BASIS IS INCLUDED FOR THE SAME REASON. Without it a run
        authorised by the standing low-risk rule and a run the operator
        OK'd item by item would produce identical entries, and a reader
        could not tell which. `approval_basis` is null on the per-item
        path, which is what the absence means.
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
            "approval_basis": self.approval_basis or None,
            "low_risk_assessment": self.low_risk_assessment or None,
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

    # ★ THE STANDING-RULE PATH. Omitting both fields leaves the per-item
    # path exactly as it was, which is why neither is in REQUIRED_FIELDS.
    # Every branch below refuses; none of them relaxes anything.
    basis = str(data.get("approval_basis") or "").strip()
    assessment = str(data.get("low_risk_assessment") or "").strip()
    if basis and basis not in STANDING_BASES:
        approval.refusals.append(
            f"approval_basis {basis!r} is not a standing rule this gate "
            f"knows (known: {', '.join(sorted(STANDING_BASES)) or 'none'}); "
            "an unrecognised basis authorises nothing"
        )
    if basis:
        if not assessment:
            approval.refusals.append(
                "a note resting on a standing rule must carry "
                "low_risk_assessment -- Cowork's own one-line reason the "
                "item is low-risk; a note that records no judgement is not "
                "evidence that one was made"
            )
        elif len(assessment) < MIN_ASSESSMENT_LENGTH:
            approval.refusals.append(
                f"low_risk_assessment is {len(assessment)} characters; at "
                f"least {MIN_ASSESSMENT_LENGTH} are needed for it to be a "
                "reason rather than a word"
            )
    elif assessment:
        approval.refusals.append(
            "low_risk_assessment is set but approval_basis is not, so this "
            "note does not say what authorises it; name the standing rule "
            "or drop the assessment and quote the operator's own OK"
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
    elif approval.rests_on_standing_rule:
        # ★ SAID OUT LOUD, not only recorded in the outbox entry. A task
        # that authorised itself from a standing rule is the one case the
        # operator did not see coming, so it belongs in the log they can
        # read without opening an entry.
        logger.info(
            "autodev approvals: %s authorised by standing rule %s "
            "(Cowork's assessment: %s)",
            task_id, approval.approval_basis, approval.low_risk_assessment,
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
