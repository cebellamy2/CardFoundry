import os
from collections import Counter
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import (
    Batch,
    FulfillmentException,
    InventoryCard,
    OrderItem,
    PickAllocation,
    SalesOrder,
)
from competitor_pricing_service import _RequestPacer
from consignment_service import apply_consignment_payout_if_consigned
from fulfillment_exception_invariants import order_has_fulfillment_submission_block
from legacy_import_service import scryfall_card_colors, wubrg_color_string
from fulfillment_exception_reconciliation_service import (
    reconcile_remote_fulfillment_exceptions,
)


ACTIVE_ALLOCATION_STATUSES = ("allocated", "picked", "packed")
KNOWN_INVENTORY_STATUSES = {"available", "unsellable", "reserved", "sold", "removed"}

# A floor on the gap between per-order GET /seller/orders/{id} calls in
# ingest_manapool_orders. Without it, reconciliation fires one unpaced
# request per order returned by get_seller_orders(since=go_live_at) --
# live evidence: 58 orders fired back-to-back tripped Mana Pool's rate
# limit, and the very next (correctly paced) /buyer/optimizer call in the
# same Perform Sync run inherited the block and failed on its first
# attempt. Same shape as the OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS
# incident (competitor_pricing_service.py) -- a different endpoint that
# simply never got the same treatment.
ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS = float(
    os.environ.get("ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS", "1.0")
)

# Pacing alone was not sufficient: a live, fully-instrumented Perform Sync
# run showed Mana Pool's rate limit trip at roughly the 60-70th request in
# a single run even with every call correctly spaced -- the limit bounds
# total request count in a rolling window, not just instantaneous rate.
# create_inventory_sync_preview's mirror-preview step runs this ingest
# unconditionally on every Perform Sync click, and apply_reconciliation_
# preview's own freshness re-ingest can run again in the same click, so
# both are capped at this same value. Chosen so the worst case (both
# running back-to-back) plus the run's other calls (inventory listings,
# optimizer batches) stays comfortably under the observed threshold.
ORDER_SYNC_MAX_ORDERS_PER_RUN = int(
    os.environ.get("ORDER_SYNC_MAX_ORDERS_PER_RUN", "20")
)


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


def _remote_order_item(
    remote_item: dict, order_id: int, color: str | None = None,
) -> OrderItem | None:
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
        price_cents=(
            int(remote_item["price_cents"])
            if remote_item.get("price_cents") is not None
            else None
        ),
        color=color,
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


def _color_by_scryfall_id(detail: dict, scryfall_lookup) -> dict:
    """Best-effort enrichment only -- a Scryfall outage must never block
    order sync, so any lookup failure yields an empty map (no color shown
    yet) rather than surfacing as a sync error."""
    if not scryfall_lookup:
        return {}
    ids = sorted({
        str((((raw.get("product") or {}).get("single") or {}).get("scryfall_id")) or "")
        for raw in detail.get("items") or []
    } - {""})
    if not ids:
        return {}
    try:
        result = scryfall_lookup(ids)
        cards_by_id = result[0] if isinstance(result, tuple) else result
    except Exception:
        return {}
    return {
        scryfall_id: wubrg_color_string(scryfall_card_colors(card))
        for scryfall_id, card in cards_by_id.items()
    }


def _build_remote_items(
    detail: dict, remote_id: str, order_id: int, scryfall_lookup=None,
) -> list[OrderItem]:
    color_by_id = _color_by_scryfall_id(detail, scryfall_lookup)
    remote_items = []
    for raw in detail.get("items") or []:
        single = (raw.get("product") or {}).get("single") or {}
        color = color_by_id.get(str(single.get("scryfall_id") or ""))
        item = _remote_order_item(raw, order_id, color)
        if item:
            remote_items.append(item)
    if not remote_items:
        raise InventoryAllocationError(f"Mana Pool order {remote_id} has no exact single lines.")
    return remote_items


def _apply_shipping_address(order: SalesOrder, detail: dict) -> None:
    """Only the order-detail payload carries shipping_address; the list
    endpoint doesn't. A prior sync's address is left alone if this pass's
    detail has none (e.g. a needs_review retry hitting a payload gap).
    """
    address = detail.get("shipping_address")
    if not address:
        return
    order.shipping_name = address.get("name")
    order.shipping_line1 = address.get("line1")
    order.shipping_line2 = address.get("line2")
    order.shipping_city = address.get("city")
    order.shipping_state = address.get("state")
    order.shipping_postal_code = address.get("postal_code")
    order.shipping_country = address.get("country")


def _apply_shipping_cost(order: SalesOrder, detail: dict) -> None:
    """Only the order-detail payload carries payment.shipping_cents."""
    payment = detail.get("payment")
    if not payment or payment.get("shipping_cents") is None:
        return
    order.shipping_cents = int(payment["shipping_cents"])


