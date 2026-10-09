import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from actor_context import current_actor
from models import (
    Batch,
    InventoryCard,
    OrderItem,
    PickAllocation,
    PickWave,
    PickWaveEvent,
    PickWaveOrder,
    SalesOrder,
)
from fulfillment_exception_invariants import order_has_fulfillment_submission_block
from fulfillment_exception_resolution_service import auto_resolved_on_submission_ids
from models import FulfillmentException


logger = logging.getLogger("cardfoundry")


REOPEN_MANA_POOL_NOTE = (
    "Mana Pool has already been told these orders are processing -- "
    "reopening this wave does not undo that."
)


ELIGIBLE_ORDER_STATUS = "ready_to_pick"

# ★ THE WAVE'S OWN LIFECYCLE (v2.22.0). It used to be active -> completed,
# where "completed" meant "picking finished" and the operator's real work --
# packing and shipping -- happened afterwards with no wave-level record of it
# at all. The wave now follows the work: active (picking) -> picked -> packed
# -> shipped, with cancelled still reachable only from active.
#
# WAVE_STATUS_COMPLETED IS NOT RETIRED. Every wave that shipped before this
# change is stored as "completed", and rewriting history would be a
# destructive migration to fix a vocabulary problem. It is read as a legacy
# synonym for "picking finished" and displayed per _legacy_display_status;
# nothing writes it any more.
WAVE_STATUS_ACTIVE = "active"
WAVE_STATUS_PICKED = "picked"
WAVE_STATUS_PACKED = "packed"
WAVE_STATUS_SHIPPED = "shipped"
WAVE_STATUS_CANCELLED = "cancelled"
WAVE_STATUS_COMPLETED_LEGACY = "completed"

# A wave past picking but not yet shipped. Its pick list is still live, its
# orders still belong to it, and it can still go Back to Picking.
WAVE_STATUSES_IN_FLIGHT = (
    WAVE_STATUS_PICKED, WAVE_STATUS_PACKED, WAVE_STATUS_COMPLETED_LEGACY,
)
WAVE_STATUSES_TERMINAL = (WAVE_STATUS_SHIPPED, WAVE_STATUS_CANCELLED)

# Order statuses a Back to Picking may legitimately find. "packed" is here
# because packing is local and reversible and the operator's rule is that
# packed orders stay packed; "shipped" and "cancelled" are deliberately
# absent -- see the guard in reopen_pick_wave.
REOPENABLE_ORDER_STATUSES = ("picked", "in_pick_wave", "packed")


class PickWaveSelectionError(ValueError):
    """Raised when the requested order selection cannot become a pick wave."""


def create_pick_wave(
    session: Session,
    order_ids: list[int],
    label: str | None = None,
) -> PickWave:
    """Create a pick wave from exactly the selected orders.

    Never auto-includes other ready orders. Selection is all-or-nothing: if
    any requested order is missing or no longer eligible, the whole call
    fails with the offending orders named, and no wave is created.
    """
    unique_ids = list(dict.fromkeys(order_ids))

    if not unique_ids:
        raise PickWaveSelectionError("Select at least one order for the pick wave.")

    orders_by_id = {
        order.id: order
        for order in session.query(SalesOrder).filter(SalesOrder.id.in_(unique_ids)).all()
    }

    ineligible = []
    for order_id in unique_ids:
        order = orders_by_id.get(order_id)
        if order is None:
            ineligible.append(f"#{order_id} (not found)")
        elif order.status != ELIGIBLE_ORDER_STATUS:
            display = order.external_label or order.external_order_id
            ineligible.append(f"{display} (now {order.status!r}, not ready_to_pick)")

    if ineligible:
        raise PickWaveSelectionError(
            "Selected orders are no longer eligible: " + "; ".join(ineligible)
        )

    if not label:
        label = datetime.now().strftime(
            "Wave %Y-%m-%d %I:%M %p"
        )

    wave = PickWave(
        label=label,
        status="active",
    )

    session.add(wave)
    session.flush()

    for order_id in unique_ids:
        order = orders_by_id[order_id]

        session.add(
            PickWaveOrder(
                wave_id=wave.id,
                order_id=order.id,
                status="active",
            )
        )

        order.status = "in_pick_wave"

    session.flush()

    return wave


