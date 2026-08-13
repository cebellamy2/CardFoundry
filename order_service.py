from collections import Counter
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


ACTIVE_ALLOCATION_STATUSES = ("allocated", "picked", "packed")
KNOWN_INVENTORY_STATUSES = {"available", "reserved", "sold"}


class InventoryAllocationError(ValueError):
    pass


def _canonical_item_key(item: OrderItem):
    values = (
        item.mtgjson_id,
        item.language_id,
        item.condition_id,
        item.finish_id,
    )
    if not all(str(value or "").strip() for value in values):
        raise InventoryAllocationError(
            f"Order item {item.name!r} lacks exact canonical identity."
        )
    return tuple(str(value).strip().upper() for value in values)


def validate_inventory_invariants(session: Session):
    unknown = (
        session.query(InventoryCard.status)
        .filter(~InventoryCard.status.in_(KNOWN_INVENTORY_STATUSES))
        .distinct()
        .all()
    )
    if unknown:
        raise InventoryAllocationError(
            "Unknown inventory status: " + ", ".join(str(row[0]) for row in unknown)
        )
    inconsistent = (
        session.query(PickAllocation.id)
        .join(InventoryCard, PickAllocation.inventory_card_id == InventoryCard.id)
        .filter(
            PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES),
            InventoryCard.status == "available",
        )
        .first()
    )
    if inconsistent:
        raise InventoryAllocationError(
            "An active allocation is attached to an available inventory card."
        )


def desired_sellable_quantities(session: Session) -> Counter:
    """Count availability once, from card status; never subtract allocations."""
    validate_inventory_invariants(session)
    rows = (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.status == "available",
            Batch.is_archived == False,
            InventoryCard.mtgjson_id.isnot(None),
            InventoryCard.language_id.isnot(None),
            InventoryCard.condition_id.isnot(None),
            InventoryCard.finish_id.isnot(None),
        )
        .all()
    )
    return Counter(
        (
            card.mtgjson_id.upper(),
            card.language_id.upper(),
            card.condition_id.upper(),
            card.finish_id.upper(),
        )
        for card in rows
    )


def _remote_order_item(remote_item: dict, order_id: int) -> OrderItem | None:
    product = remote_item.get("product") or {}
    single = product.get("single") or {}
    if not single:
        return None
    required = {
        "mtgjson_id": single.get("mtgjson_id"),
        "language_id": single.get("language_id"),
        "condition_id": single.get("condition_id"),
        "finish_id": single.get("finish_id"),
    }
    if not all(str(value or "").strip() for value in required.values()):
        raise InventoryAllocationError(
            f"Mana Pool order line {single.get('name')!r} lacks canonical identity."
        )
    return OrderItem(
        order_id=order_id,
        name=str(single.get("name") or "Unknown Card"),
        set_code=str(single.get("set") or "") or None,
        collector_number=str(single.get("number") or "") or None,
        finish=str(single.get("finish_id") or "") or None,
        scryfall_id=str(single.get("scryfall_id") or "") or None,
        mtgjson_id=str(required["mtgjson_id"]),
        language_id=str(required["language_id"]).upper(),
        condition_id=str(required["condition_id"]).upper(),
        finish_id=str(required["finish_id"]).upper(),
        tcgsku=str(remote_item.get("tcgsku") or product.get("tcgplayer_sku") or "") or None,
        quantity=int(remote_item.get("quantity") or 1),
    )


def _line_signature(items: list[OrderItem]) -> Counter:
    result = Counter()
    for item in items:
        key = (
            item.mtgjson_id,
            item.language_id,
            item.condition_id,
            item.finish_id,
            (item.set_code or "").upper(),
            (item.collector_number or "").upper(),
        )
        result[key] += int(item.quantity)
    return result


def ingest_manapool_orders(session: Session, remote_orders: list[dict], detail_loader):
    """Reconcile and immediately reserve exact inventory for live orders."""
    validate_inventory_invariants(session)
    result = {"imported": 0, "already_known": 0}
    for summary in remote_orders:
        remote_id = str(summary.get("id") or "").strip()
        if not remote_id:
            continue
        response = detail_loader(remote_id)
        detail = response.get("order") or response
        existing = session.query(SalesOrder).filter(
            SalesOrder.source == "manapool",
            SalesOrder.external_order_id == remote_id,
        ).first()
        if existing:
            order = existing
            result["already_known"] += 1
        else:
            order = SalesOrder(
                external_order_id=remote_id,
                source="manapool",
                status="needs_review",
            )
            session.add(order)
            session.flush()
            result["imported"] += 1

        remote_items = []
        for raw in detail.get("items") or []:
            item = _remote_order_item(raw, order.id)
            if item:
                remote_items.append(item)
        if not remote_items:
            raise InventoryAllocationError(f"Mana Pool order {remote_id} has no exact single lines.")

        current_items = session.query(OrderItem).filter(OrderItem.order_id == order.id).all()
        allocations = (
            session.query(PickAllocation)
            .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
            .filter(OrderItem.order_id == order.id)
            .all()
        )
        if current_items and _line_signature(current_items) != _line_signature(remote_items):
            if allocations:
                raise InventoryAllocationError(
                    f"Mana Pool order {remote_id} lines changed after allocation."
                )
            for item in current_items:
                session.delete(item)
            session.flush()
            current_items = []
        if not current_items:
            session.add_all(remote_items)
            session.flush()

        active = [a for a in allocations if a.status in ACTIVE_ALLOCATION_STATUSES]
        if not active and order.status not in {"cancelled", "shipped"}:
            allocate_order(session, order, preserve_order_status=True)

        order.external_label = detail.get("label") or summary.get("label")
        order.remote_fulfillment_status = (
            detail.get("latest_fulfillment_status")
            or summary.get("latest_fulfillment_status")
            or None
        )
        order.last_synced_at = datetime.now()
    return result