def _sync_one_manapool_order(
    session: Session, remote_id: str, summary: dict, detail: dict, scryfall_lookup=None,
) -> str:
    existing = session.query(SalesOrder).filter(
        SalesOrder.source == "manapool",
        SalesOrder.external_order_id == remote_id,
    ).first()
    is_new = existing is None
    if existing:
        order = existing
    else:
        order = SalesOrder(
            external_order_id=remote_id,
            source="manapool",
            status="needs_review",
        )
        session.add(order)
        session.flush()

    current_items = session.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    allocations = (
        session.query(PickAllocation)
        .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
        .filter(OrderItem.order_id == order.id)
        .all()
    )

    try:
        remote_items = _build_remote_items(detail, remote_id, order.id, scryfall_lookup)
    except InventoryAllocationError as exc:
        if current_items:
            # This order already has good items from a prior sync; a data
            # problem in this pass's payload doesn't get to overwrite that.
            # Surface it as a batch-isolated failure instead of touching
            # the order.
            raise
        order.status = "needs_review"
        order.review_detail = str(exc)
        order.external_label = detail.get("label") or summary.get("label")
        order.shipping_method = detail.get("shipping_method") or summary.get("shipping_method")
        _apply_shipping_address(order, detail)
        _apply_shipping_cost(order, detail)
        order.last_synced_at = datetime.now()
        return "imported" if is_new else "already_known"

    has_fulfillment_exceptions = session.query(FulfillmentException).filter(
        FulfillmentException.sales_order_id == order.id,
    ).count() > 0
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
    if (
        not active
        and not has_fulfillment_exceptions
        and order.status not in {"cancelled", "shipped"}
    ):
        try:
            result = allocate_order(session, order)
        except InventoryAllocationError as exc:
            order.status = "needs_review"
            order.review_detail = str(exc)
        else:
            _apply_allocation_outcome(order, result)

    order.external_label = detail.get("label") or summary.get("label")
    order.remote_fulfillment_status = (
        detail.get("latest_fulfillment_status")
        or summary.get("latest_fulfillment_status")
        or None
    )
    order.shipping_method = detail.get("shipping_method") or summary.get("shipping_method")
    _apply_shipping_address(order, detail)
    _apply_shipping_cost(order, detail)
    reconcile_remote_fulfillment_exceptions(session, order, detail)
    order.last_synced_at = datetime.now()
    return "imported" if is_new else "already_known"


def _prioritize_orders_for_sync(session: Session, remote_orders: list[dict]) -> list[dict]:
    """Never-synced orders first (an order must exist locally before it can
    be picked at all), then by staleness -- oldest ``last_synced_at`` first.
    This is what lets a capped run still drain the whole backlog over a
    few consecutive Perform Sync clicks instead of the same tail of
    orders being skipped every single time."""
    remote_ids = [
        str(order.get("id") or "").strip() for order in remote_orders if order.get("id")
    ]
    last_synced_by_id = {
        order.external_order_id: order.last_synced_at
        for order in session.query(SalesOrder).filter(
            SalesOrder.source == "manapool",
            SalesOrder.external_order_id.in_(remote_ids),
        )
    }

    def sort_key(order: dict):
        remote_id = str(order.get("id") or "").strip()
        last_synced = last_synced_by_id.get(remote_id)
        return (last_synced is not None, last_synced or datetime.min)

    return sorted(remote_orders, key=sort_key)


def ingest_manapool_orders(
    session: Session, remote_orders: list[dict], detail_loader, scryfall_lookup=None,
    min_request_interval: float | None = None, max_orders: int | None = None,
):
    """Reconcile and reserve exact inventory for live orders.

    Each order in the batch is processed and committed independently: one
    order's problem (a plain shortfall, an identity/data issue, or anything
    unexpected) never blocks or rolls back any other order in the same
    batch. Per-order failures are reported in ``result["failed"]`` rather
    than raised, except for a genuine batch-wide invariant violation
    (checked once, up front, before any order is processed).

    Because a per-order failure calls ``session.rollback()``, callers must
    not have other uncommitted work on ``session`` before invoking this --
    that work would be discarded by the first order's rollback, not just
    this function's own changes. The production caller always opens a
    fresh session immediately before calling this.

    ``detail_loader`` is paced (see ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS)
    -- one call per order in ``remote_orders``, unpaced, is exactly the
    burst that tripped Mana Pool's rate limit live. Pacing alone was not
    enough: a live, fully-instrumented Perform Sync run showed correctly
    -paced traffic still tripping the limit at roughly the 60-70th
    request in a run, meaning Mana Pool's limit bounds total request
    *count* in a rolling window, not just instantaneous rate. ``max_orders``
    (see ORDER_SYNC_MAX_ORDERS_PER_RUN) caps how many orders get a fresh
    detail fetch this call -- prioritized by ``_prioritize_orders_for_sync``
    so a capped run drains an oversized backlog over successive clicks.
    Anything left over is reported in ``result["deferred"]``, never
    silently dropped -- it is simply left for the next Perform Sync click.
    """
    validate_inventory_invariants(session)
    pacer = _RequestPacer(
        ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS
        if min_request_interval is None else min_request_interval
    )
    result = {"imported": 0, "already_known": 0, "failed": [], "deferred": 0}
    ordered = _prioritize_orders_for_sync(session, remote_orders)
    if max_orders is not None:
        to_process, deferred = ordered[:max_orders], ordered[max_orders:]
        result["deferred"] = len(deferred)
    else:
        to_process = ordered
    for summary in to_process:
        remote_id = str(summary.get("id") or "").strip()
        if not remote_id:
            continue
        try:
            pacer.wait()
            response = detail_loader(remote_id)
            detail = response.get("order") or response
            outcome = _sync_one_manapool_order(
                session, remote_id, summary, detail, scryfall_lookup,
            )
            result[outcome] += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            result["failed"].append(f"{remote_id}: {exc}")
    return result