def remove_order_from_wave(
    session: Session,
    wave: PickWave,
    order: SalesOrder,
) -> None:
    """Remove a single order from an active wave; it becomes pickable again.

    Equivalent in effect to cancelling the whole wave, scoped to one order.
    Allocations are untouched: they were reserved at order-approval time,
    independent of wave membership.
    """
    if wave.status != "active":
        raise PickWaveSelectionError("Only an active pick wave can have an order removed.")

    membership = (
        session.query(PickWaveOrder)
        .filter(
            PickWaveOrder.wave_id == wave.id,
            PickWaveOrder.order_id == order.id,
            PickWaveOrder.status == "active",
        )
        .first()
    )

    if not membership:
        raise PickWaveSelectionError("That order is not an active member of this wave.")

    membership.status = "removed"

    if order.status == "in_pick_wave":
        order.status = "ready_to_pick"


def get_wave_orders(
    session: Session,
    wave_id: int,
    *,
    active_only: bool = True,
):
    """Orders currently associated with a wave.

    ``active_only=True`` (the default) returns only orders whose
    membership is still ``active`` -- used by the completion/cancellation
    transitions themselves, which only ever run against an active wave
    anyway. ``active_only=False`` returns every order that still belongs
    to the wave in any meaningful sense (``active`` or ``closed``,
    i.e. everything except a membership explicitly ``removed`` from the
    wave) -- for display/action surfaces like the wave detail page, which
    need to keep showing an order after the wave completes (memberships
    flip to ``closed`` on completion, not removed).
    """
    query = (
        session.query(SalesOrder)
        .join(
            PickWaveOrder,
            PickWaveOrder.order_id == SalesOrder.id,
        )
        .filter(
            PickWaveOrder.wave_id == wave_id
        )
    )

    if active_only:
        query = query.filter(PickWaveOrder.status == "active")
    else:
        query = query.filter(PickWaveOrder.status != "removed")

    return (
        query
        .order_by(
            SalesOrder.created_at,
            SalesOrder.id,
        )
        .all()
    )


# Memberships that still mean "this order belongs to this wave". "removed"
# is deliberately absent: remove_order_from_wave takes an order OUT of the
# wave and hands it back to the pool, and its allocations are left
# untouched, so a removed order is genuinely somebody else's to pick.
PICKLIST_MEMBERSHIP_STATUSES = ("active", "closed")

# ★ "exception" JOINED THE PICK LIST IN v2.25.0. Reporting a card moves its
# allocation straight to "exception", which used to drop the line off this
# list entirely -- so the moment the operator reported a card he could no
# longer see that he had. His own words: on a reopened pick list he must
# SEE everything, including which cards were reported. The line is rendered
# read-only with its submission and resolution state; ACTING on it is a
# separate question (there is currently no reachable path once an exception
# has been reported -- see the Attention-tab finding).
# ★ THE CAUSE OF THE DISAPPEARING MASTER PICK LIST (found 2026-10-09).
# order_service.mark_shipped sets allocation.status = "shipped", and
# "shipped" was not in this tuple -- so the moment a wave shipped, every
# one of its lines dropped out of this query and the Master Pick List went
# blank. The operator's words: "for picklists that have been shipped, it
# removes the picklist entries from the master list. I want to make sure
# that the master picklist is always viewable no matter the status of the
# pickwave."
#
# "exception" joined in v2.25.0 and "packed" in v2.26.0 for the same
# reason: a line that VANISHES is what sends him looking for the card. The
# list is now the wave's record for its whole life.
#
# ★ "released" IS DELIBERATELY ABSENT. release_order sets it when an ORDER
# is cancelled and its cards go back to stock, so the line is no longer any
# part of this wave's work -- and uncancel_order restores the allocation to
# its recorded released_from_status, at which point it reappears here on
# its own.
PICKLIST_ALLOCATION_STATUSES = (
    "allocated", "picked", "exception", "packed", "shipped",
)


