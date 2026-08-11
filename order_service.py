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
    """
    Expected format:

    Card Name | SET | Collector Number | Finish | Quantity

    Example:
    Sol Ring | CMM | 396 | normal | 1
    """

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
                f"Line {line_number}: expected 5 fields separated by |"
            )
            continue

        name, set_code, collector_number, finish, quantity_text = parts

        if not name:
            errors.append(
                f"Line {line_number}: card name is missing."
            )
            continue

        try:
            quantity = int(quantity_text)
        except ValueError:
            errors.append(
                f"Line {line_number}: quantity must be a number."
            )
            continue

        if quantity < 1:
            errors.append(
                f"Line {line_number}: quantity must be at least 1."
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
    """
    Allocate physical inventory to every order item.

    Oldest inventory record wins, which gradually empties
    older batches before newer ones.
    """

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
                InventoryCard.status == "available",
                func.lower(InventoryCard.name)
                == item.name.lower(),
            )
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

        available_cards = (
            query
            .order_by(
                InventoryCard.imported_at,
                InventoryCard.id,
            )
            .limit(item.quantity)
            .all()
        )

        if len(available_cards) < item.quantity:
            has_shortage = True

        for card in available_cards:
            allocation = PickAllocation(
                order_item_id=item.id,
                inventory_card_id=card.id,
                batch_id=card.batch_id,
                status="allocated",
            )

            session.add(allocation)

            card.status = "reserved"

    if has_shortage:
        order.status = "short"
    else:
        order.status = "ready_to_pick"


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
            PickAllocation.status == "allocated",
        )
        .all()
    )

    for allocation in allocations:
        card = session.get(
            InventoryCard,
            allocation.inventory_card_id,
        )

        if card and card.status == "reserved":
            card.status = "available"

        allocation.status = "released"

    order.status = "cancelled"


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
            PickAllocation.status == "allocated",
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

    for allocation, item, card, batch in allocations:
        grouped.setdefault(
            batch.batch_code,
            [],
        )

        grouped[batch.batch_code].append(
            {
                "allocation": allocation,
                "item": item,
                "card": card,
            }
        )

    return grouped