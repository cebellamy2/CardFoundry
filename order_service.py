from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import (
    Batch,
    InventoryCard,
    OrderItem,
    PickAllocation,
    SalesOrder,
)


def parse_order_lines(text: str):
    parsed_items = []
    errors = []

    for line_number, raw_line in enumerate(
        text.splitlines(),
        start=1,
    ):
        line = raw_line.strip()

        if not line:
            continue

        parts = [
            part.strip()
            for part in line.split("|")
        ]

        if len(parts) != 5:
            errors.append(
                f"Line {line_number}: "
                "expected 5 fields separated by |"
            )
            continue

        (
            name,
            set_code,
            collector_number,
            finish,
            quantity_text,
        ) = parts

        try:
            quantity = int(quantity_text)

        except ValueError:
            errors.append(
                f"Line {line_number}: "
                "quantity must be a number."
            )
            continue

        if quantity < 1:
            errors.append(
                f"Line {line_number}: "
                "quantity must be at least 1."
            )
            continue

        parsed_items.append(
            {
                "name": name,
                "set_code": set_code,
                "collector_number": collector_number,
                "finish": finish,
                "quantity": quantity,
            }
        )

    return parsed_items, errors


def allocate_order(
    session: Session,
    order: SalesOrder,
):
    has_shortage = False

    items = (
        session.query(OrderItem)
        .filter(
            OrderItem.order_id == order.id
        )
        .order_by(OrderItem.id)
        .all()
    )

    for item in items:

        query = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status
                == "available"
            )
        )

        # Mana Pool gives us Scryfall ID.
        # This is our preferred exact-printing match.
        if item.scryfall_id:

            query = query.filter(
                InventoryCard.scryfall_id
                == item.scryfall_id
            )

        else:

            query = query.filter(
                func.lower(InventoryCard.name)
                == item.name.lower()
            )

            if item.set_code:
                query = query.filter(
                    func.lower(
                        InventoryCard.set_code
                    )
                    == item.set_code.lower()
                )

            if item.collector_number:
                query = query.filter(
                    func.lower(
                        InventoryCard.collector_number
                    )
                    == item.collector_number.lower()
                )

        if item.finish:
            query = query.filter(
                func.lower(
                    InventoryCard.finish
                )
                == item.finish.lower()
            )

        cards = (
            query
            .order_by(
                InventoryCard.imported_at,
                InventoryCard.id,
            )
            .limit(item.quantity)
            .all()
        )

        if len(cards) < item.quantity:
            has_shortage = True

        for card in cards:

            session.add(
                PickAllocation(
                    order_item_id=item.id,
                    inventory_card_id=card.id,
                    batch_id=card.batch_id,
                    status="allocated",
                )
            )

            card.status = "reserved"

    order.status = (
        "short"
        if has_shortage
        else "ready_to_pick"
    )


def release_order(
    session: Session,
    order: SalesOrder,
):
    allocations = (
        session.query(PickAllocation)
        .join(
            OrderItem,
            PickAllocation.order_item_id
            == OrderItem.id,
        )
        .filter(
            OrderItem.order_id == order.id,
            PickAllocation.status.in_(
                [
                    "allocated",
                    "picked",
                    "packed",
                ]
            ),
        )
        .all()
    )

    for allocation in allocations:

        card = session.get(
            InventoryCard,
            allocation.inventory_card_id,
        )

        if (
            card
            and card.status == "reserved"
        ):
            card.status = "available"

        allocation.status = "released"

    order.status = "cancelled"


def mark_picked(
    session: Session,
    order: SalesOrder,
):
    allocations = _active_allocations(
        session,
        order.id,
    )

    for allocation in allocations:
        allocation.status = "picked"

    order.status = "picked"
    order.picked_at = datetime.now()


def mark_packed(
    session: Session,
    order: SalesOrder,
):
    allocations = _active_allocations(
        session,
        order.id,
    )

    for allocation in allocations:
        allocation.status = "packed"

    order.status = "packed"
    order.packed_at = datetime.now()


def mark_shipped(
    session: Session,
    order: SalesOrder,
    tracking_number: str | None,
):
    allocations = _active_allocations(
        session,
        order.id,
    )

    for allocation in allocations:

        card = session.get(
            InventoryCard,
            allocation.inventory_card_id,
        )

        if card:
            card.status = "sold"

        allocation.status = "shipped"

    order.status = "shipped"
    order.shipped_at = datetime.now()

    cleaned_tracking = (
        tracking_number.strip()
        if tracking_number
        else ""
    )

    order.tracking_number = (
        cleaned_tracking or None
    )


def _active_allocations(
    session: Session,
    order_id: int,
):
    return (
        session.query(PickAllocation)
        .join(
            OrderItem,
            PickAllocation.order_item_id
            == OrderItem.id,
        )
        .filter(
            OrderItem.order_id == order_id,
            PickAllocation.status.in_(
                [
                    "allocated",
                    "picked",
                    "packed",
                ]
            ),
        )
        .all()
    )


def get_picklist(
    session: Session,
    order_id: int,
):
    allocations = (
        session.query(
            PickAllocation,
            OrderItem,
            InventoryCard,
            Batch,
        )
        .join(
            OrderItem,
            PickAllocation.order_item_id
            == OrderItem.id,
        )
        .join(
            InventoryCard,
            PickAllocation.inventory_card_id
            == InventoryCard.id,
        )
        .join(
            Batch,
            PickAllocation.batch_id
            == Batch.id,
        )
        .filter(
            OrderItem.order_id == order_id,
            PickAllocation.status.in_(
                [
                    "allocated",
                    "picked",
                    "packed",
                    "shipped",
                ]
            ),
        )
        .order_by(
            Batch.batch_code,
            InventoryCard.name,
            InventoryCard.set_code,
            InventoryCard.collector_number,
        )
        .all()
    )

    grouped = {}

    for (
        allocation,
        item,
        card,
        batch,
    ) in allocations:

        grouped.setdefault(
            batch.batch_code,
            [],
        )

        grouped[
            batch.batch_code
        ].append(
            {
                "allocation": allocation,
                "item": item,
                "card": card,
            }
        )

    return grouped