def get_wave_picklist(
    session: Session,
    wave_id: int,
):
    """The wave's pick list, keyed on the WAVE rather than on live membership.

    ★ WHY THIS STOPPED KEYING ON ACTIVE MEMBERSHIP (v2.21.0). Completion
    closes every membership (_close_active_memberships), so this query
    went empty the moment the operator pressed Complete -- the pick list,
    and with it the Master Pick List print, vanished exactly when he was
    still working from it to pack. The orders section and the exception
    table never had this problem; they read get_wave_orders(active_only=
    False). Nothing about membership semantics changes here: whether a
    picked order may be re-waved is a separate, open question, and this
    function only reads.

    ★ EVERY WAVE STATUS HAS A PICK LIST (2026-10-09), including cancelled.
    Slice 1 returned {} for a cancelled wave, reasoning that a list would
    invite picking against an abandoned wave. The operator asked for the
    opposite and he is right: the list is the wave's RECORD, not only its
    worksheet, and a page that silently empties itself is the thing he has
    been complaining about. Picking is prevented by the page -- every
    action is gated on the wave being active -- not by hiding the
    evidence.

    A cancelled wave's lines are still "allocated" (cancel_pick_wave moves
    the ORDERS back to ready_to_pick and leaves allocations alone), so they
    show -- unless that order has since joined another wave, in which case
    the one-active-membership guard below withholds them and says so in the
    log. That is the correct outcome: whichever wave is actually picking
    the card is the only one that should list it as pickable.

    ★ A LINE IS NEVER PICKABLE ON TWO WAVES AT ONCE. An order removed from
    this wave keeps its allocations (see remove_order_from_wave) and goes
    straight back to ready_to_pick, so it can join another wave while its
    allocations still read "allocated". Showing those lines here as well
    is how one physical card gets picked twice. Two guards, because they
    fail differently: the membership filter excludes the ordinary removal,
    and the active-elsewhere exclusion below catches any other route into
    the same shape without having to enumerate them. Excluded rather than
    shown read-only -- a marked row still prints onto the Master Pick List
    and still invites a hand reaching for the card, and the order itself
    stays visible in the Orders section either way, so nothing disappears
    from the page. Withheld lines are logged rather than silently dropped.
    """
    wave = session.get(PickWave, wave_id)
    if wave is None:
        return {}

    active_elsewhere = {
        order_id for (order_id,) in session.query(PickWaveOrder.order_id).filter(
            PickWaveOrder.wave_id != wave_id,
            PickWaveOrder.status == "active",
        ).all()
    }

    rows = (
        session.query(
            PickAllocation,
            OrderItem,
            InventoryCard,
            Batch,
            SalesOrder,
        )
        .join(
            OrderItem,
            PickAllocation.order_item_id == OrderItem.id,
        )
        .join(
            SalesOrder,
            OrderItem.order_id == SalesOrder.id,
        )
        .join(
            PickWaveOrder,
            PickWaveOrder.order_id == SalesOrder.id,
        )
        .join(
            InventoryCard,
            PickAllocation.inventory_card_id == InventoryCard.id,
        )
        .join(
            Batch,
            PickAllocation.batch_id == Batch.id,
        )
        .filter(
            PickWaveOrder.wave_id == wave_id,
            PickWaveOrder.status.in_(PICKLIST_MEMBERSHIP_STATUSES),
            PickAllocation.status.in_(PICKLIST_ALLOCATION_STATUSES),
        )
        .order_by(
            Batch.batch_code,
            InventoryCard.name,
            InventoryCard.set_code,
            InventoryCard.collector_number,
            SalesOrder.id,
        )
        .all()
    )

    grouped = {}
    withheld = {}

    for allocation, item, card, batch, order in rows:
        if order.id in active_elsewhere:
            withheld[order.id] = withheld.get(order.id, 0) + 1
            continue
        grouped.setdefault(batch.batch_code, [])
        grouped[batch.batch_code].append(
            {
                "allocation": allocation,
                "item": item,
                "card": card,
                "order": order,
            }
        )

    if withheld:
        logger.warning(
            "pick wave %s: withheld %s pick-list line(s) across %s order(s) "
            "%s -- each is actively being picked on another wave, and a line "
            "pickable on two waves at once is how one physical card gets "
            "picked twice. The order(s) remain visible in this wave's Orders "
            "section.",
            wave_id, sum(withheld.values()), len(withheld),
            sorted(withheld),
        )

    return grouped