def _apply_allocation_outcome(order: SalesOrder, result: dict) -> None:
    """Decide ready_to_pick vs. short from a best-effort allocate_order() result."""
    if result["fully_matched"]:
        order.status = "ready_to_pick"
    else:
        order.status = "short"
    order.review_detail = None


def approve_reserved_order(session: Session, order: SalesOrder):
    """Confirm or retry allocation for a needs_review/short order.

    Never raises for a plain shortfall or a fresh identity/data problem --
    those become ``short`` or ``needs_review`` (with a detail message)
    respectively, so an operator can retry from the order page instead of
    hitting an unhandled error.
    """
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
    exception_count = session.query(FulfillmentException).join(
        OrderItem, FulfillmentException.order_item_id == OrderItem.id,
    ).filter(OrderItem.order_id == order.id).count()
    if active_count == 0 and exception_count == 0:
        try:
            result = allocate_order(session, order)
        except InventoryAllocationError as exc:
            order.status = "needs_review"
            order.review_detail = str(exc)
            return
        _apply_allocation_outcome(order, result)
    elif active_count + exception_count == requested:
        order.status = "ready_to_pick"
        order.review_detail = None
    else:
        order.status = "short"
        order.review_detail = None


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


def allocate_order(session: Session, order: SalesOrder) -> dict:
    """Best-effort allocation: attempts every line, even if one comes up
    short on available inventory, instead of aborting on the first
    shortfall.

    Still raises ``InventoryAllocationError`` immediately for genuine
    identity/data problems -- missing canonical identity, or ambiguous
    local cross-check metadata -- since those aren't something more stock
    or more time can fix. Never sets ``order.status``; the caller decides
    ready_to_pick vs. short from the returned per-line tally.
    """
    validate_inventory_invariants(session)

    items = (
        session.query(OrderItem)
        .filter(OrderItem.order_id == order.id)
        .order_by(OrderItem.id)
        .all()
    )

    line_results = []

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

        represented_exceptions = session.query(FulfillmentException).filter(
            FulfillmentException.order_item_id == item.id,
        ).count()
        needed = max(item.quantity - represented_exceptions, 0)
        allocated = 0
        if needed > 0:
            cards = (
                query.order_by(
                    InventoryCard.imported_at,
                    InventoryCard.id,
                )
                .limit(needed)
                .all()
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
                allocated += 1
            session.flush()

        line_results.append({
            "order_item_id": item.id,
            "name": item.name,
            "requested": needed,
            "allocated": allocated,
        })

    return {
        "line_results": line_results,
        "fully_matched": all(
            row["requested"] == row["allocated"] for row in line_results
        ),
    }


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
    if order_has_fulfillment_submission_block(session.query(FulfillmentException).join(
        OrderItem, FulfillmentException.order_item_id == OrderItem.id,
    ).filter(OrderItem.order_id == order.id).all()):
        raise InventoryAllocationError("Order has fulfillment exceptions awaiting ManaPool submission.")
    allocations = _active_allocations(session, order.id)

    for allocation in allocations:
        allocation.status = "picked"

    order.status = "picked"
    order.picked_at = datetime.now()


def mark_packed(session: Session, order: SalesOrder):
    if order_has_fulfillment_submission_block(session.query(FulfillmentException).join(
        OrderItem, FulfillmentException.order_item_id == OrderItem.id,
    ).filter(OrderItem.order_id == order.id).all()):
        raise InventoryAllocationError("Order has fulfillment exceptions awaiting ManaPool submission.")
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
    if order_has_fulfillment_submission_block(session.query(FulfillmentException).join(
        OrderItem, FulfillmentException.order_item_id == OrderItem.id,
    ).filter(OrderItem.order_id == order.id).all()):
        raise InventoryAllocationError("Order has fulfillment exceptions awaiting ManaPool submission.")
    allocations = _active_allocations(session, order.id)

    for allocation in allocations:
        card = session.get(InventoryCard, allocation.inventory_card_id)

        if card:
            card.status = "sold"
            order_item = session.get(OrderItem, allocation.order_item_id)
            if order_item and order_item.price_cents is not None:
                card.sold_price = order_item.price_cents / 100
            apply_consignment_payout_if_consigned(session, card)

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
