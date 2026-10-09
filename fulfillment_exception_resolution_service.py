"""Guarded inventory-side resolution for card-level fulfillment exceptions."""

import json
import logging
from datetime import datetime, timezone

from actor_context import current_actor
from sqlalchemy.orm import Session

from fulfillment_exception_constants import (
    FOUND_OUTCOME_BACK_TO_ORDER,
    FOUND_OUTCOME_BACK_TO_STOCK,
    FOUND_OUTCOMES,
    FULFILLMENT_EXCEPTION_AUTO_RESOLVED_ON_SUBMISSION_EVENT,
    FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
    FULFILLMENT_EXCEPTION_CARD_FOUND_UNDONE_EVENT,
    FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
    FULFILLMENT_EXCEPTION_MARK_REVERTED_EVENT,
    FULFILLMENT_INVENTORY_CORRECTION_COMPLETED_EVENT,
    REMOTE_STATES_MANA_POOL_HAS_ACTED,
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

logger = logging.getLogger("cardfoundry")


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
        actor=current_actor(),
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
            # This IS the sanctioned resolution path. The exception is
            # still "unresolved" at this instant by design -- it is marked
            # resolved a few lines below -- so the manual-edit guard in
            # transition_sellability would otherwise refuse the very
            # function that exists to clear it.
            allow_open_exception=True,
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

    timestamp = datetime.now(timezone.utc)
    previous_allocation_status = allocation.status
    previous_submission_state = exception.submission_state

    # Shared with mark_reported_card_found's own reversal rather than
    # copied: the two differ in what they MEAN, not in which fields the
    # raise touched. It also performs the expected-state check these two
    # branches used to do separately.
    previous_card_status = _clear_raise_disposition(
        card, exception, target_status="reserved",
    )

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
        actor=current_actor(),
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


def auto_resolve_after_submission(
    session: Session,
    exception_id: int,
    note: str | None = None,
    operator_metadata=None,
) -> FulfillmentException | None:
    """Close the inventory record because the exception was reported to Mana Pool.

    Operator rule, 2026-09-21: *"Once an exception is reported to Mana Pool,
    for all intents and purposes we can count that card exception as
    resolved."* By the time an operator confirms submission they have
    already personally dealt with the physical card; the inventory record
    is bookkeeping catching up to a decision that was made off-system.

    **This explicitly supersedes the Ticket A (2026-09-12) rule stated on
    TERMINAL_REMOTE_STATES_FOR_CLOSE_OUT above** -- "a terminal Mana Pool
    outcome NEVER auto-closes the inventory side ... nothing here runs
    automatically". That rule was guarding against *Mana Pool's* side
    closing a local record with no operator involved, which could bury a
    physical problem nobody had looked at. This trigger is the opposite:
    it fires on an action the operator took by hand, so the human judgement
    Ticket A was protecting has already happened by definition.

    Safe to automate because an exception is not what keeps a card off
    sale. create_fulfillment_exception sets the card's disposition at
    RAISE time, not resolution time -- "missing" goes straight to removed
    /fulfillment_missing, "inventory_mismatch" to unsellable/
    fulfillment_inventory_mismatch. So closing the record here can never
    put a card back on sale or let one be sold twice; it only clears the
    projection pair the invariant governs.

    Card status is left exactly where it is, for the same reason
    close_out_inventory_after_remote_outcome leaves it: what happened to
    the customer's order is not evidence about the physical card.

    Returns None when there is nothing to do, rather than raising -- this
    runs as an automatic consequence of submission, and a submission must
    not fail because its follow-on close was already done.
    """
    exception = session.get(FulfillmentException, exception_id)
    if not exception:
        return None
    if exception.submission_state != "submitted":
        return None
    if exception.inventory_resolution_state != "unresolved":
        return None

    exception, order, item, allocation, card = _context(session, exception_id)
    if card.inventory_exception_state != "exception_unresolved":
        raise FulfillmentExceptionError(
            "Card is not carrying an unresolved exception projection."
        )

    timestamp = datetime.now(timezone.utc)
    submitted_at = (
        exception.submitted_at.isoformat() if exception.submitted_at else "(unrecorded)"
    )
    final_note = _resolution_note(note) if note else (
        f"Inventory record closed automatically because this exception was "
        f"reported to Mana Pool (submitted {submitted_at}). Not an "
        f"operator-verified inventory fix: the card's own status is "
        f"unchanged at '{card.status}' and records what physically "
        f"happened. Mana Pool's outcome for the order was "
        f"'{exception.remote_resolution_state}' at the time of closing."
    )
    previous_status = card.status
    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = final_note
    card.inventory_exception_state = "none"
    _event(
        session, exception, FULFILLMENT_EXCEPTION_AUTO_RESOLVED_ON_SUBMISSION_EVENT,
        "unresolved", "resolved", final_note, {
            "auto_resolved_on": "submission_state=submitted",
            "submitted_at": submitted_at,
            "remote_resolution_state_at_close": exception.remote_resolution_state,
            "remote_fulfillment_status": order.remote_fulfillment_status or "(unknown)",
            "order_status": order.status,
            "card_status_unchanged": previous_status,
            "supersedes": "ticket-a-2026-09-12-no-auto-close",
            "operator_metadata": operator_metadata,
            "inventory_card_id": card.id, "allocation_id": allocation.id,
        }, timestamp,
    )
    session.add(InventoryChangeLog(
        actor=current_actor(),
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "fulfillment_exception_auto_resolved_on_submission",
            "previous_status": previous_status,
            "new_status": previous_status,
            "previous_inventory_exception_state": "exception_unresolved",
            "new_inventory_exception_state": "none",
            "fulfillment_exception_id": exception.id,
            "sales_order_id": exception.sales_order_id,
            "order_item_id": exception.order_item_id,
            "pick_allocation_id": exception.pick_allocation_id,
            "note": final_note,
            "timestamp": timestamp.isoformat(),
        }, sort_keys=True),
    ))
    session.flush()
    return exception


