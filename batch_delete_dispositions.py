"""What happens to each card when a batch is deleted.

Slice 3 of Delete batch. Slice 2 would only delete a batch whose every
card was `available`. This opens it to batches holding SOLD, REMOVED and
UNSELLABLE cards, each of which carries history worth keeping, by making
the operator say what should happen to them.

★ THE SHAPE THE OPERATOR ASKED FOR (2026-10-11): one APPLY-TO-ALL choice
at the top -- archive (the default), delete, or move to another batch --
and a per-card list below it for exceptions, which override the bulk
choice. So the common case is one click and the awkward case is still
expressible.

★ ARCHIVE IS A BATCH, NOT A FLAG. There is no per-card archive in this
schema and adding one would mean teaching ten Mana Pool queries a second
meaning of "archived". A reserved batch with is_archived=True gets the
same result for free: every batch-aware quantity query already filters on
Batch.is_archived and nothing else about the batch, so an archived card
contributes nothing to any listing while keeping its sale, its payout and
its order lines intact. Archive and "move to another batch" are therefore
the same operation with a different destination, and both go through
bulk_move_cards_to_batch, which audits per card.

★ ALLOCATED AND PICKED CARDS ARE STILL REFUSED. Resolving an open order
is slice 4. A card promised to a buyer is not a disposition question.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from batch_delete_preview import DECISION_STATUSES
from models import Batch, InventoryCard

logger = logging.getLogger("cardfoundry")

ARCHIVE_BATCH_CODE = "ARCHIVE"
ARCHIVE = "archive"
DELETE = "delete"
MOVE = "move"
CHOICES = (ARCHIVE, DELETE, MOVE)
DEFAULT_CHOICE = ARCHIVE


class DispositionError(ValueError):
    """The choices were refused. Nothing is written."""


def archive_batch(session: Session, *, create: bool = True) -> Batch | None:
    """The reserved archive batch, created on first use and reused after.

    Created archived: a batch that exists to hold cards out of circulation
    must not itself put them back on sale.
    """
    batch = (
        session.query(Batch)
        .filter(func.upper(Batch.batch_code) == ARCHIVE_BATCH_CODE)
        .first()
    )
    if batch or not create:
        return batch
    batch = Batch(batch_code=ARCHIVE_BATCH_CODE, is_archived=True,
                  is_consignment=False)
    session.add(batch)
    session.flush()
    logger.info(
        "batch delete: created the reserved archive batch %r (id %s, archived) "
        "on first use.", ARCHIVE_BATCH_CODE, batch.id,
    )
    return batch


def resolve_choices(
    session: Session, batch_id: int, *, apply_to_all: str = DEFAULT_CHOICE,
    per_card: dict | None = None, move_target_id: int | None = None,
) -> dict:
    """Decide, per card, what the operator's choices mean.

    `per_card` maps card id -> choice and overrides `apply_to_all` for
    exactly those cards. Returns the plan; writes nothing.
    """
    bulk = str(apply_to_all or "").strip().lower() or DEFAULT_CHOICE
    if bulk not in CHOICES:
        raise DispositionError(
            f"{apply_to_all!r} is not a choice. Use one of: {', '.join(CHOICES)}."
        )
    overrides = {int(k): str(v).strip().lower() for k, v in (per_card or {}).items()}
    bad = {cid: c for cid, c in overrides.items() if c not in CHOICES}
    if bad:
        raise DispositionError(
            "These per-card choices are not valid: "
            + ", ".join(f"#{cid}={c!r}" for cid, c in sorted(bad.items()))
        )

    cards = (
        session.query(InventoryCard)
        .filter(InventoryCard.batch_id == batch_id)
        .order_by(InventoryCard.id).all()
    )
    needs_choice = [c for c in cards if c.status in DECISION_STATUSES]

    target = None
    if MOVE in ({bulk} | set(overrides.values())):
        if not move_target_id:
            raise DispositionError(
                "Moving needs a destination batch."
            )
        target = session.get(Batch, move_target_id)
        if not target:
            raise DispositionError("The destination batch was not found.")
        if target.id == batch_id:
            raise DispositionError(
                "The destination cannot be the batch being deleted."
            )
        if target.is_archived and bulk == MOVE:
            # Allowed, but say so: an archived destination is the archive
            # choice by another name, and the operator should know the
            # cards will not be for sale there.
            logger.info(
                "batch delete: move destination %r is archived, so the moved "
                "cards will not be listed.", target.batch_code,
            )

    plan = {ARCHIVE: [], DELETE: [], MOVE: []}
    for card in needs_choice:
        plan[overrides.get(card.id, bulk)].append(card)

    sold_deleted = round(float(sum(
        c.sold_price or 0 for c in plan[DELETE] if c.status == "sold"
    )), 2)
    return {
        "batch_id": batch_id,
        "apply_to_all": bulk,
        "overrides": overrides,
        "move_target_id": target.id if target else None,
        "move_target_code": target.batch_code if target else None,
        "cards_total": len(cards),
        "needs_choice": len(needs_choice),
        "archive": [c.id for c in plan[ARCHIVE]],
        "delete": [c.id for c in plan[DELETE]],
        "move": [c.id for c in plan[MOVE]],
        # ★ The number the operator actually needs to see: money whose
        # record is about to be destroyed, as distinct from money merely
        # moved out of circulation.
        "sold_history_deleted": sold_deleted,
        "sold_history_archived": round(float(sum(
            c.sold_price or 0 for c in plan[ARCHIVE] if c.status == "sold"
        )), 2),
        "sold_history_moved": round(float(sum(
            c.sold_price or 0 for c in plan[MOVE] if c.status == "sold"
        )), 2),
        "by_status": {
            choice: dict(sorted(
                {s: sum(1 for c in plan[choice] if c.status == s)
                 for s in {x.status for x in plan[choice]}}.items()
            ))
            for choice in CHOICES
        },
    }
