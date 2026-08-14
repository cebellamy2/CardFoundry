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


def create_pick_wave(
    session: Session,
    label: str | None = None,
) -> PickWave | None:
    ready_orders = (
        session.query(SalesOrder)
        .filter(
            SalesOrder.status == "ready_to_pick"
        )
        .order_by(
            SalesOrder.created_at,
            SalesOrder.id,
        )
        .all()
    )

    if not ready_orders:
        return None

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

    for order in ready_orders:
        active_membership = (
            session.query(PickWaveOrder)
            .join(
                PickWave,
                PickWaveOrder.wave_id == PickWave.id,
            )
            .filter(
                PickWaveOrder.order_id == order.id,
                PickWave.status == "active",
            )
            .first()
        )

        if active_membership:
            continue

        session.add(
            PickWaveOrder(
                wave_id=wave.id,
                order_id=order.id,
            )
        )

        order.status = "in_pick_wave"

    session.flush()

    membership_count = (
        session.query(PickWaveOrder)
        .filter(
            PickWaveOrder.wave_id == wave.id
        )
        .count()
    )

    if membership_count == 0:
        session.delete(wave)
        session.flush()
        return None

    return wave


def get_wave_orders(
    session: Session,
    wave_id: int,
):
    return (
        session.query(SalesOrder)
        .join(
            PickWaveOrder,
            PickWaveOrder.order_id == SalesOrder.id,
        )
        .filter(
            PickWaveOrder.wave_id == wave_id
        )
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
):
    if wave.status != "active":
        return

    orders = get_wave_orders(
        session,
        wave.id,
    )

    now = datetime.now()

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

    wave.status = "completed"
    wave.completed_at = now


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

    wave.status = "cancelled"