def mark_wave_picked(
    session: Session,
    wave: PickWave,
) -> list[SalesOrder]:
    """Picking is finished: the operator's "Mark Wave Picked" (was "Complete").

    Returns the orders this call actually moved to "picked" -- excludes any
    order blocked by an open fulfillment exception, which stays
    in_pick_wave. Callers that need to notify Mana Pool of the picked
    transition should use exactly this list, not full wave membership.
    Unchanged: this is still the only place the processing push's order list
    comes from, and this function still never contacts Mana Pool itself.

    ★ MEMBERSHIP NO LONGER CLOSES HERE (v2.22.0, operator decision
    2026-10-08). An order stays in its wave until it SHIPS; the only exits
    are Back to Picking and removing the order. Closing membership at
    picking was what let a picked order be pulled into another wave, and
    what made the pick list vanish. The close moved to
    mark_wave_shipped_if_complete, the genuinely terminal step.

    completed_at still records this moment -- see PickWave.completed_at for
    why that name is kept.
    """
    if wave.status != WAVE_STATUS_ACTIVE:
        return []

    orders = get_wave_orders(
        session,
        wave.id,
    )

    now = datetime.now()
    newly_picked = []

    for order in orders:
        allocations = (
            session.query(PickAllocation)
            .join(
                OrderItem,
                PickAllocation.order_item_id == OrderItem.id,
            )
            .filter(
                OrderItem.order_id == order.id,
                PickAllocation.status == "allocated",
            )
            .all()
        )

        for allocation in allocations:
            allocation.status = "picked"

        blocked = order_has_fulfillment_submission_block(session.query(
            FulfillmentException,
        ).join(
            OrderItem, FulfillmentException.order_item_id == OrderItem.id,
        ).filter(OrderItem.order_id == order.id).all())
        if order.status == "in_pick_wave" and not blocked:
            order.status = "picked"
            order.picked_at = now
            newly_picked.append(order)

    wave.status = WAVE_STATUS_PICKED
    wave.completed_at = now

    return newly_picked


def mark_wave_packed(session: Session, wave: PickWave) -> None:
    """Move the WAVE to packed once its orders have been packed.

    Deliberately only the wave's own row: the per-order pack transition is
    main._pack_orders, which is batch-isolated so one refusing order cannot
    roll back the rest, and is shared with the /orders bulk-pack. This runs
    after it and records what the wave as a whole now is.

    Idempotent and silent on a wave that is not at picked -- the caller is
    a bulk action whose own result page already explains per-order
    outcomes, and raising here would turn "some orders were already packed"
    into a failed request.
    """
    if wave.status not in (WAVE_STATUS_PICKED, WAVE_STATUS_COMPLETED_LEGACY):
        return
    wave.status = WAVE_STATUS_PACKED
    wave.packed_at = datetime.now()


def orders_that_can_ship(session: Session, orders) -> list[SalesOrder]:
    """The orders whose shipping this wave is genuinely waiting on.

    A cancelled order is never going to ship, and an order whose every line
    is at "exception" has no card to put in a box
    (order_service.order_has_nothing_to_ship, found live on order 4138).
    Neither should hold a wave open forever -- operator decision 2026-10-08.
    """
    from order_service import order_has_nothing_to_ship

    return [
        order for order in orders
        if order.status != "cancelled"
        and not order_has_nothing_to_ship(session, order)
    ]


def mark_wave_shipped_if_complete(session: Session, wave: PickWave) -> bool:
    """Close the wave once every order that CAN ship has shipped.

    ★ THIS IS WHERE MEMBERSHIP CLOSES (v2.22.0), because this is the first
    genuinely terminal step: shipping sells the cards, applies consignment
    payout and tells Mana Pool. Up to here the orders still belong to the
    wave and Back to Picking is still available; past here neither is true.

    Returns whether it moved the wave. Never raises: it runs at the tail of
    a bulk ship whose own result page reports per-order outcomes, and a
    wave that simply is not finished yet is the ordinary case, not an
    error.
    """
    if wave.status in WAVE_STATUSES_TERMINAL:
        return False
    orders = get_wave_orders(session, wave.id, active_only=False)
    shippable = orders_that_can_ship(session, orders)
    if not shippable or any(order.status != "shipped" for order in shippable):
        return False

    wave.status = WAVE_STATUS_SHIPPED
    wave.shipped_at = datetime.now()
    _close_active_memberships(session, wave.id)
    logger.info(
        "pick wave %s shipped: %s of %s order(s) shipped, %s could not ship "
        "(cancelled or nothing to ship); membership closed.",
        wave.id, len(shippable), len(orders), len(orders) - len(shippable),
    )
    return True


