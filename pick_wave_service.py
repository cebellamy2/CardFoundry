from datetime import datetime

from sqlalchemy.orm import Session

from models import (
    Batch,
    InventoryCard,
    OrderItem,
    PickAllocation,
    PickWave,
    PickWaveOrder,
    SalesOrder,
)
from fulfillment_exception_invariants import order_has_fulfillment_submission_block
from models import FulfillmentException


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