def approve_reserved_order(session: Session, order: SalesOrder):
    active_count = (
        session.query(PickAllocation)
        .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
        .filter(
            OrderItem.order_id == order.id,
            PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES),
        )
        .count()
    )
    requested = sum(
        item.quantity for item in session.query(OrderItem).filter(
            OrderItem.order_id == order.id
        ).all()
    )
    if active_count == 0:
        allocate_order(session, order)
    elif active_count == requested:
        order.status = "ready_to_pick"
    else:
        raise InventoryAllocationError("Order has a partial allocation and cannot be approved.")


def parse_order_lines(text: str):
    parsed_items = []
    errors = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if not line:
            continue

        parts = [part.strip() for part in line.split("|")]

        if len(parts) != 5:
            errors.append(
                f"Line {line_number}: expected 5 fields separated by |"
            )
            continue

        name, set_code, collector_number, finish, quantity_text = parts

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


def allocate_order(session: Session, order: SalesOrder, preserve_order_status=False):
    validate_inventory_invariants(session)

    items = (
        session.query(OrderItem)
        .filter(OrderItem.order_id == order.id)
        .order_by(OrderItem.id)
        .all()
    )

    for item in items:
        mtgjson_id, language_id, condition_id, finish_id = _canonical_item_key(item)
        family_query = session.query(InventoryCard).join(
            Batch, InventoryCard.batch_id == Batch.id,
        ).filter(
            InventoryCard.status == "available",
            Batch.is_archived == False,
            func.upper(InventoryCard.mtgjson_id) == mtgjson_id,
            func.upper(InventoryCard.language_id) == language_id,
            func.upper(InventoryCard.condition_id) == condition_id,
            func.upper(InventoryCard.finish_id) == finish_id,
        )
        family = family_query.all()
        validation_identities = {
            (
                (card.name or "").casefold(),
                (card.set_code or "").upper(),
                (card.collector_number or "").upper(),
            )
            for card in family
        }
        if len(validation_identities) > 1:
            raise InventoryAllocationError(
                f"Ambiguous local cross-check metadata for {item.name}."
            )
        query = family_query.filter(func.lower(InventoryCard.name) == item.name.lower())
        if item.set_code:
            query = query.filter(func.upper(InventoryCard.set_code) == item.set_code.upper())
        if item.collector_number:
            query = query.filter(
                func.upper(InventoryCard.collector_number) == item.collector_number.upper()
            )

        cards = (
            query.order_by(
                InventoryCard.imported_at,
                InventoryCard.id,
            )
            .limit(item.quantity)
            .all()
        )

        if len(cards) != item.quantity:
            raise InventoryAllocationError(
                f"Insufficient exact inventory for {item.name}: "
                f"needed {item.quantity}, found {len(cards)}."
            )

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
        session.flush()

    if not preserve_order_status:
        order.status = "ready_to_pick"


def release_order(session: Session, order: SalesOrder):
    allocations = (
        session.query(PickAllocation)
        .join(
            OrderItem,
            PickAllocation.order_item_id == OrderItem.id,
        )
        .filter(
            OrderItem.order_id == order.id,
            PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES),
        )
        .all()
    )

    for allocation in allocations:
        card = session.get(InventoryCard, allocation.inventory_card_id)

        if card and card.status == "reserved":
            card.status = "available"

        allocation.status = "released"

    order.status = "cancelled"


def mark_picked(session: Session, order: SalesOrder):
    allocations = _active_allocations(session, order.id)

    for allocation in allocations:
        allocation.status = "picked"

    order.status = "picked"
    order.picked_at = datetime.now()


def mark_packed(session: Session, order: SalesOrder):
    allocations = _active_allocations(session, order.id)

    for allocation in allocations:
        allocation.status = "packed"

    order.status = "packed"
    order.packed_at = datetime.now()


def mark_shipped(
    session: Session,
    order: SalesOrder,
    tracking_number: str | None,
):
    allocations = _active_allocations(session, order.id)

    for allocation in allocations:
        card = session.get(InventoryCard, allocation.inventory_card_id)

        if card:
            card.status = "sold"

        allocation.status = "shipped"

    order.status = "shipped"
    order.shipped_at = datetime.now()

    cleaned_tracking = tracking_number.strip() if tracking_number else ""
    order.tracking_number = cleaned_tracking or None


def _active_allocations(session: Session, order_id: int):
    return (
        session.query(PickAllocation)
        .join(
            OrderItem,
            PickAllocation.order_item_id == OrderItem.id,
        )
        .filter(
            OrderItem.order_id == order_id,
            PickAllocation.status.in_(["allocated", "picked", "packed"]),
        )
        .all()
    )


def get_picklist(session: Session, order_id: int):
    allocations = (
        session.query(
            PickAllocation,
            OrderItem,
            InventoryCard,
            Batch,
        )
        .join(
            OrderItem,
            PickAllocation.order_item_id == OrderItem.id,
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
            OrderItem.order_id == order_id,
            PickAllocation.status.in_([
                "allocated",
                "picked",
                "packed",
                "shipped",
            ]),
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
        grouped.setdefault(batch.batch_code, [])
        grouped[batch.batch_code].append(
            {
                "allocation": allocation,
                "item": item,
                "card": card,
            }
        )

    return grouped
