import json
from datetime import datetime

from sqlalchemy.orm import Session

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
from models import FulfillmentException


REOPEN_MANA_POOL_NOTE = (
    "Mana Pool has already been told these orders are processing -- "
    "reopening this wave does not undo that."
)


ELIGIBLE_ORDER_STATUS = "ready_to_pick"


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


def get_wave_picklist(
    session: Session,
    wave_id: int,
):
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
            PickWaveOrder.status == "active",
            PickAllocation.status.in_(["allocated", "picked"]),
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

    for allocation, item, card, batch, order in rows:
        grouped.setdefault(batch.batch_code, [])
        grouped[batch.batch_code].append(
            {
                "allocation": allocation,
                "item": item,
                "card": card,
                "order": order,
            }
        )

    return grouped


def complete_pick_wave(
    session: Session,
    wave: PickWave,
) -> list[SalesOrder]:
    """Returns the orders this call actually moved to "picked" -- excludes
    any order blocked by an open fulfillment exception, which stays
    in_pick_wave. Callers that need to notify Mana Pool of the picked
    transition should use exactly this list, not full wave membership.
    """
    if wave.status != "active":
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

    _close_active_memberships(session, wave.id)
    wave.status = "completed"
    wave.completed_at = now

    return newly_picked


def cancel_pick_wave(
    session: Session,
    wave: PickWave,
):
    if wave.status != "active":
        return

    orders = get_wave_orders(
        session,
        wave.id,
    )

    for order in orders:
        if order.status == "in_pick_wave":
            order.status = "ready_to_pick"

    _close_active_memberships(session, wave.id)
    wave.status = "cancelled"


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
    -- no packing, shipment, or fulfillment-exception resolution since.
    If even one order has moved further, the whole reopen fails closed
    and nothing changes; the caller gets the offending order(s) named.

    Local-only: this never contacts Mana Pool. It cannot retract the
    `processing` push already sent when the wave completed -- that push
    has no corresponding "undo" on Mana Pool's side. The caller must
    surface that to the operator; this function only records it in the
    immutable event evidence.

    Returns the orders this call actually moved back to `in_pick_wave`.
    """
    if wave.status != "completed":
        raise PickWaveSelectionError("Only a completed pick wave can be reopened.")

    memberships = (
        session.query(PickWaveOrder)
        .filter(
            PickWaveOrder.wave_id == wave.id,
            PickWaveOrder.status == "closed",
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

    blocked = []
    for membership in memberships:
        order = orders_by_id.get(membership.order_id)
        if order is None:
            blocked.append(f"#{membership.order_id} (not found)")
        elif order.status not in ("picked", "in_pick_wave"):
            display = order.external_label or order.external_order_id
            blocked.append(f"{display} (now {order.status!r}, not picked)")

    touched_exceptions = session.query(FulfillmentException).join(
        OrderItem, FulfillmentException.order_item_id == OrderItem.id,
    ).filter(
        OrderItem.order_id.in_(orders_by_id.keys()),
    ).all()
    for exception in touched_exceptions:
        if (
            exception.inventory_resolution_state == "resolved"
            or exception.remote_resolution_state != "awaiting"
        ):
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
    for membership in memberships:
        order = orders_by_id[membership.order_id]
        membership.status = "active"
        if order.status == "picked":
            order.status = "in_pick_wave"
            order.picked_at = None
            reverted.append(order)

    wave.status = "active"
    wave.completed_at = None

    timestamp = datetime.now()
    evidence = {
        "reverted_order_ids": [order.id for order in reverted],
        "all_member_order_ids": sorted(orders_by_id.keys()),
        "previous_wave_status": "completed",
        "mana_pool_note": REOPEN_MANA_POOL_NOTE,
        "timestamp": timestamp.isoformat(),
    }
    session.add(PickWaveEvent(
        pick_wave_id=wave.id,
        event_type="reopened",
        note=str(note or "").strip() or "Pick wave reopened.",
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        created_at=timestamp,
    ))

    return reverted