def auto_resolved_on_submission_ids(session: Session, exception_ids) -> set[int]:
    """Of these exceptions, which were closed by submission rather than by
    an operator deciding the card's fate.

    The distinction exists because CF-AUTORESOLVE-001 deliberately did NOT
    add a fourth inventory_resolution_state -- that field is read both as
    == "resolved" and as == "unresolved" in different places, so a new
    value would contradict itself. The provenance lives in the event type
    instead (the CF-UNDO-001 pattern), and this is how a caller asks for
    it without re-deriving the rule.

    Unambiguous by construction: an exception can carry at most one
    closing event, because every resolver refuses or no-ops once
    inventory_resolution_state is already "resolved". So the presence of
    this event type IS how the record was closed.

    One aggregate query, not one per row -- callers hold whole waves.
    """
    ids = [int(value) for value in exception_ids]
    if not ids:
        return set()
    rows = (
        session.query(FulfillmentExceptionEvent.fulfillment_exception_id)
        .filter(
            FulfillmentExceptionEvent.fulfillment_exception_id.in_(ids),
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_AUTO_RESOLVED_ON_SUBMISSION_EVENT,
        )
        .all()
    )
    return {row[0] for row in rows}


# --- slice 7: the card turned up after the exception was reported --------

# An order that can still carry the card to the customer. A shipped or
# cancelled order cannot, and neither can one that never reached picking.
FOUND_BACK_TO_ORDER_ORDER_STATUSES = ("in_pick_wave", "picked", "packed")

# What mark_fulfillment_exception set on the card at RAISE time, per type.
# One table rather than two if/else ladders, because the find and its undo
# have to agree about it exactly.
_EXCEPTION_CARD_DISPOSITION = {
    "missing": ("removed", "removal_reason", "removal_note", "removed_at",
                "fulfillment_missing"),
    "inventory_mismatch": ("unsellable", "unsellable_reason",
                           "unsellable_note", "unsellable_at",
                           "fulfillment_inventory_mismatch"),
}