def cancel_pick_wave(
    session: Session,
    wave: PickWave,
):
    if wave.status != WAVE_STATUS_ACTIVE:
        return

    orders = get_wave_orders(
        session,
        wave.id,
    )

    for order in orders:
        if order.status == "in_pick_wave":
            order.status = "ready_to_pick"

    _close_active_memberships(session, wave.id)
    wave.status = WAVE_STATUS_CANCELLED


def _close_active_memberships(session: Session, wave_id: int) -> None:
    """Release active membership once a wave becomes terminal.

    This clears the way for the wave's orders to join a future wave without
    tripping the DB-level one-active-wave-per-order constraint.
    """
    memberships = (
        session.query(PickWaveOrder)
        .filter(
            PickWaveOrder.wave_id == wave_id,
            PickWaveOrder.status == "active",
        )
        .all()
    )
    for membership in memberships:
        membership.status = "closed"


def reopen_pick_wave(
    session: Session,
    wave: PickWave,
    note: str | None = None,
) -> list[SalesOrder]:
    """Reverse a completed wave back to active, all-or-nothing.

    Only succeeds if every order in the wave is still exactly where
    completion left it (still `picked`, or still `in_pick_wave` if
    completion itself left it blocked on an open fulfillment exception)
    -- no packing, shipment, or OPERATOR fulfillment-exception resolution
    since. If even one order has moved further, the whole reopen fails
    closed and nothing changes; the caller gets the offending order(s)
    named.

    An exception closed automatically by its own submission
    (CF-AUTORESOLVE-001) does not count as having progressed -- see the
    guard below for why.

    **A reopen does NOT rewind such an exception's resolution**, which is
    a deliberate choice rather than an omission:

    * The report to Mana Pool genuinely happened. Reopening a local wave
      cannot un-send it, and `submission_state` stays "submitted" either
      way, so rewinding only the inventory flag would make the record
      claim something untrue about itself.
    * Reopen already rewinds nothing else about an exception. The row
      stays, the allocation stays "exception", and the card keeps the
      disposition it was given when the exception was RAISED (removed for
      a missing card, unsellable for a mismatch) -- resolution never set
      that and so has nothing to give back. Rewinding one flag out of
      that set would be a partial undo of a thing that was never undone.
    * It would recreate "submitted + unresolved", the precise state
      CF-AUTORESOLVE-001 made structurally unreachable. Nothing else in
      the app can produce it, and the "Submitted to ManaPool" button is
      gated on `needs_submission`, so nothing could close it again --
      reopen would quietly become a machine for stranding exceptions,
      which is the bug this whole line of work exists to remove.

    So the wave goes back to picking while the exception stays closed.
    Both facts are true at once: the card was reported, and the wave is
    being redone. The exception records the first; the wave records the
    second.

    Local-only: this never contacts Mana Pool. It cannot retract the
    `processing` push already sent when the wave completed -- that push
    has no corresponding "undo" on Mana Pool's side. The caller must
    surface that to the operator; this function only records it in the
    immutable event evidence.

    Returns the orders this call actually moved back to `in_pick_wave`.
    """
    if wave.status not in WAVE_STATUSES_IN_FLIGHT:
        raise PickWaveSelectionError(
            "Only a wave that has been picked and has not shipped can go "
            "back to picking."
        )

    # ★ "active" JOINED "closed" HERE IN v2.22.0, and both are needed. The
    # picked transition no longer closes membership, so a wave reopened
    # today has ACTIVE memberships; every wave picked before v2.22.0 --
    # including every stored "completed" one -- has CLOSED ones. Filtering
    # on either alone would make this raise "no orders to reopen" for half
    # the waves in the database.
    memberships = (
        session.query(PickWaveOrder)
        .filter(
            PickWaveOrder.wave_id == wave.id,
            PickWaveOrder.status.in_(("active", "closed")),
        )
        .all()
    )
    if not memberships:
        raise PickWaveSelectionError("This wave has no orders to reopen.")

    orders_by_id = {
        order.id: order
        for order in session.query(SalesOrder).filter(
            SalesOrder.id.in_([membership.order_id for membership in memberships])
        ).all()
    }

    # ★ A PACKED ORDER NO LONGER BLOCKS THIS (v2.26.0, operator decision
    # 2026-10-08): "orders already packed stay packed". Packing is purely
    # local and reversible, and refusing the whole wave because one box is
    # already taped shut is what made this undo unavailable exactly when he
    # needed it. SHIPPED and CANCELLED still fail closed, and they are
    # different in kind: shipping sold the cards, applied consignment
    # payout and told Mana Pool, and a cancelled order has its own audit
    # trail -- neither is something a local wave reopen may quietly
    # contradict.
    blocked = []
    for membership in memberships:
        order = orders_by_id.get(membership.order_id)
        if order is None:
            blocked.append(f"#{membership.order_id} (not found)")
        elif order.status not in REOPENABLE_ORDER_STATUSES:
            display = order.external_label or order.external_order_id
            blocked.append(
                f"{display} (now {order.status!r}; a wave cannot go back to "
                "picking once an order has shipped or been cancelled)"
            )

    touched_exceptions = session.query(FulfillmentException).join(
        OrderItem, FulfillmentException.order_item_id == OrderItem.id,
    ).filter(
        OrderItem.order_id.in_(orders_by_id.keys()),
    ).all()
    # CF-AUTORESOLVE-002 (2026-09-21). "Resolved" stopped being a reliable
    # signal that a human decided anything: since CF-AUTORESOLVE-001,
    # reporting an exception to Mana Pool closes its inventory record
    # automatically, so this guard silently turned submission into a
    # one-way door for the whole wave -- mark one card missing, report
    # it, and the wave could never be undone again. The operator's
    # standing universal-undo principle is that there should be no risk
    # in undoing something you did yourself, so an auto-close must not
    # foreclose the undo.
    #
    # An operator-resolved exception STILL fails closed: someone made a
    # decision about that card (a printing correction, accepting it as
    # permanently absent) and reopening the wave would strand it. Only
    # the automatic close is discounted, identified by its own event
    # type rather than by re-deriving the rule here.
    auto_resolved = auto_resolved_on_submission_ids(
        session, (exception.id for exception in touched_exceptions),
    )
    for exception in touched_exceptions:
        operator_resolved = (
            exception.inventory_resolution_state == "resolved"
            and exception.id not in auto_resolved
        )
        if operator_resolved or exception.remote_resolution_state != "awaiting":
            order = orders_by_id.get(exception.sales_order_id)
            display = (
                order.external_label or order.external_order_id
                if order else f"order #{exception.sales_order_id}"
            )
            blocked.append(
                f"{display} (fulfillment exception #{exception.id} has "
                f"progressed: remote={exception.remote_resolution_state!r}, "
                f"inventory={exception.inventory_resolution_state!r})"
            )

    if blocked:
        raise PickWaveSelectionError(
            "Cannot reopen -- orders have moved past picked: " + "; ".join(blocked)
        )

    reverted = []
    left_packed = []
    for membership in memberships:
        order = orders_by_id[membership.order_id]
        membership.status = "active"
        if order.status == "picked":
            order.status = "in_pick_wave"
            order.picked_at = None
            reverted.append(order)
        elif order.status == "packed":
            # Untouched on purpose. Recorded rather than merely skipped:
            # the event is the only place that says which orders this undo
            # did and did not move, and a silently-skipped order looks
            # identical to one that was never in the wave.
            left_packed.append(order)

    previous_status = wave.status
    wave.status = WAVE_STATUS_ACTIVE
    wave.completed_at = None
    wave.packed_at = None

    timestamp = datetime.now()
    evidence = {
        "reverted_order_ids": [order.id for order in reverted],
        "left_packed_order_ids": [order.id for order in left_packed],
        "all_member_order_ids": sorted(orders_by_id.keys()),
        "previous_wave_status": previous_status,
        "mana_pool_note": REOPEN_MANA_POOL_NOTE,
        "timestamp": timestamp.isoformat(),
    }
    session.add(PickWaveEvent(
        actor=current_actor(),
        pick_wave_id=wave.id,
        event_type="reopened",
        note=str(note or "").strip() or "Pick wave reopened.",
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        created_at=timestamp,
    ))

    return reverted
