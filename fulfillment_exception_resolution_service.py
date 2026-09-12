"""Guarded inventory-side resolution for card-level fulfillment exceptions."""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from fulfillment_exception_constants import (
    FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
    FULFILLMENT_EXCEPTION_MARK_REVERTED_EVENT,
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


# Ticket A (2026-09-12), operator decision: a terminal Mana Pool outcome
# NEVER auto-closes the inventory side. It only unlocks this action, which
# an operator must click and confirm per exception. Nothing here runs
# automatically, on a schedule, or as a side effect of reconciliation.
TERMINAL_REMOTE_STATES_FOR_CLOSE_OUT = frozenset({
    "resolved_fulfilled", "resolved_refunded", "resolved_replaced",
})


def close_out_inventory_after_remote_outcome(
    session: Session,
    exception_id: int,
    note: str | None = None,
    operator_metadata=None,
) -> FulfillmentException:
    """Close out the inventory record for an exception whose Mana Pool
    outcome is already known and terminal.

    This exists because reconciliation and the inventory side were two
    fully decoupled state machines: reconciliation wrote
    remote_resolution_state and never once touched
    inventory_resolution_state, so an exception could be perfectly
    reconciled remotely and still sit unresolved forever (exception #23
    in production: resolved_replaced remotely, unresolved on inventory,
    since 2026-08-28).

    Deliberately does NOT move card.status. The card's own status already
    records what physically happened -- removed for a missing card,
    unsellable for one awaiting a printing decision -- and a remote
    delivery confirmation is evidence about the customer's order, not
    about the physical card. Changing it here would be inventing an
    answer the remote status cannot give. The two type-specific
    resolvers above (resolve_missing_inventory_exception,
    resolve_inventory_mismatch_exception) remain the paths that DO decide
    a card's fate, and this one is not a substitute for either.

    What it does change is exactly the projection pair the invariant
    governs (validate_exception_card_projection): inventory_resolution_
    state -> resolved and the card's inventory_exception_state -> none.
    """
    exception, order, item, allocation, card = _context(session, exception_id)
    if exception.remote_resolution_state not in TERMINAL_REMOTE_STATES_FOR_CLOSE_OUT:
        raise FulfillmentExceptionError(
            "Mana Pool has not reported a terminal outcome for this exception yet."
        )
    if exception.inventory_resolution_state == "resolved":
        raise FulfillmentExceptionError("This exception's inventory record is already closed out.")
    if exception.inventory_resolution_state != "unresolved":
        raise FulfillmentExceptionError("Unsupported inventory resolution state.")
    if card.inventory_exception_state != "exception_unresolved":
        raise FulfillmentExceptionError(
            "Card is not carrying an unresolved exception projection."
        )

    timestamp = datetime.now(timezone.utc)
    remote_status = order.remote_fulfillment_status or "(unknown)"
    remote_resolved_at = (
        exception.remote_resolved_at.isoformat() if exception.remote_resolved_at else "(unrecorded)"
    )
    # The note records WHY this was closed out -- the remote status and
    # when Mana Pool confirmed it -- so the audit trail answers that
    # without needing the order row alongside it.
    final_note = _resolution_note(note) if note else (
        f"Inventory record closed out after Mana Pool reported "
        f"{exception.remote_resolution_state} (order status: {remote_status}, "
        f"confirmed {remote_resolved_at}). Card status left unchanged at "
        f"'{card.status}' -- the remote outcome confirms the customer's order, "
        f"not the physical card's disposition."
    )
    previous_status = card.status
    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = final_note
    card.inventory_exception_state = "none"
    _event(
        session, exception, FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
        "unresolved", "resolved", final_note, {
            "closed_out_after_remote_outcome": exception.remote_resolution_state,
            "remote_fulfillment_status": remote_status,
            "card_status_unchanged": previous_status,
            "operator_metadata": operator_metadata,
            "inventory_card_id": card.id, "allocation_id": allocation.id,
        }, timestamp,
    )
    _projection_audit(
        session, card, exception, previous_status, previous_status, final_note, timestamp,
    )
    session.flush()
    return exception


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


# CF-UNDO-001 item 2: the app's own second-worst incident class this
# session -- an operator mis-marking an exception, with the only fix
# being hand-written database surgery (twice, for a real Paradox Engine
# exception). Distinct from resolve_missing_inventory_exception/resolve_
# inventory_mismatch_exception above: those assume something GENUINE was
# wrong and needed a real resolution. This is for the other case -- the
# exception itself should never have been filed -- and reverses every
# field mark_fulfillment_exception() touched, symmetrically.
UNDOABLE_ORDER_STATUSES = frozenset({"needs_review", "ready_to_pick", "in_pick_wave", "short"})


def revert_fulfillment_exception_mark(
    session: Session,
    exception_id: int,
    note: str,
    operator_metadata=None,
) -> FulfillmentException:
    """Undo a fulfillment exception that was marked in error. Follows
    reopen_pick_wave's own three-part shape (pick_wave_service.py):

    1. All-or-nothing, guarded on exact prior state. _context() already
       requires allocation.status == "exception" (nothing in this
       codebase ever moves an exception allocation anywhere except here,
       substitution, or a real resolution -- so this alone rules out the
       allocation having progressed). On top of that: the exception's
       own submission_state must still be exactly "needs_submission" --
       if it's already "submitted", an operator told Mana Pool about
       this by hand, and silently reverting locally would leave that
       claim stale rather than genuinely undone (see point 2). And the
       ORDER must not have moved past picking -- mark_picked/packed/
       shipped all themselves refuse while any exception on the order
       still needs submission, but cancel_order does NOT check that,
       so a cancelled order can still be sitting on an untouched
       "exception" allocation; reverting it back to "allocated" for a
       dead order would be wrong.
    2. Purely local -- marking an exception never contacts Mana Pool
       (confirmed by reading mark_fulfillment_exception itself), so
       there is nothing external to retract. The submission_state guard
       above is what keeps this honest instead of just locally true.
    3. Writes its own FulfillmentExceptionEvent (a distinct event type,
       FULFILLMENT_EXCEPTION_MARK_REVERTED_EVENT -- never reusing
       FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT, which means "a
       real problem got a real fix") plus its own InventoryChangeLog
       row, rather than silently erasing the mark's own trail.
    """
    exception, order, item, allocation, card = _context(session, exception_id)
    if order.status not in UNDOABLE_ORDER_STATUSES:
        raise FulfillmentExceptionError(
            f"Order has moved to {order.status!r} since the exception was marked; "
            "the mark can no longer be undone."
        )
    if exception.submission_state != "needs_submission":
        raise FulfillmentExceptionError(
            f"This exception's submission state has already progressed to "
            f"{exception.submission_state!r}; the mark can no longer be undone this way."
        )
    if exception.inventory_resolution_state != "unresolved":
        raise FulfillmentExceptionError("This exception has already been resolved and cannot be reverted this way.")

    cleaned_note = str(note or "").strip()
    if not cleaned_note:
        raise FulfillmentExceptionError("A reason is required to undo a fulfillment exception mark.")

    if exception.exception_type == "missing":
        if card.status != "removed" or card.removal_reason != "fulfillment_missing":
            raise FulfillmentExceptionError("Missing-card exception's card is not in the expected removed state.")
    else:
        if card.status != "unsellable" or card.unsellable_reason != "fulfillment_inventory_mismatch":
            raise FulfillmentExceptionError("Mismatch exception's card is not in the expected quarantined state.")

    timestamp = datetime.now(timezone.utc)
    previous_card_status = card.status
    previous_allocation_status = allocation.status
    previous_submission_state = exception.submission_state

    if exception.exception_type == "missing":
        card.status = "reserved"
        card.removal_reason = None
        card.removal_note = None
        card.removed_at = None
    else:
        card.status = "reserved"
        card.unsellable_reason = None
        card.unsellable_note = None
        card.unsellable_at = None
    card.inventory_exception_state = "none"

    allocation.status = "allocated"

    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = cleaned_note
    exception.submission_state = "not_required"

    evidence = {
        "sales_order_id": order.id, "order_item_id": item.id,
        "pick_allocation_id": allocation.id, "inventory_card_id": card.id,
        "exception_type": exception.exception_type,
        "resolution_kind": "operator_reverted_mistaken_mark",
        "previous_card_status": previous_card_status,
        "previous_allocation_status": previous_allocation_status,
        "previous_submission_state": previous_submission_state,
        "note": cleaned_note, "operator_metadata": operator_metadata,
        "timestamp": timestamp.isoformat(),
    }
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=FULFILLMENT_EXCEPTION_MARK_REVERTED_EVENT,
        previous_state="unresolved",
        new_state="resolved",
        note=cleaned_note,
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        evidence_hash=None,
        created_at=timestamp.replace(tzinfo=None),
        operator_metadata=json.dumps(operator_metadata, sort_keys=True, default=str)
        if operator_metadata is not None else None,
    ))
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "fulfillment_exception_mark_reverted",
            "previous_status": previous_card_status, "new_status": card.status,
            "previous_inventory_exception_state": "exception_unresolved",
            "new_inventory_exception_state": "none",
            "fulfillment_exception_id": exception.id,
            "sales_order_id": order.id, "order_item_id": item.id,
            "pick_allocation_id": allocation.id,
            "note": cleaned_note, "timestamp": timestamp.isoformat(),
        }, sort_keys=True),
    ))
    session.flush()
    return exception