class RemoteStateContradiction(FulfillmentExceptionError):
    """back_to_order was asked for while OUR RECORD says Mana Pool settled it.

    Its own class rather than a plain error so the route can tell this one
    refusal apart from every other, and offer the override only here. A
    shipped order, a card that has moved, a missing note -- none of those
    are a stale-record problem, so none of them get a second chance.
    """

    def __init__(self, message: str, recorded_remote_state: str | None):
        super().__init__(message)
        self.recorded_remote_state = recorded_remote_state


def _expected_raise_disposition(exception):
    try:
        return _EXCEPTION_CARD_DISPOSITION[exception.exception_type]
    except KeyError:
        raise FulfillmentExceptionError(
            f"Unknown exception type {exception.exception_type!r}.",
        )


def _clear_raise_disposition(card, exception, *, target_status: str) -> str:
    """Undo the card disposition mark_fulfillment_exception applied.

    Shared with revert_fulfillment_exception_mark's own reversal rather than
    copied: the two differ in what they MEAN, not in which fields the raise
    touched, and two copies of that field list is how a later exception type
    gets cleared in one place and not the other.
    """
    status, reason_field, note_field, at_field, _reason = _expected_raise_disposition(
        exception,
    )
    previous_status = card.status
    if previous_status != status or getattr(card, reason_field) != _reason:
        raise FulfillmentExceptionError(
            f"Card is {previous_status!r}, not the {status!r}/{_reason!r} "
            "state this exception left it in; it can no longer be resolved "
            "this way.",
        )
    card.status = target_status
    setattr(card, reason_field, None)
    setattr(card, note_field, None)
    setattr(card, at_field, None)
    card.inventory_exception_state = "none"
    return previous_status


