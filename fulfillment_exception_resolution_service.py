"""Guarded inventory-side resolution for card-level fulfillment exceptions."""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from fulfillment_exception_constants import (
    FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
    FULFILLMENT_INVENTORY_CORRECTION_COMPLETED_EVENT,
)
from fulfillment_exception_service import FulfillmentExceptionError
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
from printing_correction_service import apply_printing_correction
from sellability_service import transition_sellability


def _context(session: Session, exception_id: int):
    exception = session.get(FulfillmentException, exception_id, with_for_update=True)
    if not exception:
        raise FulfillmentExceptionError("Fulfillment exception not found.")
    allocation = session.get(PickAllocation, exception.pick_allocation_id, with_for_update=True)
    item = session.get(OrderItem, exception.order_item_id)
    card = session.get(InventoryCard, exception.inventory_card_id, with_for_update=True)
    order = session.get(SalesOrder, exception.sales_order_id)
    if not allocation or not item or not card or not order:
        raise FulfillmentExceptionError("Fulfillment exception linkage is incomplete.")
    if item.order_id != order.id or allocation.order_item_id != item.id:
        raise FulfillmentExceptionError("Fulfillment exception linkage is inconsistent.")
    if allocation.inventory_card_id != card.id or exception.inventory_card_id != card.id:
        raise FulfillmentExceptionError("Fulfillment exception card linkage is inconsistent.")
    if allocation.status != "exception":
        raise FulfillmentExceptionError("Exception allocation is no longer in exception state.")
    return exception, order, item, allocation, card


def _resolution_note(note: str | None) -> str:
    cleaned = str(note or "").strip()
    return cleaned or "Fulfillment inventory resolved — " + datetime.now(timezone.utc).isoformat()


def _event(session, exception, event_type, previous_state, new_state, note, evidence, timestamp):
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=event_type,
        previous_state=previous_state,
        new_state=new_state,
        note=note,
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        evidence_hash=None,
        created_at=timestamp.replace(tzinfo=None),
    ))


def _projection_audit(session, card, exception, previous_status, new_status, note, timestamp):
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "fulfillment_exception_inventory_resolved",
            "previous_status": previous_status,
            "new_status": new_status,
            "previous_inventory_exception_state": "exception_unresolved",
            "new_inventory_exception_state": "none",
            "fulfillment_exception_id": exception.id,
            "sales_order_id": exception.sales_order_id,
            "order_item_id": exception.order_item_id,
            "pick_allocation_id": exception.pick_allocation_id,
            "note": note,
            "timestamp": timestamp.isoformat(),
        }, sort_keys=True),
    ))


def resolve_missing_inventory_exception(
    session: Session,
    exception_id: int,
    note: str | None = None,
    operator_metadata=None,
) -> FulfillmentException:
    """Accept a missing card as permanently absent without restoring it."""
    exception, order, item, allocation, card = _context(session, exception_id)
    if exception.exception_type != "missing":
        raise FulfillmentExceptionError("Exception is not a missing-card exception.")
    if exception.inventory_resolution_state == "resolved":
        if card.status == "removed" and card.inventory_exception_state == "none":
            return exception
        raise FulfillmentExceptionError("Resolved missing exception has inconsistent card state.")
    if exception.inventory_resolution_state != "unresolved":
        raise FulfillmentExceptionError("Unsupported inventory resolution state.")
    if card.status != "removed" or card.inventory_exception_state != "exception_unresolved":
        raise FulfillmentExceptionError("Missing card is not in the expected removed exception state.")
    if card.removal_reason != "fulfillment_missing":
        raise FulfillmentExceptionError("Missing card removal reason changed unexpectedly.")
    final_note = _resolution_note(note)
    timestamp = datetime.now(timezone.utc)
    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = final_note
    card.inventory_exception_state = "none"
    _event(session, exception, FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
           "unresolved", "resolved", final_note, {
               "exception_type": "missing", "operator_metadata": operator_metadata,
               "inventory_card_id": card.id, "allocation_id": allocation.id,
           }, timestamp)
    _projection_audit(session, card, exception, "removed", "removed", final_note, timestamp)
    session.flush()
    return exception


def resolve_inventory_mismatch_exception(
    session: Session,
    exception_id: int,
    reviewed_correction: dict,
    current_correction: dict,
    note: str | None = None,
    operator_metadata=None,
) -> FulfillmentException:
    """Apply a validated printing correction, then explicitly restore sellability."""
    exception, order, item, allocation, card = _context(session, exception_id)
    if exception.exception_type != "inventory_mismatch":
        raise FulfillmentExceptionError("Exception is not an inventory-mismatch exception.")
    if exception.inventory_resolution_state == "resolved":
        if card.status == "available" and card.inventory_exception_state == "none":
            return exception
        raise FulfillmentExceptionError("Resolved mismatch has inconsistent card state.")
    if exception.inventory_resolution_state != "unresolved":
        raise FulfillmentExceptionError("Unsupported inventory resolution state.")
    if card.status != "unsellable" or card.inventory_exception_state != "exception_unresolved":
        raise FulfillmentExceptionError("Mismatch card is not in the expected quarantined state.")
    if card.unsellable_reason != "fulfillment_inventory_mismatch":
        raise FulfillmentExceptionError("Mismatch quarantine reason changed unexpectedly.")
    if not isinstance(reviewed_correction, dict) or not isinstance(current_correction, dict):
        raise FulfillmentExceptionError("A fresh validated correction preview is required.")

    try:
        apply_printing_correction(session, card, reviewed_correction, current_correction)
        transition_sellability(
            session, card.id, "unsellable", "available",
            card.unsellable_reason, card.unsellable_note,
        )
    except Exception as exc:
        if isinstance(exc, FulfillmentExceptionError):
            raise
        raise FulfillmentExceptionError(f"Validated inventory correction failed: {exc}") from exc

    final_note = _resolution_note(note)
    timestamp = datetime.now(timezone.utc)
    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = final_note
    card.inventory_exception_state = "none"
    _event(session, exception, FULFILLMENT_INVENTORY_CORRECTION_COMPLETED_EVENT,
           "unresolved", "unresolved", final_note, {
               "exception_type": "inventory_mismatch",
               "operator_metadata": operator_metadata,
               "inventory_card_id": card.id,
               "correction_evidence_hash": current_correction.get("evidence_hash"),
           }, timestamp)
    _event(session, exception, FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
           "unresolved", "resolved", final_note, {
               "exception_type": "inventory_mismatch", "inventory_card_id": card.id,
               "allocation_id": allocation.id,
           }, timestamp)
    _projection_audit(session, card, exception, "unsellable", "available", final_note, timestamp)
    session.flush()
    return exception
