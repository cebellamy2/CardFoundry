"""Local-stock substitution for a "missing" fulfillment exception.

Real incident: order 08ba5799-68b9-4396-83f9-af8e42b06107 listed a
Mirrorform in a batch it wasn't physically in. The operator resolved it
by hand, off the normal flow, and the order shipped short. This exists
so a picker can find another copy of the exact same printing in a
different batch, right there in the pick list, without leaving it.

Nothing here is automatic except finding candidates -- the picker always
chooses, and confirming is a guarded transition like every other one in
this codebase: re-fetch fresh, lock, re-validate, raise on drift rather
than trust the page.

Scoped to exception_type == "missing" only, v1. The reused resolution
function (resolve_missing_inventory_exception) hard-requires it, and the
motivating case -- and every dropdown outcome below -- is specifically
about a physically absent card, not a mismatched one. inventory_mismatch
exceptions keep today's flow unchanged.

Candidate rule (confirmed against the real schema, not assumed): one
scryfall_id pins name, set, collector number, AND language (v1.64.0 --
"one scryfall_id = exactly one language, enforced hard at production
-import time"). So the only axes left are finish (must match exactly)
and condition (may be the ordered condition or better, never worse) --
reusing pricing_diagnostic_service.eligible_competitor_conditions
unchanged, the same ladder Competitive Pricing already trusts, not a
new comparison.

Ordering: distance from the ordered condition ascending (an exact match
always outranks any upgrade -- spending a better copy on a lower order
burns margin an exact match wouldn't have), then imported_at ascending
within a condition tier (the existing FIFO precedent, now only ever a
tie-breaker instead of fighting condition for first place).

Consignment: every qualifying candidate is offered, never filtered --
restricting candidates would hide valid stock from a picker under time
pressure, and payout is computed from the shipping card's batch at ship
time regardless (the accounting self-corrects). Only flagged when a
candidate's consignment attribution differs from the original card's
(house vs. a named consignor, or two different consignors) -- the risk
named is surprise, not mis-payment.

Mana Pool: a substitution never contacts Mana Pool itself. The caller
must call manapool_quantity_push_service.push_for_cards(session,
[original_card, substitute_card]) after this commits -- cross-condition
substitution touches two different Mana Pool listings (Mana Pool prices
and lists per condition, confirmed live), so this is not a net-zero
change in the general case. push_for_cards already dedupes by resolved
binding and recomputes fresh per bucket, so one call correctly covers
both the same-bucket and two-bucket cases -- no new push logic here.

Submission state: a filled line has nothing to report to Mana Pool, but
mark_shipped/mark_picked/mark_packed only ever check for the literal
value "needs_submission" (never an exhaustive elif/else, confirmed by
reading every call site) -- so moving to the new "not_required" value
unblocks the order without ever falsely asserting a submission that
never happened. The FulfillmentException row is never deleted and its
inventory_card_id is never repointed -- it stays exactly what it always
was, a record of what went wrong with the original card.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from fulfillment_exception_constants import FULFILLMENT_EXCEPTION_SUBSTITUTED_EVENT
from fulfillment_exception_resolution_service import resolve_missing_inventory_exception
from fulfillment_exception_service import FulfillmentExceptionError, _audit_inventory
from models import (
    Batch, Consignor, FulfillmentException, FulfillmentExceptionEvent,
    InventoryCard, OrderItem, PickAllocation, SalesOrder,
)
from pricing_diagnostic_service import CONDITION_ORDER, eligible_competitor_conditions


SUBSTITUTION_OUTCOMES = frozenset({"remove", "needs_review"})


def _consignor_attribution(session: Session, batch: Batch | None) -> tuple[int | None, str | None]:
    """(consignor_id, consignor_name) for a batch, or (None, None) for
    house stock -- the comparison key for "does this change attribution"."""
    if not batch or not batch.is_consignment or not batch.consignor_id:
        return None, None
    consignor = session.get(Consignor, batch.consignor_id)
    return batch.consignor_id, (consignor.name if consignor else None)


def find_substitution_candidates(session: Session, exception_id: int) -> list[dict]:
    """Every currently-available InventoryCard eligible to replace the
    exceptional card: same scryfall_id, same finish_id, condition this-
    or-better. Ordered exact-condition-first, then oldest-imported-first
    within a tier. Empty list is a legitimate answer, not an error --
    the caller falls back to today's plain exception form."""
    exception = session.get(FulfillmentException, exception_id)
    if not exception or exception.exception_type != "missing":
        return []
    card = session.get(InventoryCard, exception.inventory_card_id)
    if not card or not card.scryfall_id or not card.condition_id:
        return []
    eligible_conditions = eligible_competitor_conditions(card.condition_id)
    if not eligible_conditions:
        return []
    ordered_index = CONDITION_ORDER.index(card.condition_id) if card.condition_id in CONDITION_ORDER else None
    if ordered_index is None:
        return []

    original_batch = session.get(Batch, card.batch_id)
    original_consignor_id, _ = _consignor_attribution(session, original_batch)

    candidates = (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.id != card.id,
            InventoryCard.status == "available",
            InventoryCard.scryfall_id == card.scryfall_id,
            InventoryCard.finish_id == card.finish_id,
            InventoryCard.condition_id.in_(eligible_conditions),
            Batch.is_archived == False,
        )
        .all()
    )

    rows = []
    for candidate in candidates:
        candidate_batch = session.get(Batch, candidate.batch_id)
        candidate_consignor_id, candidate_consignor_name = _consignor_attribution(session, candidate_batch)
        distance = ordered_index - CONDITION_ORDER.index(candidate.condition_id)
        rows.append({
            "card": candidate,
            "batch": candidate_batch,
            "distance": distance,
            "changes_consignment": candidate_consignor_id != original_consignor_id,
            "consignor_name": candidate_consignor_name,
        })
    rows.sort(key=lambda row: (row["distance"], row["card"].imported_at))
    return rows