def mark_reported_card_found(
    session: Session,
    exception_id: int,
    *,
    outcome: str,
    note: str,
    override_remote_state_note: str | None = None,
    operator_metadata=None,
) -> dict:
    """The card turned up after the exception had already been reported.

    ★ THE GAP THIS CLOSES. Once an operator presses "Submitted to ManaPool",
    auto_resolve_after_submission closes the inventory record
    (CF-AUTORESOLVE-001), and from that moment
    revert_fulfillment_exception_mark refuses (it requires
    submission_state == "needs_submission" AND an unresolved record) and so
    does confirm_substitution (it requires an unresolved record). So the
    operator had no way at all to act on a card he then found -- which is
    exactly when he finds them, because reporting is what prompts him to
    look again.

    ★ THE REPORT STAYS IN THE RECORD. submission_state is NOT touched.
    Nothing here may claim the report never happened, which is why this
    does not reuse FULFILLMENT_EXCEPTION_MARK_REVERTED_EVENT -- that event
    means the exception should never have been filed. The find gets its own
    event type and the report keeps its own.

    ★ THE OPERATOR CHOOSES WHICH CASE APPLIES, because only he can know:
      back_to_order -- Mana Pool has NOT acted on the line, so the card
          goes back onto the order and ships with it. The card returns to
          "reserved" and its allocation to "picked" (he is holding it; that
          is a pick). Sellable stock does not move, so there is NO Mana
          Pool write.
      back_to_stock -- Mana Pool refunded or replaced the line, so the
          customer's side is settled. The card becomes sellable stock and
          the ORDER IS LEFT ALONE: the allocation stays at "exception",
          which is the historical record of where this card was going.
          Sellable stock goes up by one, so the caller MUST push.

    ★ A CONTRADICTION REFUSES RATHER THAN BEING OVERRIDDEN. If he picks
    back_to_order while our recorded remote state says Mana Pool already
    refunded, replaced or fulfilled the line, putting the card back on that
    order would promise the customer a card they have already been settled
    for. The refusal names the recorded state so he can see what we think
    we know.

    Returns {"cards_to_push": [...]} -- the caller pushes AFTER committing,
    the same contract confirm_substitution has. This function never
    contacts Mana Pool.
    """
    kind = str(outcome or "").strip()
    if kind not in FOUND_OUTCOMES:
        raise FulfillmentExceptionError(
            "Choose whether the card goes back onto the order or back into "
            "sellable stock.",
        )
    cleaned_note = str(note or "").strip()
    if not cleaned_note:
        raise FulfillmentExceptionError(
            "A note is required to record a found card.",
        )

    exception, order, item, allocation, card = _context(session, exception_id)

    override_note = str(override_remote_state_note or "").strip()
    overrode_remote_state = False
    if kind == FOUND_OUTCOME_BACK_TO_ORDER:
        if exception.remote_resolution_state in REMOTE_STATES_MANA_POOL_HAS_ACTED:
            # ★ OVERRIDABLE, BUT ONLY DELIBERATELY (operator decision
            # 2026-10-08). Our record can be stale -- a phone call, an email
            # we have not synced -- and the operator may simply know better
            # than the last state Mana Pool told us. So this refusal offers
            # a second confirm that REQUIRES him to say, in writing, that
            # Mana Pool has not acted. The note is stored in the event, so
            # the override is exactly as auditable as the action it
            # permits, and a later reader can see both what we believed and
            # what he asserted against it.
            if not override_note:
                raise RemoteStateContradiction(
                    "Mana Pool has already settled this line with the "
                    f"customer (recorded as "
                    f"{exception.remote_resolution_state!r}), so the card "
                    "cannot go back onto this order. Put it back into "
                    "sellable stock instead -- or, if you know Mana Pool "
                    "has NOT acted, confirm that below.",
                    exception.remote_resolution_state,
                )
            overrode_remote_state = True
            logger.warning(
                "fulfillment exception %s: operator OVERRODE the recorded "
                "remote state %r to put card %s back on order %s. Their "
                "stated reason: %s",
                exception.id, exception.remote_resolution_state,
                exception.inventory_card_id, order.id, override_note,
            )
        # ★ NOT OVERRIDABLE. A shipped or cancelled order genuinely cannot
        # carry the card to the customer -- that is not a stale record, it
        # is what already happened, so there is nothing for the operator to
        # know better about.
        if order.status not in FOUND_BACK_TO_ORDER_ORDER_STATUSES:
            raise FulfillmentExceptionError(
                f"Order is {order.status!r}, so it can no longer carry this "
                "card to the customer. Put it back into sellable stock "
                "instead.",
            )

    timestamp = datetime.now(timezone.utc)
    previous_allocation_status = allocation.status
    previous_inventory_state = exception.inventory_resolution_state
    target_status = (
        "reserved" if kind == FOUND_OUTCOME_BACK_TO_ORDER else "available"
    )
    previous_card_status = _clear_raise_disposition(
        card, exception, target_status=target_status,
    )

    cards_to_push = []
    if kind == FOUND_OUTCOME_BACK_TO_ORDER:
        allocation.status = "picked"
    else:
        # Left at "exception" deliberately: the order line was settled by
        # Mana Pool, so this allocation is history, not a live claim. It is
        # also what the undo reads to put the card back.
        cards_to_push.append(card)

    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = cleaned_note

    evidence = {
        "sales_order_id": order.id, "order_item_id": item.id,
        "pick_allocation_id": allocation.id, "inventory_card_id": card.id,
        "exception_type": exception.exception_type,
        "outcome": kind,
        "previous_card_status": previous_card_status,
        "previous_allocation_status": previous_allocation_status,
        "previous_inventory_resolution_state": previous_inventory_state,
        # Recorded so the undo can prove nothing moved underneath it, and so
        # the record says what we believed Mana Pool had done at the time.
        "remote_resolution_state_at_find": exception.remote_resolution_state,
        "submission_state_untouched": exception.submission_state,
        # Present ONLY on an overridden find, so an ordinary one's evidence
        # is unchanged and a reader never has to interpret a False.
        **({"remote_state_override": {
            "recorded_remote_state": exception.remote_resolution_state,
            "operator_note": override_note,
        }} if overrode_remote_state else {}),
        "note": cleaned_note, "operator_metadata": operator_metadata,
        "timestamp": timestamp.isoformat(),
    }
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
        previous_state=previous_inventory_state,
        new_state="resolved",
        note=cleaned_note,
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        evidence_hash=None,
        created_at=timestamp.replace(tzinfo=None),
        operator_metadata=json.dumps(operator_metadata, sort_keys=True, default=str)
        if operator_metadata is not None else None,
    ))
    session.add(InventoryChangeLog(
        actor=current_actor(),
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "fulfillment_exception_card_found",
            "outcome": kind,
            **({"remote_state_override_note": override_note}
               if overrode_remote_state else {}),
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
    logger.info(
        "fulfillment exception %s: card %s found (%s); card %s -> %s, "
        "allocation %s -> %s. Report to Mana Pool left untouched (%s).",
        exception.id, card.id, kind, previous_card_status, card.status,
        previous_allocation_status, allocation.status,
        exception.submission_state,
    )
    return {
        "exception": exception, "card": card, "order": order,
        "outcome": kind, "cards_to_push": cards_to_push,
        "overrode_remote_state": overrode_remote_state,
    }


def undo_reported_card_found(
    session: Session,
    exception_id: int,
    note: str,
    operator_metadata=None,
) -> dict:
    """Reverse mark_reported_card_found, symmetrically.

    Reads the BEFORE state out of the find's own event evidence rather than
    re-deriving it, the same contract restore_membership has: a re-derived
    "before" is a guess about history, and the one thing an undo must not do
    is guess.

    Fails closed if anything moved since the find -- the card was sold, it
    was re-allocated, the order shipped. Each of those is somebody else's
    decision resting on the find, and quietly pulling the card back out
    from under it would be worse than refusing.

    Returns {"cards_to_push": [...]}: undoing a back_to_stock find takes a
    sellable card back off the shelf, which is a quantity write in the
    other direction. Undoing a back_to_order find is local only.
    """
    cleaned_note = str(note or "").strip()
    if not cleaned_note:
        raise FulfillmentExceptionError(
            "A reason is required to undo a found card.",
        )

    exception = session.get(FulfillmentException, exception_id)
    if not exception:
        raise FulfillmentExceptionError("Fulfillment exception not found.")
    event = (
        session.query(FulfillmentExceptionEvent)
        .filter(
            FulfillmentExceptionEvent.fulfillment_exception_id == exception.id,
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_EVENT,
        )
        .order_by(FulfillmentExceptionEvent.id.desc())
        .first()
    )
    if event is None:
        raise FulfillmentExceptionError(
            "This exception has no recorded found-card action to undo.",
        )
    undone = (
        session.query(FulfillmentExceptionEvent)
        .filter(
            FulfillmentExceptionEvent.fulfillment_exception_id == exception.id,
            FulfillmentExceptionEvent.event_type
            == FULFILLMENT_EXCEPTION_CARD_FOUND_UNDONE_EVENT,
            FulfillmentExceptionEvent.id > event.id,
        )
        .first()
    )
    if undone is not None:
        raise FulfillmentExceptionError(
            "That found-card action has already been undone.",
        )

    try:
        evidence = json.loads(event.evidence_json or "{}") or {}
    except ValueError as exc:
        # Not swallowed: without the recorded before-state there is nothing
        # honest to restore, so this refuses rather than guessing.
        logger.warning(
            "fulfillment exception %s: found-card event %s has unreadable "
            "evidence and cannot be undone: %s", exception.id, event.id, exc,
        )
        raise FulfillmentExceptionError(
            "The found-card record cannot be read, so it cannot be undone.",
        )

    outcome = evidence.get("outcome")
    card = session.get(InventoryCard, evidence.get("inventory_card_id"))
    allocation = session.get(PickAllocation, evidence.get("pick_allocation_id"))
    order = session.get(SalesOrder, evidence.get("sales_order_id"))
    if not card or not allocation or not order:
        raise FulfillmentExceptionError(
            "The found-card record's linkage is incomplete.",
        )

    expected_card_status = (
        "reserved" if outcome == FOUND_OUTCOME_BACK_TO_ORDER else "available"
    )
    if card.status != expected_card_status:
        raise FulfillmentExceptionError(
            f"Card is now {card.status!r}, not the {expected_card_status!r} "
            "the find left it in; the find can no longer be undone.",
        )
    if outcome == FOUND_OUTCOME_BACK_TO_ORDER:
        if allocation.status != "picked":
            raise FulfillmentExceptionError(
                f"Allocation is now {allocation.status!r}, not the 'picked' "
                "the find left it in; the find can no longer be undone.",
            )
        if order.status not in FOUND_BACK_TO_ORDER_ORDER_STATUSES:
            raise FulfillmentExceptionError(
                f"Order has moved to {order.status!r} since the card was "
                "found; the find can no longer be undone.",
            )

    status, reason_field, note_field, at_field, reason = (
        _expected_raise_disposition(exception)
    )
    timestamp = datetime.now(timezone.utc)
    previous_card_status = card.status
    card.status = status
    setattr(card, reason_field, reason)
    setattr(card, note_field, cleaned_note)
    setattr(card, at_field, timestamp.replace(tzinfo=None))
    card.inventory_exception_state = "exception_unresolved"

    previous_allocation_status = allocation.status
    allocation.status = evidence.get("previous_allocation_status") or "exception"

    exception.inventory_resolution_state = (
        evidence.get("previous_inventory_resolution_state") or "resolved"
    )
    exception.resolution_note = cleaned_note

    cards_to_push = [card] if outcome == FOUND_OUTCOME_BACK_TO_STOCK else []

    undo_evidence = {
        "undone_event_id": event.id,
        "outcome_undone": outcome,
        # Carried forward so an undo of an OVERRIDDEN find is readable as
        # such without having to go and fetch the find's own event.
        **({"undone_remote_state_override": evidence["remote_state_override"]}
           if evidence.get("remote_state_override") else {}),
        "inventory_card_id": card.id,
        "pick_allocation_id": allocation.id,
        "sales_order_id": order.id,
        "restored_card_status": card.status,
        "restored_allocation_status": allocation.status,
        "restored_inventory_resolution_state": exception.inventory_resolution_state,
        "previous_card_status": previous_card_status,
        "previous_allocation_status": previous_allocation_status,
        "note": cleaned_note, "operator_metadata": operator_metadata,
        "timestamp": timestamp.isoformat(),
    }
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=FULFILLMENT_EXCEPTION_CARD_FOUND_UNDONE_EVENT,
        previous_state="resolved",
        new_state=exception.inventory_resolution_state,
        note=cleaned_note,
        evidence_json=json.dumps(undo_evidence, sort_keys=True, default=str),
        evidence_hash=None,
        created_at=timestamp.replace(tzinfo=None),
        operator_metadata=json.dumps(operator_metadata, sort_keys=True, default=str)
        if operator_metadata is not None else None,
    ))
    session.add(InventoryChangeLog(
        actor=current_actor(),
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": "fulfillment_exception_card_found_undone",
            "outcome_undone": outcome,
            "previous_status": previous_card_status, "new_status": card.status,
            "previous_inventory_exception_state": "none",
            "new_inventory_exception_state": "exception_unresolved",
            "fulfillment_exception_id": exception.id,
            "inventory_card_id": card.id,
            "note": cleaned_note, "timestamp": timestamp.isoformat(),
        }, sort_keys=True),
    ))
    session.flush()
    logger.info(
        "fulfillment exception %s: found-card action (%s) undone; card %s "
        "%s -> %s, allocation %s -> %s.",
        exception.id, outcome, card.id, previous_card_status, card.status,
        previous_allocation_status, allocation.status,
    )
    return {
        "exception": exception, "card": card, "outcome_undone": outcome,
        "cards_to_push": cards_to_push,
    }
