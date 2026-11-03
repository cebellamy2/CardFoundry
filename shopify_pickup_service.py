"""Shopify pickup: reserved -> ready for pickup -> picked up.

★ WHAT A PICKUP ORDER IS (operator). Phase 1 Shopify is pickup only,
paid at pickup: the order is a reservation the customer collects and
pays for in person. No money moves through Shopify, and no automated
customer email is ever sent -- not on arrival, not when it is ready, not
when it is collected. A human decides what the customer is told.

★ THE STATES, AND WHERE EACH ONE COMES FROM:

    reserved            allocate_order, the same as every other order
    ready_for_pickup    wave completion, for a Shopify order only
    picked_up           the operator pressing "Mark picked up"

★ NO HOLD EVER EXPIRES ON ITS OWN (operator). An uncollected order sits
in ready_for_pickup indefinitely; after seven days it appears on the
Attention page, and the operator returns the cards to inventory BY HAND.
Nothing in this module reclaims stock on a timer, because a timer would
eventually sell a card out from under a customer who turned up on day
eight.

★ NO CHECK CONSTRAINT WAS WIDENED TO ADD THESE STATES, because
sales_orders.status has none -- verified against the live production
schema, where it is a plain `status VARCHAR NOT NULL`. The vocabulary
lives in code. Adding a status is adding a string and teaching the code
that reads it.

★ A PICKUP ORDER NEVER TOUCHES MANA POOL'S SHIPMENT PATH. It is not
shipped, there is no tracking, and the Mana Pool processing and shipment
pushes are already scoped to source == "manapool" -- which this module
relies on rather than re-checking, except where a route needs to refuse
an action outright.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from actor_context import current_actor
from consignment_service import apply_consignment_payout_if_consigned
from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES
from models import (
    InventoryCard, InventoryChangeLog, OrderItem, PickAllocation, SalesOrder,
)
from shopify_order_service import SOURCE

logger = logging.getLogger("cardfoundry")

STATUS_READY_FOR_PICKUP = "ready_for_pickup"
STATUS_PICKED_UP = "picked_up"

# The states a pickup order can be in that are NOT finished.
OPEN_PICKUP_STATUSES = (STATUS_READY_FOR_PICKUP,)

# The states whose order page offers a pickup action. Exported so the
# route's status branch and this module cannot disagree about which
# pages get a route forward.
PICKUP_ACTION_STATUSES = (STATUS_READY_FOR_PICKUP, STATUS_PICKED_UP)

# How long an uncollected order waits before the Attention page mentions
# it (operator). Not an expiry -- nothing happens at seven days except
# that the operator is told.
UNCOLLECTED_ATTENTION_DAYS = 7


# ★ THE SHARED DEFINITION of an allocation that still holds a card,
# from inventory_mirror_service, rather than a local list. A local
# ("allocated", "picked") would have silently dropped a packed
# allocation, which is exactly how two callers start disagreeing about
# what an order is holding.
_HELD_ALLOCATION_STATUSES = tuple(ACTIVE_ALLOCATION_STATUSES)


class PickupTransitionError(RuntimeError):
    """A pickup transition that was refused. Nothing was written."""


def is_pickup_order(order) -> bool:
    """Every Shopify order is a pickup order in phase 1.

    One predicate, so the day a Shopify order can be shipped there is
    exactly one place that learns the difference.
    """
    return getattr(order, "source", None) == SOURCE


def picked_status_for(order) -> str | None:
    """The status wave completion should leave this order in, or None to
    use the ordinary one.

    ★ CALLED FROM mark_wave_picked so there is ONE definition of "a
    picked Shopify order is ready for collection, not picked". A pickup
    order reaching `picked` would sit in the state the Mana Pool
    processing push fires from and the shipped flow expects.
    """
    return STATUS_READY_FOR_PICKUP if is_pickup_order(order) else None


def _cards_for(session: Session, order: SalesOrder) -> list[InventoryCard]:
    """Every card this order currently holds, through its allocations."""
    rows = (
        session.query(InventoryCard)
        .join(PickAllocation,
              PickAllocation.inventory_card_id == InventoryCard.id)
        .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
        .filter(OrderItem.order_id == order.id,
                PickAllocation.status.in_(_HELD_ALLOCATION_STATUSES))
        .order_by(InventoryCard.id)
        .all()
    )
    return rows


def _audit(session: Session, card: InventoryCard, summary: str) -> None:
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        changed_at=datetime.now(),
        change_summary=summary,
        actor=current_actor(),
    ))


def mark_picked_up(session: Session, order: SalesOrder, *,
                   note: str | None = None) -> dict:
    """The customer collected and paid. Cards sold, payout resolved.

    ★ THE PAYOUT SKIPS THE SHIPPING DEDUCTION (operator, 2026-11-02). A
    collected card incurs no shipping, so the top band's flat
    pass-through is not applied -- the percentage is unchanged. See
    consignment_service.resolve_consignment_payout; this is the only
    caller that passes pickup=True.

    Audited per card and undoable: see undo_picked_up. Caller commits.
    """
    if not is_pickup_order(order):
        raise PickupTransitionError(
            "Only a Shopify pickup order can be marked picked up.")
    if order.status != STATUS_READY_FOR_PICKUP:
        raise PickupTransitionError(
            f"This order is {order.status!r}, not {STATUS_READY_FOR_PICKUP!r}, "
            f"so it cannot be marked picked up.")
    cards = _cards_for(session, order)
    if not cards:
        raise PickupTransitionError(
            "This order holds no cards, so there is nothing to collect.")

    items_by_id = {item.id: item for item in
                   session.query(OrderItem)
                   .filter(OrderItem.order_id == order.id).all()}
    allocations = (session.query(PickAllocation)
                   .join(OrderItem,
                         PickAllocation.order_item_id == OrderItem.id)
                   .filter(OrderItem.order_id == order.id,
                           PickAllocation.status.in_(
                               _HELD_ALLOCATION_STATUSES))
                   .all())
    price_by_card = {}
    for allocation in allocations:
        item = items_by_id.get(allocation.order_item_id)
        if item is not None and item.price_cents is not None:
            price_by_card[allocation.inventory_card_id] = item.price_cents / 100

    now = datetime.now()
    sold = []
    for card in cards:
        before = card.status
        card.status = "sold"
        card.disposed_at = now
        price = price_by_card.get(card.id)
        if price is not None:
            card.sold_price = price
        # ★ THE ONE pickup=True CALLER.
        apply_consignment_payout_if_consigned(session, card, pickup=True)
        _audit(session, card,
               f"status: {before} -> sold; collected in person on Shopify "
               f"order {order.external_label or order.external_order_id}"
               + (f"; payout resolved to ${card.consignment_amount_owed:.2f} "
                  f"with no shipping deduction (pickup)"
                  if card.consignment_amount_owed is not None else ""))
        sold.append(card)
    # ★ "shipped" ON A PICKUP ALLOCATION IS DELIBERATE. A new terminal
    # status ("collected") would read better and would be a third
    # vocabulary for the same fact: "shipped" is the one terminal
    # allocation status every reader in the app already understands, and
    # undo_picked_up reads it back. The ORDER carries the distinction
    # that matters, in its own status and picked_up_at.
    #
    # ★ AND THE SALE DATE DOES NOT COME FROM HERE. The shipped-
    # allocation lookup reads SalesOrder.shipped_at, which is NULL for
    # a collected order and is MEANT to be -- nothing shipped. The date
    # comes from consignment_service.local_sale_date instead, off the
    # card's own disposed_at and sold_price, which is exactly the
    # across-the-counter case v2.57.0 built it for. Both are set above;
    # disposition_type is deliberately left unset, because a pickup is
    # an ordinary sale and not one of the non-sale dispositions.
    for allocation in allocations:
        allocation.status = "shipped"

    order.status = STATUS_PICKED_UP
    order.picked_up_at = now
    order.picked_up_note = (str(note).strip() or None) if note else None
    session.flush()
    # ★ THE SAME LOWERING PUSH mark_shipped makes, for the same reason.
    # A collected card is SOLD, which is the strongest lowering event
    # there is. The reservation already pushed, so this is normally a
    # no-op -- but if the push was off or failed then, this is the
    # second chance, and push_lower_only can only ever reduce a
    # quantity, so calling it before the caller commits is safe.
    from order_service import _push_shopify_publishability

    _push_shopify_publishability(session, sold, origin="shopify-picked-up")
    logger.warning(
        "Shopify order %s marked PICKED UP: %s card(s) sold, collected in "
        "person.", order.external_label or order.external_order_id,
        len(sold),
    )
    return {"order_id": order.id, "cards": [c.id for c in sold],
            "picked_up_at": now}


def undo_picked_up(session: Session, order: SalesOrder) -> dict:
    """Put a picked-up order back to ready_for_pickup. Audited.

    ★ REFUSED ONCE ANY PAYOUT IS PAID. Reversing a sale whose consignor
    has already been paid would make the money and the record disagree,
    and this app has no way to un-pay someone. The operator corrects
    that through the payout pages, which are built for it.

    The cards go back to `reserved`, not `available`: the customer's
    hold still exists, which is the state the order is returning to.
    """
    if order.status != STATUS_PICKED_UP:
        raise PickupTransitionError(
            f"This order is {order.status!r}, not {STATUS_PICKED_UP!r}.")
    allocations = (session.query(PickAllocation)
                   .join(OrderItem,
                         PickAllocation.order_item_id == OrderItem.id)
                   .filter(OrderItem.order_id == order.id,
                           PickAllocation.status == "shipped")
                   .all())
    card_ids = [a.inventory_card_id for a in allocations]
    cards = (session.query(InventoryCard)
             .filter(InventoryCard.id.in_(card_ids)).all()) if card_ids else []
    paid = [c for c in cards if c.consignment_payout_status == "paid"]
    if paid:
        raise PickupTransitionError(
            f"{len(paid)} card(s) on this order have already been paid out "
            f"to their consignor, so the collection cannot be undone here. "
            f"Correct it from the payout pages.")

    now = datetime.now()
    for card in cards:
        before = card.status
        card.status = "reserved"
        card.disposed_at = None
        card.sold_price = None
        card.consignment_amount_owed = None
        card.consignment_payout_status = None
        _audit(session, card,
               f"status: {before} -> reserved; the collection of Shopify "
               f"order {order.external_label or order.external_order_id} was "
               f"undone, and any resolved payout was cleared")
    for allocation in allocations:
        allocation.status = "picked"
    order.status = STATUS_READY_FOR_PICKUP
    order.picked_up_at = None
    order.picked_up_note = None
    session.flush()
    logger.warning(
        "Shopify order %s: collection UNDONE, %s card(s) back to reserved.",
        order.external_label or order.external_order_id, len(cards),
    )
    return {"order_id": order.id, "cards": [c.id for c in cards],
            "undone_at": now}


def return_to_inventory(session: Session, order: SalesOrder, *,
                        reason: str = "pickup never collected") -> dict:
    """The customer never came. Release the hold, BY HAND (operator).

    ★ THROUGH THE CANONICAL CANCELLATION, release_order -- the same
    function the manual route, the Mana Pool reconciliation and the
    Shopify cancellation webhook all use. A second release path would be
    a second answer to what a release puts back and what it records.
    """
    if not is_pickup_order(order):
        raise PickupTransitionError(
            "Only a Shopify pickup order is returned to inventory this way.")
    if order.status not in OPEN_PICKUP_STATUSES:
        raise PickupTransitionError(
            f"This order is {order.status!r}; only an order waiting to be "
            f"collected can be returned to inventory.")
    from order_service import release_order

    release_order(session, order, reason=reason, initiated_by="operator")
    session.flush()
    logger.warning(
        "Shopify order %s returned to inventory by hand: %s",
        order.external_label or order.external_order_id, reason,
    )
    return {"order_id": order.id, "reason": reason}


def uncollected_orders(session: Session, *, days: int | None = None,
                       now: datetime | None = None) -> list[SalesOrder]:
    """Pickup orders waiting longer than `days` to be collected.

    Measured from when the order became ready for collection, which is
    `picked_at` -- wave completion stamps it, the same field the ordinary
    picked transition uses. An order with no stamp is included only once
    its placed_at is old enough, so a missing stamp cannot hide one
    forever.
    """
    cutoff = (now or datetime.now()) - timedelta(
        days=UNCOLLECTED_ATTENTION_DAYS if days is None else days)
    rows = (session.query(SalesOrder)
            .filter(SalesOrder.source == SOURCE,
                    SalesOrder.status == STATUS_READY_FOR_PICKUP)
            .order_by(SalesOrder.id)
            .all())
    waiting = []
    for order in rows:
        since = order.picked_at or order.placed_at or order.created_at
        if since is not None and since <= cutoff:
            waiting.append(order)
    return waiting
