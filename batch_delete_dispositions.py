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

★ ALLOCATED AND PICKED CARDS ARE STILL REFUSED, with ONE exception
added for E4 (operator, 2026-10-11): a `reserved` card held by an
`allocated` allocation for an unshipped order may be MOVED to another
live batch, so its order still ships while the rest of the batch goes.
Archive and delete stay refused for it -- a card promised to a buyer must
not leave circulation, and destroying its record would strand the order.
Resolving an order by substitution or marking short is still slice 4 and
is NOT built here.

★ THE RESERVED MOVE IS PHYSICAL. The pick list sends the picker to the
card's batch, so moving these rows re-points real people at a real shelf.
It has its own destination field rather than sharing the history-cards
one, because the two answer different questions: where sold history
should be filed is bookkeeping, where a promised card lives is a place
somebody has to walk to.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from batch_delete_preview import DECISION_STATUSES
from fulfillment_exception_service import TERMINAL_ORDER_STATUSES
from models import Batch, InventoryCard, OrderItem, PickAllocation, SalesOrder

logger = logging.getLogger("cardfoundry")

ARCHIVE_BATCH_CODE = "ARCHIVE"
ARCHIVE = "archive"
DELETE = "delete"
MOVE = "move"
CHOICES = (ARCHIVE, DELETE, MOVE)
DEFAULT_CHOICE = ARCHIVE
CONSIGNMENT_CODE_PREFIX = "CON_"
RESERVED_STATUS = "reserved"
# The ONE allocation status a reserved card may carry and still be moved.
# `picked` and `packed` mean someone has physically handled it for a
# shipment, `exception` means it is already mid-resolution, and `released`
# is the only restore path for a cancelled order -- none of those are a
# move question.
MOVABLE_ALLOCATION_STATUS = "allocated"


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


def reserved_rows(session: Session, batch_id: int) -> list[dict]:
    """Every `reserved` card in this batch, with its allocation and order.

    `movable` is the only one of these a delete may act on: an `allocated`
    allocation on an order that has not reached a terminal state. Anything
    else is returned too, flagged with why, so the screen and the refusal
    can both name it instead of the operator meeting a silent skip.
    """
    rows = (
        session.query(InventoryCard, PickAllocation, SalesOrder)
        .outerjoin(PickAllocation, PickAllocation.inventory_card_id == InventoryCard.id)
        .outerjoin(OrderItem, OrderItem.id == PickAllocation.order_item_id)
        .outerjoin(SalesOrder, SalesOrder.id == OrderItem.order_id)
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == RESERVED_STATUS,
        )
        .order_by(InventoryCard.id)
        .all()
    )
    found = []
    for card, allocation, order in rows:
        if allocation is None or order is None:
            reason = "has no live allocation or order to ship"
        elif allocation.status != MOVABLE_ALLOCATION_STATUS:
            reason = f"its allocation is {allocation.status!r}, not 'allocated'"
        elif str(order.status or "") in TERMINAL_ORDER_STATUSES:
            reason = f"its order is already {order.status!r}"
        else:
            reason = None
        found.append({
            "card_id": card.id,
            "name": card.name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "condition_id": card.condition_id,
            "finish_id": card.finish_id,
            "allocation_id": getattr(allocation, "id", None),
            "allocation_status": getattr(allocation, "status", None),
            "order_id": getattr(order, "id", None),
            "order_status": getattr(order, "status", None),
            "order_label": (
                getattr(order, "external_label", None)
                or getattr(order, "external_order_id", None)
                or (str(order.id) if order else "")
            ),
            "movable": reason is None,
            "reason": reason,
        })
    return found


def validate_move_target(
    session: Session, batch_id: int, target_id: int | None, *, what: str,
) -> Batch:
    """The destination rules, in one place, applied by the dry run AND the
    apply because both go through resolve_choices.

    Archived is refused HERE even though the archive choice deliberately
    moves INTO an archived batch: that destination is chosen by the code,
    not typed by an operator, and a card an operator is moving on purpose
    should not silently stop being for sale.
    """
    if not target_id:
        raise DispositionError(f"{what} needs a destination batch.")
    target = session.get(Batch, target_id)
    if not target:
        raise DispositionError(f"{what}: the destination batch was not found.")
    if target.id == batch_id:
        raise DispositionError(
            f"{what}: the destination cannot be the batch being deleted."
        )
    if target.is_archived:
        raise DispositionError(
            f"{what}: {target.batch_code!r} is archived, so cards moved there "
            "would stop being for sale."
        )
    if target.is_consignment:
        raise DispositionError(
            f"{what}: {target.batch_code!r} is a consignment batch, so moving "
            "our cards there would attribute them to a consignor."
        )
    if str(target.batch_code or "").upper().startswith(CONSIGNMENT_CODE_PREFIX):
        raise DispositionError(
            f"{what}: {target.batch_code!r} is named like a consignment batch. "
            "Settle what it is before moving cards into it."
        )
    return target


def resolve_choices(
    session: Session, batch_id: int, *, apply_to_all: str = DEFAULT_CHOICE,
    per_card: dict | None = None, move_target_id: int | None = None,
    reserved_target_id: int | None = None,
) -> dict:
    """Decide, per card, what the operator's choices mean.

    `per_card` maps card id -> choice and overrides `apply_to_all` for
    exactly those cards. Returns the plan; writes nothing.

    Reserved cards are NOT part of that choice. They take one disposition
    only -- move, to `reserved_target_id` -- and offering them the same
    radio group would invite archive or delete on a card somebody has
    already paid for.
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
        target = validate_move_target(
            session, batch_id, move_target_id, what="Moving cards that carry history",
        )

    # ★ RESERVED CARDS. Forced onto move, with their own destination.
    # Only the MOVABLE ones are planned; a reserved card that cannot be
    # moved honestly is deliberately NOT refused here, because
    # slice_2_refusals already refuses the whole delete for it and that is
    # the screen which lists every reason at once. Raising here as well
    # would pre-empt it with a narrower message, and a card left out of
    # the plan simply stays in the batch -- which the refusal then blocks,
    # so it can never be swept into a delete by this omission.
    all_reserved = reserved_rows(session, batch_id)
    reserved = [r for r in all_reserved if r["movable"]]
    stuck = [r for r in all_reserved if not r["movable"]]
    reserved_target = None
    if stuck:
        logger.info(
            "batch delete: batch %s holds %s reserved card(s) that cannot be "
            "moved (%s); planning nothing for them and leaving the refusal to "
            "say so.",
            batch_id, len(stuck),
            "; ".join(f"#{r['card_id']} {r['reason']}" for r in stuck),
        )
    elif reserved:
        reserved_target = validate_move_target(
            session, batch_id, reserved_target_id,
            what=f"Moving the {len(reserved)} card(s) reserved for an order",
        )
        logger.info(
            "batch delete: %s reserved card(s) in batch %s will move to %r so "
            "their orders still ship.",
            len(reserved), batch_id, reserved_target.batch_code,
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
        "reserved_target_id": reserved_target.id if reserved_target else None,
        "reserved_target_code": (
            reserved_target.batch_code if reserved_target else None
        ),
        "reserved_move": [r["card_id"] for r in reserved],
        "reserved_rows": all_reserved,
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