def confirm_substitution(
    session: Session,
    exception_id: int,
    candidate_card_id: int,
    outcome: str,
    note: str | None = None,
    operator_metadata=None,
) -> dict:
    """Guarded: locks and re-validates both the exception and the chosen
    candidate fresh, then creates a new allocation for the candidate
    (status "picked" -- the picker is confirming this substitute right
    now, in the same motion as picking it), applies `outcome` to the
    original card, and moves the exception's submission_state to
    "not_required". Never contacts Mana Pool -- the caller pushes both
    cards afterward. Caller holds inventory_sync_lease and owns commit.
    """
    exception = session.get(FulfillmentException, exception_id, with_for_update=True)
    if not exception:
        raise FulfillmentExceptionError("Fulfillment exception not found.")
    if exception.exception_type != "missing":
        raise FulfillmentExceptionError("Substitution is only available for a missing-card exception.")
    if exception.inventory_resolution_state != "unresolved":
        raise FulfillmentExceptionError("This exception is already resolved.")
    allocation = session.get(PickAllocation, exception.pick_allocation_id, with_for_update=True)
    if not allocation or allocation.status != "exception":
        raise FulfillmentExceptionError("Exception allocation is no longer in exception state.")
    original_card = session.get(InventoryCard, exception.inventory_card_id, with_for_update=True)
    item = session.get(OrderItem, exception.order_item_id)
    order = session.get(SalesOrder, exception.sales_order_id)
    if not original_card or not item or not order:
        raise FulfillmentExceptionError("Fulfillment exception linkage is incomplete.")

    kind = str(outcome or "").strip()
    if kind not in SUBSTITUTION_OUTCOMES:
        raise FulfillmentExceptionError("Select a valid outcome for the original card.")

    candidate = session.get(InventoryCard, candidate_card_id, with_for_update=True)
    if not candidate:
        raise FulfillmentExceptionError("Substitution candidate not found.")
    if candidate.id == original_card.id:
        raise FulfillmentExceptionError("Substitution candidate must be a different card.")
    if candidate.status != "available":
        raise FulfillmentExceptionError("Substitution candidate is no longer available.")
    if candidate.scryfall_id != original_card.scryfall_id or candidate.finish_id != original_card.finish_id:
        raise FulfillmentExceptionError("Substitution candidate no longer matches the ordered printing.")
    eligible_conditions = eligible_competitor_conditions(original_card.condition_id)
    if candidate.condition_id not in eligible_conditions:
        raise FulfillmentExceptionError("Substitution candidate's condition no longer qualifies.")
    candidate_batch = session.get(Batch, candidate.batch_id)
    if not candidate_batch or candidate_batch.is_archived:
        raise FulfillmentExceptionError("Substitution candidate's batch is no longer active.")

    final_note = str(note or "").strip()
    if not final_note:
        final_note = f"Substituted for card #{original_card.id} — " + datetime.now(timezone.utc).isoformat()
    timestamp = datetime.now(timezone.utc)

    candidate.status = "reserved"
    new_allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=candidate.id,
        batch_id=candidate.batch_id, status="picked",
    )
    session.add(new_allocation)
    session.flush()

    _audit_inventory(
        session, candidate, candidate_batch, exception,
        "available", "reserved", "fulfillment_substitution", final_note, timestamp,
    )

    if kind == "remove":
        resolve_missing_inventory_exception(session, exception.id, final_note, operator_metadata)
    # kind == "needs_review": leave inventory_resolution_state exactly as
    # mark_fulfillment_exception already left it -- no code needed.

    previous_submission_state = exception.submission_state
    exception.submission_state = "not_required"

    evidence = {
        "sales_order_id": order.id,
        "order_item_id": item.id,
        "original_inventory_card_id": original_card.id,
        "substitute_inventory_card_id": candidate.id,
        "new_allocation_id": new_allocation.id,
        "outcome": kind,
        "previous_submission_state": previous_submission_state,
        "note": final_note,
        "operator_metadata": operator_metadata,
        "timestamp": timestamp.isoformat(),
    }
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=FULFILLMENT_EXCEPTION_SUBSTITUTED_EVENT,
        previous_state=previous_submission_state,
        new_state="not_required",
        note=final_note,
        evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        evidence_hash=None,
        created_at=timestamp.replace(tzinfo=None),
        operator_metadata=json.dumps(operator_metadata, sort_keys=True, default=str)
        if operator_metadata is not None else None,
    ))
    session.flush()

    return {"original_card": original_card, "substitute_card": candidate, "exception": exception}
