"""Guarded local confirmation of manual ManaPool exception submission."""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from fulfillment_exception_constants import (
    FULFILLMENT_EXCEPTION_SUBMITTED_EVENT,
    SUBMISSION_NOTE_PREFIX,
)
from fulfillment_exception_service import FulfillmentExceptionError
from models import (
    FulfillmentException,
    FulfillmentExceptionEvent,
    InventoryCard,
    OrderItem,
    PickAllocation,
    SalesOrder,
)


TERMINAL_ORDER_STATUSES = {
    "cancelled", "completed", "fulfilled", "refunded", "shipped", "closed",
}


def _linked_context(session: Session, exception_id: int):
    exception = session.get(FulfillmentException, exception_id, with_for_update=True)
    if not exception:
        raise FulfillmentExceptionError("Fulfillment exception not found.")
    order = session.get(SalesOrder, exception.sales_order_id)
    item = session.get(OrderItem, exception.order_item_id)
    allocation = session.get(PickAllocation, exception.pick_allocation_id, with_for_update=True)
    card = session.get(InventoryCard, exception.inventory_card_id, with_for_update=True)
    if not order or not item or not allocation or not card:
        raise FulfillmentExceptionError("Fulfillment exception linkage is incomplete.")
    if order.status in TERMINAL_ORDER_STATUSES:
        raise FulfillmentExceptionError("Order is terminal and cannot accept submission confirmation.")
    if (
        item.order_id != order.id
        or allocation.order_item_id != item.id
        or allocation.inventory_card_id != card.id
        or exception.inventory_card_id != card.id
    ):
        raise FulfillmentExceptionError("Fulfillment exception linkage is inconsistent.")
    if allocation.status != "exception":
        raise FulfillmentExceptionError("Exception allocation is no longer in exception state.")
    return exception, order, item, allocation, card


def confirm_fulfillment_exception_submitted(
    session: Session,
    exception_id: int,
    note: str | None = None,
    operator_metadata=None,
) -> FulfillmentException:
    """Record an operator's completed manual ManaPool submission.

    The caller holds the normal inventory synchronization lease and owns the
    transaction.  This operation never calls ManaPool and never commits.
    """
    exception, order, item, allocation, card = _linked_context(session, exception_id)
    if exception.submission_state == "submitted":
        if exception.submitted_at and session.query(FulfillmentExceptionEvent).filter(
            FulfillmentExceptionEvent.fulfillment_exception_id == exception.id,
            FulfillmentExceptionEvent.event_type == FULFILLMENT_EXCEPTION_SUBMITTED_EVENT,
        ).count() == 1:
            return exception
        raise FulfillmentExceptionError("Submitted exception has inconsistent audit state.")
    if exception.submission_state != "needs_submission":
        raise FulfillmentExceptionError("Unsupported submission state.")
    final_note = str(note or "").strip()
    if not final_note:
        final_note = SUBMISSION_NOTE_PREFIX + datetime.now(timezone.utc).isoformat()
    timestamp = datetime.now(timezone.utc)
    exception.submission_state = "submitted"
    exception.submitted_at = timestamp.replace(tzinfo=None)
    evidence = {
        "sales_order_id": order.id,
        "order_item_id": item.id,
        "pick_allocation_id": allocation.id,
        "inventory_card_id": card.id,
        "exception_type": exception.exception_type,
        "previous_submission_state": "needs_submission",
        "new_submission_state": "submitted",
        "note": final_note,
        "operator_metadata": operator_metadata,
        "timestamp": timestamp.isoformat(),
    }
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=FULFILLMENT_EXCEPTION_SUBMITTED_EVENT,
        previous_state="needs_submission",
        new_state="submitted",
        note=final_note,
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        evidence_hash=None,
        created_at=timestamp.replace(tzinfo=None),
        operator_metadata=json.dumps(operator_metadata, sort_keys=True, default=str)
        if operator_metadata is not None else None,
    ))
    session.flush()
    return exception
