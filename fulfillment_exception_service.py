"""Guarded local creation of card-level fulfillment exceptions.

This commit contains only the shared mark-exception transition.  Callers are
expected to hold the existing inventory synchronization lease and the caller's
transaction owns commit/rollback; this function never contacts Mana Pool or
commits independently.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from fulfillment_exception_constants import (
    EXCEPTION_NOTE_PREFIX,
    EXCEPTION_TYPES,
    FULFILLMENT_EXCEPTION_CREATED_EVENT,
)
from fulfillment_exception_invariants import validate_exception_values
from models import (
    Batch,
    FulfillmentException,
    FulfillmentExceptionEvent,
    InventoryCard,
    InventoryChangeLog,
    OrderItem,
    PickAllocation,
    SalesOrder,
)


class FulfillmentExceptionError(ValueError):
    pass


TERMINAL_ORDER_STATUSES = {
    "cancelled", "completed", "fulfilled", "refunded", "shipped", "closed",
}
ALLOWED_ALLOCATION_STATUSES = {"allocated", "picked"}


def _identity_matches(item: OrderItem, card: InventoryCard) -> bool:
    fields = ("mtgjson_id", "language_id", "condition_id", "finish_id")
    if not all(str(getattr(item, field, None) or "").strip() for field in fields):
        return False
    if not all(str(getattr(card, field, None) or "").strip() for field in fields):
        return False
    if any(
        str(getattr(item, field, "") or "").strip().upper()
        != str(getattr(card, field, "") or "").strip().upper()
        for field in fields
    ):
        return False
    for field in ("name", "set_code", "collector_number", "scryfall_id"):
        item_value = str(getattr(item, field, "") or "").strip().casefold()
        card_value = str(getattr(card, field, "") or "").strip().casefold()
        if item_value != card_value:
            return False
    return True


def _audit_inventory(
    session: Session,
    card: InventoryCard,
    batch: Batch,
    exception: FulfillmentException,
    previous_status: str,
    new_status: str,
    reason: str,
    note: str,
    timestamp: datetime,
):
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "fulfillment_exception_inventory_change",
            "previous_status": previous_status,
            "new_status": new_status,
            "reason": reason,
            "note": note,
            "fulfillment_exception_id": exception.id,
            "sales_order_id": exception.sales_order_id,
            "order_item_id": exception.order_item_id,
            "pick_allocation_id": exception.pick_allocation_id,
            "timestamp": timestamp.isoformat(),
            "card_identity": {
                "name": card.name,
                "set_code": card.set_code,
                "collector_number": card.collector_number,
                "scryfall_id": card.scryfall_id,
                "mtgjson_id": card.mtgjson_id,
                "language_id": card.language_id,
                "condition_id": card.condition_id,
                "finish_id": card.finish_id,
            },
            "batch": {"id": batch.id, "batch_code": batch.batch_code},
            "import_record_id": card.import_id,
        }, sort_keys=True),
    ))


def mark_fulfillment_exception(
    session: Session,
    allocation_id: int,
    exception_type: str,
    note: str | None = None,
    operator_metadata=None,
) -> FulfillmentException:
    """Atomically mark one reserved physical allocation as exceptional.

    The caller must hold ``inventory_sync_lease()``.  No commit is performed;
    callers can include this operation in their existing transaction and roll
    back every mutation on any failure.
    """
    kind = str(exception_type or "").strip().lower()
    if kind not in EXCEPTION_TYPES:
        raise FulfillmentExceptionError("Unsupported fulfillment exception type.")
    final_note = str(note or "").strip()
    if not final_note:
        final_note = EXCEPTION_NOTE_PREFIX + datetime.now(timezone.utc).isoformat()
    if not final_note.strip():
        raise FulfillmentExceptionError("Fulfillment exception note is required.")

    allocation = session.get(PickAllocation, allocation_id, with_for_update=True)
    if not allocation:
        raise FulfillmentExceptionError("Pick allocation not found.")
    existing = session.query(FulfillmentException).filter(
        FulfillmentException.pick_allocation_id == allocation.id,
    ).first()
    if existing:
        if existing.exception_type == kind:
            card = session.get(InventoryCard, allocation.inventory_card_id)
            if (
                allocation.status == "exception"
                and card
                and card.inventory_exception_state == "exception_unresolved"
                and existing.submission_state == "needs_submission"
                and existing.inventory_resolution_state == "unresolved"
            ):
                return existing
            raise FulfillmentExceptionError("Existing fulfillment exception is inconsistent.")
        raise FulfillmentExceptionError("A conflicting fulfillment exception already exists.")
    if allocation.status not in ALLOWED_ALLOCATION_STATUSES:
        raise FulfillmentExceptionError("Only allocated or picked cards can be marked exceptional.")

    item = session.get(OrderItem, allocation.order_item_id)
    card = session.get(InventoryCard, allocation.inventory_card_id, with_for_update=True)
    if not item or not card:
        raise FulfillmentExceptionError("Allocation is missing its order item or inventory card.")
    order = session.get(SalesOrder, item.order_id)
    if not order or order.status in TERMINAL_ORDER_STATUSES:
        raise FulfillmentExceptionError("Order is terminal or incompatible with a fulfillment exception.")
    if card.status != "reserved":
        raise FulfillmentExceptionError("Inventory card is not reserved for this allocation.")
    if card.inventory_exception_state != "none":
        raise FulfillmentExceptionError("Inventory card already has an unresolved exception projection.")
    duplicate_card = session.query(FulfillmentException).filter(
        FulfillmentException.inventory_card_id == card.id,
    ).first()
    if duplicate_card:
        raise FulfillmentExceptionError("A fulfillment exception already references this card.")
    if not _identity_matches(item, card):
        raise FulfillmentExceptionError("Order and physical card identities no longer match.")

    validate_exception_values(kind, "needs_submission", "awaiting", "unresolved")
    timestamp = datetime.now(timezone.utc)
    exception = FulfillmentException(
        sales_order_id=order.id,
        order_item_id=item.id,
        pick_allocation_id=allocation.id,
        inventory_card_id=card.id,
        exception_type=kind,
        submission_state="needs_submission",
        remote_resolution_state="awaiting",
        inventory_resolution_state="unresolved",
        note=final_note,
        remote_order_id=order.external_order_id,
        created_at=timestamp.replace(tzinfo=None),
    )
    session.add(exception)
    session.flush()

    allocation.status = "exception"
    card.inventory_exception_state = "exception_unresolved"
    batch = session.get(Batch, card.batch_id)
    if not batch:
        raise FulfillmentExceptionError("Inventory card batch not found.")
    if kind == "missing":
        card.status = "removed"
        card.removal_reason = "fulfillment_missing"
        card.removal_note = final_note
        card.removed_at = timestamp.replace(tzinfo=None)
        new_status = "removed"
        reason = "fulfillment_missing"
    else:
        card.status = "unsellable"
        card.unsellable_reason = "fulfillment_inventory_mismatch"
        card.unsellable_note = final_note
        card.unsellable_at = timestamp.replace(tzinfo=None)
        new_status = "unsellable"
        reason = "fulfillment_inventory_mismatch"

    evidence = {
        "sales_order_id": order.id,
        "order_item_id": item.id,
        "pick_allocation_id": allocation.id,
        "inventory_card_id": card.id,
        "exception_type": kind,
        "initial_states": {
            "submission_state": "needs_submission",
            "remote_resolution_state": "awaiting",
            "inventory_resolution_state": "unresolved",
        },
        "note": final_note,
        "operator_metadata": operator_metadata,
        "timestamp": timestamp.isoformat(),
    }
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=FULFILLMENT_EXCEPTION_CREATED_EVENT,
        previous_state=None,
        new_state="needs_submission",
        note=final_note,
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        evidence_hash=None,
        created_at=timestamp.replace(tzinfo=None),
        operator_metadata=json.dumps(operator_metadata, sort_keys=True, default=str)
        if operator_metadata is not None else None,
    ))
    _audit_inventory(
        session, card, batch, exception, "reserved", new_status, reason, final_note, timestamp,
    )
    session.flush()
    return exception
